#!/usr/bin/env python3
"""Experiment status: what is going on under the hood, on one page.

    python3 status/status_server.py                 # http://localhost:8060/ (status), /limits (limits and names)

Reads only; it never talks to the digitizers. Every --interval seconds it gathers:

  FSUDAQ       the running process (/proc), the newest run folder's run file (<run>_run.json: start,
               stop, problems, board clocks, LED), the disk of the data path
  online       the online dashboard's GET /state (localhost:8050): its status, health, lag, boards
  pion-ros     the plc_command relay's GET /status on romulus: the latest PLC status (heartbeat,
               analog and thermocouple inputs, outputs) and the ROS nodes up
  limits.json  the names, units, scaling and alarm rules of the PLC inputs (isobutane, pressure, ...)
               and the modes (Cherenkov: normal / full of isobutane), edited on /limits; every save
               is appended to limits_log.jsonl, so what was in force when is on record

and turns it into items: a level (ok, info, warn, bad, critical, nodata), one line of text and the
details behind it. The page polls GET /api/status.
"""
from __future__ import annotations

import argparse
import collections
import copy
import glob
import json
import math
import os
import shutil
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
# summary order: the worst item names the page; info (a plain fact) and ok rank lowest
RANK = {"info": 0, "idle": 0, "ok": 1, "nodata": 2, "warn": 3, "bad": 4, "critical": 5}   # idle: blue, not running
RULE_LEVELS = ("warn", "bad", "critical")
PLC_INPUTS = [f"ai{i}" for i in range(8)] + [f"tc{i}" for i in range(8)]
TC_OPEN = 3276.0          # a thermocouple input reads 3276.7 with nothing connected

DEFAULT_LIMITS = {
    "_comment": "Edited on the status server's /limits page; every save is appended to limits_log.jsonl. "
                "value = raw * scale + offset. Rules: at_or_above / above / below a value, or a change of at "
                "least 'change' within 'within_s' seconds; 'when' limits a rule to a mode.",
    "modes": {"cherenkov": {"name": "Cherenkov", "choices": ["normal", "full of isobutane"], "value": "normal"}},
    # the PLC's inputs (Slowtronics, 31 Oct 2026): ai0 pressure, ai1-ai2 gas sensors, ai5-ai7 Trumeters, tc0-tc5 thermocouples
    "channels": [
        {"id": "cherenkov_pressure", "name": "Cherenkov pressure (WIKA WUC-10)", "group": "Cherenkov", "source": "plc.ai0",
         "scale": 1.0, "offset": 0.0, "unit": "psig", "digits": 3, "range": [-14.7, 100],
         "rules": [{"above": 2, "level": "warn"},
                   {"below": 0.3, "level": "warn", "when": {"cherenkov": "full of isobutane"}},
                   {"change": 0.05, "within_s": 3600, "level": "warn", "when": {"cherenkov": "full of isobutane"}}]},
        {"id": "isobutane_1", "name": "Gas sensor 1 (RKI M2A)", "group": "Cherenkov", "source": "plc.ai1",
         "scale": 1.0, "offset": 0.0, "unit": "%LEL", "digits": 1, "range": [0, 100],
         "note": "100 %LEL = 1.6 % isobutane in air",
         "rules": [{"at_or_above": 5, "level": "warn"}, {"at_or_above": 10, "level": "bad"},
                   {"at_or_above": 100, "level": "critical"}]},
        {"id": "isobutane_2", "name": "Gas sensor 2 (RKI M2A)", "group": "Cherenkov", "source": "plc.ai2",
         "scale": 1.0, "offset": 0.0, "unit": "%LEL", "digits": 1, "range": [0, 100],
         "note": "100 %LEL = 1.6 % isobutane in air",
         "rules": [{"at_or_above": 5, "level": "warn"}, {"at_or_above": 10, "level": "bad"},
                   {"at_or_above": 100, "level": "critical"}]},
    ] + [{"id": f"trumeter_{i - 4}", "name": f"Trumeter {i - 4}", "group": "PLC", "source": f"plc.ai{i}",
          "scale": 1.0, "offset": 0.0, "unit": "", "digits": 3, "range": [0, 5], "rules": []} for i in (5, 6, 7)]
      # two thermocouples per detector: tc0/1 tracker 0, tc2/3 tracker 1, tc4/5 tracker 2, tc6/7 the Cherenkov.
      # An open input on this card reads a noisy staircase (about 120-520) or 3276.7, which looks like a real temperature:
      # "connected" says which are plugged in; the others show as not connected whatever they read.
      + [{"id": f"tc{i}", "name": (f"Tracker {i // 2} temperature {'AB'[i % 2]}" if i < 6 else f"Cherenkov temperature {'AB'[i % 2]}") + f" (tc{i})",
          "group": "Trackers" if i < 6 else "Cherenkov", "source": f"plc.tc{i}",
          "scale": 1.0, "offset": 0.0, "unit": "°C", "digits": 1, "range": [-50, 1300], "rules": [{"above": 80, "level": "bad"}],
          "connected": i >= 6} for i in range(8)],   # tc0-tc5 read open inputs (31 Oct 2026): tick them on /limits when plugged in
    "unused": ["plc.ai3", "plc.ai4"],
    "daq": {"disk_warn_gb": 200, "disk_bad_gb": 50, "plc_stale_s": 10, "analysis_lag_warn_s": 30},
}


