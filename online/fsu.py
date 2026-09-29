#!/usr/bin/env python3
"""Decoder for FSUDAQ ``*.fsu`` files (raw CAEN x730 DPP-PSD aggregate stream).

An ``.fsu`` file is a straight ``fwrite`` of the CAEN readout buffer: a
sequence of *board aggregates*, each holding one *couple aggregate* per
enabled channel pair, each holding fixed-size events.  No wrapper, no
FSUDAQ header.  Layout (32-bit little-endian words), as decoded by
FSUDAQ's ``Data::DecodeBuffer`` / ``DecodePSDDualChannelBlock``:

board aggregate
    w0  [31:28]=0xA  [27:0]=size in words incl. header
    w1  [31:27]=board id  [26]=fail  [22:8]=pattern  [7:0]=couple mask
    w2  [22:0]=board aggregate counter
    w3  board aggregate time tag
couple aggregate (one per set bit in the couple mask, ascending)
    w0  [31]=1  [21:0]=size in words incl. its 2 header words
    w1  [31]=dual trace [30]=charge [29]=timestamp [28]=extras [27]=waveform
        [26:24]=extras option  [15:0]=samples/8 (waveform only)
    events, each  [TTT|ch] [waveform...] [extras] [charge]
        TTT word    [31]=channel within couple  [30:0]=trigger time tag (2 ns ticks)
        extras (opt 2) [31:16]=extended TTT  [15:10]=flags  [9:0]=fine time (1/1024 tick)
        charge      [31:16]=Qlong  [15]=PUR (pile-up / saturation)  [14:0]=Qshort

Unlike FSUDAQ's own decoder, which drops events with PUR set, this keeps
every event on disk (matching CoMPASS's ``Output + Saturation``).

Two passes: a light pure-Python walk that indexes every couple aggregate,
then a vectorised numpy decode of all events in memory-bounded chunks.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, field

import numpy as np

TICK_PS = 2000          # x730: 500 MS/s, one trigger-time-tag tick = 2 ns
FINE_DIV = 1024         # 10-bit fine time stamp spans one tick
AGG_COUNTER_MOD = 1 << 23
CHUNK_EVENTS = 20_000_000

EVENT_DTYPE = np.dtype([
    ("channel", np.uint8),
    ("ts_ps", np.int64),
    ("qlong", np.uint16),
    ("qshort", np.uint16),
    ("pur", np.uint8),
    ("flags", np.uint8),      # extras bits [15:10], shifted down
    ("agg", np.uint32),       # board aggregate counter the event came in
])

# extras-word flag bits (option 2), relative to bit 10
FLAG_LOST_TRIGGER_COUNTED = 1 << 2   # bit 12
FLAG_1024_TRIGGERS_COUNTED = 1 << 3  # bit 13
FLAG_OVER_RANGE = 1 << 4             # bit 14
FLAG_TRIGGER_LOST = 1 << 5           # bit 15


@dataclass
class FileIndex:
    """Where every couple aggregate's events live, plus board-level metadata."""
    ev_start: np.ndarray      # word index of first event in each couple aggregate
    nev: np.ndarray
    couple: np.ndarray
    agg_counter: np.ndarray   # board aggregate counter for each couple aggregate
    wpe: np.ndarray           # words per event
    has_extra: np.ndarray
    counters: np.ndarray      # board aggregate counters, in file order
    board_ids: set
    n_aggregates: int
    malformed: int
    truncated_words: int


@dataclass
class FileSummary:
    path: str
    serial: int
    nbytes: int
    n_aggregates: int = 0
    n_events: int = 0
    n_pur: int = 0
    n_1024_markers: int = 0
    n_lost_trigger_flags: int = 0
    n_overrange_flags: int = 0
    n_lost_counted_flags: int = 0
    counter_first: int = -1
    counter_last: int = -1
    counter_gaps: int = 0
    counter_missing: int = 0
    malformed_couples: int = 0
    truncated_words: int = 0
    per_channel: np.ndarray = field(default_factory=lambda: np.zeros(16, np.int64))
    per_channel_pur: np.ndarray = field(default_factory=lambda: np.zeros(16, np.int64))
    per_channel_1024: np.ndarray = field(default_factory=lambda: np.zeros(16, np.int64))
    ts_first: int = 1 << 62
    ts_last: int = 0
    board_ids: set = field(default_factory=set)
    run: int = -1
    record: "RunRecord | None" = None   # start/stop time and comments from RunTimeStamp.csv, if found

    @property
    def span_s(self) -> float:
        return (self.ts_last - self.ts_first) / 1e12 if self.n_events else 0.0


