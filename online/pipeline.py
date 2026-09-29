#!/usr/bin/env python3
"""Load a CoMPASS .root file and hand each event to the online classifier.

    process_file("run.root") -> Result

The file is a flat, time-ordered stream of single-channel hits. The classifier
wants one row per event: a triplet (ch0, ch1, ch2) of 1-based amplifier numbers,
plus counters that may be None. Getting from one to the other is this module:

  1. (Board, Channel) -> global channel, via the detector LUT
  2. group hits into events on a coincidence window (the bunch)
  3. winner-takes-all per plane: the largest Energy in that plane IS the
     amplifier number, exactly as the front end decodes it online. The bars
     interlock so a track lights two; WTA picks one. A plane with no hit
     reports 0.
  4. tof_ns = t(T2) - t(T0) of the winning hits. z(T2) - z(T0) is the
     classifier's TOF_BASELINE_CM, so this is the quantity it expects.
  5. a counter channel that is in the map reports True/False per event; one
     that is NOT in the map is not read out and reports **None**, which is not
     the same as False and never sets a class.
  6. classify.

Two classify backends, which must agree:
  'vector'    -- dense [ch0][ch1][ch2] table + numpy precedence. Bulk offline.
  'reference' -- online_classify.classify() per event. The handover contract.

`--check` runs both and asserts they agree event for event.

CLI:
  pipeline.py run.root
  pipeline.py run.root --backend reference
  pipeline.py run.root --check
  pipeline.py data/*.root --repeat 3
"""

from __future__ import annotations

import argparse
import glob
import os
import time
from dataclasses import dataclass, field

import numpy as np
try:                      # only the CoMPASS .root paths need it; the FSUDAQ front-end does not
    import uproot
except ImportError:       # pragma: no cover
    uproot = None

import merge as merge_mod
import online_classify as oc
from detector import AUX_COUNTERS, ChannelMap, parse_planes

# Only what event building and classification actually touch. CoMPASS writes
# six branches, but EnergyShort and Flags are never read here, and carrying them
# costs decompression, concatenation and a gather per merge block for nothing --
# ~10% of the bytes and a third of the merge's gather. Add a branch back here
# the moment something downstream needs it (a Flags quality cut would).
BRANCHES = ["Channel", "Timestamp", "Board", "Energy"]
ALL_BRANCHES = ["Channel", "Timestamp", "Board", "Energy", "EnergyShort", "Flags"]

# order matters: index = number of planes that fired
MISS_CLASSES = ("EMPTY", "SINGLE", "DOUBLE", "UNPHYSICAL_3")
CLASSES = MISS_CLASSES + ("PION", "ELECTRON", "HEAVY", "MULTITRACK", "COLLIMATOR")
CODE = {name: i for i, name in enumerate(CLASSES)}
TAGS = ("LUCITE_YES", "LUCITE_NO", "ESCAPED_HIGH_P", "ESCAPED_LOW_P",
        "LOW_STATS", "TOF_HEAVY")


def human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:,.1f} {unit}" if unit != "B" else f"{n:,.0f} B"
        n /= 1024


def drop_cache(path: str) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    finally:
        os.close(fd)


def _starts_new_event(ts: np.ndarray, window) -> np.ndarray:
    """Boolean: this hit opens a new event (gap to the previous one > window).

    Timestamp is uint64 picoseconds in real CoMPASS output, and np.diff on an
    unsigned dtype WRAPS: a backward step becomes a huge positive number rather
    than a negative one. So integer timestamps are widened to int64 (exact to
    106 days of picoseconds) instead of being diffed in place, and float
    timestamps -- the `spec` profile, or --time-unit ns/s -- stay in float.
    """
    t = ts.astype(np.int64) if ts.dtype.kind in "ui" else ts
    new = np.empty(t.size, bool)
    new[0] = True
    np.greater(t[1:] - t[:-1], type(t[0])(window) if t.dtype.kind == "i" else window,
               out=new[1:])
    return new


