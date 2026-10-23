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
  mid-run, a worker process killed mid-run.

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


def drive(online, clock, boards, t_from, t_to, step_s=1.0, gen=True, settle=False):
    """Run the boards and the dashboard from t_from to t_to (s of data time = wall time)."""
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
    return t


def wait_tasks(online, timeout=120):
    t_end = time.time() + timeout
    while online.pending and time.time() < t_end:
        time.sleep(0.05)
        online.step()


def finish(online, clock, boards):
    """Stop the boards (they write what they hold) and let the dashboard process everything."""
    for b in boards:
        b.flush()
    for _ in range(20):
        clock.t += 1.0
        online.step()
        wait_tasks(online)


def truth(boards, serials):
    """slot -> (ts, qlong) arrays of everything written readable."""
    out = {}
    for b in boards:
        bi = serials.index(b.serial)
        for t, ch, ql, qs in b.truth:
            out.setdefault(bi * 16 + ch, []).append((t, ql))
    return out


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
    total = sum(len(b.truth) for b in boards)
    in_finder, late = int(online.owned), int(online.daq.late.sum())
    check(in_finder + late == total, f"{what}: finder {in_finder:,} + late {late:,} != written {total:,}")
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
    if good:
        ok(f"{tot:,} hits: counts and pulse heights exact, all in the finder, finder identical to one pass")


def scenario_slow(tmp):
    print("== a slow channel (its couple writes every ~20 s): late hits counted, none lost or twice")
    folder = os.path.join(tmp, "slow"); os.makedirs(folder)
    clock = Clock()
    boards = boards_for(folder, slow=[(15880, 4, 1.5), (15880, 5, 1.5), (1443, 6, 0.0), (1443, 7, 3.0)])
    online = make_online(folder, SERIALS, EIGHT, clock, extra=["--hold-s", "8"])
    drive(online, clock, boards, 0, 60)
    finish(online, clock, boards)
    good = check_daq(online, boards, SERIALS, "slow")
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
    check(online.status.startswith("run ended or paused"), f"pause: status '{online.status}'")
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
    online.follow = folder2; online.restart = True
    t = drive(online, clock, boards2, t, t + 8)
    finish(online, clock, boards2)
    good &= check_daq(online, boards2, SERIALS, "new folder")
    check_partition(online, boards2, "new folder")
    if good:
        ok("each run counted alone: the replaced files, then the new folder")


def scenario_live_start(tmp):
    print("== the dashboard started mid-run: what was on disk is skipped, the rest is exact")
    folder = os.path.join(tmp, "live"); os.makedirs(folder)
    clock = Clock(); boards = boards_for(folder)
    for t in range(10):
        for b in boards:
            b.hits(t * PS, (t + 1) * PS)
    before = {id(b): len(b.truth) for b in boards}
    online = od.Online(od.parse_args(["--run", folder, "--boards", ",".join(map(str, SERIALS)), "--eight-channel-boards", "1443",
                                      "--workers", "0", "--config", "/nonexistent"]), clock=clock)
    clock.t += 10
    online.step()
    drive(online, clock, boards, 10, 20)
    finish(online, clock, boards)
    for b in boards:
        b.truth = b.truth[before[id(b)]:]
    good = check_daq(online, boards, SERIALS, "live start")
    check_partition(online, boards, "live start")
    good &= check(online.state()["live_start"], "live start: not said on the page")
    if good:
        ok("counts exactly the hits written after the start; the page says it started mid-run")


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
    if good:
        ok(f"{tot:,} hits over {sum(len(b.scans) for b in online.boards.values())} files replayed: DAQ exact, finder + late = all")


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


def main():
    tmp = tempfile.mkdtemp(prefix="fsudaq_test_online_")
    try:
        for sc in (scenario_steady, scenario_slow, scenario_pause, scenario_damage_rollover, scenario_run_switch,
                   scenario_live_start, scenario_offsets, scenario_replay, scenario_short_hold, scenario_worker_killed,
                   scenario_idle_worker_killed):
            sc(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{'FAILED' if N_FAIL else 'PASSED'}: {N_FAIL} problem(s)")
    return 1 if N_FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