def serial_from_name(path: str) -> int:
    m = re.search(r"_(\d+)_PSD_\d+_\d+\.fsu$", os.path.basename(path))
    return int(m.group(1)) if m else -1


def run_from_name(path: str) -> int:
    """Run number from an FSUDAQ file name <prefix>_<run>_<serial>_<DPP>[_<tick>_<idx>].(fsu|bin)."""
    m = re.search(r"_(\d{3,})_\d+_(?:PHA|PSD|QDC)(?:_\d+_\d+\.fsu|\.bin)$", os.path.basename(path))
    return int(m.group(1)) if m else -1


# ---- run records written by FSUDAQ::WriteRunTimestamp (RunTimeStamp.csv in the data path)

RUN_TS = r"\d{4}\.\d{2}\.\d{2} \d{2}:\d{2}:\d{2}"
# one row per run: run,start time,start comment[,stop time,stop comment]; the comments are
# unquoted and may themselves contain commas, so split on the time stamps rather than on commas
_RUN_ROW = re.compile(rf"^(\d+),({RUN_TS}),(.*?)(?:,({RUN_TS}),(.*))?$")
_COMMENT_PREFIX = re.compile(r"^(Start|Stop) Comment:\s*")


@dataclass
class RunRecord:
    run: int
    start: str = ""            # "YYYY.MM.DD hh:mm:ss", local time of the DAQ host
    start_comment: str = ""
    stop: str = ""
    stop_comment: str = ""

    def __str__(self) -> str:
        s = f"run {self.run}  start {self.start or '?'}  \"{self.start_comment}\""
        if self.stop:
            s += f"  stop {self.stop}  \"{self.stop_comment}\""
        return s


