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

It keeps running when FSUDAQ closes (FSUDAQ starts it detached and finds it again on its port). A
recorded run can be loaded for a look (GET /runs lists them, /load?folder=... reads one whole, /live
goes back); a run FSUDAQ starts takes the dashboard back to live.

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

Beam vs map. The floor tool (pion-spectrometer-simulations tool, Publish to online) writes what the live
data should look like for the current settings (--beam-expected, read again when it changes); GET
/expected compares it with the finder: map (sha256 after CRLF -> LF), setting and field first, then
the occupancy per plane and the momentum histogram as shapes (see compare_beam).

Run summaries. When a reading of a run ends with everything on disk processed (FSUDAQ's /stopped,
the stop in its run file, or no file written for FINISHED_QUIET_S), what the dashboard computed is
written to <run folder>/<run folder name>_summary.json (whole: a temporary file, then a rename):
the run's metadata, per board and channel totals, the finder's counts and histograms, the rate
history, the problems seen, the settings' files, and whether the dashboard saw the whole run
(complete, and why not). A reading that goes on (the run was written again) writes it again at
its end. Between runs (nothing taken for BACKFILL_IDLE_S), runs without a summary get one: one
at a time, read by a child process of this file (--summarise, nice 19, idle I/O) through the same
code, killed at once when a run starts. GET /summary?run=<folder name> serves a summary to anyone;
the page shows it in that browser only.
"""
from __future__ import annotations

import argparse
import collections
import concurrent.futures as cf
import csv
import datetime
import glob
import hashlib
import json
import math
import queue
import multiprocessing as mp
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from concurrent.futures.process import BrokenProcessPool
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _die_with_parent():
    """Linux: this process is killed when the one that started it ends (prctl PR_SET_PDEATHSIG)."""
    try:
        import ctypes
        ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL, 0, 0, 0)
    except (OSError, AttributeError):
        pass


if "--summarise" in sys.argv:   # the backfill child: from before the heavy imports on (summarise_main checks the parent)
    _die_with_parent()

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
PRODUCTION_PORT = 8050                        # FSUDAQ's dashboard: the one that writes run summaries by default
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
SLOW_KNOWN_S = 300                            # a channel pair silent this long is taken to have nothing more to write
FINISHED_QUIET_S = 300                        # a folder no file of which was written for this long: a finished run
                                              # (when its run file has no stop entry: an older run, or FSUDAQ died)
SUMMARY_FORMAT, SUMMARY_VERSION = "FSUDAQ online run summary", 1
RUN_BIN_S = 10                                # rate history of a whole run: hits and bunches per 10 s of data time
RUN_BINS_MAX = 200_000                        # bins kept at most (a damaged time tag far off must not grow it without end)
HISTORY_POINTS = 720                          # the summary's rate history: at most this many points (bins merged)
PROBLEMS_MAX = 50                             # distinct problems kept per run; more are only counted
SUMMARY_RETRY_S = 60                          # a summary that could not be written: tried again after this
SUMMARY_MAX_BYTES = 20 << 20                  # a summary file larger than this is not served
BACKFILL_IDLE_S = 120                         # backfill starts only after nothing was taken for this long
BACKFILL_MAX_S = 4 * 3600                     # a backfill taking longer is stopped, and the run skipped
OPEN_RUN_MAX_S = 12 * 3600                    # a run with no stop recorded counts as being taken (no backfill) until no file of it
                                              # grew for this long: beam pauses and shift changes are far shorter, and a run that
                                              # FSUDAQ really left open (it died) only delays the backfill by that much
OUTLIER_GAP_S = 3600                          # hits this far (in data time) from the rest, and few (OUTLIER_SHARE): damaged time tags
OUTLIER_SHARE = 1e-3
BACKFILL_LOOK_S = 30                          # how often the run folders are looked through for a run without summary


# ---------------------------------------------------------------- run configuration
WINDOW_NS = 25.0                              # bunch window when neither --window nor the config gives one
WINDOW_RANGE_NS = (1.0, 1000.0)


def initial_window(a):
    """The bunch window (ps) and where it came from: --window, else the config file's window_ns (the page
    saves it there), else WINDOW_NS."""
    if a.window is not None:
        return int(round(a.window * 1000)), "command line"
    try:
        with open(a.config) as f:
            v = float(json.load(f).get("window_ns"))
        if WINDOW_RANGE_NS[0] <= v <= WINDOW_RANGE_NS[1]:
            return int(round(v * 1000)), os.path.basename(a.config)
        say(f"window_ns {v} in {a.config} is out of {WINDOW_RANGE_NS} ns: {WINDOW_NS:g} ns used")
    except (OSError, ValueError, TypeError, AttributeError):
        pass                                  # no file, not JSON, no window_ns in it
    return int(round(WINDOW_NS * 1000)), "default"


def read_config_content(path):
    """(The config file's content as it is now, what the summary keeps of it; whether values JSON cannot
    hold, NaN or Infinity, were turned into text). (None, False) without one."""
    try:
        with open(path) as f:
            return _json_safe(json.load(f))
    except (OSError, ValueError, TypeError):
        return None, False


def _json_safe(x):
    """(x with NaN and Infinity as text, whether there were any): a summary must stay valid JSON."""
    if isinstance(x, float) and not math.isfinite(x):
        return repr(x), True
    if isinstance(x, dict):
        out = {k: _json_safe(v) for k, v in x.items()}
        return {k: v[0] for k, v in out.items()}, any(v[1] for v in out.values())
    if isinstance(x, list):
        out = [_json_safe(v) for v in x]
        return [v[0] for v in out], any(v[1] for v in out)
    return x, False


def file_sha256(path):
    if not path:
        return None
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


# what the finder reads besides the map (finder/online_finder.py load(), finder_fast.Tables): its constants
# (threshold, K, sigmas, plane positions), channel table and one-track light. map_summary.json and INTERFACE.md
# are not read.
FINDER_FILES = ("handoff_meta.json", "channels.csv", "pion_light.csv")


def setup_files_record(cfg):
    """{kind: {path, sha256} or None} of every file the analysis reads besides the data: cabling, gains,
    offsets and map (load_config's values; the map: the default one too), and the finder's own files."""
    out = {}
    for k in ("cabling", "gains", "time_offsets", "map"):
        path = cfg.get(k) or (os.path.join(ff.FINDER_DIR, "momentum_map.csv") if k == "map" else None)
        out[k] = None if not path else dict(path=os.path.abspath(path), sha256=file_sha256(path))
    for f in FINDER_FILES:
        path = os.path.join(ff.FINDER_DIR, f)
        out["finder/" + f] = dict(path=os.path.abspath(path), sha256=file_sha256(path))
    return out


# the analysis code as loaded (its constants: windows, bins, decisions): sha256 of each source file
CODE_FILES = ("online_dashboard.py", "finder_fast.py", "fsu_fast.py", "daq_hist.py", "finder/online_finder.py")
CODE_HASHES = {f: file_sha256(os.path.join(HERE, f)) for f in CODE_FILES}


def setup_identity(setting, record, window_ps, serials, nch):
    """What makes two analyses of the same files give the same summary: the beam setting, the bunch window,
    the content (sha256) of every file read (cabling, gains, offsets, map, the finder's constants, channel
    table and light), the analysis code (CODE_HASHES), the boards. Not the reference channel (its
    histograms are not in the summary)."""
    key = dict(setting=setting, window_ps=int(window_ps), files={k: v and v["sha256"] for k, v in record.items()},
               code=CODE_HASHES, boards=list(serials), eight=sorted(sn for sn in serials if nch[sn] == 8))
    return hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()[:16]


# ---------------------------------------------------------------- beam vs map: the floor tool's published expectation
EXPECTED_FORMAT = "floor-tool-expected/1"     # pion-spectrometer-simulations tool/README.md "Publish to online"
EXPECTED_DEFAULT = os.path.expanduser("~/pion-spectrometer-simulations/tool/published/expected.json")
EXPECTED_MAX_BYTES = 50 << 20
CHERENKOV_E_EFF = 1.0                         # share of electrons whose bunch the gas Cherenkov marks "electron" (assumed:
                                              # all; muons and pions below its threshold never fire it)


def map_identity(path):
    """sha256 of a momentum map's bytes after CRLF -> LF, as the floor tool's file_identity() (sha256_lf); None if
    it cannot be read."""
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read().replace(b"\r\n", b"\n")).hexdigest()
    except (OSError, TypeError):
        return None


def map_field(map_path):
    """(field_T, where from) of the map the finder loads: the handoff_meta.json beside it, else the finder's."""
    for meta in (os.path.join(os.path.dirname(os.path.abspath(map_path)), "handoff_meta.json"),
                 os.path.join(ff.FINDER_DIR, "handoff_meta.json")):
        try:
            with open(meta) as f:
                v = json.load(f).get("field_T")
            return (None if v is None else float(v)), meta
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return None, None


TRACKS_MAX = 3000                       # Beam vs map: the most frequent map triplets sent to the page


def finder_geometry(setting, tables):
    """Where the live finder puts its tracker channels and planes, to draw its accepted tracks (Beam vs map):
    finder/channels.csv a_lo_mm/a_hi_mm (lab x as the floor tool exports it, 1-based channels) for this setting,
    and the planes' distance from the pipe exit (handoff_meta.json planes_s_mm). codes: the map's triplets, in the
    order of Tables.map_p (= Result.h_trip)."""
    try:
        planes = [dict(s_mm=float(v), ch=[]) for v in tables.T["s_mm"]]
        with open(os.path.join(ff.FINDER_DIR, "channels.csv"), newline="") as f:
            for r in csv.DictReader(f):
                if r["setting"] == setting:
                    planes[int(r["plane"])]["ch"].append([int(r["ch"]), float(r["a_lo_mm"]), float(r["a_hi_mm"])])
        return dict(planes=planes, codes=[list(c) for c in tables.T["map"]], error=None)
    except (OSError, ValueError, KeyError, IndexError, TypeError) as ex:
        return dict(planes=None, codes=None, error=f"the finder's channels.csv was not read: {ex!r}")


class BeamExpected:
    """The floor tool's expected.json, read again whenever it changes (mtime, size). The tool writes it whole
    (temp file + rename), so it is never seen half written."""

    def __init__(self, path):
        self.path, self.key, self.doc, self.error = path, None, None, None

    def get(self):
        """(document or None, why not: None when there is one)."""
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            self.key, self.doc, self.error = None, None, f"nothing published yet ({self.path} not there)"
            return None, self.error
        except OSError as ex:
            return None, f"{self.path} not readable: {ex}"
        key = (st.st_mtime_ns, st.st_size)
        if key != self.key:
            self.key, self.doc, self.error = key, None, None
            try:
                if st.st_size > EXPECTED_MAX_BYTES:
                    raise ValueError(f"too large ({st.st_size:,} bytes)")
                with open(self.path, encoding="utf-8") as f:
                    d = json.load(f)
                if not isinstance(d, dict) or d.get("format") != EXPECTED_FORMAT:
                    raise ValueError(f"not a {EXPECTED_FORMAT} file (format {d.get('format') if isinstance(d, dict) else None!r})")
                for k in ("settings", "map", "planes"):
                    if not isinstance(d.get(k), dict):
                        raise ValueError(f"no '{k}' block")
                self.doc = _json_safe(d)[0]
            except (OSError, ValueError, TypeError) as ex:
                self.error = f"{os.path.basename(self.path)} cannot be used: {ex}"
        return self.doc, self.error


def _shape_stats(w, x):
    """Mean and sd of positions x weighted by w (a normalised histogram); (None, None) without weight."""
    w = np.asarray(w, float); x = np.asarray(x, float)
    s = w.sum()
    if not s > 0:
        return None, None
    m = float((w * x).sum() / s)
    return m, float(np.sqrt((w * (x - m) ** 2).sum() / s))


def compare_beam(exp, live, cherenkov_eff=CHERENKOV_E_EFF):
    """The live finder against the floor tool's expectation (pure: tested on its own).
    live: setting, setting_given, map, map_sha256_lf, map_field_T, placeholder (cabling), cherenkov (cabled),
    occupancy (the finder's, per plane, index 0 unused), hist_p (counts), p_edges.
    Returns mismatch (the momentum or channels are not comparable: shown first), notes, planes, momentum."""
    s, m = exp.get("settings") or {}, exp.get("map") or {}
    fm = m.get("finder_map")
    mismatch, notes = [], []
    if not fm:
        mismatch.append("the publish did not name the live finder's map: its momentum is read with the tool's own map "
                        "(not the live one), so the momentum shapes are not comparable")
    elif fm.get("sha256_lf") != live["map_sha256_lf"]:
        mismatch.append(f"the map the live finder loaded ({live['map']}, sha256 {str(live['map_sha256_lf'])[:12]}…) is not the "
                        f"one the publish read ({fm.get('path')}, sha256 {str(fm.get('sha256_lf'))[:12]}…): the momenta are "
                        "not comparable")
    if s.get("setting") is None:
        mismatch.append("the published table positions are at neither slot preset: no finder setting matches them")
    elif s["setting"] != live["setting"]:
        mismatch.append(f"the publish is for setting '{s['setting']}', the live finder uses '{live['setting']}'"
                        + ("" if live["setting_given"] else " (not given in experiment.json: assumed)")
                        + ": the momenta are not comparable")
    if s.get("field_T") is not None and live["map_field_T"] is not None and abs(float(s["field_T"]) - live["map_field_T"]) > 1e-6:
        mismatch.append(f"the publish is for {s['field_T']} T, the live map was built for {live['map_field_T']} T: the momentum "
                        "numbers are not comparable (another field needs another map)")
    elif live["map_field_T"] is None:
        notes.append("the live map's field is not known (no handoff_meta.json): not compared")
    if not live["setting_given"] and s.get("setting") == live["setting"]:
        notes.append(f"the live finder's setting '{live['setting']}' is assumed (not given in experiment.json): the agreement "
                     "with the publish is only as good as that assumption")
    if live["placeholder"]:
        notes.append("cabling is the sequential placeholder: the channel numbers are not known to be the detector's, so "
                     "the per-channel comparison is shape only")

    planes = {}
    for name, P in (exp.get("planes") or {}).items():
        if not isinstance(P, dict) or "plane" not in P:
            continue
        i, n = int(P["plane"]), int(P["n_channels"])
        occ = live["occupancy"][i][1:] if i < len(live["occupancy"]) else []
        note = "" if len(occ) == n else f"the live finder has {len(occ)} channels on this plane, the publish {n}"
        L = (list(occ) + [0] * n)[:n]
        tot = float(sum(L))
        ls = [v / tot for v in L] if tot > 0 else None
        es = [float(v) for v in P["share"]]
        ch = list(range(1, n + 1))
        xc = [(float(a) + float(b)) / 2 for a, b in zip(P["x_lo_mm"], P["x_hi_mm"])]
        lm, lsd = _shape_stats(ls, ch) if ls else (None, None)
        em, esd = _shape_stats(es, ch)
        lmm, lsdmm = _shape_stats(ls, xc) if ls else (None, None)
        emm, esdmm = _shape_stats(es, xc)
        planes[name] = dict(plane=i, n_channels=n, live_hits=int(tot), live_share=ls, expected_share=es, note=note,
                            x_lo_mm=P["x_lo_mm"], x_hi_mm=P["x_hi_mm"],
                            centroid_ch=dict(live=lm, expected=em), width_ch=dict(live=lsd, expected=esd),
                            centroid_mm=dict(live=lmm, expected=emm), width_mm=dict(live=lsdmm, expected=esdmm),
                            overlap=None if ls is None else float(sum(min(a, b) for a, b in zip(ls, es))))

    mom = None
    M = exp.get("momentum")
    if M:
        r, which = M.get("read_with_finder_map"), "finder_map"
        if r is None:
            r, which = M.get("read_with_tool_map"), "tool_map"
        edges = [float(v) for v in M.get("bin_edges_MeV") or []]
        if r is None:
            notes.append("the publish has no momentum histogram")
        elif len(edges) != len(live["p_edges"]) or any(abs(a - b) > 1e-9 for a, b in zip(edges, live["p_edges"])):
            notes.append("the published momentum bins are not the live hist_p's: not compared")
        else:
            e = np.asarray(r["per_beam_particle"], float)
            ee = np.asarray((r.get("by_kind") or {}).get("e-", [0.0] * e.size), float)
            # hist_p leaves out bunches where the gas Cherenkov fired (status "electron"): with it cabled, the
            # electrons it would mark are taken out of the expectation, so both count the same tracks
            corr = live["cherenkov"] and ee.any()
            ec = np.clip(e - cherenkov_eff * ee, 0, None) if corr else e
            lc = np.asarray(live["hist_p"], float)
            centres = [(a + b) / 2 for a, b in zip(edges, edges[1:])]
            en = ec / ec.sum() if ec.sum() > 0 else None
            ln = lc / lc.sum() if lc.sum() > 0 else None
            em_, esd_ = _shape_stats(en, centres) if en is not None else (None, None)
            lm_, lsd_ = _shape_stats(ln, centres) if ln is not None else (None, None)
            mom = dict(edges=edges, read_with=which, expected=None if en is None else en.tolist(),
                       live=None if ln is None else ln.tolist(), live_tracks=int(lc.sum()),
                       mean=dict(live=lm_, expected=em_, diff=None if lm_ is None or em_ is None else lm_ - em_),
                       sd=dict(live=lsd_, expected=esd_, diff=None if lsd_ is None or esd_ is None else lsd_ - esd_),
                       published_mean_MeV=r.get("mean_MeV"), published_sd_MeV=r.get("sd_MeV"),
                       electron_correction=(
                           f"electrons taken out of the expectation (x {cherenkov_eff:g}: by_kind['e-'], their bunches "
                           "are status electron, not in hist_p): " + f"{100 * float(ee.sum() / e.sum()):.1f} % of it"
                           if corr and e.sum() > 0 else
                           "none: the gas Cherenkov is not cabled in the live finder, so electrons stay in hist_p"
                           if not live["cherenkov"] else "none: the publish expects no accepted electrons"))
            if which == "tool_map":
                notes.append("the momentum was read with the tool's own map, not the live finder's")
    return dict(mismatch=mismatch, notes=notes, planes=planes, momentum=mom)


def save_window(path, ns):
    """Write window_ns into the config file, keeping the rest of it. Returns what happened, for the page."""
    if not path:
        return "not saved (no config file)"
    try:
        cfg = {}
        if os.path.exists(path):
            with open(path) as f:
                cfg = json.load(f)
            if not isinstance(cfg, dict):
                return f"not saved ({os.path.basename(path)} is not a JSON object)"
        cfg["window_ns"] = ns
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
        return f"saved in {os.path.basename(path)}"
    except (OSError, ValueError) as ex:
        return f"not saved ({ex})"


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
        self.record = setup_files_record(cfg)  # path and sha256 of each file used (map: also the default one)
        self.config_path = a.config
        self.config_content, self.config_sanitized = read_config_content(a.config)
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
        self.track_geometry = finder_geometry(self.setting, self.tables)   # Beam vs map: where tracks are drawn
        rows, cab_problems = ff.check_cabling(rows, serials, nch, plane_channels)
        problems += cab_problems
        self.chans = ff.Channels(serials, rows, gains, offs)
        if self.chans.uncalibrated:
            problems.append("no gain for " + ", ".join(f"{s} ch{c}" for s, c in self.chans.uncalibrated[:12])
                            + (f" and {len(self.chans.uncalibrated) - 12} more" if len(self.chans.uncalibrated) > 12 else "")
                            + ": their hits are left out of the finder")
        self.map_path = cfg["map"] or os.path.join(ff.FINDER_DIR, "momentum_map.csv")
        self.map_sha256_lf = map_identity(self.map_path)   # the map loaded, as the floor tool identifies it (Beam vs map)
        self.map_field_T, self.map_meta = map_field(self.map_path)
        self.calibrated = gains is not None
        self.has_offsets = offs is not None
        self.max_offset_ps = int(np.abs(self.chans.toff_ps).max(initial=0))
        self.warnings, self.problems = warnings, problems


# ---------------------------------------------------------------- work done in the worker processes
_G: dict = {}


def _init_worker(tables, chans):
    try:
        os.nice(10)                           # below the DAQ, which shares the machine
    except OSError:
        pass
    signal.signal(signal.SIGINT, signal.SIG_IGN)   # the main process decides when to stop
    _G.update(tables=tables, chans=chans)


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
    tables, chans, window_ps = _G["tables"], _G["chans"], task["window"]
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
        tc = ts - _toff_of(slot)
        # on the finder's clock (offsets removed): with the boards' raw times, the -217 to -239 ns offsets of
        # 1399, 1443 and 30505 put their peak outside the +-200 ns of "time to the reference" (1 Nov 2026)
        daq = dh.process_ordered(tc, slot, np.argsort(tc, kind="stable"), t0, t1, task["ref"], chans.n_slots)
        owned = int(np.count_nonzero((tc >= t0) & (tc < t1)))   # hits whose own time (offset removed) is in the slice
    else:
        res = ff.Result(chans.n_slots, tables.map_n)
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
        self.waiting = 0                      # files not indexed yet because an earlier one is not indexed to its end

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
        """Index what was written since the last call (at most max_words words per file), file by file: a
        file only once the one before it is indexed to its end. (Indexing the next file early moved the
        couples' newest time past the unread rest of the earlier one: the finder passed it, its hits came
        late, and the earlier file was released before the rest was read, 1 Nov 2026.)"""
        live = self._live()
        self.waiting = 0
        for i, p in enumerate(live):
            fs = self.scans[p]
            n0 = fs.hits
            fs.update(now, max_words)
            self.hits += fs.hits - n0
            if fs.behind:
                self.waiting = len(live) - 1 - i
                break

    def behind(self):
        """More is written than indexed (the indexing reads a limited amount per step)."""
        return self.waiting > 0 or any(self.scans[p].behind for p in self._live())

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
        return all((self.scans[p].ino is not None or not marks.get(p, 0))       # a file not looked at yet is not ready
                   and (self.scans[p].pos >= marks.get(p, 0) or not self.scans[p].behind) for p in self._live())

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
            if fs.ino is None or fs.behind:     # not indexed to its end: what is not read yet is still to come
                continue
            A = fs.arrays()
            if self.done[p] == A["nev"].size and (A["nev"].size == 0 or int(A["t_last"].max()) < before):
                fs.release()
                self.released.add(p)

    def gaps(self):
        """Board aggregates missing from the files: counter gaps inside a file and between files."""
        f = [self.scans[p] for p in list(self.order)]
        n = sum(s.gaps for s in f)
        for x, y in zip(f, f[1:]):
            if x.last_ctr >= 0 and y.first_ctr >= 0 and not x.behind:   # x's last counter: only once x is indexed to its end
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
    # whether FSUDAQ runs (True, False, None: cannot tell); tests put their own here, never the host's
    fsudaq_probe = staticmethod(lambda: fsudaq_running())
    def __init__(self, a, clock=time.time):
        self.a, self.clock = a, clock
        self.serials = [int(x) for x in a.boards.split(",") if x.strip()]
        eight = {int(x) for x in a.eight_channel_boards.split(",") if x.strip()}
        self.nch = {s: (8 if s in eight else 16) for s in self.serials}
        self.window_ps, self.window_src = initial_window(a)
        if getattr(a, "window_src", None) and a.window is not None:
            self.window_src = a.window_src
        self.window_note = ""                   # the page's last change of the window, and whether it was saved
        self.lock = threading.RLock()           # the loop and the web server's threads
        self.pool = None
        self.setup = None
        self.ref_slot = a.ref_slot
        self.follow = a.follow                  # run folder FSUDAQ named (--follow or GET /run); None = newest in the data path
        self.inspect = None                     # a recorded run loaded from the page (GET /load); None = live
        self.restart = False                    # set by GET /run (a new run starts, even in the same folder) and GET /load
        self.by_run = False                     # the restart is GET /run's: the run is being taken, not finished
        self.stop_notice = False                # FSUDAQ stopped this run and closed its files
        self.stopped_folder = None              # the folder GET /stopped named (it may come before we follow it)
        self.wake = threading.Event()
        self.step_error = None
        self.folder = None
        self.reading = 0                        # counts the readings (reset): a summary is for the reading it was made in
        self.expected = BeamExpected(a.beam_expected)   # the floor tool's published expectation (Beam vs map)
        self.run_open = None                    # folder of the run FSUDAQ started (GET /run) and has not said stopped
        self.run_open_wall = 0.0
        self.interrupted = None                 # summary of a run followed live, left before its end (written by step)
        self.carry = None                       # a live reading left without a summary: its problems, for the next reading of it
        self.write_async = False                # summaries written by a thread (main() sets it; tests write at once)
        self.writes = None                      # its queue
        self.summary_queued = None              # (end, run file, reading) queued and not yet written
        self.bf_reap = []                       # children killed, to be cleaned up once they are gone
        self.bf_notes = {}                      # folder -> why its incomplete summary is not read again (the run list)
        self.open_cache = (0.0, None)           # (when, result) of the open-run look on disk
        # backfill: summaries of older runs, made by a child process between runs (see backfill_tick)
        self.bf_lock = threading.Lock()         # starting and stopping the child: never one while the other is half done
        self.bf = None                          # the child at work: dict(proc, folder, started, mtime, err)
        self.bf_on = bool(a.data_path and not a.run and not a.replay and not a.no_backfill and not a.no_summaries)
        self.bf_skipped = {}                    # folder -> (its files' fingerprint, why it got no summary): not tried again
        self.bf_done = []                       # (folder, when) of the summaries made, newest last (the page's note)
        self.bf_idle_since = None
        self.bf_busy = "not looked yet"         # why no backfill now (the page shows it)
        self.bf_looked = 0.0
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
                                               initargs=(self.setup.tables, self.setup.chans))
        else:
            _G.update(tables=self.setup.tables, chans=self.setup.chans)

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
        self.acc = ff.Result(S.chans.n_slots, S.tables.map_n)   # the finder's histograms and counters
        self.trip_skipped = 0                   # slices whose track counts did not fit the map (Beam vs map)
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
        self.finished_at_start = False          # the folder was already finished when it was first read
        self.finished_guess = False             # ... judged so by its files' age only (no stop in its run file)
        self.quiet_s = 0.0                      # how long no couple has written
        self.behind = False                     # more is written than indexed
        self.untaken_t = None                   # oldest time seen on disk and not yet given to the DAQ part
        self.late_log = collections.deque()     # (wall time, late hits) of each result, for the recent count
        self.flush_wall = None                  # when the finder last went on without couples silent in a pause
        self.rr = 0                             # the board the DAQ part takes from first (round robin)
        self.first_files = {}
        self.others = set()                     # serials with files in the folder that are not analysed
        # for the run summary: what the live view keeps only for a while (rates: HISTORY_S) or since Clear
        self.reading += 1
        self.reading_wall = time.time()
        self.all_done = False                   # everything on disk processed (set by _advance)
        self.run_counts = np.zeros(S.chans.n_slots, np.int64)   # hits per slot, the whole reading (Clear does not reset it)
        self.run_daq_bins = {}                  # RUN_BIN_S bin of data time -> hits per board
        self.run_fin_bins = {}                  # RUN_BIN_S bin of a slice's start -> [seconds covered, bunches, tracks, statuses...]
        self.bins_dropped = 0                   # results whose bin was not kept (RUN_BINS_MAX)
        self.agg_events = []                    # Clear, bunch window and reference changes with data already read
        self.problems = {}                      # what the page warned of during the reading, see _note_problems
        self.watch_gaps = []                    # times this run was not watched (the page showed another run meanwhile)
        self.problems_over = 0
        self.nstep = 0
        self.loop_errors = 0; self.loop_error_last = None
        self.summary_kind = None                # how the run's end was known when its summary was written (None: not yet)
        self.summary_rf = None                  # the run file (mtime, size) then: a stop recorded later writes it again
        self.summary_error = None; self.summary_retry = 0.0
        self.summary_written = None             # (path, when) of this reading's summary
        self.summary_last = None                # the document written then (a later run-file change renews only its "run")
        self.summary_kept = False               # the summary there was kept (it covered more): the backfill child's exit code
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
        """GET /run: FSUDAQ starts a saving run in this folder (also the same folder again). A run
        loaded for a look gives way: the data being taken come first."""
        with self.lock:
            self.follow = folder
            self.inspect = None
            self.stopped_folder = None
            self.restart = self.by_run = True
            self.run_open = os.path.abspath(folder)
            self.run_open_wall = time.time()
        self.backfill_stop("a run started")     # here, not at the next step: the run comes first at once
        self.wake.set()

    def run_stopped(self, folder):
        """GET /stopped: FSUDAQ stopped the run in this folder and closed its files."""
        self.stopped_folder = os.path.abspath(folder)
        with self.lock:
            if self.run_open == self.stopped_folder:
                self.run_open = None
        self.wake.set()

    def load(self, folder: str) -> str:
        """GET /load: read a recorded run folder whole, for a look. Returns why not, if not."""
        folder = os.path.abspath(os.path.expanduser(folder or ""))
        if not os.path.isdir(folder):
            return f"{folder} is not a folder"
        if not glob.glob(os.path.join(folder, "*.fsu")):
            return f"no .fsu files in {folder}"
        with self.lock:
            self.inspect = folder
            self.restart = True                 # read from its start, also when it is the run shown now
        self.wake.set()
        return ""

    def live(self):
        """GET /live: back to the run FSUDAQ named, or the newest in the data path."""
        with self.lock:
            if self.inspect is not None:
                self.inspect = None
        self.wake.set()

    def run_roots(self) -> set:
        """Where the recorded runs are: the data path, and beside the run FSUDAQ named."""
        follow = self.follow
        return {os.path.abspath(r) for r in (self.a.data_path, follow and os.path.dirname(os.path.abspath(follow))) if r}

    def resolve_run(self, name):
        """The run folder a bare folder name means, directly under one of run_roots(); (folder, None) or
        (None, (HTTP code, why)). Anything that is not a plain name (a path, "..", a link out of the root)
        is refused: anyone may ask, and the answer must never be a file elsewhere."""
        if (not isinstance(name, str) or not name or len(name) > 255 or name in (".", "..") or name.startswith(".")
                or any(c in name for c in "/\\\x00") or os.path.isabs(name)):
            return None, (400, "not a run folder name")
        for root in sorted(self.run_roots()):
            d = os.path.join(root, name)
            try:
                real_root = os.path.realpath(root)
                if os.path.dirname(os.path.realpath(d)) != real_root or not os.path.isdir(d):
                    continue                    # absent, or a link leading out of the root
            except (OSError, ValueError):
                continue
            if glob.glob(os.path.join(glob.escape(d), "*.fsu")):
                return d, None
        return None, (404, f"no run folder {name}")

    def summary_reply(self, name):
        """GET /summary?run=<folder name>: the run's summary as written (or none), with its run file as it is
        now, and whether its data files changed since (stale). (HTTP code, JSON)."""
        folder, why = self.resolve_run(name)
        if folder is None:
            return why[0], dict(run=None, summary=None, note=why[1])   # (what was asked is not echoed)
        fp = run_fingerprint(folder)
        s, err = load_summary(folder)
        out = dict(run=os.path.basename(folder), folder=folder, files=fp["files"], bytes=fp["bytes"],
                   run_file=read_run_file(folder, cache=False), summary=s, stale=False, note="")
        if s is None:
            out["note"] = err or "no summary recorded"
            bf, skipped = self.bf, self.bf_skipped.get(folder)      # (no lock: as they are now)
            if bf and bf["folder"] == folder:
                out["note"] += " (being made now)"
            elif skipped:
                out["note"] += f" (none can be made: {skipped[1]})"
        elif s.get("fingerprint") != fp:
            out["stale"] = True
            then = s.get("fingerprint") or {}
            out["note"] = (f"the run's data files changed after this summary was written ({then.get('files')} files, "
                           f"{then.get('bytes')} bytes then; {fp['files']} files, {fp['bytes']} bytes now): it does not describe them")
        return 200, out

    def runs(self) -> list:
        """GET /runs: the run folders in the data path (and beside the run FSUDAQ named), newest first."""
        roots = self.run_roots()
        out = []
        for root in roots:
            for d in glob.glob(os.path.join(root, "*")):
                files = glob.glob(os.path.join(d, "*.fsu")) if os.path.isdir(d) else []
                if not files:
                    continue
                try:
                    st = [os.stat(f) for f in files]
                except OSError:
                    continue
                rf = read_run_file(d, cache=False) or {}
                nbytes = sum(x.st_size for x in st)
                out.append(dict(folder=d, name=os.path.basename(d), files=len(files), bytes=nbytes,
                                written=max(x.st_mtime for x in st), start=rf.get("start"),
                                comment=((rf.get("stop") or {}).get("comment") or rf.get("start_comment")),
                                stopped=bool(rf.get("stop")),
                                summary=summary_meta(d, dict(files=len(files), bytes=nbytes))))
        out = sorted(out, key=lambda r: -r["written"])[:300]
        bf, skipped = self.bf, dict(self.bf_skipped)                # (no lock: as they are now)
        for r in out:
            if bf and bf["folder"] == r["folder"]:
                r["backfill"] = "being made now"
            elif r["folder"] in skipped:
                r["backfill"] = "cannot be made: " + skipped[r["folder"]][1]
            elif r["folder"] in self.bf_notes:
                r["backfill"] = self.bf_notes[r["folder"]]
        return out

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
                self._loop_failed(ex)
                raise
        doc, self.interrupted = self.interrupted, None
        if doc is not None:
            self._queue_write(doc)              # the run left before its end (see _begin), outside the lock
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
                self._loop_failed(ex)
            raise
        doc = None
        with self.lock:
            if self.folder != folder or list(self.boards.values()) != boards:
                return                          # a new run began meanwhile
            try:
                self._advance(stopped)
                self.step_error = None
            except Exception as ex:             # noqa: BLE001
                self._loop_failed(ex)
                raise
            # the summary's bookkeeping must never stop the analysis: its failures are only reported
            try:
                self._note_problems()
                doc = self._summary_due()
            except Exception as ex:             # noqa: BLE001
                self.summary_error = f"not made: {ex!r}"; self.summary_retry = self.clock() + SUMMARY_RETRY_S
                say(f"run summary not made: {ex!r}")
        if doc is not None:
            self.summary_queued = doc[1:]
            self._queue_write(doc)              # outside the lock: the page is served meanwhile

    def _queue_write(self, doc):
        """A summary to write: at once (tests, the backfill child), or (main(): write_async) by one writer
        thread, in the order queued, so a run start never waits for a disk."""
        if not self.write_async:
            return self._write_summary(*doc)
        if self.writes is None:
            self.writes = queue.Queue()

            def writer():
                while True:
                    d = self.writes.get()
                    try:
                        self._write_summary(*d)
                    except Exception as ex:     # noqa: BLE001  (_write_summary reports its own failures)
                        say(f"summary writer: {ex!r}")
                    finally:
                        self.writes.task_done()
            threading.Thread(target=writer, daemon=True, name="summary-writer").start()
        self.writes.put(doc)

    def flush_writes(self, timeout=10.0):
        """Wait (at most timeout s) until the queued summaries are written."""
        t_end = time.time() + timeout
        while self.writes is not None and self.writes.unfinished_tasks and time.time() < t_end:
            time.sleep(0.02)

    def _loop_failed(self, ex):
        self.step_error = repr(ex)
        self.loop_errors += 1; self.loop_error_last = repr(ex)

    def _begin(self):
        a = self.a
        folder = a.run or self.inspect or self.follow or newest_run_folder(a.data_path)
        if folder != self.folder or self.restart or (folder and self._run_replaced(folder)):
            # a run that starts while we watch is read from its start; the one running when the
            # dashboard started only from then on (unless --from-start); a run already finished then
            # is read whole
            from_start = bool(a.replay or a.from_start or self.restart or self.folder is not None or folder == self.inspect)
            # finished: its run file has the stop, or no file of it was written for FINISHED_QUIET_S. Not a run
            # FSUDAQ just started, and not merely quiet for a while: a beam pause is not the end of the run
            # (a 5 s quiet once made a run being taken "finished" for good; and "back to live" showed a run
            # finished long ago as running, 1 Nov 2026)
            finished = guess = False
            if folder is not None and not self.by_run:
                rf = read_run_file(folder) or {}
                finished = bool(rf.get("stop"))
                if not finished and self.run_open != os.path.abspath(folder):   # (never while /run without /stopped)
                    finished = guess = folder_quiet(folder, FINISHED_QUIET_S)
            carry = None
            if self.folder is not None and (folder != self.folder or self.restart):
                old = os.path.abspath(self.folder)
                live_left = self._source() == "live" and self.summary_kind is None
                if live_left and not self.by_run:   # what it saw goes with the run (not to a run recorded again by /run)
                    self.carry = dict(folder=old, problems=self.problems, over=self.problems_over, left=time.time(),
                                      gaps=list(self.watch_gaps))
                # the next run started (FSUDAQ's /run elsewhere, or a newer folder): the run left is over, and its
                # summary is written with what was processed, said incomplete. Never for a look at another run
                # (/load: the live run goes on, and is read again from its start on return) nor for a run still open
                if folder != self.folder and live_left and not self.inspect and self.run_open != old:
                    try:
                        self.interrupted = self._interrupted_doc()
                    except Exception as ex:     # noqa: BLE001  never in the way of the next run
                        say(f"run summary of {self.folder} not made: {ex!r}")
            if self.carry and folder is not None and self.carry["folder"] == os.path.abspath(folder):
                carry, self.carry = self.carry, None
            self.restart = self.by_run = False
            self.reset(folder, from_start or finished)
            if carry:                           # back on a run read live before: its problems so far, and when it was not watched
                self.problems, self.problems_over = carry["problems"], carry["over"]
                self.watch_gaps = list(carry["gaps"])
                if time.time() - carry["left"] > 2:
                    self.watch_gaps.append({"from": iso_time(carry["left"]), "to": iso_time(time.time())})
            if finished:
                self.finished_at_start = self.stop_notice = True
                self.finished_guess = guess
        if folder is not None and self.stopped_folder == os.path.abspath(folder):
            self.stop_notice = True
            self.finished_guess = False
        elif self.finished_guess and folder is not None and not folder_quiet(folder, a.stall_s):
            # judged finished by its files' age, and now written again: it is being taken after all
            self.stop_notice = self.finished_at_start = self.finished_guess = False
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

    def _hold_target(self, holding, boards, stopped, margin):
        """Where the finder may cut to while data are written or a board's files are not all indexed: the
        newest time on disk of every couple holding it, less the margin. A board not indexed to the end
        holds it at its indexed part less --hold-s (its couples not seen yet may have hits that far back
        in the part not read yet); in a run already over (all on disk) the finder waits for the index."""
        ts = [int(b.newest[c]) for b, c in holding]
        for b in boards:
            if b.behind():
                if stopped or b.newest.max() < 0:
                    return self.W
                ts.append(int(b.newest.max()) - int(self.a.hold_s * PS))
        return min(ts) - margin if ts else self.W

    def _advance(self, stopped):
        a = self.a
        now = self.clock()
        S = self.setup
        boards = list(self.boards.values())
        self.all_done = False
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
            if self.behind and stopped:         # a run already over: its first time from the whole index (a couple's
                self.status = "falling behind: reading the files"   # first hits may lie past the part read so far)
                self.mode = "running"
                return
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
            target = self._hold_target(holding, boards, stopped, margin)
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
            self._submit("slice", dict(t0=int(self.W), t1=int(t1), margin=int(margin), files=files, ref=self.ref_slot,
                                       window=self.window_ps))
            self.W = t1; n_slices += 1
        all_done = self.W >= target and not self.pending and self.untaken_t is None and not self.behind
        self.all_done = all_done
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
            what = ("recorded run, loaded for a look" if self.inspect and self.folder == self.inspect else
                    "last run (already finished when it was read)" if self.finished_at_start else "run stopped")
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
                # the whole reading, for the summary (as secs: every epoch, Clear does not reset it)
                np.add.at(self.run_counts, slots, cnt)
                bd = slots // self.setup.chans.board_channels
                bins = secs // RUN_BIN_S
                for k in np.unique(bins):
                    row = self.run_daq_bins.get(int(k))
                    if row is None:
                        if len(self.run_daq_bins) >= RUN_BINS_MAX:
                            self.bins_dropped += 1
                            continue
                        row = self.run_daq_bins[int(k)] = np.zeros(len(self.serials), np.int64)
                    m = bins == k
                    np.add.at(row, bd[m], cnt[m])
                if epoch == self.epoch:
                    self.daq.add(r["daq"])
                    n_late = int(np.asarray(r["daq"]["late"]).sum())
                    if n_late:
                        self.late_log.append((self.clock(), n_late))
            else:
                res = r["res"]
                if epoch == self.epoch:
                    for k in ff.Result.ARRAYS:
                        a = getattr(self.acc, k)
                        if k == "h_trip" and a.shape != np.shape(res[k]):
                            self.trip_skipped += 1   # counted with other tables (another map): not added, not a crash
                            continue
                        a[...] += res[k]
                    for k in ff.Result.SCALARS:
                        setattr(self.acc, k, getattr(self.acc, k) + res[k])
                    d = r["daq"]
                    if r["ref"] != self.ref_slot:   # made before the reference channel changed: keep all but dt
                        d = dict(d); d["dt"] = np.zeros_like(d["dt"])
                    self.daq.add(d)
                    self.owned += r["owned"]
                n_trk = int((res["h_ntrk"] * np.arange(ff.NTRK)).sum())
                status = {ff.STATUS[i]: int(c) for i, c in enumerate(res["h_status"]) if c}
                k = int(r["t0"] // (RUN_BIN_S * PS))      # by the slice's start: a slice (<= --max-slice-s) is not split
                row = self.run_fin_bins.get(k)
                if row is None and len(self.run_fin_bins) < RUN_BINS_MAX:
                    row = self.run_fin_bins[k] = np.zeros(3 + len(ff.STATUS), np.float64)
                if row is None:
                    self.bins_dropped += 1
                else:
                    row[0] += (r["t1"] - r["t0"]) / PS; row[1] += int(res["n_bunches"]); row[2] += n_trk
                    row[3:] += np.asarray(res["h_status"])
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

    def beam_vs_map(self):
        """GET /expected: what the floor tool published (its head) and how the finder's occupancy and momentum
        now compare with it (compare_beam). The live numbers count since the reading began, or the last Clear."""
        doc, err = self.expected.get()
        with self.lock:
            S, ch = self.setup, self.setup.chans
            occ, _ = self._occupancy()
            live = dict(setting=S.setting, setting_given=S.src.get("setting") is not None, map=S.map_path,
                        map_sha256_lf=S.map_sha256_lf, map_field_T=S.map_field_T, map_meta=S.map_meta,
                        placeholder=S.cabling.startswith("placeholder"),
                        cherenkov=bool(ch.installed[ff.COUNTERS.index("cherenkov")]),
                        occupancy=occ, hist_p=self.acc.h_p.tolist(), p_edges=ff.P_EDGES.tolist())
            cleared = [e for e in self.agg_events if e["what"] in ("clear", "window")]
            context = dict(folder=self.folder, inspect=self.inspect if self.inspect == self.folder else None, mode=self.mode,
                           counted="since the reading began" if not cleared else f"since the last {cleared[-1]['what']} change",
                           bunches=int(self.acc.n_bunches))
            # where the accepted tracks went: count per map triplet, most frequent first, drawn with the finder's own
            # channel positions (the publish's top view gives the z frame: its pipe exit)
            G, trip = S.track_geometry, self.acc.h_trip
            tracks = dict(planes=G["planes"], error=G["error"], rows=None, total=int(trip.sum()), shown=0, distinct=0)
            if G["codes"] is not None and trip.size == len(G["codes"]):
                nz = np.flatnonzero(trip)
                nz = nz[np.argsort(-trip[nz], kind="stable")][:TRACKS_MAX]
                tracks.update(rows=[[*G["codes"][j], int(trip[j]), round(float(S.tables.map_p[j]), 1)] for j in nz],
                              shown=int(trip[nz].sum()), distinct=int(np.count_nonzero(trip)))
            elif G["codes"] is not None:
                tracks["error"] = "the track counts do not match the map loaded (not shown)"
            if getattr(self, "trip_skipped", 0):
                tracks["error"] = (tracks["error"] + "; " if tracks["error"] else "") + \
                    f"{self.trip_skipped} slice(s) counted with another map: their tracks are not in the picture"
        out = dict(path=self.expected.path, error=err, published=None, compare=None, context=context, tracks=tracks,
                   live={k: v for k, v in live.items() if k not in ("occupancy", "hist_p", "p_edges")})
        if doc is not None:
            M = doc.get("momentum") or {}
            out["published"] = dict(
                published_at=doc.get("published_at"), tool_commit=doc.get("tool_commit"), settings=doc.get("settings"),
                map={k: v for k, v in (doc.get("map") or {}).items()}, warnings=doc.get("warnings") or [],
                has_view=isinstance(doc.get("top_view"), dict),
                meanings=dict(planes=(doc.get("planes") or {}).get("meaning"), momentum=M.get("meaning"), events=M.get("events")))
            try:
                out["compare"] = compare_beam(doc, live)
            except (KeyError, TypeError, ValueError, IndexError) as ex:
                out["error"] = f"the published file cannot be compared: {ex!r}"
        return out

    def expected_view(self):
        """GET /expected_view: the publish's top view (outlines, sample tracks), apart from /expected: it is large
        and changes only with a new publish (the page asks again when published_at changes)."""
        doc, err = self.expected.get()
        tv = (doc or {}).get("top_view")
        if not isinstance(tv, dict):
            return 404, dict(note=err or "the publish has no top view (written by an older floor tool: Publish again)")
        return 200, dict(published_at=doc.get("published_at"), top_view=tv)

    def _board_rows(self, now):
        """Per board: files, what is read, late and early hits, and the live checks (state()["boards"])."""
        ch = self.setup.chans
        boards = []
        late, early = self.daq.late, self.daq.early
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
                               quiet_alarm=bool(running and not self.behind and quiet is not None and quiet >= self.a.stall_s),
                               slow_couples=[int(c) for c in range(8) if running and not self.behind and seen[c] and now - b.newest_wall[c] >= self.a.stall_s
                                             and now - b.newest_wall.max() < self.a.stall_s]))
        return boards

    def _occupancy(self):
        """The finder's hits per amplifier channel, by plane (index 0 unused), and per counter."""
        ch, A = self.setup.chans, self.acc
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
        return occ, counters

    def _health(self, now, boards):
        """What the page warns of at the top (state()["health"]), from the board rows of _board_rows()."""
        running = self.mode == "running"
        pending = list(self.pending.values())
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
            if self.t_first is not None and not b["has_data"] and not self.behind and (running or self.mode == "paused"):
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
        if self.summary_error:
            health.append(f"run summary of {os.path.basename(self.folder or '')}: {self.summary_error}")
        return health

    def state(self) -> dict:
        with self.lock:
            now = self.clock()
            S, A, ch = self.setup, self.acc, self.setup.chans
            boards = self._board_rows(now)
            occ, counters = self._occupancy()
            tb = S.tables
            done = self._finder_done()
            tops = [int(b.newest.max()) for b in self.boards.values() if b.newest.max() >= 0]
            newest = max(tops) if tops and self.t_first is not None else None
            ages = [now - t_sub for _, _, t_sub, *_ in self.pending.values()]
            health = self._health(now, boards)
            late, early = self.daq.late, self.daq.early
            while self.late_log and now - self.late_log[0][0] > LATE_RECENT_S:
                self.late_log.popleft()
            n_late = int(late.sum())
            return dict(
                # inspect: only once the loaded run is the one read (until the next step it is still the one before)
                server_time=now, folder=self.folder, inspect=self.inspect if self.inspect == self.folder else None, status=self.status, mode=self.mode, kernels="numba" if FAST else "numpy",
                workers=self.a.workers, in_flight=len(self.pending), oldest_task_s=max(ages) if ages else None,
                last_result_s=None if self.last_fold_wall is None else now - self.last_fold_wall,
                replay=bool(self.a.replay), window_ns=self.window_ps / 1000, window_src=self.window_src,
                window_note=self.window_note, window_range=WINDOW_RANGE_NS, hold_s=self.a.hold_s, stall_s=self.a.stall_s,
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
                # FSUDAQ's run file; t0_s: the board-clock second the rate plots count from (their 0 s), so a
                # run-clock time (LED run_ms) is at run_ms / 1000 - t0_s on them
                run_file=read_run_file(self.folder),
                t0_s=None if self.t_first is None else int(self.t_first // PS),
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
                # this reading's summary file, and the backfill of older runs (Data and processing card)
                summary=dict(file=self.summary_written and os.path.basename(self.summary_written[0]),
                             written=self.summary_written and self.summary_written[1], end=self.summary_kind,
                             error=self.summary_error, enabled=not self.a.no_summaries, off=self.a.summaries_off),
                backfill=self.backfill_state(),
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
            seconds=[dict(t=s - t0s, boards=[None if s >= self._known_until(bi, slow) else int(self.secs[s][bi * nbc:(bi + 1) * nbc].sum())
                                             for bi in range(len(self.serials))])
                     for s in secs[-HISTORY_S:]])

    def _known_until(self, bi, slow, couple=None):
        """The first second (board clock) whose count of board bi (or of one couple of it) is not known yet:
        a couple that does not hold the finder back (slow, see _complete_seconds) but has written has its
        hits on disk only up to its newest time. (Its newest seconds were drawn as 0 Hz, 1 Nov 2026.) Not in
        a run that is over (all of it is on disk), and not for a couple silent for SLOW_KNOWN_S (taken to
        have nothing more: one that stopped for good blanked its board's rate for the rest of the run)."""
        if self.mode == "stopped":
            return 1 << 62
        b = self.boards[self.serials[bi]]
        now = self.clock()
        cs = [c for c in range(8) if (bi, c) in slow and b.newest[c] >= 0 and (couple is None or c == couple)
              and now - b.newest_wall[c] < SLOW_KNOWN_S]
        return min((int(b.newest[c]) // PS for c in cs), default=1 << 62)

    def channel(self, sl, view=None, light=False):
        """One channel's histograms for the DAQ tab (sl = -1: all channels together, spectrum only).
        view = (lo, hi, nb, log): the Qlong range [lo, hi) and binning the page shows.
        light: without the PSD map and the rate spectrum (the side-by-side comparison does not show them)."""
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
            nbc = self.setup.chans.board_channels
            secs, slow = self._complete_seconds()
            known = self._known_until(sl // nbc, slow, (sl % nbc) // 2)
            if self.boards[self.serials[sl // nbc]].newest[(sl % nbc) // 2] < 0 and self.mode != "stopped":
                known = -(1 << 62)              # its pair has written nothing yet: a first buffer may be filling, no rate known
            rates = [[s - t0s, int(self.secs[s][sl])] for s in secs if s < known]
            lo, hi, nb, log = view or (0, 65536, 4096, False)
            e = dh.q_edges(lo, hi, nb, log)
            row = D.q[sl]
            tot = int(row.sum())
            inside = dh.rebin(row, np.array([0, 65536]))[0]          # all but Q = 0 and 65535
            shown = dh.rebin(row, e)
            out = dict(slot=sl, **self.labels[sl], count=int(D.counts[sl]), late=int(D.late[sl]), early=int(D.early[sl]), ref=self.ref_slot,
                       q=dict(edges=e.tolist(), counts=shown.tolist(), total=tot, zero=int(row[0]), sat=int(row[65535]),
                              outside=int(inside - shown.sum()), log=bool(log)),
                       iat=dict(edges=dh.IAT_EDGES.tolist(), counts=D.iat[sl].tolist()),
                       dt=dict(edges=dh.DT_EDGES.tolist(), counts=D.dt[sl].tolist()),
                       windows=D.n_seg, rate=rates)
            if light:
                return out
            # PSD map: the Qlong bins that overlap the shown range
            i0 = max(0, int(np.searchsorted(dh.PX_EDGES, e[0], side="right")) - 1)
            i1 = min(dh.NPX, int(np.searchsorted(dh.PX_EDGES, e[-1], side="left")))
            P = D.psd[sl, i0:i1]
            out["psd"] = dict(xedges=dh.PX_EDGES[i0:i1 + 1].tolist(), ny=dh.NPY, counts=P.tolist(),
                              n=int(D.psd[sl].sum()), out=int(D.psd_out[sl]))
            out["spectrum"] = dict(f=f.tolist(), p=(D.spec_p[sl] / n if n else D.spec_p[sl]).tolist())
            return out

    COMPARE_MAX = 8   # channels side by side on the page

    def compare(self, slots, view=None):
        """Several channels' histograms for the side-by-side comparison: pulse height, time since the previous
        hit, time to the reference, rate. Channels not in the setup are left out."""
        out = []
        for sl in slots[:self.COMPARE_MAX]:
            d = self.channel(sl, view, light=True) if sl >= 0 else None
            if d is not None:
                out.append(d)
        return dict(ref=self.ref_slot, channels=out)

    def grid(self, view):
        """Every channel's Qlong spectrum over the shown range, coarsely binned, for the overview grid."""
        lo, hi, nb, log = view
        e = dh.q_edges(lo, hi, nb, log)
        with self.lock:
            C = dh.rebin(self.daq.q, e)[self.slots]                  # no copy of the full array
            return dict(edges=e.tolist(), log=bool(log),
                        slots=[dict(slot=sl, **self.labels[sl], counts=C[i].tolist()) for i, sl in enumerate(self.slots)])

    def _agg_event(self, what, detail):
        """A change of what the aggregates mean, once data of this reading were read or are being read
        (the summary says so: its numbers do not cover the whole run alike)."""
        if self.t_first is None and not self.pending and not self.run_counts.any():
            return
        if len(self.agg_events) < 100:
            self.agg_events.append(dict(what=what, at=iso_time(time.time()), detail=detail,
                                        data_s=None if self.W is None or self.t_first is None else (self.W - self.t_first) / PS))

    def clear(self, why="Clear"):
        """The page's Clear: accumulated histograms start again; tasks already out are not added."""
        with self.lock:
            if why == "Clear":
                self._agg_event("clear", "the page's Clear: the finder's results and the late and early counts start again")
            self.acc = ff.Result(self.setup.chans.n_slots, self.setup.tables.map_n)
            self.trip_skipped = 0
            self.daq = dh.DaqResult(self.setup.chans.n_slots)
            self.owned = 0
            self.late_log.clear()
            self.epoch += 1

    def set_window(self, ns):
        """The page's bunch window: used for the slices cut from now on; the accumulated counts start again
        (as Clear), so no histogram mixes two windows. Saved in the config file for the next start."""
        lo, hi = WINDOW_RANGE_NS
        if not (lo <= ns <= hi):
            raise ValueError(f"the window must be {lo} to {hi} ns")
        with self.lock:
            old = self.window_ps / 1000
            if int(round(ns * 1000)) == self.window_ps:
                return f"already {old:g} ns: nothing changed"
            self.window_ps = int(round(ns * 1000))
            self._agg_event("window", f"bunch window changed from {old:g} to {self.window_ps / 1000:g} ns: the finder's "
                                      "results and the late and early counts start again")
            self.clear("window")
            saved = save_window(self.a.config, self.window_ps / 1000)
            self.window_src = "page"
            self.window_note = (f"changed from {old:g} to {self.window_ps / 1000:g} ns at {time.strftime('%H:%M:%S')}, counts "
                                f"restarted; {saved}")
            say(f"{time.strftime('%H:%M:%S')} bunch window {old:g} -> {self.window_ps / 1000:g} ns ({saved})")
            return self.window_note

    def set_ref(self, sl):
        with self.lock:
            new = sl if sl in self.labels else None
            if new != self.ref_slot:
                self._agg_event("reference", f"timing reference changed from slot {self.ref_slot} to {new} (time-to-reference "
                                             "histograms start again; they are not in the summary)")
            self.ref_slot = new
            self.daq.dt[...] = 0

    # ------------------------------------------------------------ the run summary
    def _note_problems(self):
        """Each step: what the page warns of now, kept per kind of warning (its numbers taken out, a
        board's name kept) with when it was first and last seen, in how many steps and in how many
        separate episodes. Bounded: PROBLEMS_MAX kinds, the rest only counted."""
        self.nstep += 1
        if self.folder is None:
            return
        now = self.clock()
        wall = None
        for text in self._health(now, self._board_rows(now)):
            wall = wall or iso_time(time.time())
            key = PROBLEM_NUMBERS.sub("#", text)
            p = self.problems.get(key)
            if p is None:
                if len(self.problems) >= PROBLEMS_MAX:
                    self.problems_over += 1
                    continue
                p = self.problems[key] = dict(text=text, first=wall, last=wall, steps=0, episodes=0, last_step=-2)
            if p["last_step"] != self.nstep - 1:
                p["episodes"] += 1
            p.update(text=text, last=wall, last_step=self.nstep)
            p["steps"] += 1

    def _finished_kind(self):
        """How the end of the run read is known, once everything on disk is processed: "stopped" (FSUDAQ's
        /stopped, or the stop in its run file), "guess" (no stop recorded and no file written for
        FINISHED_QUIET_S), "replay" (a replay has shown the whole run); None: not over, or not all processed."""
        if not self.all_done or self.folder is None:
            return None
        if self.a.replay and not all(b.all_seen() for b in self.boards.values()):
            return None                         # a replay is "stopped" early when the run file has the stop
        if self.mode == "stopped":
            if self.a.replay and not self.stop_notice:
                return "replay"
            return "guess" if self.finished_guess else "stopped"
        if self.mode == "quiet":                # nothing written for --hold-s, no word from FSUDAQ
            if run_file_has_stop(self.folder):
                return "stopped"
            if self.run_open != os.path.abspath(self.folder) and folder_quiet(self.folder, FINISHED_QUIET_S):
                return "guess"                  # (never while FSUDAQ said the run goes on)
        return None

    def _summary_due(self):
        """After a step, under the lock: (document, end, run file key, reading) when the reading came to an
        end its summary does not have yet, or the run file changed since (FSUDAQ records the stop and its
        comment after /stopped); else None. Data written after an end (the run went on) make it due again."""
        if self.a.no_summaries or self.folder is None:
            return None
        kind = self._finished_kind()
        if kind is None:
            if self.mode in ("running", "paused", "waiting"):
                self.summary_kind = self.summary_last = None   # more data: made again at the next end
            return None
        rf = run_file_key(self.folder)
        if kind == self.summary_kind and rf == self.summary_rf:
            return None
        if self.summary_queued == (kind, rf, self.reading):
            return None                         # queued, not written yet
        if self.summary_error and self.clock() < self.summary_retry:
            return None
        if kind == self.summary_kind and self.summary_last is not None:
            folder = os.path.abspath(self.folder)
            name = os.path.basename(os.path.normpath(folder))
            doc = dict(self.summary_last, written_at=iso_time(time.time()),
                       run=self._summary_run(kind, folder, name, read_run_file(folder, cache=False) or {}))
            doc["settings"] = dict(doc["settings"], **run_settings_files(folder, name))
            return doc, kind, rf, self.reading
        return self._summary_doc(kind), kind, rf, self.reading

    def _write_summary(self, doc, kind, rf, reading):
        """Write a document of _summary_due(); a failure is logged and shown on the page, and tried again
        after SUMMARY_RETRY_S, never raised."""
        folder = doc["run"]["folder"]
        try:
            path, written, note = write_summary_file(folder, doc)
        except Exception as ex:                 # noqa: BLE001  OSError (permissions, disk full), ValueError
            with self.lock:
                if self.reading == reading:
                    self.summary_error = f"not written: {ex}"; self.summary_retry = self.clock() + SUMMARY_RETRY_S
                else:                           # a run left before its end: said on the page, not tried again
                    self.summary_error = f"{os.path.basename(folder)}: not written: {ex}"; self.summary_retry = 0.0
                if self.summary_queued == (kind, rf, reading):
                    self.summary_queued = None
            say(f"{time.strftime('%H:%M:%S')} run summary of {folder} not written: {ex!r}")
            return
        with self.lock:
            if self.reading == reading:
                self.summary_kind, self.summary_rf, self.summary_error = kind, rf, None
                self.summary_written = (path, doc["written_at"] if written else note)
                self.summary_last = doc if written else self.summary_last
                self.summary_kept = not written
        with self.lock:
            if self.summary_queued == (kind, rf, reading):
                self.summary_queued = None
        say(f"{time.strftime('%H:%M:%S')} run summary {path}: " + (note or ("complete" if doc["complete"] else
            "incomplete (" + ", ".join(x["reason"] for x in doc["incomplete"]) + ")") + f", end: {kind}"))

    def _summary_doc(self, kind):
        """The run summary: what this reading computed, as it is now (nothing is read again). Under the lock.
        Sections and units: README "Run summary"."""
        S, A, D, ch = self.setup, self.acc, self.daq, self.setup.chans
        now = self.clock()
        folder = os.path.abspath(self.folder)
        name = os.path.basename(os.path.normpath(folder))
        nbc = ch.board_channels
        boards = self._board_rows(now)
        health = self._health(now, boards)
        occ, counters = self._occupancy()
        rf = read_run_file(folder, cache=False) or {}
        fp = run_fingerprint(folder)
        tops = [int(b.newest.max()) for b in self.boards.values() if b.newest.max() >= 0]
        first_ps = self.t_first
        last_ps = max(tops) if tops else None
        t0s = None if first_ps is None else int(first_ps // PS)
        span = None if first_ps is None or last_ps is None else (last_ps - first_ps) / PS
        src = self._source()
        cleared = [e for e in self.agg_events if e["what"] in ("clear", "window")]
        since = "the whole reading" if not cleared else f"since the last {cleared[-1]['what']} change ({cleared[-1]['at']})"
        n_late, n_early = int(D.late.sum()), int(D.early.sum())
        history, rate_span, outside, outliers = self._run_history(first_ps, last_ps, t0s)
        identity = setup_identity(S.setting, S.record, self.window_ps, self.serials, self.nch)
        setup = dict(identity=identity, config_file=S.config_path, config=S.config_content, files=S.record, code=CODE_HASHES,
                     setting=S.setting, setting_src=S.src.get("setting"), window_ns=self.window_ps / 1000,
                     window_src=self.window_src, reference_slot=self.ref_slot, boards=list(self.serials),
                     eight_channel_boards=[sn for sn in self.serials if self.nch[sn] == 8],
                     hold_s=self.a.hold_s, stall_s=self.a.stall_s)

        # complete: this reading counted every hit of the run's files (a careful re-read would find no more)
        incomplete, caveats = [], []
        if kind == "interrupted":
            done = self._finder_done()
            lag = None if done is None or last_ps is None else max(0.0, (last_ps - done) / PS)
            # bytes: of the data files on disk now (the fingerprint, also files not looked at yet) those not yet
            # scanned by the index; seconds: data time scanned but not yet through the finder
            nread = sum(fs.pos * 4 for b in self.boards.values() for fs in b.scans.values())
            unread = max(0, fp["bytes"] - nread)
            incomplete.append(dict(reason="next_run_started", unread_bytes=unread, bytes=fp["bytes"], finder_lag_s=lag, detail=(
                "the next run started before the analysis caught up: "
                + (f"{100 * unread / fp['bytes']:.1f} % of the data files' {fp['bytes']:,} bytes not yet scanned" if fp["bytes"] else "no bytes")
                + ("" if lag is None else f"; {lag:.0f} s of the data scanned not yet through the finder"))))
        if S.config_sanitized:
            caveats.append(dict(reason="config_not_json", detail="the experiment config has values JSON cannot hold "
                                "(NaN, Infinity): they are text in setup.config"))
        for g in self.watch_gaps:
            caveats.append(dict(reason="problems_unwatched", detail=(
                f"from {g['from']} to {g['to']} the dashboard showed another run (a look): problems of this run in that "
                "time were not watched; the counts are not affected (the run was read again from its start)")))
        if src != "live":
            caveats.append(dict(reason="setup_unknown", detail=(
                f"analysed with the setup of {iso_time(time.time())} (experiment config, bunch window, cabling, gains, "
                "offsets, map: see setup), not necessarily the one used when the run was taken")))
        if self.since is not None:
            incomplete.append(dict(reason="started_mid_run", detail=(
                f"the dashboard began reading this run while it was being taken ({iso_time(self.reading_wall)}): what was "
                "on disk then is not counted" + (f"; {n_early:,} hits from before then reached the disk later (counted "
                                                 "per channel, not in the finder)" if n_early else ""))))
        for e in cleared:
            incomplete.append(dict(reason="cleared" if e["what"] == "clear" else "window_changed",
                                   detail=e["detail"] + f" ({e['at']}" + ("" if e["data_s"] is None else
                                                                         f", at {e['data_s']:.1f} s of data") + ")"))
        if self.lost:
            incomplete.append(dict(reason="tasks_lost", detail=f"{self.lost} analysis task(s) failed twice: their data are missing"))
        if self.others:
            incomplete.append(dict(reason="boards_not_analysed", detail="files of boards not analysed (not in --boards): "
                                   + ", ".join(str(s) for s in sorted(self.others))))
        if self.loop_errors:
            incomplete.append(dict(reason="analysis_errors", detail=f"the analysis loop failed {self.loop_errors} time(s) during "
                                   f"the reading (last: {self.loop_error_last}): the results may miss data"))
        unread = []
        for b in self.boards.values():
            for p in b.order:
                fs = b.scans[p]
                if fs.tail_bytes() > 0 or not os.access(p, os.R_OK):
                    unread.append((os.path.basename(p), int(max(fs.tail_bytes(), 0)), os.access(p, os.R_OK)))
        if any(not r for _, _, r in unread):
            incomplete.append(dict(reason="unreadable_files", detail="files that cannot be read: "
                                   + ", ".join(f for f, _, r in unread if not r)))
        if any(n and r for _, n, r in unread):
            caveats.append(dict(reason="unread_tails", detail="bytes at the end of files that are not a whole aggregate: "
                                + ", ".join(f"{f} ({n:,} bytes)" for f, n, r in unread if n and r)))
        if kind == "guess":
            caveats.append(dict(reason="end_not_recorded", detail=(
                f"no stop recorded (no word from FSUDAQ, no stop in the run file) and no file written for {FINISHED_QUIET_S} s: "
                "the run is taken to be over; hits still in the boards' buffers may never have been written. If it goes "
                "on, this summary is written again at its end")))
        if n_late:
            caveats.append(dict(reason="late_hits", detail=f"{n_late:,} hits reached the disk after the track finder had passed "
                                f"their time: in the per-channel counts, not in the finder's results"
                                + ("" if not cleared else f" (counted {since})")))
        for e in self.agg_events:
            if e["what"] == "reference":
                caveats.append(dict(reason="reference_changed", detail=e["detail"] + f" ({e['at']})"))
        for b in boards:
            bad = [f"{b['gaps']:,} board aggregates missing (counter gaps: lost before the disk)" if b["gaps"] else "",
                   f"{b['skipped_words'] * 4:,} bytes of damaged data stepped over" if b["skipped_words"] else "",
                   f"{b['other_format']:,} couple aggregates in a format the online decoder does not read" if b["other_format"] else ""]
            if any(bad):
                caveats.append(dict(reason="data_problems", detail=f"Digi-{b['serial']}: " + "; ".join(x for x in bad if x)))
        if outside:
            caveats.append(dict(reason="history_partial", detail=(
                f"{outside:,} hits are outside the rate history and the mean rates' time range"
                + (" (from before the start of the reading, counted per channel)" if self.since is not None else "")
                + (f"; {outliers:,} of them with times far from the rest of the run (damaged time tags?)" if outliers else ""))))
        if self.bins_dropped:
            caveats.append(dict(reason="history_truncated", detail=f"the rate history keeps {RUN_BINS_MAX:,} bins at most: "
                                f"{self.bins_dropped} results outside them are in the totals only"))

        # per channel and board: the whole reading (Clear does not reset these). A mean rate over the data
        # time read; a mid-run start counts the early hits (from before it) apart, so they are taken out
        hits = self.run_counts.astype(np.float64)
        rate_note = (f"hits / rate_span_s ({'from the first hit read' if self.since is None else 'from the start of the reading'} "
                     "to the last" + (", without the far-off times" if outliers else "") + ")")
        rate_ok = rate_span is not None and rate_span > 0
        span_all, span = span, rate_span
        if self.since is not None:
            if cleared:
                rate_ok, rate_note = False, "not known: started mid-run and counts restarted (the early hits are not known)"
            else:
                hits = hits - D.early
        channels = [dict(slot=int(sl), board=self.labels[sl]["board"], ch=self.labels[sl]["ch"], role=self.labels[sl]["role"],
                         hits=int(self.run_counts[sl]), mean_rate_hz=float(hits[sl] / span) if rate_ok else None,
                         late=int(D.late[sl]), early=int(D.early[sl]))
                    for sl in self.slots]
        board_out = []
        for b in boards:
            sl = slice(b["id"] * nbc, (b["id"] + 1) * nbc)
            board_out.append(dict(serial=b["serial"], files=b["files"], bytes=b["bytes"], hits_in_files=b["hits"],
                                  hits=int(self.run_counts[sl].sum()),
                                  mean_rate_hz=float(hits[sl].sum() / span) if rate_ok else None,
                                  late=b["late"], early=b["early"], has_data=b["has_data"], aggregates_missing=b["gaps"],
                                  damaged_bytes=b["skipped_words"] * 4, other_format_aggregates=b["other_format"]))
        tb = S.tables
        finder = dict(
            counted=since, hits=int(self.owned), bunches=int(A.n_bunches), unmapped_hits=int(A.n_bad),
            late_hits=n_late, early_hits=n_early, statuses=list(ff.STATUS),
            status_counts={ff.STATUS[i]: int(c) for i, c in enumerate(A.h_status)},
            accepted_tracks=int((A.h_ntrk * np.arange(ff.NTRK)).sum()),
            lucite=dict(yes=int(A.h_lucite[0]), no=int(A.h_lucite[1])),
            decided=dict(fast_path=int(A.n_fast[0]), reference_finder=int(A.n_slow), reference_cpu_s=float(A.slow_s),
                         over_1024_hits=int(A.n_over)),
            window_ns=self.window_ps / 1000, window_src=self.window_src,
            setup=dict(setting=tb.setting, setting_src=S.src.get("setting"), map=S.map_path, map_rows=int(tb.map_n),
                       calibrated=bool(tb.calibrated), cabling=S.cabling,
                       installed=[c for c, i in zip(ff.COUNTERS, ch.installed) if i],
                       missing=[c for c, i in zip(ff.COUNTERS, ch.installed) if not i],
                       channels=[len(o) - 1 for o in occ], timing="offsets file" if S.has_offsets else "no offsets",
                       k=None if math.isinf(tb.k) else float(tb.k), nsig=float(tb.nsig), threshold=float(tb.threshold),
                       uncalibrated=len(ch.uncalibrated)))
        histograms = dict(
            momentum=dict(unit="MeV/c", what="accepted tracks, momentum from the map", edges=ff.P_EDGES.tolist(), counts=A.h_p.tolist()),
            timing_residual=dict(unit="ns", what="accepted tracks: measured minus expected flight time", edges=ff.RES_EDGES.tolist(),
                                 t0_t1=A.h_res[0].tolist(), t1_t2=A.h_res[1].tolist()),
            t2_minus_t1=dict(unit="ns", what="accepted tracks: T2 time minus T1 time", edges=ff.DT_EDGES.tolist(), counts=A.h_dt.tolist()),
            light_ratio=dict(unit="hit light / expected light of one pion", what="accepted tracks, per plane T0, T1, T2",
                             edges=ff.R_EDGES.tolist(), planes=A.h_r.tolist()),
            cluster_size=dict(unit="adjacent channels", bins=[str(i) for i in range(1, ff.NCL)] + [f"{ff.NCL}+"], planes=A.h_cl.tolist()),
            candidates_per_bunch=dict(bins=[str(i) for i in range(ff.NCAND - 1)] + [f"{ff.NCAND - 1}+"], counts=A.h_ncand.tolist()),
            tracks_per_bunch=dict(bins=[str(i) for i in range(ff.NTRK - 1)] + [f"{ff.NTRK - 1}+"], counts=A.h_ntrk.tolist()),
            hits_per_bunch=dict(bins=[str(i) for i in range(ff.NHIT - 1)] + [f"{ff.NHIT - 1}+"], counts=A.h_nhit.tolist()))
        run = self._summary_run(kind, folder, name, rf)
        files = run_settings_files(folder, name)
        return dict(
            format=SUMMARY_FORMAT, version=SUMMARY_VERSION, written_at=iso_time(time.time()), source=src,
            dashboard=dict(commit=SOURCE_COMMIT, host=socket.gethostname(), kernels="numba" if FAST else "numpy"),
            run=run,
            reading=dict(started_at=iso_time(self.reading_wall), from_start=self.since is None, steps=self.nstep,
                         worker_cpu_s=round(self.proc_s, 3)),
            data=dict(files=fp["files"], bytes=fp["bytes"], t0_s=t0s,
                      first_s=None if first_ps is None else first_ps / PS - t0s,
                      last_s=None if last_ps is None or t0s is None else last_ps / PS - t0s, span_s=span_all, rate_span_s=span,
                      hits_counted=int(self.run_counts.sum()), hits_in_finder=int(self.owned)),
            complete=not incomplete, incomplete=incomplete, caveats=caveats,
            scope=dict(per_channel_hits="the whole reading", finder=since, late_early=since, history="the whole reading",
                       mean_rate=rate_note),
            boards=board_out, channels=channels, finder=finder, histograms=histograms,
            occupancy=dict(what="the finder's hits per amplifier channel, per plane T0, T1, T2 (index 0 unused); counters",
                           planes=occ, counters=counters),
            history=history,
            setup=setup, setup_known=src == "live",
            setup_at_run_time="this setup: the dashboard followed the run while it was taken" if src == "live" else "unknown",
            aggregate_events=list(self.agg_events),
            problems_at_end=health,
            problems_during_reading=[] if src == "backfill" else
            [{k: v for k, v in p.items() if k != "last_step"} for p in self.problems.values()],
            problems_note=("built afterwards: only what the files show (problems_at_end); the live checks did not apply"
                           if src == "backfill" else
                           "seen by the live reading of this run before it was left (for a look at another run), and by this "
                           "reading" if self.watch_gaps or (src != "live" and self.problems) else
                           f"seen at the analysis steps (about one a second): first and last time, steps and separate episodes"
                           + (f"; {self.problems_over} more sightings of other problems not kept" if self.problems_over else "")),
            settings=dict(**files, experiment_config=self.a.config, setup_files=S.files, setup_src=S.src,
                          setup_warnings=list(S.warnings), setup_problems=list(S.problems),
                          window_ns=self.window_ps / 1000, window_src=self.window_src, reference_slot=self.ref_slot,
                          hold_s=self.a.hold_s, stall_s=self.a.stall_s, max_slice_s=self.a.max_slice_s,
                          boards=list(self.serials), eight_channel_boards=[s for s in self.serials if self.nch[s] == 8]),
            fingerprint=fp,
            units=dict(time="seconds of data time (the boards' clock) from data.t0_s, unless a key says otherwise; "
                            "*_at, start, stop: wall clock, ISO 8601",
                       rate="Hz (hits or bunches per second)", bytes="bytes", momentum="MeV/c", timing="ns"))

    def _source(self):
        """Who reads. The rule: "live" is a reading that began while the run was being taken, so it follows
        it (a mid-run start, and a /load of the run being taken, too). A reading that began on a run already
        over (its run file has the stop, or nothing was written for FINISHED_QUIET_S, e.g. after a restart)
        is "load" when asked for by /load, else "read_after_end"; a run written again after that becomes live
        (_begin clears finished_at_start). "backfill" and "replay" are what they say."""
        if self.a.summarise:
            return "backfill"
        if self.a.replay:
            return "replay"
        if not self.finished_at_start:
            return "live"
        if self.inspect and self.folder and os.path.abspath(self.inspect) == os.path.abspath(self.folder):
            return "load"
        return "read_after_end"

    def _interrupted_doc(self):
        """Under the lock, as a reading is left: (summary, ...) of a run followed live whose end was not
        reached, with what was processed; None if there is nothing to write."""
        if (self.a.no_summaries or self.summary_kind is not None or self._source() != "live" or self.t_first is None
                or not self.run_counts.any()):
            return None
        return self._summary_doc("interrupted"), "interrupted", run_file_key(self.folder), self.reading

    def _summary_run(self, kind, folder, name, rf):
        """The summary's "run": FSUDAQ's run file as it is now, and how the end was known."""
        stop = rf.get("stop") or None
        m = re.match(r"^(.*)_(\d+)$", name)
        how = {"stopped": "FSUDAQ said the run stopped" if self.stopped_folder == folder else "the run file has the stop",
               "guess": f"no stop recorded; no file written for {FINISHED_QUIET_S} s",
               "replay": "a replay of the whole run",
               "interrupted": "not reached: the dashboard left this run (the next one started) before its end was processed"}[kind]
        return dict(name=name, folder=folder, number=rf.get("run") if rf.get("run") is not None else (int(m.group(2)) if m else None),
                    prefix=m.group(1) if m else None, start=rf.get("start"), stop=stop and stop.get("time"),
                    start_comment=rf.get("start_comment"), stop_comment=stop and stop.get("comment"),
                    duration_s=(stop["run_ms"] / 1000 if stop and isinstance(stop.get("run_ms"), (int, float)) and stop["run_ms"] >= 0
                                else None),
                    stop_problems=stop and stop.get("problems"), led=rf.get("led"), clock=rf.get("clock"),
                    fsudaq=rf.get("source"), run_file_error=rf.get("error"), end=kind, end_how=how)

    def _run_history(self, first_ps, last_ps, t0s):
        """Hits per board and the finder's bunches over the whole reading, in RUN_BIN_S bins merged to at
        most HISTORY_POINTS. covered_s: the data time a point stands for (a bin cut by the run's first or
        last hit, or the finder's slices in it), so hits / covered_s is the mean rate there. Bins far from
        the rest (a gap of OUTLIER_GAP_S, under OUTLIER_SHARE of the hits: damaged time tags) are left out
        of the history and of the mean rates' span, and counted. Loops over the bins there are, never over
        an empty range. Returns (history, span for the mean rates in s, hits outside it, of them far off)."""
        out = dict(bin_s=RUN_BIN_S, daq=None, finder=None)
        if first_ps is None or last_ps is None:
            return out, None, 0, 0
        keys = sorted(self.run_daq_bins)
        tot = {k: int(self.run_daq_bins[k].sum()) for k in keys}
        groups, cur = [], []
        for k in keys:
            if cur and (k - cur[-1]) * RUN_BIN_S > OUTLIER_GAP_S:
                groups.append(cur); cur = []
            cur.append(k)
        if cur:
            groups.append(cur)
        n_all = sum(tot.values())
        main = [g for g in groups if sum(tot[k] for k in g) >= OUTLIER_SHARE * n_all] or groups
        far = {k for g in groups if g not in main for k in g}
        lo_s, hi_s = first_ps / PS, last_ps / PS
        if main:                                # the range of the main groups, cut to the hits read
            lo_s = max(lo_s, main[0][0] * RUN_BIN_S) if int(lo_s // RUN_BIN_S) in far else lo_s
            hi_s = min(hi_s, (main[-1][-1] + 1) * RUN_BIN_S) if int(hi_s // RUN_BIN_S) in far else hi_s
        k0, k1 = int(lo_s // RUN_BIN_S), int(hi_s // RUN_BIN_S)
        g = max(1, -(-(k1 - k0 + 1) // HISTORY_POINTS))
        npts = -(-(k1 - k0 + 1) // g)
        nb = len(self.serials)
        hits = np.zeros((npts, nb), np.int64)
        outside = outliers = 0
        for k in keys:
            if k0 <= k <= k1 and k not in far:
                hits[(k - k0) // g] += self.run_daq_bins[k]
            else:
                outside += tot[k]
                outliers += tot[k] if k in far else 0
        t0, t1, cov, keep = [], [], [], []
        for i in range(npts):
            a, b = max((k0 + i * g) * RUN_BIN_S, lo_s), min((k0 + (i + 1) * g) * RUN_BIN_S, hi_s)
            if b > a or hits[i].any():
                keep.append(i); t0.append(round(a - t0s, 6)); t1.append(round(max(a, b) - t0s, 6)); cov.append(round(max(0.0, b - a), 6))
        out["bin_s"] = RUN_BIN_S * g
        out["daq"] = dict(boards=list(self.serials), t0_s=t0, t1_s=t1, covered_s=cov, hits=hits[keep].tolist())
        fk = [k for k in sorted(self.run_fin_bins) if k0 <= k <= k1]
        if fk:
            gf = max(1, -(-(fk[-1] - fk[0] + 1) // HISTORY_POINTS))
            rows = np.zeros((-(-(fk[-1] - fk[0] + 1) // gf), 3 + len(ff.STATUS)))
            for k in fk:
                rows[(k - fk[0]) // gf] += self.run_fin_bins[k]
            idx = [i for i in range(rows.shape[0]) if rows[i, 0] > 0 or rows[i, 1] > 0]
            out["finder"] = dict(bin_s=RUN_BIN_S * gf, t0_s=[(fk[0] + i * gf) * RUN_BIN_S - t0s for i in idx],
                                 t1_s=[(fk[0] + (i + 1) * gf) * RUN_BIN_S - t0s for i in idx],
                                 covered_s=[round(float(rows[i, 0]), 6) for i in idx], bunches=[int(rows[i, 1]) for i in idx],
                                 tracks=[int(rows[i, 2]) for i in idx],
                                 status={st: [int(rows[i, 3 + j]) for i in idx] for j, st in enumerate(ff.STATUS)})
        return out, hi_s - lo_s, outside, outliers

    # ------------------------------------------------------------ backfill: summaries of older runs
    def backfill_state(self):
        """For the page; no lock (state() holds self.lock, and backfill_tick takes bf_lock before it)."""
        bf, done = self.bf, self.bf_done[-1:]
        return dict(enabled=self.bf_on, running=bf and os.path.basename(bf["folder"]), waiting=None if bf else self.bf_busy,
                    made=len(self.bf_done), last=dict(run=os.path.basename(done[0][0]), at=done[0][1]) if done else None,
                    skipped=len(self.bf_skipped))

    def _busy_reason(self):
        """Why no backfill may run now; None when the DAQ is idle. Strict: any sign of a run being taken, or
        of the dashboard still working on the run it shows, is enough; FSUDAQ's word (/run) and the disk
        (a run without a stop, see _open_on_disk) both count. Takes the lock only to copy."""
        with self.lock:
            run_open, inspect, folder, all_done, mode = self.run_open, self.inspect, self.folder, self.all_done, self.mode
            pending, follow, stopped = bool(self.pending), self.follow, self.stopped_folder
        if run_open and run_file_has_stop(run_open):   # the /stopped was missed: the run file says it ended
            with self.lock:
                if self.run_open == run_open:
                    self.run_open = None
            run_open = None
        if run_open:
            # FSUDAQ said /run and never /stopped: it is taking the run, unless it is gone, or nothing of the run
            # grew for OPEN_RUN_MAX_S (then it is said so, and the run is no longer taken for open)
            gone = self.fsudaq_probe() is False
            age = time.time() - max(newest_data_mtime(run_open) or 0.0, self.run_open_wall)
            if not gone and age < OPEN_RUN_MAX_S:
                return f"FSUDAQ is taking run {os.path.basename(run_open)}"
            with self.lock:
                if self.run_open == run_open:
                    self.run_open = None
            say(f"{time.strftime('%H:%M:%S')} run {os.path.basename(run_open)}: no stop recorded, "
                + ("FSUDAQ is not running" if gone else f"nothing written for {age / 3600:.0f} h") + ": no longer taken for open")
        if inspect:
            return "a recorded run is being read for a look"
        if folder is not None and not all_done and (mode != "waiting" or pending):
            return f"the dashboard is still reading {os.path.basename(folder)}"   # (waiting: no data; the checks below)
        if mode in ("running", "paused"):
            return "data are being written"
        live = follow or newest_run_folder(self.a.data_path)
        for f in {os.path.abspath(x) for x in (folder, live) if x}:
            if f == stopped or run_file_has_stop(f):
                continue                        # FSUDAQ said it stopped, or its run file has the stop
            if not folder_quiet(f, FINISHED_QUIET_S):
                return f"{os.path.basename(f)} was written in the last {FINISHED_QUIET_S} s and has no stop recorded"
        return self._open_on_disk(stopped)

    def _open_on_disk(self, stopped):
        """A run the disk says may be being taken: of the newest run folder by its data's time and the one with
        the highest run number (over every root), one whose run file has no stop, unless FSUDAQ said it
        stopped, FSUDAQ is not running, or no file of it grew for OPEN_RUN_MAX_S. Without a run file (older
        FSUDAQ) a folder cannot say. Looked at every 10 s at most."""
        now = time.time()
        if now - self.open_cache[0] < 10:
            return self.open_cache[1]
        cands = []
        for root in self.run_roots():
            for d in glob.glob(os.path.join(glob.escape(root), "*")):
                t = newest_data_mtime(d) if os.path.isdir(d) else None
                if t is not None:
                    m = re.search(r"_(\d+)$", d)
                    cands.append((t, int(m.group(1)) if m else -1, os.path.abspath(d)))
        look = {max(cands)[2], max(cands, key=lambda c: (c[1], c[0]))[2]} if cands else set()
        why = None
        for t, _, d in sorted(c for c in cands if c[2] in look):
            if d == stopped or run_file_key(d) is None or run_file_has_stop(d) or now - t >= OPEN_RUN_MAX_S:
                continue
            if self.fsudaq_probe() is False:
                continue                        # nobody is taking it: FSUDAQ ended without the stop
            why = f"run {os.path.basename(d)} has no stop recorded (written {(now - t) / 60:.0f} min ago, FSUDAQ running)"
        self.open_cache = (now, why)
        return why

    def backfill_tick(self):
        """Main loop, after each step: start, watch or stop the child that makes the summary of an older
        run. A child is started only when no run is being taken (_busy_reason) and none was for
        BACKFILL_IDLE_S; it is killed at once when that changes (and by run_started)."""
        if not self.bf_on:
            return
        with self.bf_lock:
            self._bf_reaped()
            busy = self._busy_reason()
            now = time.time()
            if self.bf is not None:
                if busy:
                    self._bf_kill(f"stopped: {busy}")
                else:
                    self._bf_poll(now)
            if busy:
                self.bf_busy, self.bf_idle_since = busy, None
                return
            if self.bf is not None or self.bf_reap:
                return
            if self.bf_idle_since is None:
                self.bf_idle_since = now
            if now - self.bf_idle_since < BACKFILL_IDLE_S:
                self.bf_busy = f"waits until nothing was taken for {BACKFILL_IDLE_S} s"
                return
            if now - self.bf_looked < BACKFILL_LOOK_S:
                return
            self.bf_looked = now
            folder = self._bf_next()
            if folder is None:
                self.bf_busy = "every recorded run has a summary (or cannot have one)"
                return
            self._bf_start(folder, now)

    def backfill_stop(self, why):
        """Kill the backfill child now (a run starts, the dashboard stops); its run is tried again later."""
        with self.bf_lock:
            self._bf_reaped()
            if self.bf is not None:
                self._bf_kill(f"stopped: {why}")
            self.bf_busy, self.bf_idle_since = why, None

    def _bf_next(self):
        """The newest recorded run that needs a summary (none, an unreadable one, or one of other files),
        is over (stop recorded, or quiet for FINISHED_QUIET_S), is not the run the dashboard reads, and was
        not given up on (bf_skipped, unless its files changed). Only when there is none of those: the newest
        whose summary is incomplete and was not made, or tried, by a backfill for these files (a whole
        re-read may cover it all). None: there is none."""
        current = self.folder and os.path.abspath(self.folder)
        cands = []
        for root in self.run_roots():
            for d in glob.glob(os.path.join(glob.escape(root), "*")):
                if os.path.islink(d) or not os.path.isdir(d) or os.path.abspath(d) == current:
                    continue
                try:
                    newest = max((os.path.getmtime(f) for f in glob.glob(os.path.join(glob.escape(d), "*.fsu"))), default=None)
                except OSError:
                    continue
                if newest is not None:
                    cands.append((newest, d))
        later = []                              # incomplete summaries: read again once the runs without one are done
        now_id = None
        for _, d in sorted(cands, reverse=True):
            fp = run_fingerprint(d)
            meta = summary_meta(d, fp)
            if meta and not meta.get("error") and not meta.get("stale"):
                if not meta["complete"] and meta["source"] != "backfill" and not meta["backfill_tried"]:
                    later.append((d, fp))
                continue
            if self._bf_ready(d, fp):
                return d
        for d, fp in later:
            if now_id is None:
                now_id = self.current_setup_identity()
            meta = summary_meta(d, fp)
            if meta.get("setup_identity") not in (None, now_id):    # (None: no live reading to agree with)
                self.bf_notes[d] = ("not read again: the analysis setup now differs from the one its live summary was made "
                                    "with (experiment config, window, cabling, gains, offsets or map)")
                continue
            self.bf_notes.pop(d, None)
            if self._bf_ready(d, fp):
                return d
        return None

    def current_setup_identity(self):
        """setup_identity() of what a reading would use now (the config file as it is, the window now)."""
        cfg = load_config(self.a)[0]
        return setup_identity(cfg["setting"] or "high", setup_files_record(cfg), self.window_ps, self.serials, self.nch)

    def _bf_ready(self, d, fp):
        """d may be backfilled now: not given up on for these files, over, writable."""
        skip = self.bf_skipped.get(d)
        if skip and skip[0] == fp:
            return False
        if not (run_file_has_stop(d) or folder_quiet(d, FINISHED_QUIET_S)):
            return False
        if not os.access(d, os.W_OK):
            self.bf_skipped[d] = (fp, "its folder is not writable")
            return False
        return True

    def _bf_start(self, folder, now):
        """A child reads the run with this file's own code (same boards, setup and bunch window as now) at
        nice 19 and idle I/O priority, and writes its summary. Only from backfill_tick (bf_lock held)."""
        a = self.a
        cmd = [sys.executable, os.path.join(HERE, "online_dashboard.py"), "--summarise", folder, "--boards", a.boards,
               "--eight-channel-boards", a.eight_channel_boards, "--config", a.config or "",
               "--window", repr(self.window_ps / 1000), "--hold-s", repr(a.hold_s), "--stall-s", repr(a.stall_s),
               "--max-slice-s", repr(a.max_slice_s), "--min-step-s", repr(a.min_step_s)]
        for opt in ("setting", "map", "cabling", "gains", "time_offsets"):
            if getattr(a, opt) is not None:
                cmd += ["--" + opt.replace("_", "-"), str(getattr(a, opt))]
        if self.ref_slot is not None:
            cmd += ["--ref-slot", str(self.ref_slot)]
        cmd += ["--window-src", f"the dashboard's at the time ({self.window_src})", "--parent-pid", str(os.getpid())]
        nice, ionice = shutil.which("nice"), shutil.which("ionice")
        if nice:                                # from the first instruction on (the child's own os.nice comes after its imports)
            cmd = [nice, "-n", "19"] + cmd
        if ionice:
            cmd = [ionice, "-c", "3"] + cmd     # idle I/O class: the disk only when nothing else wants it
        fp = run_fingerprint(folder)
        for t in glob.glob(os.path.join(glob.escape(folder), f".{glob.escape(os.path.basename(summary_path(folder)))}.*.tmp")):
            try:                                # a temporary file of a writer that is gone (a dashboard killed mid-write)
                pid = int(t.rsplit(".", 2)[-2])
                if pid != os.getpid():
                    os.kill(pid, 0)
            except ProcessLookupError:
                try:
                    os.unlink(t)
                except OSError:
                    pass
            except (ValueError, OSError):
                pass
        try:
            st, names = os.stat(folder), set(os.listdir(folder))
            err = tempfile.TemporaryFile()
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err, close_fds=True, cwd=HERE)
        except OSError as ex:
            self.bf_skipped[folder] = (fp, f"could not start the summarising process: {ex}")
            say(f"{time.strftime('%H:%M:%S')} backfill of {folder}: could not start: {ex!r}")
            return
        self.bf = dict(proc=proc, folder=folder, started=now, mtime=(st.st_atime_ns, st.st_mtime_ns), err=err, fp=fp,
                       names=names)
        say(f"{time.strftime('%H:%M:%S')} backfill: summarising {folder} (pid {proc.pid}, nice 19)")

    def _bf_poll(self, now):
        bf = self.bf
        rc = bf["proc"].poll()
        if rc is None:
            if now - bf["started"] > BACKFILL_MAX_S:
                self._bf_kill(f"took longer than {BACKFILL_MAX_S / 3600:g} h", skip=True)
            return
        why = ""
        try:
            bf["err"].seek(0)
            lines = bf["err"].read()[-4000:].decode("utf-8", "replace").strip().splitlines()
            why = lines[-1] if lines else ""
        except OSError:
            pass
        self._bf_cleanup(bf)
        self.bf, self.bf_looked = None, 0.0     # the next run at once
        if rc in (0, 5) and load_summary(bf["folder"])[0] is not None:
            self.bf_done.append((bf["folder"], iso_time(now)))
            del self.bf_done[:-20]
            say(f"{time.strftime('%H:%M:%S')} backfill: summary of {bf['folder']} "
                + ("written" if rc == 0 else "not replaced: the one there covers more, or was made with another setup")
                + f" ({now - bf['started']:.0f} s)")
        else:
            self.bf_skipped[bf["folder"]] = (bf["fp"], why or f"the summarising process ended with code {rc}")
            say(f"{time.strftime('%H:%M:%S')} backfill: no summary of {bf['folder']}: {self.bf_skipped[bf['folder']][1]}")

    def _bf_kill(self, why, skip=False):
        """SIGKILL, and on at once (no waiting under bf_lock, which /run takes): the child is cleaned up by
        _bf_reaped once it is gone."""
        bf = self.bf
        try:
            bf["proc"].kill()
        except OSError:
            pass
        self.bf_reap.append(bf)
        self.bf = None
        if skip:
            self.bf_skipped[bf["folder"]] = (bf["fp"], why)
        say(f"{time.strftime('%H:%M:%S')} backfill of {bf['folder']} {why}")

    def _bf_reaped(self):
        for bf in list(self.bf_reap):
            if bf["proc"].poll() is not None:
                self._bf_cleanup(bf)
                self.bf_reap.remove(bf)

    def _bf_cleanup(self, bf):
        """After the child: no temporary file of it left in the run folder, and the folder's time as it was,
        but only if nothing else changed the folder meanwhile (the same entries as when the child started:
        a run FSUDAQ starts there, e.g. one recorded again, keeps its own time)."""
        folder = bf["folder"]
        tmp = os.path.join(folder, f".{os.path.basename(summary_path(folder))}.{bf['proc'].pid}.tmp")
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        except OSError as ex:
            say(f"backfill: cannot remove {tmp}: {ex!r}")
        summary = os.path.basename(summary_path(folder))
        try:
            if (os.stat(folder).st_mtime_ns != bf["mtime"][1]
                    and set(os.listdir(folder)) - {summary} == bf["names"] - {summary}):
                os.utime(folder, ns=bf["mtime"])
        except OSError:
            pass
        try:
            bf["err"].close()
        except OSError:
            pass


def folder_quiet(folder: str, quiet_s: float) -> bool:
    """No data file of the folder was written in the last quiet_s seconds (wall clock): a finished run."""
    times = []
    for p in glob.glob(os.path.join(folder, "*.fsu")):
        try:
            times.append(os.path.getmtime(p))
        except OSError:
            pass
    return bool(times) and time.time() - max(times) > quiet_s


_RUN_FILES = {}   # path -> ((mtime_ns, size), what read_run_file made of it)


def read_run_file(folder, cache=True):
    """What the page shows of FSUDAQ's run file <folder>/<folder name>_run.json (README "Data folder"):
    start and stop, the LED trigger's settings and commands, the source FSUDAQ ran from. Not the
    registers. None when the folder has none (a run from before the run file). FSUDAQ writes it whole
    (QSaveFile), so a file is never seen half written; it is read again only when it changed."""
    if not folder:
        return None
    path = os.path.join(folder, os.path.basename(os.path.normpath(folder)) + "_run.json")
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (st.st_mtime_ns, st.st_size)
    hit = _RUN_FILES.get(path)
    if hit and hit[0] == key:
        return hit[1]
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        led = d.get("led") or {}
        stop = d.get("stop")
        out = dict(file=os.path.basename(path), run=d.get("run"), start=(d.get("start") or {}).get("time"),
                   start_comment=(d.get("start") or {}).get("comment"),
                   stop=None if not stop else {k: stop.get(k) for k in ("time", "run_ms", "comment", "problems")},
                   led=dict(enabled=bool(led.get("enabled")), delay_s=led.get("delay_s"), hold_s=led.get("hold_s"),
                            relay=led.get("relay"),
                            events=[{k: e.get(k) for k in ("what", "command", "sent", "run_ms", "reply", "ok", "switched", "error")}
                                    for e in led.get("events") or []]),
                   source=(d.get("fsudaq") or {}).get("source"), boards=len(d.get("boards") or []),
                   clock=dict(start=(d.get("clock") or {}).get("start") or None, stop=(stop or {}).get("clock") or None))
    except (OSError, ValueError, AttributeError, TypeError) as ex:
        out = dict(file=os.path.basename(path), error=f"cannot read it: {ex}")
    if cache:
        _RUN_FILES.clear()              # one run at a time
        _RUN_FILES[path] = (key, out)
    return out


def run_file_key(folder):
    """(mtime, size) of the folder's run file, None without one: whether it changed."""
    try:
        st = os.stat(os.path.join(folder, os.path.basename(os.path.normpath(folder)) + "_run.json"))
        return [st.st_mtime_ns, st.st_size]
    except OSError:
        return None


_RUN_STOPS = {}   # run file path -> (its key, has a stop): the backfill asks about many folders, often


def run_file_has_stop(folder):
    """The folder's run file records the run's stop."""
    path = os.path.join(folder, os.path.basename(os.path.normpath(folder)) + "_run.json")
    key = run_file_key(folder)
    if key is None:
        return False
    hit = _RUN_STOPS.get(path)
    if hit and hit[0] == key:
        return hit[1]
    try:
        with open(path, encoding="utf-8") as fh:
            stop = bool((json.load(fh) or {}).get("stop"))
    except (OSError, ValueError, AttributeError):
        stop = False
    if len(_RUN_STOPS) > 2000:
        _RUN_STOPS.clear()
    _RUN_STOPS[path] = (key, stop)
    return stop


def newest_data_mtime(folder):
    """When a data file of the folder was last written (None: no .fsu)."""
    times = []
    for p in glob.glob(os.path.join(glob.escape(folder), "*.fsu")):
        try:
            times.append(os.path.getmtime(p))
        except OSError:
            pass
    return max(times) if times else None


_FSUDAQ_SEEN = [0.0, None]   # (when looked, the answer)


def fsudaq_running():
    """Whether FSUDAQ runs on this machine (read-only): the process in pid.dat beside online/ (FSUDAQ writes
    it at start), else any FSUDAQ_Qt6 in /proc. True, False, or None (cannot tell). It says FSUDAQ is
    open, not that it acquires; looked at every 10 s at most."""
    now = time.time()
    if now - _FSUDAQ_SEEN[0] < 10:
        return _FSUDAQ_SEEN[1]
    ans = None
    try:
        with open(os.path.join(os.path.dirname(HERE), "pid.dat")) as f:
            with open(f"/proc/{int(f.read().strip())}/comm") as g:
                ans = True if g.read().strip() == "FSUDAQ_Qt6" else None
    except (OSError, ValueError):
        pass
    if ans is None and os.path.isdir("/proc"):
        ans = False
        for c in glob.glob("/proc/[0-9]*/comm"):
            try:
                with open(c) as g:
                    if g.read().strip() == "FSUDAQ_Qt6":
                        ans = True
                        break
            except OSError:
                pass
    _FSUDAQ_SEEN[:] = [now, ans]
    return ans


def iso_time(t):
    """Wall-clock time as FSUDAQ writes it in the run file: ISO 8601, ms, with the UTC offset."""
    return datetime.datetime.fromtimestamp(t).astimezone().isoformat(timespec="milliseconds")


PROBLEM_NUMBERS = re.compile(r"(?<![\w-])\d[\d,.]*")   # the numbers of a warning (not a board's name, Digi-1443)


def _source_commit():
    """Branch and commit of the source tree this runs from, read from .git (no process is started)."""
    git = os.path.join(os.path.dirname(HERE), ".git")
    try:
        with open(os.path.join(git, "HEAD")) as fh:
            head = fh.read().strip()
        if not head.startswith("ref: "):
            return head[:7]
        ref = head[5:]
        sha = None
        try:
            with open(os.path.join(git, ref)) as fh:
                sha = fh.read().strip()
        except OSError:
            with open(os.path.join(git, "packed-refs")) as fh:
                sha = next((ln.split()[0] for ln in fh if ln.strip().endswith(" " + ref)), None)
        return f"{ref.removeprefix('refs/heads/')} {(sha or '?')[:7]}"
    except (OSError, ValueError):
        return None


SOURCE_COMMIT = _source_commit()   # as loaded: the code that runs (uncommitted changes are not seen)


def summary_path(folder):
    return os.path.join(folder, os.path.basename(os.path.normpath(folder)) + "_summary.json")


def run_fingerprint(folder):
    """What a summary describes: the run's data files, by number and total size."""
    n = size = 0
    for p in glob.glob(os.path.join(glob.escape(folder), "*.fsu")):
        try:
            size += os.stat(p).st_size
            n += 1
        except OSError:
            pass
    return dict(files=n, bytes=size)


def run_settings_files(folder, name):
    """The run's own records of its settings, by file name in its folder (None: not there): FSUDAQ's run
    file, its copies of the program settings and run protocol (newer FSUDAQ), the boards' snapshots."""
    def there(f):
        return f if os.path.isfile(os.path.join(folder, f)) else None
    return dict(run_folder=folder, run_file=there(name + "_run.json"), program_settings=there(name + "_programSettings.txt"),
                run_protocol=there(name + "_run_protocol.json"),
                board_settings=sorted(os.path.basename(p) for p in glob.glob(os.path.join(glob.escape(folder), "*.bin"))))


def write_summary_file(folder, doc):
    """Write the run summary whole: a hidden temporary file beside it (FSUDAQ's check for a run's files
    does not see it), flushed to the disk, then renamed over the summary, so a reader never sees half a
    file; not at all if the run's data files changed meanwhile (a run recorded again). The folder's
    modification time is put back afterwards: the status page and newest_run_folder() take the newest run
    by it, and a summary written into an older run must not make that run the newest.

    Over a summary of the same files made by another reading:
    * what only a live reading knew (the problems it saw, how far it covered the run, its setup) is kept
      in "previous_live" (from a live summary, or carried on from one);
    * a reading that is not live never replaces a live summary made with another setup (setup_identity);
      with the same setup as the live reading its own setup counts as known (setup_known);
    * a complete summary is not replaced by an incomplete one, nor (by a reading that is not live) an
      incomplete one by one with more reasons.
    A backfill that is kept out leaves its mark ("backfill_tried") in the summary there, so it is not
    tried again for these files. Returns (path, written, note); raises OSError or ValueError."""
    path = summary_path(folder)
    old = load_summary(folder)[0]
    same = bool(old) and old.get("fingerprint") == doc["fingerprint"]
    # also over a summary of fewer files (the run's files grew since: a stale one): the live part is kept,
    # marked files_changed (FSUDAQ's Overwrite moves an old run's summary aside with its files)
    other = bool(old) and (old.get("source") != doc["source"]
                           or (old.get("reading") or {}).get("started_at") != (doc.get("reading") or {}).get("started_at"))
    if other:                                   # another reading's summary (not this reading's own, written again)
        if old.get("source") == "live":
            doc["previous_live"] = live_part(old, files_changed=not same)
        elif old.get("previous_live"):
            doc["previous_live"] = dict(old["previous_live"], files_changed=bool(old["previous_live"].get("files_changed") or not same))
    elif old and old.get("previous_live") and "previous_live" not in doc:
        doc["previous_live"] = old["previous_live"]  # this reading's own summary again: what it carried stays
    keep = None
    live_id = ((doc.get("previous_live") or {}).get("setup") or {}).get("identity")
    if doc["source"] != "live" and live_id is not None:
        if live_id == doc["setup"]["identity"]:
            doc["setup_known"] = True
            doc["setup_at_run_time"] = "the same as the live reading's (setup.identity)"
            doc["caveats"] = [c for c in doc["caveats"] if c["reason"] != "setup_unknown"]
        elif same and other and old.get("source") == "live":
            keep = "the live summary there was made with another setup (setup.identity)"
    if keep:
        pass
    elif same and old.get("complete") and not doc["complete"]:
        keep = "this reading did not see the whole run"
    elif (same and not old.get("complete") and not doc["complete"] and doc["source"] != "live"
          and len(doc["incomplete"]) > len(old.get("incomplete") or [])):
        keep = "this reading covered less of the run"
    if keep:
        if doc["source"] == "backfill":
            old["backfill_tried"] = dict(fingerprint=doc["fingerprint"], at=doc["written_at"], why=keep,
                                         incomplete=[x["reason"] for x in doc["incomplete"]])
            _write_json_whole(folder, path, old, old["fingerprint"])
        return path, False, f"kept the summary already there ({old.get('source')}, {old.get('written_at')}): {keep}"
    _write_json_whole(folder, path, doc, doc["fingerprint"])
    return path, True, ""


def live_part(old, files_changed=False):
    """What a live summary knew that a later reading of the run cannot: the problems seen while the run
    was taken, how far that reading covered it, and its setup (bounded: PROBLEMS_MAX kinds of problem).
    files_changed: it was written for fewer of the run's files than the summary that keeps it."""
    return dict(source="live", files_changed=files_changed, written_at=old.get("written_at"), reading=old.get("reading"), complete=old.get("complete"),
                incomplete=old.get("incomplete"), caveats=old.get("caveats"), end=(old.get("run") or {}).get("end"),
                setup=old.get("setup"),
                problems_during_reading=(old.get("problems_during_reading") or [])[:PROBLEMS_MAX],
                problems_note=old.get("problems_note"), aggregate_events=(old.get("aggregate_events") or [])[:100])


def _write_json_whole(folder, path, doc, fingerprint=None):
    """doc to path in folder: temporary file, fsync, rename (not if the run's files are no longer those of
    fingerprint); the folder's modification time put back."""
    body = json.dumps(doc, allow_nan=False, separators=(",", ":")).encode()
    st = os.stat(folder)
    tmp = os.path.join(folder, f".{os.path.basename(path)}.{os.getpid()}.tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        if fingerprint is not None and run_fingerprint(folder) != fingerprint:
            raise OSError("the run's data files changed while its summary was made (recorded again?)")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    finally:
        try:
            os.utime(folder, ns=(st.st_atime_ns, st.st_mtime_ns))
        except OSError:
            pass


def load_summary(folder):
    """(the folder's run summary, None), or (None, why not; None when there is none)."""
    path = summary_path(folder)
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None, None
    except OSError as ex:
        return None, f"summary file not readable: {ex}"
    if st.st_size > SUMMARY_MAX_BYTES:
        return None, f"summary file too large ({st.st_size:,} bytes)"
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        if not isinstance(d, dict) or d.get("format") != SUMMARY_FORMAT:
            raise ValueError("not a run summary")
        return d, None
    except (OSError, ValueError) as ex:
        return None, f"summary file not readable: {ex}"


_SUMMARY_META = {}   # summary path -> ((mtime, size), what /runs shows of it)


def summary_meta(folder, fp):
    """What the run list shows of a run's summary (None: none): complete, source, end, stale (written
    for other data files than fp, the run's files now), or the error reading it."""
    path = summary_path(folder)
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (st.st_mtime_ns, st.st_size)
    hit = _SUMMARY_META.get(path)
    if not hit or hit[0] != key:
        d, err = load_summary(folder)
        meta = dict(error=err) if d is None else dict(complete=bool(d.get("complete")), source=d.get("source"),
                                                    written_at=d.get("written_at"), end=(d.get("run") or {}).get("end"),
                                                    fingerprint=d.get("fingerprint"),
                                                    tried=(d.get("backfill_tried") or {}).get("fingerprint"),
                                                    setup_identity=((d.get("setup") if d.get("source") == "live" else
                                                                     (d.get("previous_live") or {}).get("setup")) or {}).get("identity"))
        if len(_SUMMARY_META) > 2000:
            _SUMMARY_META.clear()
        _SUMMARY_META[path] = hit = (key, meta)
    meta = dict(hit[1])
    then, tried = meta.pop("fingerprint", None), meta.pop("tried", None)
    if "error" not in meta:
        meta["stale"] = then != fp
        meta["backfill_tried"] = tried == fp        # a backfill already tried these files and was kept out
    return meta


def newest_run_folder(data_path: str):
    cands = [d for d in glob.glob(os.path.join(data_path, "*")) if os.path.isdir(d) and glob.glob(os.path.join(d, "*.fsu"))]
    if not cands:
        return None
    return max(cands, key=os.path.getmtime)


def open_page(url, display=""):
    """Show the page in a browser on that X display (FSUDAQ's, which may be another than ours: the
    dashboard outlives the FSUDAQ that started it). xdg-open, else Python's webbrowser."""
    env = dict(os.environ)
    if display:
        env["DISPLAY"] = display

    def run():
        import subprocess
        try:
            subprocess.Popen(["xdg-open", url], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError:
            import webbrowser
            webbrowser.open(url)
    threading.Thread(target=run, daemon=True).start()


# ---------------------------------------------------------------- HTTP
CONTROLS = ("/open", "/run", "/stopped", "/clear", "/window", "/set", "/load", "/live")   # FSUDAQ's calls, the page's shared controls


class Handler(BaseHTTPRequestHandler):
    online: Online = None
    page: bytes = b""

    def log_message(self, *args):          # keep the console for the analysis
        pass

    def reply(self, code, d):
        body = json.dumps(d, allow_nan=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def local(self):                       # FSUDAQ, or a browser on odin (or through an ssh tunnel)
        return self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path, q = u.path, urllib.parse.parse_qs(u.query)   # every route by its exact path: "/clear/" is no way past the check below
        if path in CONTROLS and not self.local():   # others may look, not steer what everyone sees
            body = json.dumps(dict(note="view only: changed on odin or through the ssh tunnel")).encode()
            self.send_response(403)
            self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body); return
        if path == "/open":        # FSUDAQ asks us to show the page (it must not spawn anything itself)
            open_page(f"http://localhost:{self.server.server_address[1]}/", q.get("display", [""])[0])
            self.send_response(204); self.end_headers(); return
        if path == "/runs":        # the recorded runs, for the page's run list
            return self.reply(200, dict(runs=self.online.runs(), inspect=self.online.inspect, backfill=self.online.backfill_state()))
        if path == "/expected":    # the floor tool's published expectation against the live finder, for anyone: read-only
            return self.reply(200, self.online.beam_vs_map())
        if path == "/expected_view":   # the publish's top view (large, changes only with a new publish), for anyone
            return self.reply(*self.online.expected_view())
        if path == "/summary":     # a recorded run's summary, for anyone: read-only, nothing changes for the others
            code, d = self.online.summary_reply(q.get("run", [""])[0])
            return self.reply(code, d)
        if path == "/load":        # the page loads a recorded run for a look
            why = self.online.load(q.get("folder", [""])[0])
            if not why:
                say(f"{time.strftime('%H:%M:%S')} loaded for a look: {self.online.inspect}")
            return self.reply(400 if why else 200, dict(note=why or "loaded"))
        if path == "/live":        # back to the data being taken
            self.online.live()
            self.send_response(204); self.end_headers(); return
        if path == "/run":         # FSUDAQ starts a saving run: follow its folder from now on
            folder = q.get("folder", [""])[0]
            if not folder:
                self.send_response(400); self.end_headers(); return
            self.online.run_started(folder)
            say(f"{time.strftime('%H:%M:%S')} following run folder {folder}")
            self.send_response(204); self.end_headers(); return
        if path == "/stopped":     # FSUDAQ stopped a saving run and closed its files
            folder = q.get("folder", [""])[0]
            if not folder:
                self.send_response(400); self.end_headers(); return
            self.online.run_stopped(folder)
            say(f"{time.strftime('%H:%M:%S')} run stopped: {folder}")
            self.send_response(204); self.end_headers(); return
        if path == "/clear":      # the page's Clear: per-channel and finder histograms start again
            self.online.clear()
            self.send_response(204); self.end_headers(); return
        if path == "/window":     # the page's bunch window [ns]
            try:
                note = self.online.set_window(float(q.get("ns", [""])[0]))
                code = 200
            except ValueError as ex:
                note, code = str(ex) if "window" in str(ex) else "not a number", 400
            body = json.dumps(dict(note=note)).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body); return
        if path == "/set":        # reference channel for the time differences
            try:
                self.online.set_ref(int(q.get("ref", ["-1"])[0]))
            except ValueError:
                self.send_response(400); self.end_headers(); return
            self.send_response(204); self.end_headers(); return
        if path == "/state":
            body = json.dumps(dict(self.online.state(), can_control=self.local()), allow_nan=False).encode()   # Infinity/NaN are not JSON
            ctype = "application/json"
        elif path == "/channel" or path == "/grid" or path == "/compare":
            try:
                view = None
                if "lo" in q:
                    view = (int(q["lo"][0]), int(q["hi"][0]), int(q.get("nb", ["4096"])[0]), q.get("log", ["0"])[0] == "1")
                if path == "/grid":
                    d = self.online.grid(view or (0, 65536, 64, False))
                elif path == "/compare":
                    slots = [int(x) for x in q.get("slots", [""])[0].split(",") if x.strip()]
                    d = self.online.compare(slots, view)
                else:
                    d = self.online.channel(int(q.get("slot", ["-1"])[0]), view)
            except (ValueError, KeyError):
                d = None
            if d is None:
                self.send_response(404); self.end_headers(); return
            body = json.dumps(d, allow_nan=False).encode()
            ctype = "application/json"
        elif path in ("/", "/index.html"):
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
    p.add_argument("--window", type=float, default=None,
                   help="bunch building: a gap longer than this starts a new bunch [ns]; default window_ns in the config file "
                        "(the page's Bunch window saves it there), else 25 = the finder's +-10 ns gate around each plane's "
                        "expected arrival plus the 4.3 ns T0->T2 flight at beta 0.75")
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
    p.add_argument("--bind", default="0.0.0.0", help="address to listen on (127.0.0.1: odin and ssh tunnels only); "
                   "others may view, only odin may use the controls")
    p.add_argument("--workers", type=int, default=8, help="worker processes (0 = in the main loop)")
    p.add_argument("--open-browser", action="store_true", help="open the page in the default browser once the server is up")
    p.add_argument("--exit-with-parent", action="store_true", help="stop when the process that started us (FSUDAQ) is gone")
    p.add_argument("--beam-expected", default=EXPECTED_DEFAULT, help="the floor tool's published expectation (Publish to "
                   "online: format floor-tool-expected/1), for the page's Beam vs map tab; read again when it changes")
    p.add_argument("--no-summaries", action="store_true", help="write no <run>_summary.json into the run folders (and make none "
                                                               "for older runs)")
    p.add_argument("--summaries", action="store_true", help=f"write run summaries also when not on port {PRODUCTION_PORT}: "
                   f"by default only the dashboard on port {PRODUCTION_PORT} (FSUDAQ's) writes into the run folders, so a "
                   "test or second instance never does. Test instances: use a copy of the data, or --no-summaries")
    p.add_argument("--no-backfill", action="store_true", help="make no summaries for older runs between runs")
    p.add_argument("--summarise", metavar="FOLDER", help=argparse.SUPPRESS)   # the backfill child: one run's summary, then exit
    p.add_argument("--window-src", help=argparse.SUPPRESS)                      # ... and where its --window came from
    p.add_argument("--parent-pid", type=int, help=argparse.SUPPRESS)            # ... and the dashboard that started it
    a = p.parse_args(argv)
    a.summaries_off = None
    if a.summarise:
        a.run, a.from_start, a.replay, a.workers = a.summarise, True, False, 0
    elif not a.no_summaries and a.port != PRODUCTION_PORT and not a.summaries:
        a.no_summaries = True
        a.summaries_off = f"port {a.port} is not {PRODUCTION_PORT}: no summaries written (--summaries to write them)"
    if not a.run and not a.data_path:
        p.error("give --data-path or --run")
    return a


def summarise_main(a):
    """--summarise FOLDER: the backfill child. Reads the run whole with the dashboard's own code (Online,
    workers in this process) and writes its summary, source "backfill"; then exits: 0 written, 5 the one
    there kept (it covers more, or was made live with another setup), 2 not made (why: the last line on
    stderr), 3 the dashboard is gone, 4 nothing to summarise. Lowest priority: nice 19 here, idle I/O from
    the ionice it is started with. PR_SET_PDEATHSIG was set at import; a dashboard that ended before
    that is caught here (the parent is then another process)."""
    parent = a.parent_pid or os.getppid()
    if os.getppid() != parent:
        return 3
    try:
        os.nice(19)
    except OSError:
        pass
    folder = a.summarise
    files = glob.glob(os.path.join(glob.escape(folder), "*.fsu"))
    if not files:
        sys.stderr.write("no .fsu files in the folder\n")
        return 4
    for p in files:                             # one it cannot read: no summary that leaves it out silently
        try:
            with open(p, "rb") as fh:
                fh.read(4)
        except OSError as ex:
            sys.stderr.write(f"cannot read {os.path.basename(p)}: {ex.strerror or ex}\n")
            return 2
    try:
        online = Online(a)
        while True:
            if os.getppid() != parent:
                return 3
            online.step()
            if online.summary_kind is not None:
                return 5 if online.summary_kept else 0
            if online.summary_error:
                sys.stderr.write(f"run summary {online.summary_error}\n")
                return 2
            if online.status.startswith("no data yet") and not online.behind and online.stop_notice:
                sys.stderr.write("no hits from the boards analysed (--boards)\n")
                return 4
            if not online.pending:
                time.sleep(0.01)
    except Exception as ex:                     # noqa: BLE001
        sys.stderr.write(f"cannot be read: {ex!r}\n")
        return 2


def main(argv=None):
    a = parse_args(argv)
    if a.summarise:
        code = summarise_main(a)
        sys.stderr.flush()
        os._exit(code)                          # no exit handlers (numba, a pool): the parent waits for this
    online = Online(a)
    Handler.online = online
    with open(os.path.join(HERE, "dashboard.html"), "rb") as fh:
        Handler.page = fh.read()
    srv = ThreadingHTTPServer((a.bind, a.port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    if a.open_browser:
        open_page(f"http://localhost:{a.port}/")
    say(f"online dashboard: http://{socket.gethostname()}:{a.port}/   kernels={'numba' if FAST else 'numpy (slow)'}  "
        f"{'replay of ' + a.run if a.replay else ('run ' + a.run if a.run else 'following ' + (a.follow or a.data_path))}")
    for w in online.setup.warnings + online.setup.problems:
        say("  setup: " + w)
    if a.summaries_off:
        say("  " + a.summaries_off)
    online.write_async = True                # a run start never waits for a summary's disk write
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
            try:                             # summaries of older runs, between runs only
                online.backfill_tick()
            except Exception as ex:          # noqa: BLE001
                say(f"backfill failed: {ex!r}")
            if t0 - last_report > 10:
                last_report = t0
                try:                     # the console report must never end the dashboard
                    s = online.state()
                    seen = (s['status'], round(s['processed_s'] or 0, 1), s['hits'])
                    if seen != last_seen:    # quiet while nothing happens (between runs, page closed)
                        say(f"{time.strftime('%H:%M:%S')} {s['status']}: processed {s['processed_s'] or 0:.1f} s of beam, "
                            f"{s['hits']:,} hits in the finder, {s['late']:,} late, {s['bunches']:,} bunches, "
                            f"{s['status_counts']['unique'] + s['status_counts']['tracks']:,} accepted, {s['proc_s']:.1f} s worker CPU, "
                            f"{s['in_flight']} tasks out")
                        last_seen = seen
                except Exception as ex:  # noqa: BLE001
                    say(f"report failed: {ex!r}")
            online.wake.wait(max(0.0, a.interval - (time.time() - t0)))
            online.wake.clear()
    finally:
        try:
            online.backfill_stop("the dashboard stops")
            online.flush_writes(5.0)
        except Exception:
            pass
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