def _count_out_of_order(ts: np.ndarray) -> int:
    """Hits whose Timestamp is below their predecessor's.

    Compares adjacent elements rather than subtracting, so it is correct for
    unsigned dtypes, where a difference would wrap instead of going negative.
    """
    return int(np.count_nonzero(ts[1:] < ts[:-1]))


class DenseMap:
    """momentum map as flat [ch0][ch1][ch2] arrays -- the interface's ~17k slots."""

    def __init__(self, table: dict, cmap: ChannelMap):
        self.dims = tuple(p.n_amps + 1 for p in cmap.planes)
        n = int(np.prod(self.dims))
        self.present = np.zeros(n, bool)
        self.p = np.full(n, np.nan)
        self.sigma = np.full(n, np.nan)
        self.ok = np.zeros(n, bool)
        self.skipped = 0
        for (c0, c1, c2), (p, s, ok) in table.items():
            # A triplet can reference an amplifier outside this ChannelMap (a
            # map built for a different geometry, or a cut-down test layout).
            # Skip it rather than corrupting the table or raising.
            if c0 >= self.dims[0] or c1 >= self.dims[1] or c2 >= self.dims[2]:
                self.skipped += 1
                continue
            i = self.index(c0, c1, c2)
            self.present[i] = True
            self.p[i] = p
            self.sigma[i] = s
            self.ok[i] = ok
        if self.skipped:
            print(f"  note: {self.skipped:,}/{len(table):,} map triplets reference "
                  f"amplifiers outside this channel map and were skipped")
        # a pion of the mapped momentum takes this long from T0 to T2
        beta = self.p / np.sqrt(self.p * self.p + oc.PION_MASS_MEV ** 2)
        self.t_pion_ns = oc.TOF_BASELINE_CM / (beta * oc.C_CM_PER_NS)

    def index(self, c0, c1, c2):
        return (c0 * self.dims[1] + c1) * self.dims[2] + c2


@dataclass
class Result:
    path: str = ""
    bytes: int = 0
    hits: int = 0
    events: int = 0
    unmapped: int = 0
    out_of_order: int = 0
    n_files: int = 1
    beam_seconds: float = 0.0
    open_s: float = 0.0
    read_s: float = 0.0
    build_s: float = 0.0
    classify_s: float = 0.0
    counts: dict = field(default_factory=lambda: dict.fromkeys(CLASSES, 0))
    tags: dict = field(default_factory=lambda: dict.fromkeys(TAGS, 0))
    p_sum: float = 0.0
    p_n: int = 0

    @property
    def total_s(self) -> float:
        return self.open_s + self.read_s + self.build_s + self.classify_s


