#!/usr/bin/env python3
"""FSUDAQ front-end for pipeline.py: decode a run's per-board .fsu files, merge the
board streams into one time-ordered hit stream, and drive the same event builder
and classifier that the CoMPASS path uses.

    pipeline_fsu.py RUN_FOLDER [--boards 15880,15879,58918,62839,1443,30508]

Each board's files are decoded in chunks (fsu.py) and merged with a watermark:
everything up to W = min over boards of (newest timestamp buffered) - lag is safe
to emit, because a board never delivers a hit older than what it already gave us
by more than the lag. Blocks are sorted internally; hits that still arrive later
than a block already emitted are counted as "late" (the check that the lag is big
enough). Board ids for the detector map are the position in --boards.
"""
from __future__ import annotations

import argparse
import glob
import multiprocessing as mp
import os
import re
import sys
import time

import numpy as np

import fsu
import pipeline as pl
import online_classify as oc
from detector import AUX_COUNTERS, ChannelMap, parse_planes

try:
    import fsu_fast                      # numba kernels: header walk + fused event builder
    HAVE_FAST = fsu_fast.HAVE_NUMBA
except ImportError:                      # pragma: no cover
    fsu_fast = None
    HAVE_FAST = False
FAST = False                             # set from --fast / --no-fast in main()
HEAP = False                             # fast path: heap merge of per-channel streams instead of a sort (slower, kept for reference)


def _index(words):
    return fsu_fast.index_file(words) if FAST else fsu.index_file(words)

DEFAULT_BOARDS = "15880,15879,58918,62839,1443,30508"   # FSUDAQ port order on odin


def board_files(folder: str) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    for f in glob.glob(os.path.join(folder, "*.fsu")):
        out.setdefault(fsu.serial_from_name(f), []).append(f)
    for sn in out:
        out[sn].sort(key=lambda p: int(re.search(r"_(\d+)\.fsu$", p).group(1)))
    return out


class BoardStream:
    """Chunked decode of one board's files in index order."""

    def __init__(self, serial: int, board_id: int, files: list[str], chunk_hits: int):
        self.serial, self.board_id, self.files, self.chunk_hits = serial, board_id, files, chunk_hits
        self.decode_s = 0.0
        self.hits = 0
        self.bytes = sum(os.path.getsize(f) for f in files)

    def chunks(self):
        for path in self.files:
            t0 = time.perf_counter()
            words = np.memmap(path, dtype="<u4", mode="r")
            ix = _index(words)
            self.decode_s += time.perf_counter() - t0
            if len(ix.nev) == 0:
                continue
            cum = np.cumsum(ix.nev)
            lo = 0
            while lo < len(ix.nev):
                t0 = time.perf_counter()
                target = (int(cum[lo - 1]) if lo else 0) + self.chunk_hits
                hi = max(int(np.searchsorted(cum, target, side="right")), lo + 1)
                d = fsu._decode_chunk(words, ix, lo, hi)
                self.decode_s += time.perf_counter() - t0
                lo = hi
                if d is None:
                    continue
                self.hits += len(d["ch"])
                yield d["ts_ps"].astype(np.int64, copy=False), d["ch"], d["qlong"]


def _decode_proc(stream: "BoardStream", q: "mp.Queue") -> None:
    """One process per board: decode chunks, sort each by time, hand them over."""
    for ts, ch, e in stream.chunks():
        o = np.argsort(ts, kind="stable")
        q.put((ts[o], ch[o], e[o]))
    q.put(("done", stream.decode_s, stream.hits))


class QueueStream:
    """Generator over a board's decode queue; keeps the child's decode time and hit count."""

    def __init__(self, q):
        self.q, self.decode_s, self.hits = q, 0.0, 0

    def __iter__(self):
        while True:
            item = self.q.get()
            if isinstance(item[0], str) and item[0] == "done":
                self.decode_s, self.hits = item[1], item[2]
                return
            yield item


def _cut_at_gap(block: dict, carry: dict | None, window_units):
    """Split so that no event straddles the cut: returns (ready, carry)."""
    if carry is not None and carry["Timestamp"].size:
        block = {k: np.concatenate([carry[k], block[k]]) for k in block}
    ts = block["Timestamp"]
    gaps = np.flatnonzero(ts[1:] - ts[:-1] > window_units)
    if gaps.size == 0:
        return None, block
    cut = int(gaps[-1]) + 1
    return {k: v[:cut] for k, v in block.items()}, {k: v[cut:] for k, v in block.items()}