def read_run_records(csv_path: str) -> dict[int, RunRecord]:
    """Parse RunTimeStamp.csv; the last row for a run number wins (a run restarted after a crash)."""
    records: dict[int, RunRecord] = {}
    with open(csv_path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = _RUN_ROW.match(line.rstrip("\n"))
            if not m:
                continue
            run = int(m.group(1))
            records[run] = RunRecord(
                run=run, start=m.group(2), start_comment=_COMMENT_PREFIX.sub("", m.group(3) or ""),
                stop=m.group(4) or "", stop_comment=_COMMENT_PREFIX.sub("", m.group(5) or ""))
    return records


def find_run_record(path: str, run: int | None = None) -> RunRecord | None:
    """The run record for a data file: RunTimeStamp.csv is looked for in the file's folder and,
    for the per-run folder layout <data path>/<prefix>_<run>/, in the parent folder."""
    if run is None:
        run = run_from_name(path)
    if run < 0:
        return None
    folder = os.path.dirname(os.path.abspath(path))
    for candidate in (folder, os.path.dirname(folder)):
        csv_path = os.path.join(candidate, "RunTimeStamp.csv")
        if os.path.isfile(csv_path):
            rec = read_run_records(csv_path).get(run)
            if rec is not None:
                return rec
    return None


def index_file(words: np.ndarray) -> FileIndex:
    """Pass 1: walk board and couple aggregate headers (pure Python, ~1 µs/word read)."""
    n = len(words)
    pos = 0
    ev_start, nev, couple_l, agg_l, wpe_l, extra_l, counters = [], [], [], [], [], [], []
    board_ids = set()
    n_agg = malformed = 0
    w = words  # local alias
    while pos < n:
        w0 = int(w[pos])
        if (w0 >> 28) != 0xA:
            raise ValueError(f"expected board-aggregate header at word {pos}, got 0x{w0:08X}")
        size = w0 & 0x0FFFFFFF
        if size < 4 or pos + size > n:
            break  # truncated tail: file closed mid-write
        w1 = int(w[pos + 1]); ctr = int(w[pos + 2]) & 0x7FFFFF
        mask = w1 & 0xFF
        board_ids.add(w1 >> 27); counters.append(ctr); n_agg += 1
        p = pos + 4; end = pos + size
        for c in range(8):
            if not (mask >> c) & 1:
                continue
            if p + 2 > end:
                malformed += 1; break
            c0 = int(w[p]); c1 = int(w[p + 1])
            if not (c0 >> 31):
                malformed += 1; break
            csize = c0 & 0x3FFFFF
            has_wave = (c1 >> 27) & 1; has_extra = (c1 >> 28) & 1
            has_ts = (c1 >> 29) & 1; has_chg = (c1 >> 30) & 1
            nsw = ((c1 & 0xFFFF) * 8) // 2 if has_wave else 0
            wpe = has_ts + nsw + has_extra + has_chg
            body = csize - 2
            if wpe == 0 or body % wpe or p + csize > end:
                malformed += 1; p += csize; continue
            k = body // wpe
            if k:
                ev_start.append(p + 2); nev.append(k); couple_l.append(c)
                agg_l.append(ctr); wpe_l.append(wpe); extra_l.append(has_extra)
            p += csize
        pos += size
    return FileIndex(
        ev_start=np.asarray(ev_start, np.int64), nev=np.asarray(nev, np.int64),
        couple=np.asarray(couple_l, np.int64), agg_counter=np.asarray(agg_l, np.int64),
        wpe=np.asarray(wpe_l, np.int64), has_extra=np.asarray(extra_l, np.int64),
        counters=np.asarray(counters, np.int64), board_ids=board_ids,
        n_aggregates=n_agg, malformed=malformed, truncated_words=n - pos)


def _decode_chunk(words, ix: FileIndex, lo: int, hi: int):
    """Vectorised decode of couple aggregates ix[lo:hi]. Returns dict of arrays."""
    nev = ix.nev[lo:hi]
    tot = int(nev.sum())
    if tot == 0:
        return None
    rep = lambda a: np.repeat(a[lo:hi], nev)
    start = rep(ix.ev_start); wpe = rep(ix.wpe); couple = rep(ix.couple)
    agg = rep(ix.agg_counter); has_extra = rep(ix.has_extra)
    off = np.arange(tot, dtype=np.int64) - np.repeat(np.cumsum(nev) - nev, nev)
    idx = start + off * wpe
    ttt_w = words[idx].astype(np.int64)
    chg_w = words[idx + wpe - 1].astype(np.int64)
    # extras word sits immediately before the charge word when present
    extra_w = np.where(has_extra == 1, words[np.minimum(idx + wpe - 2, len(words) - 1)].astype(np.int64), 0)
    ch = (couple * 2 + (ttt_w >> 31)).astype(np.uint8)
    ttt = ttt_w & 0x7FFFFFFF
    ext = extra_w >> 16
    fine = extra_w & 0x3FF
    flags = ((extra_w >> 10) & 0x3F).astype(np.uint8)
    ts_ps = ((ext << 31) | ttt) * TICK_PS + (fine * TICK_PS) // FINE_DIV
    pur = ((chg_w >> 15) & 1).astype(np.uint8)
    return dict(ch=ch, ts_ps=ts_ps, flags=flags, pur=pur, agg=agg,
                qlong=(chg_w >> 16).astype(np.uint16), qshort=(chg_w & 0x7FFF).astype(np.uint16))


def decode_file(path: str, keep_events: bool = False, progress: bool = False):
    """Decode one .fsu file. Returns (FileSummary, events-or-None)."""
    words = np.memmap(path, dtype="<u4", mode="r")
    summ = FileSummary(path=path, serial=serial_from_name(path), nbytes=words.nbytes,
                       run=run_from_name(path))
    summ.record = find_run_record(path, summ.run)
    if progress:
        print(f"  {os.path.basename(path)}: indexing...", end="", file=sys.stderr, flush=True)
    ix = index_file(words)
    summ.n_aggregates = ix.n_aggregates; summ.malformed_couples = ix.malformed
    summ.truncated_words = ix.truncated_words; summ.board_ids = ix.board_ids
    if len(ix.counters):
        summ.counter_first = int(ix.counters[0]); summ.counter_last = int(ix.counters[-1])
        d = (np.diff(ix.counters) - 1) % AGG_COUNTER_MOD
        summ.counter_gaps = int(np.count_nonzero(d)); summ.counter_missing = int(d.sum())
    if progress:
        print(f" {ix.n_aggregates:,} aggregates, {int(ix.nev.sum()):,} events; decoding...", end="", file=sys.stderr, flush=True)
    chunks = []
    # chunk boundaries on couple aggregates so each chunk has <= CHUNK_EVENTS events
    cum = np.cumsum(ix.nev)
    bounds = [0]
    while bounds[-1] < len(ix.nev):
        target = cum[bounds[-1] - 1] + CHUNK_EVENTS if bounds[-1] else CHUNK_EVENTS
        nxt = int(np.searchsorted(cum, target, side="right"))
        bounds.append(max(nxt, bounds[-1] + 1))
    for lo, hi in zip(bounds[:-1], bounds[1:]):
        d = _decode_chunk(words, ix, lo, hi)
        if d is None:
            continue
        n = len(d["ch"])
        summ.n_events += n
        summ.n_pur += int(d["pur"].sum())
        m1024 = (d["flags"] & FLAG_1024_TRIGGERS_COUNTED) != 0
        summ.n_1024_markers += int(m1024.sum())
        summ.n_lost_trigger_flags += int(((d["flags"] & FLAG_TRIGGER_LOST) != 0).sum())
        summ.n_overrange_flags += int(((d["flags"] & FLAG_OVER_RANGE) != 0).sum())
        summ.n_lost_counted_flags += int(((d["flags"] & FLAG_LOST_TRIGGER_COUNTED) != 0).sum())
        summ.per_channel += np.bincount(d["ch"], minlength=16)[:16]
        summ.per_channel_pur += np.bincount(d["ch"], weights=d["pur"], minlength=16)[:16].astype(np.int64)
        summ.per_channel_1024 += np.bincount(d["ch"], weights=m1024, minlength=16)[:16].astype(np.int64)
        summ.ts_first = min(summ.ts_first, int(d["ts_ps"].min()))
        summ.ts_last = max(summ.ts_last, int(d["ts_ps"].max()))
        if keep_events:
            rec = np.empty(n, EVENT_DTYPE)
            rec["channel"] = d["ch"]; rec["ts_ps"] = d["ts_ps"]; rec["qlong"] = d["qlong"]
            rec["qshort"] = d["qshort"]; rec["pur"] = d["pur"]; rec["flags"] = d["flags"]; rec["agg"] = d["agg"]
            chunks.append(rec)
    if progress:
        print(" done", file=sys.stderr)
    return summ, (np.concatenate(chunks) if chunks else None)


def print_summary(s: FileSummary, wall_s: float | None = None):
    print(f"\n{os.path.basename(s.path)}   s/n {s.serial}   {s.nbytes / 1e6:.1f} MB")
    if s.record is not None:
        print(f"  {s.record}")
    print(f"  aggregates {s.n_aggregates:,}   counter {s.counter_first} -> {s.counter_last}   "
          f"gaps {s.counter_gaps}  missing {s.counter_missing}   malformed couples {s.malformed_couples}   "
          f"truncated tail {s.truncated_words * 4} B   board id(s) {sorted(s.board_ids)}")
    print(f"  events {s.n_events:,}   PUR-flagged {s.n_pur:,} ({100 * s.n_pur / max(s.n_events, 1):.2f}%)   "
          f"non-PUR {s.n_events - s.n_pur:,}   bytes/event {s.nbytes / max(s.n_events, 1):.3f}")
    print(f"  timestamps {s.ts_first / 1e12:.6f} .. {s.ts_last / 1e12:.6f} s   span {s.span_s:.3f} s   "
          f"-> {s.n_events / max(s.span_s, 1e-9) / 1e6:.3f} M evt/s over span"
          + (f",  {s.n_events / wall_s / 1e6:.3f} M evt/s over {wall_s:.2f} s wall" if wall_s else ""))
    est_in = 1024 * s.n_1024_markers
    print(f"  extras flags: 1024-trigger markers {s.n_1024_markers:,} (=> >= {est_in:,} input triggers, "
          f"{100 * s.n_events / max(est_in, 1):.1f}% written)   trigger-lost {s.n_lost_trigger_flags:,}   "
          f"lost-counted {s.n_lost_counted_flags:,}   over-range {s.n_overrange_flags:,}")
    nch = 16 if s.per_channel[8:].any() else 8
    print("  per-channel events: " + " ".join(f"{int(x):,}" for x in s.per_channel[:nch]))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("files", nargs="+")
    ap.add_argument("--wall", type=float, default=None, help="wall-clock run length in s, for a second rate figure")
    ap.add_argument("--keep", action="store_true", help="also build the event array (memory heavy)")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)
    tot_ev = tot_b = tot_pur = tot_nonpur = 0; spans = []
    for f in a.files:
        s, _ = decode_file(f, keep_events=a.keep, progress=not a.quiet)
        print_summary(s, a.wall)
        tot_ev += s.n_events; tot_b += s.nbytes; tot_pur += s.n_pur
        tot_nonpur += s.n_events - s.n_pur; spans.append(s.span_s)
    if len(a.files) > 1:
        span = max(spans)
        print(f"\nTOTAL  {len(a.files)} files  {tot_b / 1e6:.1f} MB  {tot_ev:,} events  "
              f"({tot_nonpur:,} non-PUR)  PUR {100 * tot_pur / max(tot_ev, 1):.2f}%   "
              f"{tot_ev / span / 1e6:.2f} M evt/s over {span:.2f} s span"
              + (f"   {tot_ev / a.wall / 1e6:.2f} M evt/s over {a.wall:.2f} s wall" if a.wall else ""))


if __name__ == "__main__":
    main()