def build_events(a: dict, cmap: ChannelMap, lut: np.ndarray,
                 window_units: float, to_ns: float, res: Result):
    """Hits -> per-event triplets, tof and counter booleans.

    Returns (ch, tof_ns, aux, n_events) with ch of shape (n_events, 3).
    """
    ts = a["Timestamp"]
    board, chan = a["Board"], a["Channel"]

    in_range = (board < lut.shape[0]) & (chan < lut.shape[1])
    gch = lut[np.where(in_range, board, 0), np.where(in_range, chan, 0)]
    bad = ~in_range | (gch < 0)
    if bad.any():
        res.unmapped += int(bad.sum())
        gch = np.where(bad, 0, gch)

    ev = np.cumsum(_starts_new_event(ts, window_units)) - 1
    n_events = int(ev[-1]) + 1

    plane = cmap.plane[gch]
    trk = (plane >= 0) & ~bad

    # Winner-takes-all: largest Energy per (event, plane).
    #
    # lexsort((-energy, key)) does this in one line but costs a full O(n log n)
    # sort and was 70% of this function. Event ids are already non-decreasing,
    # so a stable argsort on key alone is nearly free (it only has to reorder
    # planes within an event); the winner within each group then comes from one
    # O(n) reduceat. Energy and position are packed into a single int64 so that
    # one maximum.reduceat yields the argmax, not just the max -- energy in the
    # high bits, a descending counter in the low bits so ties go to the first
    # hit, matching decode_wta's max(range(...)).
    key = ev[trk].astype(np.int64) * 3 + plane[trk]
    energy = a["Energy"][trk]
    order = np.argsort(key, kind="stable")
    ks = key[order]
    starts = np.empty(ks.size, bool)
    starts[0] = True
    np.not_equal(ks[1:], ks[:-1], out=starts[1:])
    starts = np.flatnonzero(starts)

    n_trk_hits = ks.size
    packed = (energy[order].astype(np.int64) << 32) | np.arange(
        n_trk_hits - 1, -1, -1, dtype=np.int64)
    win = order[(n_trk_hits - 1) - (np.maximum.reduceat(packed, starts)
                                    & 0xFFFFFFFF)]

    ch = np.zeros((n_events, 3), np.int32)
    t_win = np.zeros((n_events, 3), np.float64)
    ev_w, pl_w = ev[trk][win], plane[trk][win]
    ch[ev_w, pl_w] = cmap.amp[gch[trk][win]]
    t_win[ev_w, pl_w] = ts[trk][win]

    both = (ch[:, 0] > 0) & (ch[:, 2] > 0)
    tof_ns = np.where(both, (t_win[:, 2] - t_win[:, 0]) * to_ns, 0.0)

    # A counter absent from the channel map is NOT READ OUT -> None, never False.
    aux = {}
    for name in AUX_COUNTERS:
        if name not in cmap.aux_index:
            aux[name] = None
            continue
        fired = np.zeros(n_events, bool)
        m = gch == cmap.aux_index[name]
        if m.any():
            fired[ev[m]] = True
        aux[name] = fired

    return ch, tof_ns, aux, n_events


def classify_vector(ch, tof_ns, aux, dense: DenseMap):
    """Vectorised equivalent of online_classify.classify()."""
    c0, c1, c2 = ch[:, 0], ch[:, 1], ch[:, 2]
    idx = dense.index(c0.astype(np.int64), c1, c2)
    present = dense.present[idx]
    p = np.where(present, dense.p[idx], np.nan)
    sigma = np.where(present, dense.sigma[idx], np.nan)

    n_planes = (c0 > 0).astype(np.int8) + (c1 > 0) + (c2 > 0)
    cls = np.where(present, CODE["PION"], np.asarray(
        [CODE[m] for m in MISS_CLASSES], np.int8)[n_planes])

    # TOF is one-sided: only a LATE arrival is heavy. Early is the Cherenkov's job.
    heavy = present & (tof_ns > 0) & (
        (tof_ns - dense.t_pion_ns[idx]) > oc.TOF_N_SIGMA * oc.TOF_SIGMA_NS)

    # lowest precedence first; each overwrites the one before
    if aux["cg_electron"] is not None:
        cls = np.where(present & aux["cg_electron"], CODE["ELECTRON"], cls)
    cls = np.where(heavy, CODE["HEAVY"], cls)
    if aux["bunch_multi"] is not None:
        cls = np.where(present & aux["bunch_multi"], CODE["MULTITRACK"], cls)
    if aux["coll_veto"] is not None:
        cls = np.where(present & aux["coll_veto"], CODE["COLLIMATOR"], cls)

    tags = {t: 0 for t in TAGS}
    if aux["lucite"] is not None:
        tags["LUCITE_YES"] = int(aux["lucite"].sum())
        tags["LUCITE_NO"] = int((~aux["lucite"]).sum())
    for name, tag in (("edge_high_p", "ESCAPED_HIGH_P"), ("edge_low_p", "ESCAPED_LOW_P")):
        if aux[name] is not None:
            tags[tag] = int(aux[name].sum())
    tags["LOW_STATS"] = int((present & ~dense.ok[idx]).sum())
    tags["TOF_HEAVY"] = int(heavy.sum())
    return cls, p, sigma, tags


