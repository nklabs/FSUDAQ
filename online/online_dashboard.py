#!/usr/bin/env python3
"""Live online analysis and dashboard for FSUDAQ runs.

    online_dashboard.py --data-path /home/beamtime/FSUDAQ_data/testFSUDAQ [--port 8050]
    online_dashboard.py --run /path/to/prefix_012                  (one run folder)
    online_dashboard.py --run /path/to/finished_run --replay        (play a finished run at real-time pace)

Follows the run folder FSUDAQ names when a saving run starts (GET /run?folder=..., or --follow at
start-up), else the newest run folder in the data path; a run recorded again under the same number
(old files replaced) starts over. FSUDAQ says when the run has stopped and its files are closed
(GET /stopped?folder=...). Started mid-run, the dashboard reads from then on; started after the run
ended, it reads the whole run. Serves http://localhost:<port>/ (the page) and /state (JSON).

How the data are read. A board writes a couple's (channel pair's) events when that couple's buffer
is full, so a quiet channel's hits reach the disk long after a busy channel's newer ones. Every
board file is indexed as it grows (fsu_fast.FileScan: only the new bytes, at most INDEX_WORDS per
step, outside the lock so the page is served meanwhile; each couple aggregate with its first and
last time; damage is stepped over and counted, so are aggregates missing from the file (counter
gaps); a format the decoders do not read is counted and left out). What is "written" and how long a
couple has been silent come from this index. Then two things happen:

* the DAQ tab: every couple aggregate is decoded once when it reaches the disk, in any time order
  (counts, pulse height, PSD, time since the previous hit, hits per second): exact. At most 4
  tasks per worker are out; the rest waits on disk, and the finder does not pass it;
* the track finder needs all boards in time order: it takes time slices up to the oldest newest
  time among the couples still writing (a couple silent for --hold-s no longer holds it back), so
  every hit of those couples up to there is on disk. A hit that still arrives behind a slice
  already taken is counted as "late" and shown, never silently dropped. When FSUDAQ says the run
  stopped, everything up to the newest hit is taken. When nothing is written for --stall-s without
  that word, it is a pause: the boards still hold partly filled buffers, so the finder waits, for
  up to --hold-s, then takes everything (hits that come after are late).

Slices go to worker processes (results are added in any order; a worker that dies is replaced
and its task run again once). The run's setting, cabling, gains and time offsets come from
experiment.json (re-read for each run), else the command line; anything missing is a placeholder
the page says so about.
"""
from __future__ import annotations

import argparse
import collections
import concurrent.futures as cf
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
from concurrent.futures.process import BrokenProcessPool
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fsu_fast                              # noqa: E402  file index and decoder
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


FAST = fsu_fast.HAVE_NUMBA
DEFAULT_BOARDS = "15880,15879,58918,62839,65316,30508,1443,1399,30505"   # odin's rack order (FSUDAQ passes its boardOrder)
EIGHT_CHANNEL_BOARDS = "30508,1443,1399,30505"   # the desktop boards
CONFIG = os.path.join(HERE, "experiment.json")
HISTORY_S = 600                               # seconds of rate history kept
PS = 1_000_000_000_000                        # ps per second
FILE_RE = re.compile(r"_(\d+)_(PSD|PHA|QDC)_\d+_(\d+)\.fsu$")   # <prefix>_<run>_<serial>_<DPP>_<tick>_<index>.fsu
BATCH_HITS = 4_000_000                        # hits per DAQ task
INDEX_WORDS = 32 << 20                        # words of a file indexed per step at most (128 MB): a big run
                                              # already on disk is read over several steps
LATE_RECENT_S = 60                            # the page shows late hits of the last minute too


# ---------------------------------------------------------------- run configuration
def load_config(a):
    """Setting, cabling, gains, time offsets and map: the command line first, then the config file.
    Returns (values, where each came from, problems)."""
    cfg, problems, src = {}, [], {}
    path = a.config
    if path and os.path.exists(path):
        try:
            with open(path) as f:
                cfg = json.load(f)
            if not isinstance(cfg, dict):
                raise ValueError("not a JSON object")
        except (OSError, ValueError) as ex:
            problems.append(f"{path} not read: {ex}")
            cfg = {}
    base = os.path.dirname(os.path.abspath(path)) if path else HERE
    out = {}
    for key in ("setting", "cabling", "gains", "time_offsets", "map"):
        v = getattr(a, key)
        if v is not None:
            src[key] = "command line"
        else:
            v = cfg.get(key)
            if v in (None, ""):
                v = None
                src[key] = None
            else:
                src[key] = os.path.basename(path)
                if key != "setting" and not os.path.isabs(str(v)):
                    v = os.path.join(base, str(v))
        out[key] = v
    if out["setting"] not in (None, "high", "low"):
        problems.append(f"setting '{out['setting']}' is neither high nor low")
        out["setting"] = None; src["setting"] = None
    for key in ("cabling", "gains", "time_offsets", "map"):
        if out[key] and not os.path.exists(out[key]):
            problems.append(f"{key}: {out[key]} not found")
            out[key] = None; src[key] = None
    return out, src, problems


class Setup:
    """Everything the finder needs for one run, with what is missing or wrong said in words."""

    def __init__(self, a, serials, nch):
        cfg, src, problems = load_config(a)
        self.src = src
        self.files = {k: cfg[k] for k in ("cabling", "gains", "time_offsets", "map")}
        self.key = json.dumps([cfg, {k: os.path.getmtime(v) for k, v in self.files.items() if v}], sort_keys=True)
        warnings = []                         # placeholders and problems, shown at the top of the page
        self.setting = cfg["setting"] or "high"
        if cfg["setting"] is None:
            warnings.append("beam setting not given (experiment.json \"setting\"): the HIGH map is used")
        rows = None
        if cfg["cabling"]:
            try:
                rows = ff.load_cabling(cfg["cabling"])
            except (OSError, ValueError, KeyError) as ex:
                problems.append(f"cabling {cfg['cabling']} not read: {ex!r}")
        if rows is None:
            rows = ff.default_cabling(serials, nch)
            self.cabling = "placeholder (sequential)"
            warnings.append("cabling is a sequential placeholder: tracker channels and counters are not where the plots say")
        else:
            self.cabling = cfg["cabling"]
        gains = offs = None
        if cfg["gains"]:
            try:
                gains = ff.load_per_channel(cfg["gains"], "mevee_per_count")
            except (OSError, ValueError, KeyError) as ex:
                problems.append(f"gains {cfg['gains']} not read: {ex!r}")
        if gains is None:
            warnings.append("no light calibration (gains): light tests off, every cluster reads as one track")
        if cfg["time_offsets"]:
            try:
                offs = ff.load_per_channel(cfg["time_offsets"], "offset_ns")
            except (OSError, ValueError, KeyError) as ex:
                problems.append(f"time offsets {cfg['time_offsets']} not read: {ex!r}")
        if offs is None:
            warnings.append("no cable time offsets: the timing tests see the raw delays")
        self.tables = ff.Tables(self.setting, map_path=cfg["map"], calibrated=gains is not None)
        plane_channels = [max(self.tables.T["gang"][i]) for i in range(3)]
        rows, cab_problems = ff.check_cabling(rows, serials, nch, plane_channels)
        problems += cab_problems
        self.chans = ff.Channels(serials, rows, gains, offs)
        if self.chans.uncalibrated:
            problems.append("no gain for " + ", ".join(f"{s} ch{c}" for s, c in self.chans.uncalibrated[:12])
                            + (f" and {len(self.chans.uncalibrated) - 12} more" if len(self.chans.uncalibrated) > 12 else "")
                            + ": their hits are left out of the finder")
        self.map_path = cfg["map"] or os.path.join(ff.FINDER_DIR, "momentum_map.csv")
        self.calibrated = gains is not None
        self.has_offsets = offs is not None
        self.max_offset_ps = int(np.abs(self.chans.toff_ps).max(initial=0))
        self.warnings, self.problems = warnings, problems