# ---------------------------------------------------------------- limits
def validate_limits(L: dict) -> list:
    """What is wrong with a limits document (empty: fine)."""
    bad = []
    if not isinstance(L, dict):
        return ["not a JSON object"]
    modes = L.get("modes", {})
    if not isinstance(modes, dict):
        bad.append("modes: not an object")
        modes = {}
    for k, m in modes.items():
        if not isinstance(m, dict) or not isinstance(m.get("choices"), list) or m.get("value") not in m.get("choices", []):
            bad.append(f"mode {k}: needs choices and a value among them")
    ids = set()
    for i, c in enumerate(L.get("channels", [])):
        where = f"channel {i + 1} ({c.get('name') or c.get('id') or '?'})" if isinstance(c, dict) else f"channel {i + 1}"
        if not isinstance(c, dict):
            bad.append(f"{where}: not an object"); continue
        if not c.get("id") or c["id"] in ids:
            bad.append(f"{where}: needs an id of its own")
        ids.add(c.get("id"))
        if c.get("source") not in [None, ""] + [f"plc.{s}" for s in PLC_INPUTS]:
            bad.append(f"{where}: source {c.get('source')!r} is not a PLC input")
        for k in ("scale", "offset"):
            if not isinstance(c.get(k, 0), (int, float)) or not math.isfinite(c.get(k, 0)):
                bad.append(f"{where}: {k} is not a number")
        if not isinstance(c.get("connected", True), bool):
            bad.append(f"{where}: connected must be true or false")
        rg = c.get("range")
        if rg is not None and not (isinstance(rg, list) and len(rg) == 2 and all(isinstance(x, (int, float)) for x in rg) and rg[0] < rg[1]):
            bad.append(f"{where}: range must be [min, max] with min < max")
        for j, r in enumerate(c.get("rules", [])):
            kinds = [k for k in ("at_or_above", "above", "below", "change") if k in r]
            if len(kinds) != 1 or not isinstance(r[kinds[0]], (int, float)):
                bad.append(f"{where}, rule {j + 1}: needs one of at_or_above / above / below / change, with a number")
            if r.get("level") not in RULE_LEVELS:
                bad.append(f"{where}, rule {j + 1}: level must be one of {', '.join(RULE_LEVELS)}")
            if "change" in r and not (isinstance(r.get("within_s"), (int, float)) and r["within_s"] > 0):
                bad.append(f"{where}, rule {j + 1}: a change needs within_s > 0")
            for mk, mv in (r.get("when") or {}).items():
                if mk not in modes or mv not in modes[mk].get("choices", []):
                    bad.append(f"{where}, rule {j + 1}: when {mk} = {mv!r} is not a mode")
    for u in L.get("unused", []):
        if u not in [f"plc.{s}" for s in PLC_INPUTS]:
            bad.append(f"unused: {u!r} is not a PLC input")
    return bad


def load_limits(path: str) -> dict:
    try:
        with open(path) as f:
            L = json.load(f)
        if not validate_limits(L):
            return L
        print(f"{path}: {'; '.join(validate_limits(L))}: the defaults are used")
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as ex:
        print(f"{path}: {ex}: the defaults are used")
    return copy.deepcopy(DEFAULT_LIMITS)


