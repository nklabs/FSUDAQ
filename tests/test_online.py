#!/usr/bin/env python3
"""The online dashboard's file following, against boards simulated the way they write.

A board writes a couple's (channel pair's) events when that couple's buffer is full, so a quiet
channel's hits reach the disk long after a busy channel's newer ones. The simulated boards do the
same (smaller buffers, to keep the files small) on a fake clock, and the dashboard (online/
online_dashboard.py, its Online class, unchanged) steps once a second. What it shows is checked
against the hits that were written:

* the DAQ tab's per-channel counts and pulse heights are exact;
* every hit is either in the track finder or counted as late; none twice;
* with nothing late, the finder's result equals processing all hits at once;
* pauses, roll-over, damaged data, another data format, a run recorded again, a dashboard started
  mid-run, a worker process killed mid-run;
* the analysis falling behind (reading the files, or the DAQ part): no false "stopped", nothing
  late, and every rate shown is the true count of its second;
* the aggregate counter wrapping, gaps in it, a stale copy of an aggregate; a finished run at
  start-up; a pause longer than --hold-s; a board without data; hits exactly at a slice boundary;
* the run summary: written once at the stop, whole, with the totals the page showed; complete=false
  and why (a mid-run start, Clear, the window changed, boards not analysed); no stop word; the
  backfill (between runs only, nice 19, killed by a run start, the same as a live summary; an
  incomplete summary re-read once, the live reading's part kept); and
  GET /summary for anyone, taking only a run folder's bare name.

    python3 tests/test_online.py
"""
import json
import os
import shutil
import signal
import struct
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "online"))
import finder_fast as ff                 # noqa: E402
import online_dashboard as od            # noqa: E402

PS = od.PS
# hermetic: whether FSUDAQ runs is never asked of this machine (its pid.dat, /proc); "running" unless a scenario says
od.Online.fsudaq_probe = staticmethod(lambda: True)
TICK = 2000
N_FAIL = 0


def check(cond, msg):
    global N_FAIL
    if not cond:
        N_FAIL += 1
        print("   FAIL  " + msg, flush=True)
    return cond


def ok(msg):
    print("   ok    " + msg, flush=True)


