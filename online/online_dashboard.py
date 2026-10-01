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
builds events and classifies them with the same code as the offline pipeline, and
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
import multiprocessing as mp
import os
import re
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
import pipeline as pl                        # noqa: E402
import online_classify as oc                 # noqa: E402
from detector import AUX_COUNTERS, ChannelMap, parse_planes   # noqa: E402
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
P_BINS = np.linspace(0, 1500, 151)            # MeV/c
TOF_BINS = np.linspace(-20, 100, 121)         # ns
NHIT_BINS = np.arange(0, 41)                  # hits per event
HISTORY_S = 600                               # seconds of rate history kept


def _index(words):
    return fsu_fast.index_file(words) if FAST else fsu.index_file(words)


def _decode(words, ix):
    if FAST:
        return fsu_fast.decode_range(words, ix)
    d = fsu._decode_chunk(words, ix, 0, len(ix.nev))
    if d is None:
        return np.zeros(0, np.int64), np.zeros(0, np.uint8), np.zeros(0, np.uint16)
    return d["ts_ps"].astype(np.int64), d["ch"], d["qlong"]


# ---------------------------------------------------------------- slice processing (in a worker)
# Same scheme as pipeline_fsu.py's time-slice mode: a worker decodes the files that
# overlap its slice [t0, t1) with a margin on both sides, sorts, builds events and keeps
# those whose FIRST hit lies in the slice. Nothing large crosses a process boundary and
# no event is counted twice at a boundary.
_G: dict = {}


def _init_worker(cmap, lut, dense, window_units, to_ns):
    _G.update(cmap=cmap, lut=lut, dense=dense, window_units=window_units, to_ns=to_ns)


def _decode_file_range(path, t_lo, t_hi):
    words = np.memmap(path, dtype="<u4", mode="r")
    ix = _index(words)
    if len(ix.nev) == 0:
        return None
    if FAST:
        return fsu_fast.decode_range(words, ix, t_lo, t_hi)
    ts, ch, e = _decode(words, ix)
    m = (ts >= t_lo) & (ts < t_hi)
    return ts[m], ch[m], e[m]


