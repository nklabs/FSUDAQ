#!/usr/bin/env python3
"""The experiment status server (status/status_server.py) against fake sources and a fake clock:
the rules (isobutane 5 / 10 / 100 %LEL, Cherenkov pressure over 2 psi, and in "full of isobutane" mode
outside 0.3-2 psi or a change of 0.05 psi within an hour), the PLC heartbeat and staleness, the run
file's states, the limits checks and their log, and the HTTP API.

    python3 tests/test_status.py
"""
import copy
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "status"))
import status_server as ss            # noqa: E402

N_FAIL = 0
NAME = {c["id"]: c["name"] for c in ss.DEFAULT_LIMITS["channels"]}


def check(cond, msg):
    global N_FAIL
    if not cond:
        N_FAIL += 1
        print(f"   FAIL  {msg}")
    return bool(cond)


def ok(msg):
    print(f"   ok    {msg}")


class Clock:
    def __init__(self): self.t = 1_800_000_000.0
    def __call__(self): return self.t


class Sources:
    """What the relay and the online dashboard answer; None = no answer."""
    def __init__(self):
        self.plc = dict(source="SLOWTRONICS", heartbeat=1, **{f"ai{i}": 0.0 for i in range(8)},
                        **{f"tc{i}": 3276.7 for i in range(8)}, **{f"dq{i}": False for i in range(4)})
        self.age = 0.5
        self.nodes = ["plc_command", "plc_status", "rosbag2_recorder"]
        self.relay_up = True
        self.online = None
        self.counting = False                  # the heartbeat goes up by itself (a scenario that does not test it)

    def fetch(self, url, timeout):
        if "8770" in url or "relay" in url:
            if not self.relay_up:
                raise urllib.error.URLError("Connection refused")
            if self.counting:          # like the PLC: the heartbeat goes up with every status
                self.plc["heartbeat"] += 1
            return dict(time=0, plc=copy.deepcopy(self.plc), age_s=self.age, nodes=list(self.nodes))
        if self.online is None:
            raise urllib.error.URLError("Connection refused")
        return self.online


def make(tmp, clock, src, procs=None, limits=None):
    lp = os.path.join(tmp, "limits.json")
    if limits is not None:
        with open(lp, "w") as f:
            json.dump(limits, f)
    a = ss.parse_args(["--limits", lp, "--fsudaq-dir", os.path.join(tmp, "fsudaq"), "--relay", "http://relay:8770/status"])
    return ss.Monitor(a, clock=clock, fetch=src.fetch, procs=lambda: procs() if procs else [])


def find(m, group, name):
    for it in m.state()["items"]:
        if it["group"] == group and it["name"] == name:
            return it
    return None


def assign(L, **src):
    for c in L["channels"]:
        if c["id"] in src:
            c["source"] = src[c["id"]]
    return L


