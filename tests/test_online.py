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
  start-up; a pause longer than --hold-s; a board without data; hits exactly at a slice boundary.

    python3 tests/test_online.py
"""
import os
import shutil
import signal
import struct
import sys
import tempfile
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "online"))
import finder_fast as ff                 # noqa: E402
import online_dashboard as od            # noqa: E402

PS = od.PS
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

    def __init__(self, folder, run, serial, n_ch, rates, per_agg=64, roll_bytes=None, opt=2, seed=0, first_index=0):
        self.folder, self.run, self.serial, self.n_ch = folder, run, serial, n_ch
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
        return os.path.join(self.folder, f"run_{self.run:03d}_{self.serial}_PSD_2_{index:03d}.fsu")

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
                tick, rem = divmod(int(t), TICK)
                fine = rem * 1024 // TICK
                t = tick * TICK + fine * TICK // 1024         # what the decoder makes of it
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
        if damage:                            # a read that ended short: the header promises more than follows
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
    good &= check(any("damaged" in h for h in st["health"]) and any("format" in h for h in st["health"]), "damage: not on the page")
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
        os.remove(os.path.join(folder, f))                        # FSUDAQ removes the old files
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
    good &= check(any("1443" in h and "missing" in h for h in st["health"]), "counters: the lost aggregates are not on the page")
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
    good = check(st["status"].startswith("last run (finished") and not st["live_start"], f"finished: status '{st['status']}'")
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
    after = od.slice_task(dict(t0=T, t1=T + PS // 1000, margin=m, files=files, ref=None))["owned"]
    before = od.slice_task(dict(t0=T - PS // 1000, t1=T, margin=m, files=files, ref=None))["owned"]
    good &= check(after == 2 and before == 2, f"boundaries: slices own {before} before T and {after} from T, expected 2 and 2")
    if good:
        ok("a hit at the boundary is the later slice's, not late; one tick before it is late; offsets removed first")


def main():
    tmp = tempfile.mkdtemp(prefix="fsudaq_test_online_")
    try:
        for sc in (scenario_steady, scenario_slow, scenario_pause, scenario_damage_rollover, scenario_run_switch,
                   scenario_live_start, scenario_offsets, scenario_replay, scenario_short_hold, scenario_worker_killed,
                   scenario_idle_worker_killed, scenario_falling_behind, scenario_counters, scenario_finished_at_start,
                   scenario_long_pause, scenario_offsets_dense, scenario_missing_board, scenario_boundaries):
            sc(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{'FAILED' if N_FAIL else 'PASSED'}: {N_FAIL} problem(s)")
    return 1 if N_FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