_W: dict = {}


def _init_worker(cmap, lut, dense, table, window_units, to_ns, fast=False, heap=True):
    global FAST, HEAP
    FAST = fast; HEAP = heap
    _W.update(cmap=cmap, lut=lut, dense=dense, table=table, window_units=window_units, to_ns=to_ns)


def _work(block: dict) -> dict:
    """build + classify one independent block; returns only statistics."""
    res = pl.Result()
    t0 = time.perf_counter()
    ch, tof_ns, aux, n_events = pl.build_events(block, _W["cmap"], _W["lut"], _W["window_units"], _W["to_ns"], res)
    build_s = time.perf_counter() - t0
    t0 = time.perf_counter()
    cls, p, _sigma, tags = pl.classify_vector(ch, tof_ns, aux, _W["dense"])
    classify_s = time.perf_counter() - t0
    codes, n_each = np.unique(cls, return_counts=True)
    good = np.isfinite(p)
    return dict(hits=int(block["Timestamp"].size), events=int(n_events),
                counts=dict(zip(codes.tolist(), n_each.tolist())), tags=tags,
                p_sum=float(p[good].sum()), p_n=int(good.sum()), unmapped=res.unmapped,
                build_s=build_s, classify_s=classify_s)


def _fold(res: pl.Result, r: dict) -> None:
    res.hits += r["hits"]; res.events += r["events"]; res.unmapped += r["unmapped"]
    res.build_s += r["build_s"]; res.classify_s += r["classify_s"]
    for code, n in r["counts"].items():
        res.counts[pl.CLASSES[code]] += n
    for k, v in r["tags"].items():
        res.tags[k] += v
    res.p_sum += r["p_sum"]; res.p_n += r["p_n"]


def iter_merged(streams: list[BoardStream], lag_ps: int, stats: dict, gens: dict | None = None):
    """Watermark k-way merge over the board streams -> pipeline blocks."""
    if gens is None:
        gens = {s.board_id: s.chunks() for s in streams}
    bufs: dict[int, list] = {}          # board_id -> [ts, ch, e] still unmerged
    newest: dict[int, int] = {}         # board_id -> newest ts seen from that board

    def refill(bid) -> bool:
        try:
            ts, ch, e = next(gens[bid])
        except StopIteration:
            return False
        if bid in bufs and bufs[bid][0].size:
            b = bufs[bid]
            bufs[bid] = [np.concatenate([b[0], ts]), np.concatenate([b[1], ch]), np.concatenate([b[2], e])]
        else:
            bufs[bid] = [ts, ch, e]
        newest[bid] = max(newest.get(bid, -1), int(ts.max()))
        return True

    live = set()
    for s in streams:
        if refill(s.board_id):
            live.add(s.board_id)
    prev_max = None
    while live:
        W = min(newest[b] for b in live) - lag_ps
        parts_ts, parts_ch, parts_e, parts_b = [], [], [], []
        t0 = time.perf_counter()
        for bid in list(live):
            ts, ch, e = bufs[bid]
            take = ts <= W
            if take.any():
                parts_ts.append(ts[take]); parts_ch.append(ch[take]); parts_e.append(e[take])
                parts_b.append(np.full(int(take.sum()), bid, np.int16))
                keep = ~take
                bufs[bid] = [ts[keep], ch[keep], e[keep]]
        stats["merge_s"] += time.perf_counter() - t0
        # refill boards whose buffer ran low, so the next watermark can advance
        for bid in list(live):
            if bufs[bid][0].size < 2 or newest[bid] - lag_ps <= W:
                if not refill(bid):
                    if bufs[bid][0].size == 0:
                        live.discard(bid)
                    else:
                        newest[bid] = 1 << 62   # exhausted: what is buffered is all there is
        if not parts_ts:
            if all(newest[b] == 1 << 62 for b in live):
                # every remaining board is exhausted: flush everything
                for bid in list(live):
                    ts, ch, e = bufs[bid]
                    if ts.size:
                        parts_ts.append(ts); parts_ch.append(ch); parts_e.append(e)
                        parts_b.append(np.full(ts.size, bid, np.int16))
                    bufs[bid] = [ts[:0], ch[:0], e[:0]]
                live.clear()
                if not parts_ts:
                    break
            else:
                continue
        t0 = time.perf_counter()
        ts = np.concatenate(parts_ts); order = np.argsort(ts, kind="stable")
        block = {"Timestamp": ts[order], "Channel": np.concatenate(parts_ch)[order],
                 "Board": np.concatenate(parts_b)[order], "Energy": np.concatenate(parts_e)[order]}
        stats["merge_s"] += time.perf_counter() - t0
        if prev_max is not None:
            late = int(np.count_nonzero(block["Timestamp"] < prev_max))
            stats["late"] += late
        prev_max = int(block["Timestamp"][-1])
        stats["blocks"] += 1
        yield block