def save_limits(path: str, L: dict, who: str, clock=time.time):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(L, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    with open(os.path.join(os.path.dirname(path) or ".", "limits_log.jsonl"), "a") as f:
        f.write(json.dumps({"time": iso(clock()), "by": who, "limits": L}) + "\n")


def rule_text(r: dict) -> str:
    when = "".join(f" (when {k}: {v})" for k, v in (r.get("when") or {}).items())
    if "at_or_above" in r: return f">= {r['at_or_above']}{when}"
    if "above" in r: return f"> {r['above']}{when}"
    if "below" in r: return f"< {r['below']}{when}"
    return f"change >= {r['change']} within {r['within_s'] / 60:g} min{when}"


def evaluate(channel: dict, value, history, modes: dict, now: float):
    """(level, reasons) of one channel's value against its rules; history: [(t, value)], oldest first."""
    if value is None:
        return "nodata", []
    level, why = "ok", []
    rg = channel.get("range")
    if rg and not (rg[0] <= value <= rg[1]):   # wiring or scaling, or a sensor past full scale: never lowers what the rules say
        level, why = "warn", [f"outside {rg[0]:g} to {rg[1]:g}: not connected? (or wiring, scaling)"]
    for r in channel.get("rules", []):
        if any(modes.get(k, {}).get("value") != v for k, v in (r.get("when") or {}).items()):
            continue
        hit = False
        if "at_or_above" in r: hit = value >= r["at_or_above"]
        elif "above" in r: hit = value > r["above"]
        elif "below" in r: hit = value < r["below"]
        elif "change" in r:
            recent = [v for t, v in history if now - t <= r["within_s"]]
            hit = len(recent) >= 2 and max(recent) - min(recent) >= r["change"]
        if hit:
            why.append(f"{rule_text(r)} [{r['level']}]")
            if RANK[r["level"]] > RANK[level]:
                level = r["level"]
    return level, why


def iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t))


def hms(t: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(t))


def ago(s) -> str:
    if s is None: return "never"
    if s < 90: return f"{s:.0f} s ago"
    if s < 5400: return f"{s / 60:.0f} min ago"
    return f"{s / 3600:.1f} h ago"


def item(group, name, level, text, details=None, age_s=None):
    return dict(group=group, name=name, level=level, text=text, details=details or {}, age_s=age_s)