class Board:
    """A board that writes couple aggregates of `per_agg` events when a couple's buffer is full."""

    def __init__(self, folder, run, serial, n_ch, rates, per_agg=64, roll_bytes=None, opt=2, seed=0, first_index=0, tick=TICK):
        self.folder, self.run, self.serial, self.n_ch = folder, run, serial, n_ch
        self.tick = tick                      # ps per time-tag tick: 2000 on x730, 4000 on x725
        self.rates = rates                    # Hz per channel
        self.per_agg, self.roll_bytes, self.opt = per_agg, roll_bytes, opt
        self.rng = np.random.default_rng(seed)
        self.buf = {c: [] for c in range(n_ch // 2)}
        self.index = first_index; self.f = None; self.written = 0; self.counter = 0
        self.jumps = {}                       # counter -> how far it jumps there (aggregates lost)
        self.last_data = None                 # the bytes of the last aggregate written
        self.truth = []                       # (ts, ch, qlong, qshort) of every hit written
        self.damaged = []                     # hits of aggregates written damaged (not readable)
        self.open_next()

    def path(self, index):
        return os.path.join(self.folder, f"run_{self.run:03d}_{self.serial}_PSD_{self.tick // 1000}_{index:03d}.fsu")

    def open_next(self):
        if self.f:
            self.f.close()
        self.f = open(self.path(self.index), "wb", buffering=0)
        self.index += 1; self.written = 0

    def hits(self, t0, t1):
        """Each channel's hits in [t0, t1) (ps) into its couple's buffer; full buffers are written."""
        for ch, rate in enumerate(self.rates):
            n = self.rng.poisson(rate * (t1 - t0) / PS)
            if n == 0:
                continue
            ts = np.sort(self.rng.integers(t0, t1, n))
            for t in ts:
                tick, rem = divmod(int(t), self.tick)
                fine = rem * 1024 // self.tick
                t = tick * self.tick + fine * self.tick // 1024   # what the decoder makes of it
                ql = int(self.rng.integers(1, 65535)); qs = int(self.rng.integers(0, ql + 1)) & 0x7FFF
                self.buf[ch // 2].append((t, ch, tick, fine, ql, qs))
        for c in self.buf:
            self.buf[c].sort()
            while len(self.buf[c]) >= self.per_agg:
                self.write(c, self.buf[c][:self.per_agg])
                self.buf[c] = self.buf[c][self.per_agg:]

    def flush(self):
        """The board is stopped: every couple's partial buffer is written."""
        for c in self.buf:
            if self.buf[c]:
                self.write(c, self.buf[c]); self.buf[c] = []

    def write(self, c, events, damage=False):
        # a real board interleaves the pair's two channels out of time order (each channel is in order):
        # the even channel's events, then the odd one's
        events = sorted(events, key=lambda e: (e[1] & 1, e[0]))
        words = [0x80000000 | (2 + 3 * len(events)), 0x72000000 if self.opt == 2 else (0x70000000 | (self.opt << 24))]
        for t, ch, tick, fine, ql, qs in events:
            words += [((ch & 1) << 31) | (tick & 0x7FFFFFFF), ((tick >> 31) << 16) | fine, (ql << 16) | qs]
        self.counter += self.jumps.pop(self.counter, 0)
        board = [0xA0000000 | (4 + len(words)), (8 << 27) | (1 << c), self.counter & 0x7FFFFF, 0] + words
        self.counter += 1
        data = struct.pack(f"<{len(board)}I", *board)
        if damage == "zero":                  # written as zeros, its length kept (Digi-62839, 3 Nov 2026): it is lost,
            data = bytes(len(data))           # the aggregates on either side are whole
            self.damaged += [(t, ch, ql, qs) for t, ch, tick, fine, ql, qs in events]
        elif damage:                          # a read that ended short: the header promises more than follows
            data = data[:len(data) // 2]
            self.damaged += [(t, ch, ql, qs) for t, ch, tick, fine, ql, qs in events]
        elif self.opt == 2:
            self.truth += [(t, ch, ql, qs) for t, ch, tick, fine, ql, qs in events]
        else:
            self.damaged += [(t, ch, ql, qs) for t, ch, tick, fine, ql, qs in events]
        self.f.write(data); self.written += len(data)
        self.last_data = data
        if self.roll_bytes and self.written > self.roll_bytes:
            self.open_next()


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make_online(folder, serials, eight, clock, workers=0, extra=None):
    args = ["--run", folder, "--boards", ",".join(map(str, serials)), "--eight-channel-boards", ",".join(map(str, eight)),
            "--workers", str(workers), "--config", "/nonexistent", "--from-start"] + (extra or [])
    return od.Online(od.parse_args(args), clock=clock)


def drive(online, clock, boards, t_from, t_to, step_s=1.0, gen=True, settle=False, each=None):
    """Run the boards and the dashboard from t_from to t_to (s of data time = wall time); each(online)
    after every step."""
    t = t_from
    while t < t_to - 1e-9:
        if gen:
            for b in boards:
                b.hits(int(t * PS), int((t + step_s) * PS))
        t += step_s
        clock.t += step_s
        online.step()
        if settle:
            wait_tasks(online)
        if each:
            each(online)
    return t


def wait_tasks(online, timeout=120):
    t_end = time.time() + timeout
    while online.pending and time.time() < t_end:
        time.sleep(0.05)
        online.step()


def finish(online, clock, boards, notice=True):
    """Stop the boards (they write what they hold), FSUDAQ tells the dashboard (GET /stopped), and the
    dashboard processes everything."""
    for b in boards:
        b.flush()
    if notice:
        online.run_stopped(online.folder)
    for _ in range(20):
        clock.t += 1.0
        online.step()
        check_done_claim(online)
        wait_tasks(online)


def check_done_claim(online):
    """"everything processed" only when nothing is out."""
    if online.status.endswith("everything processed") or online.status.endswith("everything on disk processed"):
        check(not online.pending, f"status '{online.status}' with {len(online.pending)} task(s) out")


def truth(boards, serials):
    """slot -> (ts, qlong) arrays of everything written readable."""
    out = {}
    for b in boards:
        bi = serials.index(b.serial)
        for t, ch, ql, qs in b.truth:
            out.setdefault(bi * 16 + ch, []).append((t, ql))
    return out


def settle_all(online, clock, max_steps=3000):
    """Step until everything on disk is processed (a backlog drains)."""
    for _ in range(max_steps):
        if online.status.endswith("everything processed") and not online.pending:
            return True
        clock.t += 1.0
        online.step()
        check_done_claim(online)
        wait_tasks(online)
    return False


class RateWatch:
    """Records, at each step, the rate the page would show (the newest complete second); checked
    afterwards against the hits written in that second."""

    def __init__(self):
        self.seen = []                        # (second, {slot: count shown})

    def __call__(self, online):
        st = online.state()["daq"]
        if st["total_rate"] is None:
            return
        t0s = online.t_first // PS
        sec = st["rate_second"] + t0s
        self.seen.append((sec, {x["slot"]: x["rate"] for x in st["slots"] if x["rate"] is not None}))

    def check(self, boards, serials, what):
        T = truth(boards, serials)
        per = {sl: np.bincount(np.array([h[0] for h in hits]) // PS) for sl, hits in T.items() if hits}
        bad = 0
        for sec, shown in self.seen:
            for sl, r in shown.items():
                true = int(per[sl][sec]) if sl in per and sec < per[sl].size else 0
                bad += int(r) != true
        return check(bad == 0 and bool(self.seen), f"{what}: {bad} rates shown differ from the hits of their second "
                                             f"({len(self.seen)} steps showed a rate)")


def check_daq(online, boards, serials, what):
    T = truth(boards, serials)
    D = online.daq
    good = True
    for sl in online.slots:
        hits = T.get(sl, [])
        good &= check(int(D.counts[sl]) == len(hits), f"{what}: slot {sl} counts {int(D.counts[sl])}, written {len(hits)}")
        if hits:
            q = np.bincount(np.array([h[1] for h in hits]), minlength=65536)
            good &= check(np.array_equal(D.q[sl], q), f"{what}: slot {sl} pulse heights differ")
    return good


def check_partition(online, boards, what):
    """Every hit written is in the finder, late, or from before a mid-run start: once."""
    total = sum(len(b.truth) for b in boards)
    in_finder, late, early = int(online.owned), int(online.daq.late.sum()), int(online.daq.early.sum())
    check(in_finder + late + early == total, f"{what}: finder {in_finder:,} + late {late:,} + before the start {early:,} != written {total:,}")
    return in_finder, late, total


def offline(online, boards, serials):
    """All hits at once through the same finder: what the dashboard must give when none is late."""
    ts, bd, ch, e = [], [], [], []
    for b in boards:
        bi = serials.index(b.serial)
        for t, c, ql, qs in b.truth:
            ts.append(t); bd.append(bi); ch.append(c); e.append(ql)
    return ff.process_hits(online.setup.tables, online.setup.chans, np.array(ts, np.int64), np.array(bd), np.array(ch),
                           np.array(e, np.float64), online.window_ps)


def same_finder(a, b):
    return [k for k in ff.Result.ARRAYS if not np.array_equal(getattr(a, k), getattr(b, k))]


# ---------------------------------------------------------------------------------------------- scenarios
SERIALS = [15880, 1443, 58918]
EIGHT = [1443]


def boards_for(folder, run=1, slow=None, roll=None, seed=0):
    rates = {15880: [3000.0] * 16, 1443: [2000.0] * 8, 58918: [2500.0] * 16}
    if slow:
        for sn, ch, r in slow:
            rates[sn][ch] = r
    return [Board(folder, run, sn, 8 if sn in EIGHT else 16, rates[sn], roll_bytes=roll, seed=seed + i)
            for i, sn in enumerate(SERIALS)]


def scenario_steady(tmp):
    print("== steady run, every couple busy: DAQ exact, finder = all hits at once")
    folder = os.path.join(tmp, "steady"); os.makedirs(folder)
    clock = Clock(); boards = boards_for(folder)
    online = make_online(folder, SERIALS, EIGHT, clock)
    drive(online, clock, boards, 0, 20)
    finish(online, clock, boards)
    good = check_daq(online, boards, SERIALS, "steady")
    fin, late, tot = check_partition(online, boards, "steady")
    good &= check(late == 0, f"steady: {late} hits late although every couple writes often")
    diff = same_finder(online.acc, offline(online, boards, SERIALS))
    good &= check(not diff, f"steady: finder histograms differ from all hits at once: {diff}")
    good &= check(online.status == "run stopped: everything processed", f"steady: status '{online.status}' at the end")
    view = (0, 65536, 4096, False)
    a, b = online.slots[0], online.slots[1]
    cmp = online.compare([b, a, 99999], view)        # channels side by side: in the order asked, unknown ones left out
    one = online.channel(a, view)
    good &= check([d["slot"] for d in cmp["channels"]] == [b, a], f"steady: compare gave slots {[d['slot'] for d in cmp['channels']]}")
    good &= check(cmp["channels"][1]["q"] == one["q"] and cmp["channels"][1]["iat"] == one["iat"] and "psd" not in cmp["channels"][1],
                  "steady: compare differs from the channel's own histograms")
    good &= check(len(online.compare(list(online.slots), view)["channels"]) == online.COMPARE_MAX, "steady: compare not capped")
    json.dumps(cmp, allow_nan=False)
    online.clear()
    st = online.state()
    good &= check(st["hits"] == 0 and st["bunches"] == 0 and st["late"] == 0, f"steady: after Clear {st['hits']} hits, "
                  f"{st['bunches']} bunches, {st['late']} late")
    if good:
        ok(f"{tot:,} hits: counts and pulse heights exact, all in the finder, finder identical to one pass; Clear clears all")


def scenario_slow(tmp):
    print("== a slow channel (its couple writes every ~20 s): late hits counted, none lost or twice")
    folder = os.path.join(tmp, "slow"); os.makedirs(folder)
    clock = Clock()
    boards = boards_for(folder, slow=[(15880, 4, 1.5), (15880, 5, 1.5), (1443, 6, 0.0), (1443, 7, 3.0)])
    online = make_online(folder, SERIALS, EIGHT, clock, extra=["--hold-s", "8"])
    rates = RateWatch()
    unknown = []
    def each(o):
        rates(o)
        st = o.state()["daq"]
        if st["total_rate"] is not None:
            unknown.append({x["slot"] for x in st["slots"] if x["rate"] is None})
    drive(online, clock, boards, 0, 60, each=each)
    finish(online, clock, boards)
    good = check_daq(online, boards, SERIALS, "slow")
    good &= rates.check(boards, SERIALS, "slow")
    good &= check(any({4, 5} <= u for u in unknown), "slow: the slow couple's rate never shown as not known yet")
    fin, late, tot = check_partition(online, boards, "slow")
    good &= check(late > 0, "slow: no late hits counted although couples were silent longer than --hold-s")
    st = online.state()
    good &= check(st["late"] == late and sum(b["late"] for b in st["boards"]) == late, "slow: late hits not in the page state")
    if good:
        ok(f"{tot:,} hits: DAQ exact; {fin:,} in the finder + {late:,} late (shown on the page) = all")


def scenario_pause(tmp):
    print("== beam pauses longer than --stall-s: no hit counted twice")
    folder = os.path.join(tmp, "pause"); os.makedirs(folder)
    clock = Clock(); boards = boards_for(folder)
    online = make_online(folder, SERIALS, EIGHT, clock)
    t = drive(online, clock, boards, 0, 10)
    t = drive(online, clock, boards, t, t + 12, gen=False)        # 12 s without data
    check(online.mode == "paused" and "pause" in online.status, f"pause: status '{online.status}'")
    t = drive(online, clock, boards, t, t + 10)
    t = drive(online, clock, boards, t, t + 9, gen=False)
    drive(online, clock, boards, t, t + 8)
    finish(online, clock, boards)
    good = check_daq(online, boards, SERIALS, "pause")
    fin, late, tot = check_partition(online, boards, "pause")
    diff = same_finder(online.acc, offline(online, boards, SERIALS)) if late == 0 else []
    good &= check(not diff, f"pause: finder differs from one pass: {diff}")
    if good:
        ok(f"{tot:,} hits over two pauses: DAQ exact, finder {fin:,} + late {late:,} = all" + (", identical to one pass" if not late else ""))


def scenario_damage_rollover(tmp):
    print("== roll-over every ~200 kB, a damaged aggregate, a couple in another extras format")
    folder = os.path.join(tmp, "damage"); os.makedirs(folder)
    clock = Clock(); boards = boards_for(folder, roll=200_000)
    online = make_online(folder, SERIALS, EIGHT, clock)
    t = drive(online, clock, boards, 0, 8)
    b = boards[0]
    ev = [(int(9.5 * PS) + k * 1000, 0, (int(9.5 * PS) + k * 1000) // TICK, 0, 100, 50) for k in range(10)]
    b.write(0, ev, damage=True)                                   # half an aggregate, then good data again
    t = drive(online, clock, boards, t, t + 8)
    # a whole aggregate, then one of zeros, both on disk before the next read (as a run read after it ended): the
    # whole one is read (it was taken for the start of the damage, 3 Nov 2026), the zeros are stepped over
    eg = [(int((t + 1.5) * PS) + k * 1000, 2, (int((t + 1.5) * PS) + k * 1000) // TICK, 0, 100, 50) for k in range(10)]
    ez = [(int((t + 1.6) * PS) + k * 1000, 2, (int((t + 1.6) * PS) + k * 1000) // TICK, 0, 100, 50) for k in range(10)]
    b.write(1, eg); b.write(1, ez, damage="zero")
    t = drive(online, clock, boards, t, t + 8)
    other = Board(folder, 1, 58918, 16, [0.0] * 16, opt=0, seed=99, first_index=900)   # same board, extras option 0
    other.write(3, [(int(12 * PS) + k * 4000, 6, (int(12 * PS) + k * 4000) // TICK, 0, 7, 3) for k in range(5)])
    other.f.close()
    drive(online, clock, boards, t, t + 6)
    finish(online, clock, boards)
    good = check_daq(online, boards, SERIALS, "damage")
    check_partition(online, boards, "damage")
    st = online.state()
    bd = {x["serial"]: x for x in st["boards"]}
    good &= check(bd[15880]["skipped_words"] > 0, "damage: the damaged bytes are not reported")
    good &= check(bd[58918]["other_format"] == 1, f"damage: the other-format couple not reported ({bd[58918]['other_format']})")
    loss = [h for h in st["health"] + st["notes"] if h.startswith("Digi-15880") and "unreadable" in h]
    good &= check(len(loss) == 1 and " % of this board's data" in loss[0] and any("format" in h for h in st["health"]),
                  f"damage: not on the page, or without its size ({loss})")
    good &= check(bd[15880]["gaps"] == 2, f"damage: not 2 aggregates missing, the half one and the zeroed one ({bd[15880]['gaps']})")
    good &= check(bd[15880]["files"] > 3, f"damage: no roll-over ({bd[15880]['files']} files)")
    if good:
        ok(f"{bd[15880]['files']} files for Digi-15880; {bd[15880]['skipped_words'] * 4} damaged bytes and 1 other-format "
           "couple stepped over and reported; DAQ exact for the rest")


def scenario_run_switch(tmp):
    print("== a new run in the same folder (Overwrite), then a new folder (GET /run)")
    folder = os.path.join(tmp, "switch"); os.makedirs(folder)
    clock = Clock(); boards = boards_for(folder)
    online = make_online(folder, SERIALS, EIGHT, clock, extra=[])
    online.a.run = None; online.a.data_path = tmp; online.follow = folder
    t = drive(online, clock, boards, 0, 6)
    for b in boards:
        b.f.close()
    for f in os.listdir(folder):
        if os.path.isfile(os.path.join(folder, f)):               # FSUDAQ removes the old files (only files:
            os.remove(os.path.join(folder, f))                    # the dashboard's <run>_analysis/ stays)
    boards = boards_for(folder, seed=10)
    t = drive(online, clock, boards, t, t + 8)
    finish(online, clock, boards)
    good = check_daq(online, boards, SERIALS, "overwrite")
    check_partition(online, boards, "overwrite")
    folder2 = os.path.join(tmp, "switch2"); os.makedirs(folder2)
    boards2 = boards_for(folder2, run=2, seed=20)
    for t2 in range(4):                                           # a short run, all on disk before the dashboard looks
        for b in boards2:
            b.hits(int((t + t2) * PS), int((t + t2 + 1) * PS))
    for b in boards2:
        b.flush()
    online.run_started(folder2); online.run_stopped(folder2)     # /stopped comes before the next step
    for _ in range(4):
        clock.t += 1; online.step(); wait_tasks(online)
    good &= check(online.status == "run stopped: everything processed", f"new folder: '{online.status}' (the early /stopped was lost)")
    good &= check_daq(online, boards2, SERIALS, "new folder")
    check_partition(online, boards2, "new folder")
    if good:
        ok("each run counted alone: the replaced files, then the new folder")


def scenario_live_start(tmp):
    print("== the dashboard started mid-run (offsets -50 ns): what was on disk is skipped, the rest is exact")
    folder = os.path.join(tmp, "live"); os.makedirs(folder)
    clock = Clock(); boards = boards_for(folder)
    for t in range(10):
        for b in boards:
            b.hits(t * PS, (t + 1) * PS)
    before = {id(b): len(b.truth) for b in boards}
    path = os.path.join(tmp, "live_offsets.csv")              # every channel 50 ns early: a hit skipped at the start
    with open(path, "w") as f:                                 # must not land after it on the finder's clock
        f.write("serial,channel,offset_ns\n")
        for sn in SERIALS:
            for c in range(8 if sn in EIGHT else 16):
                f.write(f"{sn},{c},-50\n")
    online = od.Online(od.parse_args(["--run", folder, "--boards", ",".join(map(str, SERIALS)), "--eight-channel-boards", "1443",
                                      "--workers", "0", "--config", "/nonexistent", "--time-offsets", path]), clock=clock)
    words = od.INDEX_WORDS
    od.INDEX_WORDS = 400_000                  # what is on disk is read over several steps
    try:
        clock.t += 10
        online.step()
        good = check(online.status.startswith("reading what is already on disk"), f"live start: status '{online.status}'")
        drive(online, clock, boards, 10, 20)
        finish(online, clock, boards)
    finally:
        od.INDEX_WORDS = words
    for b in boards:
        b.truth = b.truth[before[id(b)]:]
    good &= check_daq(online, boards, SERIALS, "live start")
    fin, late, tot = check_partition(online, boards, "live start")
    st = online.state()
    good &= check(st["live_start"], "live start: not said on the page")
    good &= check(late == 0 and st["early"] > 0, f"live start: {late} late (expected 0), {st['early']} before the start (expected > 0)")
    if good:
        ok(f"counts exactly the hits written after the start; {st['early']:,} hits from before it counted apart, none late")


def scenario_worker_killed(tmp):
    print("== a worker process killed mid-run: its tasks run again, nothing lost or twice")
    folder = os.path.join(tmp, "kill"); os.makedirs(folder)
    clock = Clock(); boards = boards_for(folder)
    online = make_online(folder, SERIALS, EIGHT, clock, workers=2)
    t = drive(online, clock, boards, 0, 6, settle=True)
    for b in boards:
        b.hits(int(t * PS), int((t + 1) * PS))
    clock.t += 1; t += 1
    online.step()                                                 # tasks out
    procs = list(online.pool._processes.values())
    os.kill(procs[0].pid, signal.SIGKILL)
    t = drive(online, clock, boards, t, t + 6, settle=True)
    finish(online, clock, boards)
    good = check_daq(online, boards, SERIALS, "killed worker")
    fin, late, tot = check_partition(online, boards, "killed worker")
    good &= check(online.lost == 0, f"killed worker: {online.lost} tasks lost")
    online._kill_pool()
    if good:
        ok(f"{tot:,} hits: the dead worker's tasks were run again; DAQ exact, finder + late = all")


def scenario_offsets(tmp):
    print("== cable time offsets up to +-60 ns (beyond the 25 ns window) and a slow channel: finder + late = all")
    folder = os.path.join(tmp, "offsets"); os.makedirs(folder)
    clock = Clock(); boards = boards_for(folder, slow=[(1443, 6, 2.0), (1443, 7, 2.0)])
    rng = np.random.default_rng(5)
    path = os.path.join(tmp, "offsets.csv")
    with open(path, "w") as f:
        f.write("serial,channel,offset_ns\n")
        for sn in SERIALS:
            for c in range(8 if sn in EIGHT else 16):
                f.write(f"{sn},{c},{rng.uniform(-60, 60):.3f}\n")
    online = make_online(folder, SERIALS, EIGHT, clock, extra=["--time-offsets", path, "--hold-s", "8"])
    drive(online, clock, boards, 0, 30)
    finish(online, clock, boards)
    good = check_daq(online, boards, SERIALS, "offsets")
    fin, late, tot = check_partition(online, boards, "offsets")
    if good:
        ok(f"{tot:,} hits: DAQ exact; {fin:,} in the finder + {late:,} late = all")


def scenario_replay(tmp):
    print("== replay of a finished run that rolled over: DAQ exact, finder + late = all, no step fails")
    folder = os.path.join(tmp, "replay"); os.makedirs(folder)
    boards = boards_for(folder, roll=200_000)
    for t in range(20):
        for b in boards:
            b.hits(t * PS, (t + 1) * PS)
    for b in boards:
        b.flush(); b.f.close()
    clock = Clock()
    online = make_online(folder, SERIALS, EIGHT, clock, extra=["--replay", "--replay-speed", "2"])
    errors = 0
    for _ in range(40):
        clock.t += 1
        try:
            online.step()
        except Exception as ex:                       # noqa: BLE001
            errors += 1
    good = check(errors == 0, f"replay: {errors} steps failed")
    good &= check_daq(online, boards, SERIALS, "replay")
    fin, late, tot = check_partition(online, boards, "replay")
    diff = same_finder(online.acc, offline(online, boards, SERIALS))   # files released as the finder passes them
    good &= check(late == 0 and not diff, f"replay: {late} late, finder differs from one pass: {diff}")
    if good:
        ok(f"{tot:,} hits over {sum(len(b.scans) for b in online.boards.values())} files replayed: DAQ exact, all in the finder, "
           "identical to one pass")


def scenario_idle_worker_killed(tmp):
    print("== a worker process killed while idle: the pool is rebuilt, nothing lost")
    folder = os.path.join(tmp, "idlekill"); os.makedirs(folder)
    clock = Clock(); boards = boards_for(folder)
    online = make_online(folder, SERIALS, EIGHT, clock, workers=2)
    t = drive(online, clock, boards, 0, 5, settle=True)
    wait_tasks(online)
    os.kill(list(online.pool._processes.values())[0].pid, signal.SIGKILL)
    time.sleep(0.5)
    t = drive(online, clock, boards, t, t + 5, settle=True)
    finish(online, clock, boards)
    good = check_daq(online, boards, SERIALS, "idle kill")
    check_partition(online, boards, "idle kill")
    good &= check(online.lost == 0 and online.step_error is None, f"idle kill: lost {online.lost}, error {online.step_error}")
    online._kill_pool()
    if good:
        ok("the broken pool was noticed at the next submit and rebuilt; DAQ exact")


def scenario_short_hold(tmp):
    print("== --hold-s shorter than --stall-s: works")
    folder = os.path.join(tmp, "hold"); os.makedirs(folder)
    clock = Clock(); boards = boards_for(folder, slow=[(15880, 0, 1.0)])
    online = make_online(folder, SERIALS, EIGHT, clock, extra=["--hold-s", "2", "--stall-s", "5"])
    drive(online, clock, boards, 0, 15)
    finish(online, clock, boards)
    good = check_daq(online, boards, SERIALS, "short hold")
    check_partition(online, boards, "short hold")
    if good:
        ok("no failure, DAQ exact, finder + late = all")


def scenario_falling_behind(tmp):
    print("== the analysis falls behind (reading the files, then the DAQ part): no false stop, nothing late, rates true")
    for kind in ("index", "ingest"):
        folder = os.path.join(tmp, "behind_" + kind); os.makedirs(folder)
        clock = Clock(); boards = boards_for(folder, seed=30)
        online = make_online(folder, SERIALS, EIGHT, clock)
        saved = od.INDEX_WORDS, od.BATCH_HITS
        if kind == "index":
            od.INDEX_WORDS = 60_000           # 240 kB per file and step: less than is written
        else:
            od.BATCH_HITS = 15_000            # 60 k hits per step to the DAQ part: less than is written
        statuses = set()
        rates = RateWatch()
        def each(o):
            statuses.add(o.status)
            rates(o)
        try:
            drive(online, clock, boards, 0, 15, each=each)
            behind = [x for x in statuses if x.startswith("falling behind")]
            wrong = [x for x in statuses if not (x == "running" or x.startswith("falling behind") or x.startswith("waiting"))]
            good = check(bool(behind), f"{kind}: never said it is falling behind ({statuses})")
            good &= check(not wrong, f"{kind}: said {wrong} while data were written")
            for b in boards:
                b.flush()
            online.run_stopped(online.folder)
            good &= check(settle_all(online, clock), f"{kind}: the backlog did not drain ({online.status})")
        finally:
            od.INDEX_WORDS, od.BATCH_HITS = saved
        good &= check_daq(online, boards, SERIALS, kind)
        fin, late, tot = check_partition(online, boards, kind)
        good &= check(late == 0, f"{kind}: {late} hits late although every couple writes often")
        diff = same_finder(online.acc, offline(online, boards, SERIALS))
        good &= check(not diff, f"{kind}: finder differs from one pass: {diff}")
        good &= rates.check(boards, SERIALS, kind)
        if good:
            ok(f"{kind}: '{behind[0]}' shown; {tot:,} hits exact, none late, finder identical to one pass, "
               f"{len(rates.seen)} rates shown all true")


def scenario_counters(tmp):
    print("== the aggregate counter wraps, jumps (aggregates lost), and an old aggregate is written again")
    folder = os.path.join(tmp, "counters"); os.makedirs(folder)
    clock = Clock(); boards = boards_for(folder, seed=40)
    boards[0].counter = 0x7FFFFF - 300        # Digi-15880 wraps within seconds
    online = make_online(folder, SERIALS, EIGHT, clock)
    t = drive(online, clock, boards, 0, 4)
    b1 = boards[1]                            # Digi-1443: 4 aggregates lost inside a step, 999 at a step's start
    b1.jumps[b1.counter + 40] = 4
    t = drive(online, clock, boards, t, t + 3)
    b1.counter += 999
    b2 = boards[2]                            # Digi-58918: an old aggregate again
    old = b2.last_data
    t = drive(online, clock, boards, t, t + 2)
    b2.f.write(old); b2.written += len(old)
    drive(online, clock, boards, t, t + 4)
    finish(online, clock, boards)
    good = check_daq(online, boards, SERIALS, "counters")
    check_partition(online, boards, "counters")
    st = online.state()
    bd = {x["serial"]: x for x in st["boards"]}
    good &= check(bd[15880]["skipped_words"] == 0 and bd[15880]["gaps"] == 0, f"counters: the wrap read as damage or a gap ({bd[15880]})")
    good &= check(bd[1443]["gaps"] == 1003 and bd[1443]["skipped_words"] == 0, f"counters: Digi-1443 gaps {bd[1443]['gaps']} "
                  f"(expected 1003), skipped {bd[1443]['skipped_words']}")
    good &= check(bd[58918]["skipped_words"] * 4 == len(old) and bd[58918]["gaps"] == 0,
                  f"counters: the old copy: {bd[58918]['skipped_words'] * 4} bytes stepped over, expected {len(old)}; gaps {bd[58918]['gaps']}")
    good &= check(any("1443" in h and "lost before the disk" in h for h in st["health"]),   # 1,003 lost: an alarm, not a note
                  "counters: the lost aggregates are not on the page")
    if good:
        ok("wrap read through; 1003 lost aggregates of Digi-1443 reported, its data exact; the old copy stepped over")


def scenario_finished_at_start(tmp):
    print("== the dashboard started after the run ended: the run is read whole and said to be finished")
    folder = os.path.join(tmp, "finished"); os.makedirs(folder)
    boards = boards_for(folder, seed=50)
    for t in range(10):
        for b in boards:
            b.hits(t * PS, (t + 1) * PS)
    for b in boards:
        b.flush(); b.f.close()
    old = time.time() - 600
    for f in os.listdir(folder):
        os.utime(os.path.join(folder, f), (old, old))
    clock = Clock()
    online = od.Online(od.parse_args(["--run", folder, "--boards", ",".join(map(str, SERIALS)), "--eight-channel-boards", "1443",
                                      "--workers", "0", "--config", "/nonexistent"]), clock=clock)
    for _ in range(5):
        clock.t += 1
        online.step()
        wait_tasks(online)
    st = online.state()
    good = check(st["status"].startswith("last run (already finished") and not st["live_start"], f"finished: status '{st['status']}'")
    # all of it is on disk: every second of every board known (a slow pair once blanked the rate lines of a finished run)
    good &= check(st["daq"]["seconds"] and all(x is not None for s in st["daq"]["seconds"] for x in s["boards"]),
                  "finished: board rates not known in a run that is over")
    good &= check_daq(online, boards, SERIALS, "finished")
    fin, late, tot = check_partition(online, boards, "finished")
    good &= check(late == 0 and not same_finder(online.acc, offline(online, boards, SERIALS)), "finished: finder not as one pass")
    if good:
        ok(f"'{st['status']}', {tot:,} hits, finder identical to one pass")


def scenario_long_pause(tmp):
    print("== a pause longer than --hold-s, no word from FSUDAQ: the finder goes on, what comes after is late")
    folder = os.path.join(tmp, "longpause"); os.makedirs(folder)
    clock = Clock(); boards = boards_for(folder, seed=60)
    online = make_online(folder, SERIALS, EIGHT, clock, extra=["--hold-s", "8"])
    t = drive(online, clock, boards, 0, 8)
    modes = []
    t = drive(online, clock, boards, t, t + 12, gen=False, each=lambda o: modes.append(o.mode))
    # running until --stall-s, then paused (the finder holds) until --hold-s, then quiet (it goes on)
    good = check("".join(m[0] for m in modes) == "r" * 4 + "p" * 3 + "q" * 5, f"long pause: modes {modes}")
    t = drive(online, clock, boards, t, t + 6)
    for b in boards:
        b.flush()
    for _ in range(14):                       # no notice: done after --hold-s
        clock.t += 1; online.step(); wait_tasks(online)
    good &= check_daq(online, boards, SERIALS, "long pause")
    fin, late, tot = check_partition(online, boards, "long pause")
    st = online.state()
    good &= check(late > 0 and st["pause_flush_s"] is not None, f"long pause: {late} late, pause flush {st['pause_flush_s']}")
    good &= check(online.status.endswith("everything on disk processed"), f"long pause: status '{online.status}' at the end")
    if good:
        ok(f"held for 8 s, then went on; {late:,} hits from before the pause came late and are counted so")


def scenario_offsets_dense(tmp):
    print("== offsets up to +-60 ns and one of 1.5 us, every couple busy: nothing late, finder = one pass")
    folder = os.path.join(tmp, "offsets2"); os.makedirs(folder)
    clock = Clock(); boards = boards_for(folder, seed=70)
    rng = np.random.default_rng(7)
    path = os.path.join(tmp, "offsets2.csv")
    with open(path, "w") as f:
        f.write("serial,channel,offset_ns\n")
        for sn in SERIALS:
            for c in range(8 if sn in EIGHT else 16):
                f.write(f"{sn},{c},{1500.0 if (sn, c) == (58918, 9) else rng.uniform(-60, 60):.3f}\n")
    online = make_online(folder, SERIALS, EIGHT, clock, extra=["--time-offsets", path])
    drive(online, clock, boards, 0, 15)
    finish(online, clock, boards)
    good = check_daq(online, boards, SERIALS, "offsets dense")
    fin, late, tot = check_partition(online, boards, "offsets dense")
    good &= check(late == 0, f"offsets dense: {late} late")
    diff = same_finder(online.acc, offline(online, boards, SERIALS))
    good &= check(not diff, f"offsets dense: finder differs from one pass: {diff}")
    if good:
        ok(f"{tot:,} hits, none late, finder identical to one pass")


def scenario_missing_board(tmp):
    print("== a board writes nothing: named while waiting, then on the page; the others are not marked")
    folder = os.path.join(tmp, "missing"); os.makedirs(folder)
    clock = Clock(); boards = [b for b in boards_for(folder, seed=80) if b.serial != 1443]
    os.remove(os.path.join(folder, "run_001_1443_PSD_2_000.fsu"))
    online = make_online(folder, SERIALS, EIGHT, clock, extra=["--hold-s", "5"])
    t = drive(online, clock, boards, 0, 2)
    good = check("Digi-1443" in online.status and "waiting" in online.status, f"missing: status '{online.status}'")
    drive(online, clock, boards, t, t + 8)
    st = online.state()
    bd = {x["serial"]: x for x in st["boards"]}
    good &= check(any(h.startswith("Digi-1443: no data") for h in st["health"]), f"missing: not on the page ({st['health']})")
    good &= check(not bd[1443]["has_data"] and bd[15880]["has_data"] and bd[58918]["has_data"], "missing: has_data wrong")
    finish(online, clock, boards)
    good &= check_daq(online, boards, SERIALS, "missing")
    if good:
        ok("Digi-1443 named while waiting and on the page; the two others shown with data")


def scenario_boundaries(tmp):
    print("== hits exactly at a finder boundary, with a time offset: late and owned decided on the corrected time")
    folder = os.path.join(tmp, "bounds"); os.makedirs(folder)
    path = os.path.join(tmp, "bounds.csv")
    with open(path, "w") as f:
        f.write("serial,channel,offset_ns\n15880,2,8.0\n")       # 8 ns = 4 ticks
    clock = Clock()
    online = make_online(folder, SERIALS, EIGHT, clock, extra=["--time-offsets", path])
    b = Board(folder, 1, 15880, 16, [0.0] * 16)
    T = 5 * PS
    def ev(ch, t):
        return (t, ch, t // TICK, 0, 100, 50)
    # ch2 (offset 8 ns): raw T+6 ns -> T-2 ns (before), raw T+8 ns -> T; ch3: T-2 ns and T
    b.write(1, [ev(2, T + 6000), ev(2, T + 8000), ev(3, T - 2000), ev(3, T)])
    b.f.close()
    fs = od.fsu_fast.FileScan(b.path(0)); fs.update()
    A = fs.arrays()
    ch = online.setup.chans
    r = od.ingest_task(dict(path=b.path(0), aggs=A, bi=0, late_before=T, early_before=None, n_slots=ch.n_slots,
                            board_channels=ch.board_channels))
    late = np.asarray(r["daq"]["late"])
    good = check(late[2] == 1 and late[3] == 1 and late.sum() == 2, f"boundaries: late {late[:4].tolist()}, expected [0, 0, 1, 1]")
    r = od.ingest_task(dict(path=b.path(0), aggs=A, bi=0, late_before=T + PS, early_before=T, n_slots=ch.n_slots,
                            board_channels=ch.board_channels))
    late, early = np.asarray(r["daq"]["late"]), np.asarray(r["daq"]["early"])
    good &= check(early[2] == 1 and early[3] == 1 and late[2] == 1 and late[3] == 1,
                  f"boundaries: early {early[:4].tolist()} late {late[:4].tolist()}, expected 1, 1 each on ch2, ch3")
    files = [(0, b.path(0), {k: A[k] for k in ("ev_start", "nev", "couple", "wpe")})]
    m = online.margin_ps()
    after = od.slice_task(dict(t0=T, t1=T + PS // 1000, margin=m, files=files, ref=None, window=online.window_ps))["owned"]
    before = od.slice_task(dict(t0=T - PS // 1000, t1=T, margin=m, files=files, ref=None, window=online.window_ps))["owned"]
    good &= check(after == 2 and before == 2, f"boundaries: slices own {before} before T and {after} from T, expected 2 and 2")
    if good:
        ok("a hit at the boundary is the later slice's, not late; one tick before it is late; offsets removed first")


def scenario_window(tmp):
    print("== the page's bunch window: used from then on, counts restarted, saved in the config for the next start")
    folder = os.path.join(tmp, "window"); os.makedirs(folder)
    cfg = os.path.join(tmp, "window.json")
    with open(cfg, "w") as f:
        json.dump({"_comment": "kept", "setting": "high"}, f)
    clock = Clock(); boards = boards_for(folder, seed=7)
    online = make_online(folder, SERIALS, EIGHT, clock, extra=["--config", cfg])
    good = check(online.window_ps == 25000 and online.state()["window_src"] == "default", f"window: starts at {online.window_ps} ps")
    for bad in ("0", "1001", "nan", "x", ""):
        try:
            online.set_window(float(bad)); good &= check(False, f"window: {bad!r} accepted")
        except ValueError:
            pass
    good &= check(online.window_ps == 25000, "window: changed by a bad value")
    drive(online, clock, boards, 0, 5)
    good &= check(online.state()["hits"] > 0, "window: no hits before the change")
    note = online.set_window(1000)
    st = online.state()
    good &= check(st["window_ns"] == 1000 and st["hits"] == 0 and st["bunches"] == 0 and "saved" in note,
                  f"window: after the change {st['window_ns']} ns, {st['hits']} hits, {st['bunches']} bunches; '{note}'")
    with open(cfg) as f:
        saved = json.load(f)
    good &= check(saved == {"_comment": "kept", "setting": "high", "window_ns": 1000}, f"window: config now {saved}")
    good &= check("nothing changed" in online.set_window(1000) and online.state()["window_ns"] == 1000, "window: same value again")
    # from here every slice is cut with 1000 ns: the finder equals all later hits at once with 1000 ns
    t_change = online.W
    drive(online, clock, boards, 5, 15)
    finish(online, clock, boards)
    ts, bd, ch, e = [], [], [], []
    for b in boards:
        for t, c, ql, qs in b.truth:
            ts.append(t); bd.append(SERIALS.index(b.serial)); ch.append(c); e.append(ql)
    args = (online.setup.tables, online.setup.chans, np.array(ts, np.int64), np.array(bd), np.array(ch), np.array(e, np.float64))
    end = max(ts) + 1   # the bunches that start from the change on
    wide, narrow = ff.process_hits(*args, 1_000_000, t_change, end), ff.process_hits(*args, 25_000, t_change, end)
    good &= check(not same_finder(online.acc, wide), f"window: finder differs from 1000 ns at once: {same_finder(online.acc, wide)}")
    good &= check(bool(same_finder(wide, narrow)), "window: 1000 ns and 25 ns give the same histograms (the test shows nothing)")
    again = make_online(folder, SERIALS, EIGHT, Clock(), extra=["--config", cfg])
    good &= check(again.window_ps == 1_000_000 and again.state()["window_src"] == "window.json", f"window: next start {again.window_ps} ps")
    cli = make_online(folder, SERIALS, EIGHT, Clock(), extra=["--config", cfg, "--window", "40"])
    good &= check(cli.window_ps == 40_000 and cli.state()["window_src"] == "command line", f"window: --window gives {cli.window_ps} ps")
    if good:
        ok("bad values refused; 1000 ns used for every later slice (differs from 25 ns), counts restarted; kept in the config, --window overrides")


def scenario_run_file(tmp):
    print("== FSUDAQ's run file on the page: start, stop, LED settings and commands; read again only when it changes")
    folder = os.path.join(tmp, "run_007"); os.makedirs(folder)
    path = os.path.join(folder, "run_007_run.json")
    clock = Clock(); boards = boards_for(folder, seed=11)
    online = make_online(folder, SERIALS, EIGHT, clock)
    drive(online, clock, boards, 0, 3)
    st = online.state()
    good = check(st["run_file"] is None, f"run file: {st['run_file']} without a file")
    good &= check(isinstance(st["t0_s"], int), f"run file: t0_s is {st['t0_s']!r}")
    led = {"enabled": True, "delay_s": 5.0, "hold_s": 10.0, "relay": "http://romulus:8770/command",
           "on_command": "slowtronics,dq1,on", "off_command": "slowtronics,dq1,off", "note": "...", "events": []}
    run = {"format": "FSUDAQ run file 1", "run": 7, "prefix": "run", "folder": folder,
           "start": {"time": "2026-10-31T12:00:00.000+01:00", "comment": "<b>beam</b> on"}, "stop": None, "led": led,
           "run_control": {}, "fsudaq": {"source": "feature/led-trigger 0123abc"},
           "clock": {"start": {"checked": "2026-10-31T12:00:00.000+01:00", "unlocked": [], "lost_since_last_check": []}, "note": "..."},
           "boards": [{"serial": s, "settings": {"0x8000 Config": "0x00000010 16"}} for s in SERIALS]}

    def write():
        with open(path, "w") as f:
            json.dump(run, f, indent=2)
    write()
    r = online.state()["run_file"]
    good &= check(r and r["run"] == 7 and r["boards"] == len(SERIALS) and r["stop"] is None and r["led"]["enabled"]
                  and r["led"]["delay_s"] == 5.0 and r["led"]["events"] == [] and r["source"] == "feature/led-trigger 0123abc"
                  and "settings" not in json.dumps(r) and r["clock"]["start"]["lost_since_last_check"] == [] and r["clock"]["stop"] is None,
                  f"run file at the start: {r}")
    led["events"].append({"what": "on", "command": "slowtronics,dq1,on", "sent": "2026-10-31T12:00:05.003+01:00", "run_ms": 5003,
                          "answered": None})
    write()
    r = online.state()["run_file"]
    good &= check(r["led"]["events"] == [{"what": "on", "command": "slowtronics,dq1,on", "sent": "2026-10-31T12:00:05.003+01:00",
                                          "run_ms": 5003, "reply": None, "ok": None, "switched": None, "error": None}], f"run file: on sent: {r['led']}")
    led["events"][0].update(answered="2026-10-31T12:00:05.020+01:00", reply="DQ1,ON", ok=True)
    led["events"].append({"what": "off", "command": "slowtronics,dq1,off", "sent": "2026-10-31T12:00:09.000+01:00", "run_ms": 9000,
                          "answered": "2026-10-31T12:00:12.000+01:00", "reply": "", "ok": False, "error": "no answer from http://romulus:8770/command"})
    run["stop"] = {"time": "2026-10-31T12:00:09.000+01:00", "run_ms": 9000, "comment": "stop", "problems": [], "bytes_saved": {},
                   "clock": {"checked": "2026-10-31T12:00:09.100+01:00", "unlocked": [], "lost_since_last_check": ["Digi-1443"]}}
    write()
    st = online.state()
    r = st["run_file"]
    good &= check(r["stop"] == {"time": "2026-10-31T12:00:09.000+01:00", "run_ms": 9000, "comment": "stop", "problems": []}
                  and [e["ok"] for e in r["led"]["events"]] == [True, False] and "no answer" in r["led"]["events"][1]["error"]
                  and r["clock"]["stop"]["lost_since_last_check"] == ["Digi-1443"],
                  f"run file at the stop: {r}")
    json.dumps(st, allow_nan=False)
    with open(path, "w") as f:
        f.write("{ not json")
    r = online.state()["run_file"]
    good &= check(r["file"] == "run_007_run.json" and "cannot read" in r.get("error", ""), f"run file damaged: {r}")
    finish(online, clock, boards)
    if good:
        ok("none, at the start, an LED command waiting, answered and failed, the stop, a damaged file: shown as written")


def scenario_x725(tmp):
    print("== an x725 board (4 ns time tags, like 1399) with x730 boards: its hits at their true times")
    folder = os.path.join(tmp, "x725"); os.makedirs(folder)
    serials, eight = [15880, 1399], [1399]
    clock = Clock()
    boards = [Board(folder, 1, 15880, 16, [3000.0] * 16, seed=1),
              Board(folder, 1, 1399, 8, [2000.0] * 8, seed=2, tick=4000)]
    online = make_online(folder, serials, eight, clock)
    drive(online, clock, boards, 0, 20)
    finish(online, clock, boards)
    good = check_daq(online, boards, serials, "x725")
    fin, late, tot = check_partition(online, boards, "x725")
    good &= check(late == 0, f"x725: {late} hits late")
    diff = same_finder(online.acc, offline(online, boards, serials))
    good &= check(not diff, f"x725: finder histograms differ from all hits at their true times: {diff}")
    newest = {b["serial"]: b["newest_s"] for b in online.state()["boards"]}
    t_first = min(t for b in boards for t, c, ql, qs in b.truth)   # newest_s counts from the run's first hit
    truth_newest = (max(t for t, c, ql, qs in boards[1].truth) - t_first) / PS
    good &= check(newest.get(1399) is not None and abs(newest[1399] - truth_newest) < 1e-6,
                  f"x725: newest hit of 1399 at {newest.get(1399)} s, written at {truth_newest} s")
    fs = od.fsu_fast.FileScan(boards[1].path(0)); fs.update()
    good &= check(fs.tick == 4000 and od.fsu_fast.FileScan(boards[0].path(0)).tick == 2000, "x725: tick not taken from the file name")
    if good:
        ok(f"{tot:,} hits: DAQ exact, all in the finder, finder identical to all hits at their true times; 1399's newest hit at {newest[1399]:.3f} s")


def scenario_view_only(tmp):
    print("== the page from another machine: everything to look at, none of the controls or FSUDAQ's calls")
    folder = os.path.join(tmp, "view"); os.makedirs(folder)
    clock = Clock(); boards = boards_for(folder)
    online = make_online(folder, SERIALS, EIGHT, clock)
    drive(online, clock, boards, 0, 5)
    od.Handler.online = online

    class Remote(od.Handler):
        def local(self):
            return False
    servers = [od.ThreadingHTTPServer(("127.0.0.1", 0), h) for h in (od.Handler, Remote)]
    for srv in servers:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    here, there = (f"http://127.0.0.1:{srv.server_address[1]}" for srv in servers)

    def get(url):
        try:
            with urllib.request.urlopen(url) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
    hits = online.state()["hits"]
    good = check(hits > 0, "view: no hits to look at")
    code, body = get(there + "/state")
    good &= check(code == 200 and json.loads(body)["can_control"] is False, f"view: remote /state {code}")
    good &= check(json.loads(get(here + "/state")[1])["can_control"] is True, "view: local /state not allowed to control")
    for path in ("/", f"/channel?slot={online.slots[0]}", "/grid"):
        good &= check(get(there + path)[0] == 200, f"view: remote {path} not served")
    for path in ("/clear", "/window?ns=50", f"/set?ref={online.slots[0]}", "/run?folder=/tmp", "/stopped?folder=" + folder, "/open"):
        good &= check(get(there + path)[0] == 403, f"view: remote {path} not refused")
    # near misses of a control's path: neither refused-and-done nor done (31 Oct 2026: they went past the check)
    for path in ("/clear/", "/clearX", "/runx?folder=/tmp", "/stopped/?folder=" + folder, f"/set/?ref={online.slots[0]}",
                 "/load/?folder=" + folder, "/open/", "/live/", "/window/?ns=50"):
        good &= check(get(there + path)[0] in (403, 404), f"view: remote {path} answered")
    good &= check(online.state()["hits"] == hits and online.state()["daq"]["ref"] is None, "view: a refused call changed something")
    good &= check(get(here + "/clear")[0] == 204 and online.state()["hits"] == 0, "view: local Clear refused")
    for srv in servers:
        srv.shutdown()
    if good:
        ok("another machine sees the page, state and histograms; Clear, window, reference, /run, /stopped and /open are refused; odin keeps them")


def scenario_rollover_backlog(tmp):
    print("== a finished run with roll-overs, read a little per step (a big recorded run, or a backlog): every hit, none late")
    # 1 Nov 2026: the files of a board were indexed side by side, so the finder passed the unread rest of the
    # earlier file (its hits late) and the file was released half read (its rest never counted), while the page
    # said "everything processed" and blamed the DAQ for missing aggregates
    good = True
    for hold in (2, 30):
        folder = os.path.join(tmp, f"backlog{hold}"); os.makedirs(folder)
        boards = boards_for(folder, roll=400_000, seed=90 + hold)
        for t in range(30):
            for b in boards:
                b.hits(t * PS, (t + 1) * PS)
        for b in boards:
            b.flush(); b.f.close()
        clock = Clock()
        online = make_online(folder, SERIALS, EIGHT, clock, extra=["--hold-s", str(hold)])
        online.run_stopped(folder)
        saved, od.INDEX_WORDS = od.INDEX_WORDS, 20_000
        try:
            for _ in range(600):
                clock.t += 1
                online.step()
                wait_tasks(online)
                if online.status.endswith("everything processed") and not online.pending:
                    break
        finally:
            od.INDEX_WORDS = saved
        st = online.state()
        good &= check(st["status"].endswith("everything processed"), f"backlog {hold}: status '{st['status']}'")
        good &= check(max(bd["files"] for bd in st["boards"]) > 3, "backlog: no roll-over")
        good &= check_daq(online, boards, SERIALS, f"backlog {hold}")
        fin, late, tot = check_partition(online, boards, f"backlog {hold}")
        good &= check(late == 0 and not same_finder(online.acc, offline(online, boards, SERIALS)), f"backlog {hold}: {late} late, or finder not as one pass")
        good &= check(not any("missing" in h for h in st["health"]), f"backlog {hold}: false alarm {st['health']}")
    if good:
        ok(f"{tot:,} hits over roll-overs, read 80 kB per step: DAQ exact, none late, finder identical to one pass, no false alarm")


def scenario_quiet_live_start(tmp):
    print("== the dashboard started in a pause of a run being taken: not taken for a finished run")
    folder = os.path.join(tmp, "quietstart"); os.makedirs(folder)
    clock = Clock(); boards = boards_for(folder, seed=95)
    for t in range(5):
        for b in boards:
            b.hits(t * PS, (t + 1) * PS)
    for b in boards:
        b.flush()
    old = time.time() - 10                   # nothing written for 10 s (more than --stall-s) when it starts
    for f in os.listdir(folder):
        os.utime(os.path.join(folder, f), (old, old))
    online = make_online(folder, SERIALS, EIGHT, clock)
    clock.t += 1; online.step()
    modes = []
    drive(online, clock, boards, 5, 20, each=lambda o: modes.append(o.state()["mode"]))
    st = online.state()
    good = check(not st["finished_at_start"] and "running" in modes and st["daq"]["total_rate"] is not None,
                 f"quiet start: {st['status']}, modes {set(modes)}, rate {st['daq']['total_rate']}")
    if good:
        ok(f"read as a run being taken, modes {sorted(set(modes))}, the rate shown")


def read_summary(folder):
    with open(od.summary_path(folder)) as f:
        return json.load(f)


def write_run_file(folder, stop=True, comment="stop"):
    """FSUDAQ's run file (as in scenario_run_file), with or without the stop."""
    name = os.path.basename(folder)
    run = {"format": "FSUDAQ run file 1", "run": int(name.rsplit("_", 1)[1]), "prefix": name.rsplit("_", 1)[0], "folder": folder,
           "start": {"time": "2026-10-31T12:00:00.000+01:00", "comment": "beam on"}, "led": {"enabled": False, "events": []},
           "stop": None if not stop else {"time": "2026-10-31T12:00:12.000+01:00", "run_ms": 12000, "comment": comment, "problems": []},
           "run_control": {}, "fsudaq": {"source": "test 0000000"}, "boards": []}
    with open(os.path.join(folder, name + "_run.json"), "w") as f:
        json.dump(run, f)


def set_old(folder, age_s):
    """Every file of the folder (and the folder) last written age_s ago."""
    t = time.time() - age_s
    for f in os.listdir(folder):
        os.utime(os.path.join(folder, f), (t, t))
    os.utime(folder, (t, t))


def scenario_summary_at_stop(tmp):
    print("== run summary: written once at the stop, its totals as the page showed them, whole or not at all")
    folder = os.path.join(tmp, "sum_001"); os.makedirs(folder)
    clock = Clock(); boards = boards_for(folder, slow=[(1443, 6, 1.5), (1443, 7, 1.5)], seed=101)
    online = make_online(folder, SERIALS, EIGHT, clock, extra=["--hold-s", "8"])
    writes = []
    real, real_replace = od.write_summary_file, od.os.replace

    def counting(f, doc):
        writes.append(doc["run"]["end"])
        return real(f, doc)
    od.write_summary_file = counting
    try:
        drive(online, clock, boards, 0, 30)
        good = check(not writes and not os.path.exists(od.summary_path(folder)), "summary: written while the run was taken")
        t_old = time.time() - 3600
        os.utime(folder, (t_old, t_old))           # the folder's own time must stay as it was (newest run folder)
        finish(online, clock, boards)
        st = online.state()
        good &= check(writes == ["stopped"], f"summary: written {writes} at the stop")
        for _ in range(5):
            clock.t += 1; online.step()
        good &= check(len(writes) == 1, f"summary: written again with nothing new ({writes})")
        s = read_summary(folder)
        good &= check(abs(os.stat(folder).st_mtime - t_old) < 1, "summary: the run folder's modification time changed")
        good &= check(not [f for f in os.listdir(folder) if f.endswith(".tmp")], "summary: a temporary file left")
        good &= check(s["complete"] and not s["incomplete"] and s["source"] == "live" and s["run"]["end"] == "stopped"
                      and s["run"]["end_how"] == "FSUDAQ said the run stopped", f"summary: {s['complete']} {s['incomplete']} {s['source']} {s['run']}")
        late = st["late"]
        good &= check(late > 0 and any(c["reason"] == "late_hits" for c in s["caveats"]) and s["finder"]["late_hits"] == late,
                      f"summary: {late} late hits, caveats {s['caveats']}")
        # every number as /state showed it at the end
        good &= check(s["finder"]["hits"] == st["hits"] and s["finder"]["bunches"] == st["bunches"]
                      and s["finder"]["status_counts"] == st["status_counts"] and s["finder"]["unmapped_hits"] == st["unmapped"],
                      "summary: the finder's counts differ from the page")
        Hs = s["histograms"]
        good &= check(Hs["momentum"]["counts"] == st["hist_p"]["counts"] and Hs["momentum"]["edges"] == st["hist_p"]["edges"]
                      and Hs["timing_residual"]["t0_t1"] == st["hist_res"]["t10"] and Hs["timing_residual"]["t1_t2"] == st["hist_res"]["t21"]
                      and Hs["timing_residual"]["edges"] == st["hist_res"]["edges"]
                      and Hs["t2_minus_t1"]["counts"] == st["hist_dt"]["counts"] and Hs["light_ratio"]["planes"] == st["hist_r"]["counts"]
                      and Hs["cluster_size"]["planes"] == st["hist_cl"] and Hs["candidates_per_bunch"]["counts"] == st["hist_ncand"]
                      and Hs["tracks_per_bunch"]["counts"] == st["hist_ntrk"] and Hs["hits_per_bunch"]["counts"] == st["hist_nhit"],
                      "summary: a histogram differs from the page")
        good &= check(s["occupancy"]["planes"] == st["occupancy"] and s["occupancy"]["counters"] == st["counters"], "summary: occupancy differs")
        page = {x["slot"]: (x["count"], x["late"]) for x in st["daq"]["slots"]}
        good &= check({c["slot"]: (c["hits"], c["late"]) for c in s["channels"]} == page, "summary: per-channel hits differ from the page")
        T = truth(boards, SERIALS)
        good &= check(all(c["hits"] == len(T.get(c["slot"], [])) for c in s["channels"]), "summary: per-channel hits differ from the hits written")
        good &= check([b["bytes"] for b in s["boards"]] == [b["bytes"] for b in st["boards"]] and s["data"]["bytes"] == st["daq"]["run_bytes"]
                      and s["fingerprint"] == od.run_fingerprint(folder), "summary: file sizes differ")
        span = s["data"]["span_s"]
        good &= check(all(abs(c["mean_rate_hz"] - c["hits"] / span) < 1e-9 * (1 + c["hits"]) for c in s["channels"]), "summary: mean rates")
        H, F = s["history"]["daq"], s["history"]["finder"]
        good &= check([sum(h[i] for h in H["hits"]) for i in range(len(SERIALS))] == [b["hits"] for b in s["boards"]]
                      and abs(sum(H["covered_s"]) - span) < 1e-6 and all(x < y for x, y in zip(H["t0_s"], H["t1_s"])),
                      "summary: the rate history does not add up to the board totals over the run's span")
        good &= check(sum(F["bunches"]) == s["finder"]["bunches"] and sum(sum(v) for v in F["status"].values()) == s["finder"]["bunches"],
                      "summary: the finder's history does not add up")
        good &= check(len(online.secs) <= od.HISTORY_S + 1 and len(H["t0_s"]) == 3, f"summary: {len(H['t0_s'])} history points for 30 s")
        json.dumps(s, allow_nan=False)
        # FSUDAQ writes the stop (and its comment) into the run file after /stopped: written again with it
        write_run_file(folder, comment="beam off")
        clock.t += 1; online.step()
        s = read_summary(folder)
        good &= check(len(writes) == 2 and s["run"]["stop_comment"] == "beam off" and s["run"]["duration_s"] == 12.0,
                      f"summary: the run file's stop not taken in ({writes}, {s['run']['stop_comment']})")
        # Clear after the end, then the run file changes: only the run's part is made again, the counts stay
        hits = s["finder"]["hits"]
        online.clear()
        write_run_file(folder, comment="beam off, later")
        clock.t += 1; online.step()
        s = read_summary(folder)
        good &= check(len(writes) == 3 and s["complete"] and s["finder"]["hits"] == hits and s["run"]["stop_comment"] == "beam off, later",
                      f"summary: a Clear after the end reached it ({s['complete']}, {s['finder']['hits']} of {hits} hits)")
        # a write that fails: nothing half written, the loop goes on, the page says so, tried again later
        os.remove(od.summary_path(folder))

        def boom(a, b):
            raise OSError(28, "No space left on device")
        od.os.replace = boom
        write_run_file(folder, comment="beam off again")
        try:
            for _ in range(3):
                clock.t += 1; online.step()
        finally:
            od.os.replace = real_replace
        st = online.state()
        good &= check(not os.path.exists(od.summary_path(folder)) and not [f for f in os.listdir(folder) if f.endswith(".tmp")],
                      "summary: a failed write left a file")
        good &= check(len(writes) == 4 and any("run summary" in h and "No space" in h for h in st["health"]),
                      f"summary: a failed write not on the page ({writes}, {st['health']})")
        clock.t += od.SUMMARY_RETRY_S + 1; online.step()
        s = read_summary(folder) if os.path.exists(od.summary_path(folder)) else {}
        good &= check(online.summary_error is None and s.get("complete") and s["finder"]["hits"] == hits
                      and s["run"]["stop_comment"] == "beam off again", "summary: not written again after a failure")
    finally:
        od.write_summary_file, od.os.replace = real, real_replace
    if good:
        ok(f"written once at the stop ({s['finder']['hits']:,} hits in the finder, {late:,} late), every total as the page showed it, "
           "the folder's time kept; again when the run file got the stop; a failed write reported, left nothing, tried again")


def scenario_summary_incomplete(tmp):
    print("== run summary: complete=false when the dashboard did not see the whole run, and why")
    good = True
    # started mid-run
    folder = os.path.join(tmp, "inc_001"); os.makedirs(folder)
    clock = Clock(); boards = boards_for(folder, seed=111)
    for t in range(5):
        for b in boards:
            b.hits(t * PS, (t + 1) * PS)
    online = od.Online(od.parse_args(["--run", folder, "--boards", ",".join(map(str, SERIALS)), "--eight-channel-boards", "1443",
                                      "--workers", "0", "--config", "/nonexistent"]), clock=clock)
    clock.t += 5
    drive(online, clock, boards, 5, 12)
    finish(online, clock, boards)
    s = read_summary(folder)
    good &= check(not s["complete"] and [x["reason"] for x in s["incomplete"]] == ["started_mid_run"] and not s["reading"]["from_start"],
                  f"mid-run start: {s['complete']} {s['incomplete']}")
    D = online.daq
    good &= check([c["hits"] for c in s["channels"]] == [int(D.counts[sl]) for sl in online.slots], "mid-run start: per-channel hits")
    span = s["data"]["span_s"]
    good &= check(all(abs(c["mean_rate_hz"] - (c["hits"] - c["early"]) / span) < 1e-9 * (1 + c["hits"]) for c in s["channels"]),
                  "mid-run start: the mean rate counts hits from before the start")
    # Clear, and the bunch window, changed mid-run
    for what in ("clear", "window"):
        folder = os.path.join(tmp, f"inc_{what}_002"); os.makedirs(folder)
        clock = Clock(); boards = boards_for(folder, seed=112)
        online = make_online(folder, SERIALS, EIGHT, clock)
        drive(online, clock, boards, 0, 6)
        online.clear() if what == "clear" else online.set_window(100)
        drive(online, clock, boards, 6, 12)
        finish(online, clock, boards)
        s, st = read_summary(folder), online.state()
        T = truth(boards, SERIALS)
        reason = "cleared" if what == "clear" else "window_changed"
        good &= check(not s["complete"] and [x["reason"] for x in s["incomplete"]] == [reason] and s["finder"]["counted"].startswith("since"),
                      f"{what}: {s['incomplete']}, finder counted {s['finder']['counted']}")
        good &= check(all(c["hits"] == len(T.get(c["slot"], [])) for c in s["channels"]) and s["finder"]["hits"] == st["hits"]
                      and s["finder"]["hits"] < s["data"]["hits_counted"],
                      f"{what}: per-channel hits not the whole run, or the finder's not since the change")
    # boards in the folder that are not analysed
    folder = os.path.join(tmp, "inc_003"); os.makedirs(folder)
    clock = Clock(); boards = boards_for(folder, seed=113) + [Board(folder, 1, 30508, 8, [500.0] * 8, seed=9)]
    online = make_online(folder, SERIALS, EIGHT, clock)
    drive(online, clock, boards, 0, 6)
    finish(online, clock, boards)
    s = read_summary(folder)
    good &= check(not s["complete"] and [x["reason"] for x in s["incomplete"]] == ["boards_not_analysed"] and "30508" in s["incomplete"][0]["detail"],
                  f"other board: {s['incomplete']}")
    # no word of the stop: only once no file was written for FINISHED_QUIET_S, and never while FSUDAQ said the run goes on
    folder = os.path.join(tmp, "inc_004"); os.makedirs(folder)
    clock = Clock(); boards = boards_for(folder, seed=114)
    online = make_online(folder, SERIALS, EIGHT, clock, extra=["--hold-s", "5"])
    t = drive(online, clock, boards, 0, 6)
    for b in boards:
        b.flush()
    t = drive(online, clock, boards, t, t + 12, gen=False)
    good &= check(online.mode == "quiet" and online.all_done and not os.path.exists(od.summary_path(folder)),
                  f"no word: summary while the folder was written recently ({online.mode}, {online.status})")
    set_old(folder, od.FINISHED_QUIET_S + 60)
    online.run_open = os.path.abspath(folder)      # FSUDAQ said /run and not /stopped: the run goes on
    t = drive(online, clock, boards, t, t + 2, gen=False)
    good &= check(not os.path.exists(od.summary_path(folder)), "no word: summary written while FSUDAQ said the run goes on")
    online.run_open = None
    t = drive(online, clock, boards, t, t + 2, gen=False)
    s = read_summary(folder)
    good &= check(s["run"]["end"] == "guess" and s["complete"] and any(c["reason"] == "end_not_recorded" for c in s["caveats"]),
                  f"no word: end {s['run']['end']}, caveats {s['caveats']}")
    # ... and the run went on after all: written again at its end, with the new data
    t = drive(online, clock, boards, t, t + 6)
    good &= check(online.summary_kind is None, "went on: still taken for ended")
    finish(online, clock, boards)
    s = read_summary(folder)
    T = truth(boards, SERIALS)
    good &= check(s["run"]["end"] == "stopped" and all(c["hits"] == len(T.get(c["slot"], [])) for c in s["channels"]),
                  f"went on: summary not written again with the new data (end {s['run']['end']})")
    if good:
        ok("started mid-run, Clear, window changed, another board's files: complete=false with the reason; no stop word: "
           "only after 5 min of quiet and never while FSUDAQ says the run goes on; written again when the run went on")


def scenario_backfill(tmp):
    print("== backfill: older runs get their summary between runs only, from a nice-19 child, killed at once by a run start")
    root = os.path.join(tmp, "bf_data"); os.makedirs(root)

    def recorded(name, seed, age_s):
        folder = os.path.join(root, name); os.makedirs(folder)
        bs = boards_for(folder, seed=seed)
        for t in range(8):
            for b in bs:
                b.hits(t * PS, (t + 1) * PS)
        for b in bs:
            b.flush(); b.f.close()
        write_run_file(folder)
        set_old(folder, age_s)
        return folder
    bad = recorded("run_000", 200, 7200)                       # oldest: a file that cannot be read
    os.chmod(os.path.join(bad, "run_001_15880_PSD_2_000.fsu"), 0)
    a = recorded("run_001", 201, 3600)
    b = recorded("run_002", 202, 1800)                         # newest: the one the dashboard shows
    a_mtime = os.stat(a).st_mtime_ns
    clock = Clock()
    online = od.Online(od.parse_args(["--data-path", root, "--boards", ",".join(map(str, SERIALS)), "--eight-channel-boards", "1443",
                                      "--workers", "0", "--config", "/nonexistent"]), clock=clock)
    online.fsudaq_probe = lambda: True        # FSUDAQ runs (set here: never the host's)
    saved = od.BACKFILL_IDLE_S, od.BACKFILL_LOOK_S
    od.BACKFILL_IDLE_S, od.BACKFILL_LOOK_S = 0, 0
    try:
        for _ in range(6):
            clock.t += 1; online.step(); wait_tasks(online)
        good = check(online.folder == b and read_summary(b)["source"] == "read_after_end", f"backfill: the newest run not read and summarised ({online.folder})")
        # FSUDAQ takes a run: no child
        c = os.path.join(root, "run_003"); os.makedirs(c)
        online.run_started(c)
        for _ in range(3):
            clock.t += 1; online.step(); online.backfill_tick()
        good &= check(online.bf is None and "taking run run_003" in online.bf_busy, f"backfill: started during a run ({online.bf_busy})")
        online.run_stopped(c)
        clock.t += 1; online.step(); online.backfill_tick()
        bf = online.bf
        good &= check(bf is not None and bf["folder"] == a, f"backfill: not started between runs ({online.bf_busy})")
        if bf is None:
            return
        pid = bf["proc"].pid
        for _ in range(100):                 # held where it is (once it is python, past ionice and nice): a run must find it at work
            try:
                with open(f"/proc/{pid}/comm") as f:
                    if f.read().startswith("python"):
                        break
            except OSError:
                break
            time.sleep(0.05)
        os.kill(pid, signal.SIGSTOP)
        try:
            prio = os.getpriority(os.PRIO_PROCESS, bf["proc"].pid)
        except OSError:
            prio = None
        good &= check(prio == 19, f"backfill: the child runs at nice {prio}")
        # a run starts while it works (here: a half-written temporary file of it): killed, nothing left, folder time kept
        tmp_file = os.path.join(a, f".run_001_summary.json.{bf['proc'].pid}.tmp")
        with open(tmp_file, "w") as f:
            f.write("{\"half")
        d = os.path.join(root, "run_004"); os.makedirs(d)
        t0 = time.time()
        online.run_started(d)
        good &= check(online.bf is None and time.time() - t0 < 0.5, f"backfill: the run start waited for the child ({time.time() - t0:.1f} s)")
        while online.bf_reap and time.time() - t0 < 5:     # cleaned up once it is gone (not waited for under the lock)
            online.backfill_tick(); time.sleep(0.05)
        good &= check(not online.bf_reap and bf["proc"].poll() is not None and time.time() - t0 < 2,
                      f"backfill: the child not stopped at once when the run started ({time.time() - t0:.1f} s)")
        good &= check(not os.path.exists(tmp_file) and not os.path.exists(od.summary_path(a)) and os.stat(a).st_mtime_ns == a_mtime,
                      "backfill: a stopped child left a file, or the folder's time changed")
        online.run_stopped(d)
        # between runs again: it starts over, and goes on to the next run, which it cannot read (given up, with the reason)
        t_end = time.time() + 300
        while time.time() < t_end and not (bad in online.bf_skipped and online.bf is None):
            clock.t += 1; online.step(); online.backfill_tick()
            time.sleep(0.2)
        s = read_summary(a) if os.path.exists(od.summary_path(a)) else None
        good &= check(s is not None and s["source"] == "backfill" and s["complete"] and s["run"]["end"] == "stopped"
                      and s["setup_known"] is False and s["setup_at_run_time"] == "unknown"
                      and any(c["reason"] == "setup_unknown" for c in s["caveats"]),
                      f"backfill: no summary of run_001 ({s and (s['source'], s['complete'])})")
        good &= check(bad in online.bf_skipped and "cannot read" in online.bf_skipped[bad][1] and not os.path.exists(od.summary_path(bad)),
                      f"backfill: the unreadable run not skipped with its reason ({online.bf_skipped})")
        for _ in range(3):
            clock.t += 1; online.step(); online.backfill_tick()
        good &= check(online.bf is None, "backfill: the unreadable run tried again")
        runs = {r["name"]: r for r in online.runs()}
        good &= check(runs["run_001"]["summary"]["complete"] and runs["run_001"]["summary"]["source"] == "backfill"
                      and "cannot read" in runs["run_000"].get("backfill", ""), f"backfill: /runs {runs['run_000']}")
        good &= check(os.stat(a).st_mtime_ns == a_mtime and od.newest_run_folder(root) == b, "backfill: the folder's time changed")
        # the same run read live (a copy, by its own dashboard) gives the same summary
        cp = os.path.join(tmp, "bf_copy", "run_001")
        shutil.copytree(a, cp)
        os.remove(od.summary_path(cp))
        clock2 = Clock()
        live = make_online(cp, SERIALS, EIGHT, clock2)
        for _ in range(6):
            clock2.t += 1; live.step(); wait_tasks(live)
        L = read_summary(cp)
        same = ("complete", "incomplete", "caveats", "boards", "channels", "finder", "histograms", "occupancy", "history", "data", "fingerprint", "scope")
        for x in (L, s):                         # "analysed with the setup of <its own time>"
            x["caveats"] = [c for c in x["caveats"] if c["reason"] != "setup_unknown"]
        diff = [k for k in same if L[k] != s[k] and k != "finder"]
        fin = [k for k in L["finder"] if L["finder"][k] != s["finder"][k] and k not in ("window_src", "decided")]
        fin += [k for k in L["finder"]["decided"] if L["finder"]["decided"][k] != s["finder"]["decided"][k] and k != "reference_cpu_s"]
        diff += [f"finder.{k}" for k in fin]   # (where the window came from, and CPU seconds, differ)
        good &= check(L["source"] == "read_after_end" and not diff, f"backfill: differs from the live summary of the same run in {diff}")
    finally:
        od.BACKFILL_IDLE_S, od.BACKFILL_LOOK_S = saved
        online.backfill_stop("test over")
        os.chmod(os.path.join(bad, "run_001_15880_PSD_2_000.fsu"), 0o644)
    if good:
        ok(f"none during a run; started between runs at nice 19, killed within a second by a run start leaving nothing; "
           f"{s['data']['hits_counted']:,} hits, identical to the live summary of the same run; the unreadable run skipped once, with its reason")


def scenario_summary_route(tmp):
    print("== GET /summary: anyone may read a recorded run's summary; only a run folder's bare name is taken")
    root = os.path.join(tmp, "route_data"); os.makedirs(root)
    a = os.path.join(root, "run_001"); os.makedirs(a)
    clock = Clock(); boards = boards_for(a, seed=121)
    online = make_online(a, SERIALS, EIGHT, clock)
    drive(online, clock, boards, 0, 5)
    finish(online, clock, boards)
    write_run_file(a)
    b = os.path.join(root, "run_002"); os.makedirs(b)
    boards_for(b, seed=122)[0].write(0, [(1000, 0, 0, 512, 100, 50)])
    secret = os.path.join(tmp, "outside", "secret_001"); os.makedirs(secret)
    shutil.copy(os.path.join(a, "run_001_15880_PSD_2_000.fsu"), secret)
    os.symlink(secret, os.path.join(root, "link_001"))
    os.makedirs(os.path.join(root, ".hidden_001"))
    online.a.run = None; online.a.data_path = root; online.follow = None
    od.Handler.online = online

    class Remote(od.Handler):
        def local(self):
            return False
    servers = [od.ThreadingHTTPServer(("127.0.0.1", 0), h) for h in (od.Handler, Remote)]
    for srv in servers:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    here, there = (f"http://127.0.0.1:{srv.server_address[1]}" for srv in servers)

    def get(url):
        try:
            with urllib.request.urlopen(url) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")
    folder, hits = online.folder, online.state()["hits"]
    code, d = get(there + "/summary?run=run_001")
    good = check(code == 200 and d["summary"] and d["summary"]["complete"] and not d["stale"] and d["run_file"]["run"] == 1
                 and d["summary"]["finder"]["hits"] == hits, f"route: remote /summary of run_001: {code} {d.get('note')}")
    code, d = get(there + "/summary?run=run_002")
    good &= check(code == 200 and d["summary"] is None and d["note"].startswith("no summary recorded") and d["files"] == 3,
                  f"route: a run without summary: {code} {d}")
    refused = ["", ".", "..", "../outside/secret_001", "run_001/../run_001", "run_001/", "/etc", "%2Fetc%2Fpasswd", "%2e%2e",
               "%2e%2e%2foutside%2fsecret_001", "run_001%00", "run%5C001", "link_001", ".hidden_001", "nonexistent_001", "x" * 300,
               urllib.parse.quote(secret, safe="")]
    for name in refused:
        code, d = get(there + "/summary?run=" + name)
        good &= check(code in (400, 404) and d["summary"] is None and "folder" not in d and "secret" not in json.dumps(d),
                      f"route: /summary?run={name[:40]} answered {code} {str(d)[:120]}")
    code, d = get(there + "/summary")
    good &= check(code == 400, f"route: /summary without a run: {code}")
    good &= check(get(there + "/load?folder=" + a)[0] == 403 and get(there + "/live")[0] == 403, "route: remote /load or /live not refused")
    good &= check(get(here + "/summary?run=run_001")[0] == 200, "route: local /summary refused")
    code, d = get(there + "/runs")
    rs = {r["name"]: r for r in d["runs"]}
    good &= check(code == 200 and rs["run_001"]["summary"]["complete"] and not rs["run_001"]["summary"]["stale"]
                  and rs["run_002"]["summary"] is None, f"route: /runs {code}")
    with open(os.path.join(a, "run_001_1443_PSD_2_000.fsu"), "ab") as f:      # the files changed after the summary
        f.write(b"\0" * 16)
    code, d = get(there + "/summary?run=run_001")
    good &= check(code == 200 and d["stale"] and "changed" in d["note"], f"route: changed files not marked stale ({d.get('note')})")
    good &= check(online.folder == folder and online.state()["hits"] == hits and online.inspect is None, "route: /summary changed the live view")
    for srv in servers:
        srv.shutdown()
    if good:
        ok(f"another machine reads summaries (and run.json-only metadata) while /load and /live stay refused; "
           f"{len(refused)} names that are not a run folder's (paths, .., links out, hidden, encoded) refused; changed files marked stale")


def scenario_backfill_reread(tmp):
    print("== backfill re-reads an incomplete summary once (after the runs without one); what the live reading saw is kept")
    root = os.path.join(tmp, "rr_data"); os.makedirs(root)
    good = True
    # run_010: read live from the middle of the run, a board without data (a problem the live page showed)
    a = os.path.join(root, "run_010"); os.makedirs(a)
    clock = Clock(); boards = boards_for(a, seed=131)
    quiet = [b for b in boards if b.serial != 1443]   # Digi-1443's file is there, empty
    for t in range(3):
        for b in quiet:
            b.hits(t * PS, (t + 1) * PS)
    live = od.Online(od.parse_args(["--run", a, "--boards", ",".join(map(str, SERIALS)), "--eight-channel-boards", "1443",
                                    "--workers", "0", "--config", "/nonexistent", "--hold-s", "5"]), clock=clock)
    clock.t += 3
    drive(live, clock, quiet, 3, 12)
    finish(live, clock, boards)
    L = read_summary(a)
    lp = [p for p in L["problems_during_reading"] if p["text"].startswith("Digi-1443: no data")]
    good &= check(L["source"] == "live" and [x["reason"] for x in L["incomplete"]] == ["started_mid_run"] and bool(lp),
                  f"re-read: the live summary is not as set up ({L['incomplete']}, {[p['text'] for p in L['problems_during_reading']]})")
    # run_012: read live from its start, files of a board not analysed: a re-read cannot make it complete
    c = os.path.join(root, "run_012"); os.makedirs(c)
    clock = Clock(); boards = boards_for(c, seed=132) + [Board(c, 1, 30508, 8, [500.0] * 8, seed=8)]
    live = make_online(c, SERIALS, EIGHT, clock)
    drive(live, clock, boards, 0, 5)
    finish(live, clock, boards)
    C0 = read_summary(c)
    good &= check([x["reason"] for x in C0["incomplete"]] == ["boards_not_analysed"], f"re-read: run_012 live summary {C0['incomplete']}")
    for folder, age in ((a, 3600), (c, 3000)):
        write_run_file(folder)
        set_old(folder, age)
    b = os.path.join(root, "run_011"); os.makedirs(b)     # the newest: the run the dashboard shows
    bs = boards_for(b, seed=133)
    for t in range(3):
        for x in bs:
            x.hits(t * PS, (t + 1) * PS)
    for x in bs:
        x.flush(); x.f.close()
    write_run_file(b)
    set_old(b, 1800)
    clock = Clock()
    online = od.Online(od.parse_args(["--data-path", root, "--boards", ",".join(map(str, SERIALS)), "--eight-channel-boards", "1443",
                                      "--workers", "0", "--config", "/nonexistent"]), clock=clock)
    online.fsudaq_probe = lambda: True        # FSUDAQ runs (set here: never the host's)
    saved = od.BACKFILL_IDLE_S, od.BACKFILL_LOOK_S
    od.BACKFILL_IDLE_S, od.BACKFILL_LOOK_S = 0, 0
    try:
        for _ in range(6):
            clock.t += 1; online.step(); wait_tasks(online)
        t_end = time.time() + 300
        while time.time() < t_end and len(online.bf_done) < 2:
            clock.t += 1; online.step(); online.backfill_tick()
            time.sleep(0.2)
        for _ in range(10):                   # nothing more to do: no third child, no second try of either run
            clock.t += 1; online.step(); online.backfill_tick()
            time.sleep(0.1)
        done = [os.path.basename(f) for f, _ in online.bf_done]
        good &= check(sorted(done) == ["run_010", "run_012"] and online.bf is None and not online.bf_skipped,
                      f"re-read: backfills {done}, running {online.bf and online.bf['folder']}, skipped {online.bf_skipped}")
        A = read_summary(a)
        P = A.get("previous_live") or {}
        pp = [p for p in P.get("problems_during_reading", []) if p["text"].startswith("Digi-1443: no data")]
        good &= check(A["source"] == "backfill" and A["complete"] and P.get("source") == "live"
                      and [x["reason"] for x in P.get("incomplete", [])] == ["started_mid_run"]
                      and pp == lp and P.get("reading") == L["reading"] and P.get("written_at") == L["written_at"],
                      f"re-read: run_010 {A['source']} {A['complete']}, previous_live {P and {k: P[k] for k in ('source', 'complete', 'incomplete')}}")
        C = read_summary(c)
        good &= check(C["source"] == "backfill" and [x["reason"] for x in C["incomplete"]] == ["boards_not_analysed"]
                      and (C.get("previous_live") or {}).get("written_at") == C0["written_at"],
                      f"re-read: run_012 {C['source']} {C['incomplete']}")
        meta = od.summary_meta(c, od.run_fingerprint(c))
        good &= check(not meta["complete"] and meta["source"] == "backfill", f"re-read: run_012 meta {meta}")
    finally:
        od.BACKFILL_IDLE_S, od.BACKFILL_LOOK_S = saved
        online.backfill_stop("test over")
    # write_summary_file's rules on their own: a reading covering less is kept out and marked (not tried again);
    # a re-read of another kind carries the live part, and keeps carrying it
    d = os.path.join(tmp, "rr_unit", "run_020"); shutil.copytree(c, d)
    live_doc = dict(read_summary(c), source="live", reading={"started_at": "x"}, problems_during_reading=[{"text": "seen live"}])
    live_doc.pop("previous_live", None)
    od.write_summary_file(d, live_doc)
    worse = dict(live_doc, source="backfill", reading={"started_at": "y"}, written_at="later",
                 incomplete=live_doc["incomplete"] + [{"reason": "analysis_errors", "detail": "x"}])
    path, written, note = od.write_summary_file(d, worse)
    K = read_summary(d)
    good &= check(not written and "kept" in note and K["source"] == "live" and K["backfill_tried"]["fingerprint"] == od.run_fingerprint(d)
                  and od.summary_meta(d, od.run_fingerprint(d))["backfill_tried"], f"re-read: a worse backfill not kept out ({note})")
    better = dict(live_doc, source="load", reading={"started_at": "z"}, complete=True, incomplete=[])
    od.write_summary_file(d, better)
    again = dict(better, source="replay", reading={"started_at": "w"})
    od.write_summary_file(d, again)
    K = read_summary(d)
    good &= check(K["source"] == "replay" and K["previous_live"]["problems_during_reading"] == [{"text": "seen live"}],
                  f"re-read: the live part lost over two re-reads ({K.get('previous_live')})")
    if good:
        ok("an incomplete live summary re-read once after the others and made complete, with the live problems and coverage "
           "kept in previous_live; one that stays incomplete re-read once, not again; a worse re-read kept out and marked")


def scenario_summary_setup_and_restarts(tmp):
    print("== summaries record their setup; a later reading with another setup never replaces a live one; two restarts keep it")
    import hashlib
    import subprocess
    good = True
    root = os.path.join(tmp, "su_data"); os.makedirs(root)
    # a run followed live: its setup recorded (the map's content by sha256)
    a = os.path.join(root, "run_040"); os.makedirs(a)
    clock = Clock(); boards = boards_for(a, seed=141)
    online = make_online(a, SERIALS, EIGHT, clock, extra=["--hold-s", "5"])
    drive(online, clock, boards, 0, 12, each=lambda o: o.state())
    finish(online, clock, boards)
    L = read_summary(a)
    mp = L["setup"]["files"]["map"]["path"]
    with open(mp, "rb") as f:
        sha = hashlib.sha256(f.read()).hexdigest()
    good &= check(L["source"] == "live" and L["setup_known"] and L["setup"]["files"]["map"]["sha256"] == sha
                  and L["setup"]["window_ns"] == 25.0 and bool(L["setup"]["identity"]), f"setup: live summary {L['source']} {L.get('setup')}")
    # two restarts of the dashboard, each reading the finished run again: "read_after_end", the live part kept through both
    write_run_file(a)
    set_old(a, 3600)
    for n in (1, 2):
        r = od.Online(od.parse_args(["--run", a, "--boards", ",".join(map(str, SERIALS)), "--eight-channel-boards", "1443",
                                     "--workers", "0", "--config", "/nonexistent"]), clock=Clock())
        for _ in range(4):
            r.clock.t += 1; r.step(); wait_tasks(r)
        R = read_summary(a)
        P = R.get("previous_live") or {}
        good &= check(R["source"] == "read_after_end" and P.get("source") == "live" and P.get("written_at") == L["written_at"]
                      and P.get("problems_during_reading") == L["problems_during_reading"] and R["setup_known"]
                      and not any(c["reason"] == "setup_unknown" for c in R["caveats"]),
                      f"restart {n}: {R['source']}, previous_live from {P.get('source')} {P.get('written_at')}, setup known {R['setup_known']}")
    # another setup (bunch window) never replaces a live summary
    other = dict(R, source="backfill", reading={"started_at": "later"}, setup=dict(R["setup"], identity="different"),
                 complete=True, incomplete=[])
    other.pop("previous_live", None)
    live_again = dict(L)                     # the live one back in place, then a backfill of another setup over it
    od._write_json_whole(a, od.summary_path(a), live_again, live_again["fingerprint"])
    path, written, note = od.write_summary_file(a, other)
    K = read_summary(a)
    good &= check(not written and K["source"] == "live" and "another setup" in K["backfill_tried"]["why"],
                  f"setup: a backfill of another setup replaced the live summary ({note})")
    # files changed while the summary was made: not written, nothing left
    b = os.path.join(root, "run_041"); shutil.copytree(a, b); os.remove(os.path.join(b, "run_040_summary.json"))
    try:
        od.write_summary_file(b, dict(L, fingerprint={"files": 1, "bytes": 1}))
        good &= check(False, "files changed: written anyway")
    except OSError:
        pass
    good &= check(not os.path.exists(od.summary_path(b)) and not [f for f in os.listdir(b) if f.endswith(".tmp")],
                  "files changed: something left")
    # the child exits at once when the process that should be its parent is not (PDEATHSIG gap)
    t0 = time.time()
    rc = subprocess.run([sys.executable, os.path.join(ROOT, "online", "online_dashboard.py"), "--summarise", b, "--boards",
                         ",".join(map(str, SERIALS)), "--eight-channel-boards", "1443", "--config", "/nonexistent",
                         "--parent-pid", "1"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120).returncode
    good &= check(rc == 3 and not os.path.exists(od.summary_path(b)), f"parent check: the child exited {rc} ({time.time() - t0:.1f} s)")
    # never "finished" by the files' age while FSUDAQ said the run goes on (/run without /stopped)
    c = os.path.join(root, "run_042"); shutil.copytree(a, c); os.remove(os.path.join(c, "run_040_summary.json")); os.remove(os.path.join(c, "run_040_run.json"))
    set_old(c, 3600)
    g = od.Online(od.parse_args(["--run", c, "--boards", ",".join(map(str, SERIALS)), "--eight-channel-boards", "1443",
                                 "--workers", "0", "--config", "/nonexistent"]), clock=Clock())
    g.run_open = os.path.abspath(c)
    g.restart = True
    g.clock.t += 1; g.step()
    good &= check(not g.finished_at_start and not g.finished_guess, "guess: taken for finished while FSUDAQ said the run goes on")
    g.run_open = None; g.restart = True
    g.clock.t += 1; g.step()
    good &= check(g.finished_at_start and g.finished_guess and g._source() == "read_after_end",
                  f"guess: not taken for finished without /run, or labelled {g._source()}")
    # the rate history over a damaged far-off time tag: fast, left out, said so
    online.run_daq_bins[10 ** 5] = np.array([3, 0, 0], np.int64)
    t1 = time.time()
    hist, span, outside, far = online._run_history(online.t_first, online.t_first + 10 ** 6 * PS, int(online.t_first // PS))
    good &= check(time.time() - t1 < 1 and far == 3 and outside == 3 and span <= 20 + 1e-9 and len(hist["daq"]["t0_s"]) <= od.HISTORY_POINTS,
                  f"history: far-off bin {far} far, {outside} outside, span {span}, {time.time() - t1:.2f} s")
    # ... and in a summary: the mean rates over the run's own span, not to the far-off time
    newest = online.boards[15880].newest.copy()
    online.boards[15880].newest[0] = online.t_first + 10 ** 6 * PS
    doc = online._summary_doc("stopped")
    online.boards[15880].newest[:] = newest
    D = doc["data"]
    ch0 = doc["channels"][0]
    good &= check(D["span_s"] > 1e5 and D["rate_span_s"] <= 20 + 1e-9 and abs(ch0["mean_rate_hz"] - ch0["hits"] / D["rate_span_s"]) < 1e-6
                  and any(c["reason"] == "history_partial" and "far from the rest" in c["detail"] for c in doc["caveats"]),
                  f"rate span: span {D['span_s']}, rate span {D['rate_span_s']}, caveats {[c['reason'] for c in doc['caveats']]}")
    del online.run_daq_bins[10 ** 5]
    if good:
        ok("setup recorded (map sha256, window, identity); two restarts read_after_end with the live part kept and the setup known; "
           "another setup never replaces the live summary; changed files abort the write; the child checks its parent; no "
           "age-based end during /run; a far-off time tag left out of the history")


def scenario_summary_next_run_and_open(tmp):
    print("== the next run starting while the analysis is behind writes the live summary; backfill waits for a run open on disk")
    good = True
    root = os.path.join(tmp, "nr_data"); os.makedirs(root)
    a = os.path.join(root, "run_060"); os.makedirs(a)
    clock = Clock(); boards = boards_for(a, seed=161)
    online = make_online(a, SERIALS, EIGHT, clock)
    online.a.run = None; online.a.data_path = root; online.follow = a
    saved = od.INDEX_WORDS
    od.INDEX_WORDS = 60_000                   # behind: less is read per step than is written
    try:
        drive(online, clock, boards, 0, 10)
        good &= check(online.status.startswith("falling behind") and not os.path.exists(od.summary_path(a)), f"next run: {online.status}")
        bnext = os.path.join(root, "run_061"); os.makedirs(bnext)
        unread = od.run_fingerprint(a)["bytes"] - sum(fs.pos * 4 for bb in online.boards.values() for fs in bb.scans.values())
        online.run_started(bnext)
        clock.t += 1; online.step()
    finally:
        od.INDEX_WORDS = saved
    s = read_summary(a) if os.path.exists(od.summary_path(a)) else None
    good &= check(s is not None and s["source"] == "live" and not s["complete"]
                  and [x["reason"] for x in s["incomplete"]] == ["next_run_started"] and "not yet scanned" in s["incomplete"][0]["detail"]
                  and unread > 0 and s["incomplete"][0]["unread_bytes"] == unread and s["incomplete"][0]["bytes"] == s["data"]["bytes"]
                  and any(p["text"].startswith("the analysis is falling behind") for p in s["problems_during_reading"])
                  and s["run"]["end"] == "interrupted", f"next run: summary {s and (s['source'], s['incomplete'], s['run']['end'])}")
    # backfill: a run on disk without a stop, written 15 min ago, FSUDAQ running: open (also with no /run seen)
    c = os.path.join(root, "run_062"); os.makedirs(c)
    for x in boards_for(c, seed=162):
        x.hits(0, PS); x.flush(); x.f.close()
    write_run_file(c, stop=False)
    set_old(c, 900)
    for x in boards:
        x.f.close()                           # (what the boards still held is never written: the summary stays of these files)
    write_run_file(a); set_old(a, 7200)
    clock = Clock()
    o = od.Online(od.parse_args(["--data-path", root, "--boards", ",".join(map(str, SERIALS)), "--eight-channel-boards", "1443",
                                 "--workers", "0", "--config", "/nonexistent"]), clock=clock)
    try:
        for _ in range(4):
            clock.t += 1; o.step(); wait_tasks(o)
        o.fsudaq_probe = lambda: True
        o.open_cache = (0.0, None)
        why = o._busy_reason()
        good &= check(why is not None and "run_062 has no stop recorded" in why, f"open on disk: not seen ({why}, reading {o.folder})")
        o.fsudaq_probe = lambda: False
        o.open_cache = (0.0, None)
        good &= check(o._busy_reason() is None, "open on disk: FSUDAQ not running, still taken for open")
        o.fsudaq_probe = lambda: True
        set_old(c, od.OPEN_RUN_MAX_S + 600)
        o.open_cache = (0.0, None)
        good &= check(o._busy_reason() is None, "open on disk: nothing written for 12 h, still taken for open")
        # /run without /stopped, and FSUDAQ gone: no longer "taking run" for ever
        o.run_open, o.run_open_wall = os.path.abspath(c), time.time()
        good &= check("taking run run_062" in (o._busy_reason() or ""), "run open: not said")
        o.fsudaq_probe = lambda: False
        good &= check(o._busy_reason() is None and o.run_open is None, "run open: FSUDAQ gone, still taking the run")
        o.fsudaq_probe = lambda: True      # the /stopped missed, but the run file has the stop: not taken
        o.run_open, o.run_open_wall = os.path.abspath(c), time.time()
        write_run_file(c, stop=True)
        o.open_cache = (0.0, None)
        good &= check(o._busy_reason() is None and o.run_open is None, "run open: the run file has the stop, still taking the run")
        write_run_file(c, stop=False); set_old(c, od.OPEN_RUN_MAX_S + 600)
        # the incomplete live summary of run_060 is read again only with the same setup
        good &= check(o._bf_next() == a, f"re-read: not chosen with the same setup ({o.bf_notes})")
        o2 = od.Online(od.parse_args(["--data-path", root, "--boards", ",".join(map(str, SERIALS)), "--eight-channel-boards", "1443",
                                      "--workers", "0", "--config", "/nonexistent", "--window", "40"]), clock=Clock())
        good &= check(o2._bf_next() is None and "setup now differs" in o2.bf_notes.get(a, ""),
                      f"re-read: chosen with another bunch window ({o2.bf_notes})")
    finally:
        pass
    # a killed child's folder time is put back only if nothing else changed the folder
    folder = os.path.join(tmp, "nr_clean"); os.makedirs(folder)
    with open(os.path.join(folder, "x_001_1_PSD_2_000.fsu"), "wb") as f:
        f.write(b"\0" * 8)
    set_old(folder, 3600)
    st = os.stat(folder)

    class Gone:
        pid, returncode = 999999, -9

        def poll(self):
            return -9
    for changed in (False, True):
        bf = dict(folder=folder, proc=Gone(), mtime=(st.st_atime_ns, st.st_mtime_ns), names=set(os.listdir(folder)),
                  err=tempfile.TemporaryFile())
        if changed:                           # FSUDAQ started a run here meanwhile
            with open(os.path.join(folder, "x_001_1_PSD.bin"), "wb") as f:
                f.write(b"\0")
            bf["names"] = set(os.listdir(folder)) - {"x_001_1_PSD.bin"}
        with open(os.path.join(folder, f".{os.path.basename(od.summary_path(folder))}.999999.tmp"), "w") as f:
            f.write("{")
        os.utime(folder, None)
        now_ns = os.stat(folder).st_mtime_ns
        o._bf_cleanup(bf)
        restored = os.stat(folder).st_mtime_ns == st.st_mtime_ns
        good &= check(restored != changed, f"cleanup: folder time {'put back over a change' if changed else 'not put back'}")
    if good:
        ok("the run left for the next one got its live summary (incomplete, why, its problems); a run on disk without a stop "
           "holds the backfill while FSUDAQ runs, for 12 h at most; /run without /stopped expires when FSUDAQ is gone; "
           "a killed child's folder time is put back only over its own changes")


def scenario_summary_review2(tmp):
    print("== a look at another run during a live run writes nothing there; identity covers every input; stale live summaries")
    good = True
    root = os.path.join(tmp, "lk_data"); os.makedirs(root)
    b = os.path.join(root, "run_070"); os.makedirs(b)          # a recorded run to look at
    for x in boards_for(b, seed=171):
        x.hits(0, 2 * PS); x.flush(); x.f.close()
    write_run_file(b); set_old(b, 3600)
    a = os.path.join(root, "run_071"); os.makedirs(a)          # the run being taken; Digi-1443 writes nothing (a live problem)
    clock = Clock(); boards = boards_for(a, seed=172)
    quiet = [x for x in boards if x.serial != 1443]
    online = od.Online(od.parse_args(["--data-path", root, "--boards", ",".join(map(str, SERIALS)), "--eight-channel-boards", "1443",
                                      "--workers", "0", "--config", "/nonexistent", "--hold-s", "5"]), clock=clock)
    online.run_started(a)
    t = drive(online, clock, quiet, 0, 10)
    seen = [p["text"] for p in online.problems.values()]
    good &= check(any(x.startswith("Digi-1443: no data") for x in seen), f"look: no live problem to carry ({seen})")
    online.load(b)
    t = drive(online, clock, quiet, t, t + 3)
    time.sleep(2.1)                                            # the look lasts a while
    good &= check(online.folder == b and not os.path.exists(od.summary_path(a)) and not [f for f in os.listdir(a) if "summary" in f],
                  "look: a summary written into the open run's folder")
    online.live()
    t = drive(online, clock, quiet, t, t + 8)
    finish(online, clock, boards)
    s = read_summary(a)
    T = truth(boards, SERIALS)
    good &= check(s["source"] == "live" and s["complete"] and all(c["hits"] == len(T.get(c["slot"], [])) for c in s["channels"])
                  and any(p["text"].startswith("Digi-1443: no data") for p in s["problems_during_reading"])
                  and any(c["reason"] == "problems_unwatched" for c in s["caveats"]),
                  f"look: after it, {s['source']} {s['complete']} {[c['reason'] for c in s['caveats']]}")
    # /load of the run being taken itself: still live (it follows the run), with what was seen so far
    c = os.path.join(root, "run_072"); os.makedirs(c)
    clock = Clock(); boards = boards_for(c, seed=173)
    o = make_online(c, SERIALS, EIGHT, clock, extra=["--hold-s", "5"])
    o.a.run = None; o.a.data_path = root; o.follow = c
    t = drive(o, clock, [x for x in boards if x.serial != 1443], 0, 8)
    o.load(c)
    clock.t += 1; o.step()
    good &= check(o._source() == "live" and any(p["text"].startswith("Digi-1443: no data") for p in o.problems.values()),
                  f"look at itself: {o._source()}, problems {list(o.problems)}")
    # the identity changes with every input
    fd = os.path.join(tmp, "lk_finder"); shutil.copytree(od.ff.FINDER_DIR, fd, ignore=shutil.ignore_patterns("__pycache__"))
    saved_dir, saved_code = od.ff.FINDER_DIR, dict(od.CODE_HASHES)
    try:
        od.ff.FINDER_DIR = fd
        nch = {sn: (8 if sn in EIGHT else 16) for sn in SERIALS}
        ident = lambda: od.setup_identity("high", od.setup_files_record({}), 25000, SERIALS, nch)   # noqa: E731
        base, changed = ident(), []
        for f in od.FINDER_FILES + ("momentum_map.csv",):
            with open(os.path.join(fd, f), "ab") as fh:
                fh.write(b"\n")
            changed.append((f, ident() != base)); base = ident()
        od.CODE_HASHES["finder_fast.py"] = "0" * 64
        changed.append(("code", ident() != base)); base = ident()
        changed.append(("window", od.setup_identity("high", od.setup_files_record({}), 26000, SERIALS, nch) != base))
        changed.append(("setting", od.setup_identity("low", od.setup_files_record({}), 25000, SERIALS, nch) != base))
        good &= check(all(x for _, x in changed), f"identity: unchanged by {[f for f, x in changed if not x]}")
    finally:
        od.ff.FINDER_DIR = saved_dir
        od.CODE_HASHES.clear(); od.CODE_HASHES.update(saved_code)
    # a stale live summary (the files grew since): replaced by a later reading, which keeps the live part, marked
    L = read_summary(a)
    with open(os.path.join(a, "run_001_15880_PSD_2_000.fsu"), "ab") as fh:
        fh.write(b"\0" * 16)
    fp = od.run_fingerprint(a)
    for same_setup in (False, True):
        newer = dict(L, source="backfill", reading={"started_at": f"later {same_setup}"}, fingerprint=fp, setup_known=False,
                     setup=dict(L["setup"], identity=L["setup"]["identity"] if same_setup else "other"),
                     caveats=L["caveats"] + [{"reason": "setup_unknown", "detail": "x"}])
        newer.pop("previous_live", None)
        if same_setup:
            od._write_json_whole(a, od.summary_path(a), L)         # the live one back
        path, written, note = od.write_summary_file(a, newer)
        K = read_summary(a)
        P = K.get("previous_live") or {}
        good &= check(written and P.get("written_at") == L["written_at"] and P.get("files_changed") is True
                      and K["setup_known"] is same_setup and any(c["reason"] == "setup_unknown" for c in K["caveats"]) is not same_setup,
                      f"stale live: same setup {same_setup}: written {written}, previous_live {P.get('files_changed')}, known {K['setup_known']}")
    # a config JSON cannot hold, and the writer thread
    cfg = os.path.join(tmp, "lk_nan.json")
    with open(cfg, "w") as fh:
        fh.write('{"setting": "high", "note": NaN, "limit": Infinity}')
    d = os.path.join(root, "run_073"); os.makedirs(d)
    clock = Clock(); boards = boards_for(d, seed=174)
    o = make_online(d, SERIALS, EIGHT, clock, extra=["--config", cfg])
    o.write_async = True
    drive(o, clock, boards, 0, 4)
    finish(o, clock, boards)
    o.flush_writes()
    s = read_summary(d) if os.path.exists(od.summary_path(d)) else {}
    good &= check(s.get("setup", {}).get("config", {}).get("note") == "nan" and s["setup"]["config"]["limit"] == "inf"
                  and any(c["reason"] == "config_not_json" for c in s["caveats"]), f"NaN config / writer thread: {s.get('setup', {}).get('config')}")
    # summaries only from the production port, unless asked
    good &= check(od.parse_args(["--data-path", root, "--port", "8057"]).no_summaries
                  and not od.parse_args(["--data-path", root, "--port", "8057", "--summaries"]).no_summaries
                  and not od.parse_args(["--data-path", root]).no_summaries, "port guard")
    if good:
        ok("a look at another run during a live run writes nothing into it, the live reading goes on with its problems and says "
           "when it was not watched; /load of the run itself stays live; identity changes with every finder file, the map, the "
           "code, window and setting; a stale live summary is replaced with its live part kept and the setup known only if equal; "
           "a NaN config is kept as text; the writer thread writes; only port 8050 writes by default")


def expected_doc(map_sha, setting="high", field=0.2, finder=True, p0=200.0):
    """A small floor-tool-expected/1 file by hand (tool/README.md "Publish to online"): T0 with 3 channels."""
    edges = [float(v) for v in ff.P_EDGES]
    per, ee = [0.0] * (len(edges) - 1), [0.0] * (len(edges) - 1)
    per[20], per[21], per[22] = 0.2, 0.4, 0.2                  # 200-230 MeV/c
    ee[21] = 0.2                                               # half of the middle bin are electrons
    t0 = dict(plane=0, slot="T0", n_channels=3, channels=[1, 2, 3], live=[True] * 3, gang=[0, 0, 0],
              x_lo_mm=[0.0, 10.0, 20.0], x_hi_mm=[10.0, 20.0, 30.0], share=[0.5, 0.5, 0.0],
              hits_per_beam_particle={"total": [0.5, 0.5, 0.0]}, hz={"total": [5e4, 5e4, 0.0]},
              sum_hits_per_beam_particle=1.0, particles_with_a_hit_per_beam_particle=0.9)
    return {"format": "floor-tool-expected/1", "published_at": "2026-11-01T12:00:00+00:00", "tool_commit": "v1-2-gabc",
            "settings": dict(meaning="m", field_T=field, current_A=None, setting=setting, table_x_mm={"T0": 1.0}, p0_MeV=p0,
                             dp_p_fwhm=0.02, rate_hz=1e5, species_mode="psi", species={"pi-": 0.8, "mu-": 0.1, "e-": 0.1},
                             threshold_MeVee=0.1, n_beam=1000, seed=1),
            "map": dict(meaning="m", sha256="crlf", sha256_lf="tool-own", bytes=1, rows={}, field_T=field,
                        finder_map=dict(path="/finder/momentum_map.csv", sha256_lf=map_sha, field_T=field) if finder else None),
            "planes": {"meaning": "expected hits per channel", "T0": t0},
            "momentum": dict(meaning="what hist_p should hold", events="1000 simulated", bin_edges_MeV=edges,
                             kinds=["pi-", "mu-", "e-", "mu- (pi decay)"],
                             read_with_finder_map=dict(per_beam_particle=per, by_kind={"e-": ee}, mean_MeV=214.0, sd_MeV=8.0)),
            "warnings": ["transport maps built with a different field"], "setup": {}}


def scenario_analysis_snapshot(tmp):
    print("== a run being taken keeps the analysis files it is read with, once; a run recorded over gets its own")
    folder = os.path.join(tmp, "snap_001"); os.makedirs(folder)
    cfg, exp = os.path.join(tmp, "snap_experiment.json"), os.path.join(tmp, "snap_expected.json")
    with open(cfg, "w") as f:
        json.dump({"window_ns": 25.0}, f)
    with open(exp, "w") as f:
        f.write('{"format": "floor-tool-expected/1", "n": 1}')
    write_run_file(folder, stop=False)
    clock = Clock(); boards = boards_for(folder, seed=5)
    online = make_online(folder, SERIALS, EIGHT, clock, extra=["--config", cfg, "--beam-expected", exp])
    t = drive(online, clock, boards, 0, 4)
    snap = os.path.join(folder, "snap_001_analysis")
    man = os.path.join(snap, "manifest.json")
    good = check(os.path.isfile(man), "snapshot: no manifest")
    if not good:
        return
    m = json.load(open(man))
    want = {"experiment.json", "expected.json", "momentum_map.csv", "finder/channels.csv"}
    good &= check(want <= set(m["files"]) and m["run_start"] == "2026-10-31T12:00:00.000+01:00",
                  f"snapshot: files {sorted(m['files'])}, run start {m['run_start']}")
    good &= check(open(os.path.join(snap, "expected.json")).read() == open(exp).read()
                  and m["files"]["expected.json"]["sha256"] == od.file_sha256(exp), "snapshot: expected.json not the one read")
    good &= check(all(not (os.stat(os.path.join(snap, k)).st_mode & 0o222) for k in m["files"]), "snapshot: a copy is writable")
    with open(exp, "w") as f:                    # the floor tool publishes again; the run is read again (same start)
        f.write('{"format": "floor-tool-expected/1", "n": 2}')
    online.run_started(folder)
    t = drive(online, clock, boards, t, t + 3)
    good &= check('"n": 1' in open(os.path.join(snap, "expected.json")).read(), "snapshot: replaced on a re-read of the same run")
    run = json.load(open(os.path.join(folder, "snap_001_run.json")))   # recorded again under the same number
    run["start"]["time"] = "2026-10-31T13:00:00.000+01:00"
    with open(os.path.join(folder, "snap_001_run.json"), "w") as f:
        json.dump(run, f)
    online.run_started(folder)
    t = drive(online, clock, boards, t, t + 3)
    good &= check('"n": 2' in open(os.path.join(snap, "expected.json")).read()
                  and json.load(open(man))["run_start"] == "2026-10-31T13:00:00.000+01:00", "snapshot: a run recorded over kept the old one")
    write_run_file(folder, stop=True)
    finish(online, clock, boards)
    s = json.load(open(od.summary_path(folder)))
    good &= check(s["settings"].get("analysis") == "snap_001_analysis", f"snapshot: not in the summary ({s['settings'].get('analysis')})")
    if good:
        ok(f"{len(m['files'])} files kept read-only with their manifest; kept on a re-read, renewed for a run recorded over; in the summary")


def scenario_beam_vs_map(tmp):
    print("== Beam vs map: the floor tool's published expectation against the live finder")
    import hashlib
    good = True
    edges = [float(v) for v in ff.P_EDGES]
    hp = [0] * (len(edges) - 1)
    hp[20], hp[21], hp[22] = 1, 1, 2
    live = dict(setting="high", setting_given=True, map="/finder/momentum_map.csv", map_sha256_lf="abc", map_field_T=0.2,
                placeholder=True, cherenkov=True, occupancy=[[0, 1, 2, 1], [0], [0]], hist_p=hp, p_edges=edges)
    C = od.compare_beam(expected_doc("abc"), live)
    T = C["planes"]["T0"]
    close = lambda a, b: a is not None and abs(a - b) < 1e-9   # noqa: E731
    # by hand: live shares 1/4 1/2 1/4 at channels 1 2 3 (x 5 15 25 mm); expected 1/2 1/2 0
    good &= check(not C["mismatch"] and T["live_share"] == [0.25, 0.5, 0.25] and close(T["centroid_ch"]["live"], 2.0)
                  and close(T["width_ch"]["live"], 0.5 ** 0.5) and close(T["centroid_ch"]["expected"], 1.5) and close(T["width_ch"]["expected"], 0.5)
                  and close(T["centroid_mm"]["live"], 15.0) and close(T["width_mm"]["live"], 50 ** 0.5)
                  and close(T["centroid_mm"]["expected"], 10.0) and close(T["width_mm"]["expected"], 5.0) and close(T["overlap"], 0.75),
                  f"bvm: plane shape {T}")
    # momentum, Cherenkov cabled: 0.2 0.4-0.2 0.2 -> a third each: mean 215, sd sqrt(200/3); live 1 1 2 of 4: mean 217.5, sd sqrt(68.75)
    M = C["momentum"]
    good &= check(all(close(a, b) for a, b in zip(M["expected"][20:23], [1 / 3] * 3)) and close(sum(M["expected"]), 1.0)
                  and close(M["mean"]["expected"], 215.0) and close(M["sd"]["expected"], (200 / 3) ** 0.5)
                  and close(M["mean"]["live"], 217.5) and close(M["sd"]["live"], 68.75 ** 0.5) and close(M["mean"]["diff"], 2.5)
                  and "25.0 %" in M["electron_correction"], f"bvm: momentum {M['mean']} {M['sd']} {M['electron_correction']}")
    M = od.compare_beam(expected_doc("abc"), dict(live, cherenkov=False))["momentum"]
    good &= check(close(M["mean"]["expected"], 215.0) and close(M["sd"]["expected"], 50 ** 0.5) and "not cabled" in M["electron_correction"],
                  f"bvm: no Cherenkov, no correction: {M['sd']}")
    # mismatches: another map, setting, field, no finder map named
    for doc, word in ((expected_doc("other"), "not the one"), (expected_doc("abc", setting="low"), "setting 'low'"),
                      (expected_doc("abc", field=0.25), "0.25 T"), (expected_doc("abc", finder=False), "did not name")):
        mm = od.compare_beam(doc, live)["mismatch"]
        good &= check(len(mm) == 1 and word in mm[0], f"bvm: mismatch for {word}: {mm}")
    good &= check(any("placeholder" in n for n in C["notes"]), "bvm: placeholder cabling not said")
    # the file: missing, not JSON, another format, valid, changed
    path = os.path.join(tmp, "published", "expected.json")
    E = od.BeamExpected(path)
    good &= check(E.get()[0] is None and "nothing published yet" in E.get()[1], f"bvm: missing file: {E.get()}")
    os.makedirs(os.path.dirname(path))
    for text, word in (("{ not json", "cannot be used"), (json.dumps({"format": "other/1"}), "not a floor-tool-expected/1")):
        with open(path, "w") as f:
            f.write(text)
        good &= check(E.get()[0] is None and word in (E.get()[1] or ""), f"bvm: bad file {word}: {E.get()[1]}")
    mp = os.path.join(od.ff.FINDER_DIR, "momentum_map.csv")
    with open(mp, "rb") as f:
        sha = hashlib.sha256(f.read().replace(b"\r\n", b"\n")).hexdigest()
    with open(path, "w") as f:
        json.dump(expected_doc(sha), f)
    good &= check(E.get()[0] is not None and E.get()[1] is None, f"bvm: valid file refused: {E.get()[1]}")
    with open(path, "w") as f:
        json.dump(expected_doc(sha, p0=250.5), f)
    good &= check(E.get()[0]["settings"]["p0_MeV"] == 250.5, "bvm: a changed file not read again")
    # GET /expected for anyone, against a live finder: its map recognised (the same sha256 after CRLF -> LF)
    folder = os.path.join(tmp, "bvm_run"); os.makedirs(folder)
    clock = Clock(); boards = boards_for(folder, seed=181)
    online = make_online(folder, SERIALS, EIGHT, clock, extra=["--beam-expected", path])
    drive(online, clock, boards, 0, 4)
    od.Handler.online = online

    class Remote(od.Handler):
        def local(self):
            return False
    srv = od.ThreadingHTTPServer(("127.0.0.1", 0), Remote)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def get(url):
        try:
            with urllib.request.urlopen(base + url) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as ex:
            return ex.code, json.loads(ex.read())
    try:
        code, d = get("/expected")
        v_code, v = get("/expected_view")                  # this publish has no top view
        with open(path, "w") as f:
            json.dump(dict(expected_doc(sha, p0=250.5), top_view={"view": {"z_lo": 0}}), f)
        v2_code, v2 = get("/expected_view")
        has_view = get("/expected")[1]["published"]["has_view"]
    finally:
        srv.shutdown()
    # where the accepted tracks went: one count per map triplet, every accepted track once, most frequent first,
    # with the finder's own channel positions
    A = online.acc
    codes = [tuple(c) for c in online.setup.tables.T["map"]]
    n_acc = int((A.h_ntrk * np.arange(ff.NTRK)).sum())
    good &= check(d["tracks"]["total"] == int(A.h_trip.sum()) == n_acc and A.h_trip.size == len(codes),
                  f"bvm: accepted {n_acc}, counted on triplets {d['tracks']['total']}")
    with online.lock:                                  # known counts on three triplets (the synthetic run accepts none)
        A.h_trip[[5, 9, 2]] += [3, 7, 1]
    tr = online.beam_vs_map()["tracks"]
    rows = tr["rows"] or []
    good &= check([tuple(r[:3]) for r in rows[:3]] == [codes[9], codes[5], codes[2]] and [r[3] for r in rows[:3]] == [int(A.h_trip[9]), int(A.h_trip[5]), int(A.h_trip[2])],
                  f"bvm: triplet order {rows[:3]}")
    good &= check(tr["error"] is None and tr["total"] == int(A.h_trip.sum()) and tr["distinct"] == len(rows)
                  and tr["shown"] == tr["total"] and all(tuple(r[:3]) in codes for r in rows)
                  and [r[3] for r in rows] == sorted((r[3] for r in rows), reverse=True)
                  and all(r[4] == round(online.setup.tables.map_p[codes.index(tuple(r[:3]))], 1) for r in rows)
                  and len(tr["planes"]) == 3 and all(q["ch"] and all(lo < hi for _, lo, hi in q["ch"]) for q in tr["planes"])
                  and [q["s_mm"] for q in tr["planes"]] == list(online.setup.tables.T["s_mm"]),
                  f"bvm: live tracks {tr['total']} of {n_acc} accepted, {tr['distinct']} triplets, {tr['error']}")
    good &= check(v_code == 404 and "no top view" in v["note"] and v2_code == 200 and v2["top_view"] == {"view": {"z_lo": 0}}
                  and v2["published_at"] and has_view is True, f"bvm: /expected_view {v_code} {v} {v2_code} {has_view}")
    good &= check(code == 200 and d["live"]["map_sha256_lf"] == sha and d["published"]["settings"]["p0_MeV"] == 250.5
                  and not d["compare"]["mismatch"] and d["compare"]["planes"]["T0"]["note"].startswith("the live finder has 8 channels")
                  and d["compare"]["momentum"] is not None, f"bvm: /expected {code} {d.get('error')} {d.get('compare', {}).get('mismatch')}")
    os.remove(path)
    good &= check(online.beam_vs_map()["published"] is None and "nothing published" in online.beam_vs_map()["error"], "bvm: removed file")
    if good:
        ok("shapes, centroids, widths (channels and mm), overlap and the electron-corrected momentum match the hand-computed "
           "values; another map, setting, field or no finder map are flagged; missing, broken, foreign, valid and changed files; "
           "/expected open to all, the live map recognised by its LF sha256; every accepted track counted once on its map "
           "triplet, drawn at the finder's channel positions; /expected_view only with a top view")


def main():
    tmp = tempfile.mkdtemp(prefix="fsudaq_test_online_")
    try:
        for sc in (scenario_steady, scenario_slow, scenario_pause, scenario_damage_rollover, scenario_run_switch,
                   scenario_live_start, scenario_offsets, scenario_replay, scenario_short_hold, scenario_worker_killed,
                   scenario_idle_worker_killed, scenario_falling_behind, scenario_counters, scenario_finished_at_start,
                   scenario_long_pause, scenario_offsets_dense, scenario_missing_board, scenario_boundaries, scenario_x725,
                   scenario_window, scenario_run_file, scenario_view_only, scenario_rollover_backlog, scenario_quiet_live_start,
                   scenario_summary_at_stop, scenario_summary_incomplete, scenario_backfill, scenario_summary_route,
                   scenario_backfill_reread, scenario_summary_setup_and_restarts, scenario_summary_next_run_and_open,
                   scenario_summary_review2, scenario_beam_vs_map, scenario_analysis_snapshot):
            sc(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{'FAILED' if N_FAIL else 'PASSED'}: {N_FAIL} problem(s)")
    return 1 if N_FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
