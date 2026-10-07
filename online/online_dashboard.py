#!/usr/bin/env python3
"""Live online analysis and dashboard for FSUDAQ runs.

    online_dashboard.py --data-path /home/beamtime/FSUDAQ_data/testFSUDAQ [--port 8050]
    online_dashboard.py --run /path/to/prefix_012                  (one run folder)
    online_dashboard.py --run /path/to/finished_run --replay        (play a finished run at real-time pace)

Follows the run folder FSUDAQ names when a saving run starts (GET /run?folder=..., or
--follow at start-up), else the newest run folder in the data path; a run recorded again
under the same number (old files replaced) starts over. Tails every board's growing .fsu file
(the indexer stops at a partial aggregate, so a file being written is safe to read),
merges the boards with a watermark (everything older than min(newest per board) - lag),
builds bunches and runs the track finder on each (finder_fast.py: a numba fast path that
reproduces finder/online_finder.py exactly, and that reference itself for bunches with two
or more candidates), and
serves http://localhost:<port>/ (the page) and /state (JSON) from a background thread.
Kernels: numba (fsu_fast) when importable, numpy otherwise.

File following borrows from A. Sampat's compass-pseudo-online.py (inotify CREATE /
CLOSE_WRITE to learn about new and finished files, polling the growing file for data
instead of reacting to every MODIFY): when the `inotify` package is importable the
loop wakes on those events, otherwise it polls; a file that is closed and fully
decoded is never indexed again.
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import math
import multiprocessing as mp
import os
import re
import shutil
import signal
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fsu                                   # noqa: E402
import finder_fast as ff                     # noqa: E402  bunch building + track finder (finder/online_finder.py)
import daq_hist as dh                        # noqa: E402  per-channel monitoring (no cabling needed)


def say(msg):
    """Print a status line. Started by FSUDAQ, stdout is a pipe to it; once FSUDAQ is gone a print
    raises, and that must neither stop the analysis nor skip the clean exit (1 Oct 2026: an orphan
    stuck in Python's exit handlers kept port 8050, so the next FSUDAQ had no dashboard)."""
    try:
        print(msg, flush=True)
    except (OSError, ValueError):
        pass
try:
    import fsu_fast
    FAST = fsu_fast.HAVE_NUMBA
except ImportError:
    fsu_fast = None
    FAST = False
try:
    import inotify.adapters
    import inotify.constants
    HAVE_INOTIFY = True
except ImportError:
    HAVE_INOTIFY = False

DEFAULT_BOARDS = "15880,15879,58918,62839,1443,30508"
EIGHT_CHANNEL_BOARDS = "1443,30508"           # the DT5730s
HISTORY_S = 600                               # seconds of rate history kept


def _index(words):
    return fsu_fast.index_file(words) if FAST else fsu.index_file(words)


def _decode(words, ix):
    if FAST:
        return fsu_fast.decode_range(words, ix, short=True)
    d = fsu._decode_chunk(words, ix, 0, len(ix.nev))
    if d is None:
        return np.zeros(0, np.int64), np.zeros(0, np.uint8), np.zeros(0, np.uint16), np.zeros(0, np.uint16)
    return d["ts_ps"].astype(np.int64), d["ch"], d["qlong"], d["qshort"]


# ---------------------------------------------------------------- slice processing (in a worker)
# Same scheme as pipeline_fsu.py's time-slice mode: a worker decodes the files that
# overlap its slice [t0, t1) with a margin on both sides, sorts, builds events and keeps
# those whose FIRST hit lies in the slice. Nothing large crosses a process boundary and
# no event is counted twice at a boundary.
_G: dict = {}


def _init_worker(tables, chans, window_ps):
    _G.update(tables=tables, chans=chans, window_ps=window_ps)


def _decode_file_range(path, t_lo, t_hi):
    words = np.memmap(path, dtype="<u4", mode="r")
    ix = _index(words)
    if len(ix.nev) == 0:
        return None
    if FAST:
        return fsu_fast.decode_range(words, ix, t_lo, t_hi, short=True)
    ts, ch, e, qs = _decode(words, ix)
    m = (ts >= t_lo) & (ts < t_hi)
    return ts[m], ch[m], e[m], qs[m]


def process_slice(task: dict) -> dict:
    """task: t0, t1, margin (ps), files {board_id: [paths]}. Returns increments only: the finder's
    histograms and counters for the bunches whose first hit lies in [t0, t1)."""
    t_start = time.perf_counter()
    t0, t1, margin = task["t0"], task["t1"], task["margin"]
    tables, chans, window_ps = _G["tables"], _G["chans"], _G["window_ps"]
    parts = []
    hits_by_board = {}
    for bid, paths in task["files"].items():
        for p in paths:
            try:
                r = _decode_file_range(p, t0 - margin, t1 + margin)
            except (ValueError, OSError):
                continue
            if r is None or r[0].size == 0:
                continue
            ts, ch, e, qs = r
            parts.append((ts, ch, e, np.full(ts.size, bid, np.int16), qs))
            hits_by_board[bid] = hits_by_board.get(bid, 0) + int(np.count_nonzero((ts >= t0) & (ts < t1)))
    ref = task.get("ref")
    if parts:
        ts = np.concatenate([p[0] for p in parts]); ch = np.concatenate([p[1] for p in parts])
        e = np.concatenate([p[2] for p in parts]); bd = np.concatenate([p[3] for p in parts])
        qs = np.concatenate([p[4] for p in parts])
        order = np.argsort(ts)
        res = ff.process_sorted(tables, chans, ts, bd, ch, e, order, window_ps, t0, t1)
        slot = bd.astype(np.int64) * chans.board_channels + ch.astype(np.int64)
        daq = dh.process(ts, slot, e, qs, order, t0, t1, ref, chans.n_slots)
    else:
        res = ff.Result(chans.n_slots)
        daq = dh.DaqResult(chans.n_slots, np.uint32)
    out = res.to_dict()
    out.update(W=t1, hits_by_board=hits_by_board, daq=daq.to_dict(), ref=ref, dt=time.perf_counter() - t_start)
    return out


# ---------------------------------------------------------------- following the files (main process)
def _first_last(words, ix):
    """(first ts, last ts) of an indexed file from its first and last board aggregate only."""
    n = len(ix.nev)
    first_ctr, last_ctr = ix.agg_counter[0], ix.agg_counter[-1]
    hi0 = int(np.searchsorted(ix.agg_counter != first_ctr, True)) if n > 1 else 1
    lo1 = n - int(np.searchsorted(ix.agg_counter[::-1] != last_ctr, True)) if n > 1 else 0
    d0 = fsu._decode_chunk(words, ix, 0, max(hi0, 1))
    d1 = fsu._decode_chunk(words, ix, min(lo1, n - 1), n)
    return int(d0["ts_ps"].min()), int(d1["ts_ps"].max())


class BoardFiles:
    """What is on disk for one board: per-file time spans, the newest timestamp, sizes."""

    def __init__(self, serial: int, board_id: int):
        self.serial, self.board_id = serial, board_id
        self.files: list[str] = []
        self.span: dict[str, list] = {}         # file -> [first_ts, last_ts, complete]
        self.bytes = 0
        self.newest = None
        self.newest_wall = time.time()
        self.skipped_start = False
        self.hits_seen = 0                      # couple aggregates x events, for the display only

    def scan(self, folder: str, closed=None, from_start=True):
        pat = os.path.join(folder, f"*_{self.serial}_*.fsu")
        files = sorted(glob.glob(pat), key=lambda p: int(re.search(r"_(\d+)\.fsu$", p).group(1)))
        self.files = files
        self.bytes = sum(os.path.getsize(f) for f in files)
        for k, f in enumerate(files):
            sp = self.span.get(f)
            if sp is not None and sp[2]:
                continue                        # complete file, nothing changes
            is_last = k == len(files) - 1
            if not from_start and not self.skipped_start and not is_last:
                self.span[f] = [None, None, True]   # live start: older files are ignored
                continue
            try:
                if os.path.getsize(f) < 16:
                    continue
                words = np.memmap(f, dtype="<u4", mode="r")
                ix = _index(words)
            except (ValueError, OSError):
                continue
            if len(ix.nev) == 0:
                continue
            first, last = _first_last(words, ix)
            complete = (not is_last) or (closed is not None and f in closed)
            if sp is None or sp[1] != last:
                self.span[f] = [first, last, complete and ix.truncated_words == 0]
                if self.newest is None or last > self.newest:
                    self.newest = last; self.newest_wall = time.time()
                self.hits_seen = int(ix.nev.sum())
            elif complete:
                sp[2] = True
        self.skipped_start = True

    def files_for(self, t_lo, t_hi):
        return [f for f in self.files if (sp := self.span.get(f)) and sp[0] is not None
                and sp[1] >= t_lo and sp[0] < t_hi]


class Online:
    def __init__(self, a):
        self.a = a
        self.serials = [int(x) for x in a.boards.split(",") if x.strip()]
        eight = {int(x) for x in a.eight_channel_boards.split(",") if x.strip()}
        nch = {s: (8 if s in eight else 16) for s in self.serials}
        if a.cabling:
            rows, self.cabling_src = ff.load_cabling(a.cabling), a.cabling
        else:
            rows, self.cabling_src = ff.default_cabling(self.serials, nch), "placeholder (sequential)"
        gains = ff.load_per_channel(a.gains, "mevee_per_count") if a.gains else None
        offs = ff.load_per_channel(a.time_offsets, "offset_ns") if a.time_offsets else None
        self.tables = ff.Tables(a.setting, map_path=a.map, calibrated=gains is not None)
        self.chans = ff.Channels(self.serials, rows, gains, offs)
        self.window_ps = int(round(a.window * 1000))
        # digitizer channels that exist, and how to call them; the role only when a cabling file says so
        self.slots, self.labels = [], {}
        for bi, sn in enumerate(self.serials):
            for c in range(nch[sn]):
                sl = bi * self.chans.board_channels + c
                role = ""
                if a.cabling:
                    pl_, chn, cn = self.chans.plane[bi, c], self.chans.ch[bi, c], self.chans.counter[bi, c]
                    role = f"T{pl_}:{chn}" if pl_ >= 0 else (ff.COUNTERS[cn] if pl_ == -1 else "")
                self.slots.append(sl)
                self.labels[sl] = dict(board=sn, ch=c, role=role)
        self.ref_slot = a.ref_slot if a.ref_slot is not None else None
        self.lag_ps = int(a.lag_ms * 1e9)
        self.margin = self.lag_ps + self.window_ps
        self.lock = threading.Lock()
        self.pool = None
        if a.workers > 0:
            ctx = mp.get_context("fork")
            self.pool = ctx.Pool(a.workers, initializer=_init_worker,
                                 initargs=(self.tables, self.chans, self.window_ps))
        else:
            _init_worker(self.tables, self.chans, self.window_ps)
        self.pending: list = []
        self.follow = a.follow                  # run folder FSUDAQ named (--follow or GET /run); None = newest in the data path
        self.restart = False                    # set by GET /run: a new run starts, even in the same folder
        self.reset(None)

    def reset(self, folder):
        self.folder = folder
        self.boards = {sn: BoardFiles(sn, i) for i, sn in enumerate(self.serials)}
        self.W = None
        self.t_first = None
        self.acc = ff.Result(self.chans.n_slots)      # the run's histograms and counters
        self.daq = dh.DaqResult(self.chans.n_slots)   # per-channel histograms, since the run start or Clear
        self.cleared_beam_s = 0.0
        self.history = collections.deque()
        self.proc_s = 0.0; self.steps = 0
        self.replay_t0 = None
        self.flushed = False
        self.status = "waiting for data"
        self.closed: set[str] = set()
        self.pending = []                       # slices of the previous run still in the workers: their results are dropped
        self.first_files = {}                   # first file of each board -> inode: a re-recorded run replaces them
        self.wake = threading.Event()
        if folder and HAVE_INOTIFY and not self.a.replay:
            threading.Thread(target=self._watch, args=(folder,), daemon=True).start()

    def _watch(self, folder):
        """inotify: wake the loop when a file is created or closed by the writer."""
        try:
            notifier = inotify.adapters.Inotify()
            notifier.add_watch(folder, mask=inotify.constants.IN_CREATE | inotify.constants.IN_CLOSE_WRITE)
            for _, types, path, name in notifier.event_gen(yield_nones=False):
                if folder != self.folder:
                    return
                if "IN_CLOSE_WRITE" in types:
                    self.closed.add(os.path.join(path, name))
                self.wake.set()
        except Exception as ex:
            say(f"inotify watcher stopped: {ex!r}")

    def step(self):
        a = self.a
        folder = a.run or self.follow or newest_run_folder(a.data_path)
        if folder != self.folder or self.restart or (folder and self._run_replaced(folder)):
            self.restart = False
            with self.lock:
                self.reset(folder)
        if folder is None:
            self.status = f"no run folder under {a.data_path}"
            return
        for b in self.boards.values():
            b.scan(folder, closed=self.closed if HAVE_INOTIFY else None, from_start=a.replay or a.from_start)
        self._collect()
        active = [b for b in self.boards.values() if b.newest is not None]
        if not active:
            self.status = "no data yet"
            return
        if self.t_first is None:
            self.t_first = min(sp[0] for b in active for sp in b.span.values() if sp[0] is not None)
            self.replay_t0 = time.time()
        now = time.time()
        newest = {b.board_id: b.newest for b in active}
        if a.replay:
            limit = self.t_first + int((now - self.replay_t0) * a.replay_speed * 1e12)
            newest = {k: min(v, limit) for k, v in newest.items()}
            live = [b for b in active if newest[b.board_id] < b.newest or now - b.newest_wall < a.stall_s]
        else:
            live = [b for b in active if now - b.newest_wall < a.stall_s]
        if not live:
            if not self.flushed and self.W is not None:
                self._submit(self.W, np.int64(1) << 62, now)
                self.flushed = True
            self.status = "run ended, buffers flushed" if self.flushed else "all boards stalled"
            return
        self.flushed = False
        W = min(newest[b.board_id] for b in live) - self.lag_ps
        if self.W is not None and W - self.W < int(a.min_step_s * 1e12):
            self.status = "waiting for the watermark to advance"
            return
        t_lo = self.W if self.W is not None else self.t_first
        self._submit(t_lo, W, now)
        self.W = W

    def _run_replaced(self, folder):
        """True when a first file seen earlier is gone or is a new file: the run number was recorded
        again in the same folder (FSUDAQ deletes the old files on "Overwrite"). New boards' files are fine."""
        now = {}
        for f in glob.glob(os.path.join(folder, "*_000.fsu")):
            try:
                now[f] = os.stat(f).st_ino
            except OSError:
                pass
        replaced = any(now.get(f) != ino for f, ino in self.first_files.items())
        if not replaced:
            self.first_files.update(now)
        return replaced

    def _submit(self, t0, t1, now):
        files = {}
        for b in self.boards.values():
            sel = b.files_for(t0 - self.margin, t1 + self.margin)
            if sel:
                files[b.board_id] = sel
        if not files:
            return
        task = dict(t0=int(t0), t1=int(t1), margin=int(self.margin), files=files, ref=self.ref_slot)
        bytes_by_board = {b.board_id: b.bytes for b in self.boards.values()}
        if self.pool is None:
            self._fold(process_slice(task), now, bytes_by_board)
        else:
            while len(self.pending) >= 2 * self.a.workers:   # backpressure: lag grows, the page says so
                self.pending[0][1].wait(0.2); self._collect()
            self.pending.append((t1, self.pool.apply_async(process_slice, (task,)), now, bytes_by_board))
        self.status = "running"

    def _collect(self):
        while self.pending and self.pending[0][1].ready():
            W, ar, now, bytes_by_board = self.pending.pop(0)
            try:
                self._fold(ar.get(), now, bytes_by_board)
            except Exception as ex:
                say(f"slice failed: {ex!r}")

    def _fold(self, r: dict, now, bytes_by_board):
        with self.lock:
            for k in ff.Result.ARRAYS:
                getattr(self.acc, k)[...] += r[k]
            for k in ff.Result.SCALARS:
                setattr(self.acc, k, getattr(self.acc, k) + r[k])
            top = max(b.newest for b in self.boards.values() if b.newest is not None)
            beam_s = (min(r["W"], top) - self.t_first) * 1e-12
            status = {ff.STATUS[i]: int(c) for i, c in enumerate(r["h_status"]) if c}
            n_trk = int((r["h_ntrk"] * np.arange(ff.NTRK)).sum())
            d = r["daq"]
            if r["ref"] != self.ref_slot:         # made before the reference channel changed: keep all but dt
                d = dict(d); d["dt"] = np.zeros_like(d["dt"])
            self.daq.add(d)
            self.history.append((now, beam_s, r["hits_by_board"], bytes_by_board, int(r["n_bunches"]), status, n_trk,
                                 d["counts"]))
            while self.history and now - self.history[0][0] > HISTORY_S:
                self.history.popleft()
            self.proc_s += r["dt"]; self.steps += 1

    def state(self) -> dict:
        with self.lock:
            hist = list(self.history)
            boards = []
            for sn, b in self.boards.items():
                boards.append(dict(serial=sn, id=b.board_id, files=len(b.files), bytes=b.bytes, hits=b.hits_seen,
                                   newest_s=None if b.newest is None or self.t_first is None else (b.newest - self.t_first) * 1e-12,
                                   stalled=b.newest is not None and time.time() - b.newest_wall >= self.a.stall_s,
                                   buffered=0))
            A, ch = self.acc, self.chans
            occ = [[0] * (int(n) + 1) for n in (ch.ch[ch.plane == p].max(initial=0) for p in range(3))]
            counters = {}
            for bi in range(ch.plane.shape[0]):
                for c in range(ch.plane.shape[1]):
                    n = int(A.occ[bi * ch.board_channels + c])
                    if ch.plane[bi, c] >= 0:
                        occ[ch.plane[bi, c]][ch.ch[bi, c]] += n
                    elif ch.plane[bi, c] == -1:
                        name = ff.COUNTERS[ch.counter[bi, c]]
                        counters[name] = counters.get(name, 0) + n
            tb = self.tables
            return dict(
                folder=self.folder, status=self.status, kernels="numba" if FAST else "numpy",
                workers=self.a.workers, in_flight=len(self.pending),
                replay=bool(self.a.replay), lag_ms=self.a.lag_ms, window_ns=self.a.window,
                processed_s=None if self.W is None or self.t_first is None else (self.W - self.t_first) * 1e-12,
                hits=A.n_hits, bunches=A.n_bunches, unmapped=A.n_bad,
                proc_s=self.proc_s, steps=self.steps,
                boards=boards,
                finder=dict(setting=tb.setting, map_rows=tb.map_n, calibrated=tb.calibrated, cabling=self.cabling_src,
                            installed=[c for c, i in zip(ff.COUNTERS, ch.installed) if i],
                            missing=[c for c, i in zip(ff.COUNTERS, ch.installed) if not i],
                            channels=[len(o) - 1 for o in occ], timing="offsets file" if self.a.time_offsets else "no offsets",
                            fast=int(A.n_fast[0]), reference=A.n_slow, reference_s=A.slow_s, overflow=A.n_over,
                            k=None if math.isinf(tb.k) else tb.k, nsig=tb.nsig, threshold=tb.threshold),
                statuses=list(ff.STATUS),
                status_counts={ff.STATUS[i]: int(c) for i, c in enumerate(A.h_status)},
                history=[dict(wall=h[0], beam_s=h[1], hits=h[2], bytes=h[3], bunches=h[4], status=h[5], tracks=h[6])
                         for h in hist],
                hist_p=dict(edges=ff.P_EDGES.tolist(), counts=A.h_p.tolist()),
                hist_res=dict(edges=ff.RES_EDGES.tolist(), t10=A.h_res[0].tolist(), t21=A.h_res[1].tolist()),
                hist_dt=dict(edges=ff.DT_EDGES.tolist(), counts=A.h_dt.tolist()),
                hist_r=dict(edges=ff.R_EDGES.tolist(), counts=A.h_r.tolist()),
                hist_cl=A.h_cl.tolist(), hist_ncand=A.h_ncand.tolist(), hist_ntrk=A.h_ntrk.tolist(),
                hist_nhit=A.h_nhit.tolist(), lucite=dict(yes=int(A.h_lucite[0]), no=int(A.h_lucite[1])),
                occupancy=occ, counters=counters,
                daq=self._daq_state(hist, boards),
            )

    def _daq_state(self, hist, boards):
        """The DAQ tab: per-channel counts and rates, data volume and disk, how much has been processed."""
        D = self.daq
        rate = {}
        if len(hist) >= 2:                        # the newest step's counts over its beam time
            dt_s = hist[-1][1] - hist[-2][1]
            if dt_s > 0:
                rate = {sl: float(hist[-1][7][sl]) / dt_s for sl in self.slots}
        newest = max((b["newest_s"] or 0) for b in boards) if boards else 0
        if self.a.replay and self.replay_t0 is not None:   # a replay only "has" the data up to its clock
            newest = min(newest, (time.time() - self.replay_t0) * self.a.replay_speed)
        processed = None if self.W is None or self.t_first is None else (self.W - self.t_first) * 1e-12
        # keeping up: the lag now against the lag 30 s of wall time ago
        lag = None if processed is None else max(0.0, newest - processed)
        try:
            du = shutil.disk_usage(self.folder or self.a.data_path or "/")
            disk = dict(free_gb=du.free / 1e9, total_gb=du.total / 1e9)
        except OSError:
            disk = None
        run_bytes = sum(b["bytes"] for b in boards)
        files = sum(b["files"] for b in boards)
        return dict(
            slots=[dict(slot=sl, **self.labels[sl], count=int(D.counts[sl]), rate=rate.get(sl)) for sl in self.slots],
            ref=self.ref_slot, total_rate=sum(rate.values()) if rate else None,
            processed_pct=None if processed is None or newest <= 0 else min(100.0, 100.0 * processed / newest),
            lag_s=lag, in_flight=len(self.pending), backpressure=len(self.pending) >= 2 * max(1, self.a.workers),
            run_bytes=run_bytes, files=files, disk=disk, n_windows=D.n_seg,
            history=[dict(beam_s=h[1], total=int(sum(int(h[7][sl]) for sl in self.slots))) for h in hist])

    def channel(self, sl, view=None):
        """One channel's histograms for the DAQ tab (sl = -1: all channels together, spectrum only).
        view = (lo, hi, nb, log): the Qlong range [lo, hi) and binning the page shows."""
        with self.lock:
            D, hist = self.daq, list(self.history)
            f = 0.5 * (dh.F_EDGES[:-1] + dh.F_EDGES[1:])
            if sl == -1:
                n = float(D.spec_tot_n[0])
                return dict(slot=-1, spectrum=dict(f=f.tolist(), p=(D.spec_tot_p / n if n else D.spec_tot_p).tolist()),
                            windows=D.n_seg)
            if sl not in self.labels:
                return None
            n = float(D.spec_n[sl])
            rates = []
            for a, b in zip(hist[:-1], hist[1:]):
                if b[1] > a[1]:
                    rates.append([b[1], float(b[7][sl]) / (b[1] - a[1])])
            lo, hi, nb, log = view or (0, 65536, 4096, False)
            e = dh.q_edges(lo, hi, nb, log)
            row = D.q[sl]
            tot = int(row.sum())
            inside = dh.rebin(row, np.array([0, 65536]))[0]          # all but Q = 0 and 65535
            shown = dh.rebin(row, e)
            # PSD map: the Qlong bins that overlap the shown range
            i0 = max(0, int(np.searchsorted(dh.PX_EDGES, e[0], side="right")) - 1)
            i1 = min(dh.NPX, int(np.searchsorted(dh.PX_EDGES, e[-1], side="left")))
            P = D.psd[sl, i0:i1]
            return dict(slot=sl, **self.labels[sl], count=int(D.counts[sl]), ref=self.ref_slot,
                        q=dict(edges=e.tolist(), counts=shown.tolist(), total=tot, zero=int(row[0]), sat=int(row[65535]),
                               outside=int(inside - shown.sum()), log=bool(log)),
                        psd=dict(xedges=dh.PX_EDGES[i0:i1 + 1].tolist(), ny=dh.NPY, counts=P.tolist(),
                                 n=int(D.psd[sl].sum()), out=int(D.psd_out[sl])),
                        iat=dict(edges=dh.IAT_EDGES.tolist(), counts=D.iat[sl].tolist()),
                        dt=dict(edges=dh.DT_EDGES.tolist(), counts=D.dt[sl].tolist()),
                        spectrum=dict(f=f.tolist(), p=(D.spec_p[sl] / n if n else D.spec_p[sl]).tolist()),
                        windows=D.n_seg, rate=rates)

    def grid(self, view):
        """Every channel's Qlong spectrum over the shown range, coarsely binned, for the overview grid."""
        lo, hi, nb, log = view
        e = dh.q_edges(lo, hi, nb, log)
        with self.lock:
            C = dh.rebin(self.daq.q, e)[self.slots]                  # no copy of the full array
            return dict(edges=e.tolist(), log=bool(log),
                        slots=[dict(slot=sl, **self.labels[sl], counts=C[i].tolist()) for i, sl in enumerate(self.slots)])

    def clear(self):
        with self.lock:
            self.acc = ff.Result(self.chans.n_slots)
            self.daq = dh.DaqResult(self.chans.n_slots)

    def set_ref(self, sl):
        with self.lock:
            self.ref_slot = sl if sl in self.labels else None
            self.daq.dt[...] = 0


def newest_run_folder(data_path: str):
    cands = [d for d in glob.glob(os.path.join(data_path, "*")) if os.path.isdir(d) and glob.glob(os.path.join(d, "*.fsu"))]
    if not cands:
        return None
    return max(cands, key=os.path.getmtime)


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    online: Online = None
    page: bytes = b""

    def log_message(self, *args):          # keep the console for the analysis
        pass

    def do_GET(self):
        if self.path.startswith("/open"):        # FSUDAQ asks us to show the page (it must not spawn anything itself)
            import webbrowser
            threading.Thread(target=lambda: webbrowser.open(f"http://localhost:{self.server.server_address[1]}/"), daemon=True).start()
            self.send_response(204); self.end_headers(); return
        if self.path.startswith("/run"):         # FSUDAQ starts a saving run: follow its folder from now on
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            folder = q.get("folder", [""])[0]
            if not folder:
                self.send_response(400); self.end_headers(); return
            self.online.follow = folder
            self.online.restart = True
            self.online.wake.set()
            say(f"{time.strftime('%H:%M:%S')} following run folder {folder}")
            self.send_response(204); self.end_headers(); return
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if self.path.startswith("/clear"):      # the page's Clear: per-channel and finder histograms start again
            self.online.clear()
            self.send_response(204); self.end_headers(); return
        if self.path.startswith("/set"):        # reference channel for the time differences
            try:
                self.online.set_ref(int(q.get("ref", ["-1"])[0]))
            except ValueError:
                self.send_response(400); self.end_headers(); return
            self.send_response(204); self.end_headers(); return
        if self.path.startswith("/state"):
            body = json.dumps(self.online.state(), allow_nan=False).encode()   # Infinity/NaN are not JSON
            ctype = "application/json"
        elif self.path.startswith("/channel") or self.path.startswith("/grid"):
            try:
                view = None
                if "lo" in q:
                    view = (int(q["lo"][0]), int(q["hi"][0]), int(q.get("nb", ["4096"])[0]), q.get("log", ["0"])[0] == "1")
                if self.path.startswith("/grid"):
                    d = self.online.grid(view or (0, 65536, 64, False))
                else:
                    d = self.online.channel(int(q.get("slot", ["-1"])[0]), view)
            except (ValueError, KeyError):
                d = None
            if d is None:
                self.send_response(404); self.end_headers(); return
            body = json.dumps(d, allow_nan=False).encode()
            ctype = "application/json"
        elif self.path == "/" or self.path.startswith("/index"):
            try:                                   # re-read so a page update needs no restart
                with open(os.path.join(HERE, "dashboard.html"), "rb") as fh:
                    body = fh.read()
            except OSError:
                body = self.page
            ctype = "text/html; charset=utf-8"
        else:
            self.send_response(404); self.end_headers(); return
        self.send_response(200)
        self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers(); self.wfile.write(body)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--data-path", help="FSUDAQ data path; the newest run folder in it is followed")
    p.add_argument("--run", help="one run folder instead of following the data path")
    p.add_argument("--follow", help="run folder to follow until FSUDAQ names the next one (GET /run?folder=...)")
    p.add_argument("--replay", action="store_true", help="play a finished run at real-time pace")
    p.add_argument("--replay-speed", type=float, default=1.0)
    p.add_argument("--boards", default=DEFAULT_BOARDS, help="serials in detector-map board order")
    p.add_argument("--map", default=None, help="momentum map (default finder/momentum_map.csv); columns per finder/INTERFACE.md")
    p.add_argument("--setting", choices=("high", "low"), default="high", help="T1/T2 position installed for this run")
    p.add_argument("--cabling", help="CSV serial,channel,kind,plane,ch (kind trk/veto/cherenkov/lucite); "
                                     "default: a sequential placeholder")
    p.add_argument("--gains", help="CSV serial,channel,mevee_per_count; without it the light tests are off")
    p.add_argument("--time-offsets", help="CSV serial,channel,offset_ns subtracted from each channel's time")
    p.add_argument("--eight-channel-boards", default=EIGHT_CHANNEL_BOARDS, help="serials of 8-channel boards")
    p.add_argument("--ref-slot", type=int, default=None,
                   help="reference channel for the time differences (board index x 16 + channel); the page can change it")
    p.add_argument("--window", type=float, default=25.0,
                   help="bunch building: a gap longer than this starts a new bunch [ns]. 25 = the finder's +-10 ns gate "
                        "around each plane's expected arrival plus the 4.3 ns T0->T2 flight at beta 0.75")
    p.add_argument("--lag-ms", type=float, default=100.0, help="merge watermark margin [ms]")
    p.add_argument("--stall-s", type=float, default=5.0, help="a board silent this long no longer holds the watermark")
    p.add_argument("--min-step-s", type=float, default=1.0, help="do not cut slices shorter than this (edge decode cost)")
    p.add_argument("--interval", type=float, default=1.0, help="seconds between processing steps (inotify wakes it earlier)")
    p.add_argument("--from-start", action="store_true", help="live mode: process the run from its first file instead of from now")
    p.add_argument("--port", type=int, default=8050)
    p.add_argument("--workers", type=int, default=16,
                   help="processes for sort / bunch building / finder (0 = in the main loop); 16 keep up with about "
                        "1 GB/s of noise data on odin, 8 with about 500 MB/s")
    p.add_argument("--open-browser", action="store_true", help="open the page in the default browser once the server is up")
    p.add_argument("--exit-with-parent", action="store_true", help="stop when the process that started us (FSUDAQ) is gone")
    a = p.parse_args(argv)
    if not a.run and not a.data_path:
        sys.exit("give --data-path or --run")

    online = Online(a)
    Handler.online = online
    with open(os.path.join(HERE, "dashboard.html"), "rb") as fh:
        Handler.page = fh.read()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    if a.open_browser:
        import webbrowser
        threading.Thread(target=lambda: webbrowser.open(f"http://localhost:{a.port}/"), daemon=True).start()
    say(f"online dashboard: http://localhost:{a.port}/   kernels={'numba' if FAST else 'numpy'}  "
          f"files={'inotify' if HAVE_INOTIFY and not a.replay else 'polling'}  "
          f"{'replay of ' + a.run if a.replay else ('run ' + a.run if a.run else 'following ' + (a.follow or a.data_path))}")
    last_report = 0.0
    last_seen = None                     # (status, processed_s, hits): report only when this changes

    def _term(signum, frame):            # SIGTERM from FSUDAQ (or kill): leave like Ctrl-C
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _term)
    parent = os.getppid()
    try:
        while True:
            t0 = time.time()
            if a.exit_with_parent and os.getppid() != parent:   # FSUDAQ died: free the port for the next one
                say("FSUDAQ is gone; stopping")
                break
            try:
                online.step()
            except Exception as ex:          # keep serving; report the problem
                online.status = f"error: {ex!r}"
                say(f"step failed: {ex!r}")
            if t0 - last_report > 10:
                s = online.state()
                seen = (s['status'], round(s['processed_s'] or 0, 1), s['hits'])
                if seen != last_seen:    # quiet while nothing happens (between runs, page closed)
                    say(f"{time.strftime('%H:%M:%S')} {s['status']}: processed {s['processed_s'] or 0:.1f} s of beam, "
                          f"{s['hits']:,} hits, {s['bunches']:,} bunches, "
                          f"{s['status_counts']['unique'] + s['status_counts']['tracks']:,} accepted, {s['proc_s']:.1f} s worker CPU, "
                          f"{s['in_flight']} steps in flight")
                    last_seen = seen
                last_report = t0
            online.wake.wait(max(0.0, a.interval - (time.time() - t0)))
            online.wake.clear()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            srv.shutdown()
        except Exception:
            pass
        # Pool.terminate() waits on the task queue's lock; a worker killed while holding it (a signal
        # to the whole process group: Ctrl-C, timeout, systemd) makes that wait forever and the port
        # stays taken. Kill the workers directly and leave without the pool's exit handlers.
        for child in mp.active_children():
            child.kill()
        say("online dashboard stopped")
        os._exit(0)


if __name__ == "__main__":
    main()