# ---------------------------------------------------------------- what is gathered
class Monitor:
    def __init__(self, a, clock=time.time, fetch=None, procs=None):
        self.a, self.clock = a, clock
        self.fetch = fetch or self._fetch                # (url, timeout) -> dict; raises on failure
        self.procs = procs or fsudaq_processes
        self.lock = threading.Lock()
        self.limits = load_limits(a.limits)
        self.hist = collections.defaultdict(collections.deque)   # channel id -> (t, value), the last hour or more
        self.plc = self.plc_err = None; self.plc_t = None          # GET /status of the relay
        self.hb = None                                             # (heartbeat, when it last changed)
        self.online = self.online_err = None; self.online_t = None; self.online_next = 0
        self.prev_bytes = None                                     # (time, run bytes) for the MB/s
        self.snapshot = dict(items=[], updated=None)

    @staticmethod
    def _fetch(url, timeout):
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())

    # -------- sources
    def poll_plc(self, now):
        try:
            d = self.fetch(self.a.relay, 3)
            self.plc, self.plc_err, self.plc_t = d, None, now
            hb = (d.get("plc") or {}).get("heartbeat")
            if hb is not None and (self.hb is None or self.hb[0] != hb):
                self.hb = (hb, now)
        except urllib.error.HTTPError as ex:       # pion-ros answers, but not with a status
            self.plc_err = (f"pion-ros answers but has no GET /status (HTTP {ex.code}): restart pion-ros with the newer plc_command"
                            if ex.code in (404, 501) else f"HTTP {ex.code}: {ex.reason}")
        except Exception as ex:                    # anything: the page says what
            self.plc_err = str(getattr(ex, "reason", ex))

    def poll_online(self, now):
        if now < self.online_next:
            return
        self.online_next = now + self.a.online_interval
        try:
            self.online, self.online_err, self.online_t = self.fetch(self.a.online, 4), None, now
        except Exception as ex:
            self.online, self.online_err = None, str(getattr(ex, "reason", ex))

    def step(self):
        now = self.clock()
        self.poll_plc(now)
        self.poll_online(now)
        items = self.build(now)
        with self.lock:
            self.snapshot = dict(items=items, updated=now, modes=self.limits.get("modes", {}),
                                 summary=summarize(items))

    # -------- the items
    def build(self, now):
        L = self.limits
        out = []
        procs = self.procs()
        fsudaq_dir = procs[0]["cwd"] if procs else self.a.fsudaq_dir
        data_path = read_data_path(fsudaq_dir)
        folder = newest_run_folder(data_path) if data_path else None
        rf, rf_err = read_run_file(folder)
        running = bool(rf) and rf.get("stop") is None
        out += self.daq_items(now, procs, fsudaq_dir, data_path, folder, rf, rf_err, running)
        out += self.online_items(now, bool(procs) and running)
        out += self.led_items(now, rf)
        out += self.plc_items(now)
        out += self.channel_items(now)
        return out

    def daq_items(self, now, procs, fsudaq_dir, data_path, folder, rf, rf_err, running):
        G, out = "DAQ", []
        if procs:
            p = procs[0]
            out.append(item(G, "FSUDAQ", "ok", f"running from {p['cwd']} (since {hms(p['started'])})", dict(processes=procs)))
        else:
            out.append(item(G, "FSUDAQ", "idle", "not running: no data are being taken", dict(settings_from=fsudaq_dir)))
        name = os.path.basename(folder) if folder else None
        if folder is None:
            out.append(item(G, "Run", "nodata", f"no run folder in {data_path or '(no data path)'}"))
        elif rf is None:
            out.append(item(G, "Run", "info", f"{name}: {rf_err}", dict(folder=folder)))
        else:
            st, start = rf.get("stop"), (rf.get("start") or {}).get("time", "")
            det = dict(folder=folder, start=start, start_comment=(rf.get("start") or {}).get("comment"), stop=st,
                       run_control=rf.get("run_control"), fsudaq=rf.get("fsudaq"))
            if st is None and procs:
                try:
                    since = time.mktime(time.strptime(start[:19], "%Y-%m-%dT%H:%M:%S"))
                    dur = f" for {(now - since) / 60:.0f} min"
                except ValueError:
                    dur = ""
                out.append(item(G, "Run", "ok", f"run {rf.get('run')} ({name}) running{dur}, since {start[11:19]}", det))
            elif st is None:
                out.append(item(G, "Run", "bad", f"run {rf.get('run')} ({name}) has no stop: FSUDAQ ended during the run", det))
            elif st.get("problems"):
                out.append(item(G, "Run", "bad", f"run {rf.get('run')} stopped {st.get('time', '')[11:19]} with problems: "
                                + "; ".join(st["problems"]), det))
            else:
                out.append(item(G, "Run", "info", f"last run {rf.get('run')} ({name}) stopped {st.get('time', '')[11:19]} "
                                f"after {st.get('run_ms', 0) / 60000:.1f} min", det))
            out.append(clock_item(G, rf))
        out += self.board_items(rf, running)
        if data_path:
            try:
                du = shutil.disk_usage(data_path)
                gb = du.free / 1e9
                d = self.limits.get("daq", {})
                lvl = "bad" if gb < d.get("disk_bad_gb", 50) else "warn" if gb < d.get("disk_warn_gb", 200) else "ok"
                it = item(G, "Disk", lvl, f"{gb:,.0f} GB free of {du.total / 1e9:,.0f} GB ({data_path})",
                          dict(warn_below_gb=d.get("disk_warn_gb"), bad_below_gb=d.get("disk_bad_gb")))
                # the page draws a bar: used share, and where the warn and bad levels start
                it["bar"] = dict(used=1 - du.free / du.total, marks=[1 - d.get("disk_warn_gb", 200) * 1e9 / du.total,
                                                                      1 - d.get("disk_bad_gb", 50) * 1e9 / du.total])
                out.append(it)
            except OSError as ex:
                out.append(item(G, "Disk", "bad", f"{data_path}: {ex}"))
        daq = (self.online or {}).get("daq") if running else None
        if daq:
            rb, rate = daq.get("run_bytes"), daq.get("total_rate")
            mbps = None
            if self.prev_bytes and rb is not None and now > self.prev_bytes[0] and rb >= self.prev_bytes[1]:
                mbps = (rb - self.prev_bytes[1]) / (now - self.prev_bytes[0]) / 1e6
            self.prev_bytes = (now, rb) if rb is not None else None
            out.append(item(G, "Data rate", "info", (f"{rate:,.0f} hits/s" if rate is not None else "hit rate not known yet")
                            + (f", {mbps:.1f} MB/s" if mbps is not None else "") + f", {(rb or 0) / 1e9:.2f} GB this run",
                            dict(run_bytes=rb, total_rate=rate, files=daq.get("files"))))
        return out

    def board_items(self, rf, running):
        """One dot per board: writing (from the online analysis while a run goes) and its clock (run file)."""
        G, out = "Boards", []
        listed = [b.get("serial") for b in (rf or {}).get("boards") or []]
        live = {b["serial"]: b for b in ((self.online or {}).get("boards") or [])} if running else {}
        ck = (rf or {}).get("clock") or {}
        start, stop = ck.get("start") or {}, ((rf or {}).get("stop") or {}).get("clock") or {}
        for sn in listed + [s for s in live if s not in listed]:
            name = f"Digi-{sn}"
            info = next((b for b in (rf or {}).get("boards") or [] if b.get("serial") == sn), {})
            what = " ".join(str(x) for x in (info.get("model"), info.get("dpp"), f"{info.get('channels')} ch" if info.get("channels") else None) if x)
            clock_bad = name in (stop.get("lost_since_last_check") or []) or name in (start.get("unlocked") or []) + (stop.get("unlocked") or [])
            clock_warn = name in (start.get("lost_since_last_check") or [])
            clock = ("clock lost during the run" if name in (stop.get("lost_since_last_check") or []) else
                     "clock not locked" if clock_bad else "clock lost before the run" if clock_warn else "")
            b = live.get(sn)
            if b is not None:
                silent = b.get("quiet_alarm") or not b.get("has_data")
                lvl = "bad" if clock_bad else "warn" if (silent or clock_warn) else "ok"
                txt = ("NOT WRITING" + (f" for {b['quiet_s']:.0f} s" if b.get("quiet_s") else "") if silent else
                       f"writing · {(b.get('bytes') or 0) / 1e6:,.0f} MB · newest {b.get('newest_s') or 0:.1f} s") + (f" · {clock}" if clock else "")
                det = dict(b, model=what)
            else:
                lvl = "bad" if clock_bad else "warn" if clock_warn else "idle"
                txt = (what or "in the last run") + (f" · {clock}" if clock else "")
                det = dict(info, clock_start=start, clock_stop=stop)
                det.pop("registers", None)
            out.append(item(G, name, lvl, txt, det))
        return out

    def online_items(self, now, taking):
        G, out = "Online analysis", []
        o = self.online
        if o is None:
            lvl = "warn" if taking else "info"
            out.append(item(G, "Dashboard", lvl, f"not answering ({self.online_err})" + (" while a run goes" if taking else ""),
                            dict(url=self.a.online)))
            return out
        health = o.get("health") or []
        lag = (o.get("daq") or {}).get("lag_s")
        lim = self.limits.get("daq", {}).get("analysis_lag_warn_s", 30)
        lvl = "bad" if health else "warn" if (o.get("status", "").startswith("falling behind") or (lag or 0) > lim) else "ok"
        txt = o.get("status", "?") + (f" · {lag:.0f} s of data behind" if lag else "") + ("; " + "; ".join(health) if health else "")
        out.append(item(G, "Dashboard", lvl, txt, dict(folder=o.get("folder"), workers=o.get("workers"), late_hits=o.get("late"),
                                                      health=health), now - self.online_t))
        if o.get("warnings"):
            out.append(item(G, "Setup", "warn", f"{len(o['warnings'])} placeholder(s) in experiment.json",
                            dict(placeholders=o["warnings"])))
        if o.get("problems"):
            out.append(item(G, "Setup problems", "bad", "; ".join(o["problems"])))
        return out

    def led_items(self, now, rf):
        G, out = "LED trigger", []
        led = (rf or {}).get("led")
        relay_up = self.plc_err is None and self.plc is not None
        if led is None:
            out.append(item(G, "LED", "nodata", "no LED record in the last run file"))
        elif not led.get("enabled"):
            out.append(item(G, "LED", "info", "disabled in the last run", led))
        else:
            ev = led.get("events") or []
            failed = [e for e in ev if e.get("ok") is False]
            last = ev[-1] if ev else None
            lvl = "bad" if failed else "ok"
            txt = f"on {led.get('delay_s')} s after the start for {led.get('hold_s')} s"
            if last:
                txt += f"; last: {last.get('what')} {str(last.get('sent', ''))[11:19]} " + (
                    "confirmed" if last.get("ok") else "waiting" if last.get("ok") is None else f"FAILED: {last.get('error')}")
            out.append(item(G, "LED", lvl, txt, led))
        enabled = bool(led and led.get("enabled"))
        out.append(item(G, "Relay", "ok" if relay_up else ("bad" if enabled else "info"),
                        f"{self.a.relay} answers" if relay_up else "no status from pion-ros (see ROS · pion-ros)", dict(url=self.a.relay)))
        return out

    def plc_items(self, now):
        out = []
        d = self.plc
        if d is None:
            out.append(item("PLC", "PLC status", "bad", "no status from pion-ros (see ROS · pion-ros)", dict(url=self.a.relay)))
            out.append(item("ROS", "pion-ros", "bad", f"no answer from {self.a.relay}: {self.plc_err}"))
            return out
        stale = self.limits.get("daq", {}).get("plc_stale_s", 10)
        plc, age = d.get("plc"), d.get("age_s")
        since = now - self.plc_t
        if plc is None:
            out.append(item("PLC", "PLC status", "bad", "pion-ros has no PLC status (plc_status not publishing)", d))
        else:
            hb_age = now - self.hb[1] if self.hb else None
            if age is not None and age > stale:
                lvl, txt = "bad", f"the last PLC status is {age:.0f} s old"
            elif hb_age is not None and hb_age > stale:
                lvl, txt = "bad", f"heartbeat stuck at {self.hb[0]} for {hb_age:.0f} s"
            else:
                lvl, txt = "ok", f"heartbeat {plc.get('heartbeat')}, status {age:.1f} s old"
            if self.plc_err:
                lvl, txt = "bad", f"no answer from pion-ros for {since:.0f} s (see ROS · pion-ros); " + txt
            out.append(item("PLC", "PLC status", lvl, txt, plc, since))
            outs = ", ".join(f"DQ{i + 1} {'ON' if plc.get(f'dq{i}') else 'off'}" for i in range(4))
            out.append(item("PLC", "Outputs", "info", outs + " (DQ1 = LED)", {f"DQ{i + 1}": plc.get(f"dq{i}") for i in range(4)}))
        nodes = d.get("nodes") or []
        up = lambda n: any(x == n or x.startswith(n) for x in nodes)
        out.append(item("ROS", "pion-ros", "bad" if self.plc_err else "ok",
                        ("no answer now; " if self.plc_err else "") + f"{len(nodes)} nodes: {', '.join(nodes)}", dict(nodes=nodes)))
        out.append(item("ROS", "PLC status node", "ok" if up("plc_status") else "bad", "plc_status " + ("up" if up("plc_status") else "NOT running")))
        out.append(item("ROS", "Rosbag recording", "ok" if up("rosbag2_recorder") else "bad",
                        "rosbag2_recorder " + ("up" if up("rosbag2_recorder") else "NOT running: nothing is recorded")))
        return out

    def channel_items(self, now):
        out = []
        L = self.limits
        modes = L.get("modes", {})
        plc = (self.plc or {}).get("plc") if self.plc_err is None else None
        stale = (self.plc or {}).get("age_s") is None or (self.plc or {}).get("age_s") > L.get("daq", {}).get("plc_stale_s", 10)
        used = set()
        for c in L.get("channels", []):
            src = (c.get("source") or "").removeprefix("plc.")
            if not src:
                out.append(item(c.get("group") or "Inputs", c["name"], "nodata", "not assigned to a PLC input (limits page)",
                                dict(rules=[rule_text(r) + f" [{r['level']}]" for r in c.get("rules", [])])))
                continue
            used.add(src)
            raw = None if plc is None or stale else plc.get(src)
            if c.get("connected") is False:   # marked not plugged in: what it reads means nothing
                out.append(item(c.get("group") or "Inputs", c["name"], "warn",
                                "not connected" + (f"; reads {raw:.4g}" if raw is not None else ""),
                                dict(source=c.get("source"), raw=raw, connected=False, hint="tick \"connected\" on the limits page once it is plugged in")))
                continue
            value = None
            if raw is not None and not (src.startswith("tc") and raw >= TC_OPEN):
                value = raw * c.get("scale", 1) + c.get("offset", 0)
                h = self.hist[c["id"]]
                h.append((now, value))
                keep = max([r.get("within_s", 0) for r in c.get("rules", [])] + [3600]) + 60
                while h and now - h[0][0] > keep:
                    h.popleft()
            h = list(self.hist[c["id"]])
            level, why = evaluate(c, value, h, modes, now)
            fmt = lambda v: f"{v:.{int(c.get('digits', 2))}f} {c.get('unit', '')}".strip()
            if value is None:
                txt = "no value: " + ("PLC status missing or stale" if raw is None else "input not connected")
            else:
                txt = fmt(value) + ("  —  " + "; ".join(why) if why else "")
            span = (now - h[0][0]) if h else 0
            rng = (min(v for _, v in h), max(v for _, v in h)) if h else None
            out.append(item(c.get("group") or "Inputs", c["name"], level, txt,
                            dict(source=c.get("source"), raw=raw, scale=c.get("scale"), offset=c.get("offset"), range=c.get("range"), note=c.get("note"),
                                 rules=[rule_text(r) + f" [{r['level']}]" for r in c.get("rules", [])],
                                 history=f"{len(h)} values over {span / 60:.0f} min" + (f", {fmt(rng[0])} to {fmt(rng[1])}" if rng else ""),
                                 modes={k: m.get("value") for k, m in modes.items()})))
        for src in PLC_INPUTS:
            if src in used or f"plc.{src}" in L.get("unused", []):
                continue
            raw = None if plc is None else plc.get(src)
            if raw is None:
                continue
            open_tc = src.startswith("tc") and raw >= TC_OPEN
            out.append(item("PLC inputs (not named)", src, "info", "not connected" if open_tc else f"{raw:.4g} (raw)", dict(raw=raw)))
        return out

    def state(self):
        with self.lock:
            return dict(self.snapshot, server_time=self.clock(), relay=self.a.relay)


