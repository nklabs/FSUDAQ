#!/usr/bin/env python3
"""numba kernels for the FSUDAQ online pipeline.

`index_file` walks the board/couple aggregate headers of an .fsu file. It is a linked chain
of variable-length blocks, so the pure-Python version in fsu.py costs ~1 us per header.
Same output as fsu.index_file. `decode_range` decodes an indexed file.

    python3 fsu_fast.py RUN/*.fsu        # compare with fsu.py on real files
"""
from __future__ import annotations

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


if __name__ == "__main__":
    import sys
    for arg in sys.argv[1:]:
        if arg.endswith(".fsu"):
            _check_file(arg)