# ---------------------------------------------------------------------------
# Time-slice parallelism: a worker takes a slice [t0, t1) of the run, decodes the
# files of every board that overlap it, merges, builds and classifies, and keeps
# the events whose first hit lies in the slice. Nothing large crosses a process
# boundary, so it scales with the number of workers. Files at a slice edge are
# decoded by two workers, which is the price of independence.

def _file_span(path: str):
    """(path, first timestamp, last timestamp, n_events) from the first and last board aggregate."""
    words = np.memmap(path, dtype="<u4", mode="r")
    ix = _index(words)
    n = len(ix.nev)
    if n == 0:
        return path, None, None, 0
    first_ctr, last_ctr = ix.agg_counter[0], ix.agg_counter[-1]
    hi0 = int(np.searchsorted(ix.agg_counter != first_ctr, True)) if n > 1 else 1
    lo1 = n - int(np.searchsorted(ix.agg_counter[::-1] != last_ctr, True)) if n > 1 else 0
    d0 = fsu._decode_chunk(words, ix, 0, max(hi0, 1))
    d1 = fsu._decode_chunk(words, ix, min(lo1, n - 1), n)
    return path, int(d0["ts_ps"].min()), int(d1["ts_ps"].max()), int(ix.nev.sum())


def _decode_range(path: str, t_lo: int, t_hi: int, chunk_hits: int):
    """Hits of one file with t_lo <= ts < t_hi, decoded chunk by chunk; stops once
    a chunk lies entirely past t_hi (a file is time-ordered to within an aggregate)."""
    words = np.memmap(path, dtype="<u4", mode="r")
    ix = _index(words)
    out_ts, out_ch, out_e = [], [], []
    if len(ix.nev) == 0:
        return out_ts, out_ch, out_e
    if FAST:
        ts, ch, e = fsu_fast.decode_range(words, ix, t_lo, t_hi)
        if ts.size:
            out_ts.append(ts); out_ch.append(ch); out_e.append(e)
        return out_ts, out_ch, out_e
    cum = np.cumsum(ix.nev)
    lo = 0
    while lo < len(ix.nev):
        target = (int(cum[lo - 1]) if lo else 0) + chunk_hits
        hi = max(int(np.searchsorted(cum, target, side="right")), lo + 1)
        d = fsu._decode_chunk(words, ix, lo, hi)
        lo = hi
        if d is None:
            continue
        ts = d["ts_ps"]
        if ts.min() >= t_hi:
            break
        m = (ts >= t_lo) & (ts < t_hi)
        if m.any():
            out_ts.append(ts[m].astype(np.int64, copy=False)); out_ch.append(d["ch"][m]); out_e.append(d["qlong"][m])
    return out_ts, out_ch, out_e