def scenario_rules(tmp):
    print("== the rules: isobutane 5 / 10 / 100 %LEL; pressure over 2 psi always; in \"full of isobutane\" outside 0.3-2 psi or a change of 0.05 psi within an hour")
    clock, src = Clock(), Sources()
    L = copy.deepcopy(ss.DEFAULT_LIMITS)               # the PLC as labelled: pressure ai0, gas sensors ai1 and ai2
    L = assign(L, isobutane_2=None)
    m = make(tmp, clock, src, limits=L)
    good = True
    for v, want in ((0, "ok"), (4.9, "ok"), (5, "warn"), (9.99, "warn"), (10, "bad"), (99, "bad"), (100, "critical"), (250, "critical")):
        src.plc["ai1"] = v; clock.t += 2; m.step()
        it = find(m, "Cherenkov", NAME["isobutane_1"])
        good &= check(it["level"] == want, f"isobutane {v} %LEL: {it['level']}, expected {want} ({it['text']})")
    src.plc["ai1"] = 0
    # normal mode: only over 2 psi counts
    for v, want in ((0.1, "ok"), (1.0, "ok"), (2.0, "ok"), (2.01, "warn")):
        src.plc["ai0"] = v; clock.t += 2; m.step()
        it = find(m, "Cherenkov", NAME["cherenkov_pressure"])
        good &= check(it["level"] == want, f"normal mode, {v} psi: {it['level']}, expected {want}")
    # full of isobutane: below 0.3 too
    m.limits["modes"]["cherenkov"]["value"] = "full of isobutane"
    m.hist.clear()
    for v, want in ((0.29, "warn"), (0.3, "ok"), (2.0, "ok"), (2.5, "warn")):
        m.hist.clear()
        src.plc["ai0"] = v; clock.t += 2; m.step()
        it = find(m, "Cherenkov", NAME["cherenkov_pressure"])
        good &= check(it["level"] == want, f"full mode, {v} psi: {it['level']}, expected {want} ({it['text']})")
    # a slow drift: 0.04 psi in the hour is fine, 0.05 within the hour is not; a change older than an hour is forgotten
    m.hist.clear()
    src.plc["ai0"] = 1.00
    for k in range(60):            # one hour, a value a minute, 1.00 -> 1.04
        src.plc["ai0"] = 1.00 + 0.04 * k / 59; clock.t += 60; m.step()
    it = find(m, "Cherenkov", NAME["cherenkov_pressure"])
    good &= check(it["level"] == "ok", f"0.04 psi in an hour: {it['level']} ({it['text']})")
    src.plc["ai0"] = 1.051; clock.t += 60; m.step()
    it = find(m, "Cherenkov", NAME["cherenkov_pressure"])
    good &= check(it["level"] == "warn" and "change" in it["text"], f"0.05 psi within the hour: {it['level']} ({it['text']})")
    for k in range(65):            # steady at 1.051 for over an hour: the change has left the window
        clock.t += 60; m.step()
    it = find(m, "Cherenkov", NAME["cherenkov_pressure"])
    good &= check(it["level"] == "ok", f"steady for an hour after the change: {it['level']} ({it['text']})")
    # back to normal mode: the change rule is off
    src.plc["ai0"] = 1.2; clock.t += 60; m.step()
    m.limits["modes"]["cherenkov"]["value"] = "normal"; clock.t += 2; m.step()
    it = find(m, "Cherenkov", NAME["cherenkov_pressure"])
    good &= check(it["level"] == "ok", f"normal mode after a change: {it['level']}")
    # not assigned, and the unnamed inputs
    it = find(m, "Cherenkov", NAME["isobutane_2"])
    good &= check(it["level"] == "nodata" and "not assigned" in it["text"], f"unassigned: {it}")
    good &= check(find(m, "Cherenkov", NAME["tc7"])["level"] == "nodata", "a connected thermocouple reading 3276.7 (open) judged")
    src.plc["tc6"] = 20.6; src.plc["tc2"] = 305.4; src.plc["tc0"] = 20.0; clock.t += 2; m.step()
    good &= check(find(m, "Cherenkov", NAME["tc6"])["level"] == "ok" and find(m, "Cherenkov", NAME["tc6"])["text"] == "20.6 °C",
                  f"Cherenkov thermocouple: {find(m, 'Cherenkov', NAME['tc6'])}")
    # the six tracker thermocouples: one line on the Trackers card; marked not connected = blue whatever they read
    it = find(m, "Trackers", "Tracker temperatures")
    good &= check(it["level"] == "notice" and it["text"] == "6 not connected" and find(m, "Tracker temperatures", NAME["tc0"]) is None,
                  f"collapsed tracker temperatures: {it}")
    good &= check(it["details"][NAME["tc2"]].startswith("not connected; reads 305.4") and it["details"][NAME["tc0"]].startswith("not connected"),
                  f"tracker temperature details: {it['details']}")
    L2 = copy.deepcopy(m.limits)    # two plugged in: their span on the line; one above 80 C: red, named first
    for c in L2["channels"]:
        if c["id"] in ("tc0", "tc1"):
            c["connected"] = True
    m.limits = L2
    src.plc["tc1"] = 23.5; clock.t += 2; m.step()
    it = find(m, "Trackers", "Tracker temperatures")
    good &= check(it["level"] == "notice" and it["text"] == "20 to 23.5 °C (2 reading) · 4 not connected", f"two connected (four still blue): {it}")
    src.plc["tc1"] = 85.0; clock.t += 2; m.step()
    it = find(m, "Trackers", "Tracker temperatures")
    good &= check(it["level"] == "bad" and it["text"].startswith(NAME["tc1"] + ": 85.0 °C"), f"a tracker at 85 C: {it}")
    src.plc["tc1"] = 3276.7
    src.plc["tc6"] = 80.0; clock.t += 2; m.step()
    good &= check(find(m, "Cherenkov", NAME["tc6"])["level"] == "ok", f"80.0 C: {find(m, 'Cherenkov', NAME['tc6'])}")
    src.plc["tc6"] = 85.0; clock.t += 2; m.step()
    it = find(m, "Cherenkov", NAME["tc6"])
    good &= check(it["level"] == "bad" and "> 80" in it["text"] and "outside" not in it["text"], f"85 C: {it}")
    src.plc["tc6"] = 20.6
    good &= check(find(m, "Trackers", NAME["trumeter_1"]) is not None, "the currents not on the Trackers card")
    good &= check(find(m, "PLC inputs (not named)", "ai1") is None, "an assigned input also listed as not named")
    good &= check(find(m, "PLC inputs (not named)", "ai3") is None, "an unused input listed")
    good &= check(find(m, "PLC inputs (not named)", "ai2")["text"].endswith("(raw)"), "a free input not listed")
    # outside the sensor's range: a wiring or scaling warning, which never lowers what the rules say
    m.hist.clear(); src.plc["ai0"] = -43.375; clock.t += 2; m.step()
    it = find(m, "Cherenkov", NAME["cherenkov_pressure"])
    good &= check(it["level"] == "warn" and "outside -14.7 to 100" in it["text"], f"pressure -43 psig: {it}")
    src.plc["ai1"] = 140; clock.t += 2; m.step()
    it = find(m, "Cherenkov", NAME["isobutane_1"])
    good &= check(it["level"] == "critical" and "outside" in it["text"], f"gas sensor past full scale: {it}")
    src.plc["ai7"] = 6.0; clock.t += 2; m.step()     # a current (no safety rules) out of its range: to read, not a safety matter
    it = find(m, "Trackers", NAME["trumeter_3"])
    good &= check(it["level"] == "notice" and "outside" in it["text"], f"ESPEON current out of range: {it}")
    if good:
        ok("isobutane at 5 / 10 / 100; pressure > 2 always, < 0.3 and a 0.05 psi change in an hour only when full; the window forgets; "
           "unassigned, unused and open inputs; out of range warns, past full scale stays critical")