def process_slice(task: dict) -> dict:
    """task: t0, t1, margin (ps), files {board_id: [paths]}. Returns increments only."""
    t_start = time.perf_counter()
    t0, t1, margin = task["t0"], task["t1"], task["margin"]
    cmap, lut, dense, W_units, to_ns = _G["cmap"], _G["lut"], _G["dense"], _G["window_units"], _G["to_ns"]
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
            ts, ch, e = r
            parts.append((ts, ch, e, np.full(ts.size, bid, np.int16)))
            hits_by_board[bid] = hits_by_board.get(bid, 0) + int(np.count_nonzero((ts >= t0) & (ts < t1)))
    n_ch = cmap.n_channels
    out = dict(W=t1, n_hits=0, n_ev=0, n_bad=0, hits_by_board=hits_by_board, counts={}, tags={},
               chan_counts=np.zeros(n_ch + 1, np.int64), nhit=np.zeros(NHIT_BINS.size - 1, np.int64), hp=None, htof=None)
    if not parts:
        out["dt"] = time.perf_counter() - t_start
        return out
    ts = np.concatenate([p[0] for p in parts]); ch = np.concatenate([p[1] for p in parts])
    e = np.concatenate([p[2] for p in parts]); bd = np.concatenate([p[3] for p in parts])
    order = np.argsort(ts)
    if FAST:
        evch, tof, aux, n_ev, n_bad, n_hits = fsu_fast.build_events(
            ts, bd, ch, e, order, cmap, lut, W_units, to_ns, t0, t1, out["chan_counts"], out["nhit"])
    else:
        block = {"Timestamp": ts[order], "Board": bd[order], "Channel": ch[order], "Energy": e[order]}
        starts = pl._starts_new_event(block["Timestamp"], W_units)
        first = block["Timestamp"][starts]
        keep_ev = (first >= t0) & (first < t1)
        ev = np.cumsum(starts) - 1
        keep_hit = keep_ev[ev]
        block = {k: v[keep_hit] for k, v in block.items()}
        res = pl.Result()
        if block["Timestamp"].size:
            evch, tof, aux, n_ev = pl.build_events(block, cmap, lut, W_units, to_ns, res)
        else:
            evch = np.zeros((0, 3), np.int32); tof = np.zeros(0); aux = {}; n_ev = 0
        n_bad = res.unmapped; n_hits = int(block["Timestamp"].size)
        g = lut[np.clip(block["Board"], 0, lut.shape[0] - 1), np.clip(block["Channel"], 0, lut.shape[1] - 1)]
        g = np.where((block["Board"] >= lut.shape[0]) | (block["Channel"] >= lut.shape[1]) | (g < 0), n_ch, g)
        out["chan_counts"] += np.bincount(g, minlength=n_ch + 1)
        if n_hits:
            st = pl._starts_new_event(block["Timestamp"], W_units)
            sizes = np.diff(np.flatnonzero(np.concatenate([st, [True]])))
            out["nhit"] += np.histogram(np.clip(sizes, 0, NHIT_BINS[-1]), NHIT_BINS)[0]
    out["n_hits"] = int(n_hits); out["n_ev"] = int(n_ev); out["n_bad"] = int(n_bad)
    if n_ev:
        cls, p, _s, tags = pl.classify_vector(evch, tof, aux, dense)
        codes, n_each = np.unique(cls, return_counts=True)
        out["counts"] = {pl.CLASSES[c]: int(n) for c, n in zip(codes, n_each)}
        out["tags"] = tags
        good = np.isfinite(p)
        out["hp"] = np.histogram(p[good], P_BINS)[0]
        both = (evch[:, 0] > 0) & (evch[:, 2] > 0)
        out["htof"] = np.histogram(tof[both], TOF_BINS)[0]
    out["dt"] = time.perf_counter() - t_start
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
        aux = tuple(x.strip() for x in a.aux.split(",") if x.strip())
        self.cmap = ChannelMap(parse_planes(a.planes), a.board_channels, aux)
        table = oc.load_map(a.map, a.setting)
        (self.cmap, self.table, self.dense, self.lut, self.window_units,
         self.to_ns, _) = pl._prepare(table, self.cmap, a.map, a.setting, "ps", a.window)
        self.lag_ps = int(a.lag_ms * 1e9)
        self.margin = self.lag_ps + int(self.window_units)
        self.lock = threading.Lock()
        self.pool = None
        if a.workers > 0:
            ctx = mp.get_context("fork")
            self.pool = ctx.Pool(a.workers, initializer=_init_worker,
                                 initargs=(self.cmap, self.lut, self.dense, self.window_units, self.to_ns))
        else:
            _init_worker(self.cmap, self.lut, self.dense, self.window_units, self.to_ns)
        self.pending: list = []
        self.follow = a.follow                  # run folder FSUDAQ named (--follow or GET /run); None = newest in the data path
        self.restart = False                    # set by GET /run: a new run starts, even in the same folder
        self.reset(None)

    def reset(self, folder):
        self.folder = folder
        self.boards = {sn: BoardFiles(sn, i) for i, sn in enumerate(self.serials)}
        self.W = None
        self.t_first = None
        self.hist_p = np.zeros(P_BINS.size - 1, np.int64)
        self.hist_tof = np.zeros(TOF_BINS.size - 1, np.int64)
        self.hist_nhit = np.zeros(NHIT_BINS.size - 1, np.int64)
        self.chan_counts = np.zeros(self.cmap.n_channels + 1, np.int64)
        self.counts = dict.fromkeys(pl.CLASSES, 0)
        self.tags = dict.fromkeys(pl.TAGS, 0)
        self.events = 0; self.hits = 0; self.unmapped = 0
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
            print(f"inotify watcher stopped: {ex!r}", flush=True)

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
        task = dict(t0=int(t0), t1=int(t1), margin=int(self.margin), files=files)
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
                print(f"slice failed: {ex!r}", flush=True)

    def _fold(self, r: dict, now, bytes_by_board):
        with self.lock:
            self.hits += r["n_hits"]; self.unmapped += r["n_bad"]; self.events += r["n_ev"]
            self.chan_counts += r["chan_counts"]; self.hist_nhit += r["nhit"]
            for k, v in r["counts"].items():
                self.counts[k] += v
            for k, v in r["tags"].items():
                self.tags[k] += v
            if r["hp"] is not None:
                self.hist_p += r["hp"]; self.hist_tof += r["htof"]
            top = max(b.newest for b in self.boards.values() if b.newest is not None)
            beam_s = (min(r["W"], top) - self.t_first) * 1e-12
            self.history.append((now, beam_s, r["hits_by_board"], bytes_by_board, r["n_ev"], r["counts"]))
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
            return dict(
                folder=self.folder, status=self.status, kernels="numba" if FAST else "numpy",
                workers=self.a.workers, in_flight=len(self.pending),
                replay=bool(self.a.replay), lag_ms=self.a.lag_ms, window_ns=self.a.window,
                processed_s=None if self.W is None or self.t_first is None else (self.W - self.t_first) * 1e-12,
                hits=self.hits, events=self.events, unmapped=self.unmapped,
                proc_s=self.proc_s, steps=self.steps,
                counts=self.counts, tags=self.tags, classes=list(pl.CLASSES),
                boards=boards,
                history=[dict(wall=h[0], beam_s=h[1], hits=h[2], bytes=h[3], events=h[4], counts=h[5]) for h in hist],
                hist_p=dict(edges=P_BINS.tolist(), counts=self.hist_p.tolist()),
                hist_tof=dict(edges=TOF_BINS.tolist(), counts=self.hist_tof.tolist()),
                hist_nhit=dict(edges=NHIT_BINS.tolist(), counts=self.hist_nhit.tolist()),
                channels=dict(counts=self.chan_counts.tolist(), plane=self.cmap.plane.tolist(),
                              board=self.cmap.board.tolist(), channel=self.cmap.channel.tolist()),
            )


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
            print(f"{time.strftime('%H:%M:%S')} following run folder {folder}", flush=True)
            self.send_response(204); self.end_headers(); return
        if self.path.startswith("/state"):
            body = json.dumps(self.online.state()).encode()
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
    p.add_argument("--map", default=os.path.join(HERE, "momentum_map.csv"))
    p.add_argument("--setting", choices=("high", "low"), default=oc.SETTING)
    p.add_argument("--planes", default="deployed")
    p.add_argument("--board-channels", type=int, default=16)
    p.add_argument("--aux", default=",".join(AUX_COUNTERS))
    p.add_argument("--window", type=float, default=100.0, help="event-building window [ns]")
    p.add_argument("--lag-ms", type=float, default=100.0, help="merge watermark margin [ms]")
    p.add_argument("--stall-s", type=float, default=5.0, help="a board silent this long no longer holds the watermark")
    p.add_argument("--min-step-s", type=float, default=1.0, help="do not cut slices shorter than this (edge decode cost)")
    p.add_argument("--interval", type=float, default=1.0, help="seconds between processing steps (inotify wakes it earlier)")
    p.add_argument("--from-start", action="store_true", help="live mode: process the run from its first file instead of from now")
    p.add_argument("--port", type=int, default=8050)
    p.add_argument("--workers", type=int, default=4, help="processes for sort/build/classify (0 = in the main loop)")
    p.add_argument("--open-browser", action="store_true", help="open the page in the default browser once the server is up")
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
    print(f"online dashboard: http://localhost:{a.port}/   kernels={'numba' if FAST else 'numpy'}  "
          f"files={'inotify' if HAVE_INOTIFY and not a.replay else 'polling'}  "
          f"{'replay of ' + a.run if a.replay else ('run ' + a.run if a.run else 'following ' + (a.follow or a.data_path))}", flush=True)
    last_report = 0.0
    last_seen = None                     # (status, processed_s, hits): report only when this changes

    def _term(signum, frame):            # SIGTERM from FSUDAQ (or kill): leave like Ctrl-C
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _term)
    try:
        while True:
            t0 = time.time()
            try:
                online.step()
            except Exception as ex:          # keep serving; report the problem
                online.status = f"error: {ex!r}"
                print(f"step failed: {ex!r}", flush=True)
            if t0 - last_report > 10:
                s = online.state()
                seen = (s['status'], round(s['processed_s'] or 0, 1), s['hits'])
                if seen != last_seen:    # quiet while nothing happens (between runs, page closed)
                    print(f"{time.strftime('%H:%M:%S')} {s['status']}: processed {s['processed_s'] or 0:.1f} s of beam, "
                          f"{s['hits']:,} hits, {s['events']:,} events, {s['proc_s']:.1f} s worker CPU, "
                          f"{s['in_flight']} steps in flight", flush=True)
                    last_seen = seen
                last_report = t0
            online.wake.wait(max(0.0, a.interval - (time.time() - t0)))
            online.wake.clear()
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown()
        # Pool.terminate() waits on the task queue's lock; a worker killed while holding it (a signal
        # to the whole process group: Ctrl-C, timeout, systemd) makes that wait forever and the port
        # stays taken. Kill the workers directly and leave without the pool's exit handlers.
        for child in mp.active_children():
            child.kill()
        print("online dashboard stopped", flush=True)
        os._exit(0)


if __name__ == "__main__":
    main()