def classify_reference(ch, tof_ns, aux, table):
    """One online_classify.classify() call per event -- the handover contract."""
    n = ch.shape[0]
    cls = np.empty(n, np.int8)
    p_out = np.full(n, np.nan)
    kw_none = {k: None for k in AUX_COUNTERS}
    for i in range(n):
        kw = dict(kw_none)
        for name in AUX_COUNTERS:
            col = aux[name]
            if col is not None:
                kw[name] = bool(col[i])
        t = float(tof_ns[i])
        c, p, _s, _tags = oc.classify(
            int(ch[i, 0]), int(ch[i, 1]), int(ch[i, 2]), table,
            tof_ns=t if t > 0 else None, **kw)
        cls[i] = CODE[c]
        if p is not None:
            p_out[i] = p
    return cls, p_out


def _consume(blocks, res: Result, handle, window_units, order):
    """Drive the event loop over an iterator of time-ordered hit blocks.

    The source is either one file's baskets (offline) or the live k-way merge
    across per-channel files (online) -- the loop is identical either way, so
    `res.read_s` covers merging too when the source is a merge.
    """
    carry = None
    span = [None, 0.0]
    it = iter(blocks)
    while True:
        t0 = time.perf_counter()
        try:
            a = next(it)
        except StopIteration:
            res.read_s += time.perf_counter() - t0
            break
        res.read_s += time.perf_counter() - t0
        res.hits += a["Timestamp"].size
        if span[0] is None:
            span[0] = float(a["Timestamp"][0])
        span[1] = float(a["Timestamp"][-1])

        if carry is not None:
            a = {k: np.concatenate([carry[k], a[k]]) for k in a}

        # Event ids are cumsum(gap > window), monotone for ANY input including a
        # shuffled one, so they cannot detect disorder -- they silently split and
        # merge events. Check Timestamp itself; a stable sort on near-sorted
        # input costs ~1% of event building.
        t0 = time.perf_counter()
        if order != "assume":
            ts = a["Timestamp"]
            n_bad = 0 if order == "sort" else _count_out_of_order(ts)
            if order == "sort" or n_bad:
                res.out_of_order += n_bad
                o = np.argsort(ts, kind="stable")
                a = {k: v[o] for k, v in a.items()}
        res.build_s += time.perf_counter() - t0

        # defer the last (possibly incomplete) event so none is split across a block
        ts = a["Timestamp"]
        ev = np.cumsum(_starts_new_event(ts, window_units)) - 1
        tail = ev == ev[-1]
        carry = {k: v[tail] for k, v in a.items()}
        if (~tail).any():
            handle({k: v[~tail] for k, v in a.items()})

    if carry is not None and carry["Timestamp"].size:
        handle(carry)
    return span


def _prepare(table, cmap, map_path, setting, time_unit, window_ns):
    if cmap is None:
        cmap = ChannelMap()
    if table is None:
        table = oc.load_map(map_path, setting)
    unit_ps = {"ps": 1.0, "ns": 1e3, "s": 1e12}[time_unit]
    return (cmap, table, DenseMap(table, cmap), cmap.lookup_table(),
            window_ns * 1e3 / unit_ps, unit_ps * 1e-3, unit_ps)


def _make_handler(res, cmap, lut, dense, table, window_units, to_ns, backend, check):
    def handle(a):
        if backend == "io":            # read-only: the raw I/O ceiling
            return
        t0 = time.perf_counter()
        ch, tof_ns, aux, n_events = build_events(a, cmap, lut, window_units, to_ns, res)
        res.build_s += time.perf_counter() - t0

        t0 = time.perf_counter()
        if backend == "reference" and not check:
            cls, p = classify_reference(ch, tof_ns, aux, table)
            tags = None
        else:
            cls, p, _sigma, tags = classify_vector(ch, tof_ns, aux, dense)
            if check:
                rcls, rp = classify_reference(ch, tof_ns, aux, table)
                bad = np.flatnonzero(cls != rcls)
                if bad.size:
                    i = bad[0]
                    raise AssertionError(
                        f"backends disagree on {bad.size}/{cls.size} events; first at "
                        f"triplet {tuple(ch[i])}: vector={CLASSES[cls[i]]} "
                        f"reference={CLASSES[rcls[i]]}")
                fin = np.isfinite(p) | np.isfinite(rp)
                if not np.allclose(p[fin], rp[fin], equal_nan=True):
                    raise AssertionError("backends disagree on momentum")
        res.classify_s += time.perf_counter() - t0

        res.events += n_events
        for code, n in zip(*np.unique(cls, return_counts=True)):
            res.counts[CLASSES[code]] += int(n)
        if tags:
            for k, v in tags.items():
                res.tags[k] += v
        good = np.isfinite(p)
        res.p_sum += float(p[good].sum())
        res.p_n += int(good.sum())
    return handle