def scenario_changes(tmp):
    print("== change over 10 s, 1 min and 10 min, from moving averages: a ramp under noise, and none until the history is long enough")
    clock, src = Clock(), Sources()
    m = make(tmp, clock, src, limits=copy.deepcopy(ss.DEFAULT_LIMITS))
    good, t0 = True, clock.t
    def step(k):                    # 0.01 psig/s, with +-0.05 psig noise that alternates every reading
        src.plc["ai0"] = 1.0 + 0.01 * (clock.t - t0) + (0.05 if k % 2 else -0.05)
        clock.t += 2; m.step()
    for k in range(20):             # 40 s
        step(k)
    ch = find(m, "Cherenkov", NAME["cherenkov_pressure"])["details"]["change"]
    good &= check(ch["10s"] is not None and ch["1m"] is None and ch["10m"] is None, f"after 40 s: {ch}")
    for k in range(20, 330):        # 11 min in all
        step(k)
    it = find(m, "Cherenkov", NAME["cherenkov_pressure"])
    ch = it["details"]["change"]
    for label, want in (("10s", 0.1), ("1m", 0.6), ("10m", 6.0)):
        good &= check(ch[label] is not None and abs(ch[label] - want) < 0.01 * want, f"change over {label}: {ch[label]}, expected {want}")
    good &= check(it["trend"] == [["Δ10s", "+0.100"], ["Δ1m", "+0.600"], ["Δ10m", "+6.000"]], f"trend: {it.get('trend')}")
    good &= check("trend" not in find(m, "Trackers", "Tracker temperatures"), "a trend on the collapsed tracker temperatures")
    if good:
        ok("0.1 / 0.6 / 6.0 psig over 10 s / 1 min / 10 min from a 0.01 psig/s ramp under noise; '–' until each interval is covered")