def _work_slice(task: dict) -> dict:
    """task: t0, t1 (ps), margin (ps), files {board_id: [paths]}, chunk_hits."""
    t0, t1, margin = task["t0"], task["t1"], task["margin"]
    W = _W["window_units"]
    tdec = time.perf_counter()
    parts_ts, parts_ch, parts_e, parts_b = [], [], [], []
    for bid, paths in task["files"].items():
        for p in paths:
            ts_l, ch_l, e_l = _decode_range(p, t0 - margin, t1 + margin, task["chunk_hits"])
            for ts, ch, e in zip(ts_l, ch_l, e_l):
                parts_ts.append(ts); parts_ch.append(ch); parts_e.append(e)
                parts_b.append(np.full(ts.size, bid, np.int16))
    decode_s = time.perf_counter() - tdec
    empty = dict(hits=0, events=0, counts={}, tags={t: 0 for t in pl.TAGS}, p_sum=0.0, p_n=0,
                 unmapped=0, build_s=0.0, classify_s=0.0, decode_s=decode_s, merge_s=0.0)
    if not parts_ts:
        return empty
    tm = time.perf_counter()
    if FAST and HEAP:
        # no sort at all: per-channel streams are time-ordered in the raw file, so a heap
        # merge over them, fused with the event builder, walks the hits in time order
        # with sequential memory access (the sort was memory-bandwidth bound with many workers)
        per_board = {}
        for ts, ch, e, bd in zip(parts_ts, parts_ch, parts_e, parts_b):
            per_board.setdefault(int(bd[0]), []).append((ts, ch, e))
        streams = [(bid, np.concatenate([x[0] for x in lst]), np.concatenate([x[1] for x in lst]),
                    np.concatenate([x[2] for x in lst])) for bid, lst in per_board.items()]
        merge_s = time.perf_counter() - tm
        tb = time.perf_counter()
        ch, tof_ns, aux, n_events, n_bad, n_hits = fsu_fast.merge_build(
            streams, _W["cmap"], _W["lut"], W, _W["to_ns"], t0, t1)
        build_s = time.perf_counter() - tb
    elif FAST:
        # sort once, then one fused pass does event boundaries, slice membership,
        # winner-takes-all and aux flags without materialising a sorted copy
        ts = np.concatenate(parts_ts)
        order = np.argsort(ts)                       # quicksort: ties in ps between boards are astronomically rare
        merge_s = time.perf_counter() - tm
        tb = time.perf_counter()
        ch, tof_ns, aux, n_events, n_bad, n_hits = fsu_fast.build_events(
            ts, np.concatenate(parts_b), np.concatenate(parts_ch), np.concatenate(parts_e), order,
            _W["cmap"], _W["lut"], W, _W["to_ns"], t0, t1)
        build_s = time.perf_counter() - tb
        if n_events == 0:
            empty["decode_s"] = decode_s; empty["merge_s"] = merge_s
            return empty
        tc = time.perf_counter()
        cls, p, _sigma, tags = pl.classify_vector(ch, tof_ns, aux, _W["dense"])
        classify_s = time.perf_counter() - tc
        codes, n_each = np.unique(cls, return_counts=True)
        good = np.isfinite(p)
        return dict(hits=n_hits, events=n_events, counts=dict(zip(codes.tolist(), n_each.tolist())), tags=tags,
                    p_sum=float(p[good].sum()), p_n=int(good.sum()), unmapped=n_bad,
                    build_s=build_s, classify_s=classify_s, decode_s=decode_s, merge_s=merge_s)
    ts = np.concatenate(parts_ts); order = np.argsort(ts, kind="stable")
    block = {"Timestamp": ts[order], "Channel": np.concatenate(parts_ch)[order],
             "Board": np.concatenate(parts_b)[order], "Energy": np.concatenate(parts_e)[order]}
    ts = block["Timestamp"]
    # events start where the gap exceeds the window; an event belongs to this slice
    # if its first hit does, which is why the margin hits were fetched
    starts = pl._starts_new_event(ts, W)
    first_ts = ts[starts]
    keep_ev = (first_ts >= t0) & (first_ts < t1)
    if not keep_ev.any():
        return empty
    ev = np.cumsum(starts) - 1
    keep_hit = keep_ev[ev]
    block = {k: v[keep_hit] for k, v in block.items()}
    merge_s = time.perf_counter() - tm
    r = _work(block)
    r["decode_s"] = decode_s; r["merge_s"] = merge_s
    return r