def process_file(path, table=None, cmap=None, map_path="momentum_map.csv",
                 setting=oc.SETTING, tree="Data_R", window_ns=100.0,
                 step_size=1_000_000, time_unit="ps", backend="vector",
                 check=False, cold=False, order="check") -> Result:
    """Load one merged .root file, build events, classify them. -> Result."""
    cmap, table, dense, lut, window_units, to_ns, unit_ps = _prepare(
        table, cmap, map_path, setting, time_unit, window_ns)

    res = Result(path=path, bytes=os.path.getsize(path), n_files=1)
    if cold:
        drop_cache(path)
    handle = _make_handler(res, cmap, lut, dense, table, window_units, to_ns,
                           backend, check)

    t_open = time.perf_counter()
    with uproot.open(path) as f:
        t = f[tree]
        res.open_s = time.perf_counter() - t_open
        span = _consume(t.iterate(BRANCHES, step_size=step_size, library="np"),
                        res, handle, window_units, order)

    if span[0] is not None:
        res.beam_seconds = (span[1] - span[0]) * unit_ps * 1e-12
    return res


def process_files(paths, table=None, cmap=None, map_path="momentum_map.csv",
                  setting=oc.SETTING, tree="Data_R", window_ns=100.0,
                  buffer_hits=4_000_000, step_size=None, time_unit="ps",
                  backend="vector", check=False, order="check") -> Result:
    """Process CoMPASS per-channel files directly, merging in memory.

    The online path. Writing a merged .root file and reading it straight back
    costs a compress plus a decompress of the entire run for nothing, which
    dominates latency on short chunks -- at 5 s the merge was 86% of the time.
    This streams `merge.iter_merged` into the same event loop instead, so
    nothing is ever written.
    """
    cmap, table, dense, lut, window_units, to_ns, unit_ps = _prepare(
        table, cmap, map_path, setting, time_unit, window_ns)

    res = Result(path=os.path.dirname(paths[0]) if paths else "",
                 bytes=sum(os.path.getsize(p) for p in paths), n_files=len(paths))
    handle = _make_handler(res, cmap, lut, dense, table, window_units, to_ns,
                           backend, check)
    span = _consume(merge_mod.iter_merged(paths, tree, step_size, buffer_hits,
                                          branches=BRANCHES),
                    res, handle, window_units, order)
    if span[0] is not None:
        res.beam_seconds = (span[1] - span[0]) * unit_ps * 1e-12
    return res