def scenario_plc(tmp):
    print("== the PLC: heartbeat stuck, status stale, pion-ros not answering, nodes missing")
    clock, src = Clock(), Sources()
    L = assign(copy.deepcopy(ss.DEFAULT_LIMITS), isobutane_1="plc.ai0")
    m = make(tmp, clock, src, limits=L)
    good = True
    for k in range(3):
        src.plc["heartbeat"] += 2; clock.t += 2; m.step()
    good &= check(find(m, "PLC", "PLC status")["level"] == "ok", f"heartbeat counting: {find(m, 'PLC', 'PLC status')}")
    for k in range(7):             # 14 s with the same heartbeat
        clock.t += 2; m.step()
    it = find(m, "PLC", "PLC status")
    good &= check(it["level"] == "bad" and "stuck" in it["text"], f"heartbeat stuck: {it}")   # the safety readings blind: red
    src.plc["heartbeat"] += 1; src.age = 25; clock.t += 2; m.step()
    it = find(m, "PLC", "PLC status")
    good &= check(it["level"] == "bad" and "old" in it["text"], f"status stale: {it}")
    good &= check(find(m, "Cherenkov", NAME["isobutane_1"])["level"] == "nodata", "a stale status still judged")
    src.age = 0.5; src.relay_up = False; clock.t += 2; m.step()
    it = find(m, "PLC", "PLC status")
    good &= check(it["level"] == "bad" and "no answer" in it["text"], f"relay down: {it}")
    good &= check(find(m, "ROS", "pion-ros")["level"] == "problem", "pion-ros not pink with the relay down")
    good &= check(find(m, "Trackers", "LED relay live")["level"] == "info" and find(m, "Trackers", "LED relay live")["text"].startswith("NO"), f"relay live with pion-ros down: {find(m, 'Trackers', 'LED relay live')}")
    good &= check(find(m, "Cherenkov", NAME["isobutane_1"])["level"] == "nodata", "judged without an answer")
    src.relay_up = True; src.nodes = ["plc_command", "plc_status"]; clock.t += 2; m.step()
    good &= check(find(m, "ROS", "Rosbag recording")["level"] == "problem", f"no recorder: {find(m, 'ROS', 'Rosbag recording')}")
    good &= check(find(m, "PLC", "Outputs")["text"].startswith("DQ1 off"), f"outputs: {find(m, 'PLC', 'Outputs')}")
    if good:
        ok("stuck heartbeat, stale status, pion-ros down and a missing recorder are red; nothing judged on old values")


def write_run(folder, run):
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, os.path.basename(folder) + "_run.json"), "w") as f:
        json.dump(run, f)


