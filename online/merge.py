#!/usr/bin/env python3
"""Merge CoMPASS per-channel RAW files into one time-ordered Data_R tree.

CoMPASS writes one ROOT file per (board, channel) --
`RAW/DataR_CH<ch>@<model>_<serial>_<run>.root` -- each holding only that
channel's hits. The processing pipeline wants a single time-ordered stream, so
somebody has to merge them, and the merge is where cross-board time ordering is
decided.

Concatenating the files is NOT enough. Each input is sorted on its own, but a
concatenation is sorted only within each channel's block, which is the
worst-case input for event building: `pipeline.py` would group hits by channel
rather than by time. This does a real k-way merge instead.

The merge is streaming and bounded in memory: each input is read in chunks, and
at each step everything up to `min(last buffered timestamp)` is safe to emit,
because every input is individually sorted. That slice is concatenated, stably
sorted and appended.

  merge.py RAW/ -o merged.root
  merge.py RAW/*.root -o merged.root --compression zstd:3
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import time

import numpy as np
try:
    import uproot
except ImportError:       # pragma: no cover - FSUDAQ path does not need it
    uproot = None

BRANCHES = ["Channel", "Timestamp", "Board", "Energy", "EnergyShort", "Flags"]
#: dtypes real CoMPASS output uses
COMPASS_DTYPES = {
    "Channel": np.uint16, "Timestamp": np.uint64, "Board": np.uint16,
    "Energy": np.uint16, "EnergyShort": np.uint16, "Flags": np.uint32,
}
NAME_RE = re.compile(r"DataR_CH(\d+)@(\w+?)_(\d+)_")


def human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:,.1f} {unit}" if unit != "B" else f"{n:,.0f} B"
        n /= 1024


def make_compression(spec: str):
    if spec.lower() in ("none", "off", "0"):
        return None
    name, _, level = spec.partition(":")
    cls = {"zlib": uproot.ZLIB, "lz4": uproot.LZ4, "zstd": uproot.ZSTD}[name.lower()]
    return cls(int(level) if level else 1)


def find_inputs(args_files) -> list[str]:
    out = []
    for a in args_files:
        if os.path.isdir(a):
            out += sorted(glob.glob(os.path.join(a, "DataR_CH*.root")))
        else:
            out += sorted(glob.glob(a))
    return out


def iter_merged(inputs, tree_name="Data_R", step_size=None, buffer_hits=4_000_000,
                branches=None):
    """Yield time-ordered blocks of hits merged across per-channel files.

    Each input is individually sorted, so at every step everything at or below
    `min(last buffered timestamp)` is already buffered everywhere and can be
    emitted. Blocks are contiguous and globally ordered, so a consumer can treat
    the sequence as one stream.

    This is the merge itself, with no output file: `merge()` writes the blocks to
    disk, and the online path in pipeline.py processes them directly, skipping a
    compress-and-immediately-decompress round trip.
    """
    branches = list(branches) if branches else BRANCHES
    files = [uproot.open(p) for p in inputs]
    try:
        trees = [f[tree_name] for f in files]
        # Memory is n_files x step_size, so a per-file chunk that is fine for 2
        # inputs is not fine for 100. Size it from a total budget instead.
        if step_size is None:
            step_size = max(20_000, buffer_hits // max(len(inputs), 1))

        readers = [t.iterate(branches, step_size=step_size, library="np") for t in trees]
        bufs: list[dict | None] = [None] * len(inputs)
        done = [False] * len(inputs)

        while True:
            for i in range(len(inputs)):
                while not done[i] and (bufs[i] is None or bufs[i]["Timestamp"].size == 0):
                    try:
                        bufs[i] = next(readers[i])
                    except StopIteration:
                        bufs[i], done[i] = None, True

            live = [i for i in range(len(inputs)) if bufs[i] is not None]
            if not live:
                return

            boundary = min(int(bufs[i]["Timestamp"][-1]) for i in live)

            take = []
            for i in live:
                ts = bufs[i]["Timestamp"]
                k = int(np.searchsorted(ts, boundary, side="right"))
                if k:
                    take.append({b: bufs[i][b][:k] for b in branches})
                    bufs[i] = {b: bufs[i][b][k:] for b in branches}

            if not take:
                continue
            block = {b: np.concatenate([t[b] for t in take]) for b in branches}
            order = np.argsort(block["Timestamp"], kind="stable")
            yield {b: block[b][order] for b in branches}
    finally:
        for f in files:
            f.close()


def merge(inputs, out_path, tree_name="Data_R", compression="zstd:3",
          step_size=None, buffer_hits=4_000_000, verbose=True):
    if os.path.exists(out_path):
        os.remove(out_path)        # uproot.recreate does not truncate

    totals = 0
    for p in inputs:
        with uproot.open(p) as f:
            totals += f[tree_name].num_entries
    if verbose:
        eff = step_size or max(20_000, buffer_hits // max(len(inputs), 1))
        print(f"{len(inputs)} input files, {totals:,} hits total, "
              f"step {eff:,} hits/file (~{human(len(inputs) * eff * 20)} buffered)")

    n_written = 0
    t_start = time.perf_counter()
    with uproot.recreate(out_path, compression=make_compression(compression)) as fout:
        fout.mktree(tree_name, COMPASS_DTYPES)
        otree = fout[tree_name]
        for block in iter_merged(inputs, tree_name, step_size, buffer_hits):
            otree.extend({b: block[b].astype(COMPASS_DTYPES[b], copy=False)
                          for b in BRANCHES})
            n_written += block["Timestamp"].size

    wall = time.perf_counter() - t_start
    if verbose:
        size = os.path.getsize(out_path)
        print(f"wrote {n_written:,} hits to {out_path} ({human(size)}) in {wall:.1f} s")
        if n_written != totals:
            print(f"  WARNING: expected {totals:,} hits, wrote {n_written:,}")
    return n_written


def main() -> None:
    p = argparse.ArgumentParser(
        description="Merge CoMPASS per-channel RAW files into one time-ordered tree.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("files", nargs="+", help="a RAW/ directory, or DataR_*.root files")
    p.add_argument("-o", "--out", required=True)
    p.add_argument("--tree", default="Data_R")
    p.add_argument("--compression", default="zstd:3",
                   help="zstd:3 matches zlib:1's size for a ~1.5x faster chain; "
                        "lz4:4 is faster still at ~42%% more disk. Use zlib:1 if "
                        "something downstream predates ROOT 6.20's zstd support.")
    p.add_argument("--step-size", type=int, default=None,
                   help="hits buffered per input file; default derives it from "
                        "--buffer-hits and the number of inputs")
    p.add_argument("--buffer-hits", type=int, default=4_000_000,
                   help="total hits to hold in memory across all inputs")
    p.add_argument("--verify", action="store_true",
                   help="re-read the output and check it is time-ordered and complete")
    args = p.parse_args()

    inputs = find_inputs(args.files)
    if not inputs:
        raise SystemExit("no DataR_*.root inputs found")

    # the Board/Channel branches should agree with what the filename claims
    for path in inputs:
        m = NAME_RE.search(os.path.basename(path))
        if not m:
            continue
        t = uproot.open(path)[args.tree]
        if not t.num_entries:
            continue
        a = t.arrays(["Channel", "Board"], entry_stop=1, library="np")
        if int(a["Channel"][0]) != int(m.group(1)):
            print(f"  WARNING: {os.path.basename(path)} holds Channel "
                  f"{int(a['Channel'][0])}, filename says CH{m.group(1)}")

    n = merge(inputs, args.out, args.tree, args.compression, args.step_size,
              args.buffer_hits)

    if args.verify:
        t = uproot.open(args.out)[args.tree]
        a = t.arrays(["Timestamp", "Board", "Channel"], library="np")
        ts = a["Timestamp"].astype(np.int64)
        print(f"verify: {t.num_entries:,} entries "
              f"({'complete' if t.num_entries == n else 'INCOMPLETE'}), "
              f"time-ordered: {bool(np.all(np.diff(ts) >= 0))}, "
              f"boards: {sorted(set(a['Board'].tolist()))}, "
              f"channels: {len(set(a['Channel'].tolist()))}")


if __name__ == "__main__":
    main()