def clock_item(G, rf):
    ck = rf.get("clock")
    if not ck:
        return item(G, "Board clocks", "nodata", "not recorded (FSUDAQ before the clock check)")
    start, stop = ck.get("start") or {}, (rf.get("stop") or {}).get("clock") or {}
    lost_in, lost_before = stop.get("lost_since_last_check") or [], start.get("lost_since_last_check") or []
    unlocked = sorted(set((start.get("unlocked") or []) + (stop.get("unlocked") or [])))
    if lost_in or unlocked:
        lvl, txt = "bad", "; ".join(([f"lost during the run: {', '.join(lost_in)}"] if lost_in else []) +
                                    ([f"not locked: {', '.join(unlocked)}"] if unlocked else []))
    elif lost_before:
        lvl, txt = "warn", f"lost before the run, locked again: {', '.join(lost_before)} (16 ns phase may have changed)"
    else:
        lvl, txt = "ok", "all locked at the start" + (" and the stop" if stop else "")
    return item(G, "Board clocks", lvl, txt, dict(start=start, stop=stop))


def summarize(items):
    if not items:
        return dict(level="nodata", text="nothing gathered yet", counts={})
    worst = max(items, key=lambda i: RANK[i["level"]])
    counts = collections.Counter(i["level"] for i in items)
    bad = [i for i in items if RANK[i["level"]] >= RANK["warn"]]
    if not bad:
        return dict(level="ok", text="All well", counts=counts)
    bad.sort(key=lambda i: -RANK[i["level"]])
    return dict(level=worst["level"], counts=counts,
                text=f"{bad[0]['group']} · {bad[0]['name']}: {bad[0]['text']}" + (f"  (+{len(bad) - 1} more)" if len(bad) > 1 else ""))