def scenario_run_file(tmp):
    print("== FSUDAQ and its run file: running, stopped, no stop, problems, clocks, LED")
    clock, src = Clock(), Sources()
    fs = os.path.join(tmp, "fsudaq"); data = os.path.join(tmp, "data")
    os.makedirs(fs); os.makedirs(data)
    with open(os.path.join(fs, "programSettings.txt"), "w") as f:
        f.write(data + "\n")
    procs = [dict(pid=4242, cwd=fs, started=clock.t - 600)]
    src.counting = True
    running = {"on": True}
    m = make(tmp, clock, src, procs=lambda: procs if running["on"] else [])
    good = True
    clock.t += 2; m.step()
    good &= check(find(m, "DAQ", "Run")["level"] == "nodata", f"no run folder: {find(m, 'DAQ', 'Run')}")
    folder = os.path.join(data, "test_047")
    start = ss.iso(clock.t - 120)
    run = {"run": 47, "start": {"time": start + ".000+01:00"}, "stop": None,
           "clock": {"start": {"checked": start, "unlocked": [], "lost_since_last_check": []}},
           "led": {"enabled": True, "delay_s": 5, "hold_s": 10, "events": [
               {"what": "on", "sent": start, "run_ms": 5000, "ok": True, "reply": "DQ1,ON", "switched": "yes"}]},
           "boards": [{"serial": 1443}, {"serial": 1399}]}
    write_run(folder, run)
    src.online = {"status": "running", "health": [], "warnings": [], "problems": [], "folder": folder,
                  "daq": {"lag_s": 1.0, "run_bytes": 1_000_000, "total_rate": 3000.0, "files": 4},
                  "boards": [{"serial": 1443, "has_data": True, "quiet_alarm": False}, {"serial": 1399, "has_data": True, "quiet_alarm": False}]}
    m.online_next = 0; clock.t += 2; m.step()
    it = find(m, "DAQ", "Run")
    good &= check(it["level"] == "ok" and "running for 2 min" in it["text"], f"running: {it}")
    good &= check(find(m, "DAQ", "Board clocks")["level"] == "ok", f"clocks: {find(m, 'DAQ', 'Board clocks')}")
    good &= check(find(m, "DAQ", "Boards")["level"] == "ok" and find(m, "DAQ", "Boards")["text"] == "2 of 2 writing"
                  and find(m, "DAQ", "Digi-1399") is None, f"boards: {find(m, 'DAQ', 'Boards')}")
    good &= check(find(m, "DAQ", "LED trigger") is None and find(m, "Trackers", "LED relay live")["level"] == "ok"
                  and find(m, "Trackers", "LED relay live")["text"].startswith("yes"), f"LED: {find(m, 'Trackers', 'LED relay live')}")
    probs = [i["name"] for i in m.state()["items"] if ss.RANK[i["level"]] >= ss.RANK["problem"] and i["name"] != "Disk"]   # /tmp may be small
    good &= check(not probs, f"all well while running, but: {probs}")
    good &= check(find(m, "Online analysis", "Dashboard")["level"] == "ok", f"online: {find(m, 'Online analysis', 'Dashboard')}")
    src.online["daq"]["run_bytes"] = 21_000_000; m.online_next = 0; clock.t += 5; m.step()
    good &= check("4.0 MB/s" in find(m, "DAQ", "Data rate")["text"], f"data rate: {find(m, 'DAQ', 'Data rate')}")
    # a board falls silent; the analysis falls behind
    src.online["boards"][1]["quiet_alarm"] = True; src.online["status"] = "falling behind: the DAQ part"; m.online_next = 0
    clock.t += 5; m.step()
    it = find(m, "DAQ", "Digi-1399")
    good &= check(it["level"] == "problem" and "NOT WRITING" in it["text"] and find(m, "DAQ", "Digi-1443") is None
                  and find(m, "DAQ", "Boards")["text"] == "1 of 2 writing", f"silent board: {it}")
    good &= check(find(m, "Online analysis", "Dashboard")["level"] == "problem", "falling behind not pink")
    # FSUDAQ gone with no stop
    running["on"] = False; clock.t += 2; m.step()
    good &= check(find(m, "DAQ", "Run")["level"] == "problem" and "no stop" in find(m, "DAQ", "Run")["text"], f"no stop: {find(m, 'DAQ', 'Run')}")
    good &= check(find(m, "DAQ", "FSUDAQ")["level"] == "info", "FSUDAQ not running not grey")
    # stopped with a clock loss and a failed LED off
    running["on"] = True
    run["stop"] = {"time": ss.iso(clock.t) + ".000+01:00", "run_ms": 125000, "problems": ["Digi-1399 lost its clock lock (PLL) during the run"],
                   "clock": {"checked": ss.iso(clock.t), "unlocked": [], "lost_since_last_check": ["Digi-1399"]}}
    run["led"]["events"].append({"what": "off", "sent": ss.iso(clock.t), "run_ms": 15000, "ok": False, "error": "no answer", "switched": "unknown"})
    write_run(folder, run)
    clock.t += 2; m.step()
    good &= check(find(m, "DAQ", "Run")["level"] == "problem" and "1 problem(s)" in find(m, "DAQ", "Run")["text"], f"stop problems: {find(m, 'DAQ', 'Run')}")
    good &= check(find(m, "DAQ", "Board clocks")["level"] == "problem" and "Digi-1399" in find(m, "DAQ", "Board clocks")["text"], f"clock lost: {find(m, 'DAQ', 'Board clocks')}")
    good &= check(find(m, "DAQ", "Boards")["level"] == "info" and find(m, "DAQ", "Boards")["text"] == "2 in the last run", f"boards after the stop: {find(m, 'DAQ', 'Boards')}")
    it = find(m, "DAQ", "LED trigger")
    good &= check(it and it["level"] == "problem" and "off at" in it["text"] and "no answer" in it["text"], f"LED off failed: {it}")
    s = m.state()["summary"]
    good &= check(s["level"] == "problem" and s["counts"]["problem"] >= 3 and not any(s["counts"].get(k) for k in ("warn", "bad", "critical")),
                  f"summary: {s}")   # data problems: pink, no safety colour
    # a clean stop
    run["stop"] = {"time": ss.iso(clock.t) + ".000+01:00", "run_ms": 125000, "problems": [],
                   "clock": {"checked": ss.iso(clock.t), "unlocked": [], "lost_since_last_check": []}}
    run["led"]["events"][-1].update(ok=True, error=None)
    write_run(folder, run)
    clock.t += 2; m.step()
    good &= check(find(m, "DAQ", "Run")["level"] == "info" and "stopped" in find(m, "DAQ", "Run")["text"], f"clean stop: {find(m, 'DAQ', 'Run')}")
    good &= check(find(m, "DAQ", "Board clocks")["text"] == "all locked at the start and the stop", f"clocks after: {find(m, 'DAQ', 'Board clocks')}")
    json.dumps(m.state(), allow_nan=False)
    if good:
        ok("running, MB/s, a silent board, falling behind, FSUDAQ gone without a stop, stop problems, a clock loss and a failed LED; a clean stop")