# ---------------------------------------------------------------- work done in the worker processes
_G: dict = {}


def _init_worker(tables, chans, window_ps):
    try:
        os.nice(10)                           # below the DAQ, which shares the machine
    except OSError:
        pass
    signal.signal(signal.SIGINT, signal.SIG_IGN)   # the main process decides when to stop
    _G.update(tables=tables, chans=chans, window_ps=window_ps)


def _toff_of(slot):
    """The time offset (ps) of each hit's channel."""
    return _G["chans"].toff_ps.reshape(-1)[slot]


def ingest_task(task: dict) -> dict:
    """The DAQ tab's exact part for some couple aggregates of one file, each decoded once."""
    t_start = time.perf_counter()
    A = task["aggs"]
    ts, ch, e, qs = fsu_fast.decode_aggs(task["path"], A["ev_start"], A["nev"], A["couple"], A["wpe"])
    slot = task["bi"] * task["board_channels"] + ch.astype(np.int64)
    # late: by the finder's time (offsets removed), as the slices own their hits
    r, (secs, slots, cnt) = dh.process_exact(ts, slot, e, qs, task["late_before"], task["n_slots"], ts - _toff_of(slot),
                                             task.get("early_before"))
    return dict(daq=r.to_dict(), secs=(secs, slots, cnt), n=int(ts.size), bi=task["bi"], dt=time.perf_counter() - t_start)


def slice_task(task: dict) -> dict:
    """The track finder (and the DAQ tab's time-ordered part) for the bunches whose first hit, on the
    common clock, is in [t0, t1). Decodes only the couple aggregates that overlap the slice."""
    t_start = time.perf_counter()
    t0, t1, margin = task["t0"], task["t1"], task["margin"]
    tables, chans, window_ps = _G["tables"], _G["chans"], _G["window_ps"]
    parts = []
    for bi, path, A in task["files"]:
        ts, ch, e, qs = fsu_fast.decode_aggs(path, A["ev_start"], A["nev"], A["couple"], A["wpe"], t0 - margin, t1 + margin)
        if ts.size:
            parts.append((ts, ch, e, np.full(ts.size, bi, np.int64)))
    if parts:
        ts = np.concatenate([p[0] for p in parts]); ch = np.concatenate([p[1] for p in parts]).astype(np.int64)
        e = np.concatenate([p[2] for p in parts]); bd = np.concatenate([p[3] for p in parts])
        res = ff.process_hits(tables, chans, ts, bd, ch, e.astype(np.float64), window_ps, t0, t1)
        slot = bd * chans.board_channels + ch
        daq = dh.process_ordered(ts, slot, np.argsort(ts, kind="stable"), t0, t1, task["ref"], chans.n_slots)
        tc = ts - _toff_of(slot)
        owned = int(np.count_nonzero((tc >= t0) & (tc < t1)))   # hits whose own time (offset removed) is in the slice
    else:
        res = ff.Result(chans.n_slots)
        daq = dh.DaqResult(chans.n_slots, np.uint32)
        owned = 0
    return dict(t0=t0, t1=t1, res=res.to_dict(), daq=daq.to_dict(), ref=task["ref"], owned=owned, dt=time.perf_counter() - t_start)


class _Done:
    """A finished task when there are no worker processes (--workers 0)."""

    def __init__(self, fn, arg):
        try:
            self._r, self._ex = fn(arg), None
        except Exception as ex:               # noqa: BLE001  reported like a worker's
            self._r, self._ex = None, ex

    def done(self):
        return True

    def result(self):
        if self._ex:
            raise self._ex
        return self._r


