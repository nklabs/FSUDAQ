#!/usr/bin/env python3
"""numba kernels for the FSUDAQ online pipeline.

`index_file` walks the board/couple aggregate headers of an .fsu file. It is a linked chain
of variable-length blocks, so the pure-Python version in fsu.py costs ~1 us per header.
Same output as fsu.index_file. `decode_range` decodes an indexed file.

    python3 fsu_fast.py RUN/*.fsu        # compare with fsu.py on real files
"""
from __future__ import annotations

import os

import numpy as np

try:
    from numba import njit
    HAVE_NUMBA = True
except ImportError:  # pragma: no cover
    HAVE_NUMBA = False

    def njit(*a, **k):
        def deco(f):
            return f
        return deco if not (a and callable(a[0])) else a[0]


# ------------------------------------------------------------------ index
@njit(cache=True, nogil=True)
def _index_kernel(w):
    n = w.size
    cap = n // 2 + 1
    ev_start = np.empty(cap, np.int64); nev = np.empty(cap, np.int64)
    couple = np.empty(cap, np.int64); agg = np.empty(cap, np.int64)
    wpe_a = np.empty(cap, np.int64); extra = np.empty(cap, np.int64)
    counters = np.empty(n // 4 + 1, np.int64)
    board_ids = np.zeros(32, np.int64)
    k = 0; na = 0; malformed = 0; pos = 0; bad_header = 0
    while pos < n:
        w0 = np.int64(w[pos])
        if (w0 >> 28) != 0xA:
            bad_header = 1
            break
        size = w0 & 0x0FFFFFFF
        if size < 4 or pos + size > n:
            break
        w1 = np.int64(w[pos + 1]); ctr = np.int64(w[pos + 2]) & 0x7FFFFF
        mask = w1 & 0xFF
        board_ids[(w1 >> 27) & 31] += 1
        counters[na] = ctr; na += 1
        p = pos + 4; end = pos + size
        for c in range(8):
            if not (mask >> c) & 1:
                continue
            if p + 2 > end:
                malformed += 1; break
            c0 = np.int64(w[p]); c1 = np.int64(w[p + 1])
            if not (c0 >> 31):
                malformed += 1; break
            csize = c0 & 0x3FFFFF
            has_wave = (c1 >> 27) & 1; has_extra = (c1 >> 28) & 1
            has_ts = (c1 >> 29) & 1; has_chg = (c1 >> 30) & 1
            nsw = ((c1 & 0xFFFF) * 8) // 2 if has_wave else 0
            wpe = has_ts + nsw + has_extra + has_chg
            body = csize - 2
            if wpe == 0 or body % wpe != 0 or p + csize > end:
                malformed += 1; p += csize; continue
            m = body // wpe
            if m > 0:
                ev_start[k] = p + 2; nev[k] = m; couple[k] = c
                agg[k] = ctr; wpe_a[k] = wpe; extra[k] = has_extra; k += 1
            p += csize
        pos += size
    return (ev_start[:k], nev[:k], couple[:k], agg[:k], wpe_a[:k], extra[:k],
            counters[:na], board_ids, na, malformed, n - pos, bad_header)


def index_file(words: np.ndarray):
    """Drop-in for fsu.index_file (same FileIndex), ~50x faster."""
    import fsu
    w = np.ascontiguousarray(words, dtype=np.uint32)
    (ev_start, nev, couple, agg, wpe, extra, counters, board_ids, n_agg,
     malformed, truncated, bad_header) = _index_kernel(w)
    if bad_header:
        pos = int(w.size - truncated)
        raise ValueError(f"expected board-aggregate header at word {pos}, got 0x{int(w[pos]):08X}")
    return fsu.FileIndex(ev_start=ev_start, nev=nev, couple=couple, agg_counter=agg, wpe=wpe,
                         has_extra=extra, counters=counters,
                         board_ids=set(int(i) for i in np.flatnonzero(board_ids)),
                         n_aggregates=int(n_agg), malformed=int(malformed), truncated_words=int(truncated))


# ------------------------------------------------------------------ decode
TICK_PS = 2000
FINE_DIV = 1024


@njit(cache=True, nogil=True)
def _decode_kernel(w, ev_start, nev, couple, wpe, has_extra, t_lo, t_hi, ts_out, ch_out, e_out, s_out):
    """Linear pass over couple aggregates: only what the pipeline needs (time, channel,
    long and short charge), keeping hits with t_lo <= ts < t_hi. Returns the number kept."""
    k = 0
    for a in range(ev_start.size):
        p = ev_start[a]; step = wpe[a]; c2 = couple[a] * 2; hx = has_extra[a]
        for j in range(nev[a]):
            ttt_w = np.int64(w[p])
            chg_w = np.int64(w[p + step - 1])
            if hx == 1:
                extra_w = np.int64(w[p + step - 2])
                ext = extra_w >> 16
                fine = extra_w & 0x3FF
            else:
                ext = 0; fine = 0
            ts = ((ext << 31) | (ttt_w & 0x7FFFFFFF)) * TICK_PS + (fine * TICK_PS) // FINE_DIV
            if ts >= t_lo and ts < t_hi:
                ts_out[k] = ts
                ch_out[k] = c2 + (ttt_w >> 31)
                e_out[k] = chg_w >> 16
                s_out[k] = chg_w & 0x7FFF
                k += 1
            p += step
    return k


def decode_range(words: np.ndarray, ix, t_lo=None, t_hi=None, short=False):
    """Hits of an indexed file with t_lo <= ts < t_hi as (ts_ps int64, ch uint8, qlong uint16),
    and qshort uint16 (15 bits) as a fourth array with short=True."""
    w = np.ascontiguousarray(words, dtype=np.uint32)
    if t_lo is None:
        t_lo = np.int64(-1) << 62
    if t_hi is None:
        t_hi = np.int64(1) << 62
    tot = int(ix.nev.sum())
    ts = np.empty(tot, np.int64); ch = np.empty(tot, np.uint8); e = np.empty(tot, np.uint16)
    qs = np.empty(tot, np.uint16)
    k = _decode_kernel(w, ix.ev_start, ix.nev, ix.couple, ix.wpe, ix.has_extra,
                       np.int64(t_lo), np.int64(t_hi), ts, ch, e, qs)
    return (ts[:k], ch[:k], e[:k], qs[:k]) if short else (ts[:k], ch[:k], e[:k])


# ------------------------------------------------------------------ incremental scan (the online dashboard)
# The format the online decoders read: time tag, extras option 2 (extended time stamp, flags, fine
# time) and charges, as odin's boards write. Any other couple header is counted and left out: with
# another extras option the extended time stamp and the fine time are not where the decoders look.
@njit(cache=True, nogil=True)
def _event_ts(w, p, step):
    extra_w = np.int64(w[p + step - 2])
    return (((extra_w >> 16) << 31) | (np.int64(w[p]) & 0x7FFFFFFF)) * TICK_PS + ((extra_w & 0x3FF) * TICK_PS) // FINE_DIV


@njit(cache=True, nogil=True)
def _next_ok(w, q, n):
    """Is w[q] where a board aggregate may start: the end of what is written, or a board-aggregate
    header whose own end is the end, still being written, or followed by another header."""
    if q == n:
        return True
    x = np.int64(w[q]); s = x & 0x0FFFFFFF
    if (x >> 28) != 0xA or s < 4:
        return False
    return q + s >= n or (np.int64(w[q + s]) >> 28) == 0xA


@njit(cache=True, nogil=True)
def _scan_kernel(w, pos, n):
    """Index the complete board aggregates in w[pos:n]. Per couple aggregate in the readable format:
    ev_start, nev, couple, wpe, t_first, t_last (ps). Also the word after the last complete board
    aggregate, words skipped over damage, couple aggregates in another format, malformed couple
    aggregates, and board aggregates read.
    A board aggregate counts only if the next one starts right after it: a read that ended inside an
    aggregate (seen 28 Sep 2026) leaves a header promising more words than follow, and the start of
    the next aggregate would otherwise be read as its events."""
    cap = (n - pos) // 2 + 1
    ev_start = np.empty(cap, np.int64); nev = np.empty(cap, np.int64); couple = np.empty(cap, np.int64)
    wpe_a = np.empty(cap, np.int64); t_first = np.empty(cap, np.int64); t_last = np.empty(cap, np.int64)
    k = 0; skipped = 0; other = 0; malformed = 0; n_board = 0
    while pos < n:
        w0 = np.int64(w[pos])
        size = w0 & 0x0FFFFFFF
        if (w0 >> 28) == 0xA and size >= 4 and pos + size > n:
            break                             # still being written
        if (w0 >> 28) != 0xA or size < 4 or not _next_ok(w, pos + size, n):
            # damage: go on at the next word that starts a complete board aggregate followed by another
            # (or by the end of what is written)
            q = pos + 1; found = -1; wait = False
            while q < n:
                x = np.int64(w[q]); s = x & 0x0FFFFFFF
                if (x >> 28) == 0xA and s >= 4:
                    if q + s > n:
                        wait = True           # cannot tell yet: wait for more data
                        break
                    if _next_ok(w, q + s, n):
                        found = q
                        break
                q += 1
            if found < 0:
                break                         # nothing readable after it yet: wait for more data
            skipped += found - pos
            pos = found
            continue
        mask = np.int64(w[pos + 1]) & 0xFF
        n_board += 1
        p = pos + 4; end = pos + size
        for c in range(8):
            if not (mask >> c) & 1:
                continue
            if p + 2 > end:
                malformed += 1; break
            c0 = np.int64(w[p]); c1 = np.int64(w[p + 1])
            if not (c0 >> 31):
                malformed += 1; break
            csize = c0 & 0x3FFFFF
            has_wave = (c1 >> 27) & 1; has_extra = (c1 >> 28) & 1
            has_ts = (c1 >> 29) & 1; has_chg = (c1 >> 30) & 1; opt = (c1 >> 24) & 7
            nsw = ((c1 & 0xFFFF) * 8) // 2 if has_wave else 0
            wpe = has_ts + nsw + has_extra + has_chg
            body = csize - 2
            if csize < 2 or wpe == 0 or body % wpe != 0 or p + csize > end:
                malformed += 1; break
            m = body // wpe
            if m > 0:
                if has_ts == 1 and has_chg == 1 and has_extra == 1 and opt == 2:
                    ev_start[k] = p + 2; nev[k] = m; couple[k] = c; wpe_a[k] = wpe
                    t_first[k] = _event_ts(w, p + 2, wpe)
                    t_last[k] = _event_ts(w, p + 2 + (m - 1) * wpe, wpe)
                    k += 1
                else:
                    other += 1
            p += csize
        pos += size
    return (ev_start[:k], nev[:k], couple[:k], wpe_a[:k], t_first[:k], t_last[:k], pos, skipped, other, malformed, n_board)


class FileScan:
    """The readable couple aggregates of one growing .fsu file, indexed as far as it is written.
    update() reads only what was added since the last call."""
    FIELDS = ("ev_start", "nev", "couple", "wpe", "t_first", "t_last")

    def __init__(self, path):
        self.path = path
        self.pos = 0                          # words indexed so far (up to a whole board aggregate)
        self.size = 0                         # bytes when last looked at
        self.ino = None
        self.parts = {f: [] for f in self.FIELDS}
        self.n = 0                            # couple aggregates indexed
        self.hits = 0
        self.skipped = 0; self.other = 0; self.malformed = 0; self.n_board = 0

    def replaced(self):
        """True when the file at this path is no longer the one indexed (removed, or written anew)."""
        try:
            st = os.stat(self.path)
        except OSError:
            return True
        return (self.ino is not None and st.st_ino != self.ino) or st.st_size < self.size

    def update(self):
        """Index what was added. Returns the number of new couple aggregates."""
        try:
            st = os.stat(self.path)
        except OSError:
            return 0
        if self.ino is None:
            self.ino = st.st_ino
        self.size = st.st_size
        n_words = st.st_size // 4
        if n_words <= self.pos:
            return 0
        w = np.memmap(self.path, dtype="<u4", mode="r", shape=(n_words,))
        r = _scan_kernel(w, np.int64(self.pos), np.int64(n_words))
        del w
        for f, a in zip(self.FIELDS, r[:6]):
            if a.size:
                self.parts[f].append(a.copy())
        self.pos = int(r[6]); self.skipped += int(r[7]); self.other += int(r[8]); self.malformed += int(r[9])
        self.n_board += int(r[10])
        new = int(r[0].size)
        self.n += new
        self.hits += int(r[1].sum())
        return new

    def arrays(self):
        """The index as one array per field (joined on demand)."""
        out = {}
        for f in self.FIELDS:
            if len(self.parts[f]) > 1:
                self.parts[f] = [np.concatenate(self.parts[f])]
            out[f] = self.parts[f][0] if self.parts[f] else np.zeros(0, np.int64)
        return out


def decode_aggs(path, ev_start, nev, couple, wpe, t_lo=None, t_hi=None):
    """Hits of the given couple aggregates of a file (in the readable format) with t_lo <= ts < t_hi:
    (ts_ps int64, ch uint8, qlong uint16, qshort uint16), in file order."""
    if t_lo is None:
        t_lo = np.int64(-1) << 62
    if t_hi is None:
        t_hi = np.int64(1) << 62
    tot = int(np.sum(nev))
    ts = np.empty(tot, np.int64); ch = np.empty(tot, np.uint8); e = np.empty(tot, np.uint16); qs = np.empty(tot, np.uint16)
    if tot == 0:
        return ts, ch, e, qs
    last = int(np.max(ev_start + nev * wpe))
    w = np.memmap(path, dtype="<u4", mode="r", shape=(last,))
    k = _decode_kernel(w, np.ascontiguousarray(ev_start, np.int64), np.ascontiguousarray(nev, np.int64),
                       np.ascontiguousarray(couple, np.int64), np.ascontiguousarray(wpe, np.int64),
                       np.ones(len(ev_start), np.int64), np.int64(t_lo), np.int64(t_hi), ts, ch, e, qs)
    del w
    return ts[:k], ch[:k], e[:k], qs[:k]


def _check_file(path):
    """Compare the numba decode of a real .fsu file with fsu._decode_chunk."""
    import time
    import fsu
    w = np.memmap(path, dtype="<u4", mode="r")
    t0 = time.perf_counter(); ix = fsu.index_file(w); t_ix = time.perf_counter() - t0
    t0 = time.perf_counter(); d = fsu._decode_chunk(w, ix, 0, len(ix.nev)); t_np = time.perf_counter() - t0
    decode_range(w[:5000], index_file(w[:5000]))  # compile
    t0 = time.perf_counter(); ix2 = index_file(w); ts, ch, e = decode_range(w, ix2); t_nb = time.perf_counter() - t0
    n = len(ts)
    assert n == len(d["ch"]), (n, len(d["ch"]))
    assert np.array_equal(ts, d["ts_ps"]) and np.array_equal(ch, d["ch"]) and np.array_equal(e, d["qlong"])
    assert np.array_equal(decode_range(w, ix2, short=True)[3], d["qshort"])
    lo, hi = int(ts[n // 3]), int(ts[2 * n // 3])
    ts2, ch2, e2 = decode_range(w, ix2, lo, hi)
    m = (d["ts_ps"] >= lo) & (d["ts_ps"] < hi)
    assert np.array_equal(ts2, d["ts_ps"][m]) and np.array_equal(e2, d["qlong"][m])
    print(f"{path.split('/')[-1]}: {n:,} hits identical; numpy index+decode {(t_ix + t_np) * 1e9 / n:.1f} ns/hit, "
          f"numba {t_nb * 1e9 / n:.1f} ns/hit ({(t_ix + t_np) / t_nb:.1f}x); time-window decode identical")
    # the incremental scan, fed the file in uneven pieces, finds the same hits
    fs = FileScan(path)
    words = w.size
    for cut in sorted(set(np.random.default_rng(1).integers(1, words, 20).tolist()) | {words}):
        r = _scan_kernel(w, np.int64(fs.pos), np.int64(cut))
        for f, a in zip(FileScan.FIELDS, r[:6]):
            if a.size:
                fs.parts[f].append(a.copy())
        fs.pos = int(r[6]); fs.skipped += int(r[7]); fs.other += int(r[8]); fs.malformed += int(r[9])
    A = fs.arrays()
    ts3, ch3, e3, qs3 = decode_aggs(path, A["ev_start"], A["nev"], A["couple"], A["wpe"])
    assert fs.pos == words - ix2.truncated_words and fs.skipped == 0 and fs.malformed == ix2.malformed, (fs.pos, fs.skipped)
    assert np.array_equal(ts3, ts) and np.array_equal(ch3, ch) and np.array_equal(e3, e), "scan in pieces differs"
    ends = np.cumsum(A["nev"])
    assert np.array_equal(ts[ends - A["nev"]], A["t_first"]) and np.array_equal(ts[ends - 1], A["t_last"]), "first/last times"
    print(f"    incremental scan in 20 pieces: {A['nev'].sum():,} hits identical, {fs.other} couple aggregates in another format")


if __name__ == "__main__":
    import sys
    for arg in sys.argv[1:]:
        if arg.endswith(".fsu"):
            _check_file(arg)