def scenario_limits_api(tmp):
    print("== limits: checked, saved with a log entry, used at once; the HTTP API; edits only from localhost")
    clock, src = Clock(), Sources()
    m = make(tmp, clock, src)
    good = check(m.limits == ss.DEFAULT_LIMITS, "no limits file: not the defaults")
    bad = copy.deepcopy(ss.DEFAULT_LIMITS)
    bad["channels"][0]["source"] = "plc.ai9"
    bad["channels"][1]["id"] = bad["channels"][0]["id"]
    bad["channels"][2]["rules"].append({"change": 0.1, "level": "warn"})
    bad["channels"][2]["rules"].append({"above": 1, "level": "panic"})
    bad["channels"][2]["rules"].append({"above": 1, "level": "warn", "when": {"cherenkov": "half full"}})
    errs = ss.validate_limits(bad)
    good &= check(len(errs) == 5, f"{len(errs)} problems found, expected 5: {errs}")
    ss.Handler.mon = m
    srv = ss.ThreadingHTTPServer(("127.0.0.1", 0), ss.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    def post(L):
        req = urllib.request.Request(base + "/api/limits", data=json.dumps(L).encode(), method="POST", headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
    code, d = post(bad)
    good &= check(code == 400 and len(d["errors"]) == 5 and not os.path.exists(m.a.limits), f"bad limits: {code} {d}")
    L = assign(copy.deepcopy(ss.DEFAULT_LIMITS), isobutane_1="plc.ai0")
    L["modes"]["cherenkov"]["value"] = "full of isobutane"
    src.plc["ai0"] = 12
    code, d = post(L)
    good &= check(code == 200 and "saved" in d, f"good limits: {code} {d}")
    with open(m.a.limits) as f:
        good &= check(json.load(f) == L, "limits.json differs from what was saved")
    with open(os.path.join(tmp, "limits_log.jsonl")) as f:
        log = [json.loads(x) for x in f]
    good &= check(len(log) == 1 and log[0]["limits"] == L and log[0]["time"], f"log: {log}")
    good &= check(find(m, "Cherenkov", NAME["isobutane_1"])["level"] == "bad", "new limits not used at once")
    with urllib.request.urlopen(base + "/api/status") as r:
        st = json.loads(r.read())
    good &= check(st["modes"]["cherenkov"]["value"] == "full of isobutane" and st["summary"]["level"] in ("bad", "critical"), f"status api: {st['summary']}")
    with urllib.request.urlopen(base + "/api/limits") as r:
        li = json.loads(r.read())
    good &= check(li["inputs"]["ai0"] == 12 and "plc.tc7" in li["sources"], f"limits api: {li['inputs']}")
    for page in ("/", "/limits"):
        with urllib.request.urlopen(base + page) as r:
            good &= check(r.status == 200 and b"<title>" in r.read(), f"{page} not served")
    again = make(tmp, Clock(), src)
    good &= check(again.limits == L, "limits not kept for the next start")
    srv.shutdown()
    if good:
        ok("five kinds of wrong limits refused, nothing written; good ones saved, logged, used at once and kept; pages and API served")


def main():
    for sc in (scenario_rules, scenario_changes, scenario_plc, scenario_run_file, scenario_limits_api):
        tmp = tempfile.mkdtemp(prefix="fsudaq_test_status_")
        try:
            sc(tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{'FAILED' if N_FAIL else 'PASSED'}: {N_FAIL} problem(s)")
    return 1 if N_FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