def run_slices(a, streams, cmap, lut, dense, table, window_units, to_ns, res: pl.Result, stats: dict):
    ctx = mp.get_context("fork")
    all_files = [(s.board_id, f) for s in streams for f in s.files]
    with ctx.Pool(a.workers) as pool:
        spans = pool.map(_file_span, [f for _, f in all_files], chunksize=4)
    by_board: dict[int, list] = {}
    T0, T1 = 1 << 62, 0
    for (bid, f), (_, tf, tl, n) in zip(all_files, spans):
        if tf is None:
            continue
        by_board.setdefault(bid, []).append((tf, tl, f))
        T0, T1 = min(T0, tf), max(T1, tl)
    stats["index_s"] = time.perf_counter() - stats["t_start"]
    slice_ps = int(a.slice_s * 1e12); margin = int(a.lag_ms * 1e9) + int(window_units)
    tasks = []
    t = T0
    while t <= T1:
        t_end = t + slice_ps
        files = {}
        for bid, lst in by_board.items():
            sel = [f for (tf, tl, f) in lst if tl >= t - margin and tf < t_end + margin]
            if sel:
                files[bid] = sel
        tasks.append(dict(t0=t, t1=t_end, margin=margin, files=files, chunk_hits=a.chunk_hits))
        t = t_end
    stats["slices"] = len(tasks)
    stats["files_decoded"] = sum(len(v) for tk in tasks for v in tk["files"].values())
    with ctx.Pool(a.workers, initializer=_init_worker,
                  initargs=(cmap, lut, dense, table, window_units, to_ns, FAST, HEAP)) as pool:
        for r in pool.imap_unordered(_work_slice, tasks):
            _fold(res, r)
            stats["decode_s"] += r["decode_s"]; stats["merge_s"] += r["merge_s"]
    res.beam_seconds = (T1 - T0) * 1e-12


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("folder")
    p.add_argument("--boards", default=DEFAULT_BOARDS, help="serials in detector-map board order")
    p.add_argument("--map", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "momentum_map.csv"))
    p.add_argument("--setting", choices=("high", "low"), default=oc.SETTING)
    p.add_argument("--planes", default="deployed")
    p.add_argument("--board-channels", type=int, default=16)
    p.add_argument("--aux", default=",".join(AUX_COUNTERS))
    p.add_argument("--window", type=float, default=100.0, help="event-building window [ns]")
    p.add_argument("--lag-ms", type=float, default=0.0, help="merge watermark margin [ms]")
    p.add_argument("--chunk-hits", type=int, default=4_000_000, help="decode chunk per board")
    p.add_argument("--max-files-per-board", type=int, default=0, help="0 = all")
    p.add_argument("--backend", choices=("vector", "reference", "io"), default="vector")
    p.add_argument("--order", choices=("check", "sort", "assume"), default="sort",
                   help="a board stream is only time-ordered per channel, so blocks are sorted")
    p.add_argument("--workers", type=int, default=0,
                   help="parallel mode: one decode process per board plus this many build/classify workers (0 = single process)")
    p.add_argument("--slice-s", type=float, default=0.0,
                   help="time-slice parallel mode: each of --workers processes handles whole slices of this many seconds")
    p.add_argument("--fast", dest="fast", action="store_true", default=HAVE_FAST,
                   help="numba kernels for the header walk and event building (default when numba is importable)")
    p.add_argument("--no-fast", dest="fast", action="store_false")
    p.add_argument("--merge", choices=("sort", "heap"), default="sort",
                   help="fast path: argsort of the slice (default; the heap merge of per-channel streams "
                        "is exact too but 2-3x slower under many-worker contention on nothing_011)")
    a = p.parse_args(argv)
    global FAST, HEAP
    FAST = bool(a.fast and HAVE_FAST)
    HEAP = a.merge == "heap"
    if a.fast and not HAVE_FAST:
        print("note: numba not importable, using the numpy path")

    serials = [int(x) for x in a.boards.split(",") if x.strip()]
    files = board_files(a.folder)
    streams = []
    for bid, sn in enumerate(serials):
        fl = files.get(sn, [])
        if a.max_files_per_board:
            fl = fl[:a.max_files_per_board]
        if fl:
            streams.append(BoardStream(sn, bid, fl, a.chunk_hits))
    unknown = sorted(set(files) - set(serials))
    if unknown:
        print(f"note: boards {unknown} are in the folder but not in --boards; ignored")
    if not streams:
        sys.exit("no .fsu files for the listed boards")

    aux = tuple(x.strip() for x in a.aux.split(",") if x.strip())
    cmap = ChannelMap(parse_planes(a.planes), a.board_channels, aux)
    table = oc.load_map(a.map, a.setting)
    cmap, table, dense, lut, window_units, to_ns, unit_ps = pl._prepare(
        table, cmap, a.map, a.setting, "ps", a.window)
    print(f"{len(table)} triplets, setting {a.setting}  |  {cmap.n_channels} channels on "
          f"{cmap.n_boards} boards  |  backend {a.backend}  |  window {a.window} ns  |  lag {a.lag_ms} ms"
          f"  |  kernels {'numba' if FAST else 'numpy'}{' (heap merge)' if FAST and HEAP else ''}")
    for s in streams:
        print(f"  board {s.board_id}: Digi-{s.serial}  {len(s.files)} files  {s.bytes/1e9:.2f} GB")

    res = pl.Result(path=a.folder, bytes=sum(s.bytes for s in streams), n_files=sum(len(s.files) for s in streams))
    stats = {"merge_s": 0.0, "late": 0, "blocks": 0, "decode_s": 0.0, "t_start": time.perf_counter()}
    t_wall = time.perf_counter()
    if a.slice_s > 0:
        run_slices(a, streams, cmap, lut, dense, table, window_units, to_ns, res, stats)
        decode_s = stats["decode_s"]
        span = [None, 0.0]
    elif a.workers <= 0:
        handle = pl._make_handler(res, cmap, lut, dense, table, window_units, to_ns, a.backend, False)
        span = pl._consume(iter_merged(streams, int(a.lag_ms * 1e9), stats), res, handle, window_units, a.order)
        decode_s = sum(s.decode_s for s in streams)
    else:
        ctx = mp.get_context("fork")
        queues = {s.board_id: ctx.Queue(maxsize=3) for s in streams}
        procs = [ctx.Process(target=_decode_proc, args=(s, queues[s.board_id]), daemon=True) for s in streams]
        for pr in procs:
            pr.start()
        qstreams = {s.board_id: QueueStream(queues[s.board_id]) for s in streams}
        gens = {bid: iter(qs) for bid, qs in qstreams.items()}
        pool = ctx.Pool(a.workers, initializer=_init_worker,
                        initargs=(cmap, lut, dense, table, window_units, to_ns, FAST))
        pending: list = []
        carry = None
        span = [None, 0.0]
        for block in iter_merged(streams, int(a.lag_ms * 1e9), stats, gens):
            if span[0] is None:
                span[0] = float(block["Timestamp"][0])
            span[1] = float(block["Timestamp"][-1])
            ready, carry = _cut_at_gap(block, carry, window_units)
            if ready is None or ready["Timestamp"].size == 0:
                continue
            pending.append(pool.apply_async(_work, (ready,)))
            while len(pending) > 2 * a.workers:          # bounded look-ahead keeps memory flat
                _fold(res, pending.pop(0).get())
        if carry is not None and carry["Timestamp"].size:
            pending.append(pool.apply_async(_work, (carry,)))
        for job in pending:
            _fold(res, job.get())
        pool.close(); pool.join()
        for pr in procs:
            pr.join()
        decode_s = sum(qs.decode_s for qs in qstreams.values())
    wall = time.perf_counter() - t_wall
    if span[0] is not None:
        res.beam_seconds = (span[1] - span[0]) * 1e-12
    n_files = sum(len(s.files) for s in streams)

    print()
    if a.slice_s > 0:
        print(f"time-slice parallel: {stats['slices']} slices of {a.slice_s} s on {a.workers} workers; "
              f"indexing {stats['index_s']:.1f} s; {stats['files_decoded']} file decodes for {n_files} files "
              f"(edge redundancy {stats['files_decoded'] / max(n_files, 1) - 1:.0%}); "
              f"decode/merge/build/class below are summed worker CPU seconds")
    elif a.workers > 0:
        print(f"parallel: {len(streams)} decode processes + {a.workers} build/classify workers "
              f"(decode/build/class below are summed CPU seconds; merge is the main process)")
    print(f"{'decode':>8} {'merge':>8} {'build':>8} {'class':>8} {'wall':>8} | {'GB':>6} {'hits':>13} {'events':>12} "
          f"{'MB/s':>7} {'Mhit/s':>7} {'Mevt/s':>7} {'x RT':>6}")
    print(f"{decode_s:8.2f} {stats['merge_s']:8.2f} {res.build_s:8.2f} {res.classify_s:8.2f} {wall:8.2f} | "
          f"{res.bytes/1e9:6.2f} {res.hits:13,} {res.events:12,} {res.bytes/1e6/wall:7.1f} "
          f"{res.hits/1e6/wall:7.2f} {res.events/1e6/wall:7.3f} {res.beam_seconds/max(wall,1e-9):6.2f}")
    if a.slice_s > 0:
        print(f"beam {res.beam_seconds:.2f} s; unmapped: {res.unmapped:,}")
    else:
        print(f"beam {res.beam_seconds:.2f} s in {stats['blocks']} blocks; late hits (arrived after a block "
              f"they belong before): {stats['late']:,}; in-block re-sorted: {res.out_of_order:,}; unmapped: {res.unmapped:,}")
    tot = max(res.events, 1)
    for name in pl.CLASSES:
        n = res.counts[name]
        if n:
            print(f"  {name:<14} {n:>12,}  {n / tot:>6.2%}")
    if res.p_n:
        print(f"  mean momentum where reported: {res.p_sum / res.p_n:.1f} MeV/c ({res.p_n:,} events)")
    tags = {k: v for k, v in res.tags.items() if v}
    if tags:
        print("  tags: " + ", ".join(f"{k}={v:,}" for k, v in tags.items()))


if __name__ == "__main__":
    main()