# ---------------------------------------------------------------- the files of one board
class BoardStream:
    """One board's files: the index (what is on disk), what of it has been seen (a replay shows it at
    the replay's pace), and what has been given to the DAQ part."""

    def __init__(self, serial: int, bi: int):
        self.serial, self.bi = serial, bi
        self.scans: dict[str, fsu_fast.FileScan] = {}
        self.order: list[str] = []            # file paths by index
        self.seen: dict[str, int] = {}        # couple aggregates of the index seen (all, or up to the replay time)
        self.done: dict[str, int] = {}        # couple aggregates already given to the DAQ part
        self.released: set[str] = set()       # rolled-over files read and passed: index dropped
        self.newest = np.full(8, -1, np.int64)        # per couple: newest time on disk
        self.newest_wall = np.zeros(8)                # when it last moved
        self.first = None                     # oldest time seen
        self.hits = 0                         # hits indexed (all files)

    def _live(self):
        return [p for p in self.order if p not in self.released]

    def discover(self, folder):
        names = []
        for p in glob.glob(os.path.join(folder, "*.fsu")):
            m = FILE_RE.search(p)
            if m and int(m.group(1)) == self.serial:
                names.append((int(m.group(3)), p))
        for _, p in sorted(names):
            if p not in self.scans:
                self.seen[p] = 0
                self.done[p] = 0
                self.scans[p] = fsu_fast.FileScan(p)
                self.order.append(p)

    def index(self, now, max_words=None):
        """Index what was written since the last call (at most max_words words per file)."""
        for p in self._live():
            fs = self.scans[p]
            n0 = fs.hits
            fs.update(now, max_words)
            self.hits += fs.hits - n0

    def behind(self):
        """More is written than indexed (the indexing reads a limited amount per step)."""
        return any(self.scans[p].behind for p in self._live())

    def replaced(self):
        return any(self.scans[p].replaced() for p in self._live())

    def observe(self, now, limit=None):
        """The couple aggregates indexed since the last look (a replay: up to limit) move the couples'
        newest time on disk, and when it last moved."""
        for p in self._live():
            A = self.scans[p].arrays()
            a, b = self.seen[p], A["nev"].size
            if limit is not None and b > a:
                over = np.flatnonzero(A["t_last"][a:b] > limit)
                if over.size:
                    b = a + int(over[0])
            if b <= a:
                continue
            cpl, tl = A["couple"][a:b], A["t_last"][a:b]
            for c in np.unique(cpl):
                t = int(tl[cpl == c].max())
                if t > self.newest[c]:
                    self.newest[c] = t; self.newest_wall[c] = now
            t = int(A["t_first"][a:b].min())
            self.first = t if self.first is None else min(self.first, t)
            self.seen[p] = b

    def all_seen(self):
        return all(self.seen[p] == self.scans[p].n for p in self._live())

    def untaken_first(self):
        """The oldest time among the couple aggregates seen but not yet given to the DAQ part (None: none)."""
        m = None
        for p in self._live():
            a, b = self.done[p], self.seen[p]
            if b > a:
                t = int(self.scans[p].arrays()["t_first"][a:b].min())
                m = t if m is None else min(m, t)
        return m

    def take_new(self, max_hits=None):
        """Couple aggregates seen and not yet given to the DAQ part, per file, in file order: about
        max_hits hits of them (at least one aggregate)."""
        out = []
        left = max_hits
        for p in self._live():
            a, b = self.done[p], self.seen[p]
            if b <= a:
                continue
            if left is not None and left <= 0:
                break
            A = self.scans[p].arrays()
            if left is not None:
                k = int(np.searchsorted(np.cumsum(A["nev"][a:b]), left, side="right"))
                b = a + max(1, min(k, b - a))
            sel = {k: A[k][a:b] for k in A}
            self.done[p] = b
            if left is not None:
                left -= int(sel["nev"].sum())
            out.append((p, sel))
        return out

    def ready_to_skip(self, marks):
        """Every file indexed up to its size at the start (or as far as it can be)."""
        return all(self.scans[p].pos >= marks.get(p, 0) or not self.scans[p].behind for p in self._live())

    def skip_to(self, marks):
        """A live start: mark the couple aggregates that were on disk at the start (before marks[path]
        words) as given; returns the newest time among them (None: none)."""
        top = None
        for p in self._live():
            A = self.scans[p].arrays()
            k = int(np.searchsorted(A["ev_start"][:self.seen[p]], marks.get(p, 0)))
            if k > self.done[p]:
                t = int(A["t_last"][self.done[p]:k].max())
                top = t if top is None else max(top, t)
                self.done[p] = k
        return top

    def overlapping(self, lo, hi):
        """(path, couple aggregates given to the DAQ part that overlap [lo, hi)) per file."""
        out = []
        for p in self._live():
            if self.done[p] == 0:
                continue
            A = self.scans[p].arrays()
            n = self.done[p]
            m = np.flatnonzero((A["t_last"][:n] >= lo) & (A["t_first"][:n] < hi))
            if m.size:
                out.append((p, {k: A[k][m] for k in ("ev_start", "nev", "couple", "wpe")}))
        return out

    def release(self, before):
        """Drop the index of rolled-over files read completely and ending before `before`."""
        for p in self.order[:-1]:
            if p in self.released:
                continue
            fs = self.scans[p]
            A = fs.arrays()
            if self.done[p] == A["nev"].size and (A["nev"].size == 0 or int(A["t_last"].max()) < before):
                fs.release()
                self.released.add(p)

    def gaps(self):
        """Board aggregates missing from the files: counter gaps inside a file and between files."""
        f = [self.scans[p] for p in list(self.order)]
        n = sum(s.gaps for s in f)
        for x, y in zip(f, f[1:]):
            if x.last_ctr >= 0 and y.first_ctr >= 0:
                step = (y.first_ctr - x.last_ctr) & fsu_fast.CTR_MASK
                if 1 <= step <= fsu_fast.FORWARD:
                    n += step - 1
        return n

    def summary(self, now):
        f = [self.scans[p] for p in list(self.order)]
        stuck = [(s, s.stuck_s(now)) for s in f if s.stuck_since is not None]
        return dict(files=len(f), bytes=sum(s.size for s in f), indexed=sum(s.pos * 4 for s in f),
                    skipped_words=sum(s.skipped for s in f), other_format=sum(s.other for s in f), gaps=self.gaps(),
                    behind=self.behind(),
                    stuck=[dict(file=os.path.basename(s.path), bytes=int(s.tail_bytes()), s=float(t)) for s, t in stuck])