# ---------------------------------------------------------------- FSUDAQ's files
def fsudaq_processes():
    out = []
    for d in glob.glob("/proc/[0-9]*"):
        try:
            with open(d + "/comm") as f:
                if f.read().strip() != "FSUDAQ_Qt6":
                    continue
            out.append(dict(pid=int(os.path.basename(d)), cwd=os.readlink(d + "/cwd"), started=os.stat(d).st_mtime))
        except OSError:
            pass
    return sorted(out, key=lambda p: p["started"])


def read_data_path(fsudaq_dir):
    try:
        with open(os.path.join(fsudaq_dir, "programSettings.txt")) as f:
            p = f.readline().strip()
        return p if p and os.path.isdir(p) else None
    except OSError:
        return None


def newest_run_folder(data_path):
    dirs = [d for d in glob.glob(os.path.join(data_path, "*")) if os.path.isdir(d)]
    return max(dirs, key=os.path.getmtime) if dirs else None


def read_run_file(folder):
    if not folder:
        return None, "no run folder"
    path = os.path.join(folder, os.path.basename(os.path.normpath(folder)) + "_run.json")
    try:
        with open(path) as f:
            return json.load(f), None
    except FileNotFoundError:
        return None, "no run file (a run from before FSUDAQ wrote one)"
    except (OSError, ValueError) as ex:
        return None, f"cannot read {os.path.basename(path)}: {ex}"


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    mon: Monitor = None

    def log_message(self, *args):
        pass

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, allow_nan=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers(); self.wfile.write(data)

    def page(self, name):
        try:
            with open(os.path.join(HERE, name), "rb") as f:     # re-read: a page update needs no restart
                self.send(200, f.read(), "text/html; charset=utf-8")
        except OSError:
            self.send(404, {"error": f"{name} missing"})

    def do_GET(self):
        p = self.path.split("?")[0]
        if p in ("/", "/index.html"):
            return self.page("status.html")
        if p == "/limits":
            return self.page("limits.html")
        if p == "/api/status":
            return self.send(200, self.mon.state())
        if p == "/api/limits":
            m = self.mon
            plc = (m.plc or {}).get("plc") or {}
            return self.send(200, dict(limits=m.limits, inputs={k: plc.get(k) for k in PLC_INPUTS}, levels=list(RULE_LEVELS),
                                       sources=[f"plc.{s}" for s in PLC_INPUTS]))
        self.send(404, {"error": "not here"})

    def do_POST(self):
        if self.path.split("?")[0] != "/api/limits":
            return self.send(404, {"error": "not here"})
        if self.client_address[0] not in ("127.0.0.1", "::1"):      # through the ssh tunnel, or on odin
            return self.send(403, {"error": "limits are changed from odin or through the ssh tunnel only"})
        try:
            L = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 1_000_000)))
        except ValueError:
            return self.send(400, {"errors": ["not JSON"]})
        errors = validate_limits(L)
        if errors:
            return self.send(400, {"errors": errors})
        m = self.mon
        save_limits(m.a.limits, L, self.headers.get("X-Who") or "the limits page", m.clock)
        with m.lock:
            old = {c["id"]: c for c in m.limits.get("channels", [])}
            for c in L.get("channels", []):     # a channel whose source or scaling changed starts its history again
                o = old.get(c["id"])
                if not o or any(o.get(k) != c.get(k) for k in ("source", "scale", "offset")):
                    m.hist.pop(c["id"], None)
            m.limits = L
        m.step()
        self.send(200, {"saved": iso(m.clock())})


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--port", type=int, default=8060)
    p.add_argument("--interval", type=float, default=2.0, help="seconds between gatherings")
    p.add_argument("--online", default="http://localhost:8050/state", help="the online dashboard's state")
    p.add_argument("--online-interval", type=float, default=5.0, help="seconds between reads of the online state (it is large)")
    p.add_argument("--relay", default="http://129.129.141.236:8770/status", help="pion-ros plc_command relay's GET /status")
    p.add_argument("--fsudaq-dir", default=os.path.dirname(HERE), help="FSUDAQ folder (programSettings.txt) when FSUDAQ is not running")
    p.add_argument("--limits", default=os.path.join(HERE, "limits.json"))
    return p.parse_args(argv)


def main(argv=None):
    a = parse_args(argv)
    mon = Monitor(a)
    if not os.path.exists(a.limits):
        save_limits(a.limits, mon.limits, "first start (defaults)")
    Handler.mon = mon
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)   # local only: view it through the ssh tunnel
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"experiment status on http://localhost:{a.port}/ (limits: /limits)", flush=True)
    while True:
        t0 = time.time()
        try:
            mon.step()
        except Exception as ex:          # one bad gathering must not end the page
            print(f"{time.strftime('%H:%M:%S')} gathering failed: {ex!r}", flush=True)
        time.sleep(max(0.2, a.interval - (time.time() - t0)))


if __name__ == "__main__":
    main()