def main() -> None:
    p = argparse.ArgumentParser(
        description="Load CoMPASS .root files and classify their events.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("files", nargs="+",
                   help="merged .root file(s), or a RAW/ directory of per-channel "
                        "files to merge in memory and process without an "
                        "intermediate file (the online path)")
    p.add_argument("--map", default="momentum_map.csv")
    p.add_argument("--setting", choices=("high", "low"), default=oc.SETTING)
    p.add_argument("--planes", default="deployed")
    p.add_argument("--board-channels", type=int, default=16,
                   help="channels per digitizer (16 on the VX1730SB in use)")
    p.add_argument("--aux", default=",".join(AUX_COUNTERS),
                   help="counters that are read out; omit one and it reports None")
    p.add_argument("--tree", default="Data_R")
    p.add_argument("--window", type=float, default=100.0,
                   help="event-building coincidence window [ns]")
    p.add_argument("--step-size", type=int, default=1_000_000)
    p.add_argument("--time-unit", choices=("ps", "ns", "s"), default="ps")
    p.add_argument("--backend", choices=("vector", "reference", "io"), default="vector",
                   help="'io' reads the branches and stops: the raw I/O ceiling")
    p.add_argument("--check", action="store_true",
                   help="run both backends and assert they agree")
    p.add_argument("--order", choices=("check", "sort", "assume"), default="check",
                   help="'check' verifies Timestamp is non-decreasing per chunk and "
                        "stable-sorts it if not, reporting how many hits were out of "
                        "order; 'sort' always sorts; 'assume' trusts the file")
    p.add_argument("--cold", action="store_true")
    p.add_argument("--buffer-hits", type=int, default=4_000_000,
                   help="RAW-directory mode: total hits buffered across inputs")
    p.add_argument("--repeat", type=int, default=1)
    args = p.parse_args()

    aux = tuple(x.strip() for x in args.aux.split(",") if x.strip())
    cmap = ChannelMap(parse_planes(args.planes), args.board_channels, aux)
    table = oc.load_map(args.map, args.setting)
    print(f"{len(table)} triplets, setting {args.setting}  |  {cmap.n_channels} channels"
          f"  |  backend {args.backend}{' + check' if args.check else ''}")

    hdr = (f"{'file':<24} {'size':>9} {'hits':>12} {'events':>11} {'read':>7} "
           f"{'build':>7} {'class':>7} {'total':>7} {'MB/s':>7} {'Mevt/s':>7} {'x RT':>6}")
    print(hdr)
    print("-" * len(hdr))

    results = []
    for path in args.files:
        raw = None
        if os.path.isdir(path):
            raw = sorted(glob.glob(os.path.join(path, "DataR_CH*.root")))
            if not raw:
                raise SystemExit(f"{path}: no DataR_CH*.root files")
        best = None
        for _ in range(args.repeat):
            if raw:
                r = process_files(raw, table=table, cmap=cmap, setting=args.setting,
                                  tree=args.tree, window_ns=args.window,
                                  buffer_hits=args.buffer_hits,
                                  time_unit=args.time_unit, backend=args.backend,
                                  check=args.check, order=args.order)
            else:
                r = process_file(path, table=table, cmap=cmap, setting=args.setting,
                                 tree=args.tree, window_ns=args.window,
                                 step_size=args.step_size, time_unit=args.time_unit,
                                 backend=args.backend, check=args.check,
                                 cold=args.cold, order=args.order)
            if best is None or r.total_s < best.total_s:
                best = r
        results.append(best)
        label = (f"{os.path.basename(path.rstrip('/'))}/ ({best.n_files}f)"
                 if raw else os.path.basename(path))
        print(f"{label:<24} {human(best.bytes):>9} {best.hits:>12,} "
              f"{best.events:>11,} {best.read_s:>7.3f} {best.build_s:>7.3f} "
              f"{best.classify_s:>7.3f} {best.total_s:>7.3f} "
              f"{best.bytes / 1e6 / best.total_s:>7.1f} "
              f"{best.events / 1e6 / best.total_s:>7.3f} "
              f"{best.beam_seconds / max(best.total_s, 1e-9):>6.1f}")

    for r in results:
        print(f"\n{os.path.basename(r.path.rstrip('/'))}: {r.events:,} events, "
              f"{r.beam_seconds:.2f} s of beam")
        tot = max(r.events, 1)
        for name in CLASSES:
            n = r.counts[name]
            if n:
                print(f"  {name:<14} {n:>10,}  {n / tot:>6.2%}")
        if r.p_n:
            print(f"  mean momentum where reported: {r.p_sum / r.p_n:.1f} MeV/c "
                  f"({r.p_n:,} events)")
        tags = {k: v for k, v in r.tags.items() if v}
        if tags:
            print("  tags: " + ", ".join(f"{k}={v:,}" for k, v in tags.items()))
        if r.out_of_order:
            print(f"  WARNING: {r.out_of_order:,} hits arrived out of time order and "
                  f"were re-sorted; without that the event grouping would be wrong")
        if r.unmapped:
            print(f"  WARNING: {r.unmapped:,} hits had a (Board, Channel) outside the map")


if __name__ == "__main__":
    main()