# ---------------------------------------------------------------- the analysis
class Online:
    def __init__(self, a, clock=time.time):
        self.a, self.clock = a, clock
        self.serials = [int(x) for x in a.boards.split(",") if x.strip()]
        eight = {int(x) for x in a.eight_channel_boards.split(",") if x.strip()}
        self.nch = {s: (8 if s in eight else 16) for s in self.serials}
        self.window_ps = int(round(a.window * 1000))
        self.lock = threading.RLock()           # the loop and the web server's threads
        self.pool = None
        self.setup = None
        self.ref_slot = a.ref_slot
        self.follow = a.follow                  # run folder FSUDAQ named (--follow or GET /run); None = newest in the data path
        self.restart = False                    # set by GET /run: a new run starts, even in the same folder
        self.stop_notice = False                # FSUDAQ stopped this run and closed its files
        self.stopped_folder = None              # the folder GET /stopped named (it may come before we follow it)
        self.wake = threading.Event()
        self.step_error = None
        self.folder = None
        self.reset(None, from_start=bool(a.replay or a.from_start))

    # ------------------------------------------------------------ pool
    def _kill_pool(self):
        if self.pool is not None:
            procs = list((getattr(self.pool, "_processes", None) or {}).values())
            self.pool.shutdown(wait=False, cancel_futures=True)
            for p in procs:
                try:
                    p.kill()
                except Exception:                 # noqa: BLE001
                    pass
            self.pool = None

    def _make_pool(self):
        self._kill_pool()
        if self.a.workers > 0:
            self.pool = cf.ProcessPoolExecutor(self.a.workers, mp_context=mp.get_context("fork"), initializer=_init_worker,
                                               initargs=(self.setup.tables, self.setup.chans, self.window_ps))
        else:
            _G.update(tables=self.setup.tables, chans=self.setup.chans, window_ps=self.window_ps)

    def _submit(self, kind, task, tries=0, epoch=None):
        fn = ingest_task if kind == "ingest" else slice_task
        epoch = self.epoch if epoch is None else epoch
        if self.pool is None:
            fut = _Done(fn, task)
        else:
            try:
                fut = self.pool.submit(fn, task)
            except (BrokenProcessPool, RuntimeError):
                # a worker died while the pool was idle: new workers, and every task out goes again
                say("the worker pool is broken; the workers are restarted")
                again = [(k, t, tr, ep) for k, t, _, tr, ep in self.pending.values()]
                self.pending = {}
                self._make_pool()
                for k, t, tr, ep in again:
                    self._submit(k, t, tr, ep)
                fut = self.pool.submit(fn, task)
        self.pending[fut] = (kind, task, self.clock(), tries, epoch)

    # ------------------------------------------------------------ a run
    def reset(self, folder, from_start):
        setup = Setup(self.a, self.serials, self.nch)
        new_setup = self.setup is None or setup.key != self.setup.key
        if new_setup:
            self.setup = setup
        else:
            self.setup.warnings, self.setup.problems = setup.warnings, setup.problems
        if self.folder is not None or new_setup or self.pool is None:
            self._make_pool()                   # drops the previous run's tasks; new tables if the setup changed
        S = self.setup
        self.folder = folder
        self.boards = {sn: BoardStream(sn, i) for i, sn in enumerate(self.serials)}
        self.pending = {}
        self.epoch = 0
        self.W = None                           # the finder has slices up to here (ps, raw time)
        self.t_first = None
        self.first_data_wall = None
        self.skip_existing = not from_start
        self.skip_marks = None                  # live start: each file's size (words) when the dashboard started
        self.since = None                       # live start: the time from which data are read
        self.acc = ff.Result(S.chans.n_slots)   # the finder's histograms and counters
        self.daq = dh.DaqResult(S.chans.n_slots)
        self.secs = {}                          # second of data time -> hits per slot
        self.slices = []                        # (t0, t1, bunches, statuses, tracks) of every folded slice
        self.proc_s = 0.0; self.steps = 0
        self.lost = 0                           # tasks that failed twice, this run
        self.owned = 0                          # hits the finder's slices took (by their own time)
        self.replay_first = None                # replay: the run's first time, fixed once
        self.ingested_hits = np.zeros(len(self.serials), np.int64)
        self.last_fold_wall = None
        self.replay_t0 = None
        self.reset_wall = self.clock()
        self.status = "waiting for data"
        self.mode = "waiting"                   # running, paused, stopped, quiet (see _advance)
        self.stop_notice = False
        self.finished_at_start = False          # the folder was already finished when the dashboard started
        self.quiet_s = 0.0                      # how long no couple has written
        self.behind = False                     # more is written than indexed
        self.untaken_t = None                   # oldest time seen on disk and not yet given to the DAQ part
        self.late_log = collections.deque()     # (wall time, late hits) of each result, for the recent count
        self.flush_wall = None                  # when the finder last went on without couples silent in a pause
        self.rr = 0                             # the board the DAQ part takes from first (round robin)
        self.first_files = {}
        self.others = set()                     # serials with files in the folder that are not analysed
        self.labels = {}
        self.slots = []
        ch = S.chans
        for bi, sn in enumerate(self.serials):
            for c in range(self.nch[sn]):
                sl = bi * ch.board_channels + c
                role = ""
                if not S.cabling.startswith("placeholder"):
                    pl_, chn, cn = ch.plane[bi, c], ch.ch[bi, c], ch.counter[bi, c]
                    role = f"T{pl_}:{chn}" if pl_ >= 0 else (ff.COUNTERS[cn] if pl_ == -1 else "")
                self.slots.append(sl)
                self.labels[sl] = dict(board=sn, ch=c, role=role)

    def _run_replaced(self, folder):
        """True when a file seen earlier is gone or is a new file: the run number was recorded again in
        the same folder (FSUDAQ deletes the old files on "Overwrite")."""
        now = {}
        for f in glob.glob(os.path.join(folder, "*_000.fsu")):
            try:
                now[f] = os.stat(f).st_ino
            except OSError:
                pass
        replaced = any(now.get(f) != ino for f, ino in self.first_files.items())
        if not replaced:
            self.first_files.update(now)
        return replaced or any(b.replaced() for b in self.boards.values())

    def run_started(self, folder):
        """GET /run: FSUDAQ starts a saving run in this folder (also the same folder again)."""
        self.follow = folder
        self.stopped_folder = None
        self.restart = True
        self.wake.set()

    def run_stopped(self, folder):
        """GET /stopped: FSUDAQ stopped the run in this folder and closed its files."""
        self.stopped_folder = os.path.abspath(folder)
        self.wake.set()

    def margin_ps(self):
        return self.window_ps + 2 * self.setup.max_offset_ps + 1_000_000   # + 1 us

    # ------------------------------------------------------------ one step of the loop
    def step(self):
        """Index what was written (outside the lock: the numba scan releases the GIL, so the page is
        served while a big run is read), then everything else under the lock."""
        with self.lock:
            try:
                ctx = self._begin()
            except Exception as ex:             # noqa: BLE001  shown on the page; the loop goes on
                self.step_error = repr(ex)
                raise
        if ctx is None:
            return
        boards, folder, stopped = ctx
        try:
            now = self.clock()
            for b in boards:
                b.discover(folder)
                b.index(now, INDEX_WORDS)
        except Exception as ex:                 # noqa: BLE001
            with self.lock:
                self.step_error = repr(ex)
            raise
        with self.lock:
            if self.folder != folder or list(self.boards.values()) != boards:
                return                          # a new run began meanwhile
            try:
                self._advance(stopped)
                self.step_error = None
            except Exception as ex:             # noqa: BLE001
                self.step_error = repr(ex)
                raise

    def _begin(self):
        a = self.a
        folder = a.run or self.follow or newest_run_folder(a.data_path)
        if folder != self.folder or self.restart or (folder and self._run_replaced(folder)):
            # a run that starts while we watch is read from its start; the one running when the
            # dashboard started only from then on (unless --from-start); a run already finished then
            # is read whole
            from_start = bool(a.replay or a.from_start or self.restart or self.folder is not None)
            finished = (not from_start and folder is not None and self.folder is None
                        and folder_quiet(folder, a.stall_s))
            self.restart = False
            self.reset(folder, from_start or finished)
            if finished:
                self.finished_at_start = self.stop_notice = True
        if folder is not None and self.stopped_folder == os.path.abspath(folder):
            self.stop_notice = True
        if folder is None:
            self.status = f"no run folder under {a.data_path}"
            return None
        if self.skip_existing and self.skip_marks is None:
            self.skip_marks = {}
            for p in glob.glob(os.path.join(folder, "*.fsu")):
                try:
                    self.skip_marks[p] = os.stat(p).st_size // 4
                except OSError:
                    pass
        self._collect()
        return list(self.boards.values()), folder, self.stop_notice

    def _holding(self, now, boards):
        """Couples that hold the finder back: written within --hold-s (or --stall-s), or of a board
        whose files are not all indexed yet."""
        span = max(self.a.hold_s, self.a.stall_s)
        return [(b, c) for b in boards for c in range(8)
                if b.newest[c] >= 0 and (now - b.newest_wall[c] < span or b.behind())]

    def _advance(self, stopped):
        a = self.a
        now = self.clock()
        S = self.setup
        boards = list(self.boards.values())
        self.others = {int(m.group(1)) for p in glob.glob(os.path.join(self.folder, "*.fsu"))
                       if (m := FILE_RE.search(p)) and int(m.group(1)) not in self.boards}
        self.behind = any(b.behind() for b in boards)

        # 1. what the index shows (a replay: up to the replay time)
        limit = None
        if a.replay:
            if self.replay_first is None:
                firsts = [int(s.arrays()["t_first"].min()) for b in boards for s in b.scans.values() if s.n]
                if firsts:
                    self.replay_first = min(firsts); self.replay_t0 = now
            if self.replay_first is not None:
                limit = self.replay_first + int((now - self.replay_t0) * a.replay_speed * PS)
        for b in boards:
            b.observe(now, limit)
        if a.replay and self.replay_first is not None and not self.behind and all(b.all_seen() for b in boards):
            stopped = True                      # the replay has shown the whole run
        if self.skip_existing:
            if not all(b.ready_to_skip(self.skip_marks) for b in boards):
                self.status = f"reading what is already on disk ({sum(self.skip_marks.values()) * 4 / 1e9:.1f} GB)"
                return
            tops = [t for t in (b.skip_to(self.skip_marks) for b in boards) if t is not None]
            self.skip_existing = False
            if tops:
                # read from just after the newest hit on disk, on the finder's clock (offsets removed): a hit
                # skipped there is before this, whatever its channel's offset; what is older and comes later
                # is counted apart ("before the dashboard started"), not as late
                self.since = max(tops) + 1 + S.max_offset_ps
                self.t_first = self.W = self.since

        # 2. the DAQ tab: every couple aggregate once, as it reaches the disk; at most 4 tasks per worker
        #    out (the rest waits on disk: the finder does not pass it, see untaken_t)
        late_before = self.W if self.W is not None else -(1 << 62)
        room = 4 * max(1, a.workers) - sum(1 for k, *_ in self.pending.values() if k == "ingest")
        for i in range(len(boards)):
            if room <= 0:
                break
            b = boards[(self.rr + i) % len(boards)]
            for path, sel in b.take_new(room * BATCH_HITS):
                cum = np.cumsum(sel["nev"])
                cuts = np.searchsorted(cum, np.arange(BATCH_HITS, int(cum[-1]), BATCH_HITS))
                for lo, hi in zip(np.r_[0, cuts], np.r_[cuts, sel["nev"].size]):
                    if hi > lo:
                        self._submit("ingest", dict(path=path, aggs={k: v[lo:hi] for k, v in sel.items()}, bi=b.bi,
                                                    late_before=late_before, early_before=self.since,
                                                    n_slots=S.chans.n_slots, board_channels=S.chans.board_channels))
                        room -= 1
        self.rr += 1
        untaken = [t for t in (b.untaken_first() for b in boards) if t is not None]
        self.untaken_t = min(untaken) if untaken else None

        # 3. the track finder: time slices up to where every couple still writing is on disk
        seen = [(b, c) for b in boards for c in range(8) if b.newest[c] >= 0]
        if not seen:
            self.status = "no data yet" + ("" if self.behind else f" (no files of {', '.join(f'Digi-{b.serial}' for b in boards)})")
            self.mode = "waiting"
            return
        if self.first_data_wall is None:
            self.first_data_wall = now
        if self.t_first is None:
            # wait a moment for every board's first data, so a slower board's first hits are not late
            missing = [b.serial for b in boards if b.newest.max() < 0]
            if missing and now - self.first_data_wall < a.hold_s and not stopped:
                self.status = (f"waiting up to {a.hold_s:.0f} s for the first data of "
                               + ", ".join(f"Digi-{x}" for x in missing))
                self.mode = "waiting"
                return
            self.t_first = min(b.first for b in boards if b.first is not None)
            self.W = self.t_first - S.max_offset_ps   # a hit's time on the finder's clock can be that much earlier
        top = max(int(b.newest[c]) for b, c in seen)
        writing = any(now - b.newest_wall[c] < a.stall_s for b, c in seen)
        self.quiet_s = float(now - max(b.newest_wall[c] for b, c in seen))
        holding = self._holding(now, boards)
        margin = self.margin_ps()
        if stopped and not self.behind:
            mode = "stopped"                    # FSUDAQ stopped the run (or it was over): all of it is on disk
            target = top + margin + 1
        elif writing or self.behind:
            mode = "running"
            target = min(int(b.newest[c]) for b, c in holding) - margin
        elif self.quiet_s < a.hold_s:
            mode = "paused"                     # nothing written for a while: a beam pause holds data in the
            target = min(int(b.newest[c]) for b, c in holding) - margin   # boards' buffers; wait for them
        else:
            mode = "quiet"                      # nothing written for --hold-s: process all that is on disk
            target = top + margin + 1
            if self.mode != "quiet":
                self.flush_wall = now           # the finder goes on without the couples silent in the pause
        self.mode = mode
        if self.untaken_t is not None:          # nothing the DAQ part has not had: its hits would count twice
            target = min(target, self.untaken_t - margin)
        n_slices = sum(1 for k, *_ in self.pending.values() if k == "slice")
        n_ingest = len(self.pending) - n_slices
        max_slice = int(a.max_slice_s * PS); min_step = int(a.min_step_s * PS)
        while self.W < target and n_slices < 2 * max(1, a.workers):
            if target - self.W < min_step and mode == "running":
                break                           # too thin a slice: wait for more
            t1 = min(target, self.W + max_slice)
            files = [(b.bi, p, A) for b in boards for p, A in b.overlapping(self.W - margin, t1 + margin)]
            self._submit("slice", dict(t0=int(self.W), t1=int(t1), margin=int(margin), files=files, ref=self.ref_slot))
            self.W = t1; n_slices += 1
        all_done = self.W >= target and not self.pending and self.untaken_t is None and not self.behind
        if mode == "running":
            if self.behind:
                self.status = "falling behind: reading the files"
            elif self.untaken_t is not None or n_ingest >= 4 * max(1, a.workers):
                self.status = "falling behind: the DAQ part"
            elif self.W < target and n_slices >= 2 * max(1, a.workers):
                self.status = "falling behind: every worker is busy"
            else:
                self.status = "running"
        elif mode == "stopped":
            what = "last run (finished before the dashboard started)" if self.finished_at_start else "run stopped"
            self.status = f"{what}: " + ("everything processed" if all_done else "processing what is on disk")
        elif mode == "paused":
            self.status = (f"no new data for {self.quiet_s:.0f} s (a pause?): the finder waits up to {a.hold_s:.0f} s "
                           "for hits still in the boards' buffers")
        else:
            self.status = f"no new data for {self.quiet_s:.0f} s: " + ("everything on disk processed" if all_done
                                                                      else "processing what is on disk")
        for b in boards:
            b.release(self.W - margin - int(a.hold_s * PS))
        # tasks that never come back (a hung worker) are said on the page, see state()

    def _collect(self):
        again = []                              # (kind, task, tries, epoch) to run (again)
        broken = False
        for fut in [f for f in self.pending if f.done()]:
            kind, task, t_sub, tries, epoch = self.pending.pop(fut)
            try:
                r = fut.result()
            except Exception as ex:             # noqa: BLE001  BrokenProcessPool: a worker died
                broken = broken or isinstance(ex, BrokenProcessPool)
                if tries == 0:
                    say(f"{kind} task failed ({ex!r}); running it again")
                    again.append((kind, task, 1, epoch))
                else:
                    self.lost += 1
                    say(f"{kind} task failed twice ({ex!r}); its data are missing from the dashboard")
                continue
            self._fold(kind, r, epoch)
        if broken:
            say("a worker process died; the workers are restarted")
            for fut, (kind, task, t_sub, tries, epoch) in self.pending.items():
                again.append((kind, task, tries, epoch))   # out with the dead pool: run them again
            self.pending = {}
            self._make_pool()
        for kind, task, tries, epoch in again:
            self._submit(kind, task, tries, epoch)

    def _fold(self, kind, r, epoch):
        with self.lock:
            self.last_fold_wall = self.clock()
            self.proc_s += r["dt"]; self.steps += 1
            if kind == "ingest":
                self.ingested_hits[r["bi"]] += r["n"]
                secs, slots, cnt = r["secs"]
                n_slots = self.setup.chans.n_slots
                for s in np.unique(secs):
                    row = self.secs.get(int(s))
                    if row is None:
                        row = self.secs[int(s)] = np.zeros(n_slots, np.int64)
                    m = secs == s
                    np.add.at(row, slots[m], cnt[m])
                if self.secs:
                    newest = max(self.secs)
                    for s in [s for s in self.secs if s < newest - HISTORY_S]:
                        del self.secs[s]
                if epoch == self.epoch:
                    self.daq.add(r["daq"])
                    n_late = int(np.asarray(r["daq"]["late"]).sum())
                    if n_late:
                        self.late_log.append((self.clock(), n_late))
            else:
                res = r["res"]
                if epoch == self.epoch:
                    for k in ff.Result.ARRAYS:
                        getattr(self.acc, k)[...] += res[k]
                    for k in ff.Result.SCALARS:
                        setattr(self.acc, k, getattr(self.acc, k) + res[k])
                    d = r["daq"]
                    if r["ref"] != self.ref_slot:   # made before the reference channel changed: keep all but dt
                        d = dict(d); d["dt"] = np.zeros_like(d["dt"])
                    self.daq.add(d)
                    self.owned += r["owned"]
                n_trk = int((res["h_ntrk"] * np.arange(ff.NTRK)).sum())
                status = {ff.STATUS[i]: int(c) for i, c in enumerate(res["h_status"]) if c}
                self.slices.append((r["t0"], r["t1"], int(res["n_bunches"]), status, n_trk))
                newest = max(s[1] for s in self.slices)
                self.slices = [s for s in self.slices if s[1] > newest - HISTORY_S * PS]

    # ------------------------------------------------------------ what the page shows
    def _finder_done(self):
        """Data time up to which every slice has come back."""
        t0s = [task["t0"] for kind, task, *_ in self.pending.values() if kind == "slice"]
        if self.W is None:
            return None
        return min(t0s) if t0s else self.W

    def state(self) -> dict:
        with self.lock:
            now = self.clock()
            S, A, ch = self.setup, self.acc, self.setup.chans
            boards = []
            late, early = self.daq.late, self.daq.early
            pending = list(self.pending.values())
            running = self.mode == "running"
            for sn, b in self.boards.items():
                seen = b.newest >= 0
                top = int(b.newest.max()) if seen.any() else None
                t_ref = self.t_first if self.t_first is not None else min((x.first for x in self.boards.values() if x.first is not None), default=None)
                quiet = None if not seen.any() else float(now - b.newest_wall[seen].max())
                sl = slice(b.bi * ch.board_channels, (b.bi + 1) * ch.board_channels)
                boards.append(dict(serial=sn, id=b.bi, **b.summary(now), hits=int(b.hits), read_hits=int(self.ingested_hits[b.bi]),
                                   late=int(late[sl].sum()), early=int(early[sl].sum()), has_data=top is not None,
                                   newest_s=None if top is None or t_ref is None else (top - t_ref) / PS,
                                   quiet_s=quiet,
                                   # silent while the others write: worth a look (not after the run ended)
                                   quiet_alarm=bool(running and quiet is not None and quiet >= self.a.stall_s),
                                   slow_couples=[int(c) for c in range(8) if running and seen[c] and now - b.newest_wall[c] >= self.a.stall_s
                                                 and now - b.newest_wall.max() < self.a.stall_s]))
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
            tb = S.tables
            done = self._finder_done()
            tops = [int(b.newest.max()) for b in self.boards.values() if b.newest.max() >= 0]
            newest = max(tops) if tops and self.t_first is not None else None
            ages = [now - t_sub for _, _, t_sub, *_ in pending]
            health = []
            if not FAST:
                health.append("numba is not installed (online/setup-venv.sh): the analysis cannot keep up")
            if ages and max(ages) > self.a.task_timeout:
                health.append(f"a task has not come back for {max(ages):.0f} s: the analysis is stalled")
            if self.lost:
                health.append(f"{self.lost} task(s) failed twice: their data are missing")
            if self.others:
                health.append("files of boards not analysed: " + ", ".join(str(s) for s in sorted(self.others))
                              + " (not in --boards)")
            if self.status.startswith("falling behind"):
                health.append(f"the analysis is {self.status}: what the page shows lags the data")
            for b in boards:
                if self.t_first is not None and not b["has_data"] and (running or self.mode == "paused"):
                    health.append(f"Digi-{b['serial']}: no data in this run")
                elif b["quiet_alarm"]:
                    health.append(f"Digi-{b['serial']}: nothing written for {b['quiet_s']:.0f} s while the other boards write")
                if b["gaps"]:
                    health.append(f"Digi-{b['serial']}: {b['gaps']:,} board aggregates missing from the files (counter gaps): "
                                  "data lost before they reached the disk")
                if b["skipped_words"]:
                    health.append(f"Digi-{b['serial']}: {b['skipped_words'] * 4:,} bytes of damaged data stepped over")
                if b["other_format"]:
                    health.append(f"Digi-{b['serial']}: {b['other_format']:,} couple aggregates in a format the online "
                                  "decoder does not read (extras option 2 with time tag and charges needed); left out")
                for x in b["stuck"]:
                    if x["s"] > self.a.stall_s:
                        health.append(f"Digi-{b['serial']}: the last {x['bytes']:,} bytes of {x['file']} cannot be read "
                                      f"(for {x['s']:.0f} s)")
            if self.step_error:
                health.append(f"the analysis loop fails: {self.step_error}")
            while self.late_log and now - self.late_log[0][0] > LATE_RECENT_S:
                self.late_log.popleft()
            n_late = int(late.sum())
            return dict(
                server_time=now, folder=self.folder, status=self.status, mode=self.mode, kernels="numba" if FAST else "numpy",
                workers=self.a.workers, in_flight=len(self.pending), oldest_task_s=max(ages) if ages else None,
                last_result_s=None if self.last_fold_wall is None else now - self.last_fold_wall,
                replay=bool(self.a.replay), window_ns=self.a.window, hold_s=self.a.hold_s, stall_s=self.a.stall_s,
                quiet_s=self.quiet_s, finished_at_start=self.finished_at_start,
                live_start=self.since is not None, since_wall=self.reset_wall,
                processed_s=None if done is None or self.t_first is None else (done - self.t_first) / PS,
                newest_s=None if newest is None else (newest - self.t_first) / PS,
                hits=self.owned, late=n_late, late_recent=int(sum(n for _, n in self.late_log)), late_recent_s=LATE_RECENT_S,
                early=int(early.sum()),
                pause_flush_s=None if self.flush_wall is None else now - self.flush_wall,
                bunches=A.n_bunches, unmapped=A.n_bad,
                proc_s=self.proc_s, steps=self.steps, health=health,
                warnings=S.warnings, problems=S.problems,
                boards=boards,
                finder=dict(setting=tb.setting, setting_src=S.src.get("setting"), map=S.map_path, map_rows=tb.map_n,
                            calibrated=tb.calibrated, cabling=S.cabling,
                            installed=[c for c, i in zip(ff.COUNTERS, ch.installed) if i],
                            missing=[c for c, i in zip(ff.COUNTERS, ch.installed) if not i],
                            channels=[len(o) - 1 for o in occ], timing="offsets file" if S.has_offsets else "no offsets",
                            fast=int(A.n_fast[0]), reference=A.n_slow, reference_s=A.slow_s, overflow=A.n_over,
                            k=None if math.isinf(tb.k) else tb.k, nsig=tb.nsig, threshold=tb.threshold,
                            uncalibrated=len(ch.uncalibrated)),
                statuses=list(ff.STATUS),
                status_counts={ff.STATUS[i]: int(c) for i, c in enumerate(A.h_status)},
                history=self._slice_history(),
                hist_p=dict(edges=ff.P_EDGES.tolist(), counts=A.h_p.tolist()),
                hist_res=dict(edges=ff.RES_EDGES.tolist(), t10=A.h_res[0].tolist(), t21=A.h_res[1].tolist()),
                hist_dt=dict(edges=ff.DT_EDGES.tolist(), counts=A.h_dt.tolist()),
                hist_r=dict(edges=ff.R_EDGES.tolist(), counts=A.h_r.tolist()),
                hist_cl=A.h_cl.tolist(), hist_ncand=A.h_ncand.tolist(), hist_ntrk=A.h_ntrk.tolist(),
                hist_nhit=A.h_nhit.tolist(), lucite=dict(yes=int(A.h_lucite[0]), no=int(A.h_lucite[1])),
                occupancy=occ, counters=counters,
                daq=self._daq_state(boards, newest, done),
            )

    def _slice_history(self):
        """The finder's slices in time order, merged into 1 s steps."""
        if self.t_first is None:
            return []
        out = []
        for t0, t1, nb, st, ntrk in sorted(self.slices):
            if out and t1 - out[-1]["t0_ps"] < PS and out[-1]["t1_ps"] == t0:
                o = out[-1]
                o["t1_ps"] = t1; o["bunches"] += nb; o["tracks"] += ntrk
                for k, v in st.items():
                    o["status"][k] = o["status"].get(k, 0) + v
            else:
                out.append(dict(t0_ps=t0, t1_ps=t1, bunches=nb, status=dict(st), tracks=ntrk))
        return [dict(t0=(o["t0_ps"] - self.t_first) / PS, t1=(o["t1_ps"] - self.t_first) / PS, bunches=o["bunches"],
                     status=o["status"], tracks=o["tracks"]) for o in out]

    def _complete_seconds(self):
        """Whole seconds of data time inside the run whose hits from the couples still writing are all
        counted: on disk, given to the DAQ part, and its results back (a second the run starts or ends
        in is left out: its rate would read low). Also the couples that do not hold this back (silent
        for --hold-s, or not written yet: a slow couple's first buffer may still be filling): their count
        in those seconds is not known yet. Returns (seconds, those couples as (board index, couple))."""
        now = self.clock()
        boards = list(self.boards.values())
        holding = self._holding(now, boards)
        if holding:
            bound = min(int(b.newest[c]) for b, c in holding)
        else:
            tops = [int(b.newest.max()) for b in boards if b.newest.max() >= 0]
            if not tops:
                return [], set()
            bound = max(tops)                   # nothing writing: all is on disk, but the last second is cut
        out = [int(task["aggs"]["t_first"].min()) for kind, task, *_ in self.pending.values()
               if kind == "ingest" and task["aggs"]["t_first"].size]
        if self.untaken_t is not None:
            out.append(self.untaken_t)
        if out:
            bound = min(bound, min(out))        # counts not back yet
        last = bound // PS - 1
        firsts = [b.first for b in boards if b.first is not None]
        first = -(-min(firsts) // PS) if firsts else 0
        if self.t_first is not None:
            first = max(first, -(-self.t_first // PS))
        hold = {(b.bi, c) for b, c in holding}
        slow = {(b.bi, c) for b in boards for c in range(8) if (b.bi, c) not in hold and b.newest[c] < (last + 1) * PS}
        return [s for s in sorted(self.secs) if first <= s <= last], slow

    def _daq_state(self, boards, newest, done):
        D = self.daq
        secs, slow = self._complete_seconds()
        rate = {}
        if secs and self.mode == "running":     # the newest complete second, while data are written
            row = self.secs[secs[-1]]
            nbc = self.setup.chans.board_channels
            rate = {sl: (None if (sl // nbc, (sl % nbc) // 2) in slow else float(row[sl])) for sl in self.slots}
        try:
            du = shutil.disk_usage(self.folder or self.a.data_path or "/")
            disk = dict(free_gb=du.free / 1e9, total_gb=du.total / 1e9)
        except OSError:
            disk = None
        nbc = self.setup.chans.board_channels
        t0s = self.t_first // PS if self.t_first is not None else (secs[0] if secs else 0)
        known = [v for v in rate.values() if v is not None]
        return dict(
            slots=[dict(slot=sl, **self.labels[sl], count=int(D.counts[sl]), rate=rate.get(sl), late=int(D.late[sl]),
                        early=int(D.early[sl]), written=bool(self.boards[self.serials[sl // nbc]].newest[(sl % nbc) // 2] >= 0))
                   for sl in self.slots],
            ref=self.ref_slot, total_rate=sum(known) if rate else None, rate_unknown=len(rate) - len(known),
            rate_second=None if not rate else secs[-1] - t0s,
            lag_s=None if newest is None or done is None else max(0.0, (newest - done) / PS),
            in_flight=len(self.pending), backpressure=self.status.startswith("falling behind"),
            run_bytes=sum(b["bytes"] for b in boards), files=sum(b["files"] for b in boards), disk=disk, n_windows=D.n_seg,
            seconds=[dict(t=s - t0s, boards=[int(self.secs[s][bi * nbc:(bi + 1) * nbc].sum()) for bi in range(len(self.serials))])
                     for s in secs[-HISTORY_S:]])

    def channel(self, sl, view=None):
        """One channel's histograms for the DAQ tab (sl = -1: all channels together, spectrum only).
        view = (lo, hi, nb, log): the Qlong range [lo, hi) and binning the page shows."""
        with self.lock:
            D = self.daq
            f = 0.5 * (dh.F_EDGES[:-1] + dh.F_EDGES[1:])
            if sl == -1:
                n = float(D.spec_tot_n[0])
                return dict(slot=-1, spectrum=dict(f=f.tolist(), p=(D.spec_tot_p / n if n else D.spec_tot_p).tolist()),
                            windows=D.n_seg)
            if sl not in self.labels:
                return None
            n = float(D.spec_n[sl])
            t0s = self.t_first // PS if self.t_first is not None else 0
            rates = [[s - t0s, int(self.secs[s][sl])] for s in self._complete_seconds()[0]]
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
            return dict(slot=sl, **self.labels[sl], count=int(D.counts[sl]), late=int(D.late[sl]), early=int(D.early[sl]), ref=self.ref_slot,
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
        """The page's Clear: accumulated histograms start again; tasks already out are not added."""
        with self.lock:
            self.acc = ff.Result(self.setup.chans.n_slots)
            self.daq = dh.DaqResult(self.setup.chans.n_slots)
            self.owned = 0
            self.late_log.clear()
            self.epoch += 1

    def set_ref(self, sl):
        with self.lock:
            self.ref_slot = sl if sl in self.labels else None
            self.daq.dt[...] = 0


def folder_quiet(folder: str, quiet_s: float) -> bool:
    """No data file of the folder was written in the last quiet_s seconds (wall clock): a finished run."""
    times = []
    for p in glob.glob(os.path.join(folder, "*.fsu")):
        try:
            times.append(os.path.getmtime(p))
        except OSError:
            pass
    return bool(times) and time.time() - max(times) > quiet_s


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
            self.online.run_started(folder)
            say(f"{time.strftime('%H:%M:%S')} following run folder {folder}")
            self.send_response(204); self.end_headers(); return
        if self.path.startswith("/stopped"):     # FSUDAQ stopped a saving run and closed its files
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            folder = q.get("folder", [""])[0]
            if not folder:
                self.send_response(400); self.end_headers(); return
            self.online.run_stopped(folder)
            say(f"{time.strftime('%H:%M:%S')} run stopped: {folder}")
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


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--data-path", help="FSUDAQ data path; the newest run folder in it is followed")
    p.add_argument("--run", help="one run folder instead of following the data path")
    p.add_argument("--follow", help="run folder to follow until FSUDAQ names the next one (GET /run?folder=...)")
    p.add_argument("--replay", action="store_true", help="play a finished run at real-time pace")
    p.add_argument("--replay-speed", type=float, default=1.0)
    p.add_argument("--boards", default=DEFAULT_BOARDS, help="serials in detector-map board order")
    p.add_argument("--eight-channel-boards", default=EIGHT_CHANNEL_BOARDS, help="serials of 8-channel boards")
    p.add_argument("--config", default=CONFIG, help="the run's setting, cabling, gains, time offsets and map (JSON, "
                                                     "re-read for each run); the options below override it")
    p.add_argument("--setting", choices=("high", "low"), default=None, help="T1/T2 position installed for this run")
    p.add_argument("--map", default=None, help="momentum map (default finder/momentum_map.csv); columns per finder/INTERFACE.md")
    p.add_argument("--cabling", default=None, help="CSV serial,channel,kind,plane,ch (kind trk/veto/cherenkov/lucite)")
    p.add_argument("--gains", default=None, help="CSV serial,channel,mevee_per_count; without it the light tests are off")
    p.add_argument("--time-offsets", default=None, help="CSV serial,channel,offset_ns subtracted from each channel's time")
    p.add_argument("--ref-slot", type=int, default=None,
                   help="reference channel for the time differences (board index x 16 + channel); the page can change it")
    p.add_argument("--window", type=float, default=25.0,
                   help="bunch building: a gap longer than this starts a new bunch [ns]. 25 = the finder's +-10 ns gate "
                        "around each plane's expected arrival plus the 4.3 ns T0->T2 flight at beta 0.75")
    p.add_argument("--stall-s", type=float, default=5.0, help="no couple written for this long: a pause (or the run ended "
                                                              "without word from FSUDAQ)")
    p.add_argument("--hold-s", type=float, default=30.0, help="a couple silent this long no longer holds the finder back, "
                                                              "nor does a pause (its later hits are counted as late)")
    p.add_argument("--min-step-s", type=float, default=0.5, help="do not cut finder slices shorter than this while data come in")
    p.add_argument("--max-slice-s", type=float, default=2.0, help="the longest finder slice (memory per worker)")
    p.add_argument("--task-timeout", type=float, default=120.0, help="a task out longer than this is reported as stalled")
    p.add_argument("--interval", type=float, default=1.0, help="seconds between processing steps")
    p.add_argument("--from-start", action="store_true", help="read the run already running at start-up from its beginning")
    p.add_argument("--port", type=int, default=8050)
    p.add_argument("--workers", type=int, default=8, help="worker processes (0 = in the main loop)")
    p.add_argument("--open-browser", action="store_true", help="open the page in the default browser once the server is up")
    p.add_argument("--exit-with-parent", action="store_true", help="stop when the process that started us (FSUDAQ) is gone")
    a = p.parse_args(argv)
    if not a.run and not a.data_path:
        p.error("give --data-path or --run")
    return a


def main(argv=None):
    a = parse_args(argv)
    online = Online(a)
    Handler.online = online
    with open(os.path.join(HERE, "dashboard.html"), "rb") as fh:
        Handler.page = fh.read()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    if a.open_browser:
        import webbrowser
        threading.Thread(target=lambda: webbrowser.open(f"http://localhost:{a.port}/"), daemon=True).start()
    say(f"online dashboard: http://localhost:{a.port}/   kernels={'numba' if FAST else 'numpy (slow)'}  "
        f"{'replay of ' + a.run if a.replay else ('run ' + a.run if a.run else 'following ' + (a.follow or a.data_path))}")
    for w in online.setup.warnings + online.setup.problems:
        say("  setup: " + w)
    last_report = 0.0
    last_seen = None                     # (status, processed_s, hits): report only when this changes

    stopping = threading.Event()

    def _term(signum, frame):            # SIGTERM from FSUDAQ, or Ctrl-C: leave at the next step
        stopping.set(); online.wake.set()
    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)
    parent = os.getppid()
    try:
        while not stopping.is_set():
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
                        f"{s['hits']:,} hits in the finder, {s['late']:,} late, {s['bunches']:,} bunches, "
                        f"{s['status_counts']['unique'] + s['status_counts']['tracks']:,} accepted, {s['proc_s']:.1f} s worker CPU, "
                        f"{s['in_flight']} tasks out")
                    last_seen = seen
                last_report = t0
            online.wake.wait(max(0.0, a.interval - (time.time() - t0)))
            online.wake.clear()
    finally:
        try:
            srv.shutdown()
        except Exception:
            pass
        # kill the workers directly and leave without the pool's exit handlers, which wait on a
        # queue lock a killed worker may hold (the port would stay taken)
        for child in mp.active_children():
            child.kill()
        say("online dashboard stopped")
        os._exit(0)


if __name__ == "__main__":
    main()
