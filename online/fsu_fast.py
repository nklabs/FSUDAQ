#!/usr/bin/env python3
"""numba kernels for the FSUDAQ online pipeline.

Two hot loops that numpy cannot vectorise well:

* `index_file` -- the walk over board/couple aggregate headers of an .fsu file. It is
  a linked chain of variable-length blocks, so the pure-Python version in fsu.py
  costs ~1 us per header and dominated decode CPU. Same output as fsu.index_file.
* `build_events` -- one pass over the time-sorted hits: event boundaries (gap >
  window), winner-takes-all per plane (largest energy, first in time on ties),
  aux counter flags per event, and the slice membership test on the event's first
  hit. Replaces concatenate + argsort(key) + reduceat + several gathers.

Both are checked against the numpy versions by `python3 fsu_fast.py --selftest`.
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
def _decode_kernel(w, ev_start, nev, couple, wpe, has_extra, t_lo, t_hi, ts_out, ch_out, e_out):
    """Linear pass over couple aggregates: only what the pipeline needs (time, channel,
    long charge), keeping hits with t_lo <= ts < t_hi. Returns the number kept."""
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
                k += 1
            p += step
    return k


def decode_range(words: np.ndarray, ix, t_lo=None, t_hi=None):
    """Hits of an indexed file with t_lo <= ts < t_hi as (ts_ps int64, ch uint8, qlong uint16)."""
    w = np.ascontiguousarray(words, dtype=np.uint32)
    if t_lo is None:
        t_lo = np.int64(-1) << 62
    if t_hi is None:
        t_hi = np.int64(1) << 62
    tot = int(ix.nev.sum())
    ts = np.empty(tot, np.int64); ch = np.empty(tot, np.uint8); e = np.empty(tot, np.uint16)
    k = _decode_kernel(w, ix.ev_start, ix.nev, ix.couple, ix.wpe, ix.has_extra,
                       np.int64(t_lo), np.int64(t_hi), ts, ch, e)
    return ts[:k], ch[:k], e[:k]


# ------------------------------------------------------------------ event builder
@njit(cache=True, nogil=True)
def _count_events(ts, order, window, t0, t1):
    """Events whose first hit falls in [t0, t1); also the total number of events."""
    n = order.size
    n_ev = 0; n_keep = 0
    prev = np.int64(-1) << 62
    for j in range(n):
        t = ts[order[j]]
        if t - prev > window:
            n_ev += 1
            if t >= t0 and t < t1:
                n_keep += 1
        prev = t
    return n_ev, n_keep


@njit(cache=True, nogil=True)
def _build_kernel(ts, board, chan, energy, order, lut, plane_of, amp_of, aux_gch, window, t0, t1,
                  ch_out, t_out, aux_out, hits_out, chan_counts, nhit_hist):
    """chan_counts[g] += 1 per mapped hit (last slot: unmapped); nhit_hist[min(hits, last)] per event."""
    n = order.size
    n_boards = lut.shape[0]; n_chan = lut.shape[1]; n_aux = aux_gch.size
    n_unmapped_slot = chan_counts.size - 1; nh_last = nhit_hist.size - 1
    e = -1            # index of the current kept event in the outputs
    in_slice = False
    prev = np.int64(-1) << 62
    best_e0 = -1; best_e1 = -1; best_e2 = -1
    n_bad = 0
    for j in range(n):
        i = order[j]
        t = ts[i]
        if t - prev > window:                      # new event
            if in_slice and e >= 0:
                h = hits_out[e]
                nhit_hist[h if h < nh_last else nh_last] += 1
            in_slice = t >= t0 and t < t1
            if in_slice:
                e += 1
                best_e0 = -1; best_e1 = -1; best_e2 = -1
                ch_out[e, 0] = 0; ch_out[e, 1] = 0; ch_out[e, 2] = 0
                t_out[e, 0] = 0; t_out[e, 1] = 0; t_out[e, 2] = 0
                for a in range(n_aux):
                    aux_out[a, e] = False
                hits_out[e] = 0
        prev = t
        if not in_slice:
            continue
        hits_out[e] += 1
        b = board[i]; c = chan[i]
        if b < 0 or b >= n_boards or c < 0 or c >= n_chan:
            n_bad += 1; chan_counts[n_unmapped_slot] += 1; continue
        g = lut[b, c]
        if g < 0:
            n_bad += 1; chan_counts[n_unmapped_slot] += 1; continue
        chan_counts[g] += 1
        pl = plane_of[g]
        if pl >= 0:
            en = np.int64(energy[i])
            if pl == 0:
                if en > best_e0:
                    best_e0 = en; ch_out[e, 0] = amp_of[g]; t_out[e, 0] = t
            elif pl == 1:
                if en > best_e1:
                    best_e1 = en; ch_out[e, 1] = amp_of[g]; t_out[e, 1] = t
            else:
                if en > best_e2:
                    best_e2 = en; ch_out[e, 2] = amp_of[g]; t_out[e, 2] = t
        else:
            for a in range(n_aux):
                if aux_gch[a] == g:
                    aux_out[a, e] = True
    if in_slice and e >= 0:
        h = hits_out[e]
        nhit_hist[h if h < nh_last else nh_last] += 1
    return n_bad


def build_events(ts, board, chan, energy, order, cmap, lut, window_units, to_ns, t0=None, t1=None,
                 chan_counts=None, nhit_hist=None):
    """Fused replacement for pipeline.build_events on an unsorted block + its sort order.

    Returns (ch, tof_ns, aux, n_events_kept, n_unmapped, n_hits_kept). If chan_counts
    (size n_channels+1) and nhit_hist arrays are given they are accumulated in place.
    """
    from detector import AUX_COUNTERS
    if t0 is None:
        t0 = np.int64(-1) << 62
    if t1 is None:
        t1 = np.int64(1) << 62
    ts = np.ascontiguousarray(ts, np.int64)
    order = np.ascontiguousarray(order, np.int64)
    aux_names = [n for n in AUX_COUNTERS if n in cmap.aux_index]
    aux_gch = np.asarray([cmap.aux_index[n] for n in aux_names], np.int64)
    n_ev, n_keep = _count_events(ts, order, np.int64(window_units), np.int64(t0), np.int64(t1))
    ch = np.zeros((n_keep, 3), np.int32)
    t_win = np.zeros((n_keep, 3), np.int64)
    aux_arr = np.zeros((aux_gch.size, n_keep), np.bool_)
    hits = np.zeros(n_keep, np.int64)
    if chan_counts is None:
        chan_counts = np.zeros(cmap.n_channels + 1, np.int64)
    if nhit_hist is None:
        nhit_hist = np.zeros(41, np.int64)
    n_bad = _build_kernel(ts, np.ascontiguousarray(board, np.int64), np.ascontiguousarray(chan, np.int64),
                          np.ascontiguousarray(energy, np.int64), order, np.ascontiguousarray(lut, np.int64),
                          np.ascontiguousarray(cmap.plane, np.int64), np.ascontiguousarray(cmap.amp, np.int64),
                          aux_gch, np.int64(window_units), np.int64(t0), np.int64(t1), ch, t_win, aux_arr, hits,
                          chan_counts, nhit_hist)
    both = (ch[:, 0] > 0) & (ch[:, 2] > 0)
    tof_ns = np.where(both, (t_win[:, 2] - t_win[:, 0]) * to_ns, 0.0)
    aux = {n: None for n in AUX_COUNTERS}
    for k, n in enumerate(aux_names):
        aux[n] = aux_arr[k]
    return ch, tof_ns, aux, int(n_keep), int(n_bad), int(hits.sum())


# ------------------------------------------------------------------ fused k-way merge + event builder
# Each (board, channel) hit stream is time-ordered in the raw file (verified: zero
# backward steps per channel, thousands per couple), so a heap merge over the
# per-channel streams yields the global time order without any sort, with
# sequential memory access -- the sort was the stage that collapsed under
# memory-bandwidth contention with many workers.

@njit(cache=True, nogil=True)
def partition_by_channel(ts, ch, e, n_ch):
    """Stable counting partition by channel: returns (ts2, ch2, e2, offsets[n_ch+1])."""
    n = ts.size
    cnt = np.zeros(n_ch + 1, np.int64)
    for i in range(n):
        cnt[ch[i] + 1] += 1
    for c in range(n_ch):
        cnt[c + 1] += cnt[c]
    pos = cnt.copy()
    ts2 = np.empty(n, np.int64); ch2 = np.empty(n, np.uint8); e2 = np.empty(n, np.uint16)
    for i in range(n):
        c = ch[i]; p = pos[c]; pos[c] = p + 1
        ts2[p] = ts[i]; ch2[p] = c; e2[p] = e[i]
    return ts2, ch2, e2, cnt


@njit(cache=True, nogil=True)
def _merge_build_kernel(ts, ch, e, s_off, s_board, lut, plane_of, amp_of, aux_gch, window, t0, t1,
                        ch_out, t_out, aux_out):
    """Heap merge of the sorted streams ts[s_off[s]:s_off[s+1]] fused with the event builder.
    Returns (n_events_kept, n_hits_kept, n_bad, overflow)."""
    S = s_off.size - 1
    cap = ch_out.shape[0]
    n_boards = lut.shape[0]; n_chan = lut.shape[1]; n_aux = aux_gch.size
    # binary heap of (time, stream)
    hp_t = np.empty(S, np.int64); hp_s = np.empty(S, np.int64); hn = 0
    pos = np.empty(S, np.int64)
    for s in range(S):
        pos[s] = s_off[s]
        if pos[s] < s_off[s + 1]:
            # push
            t = ts[pos[s]]; k = hn; hn += 1
            while k > 0:
                par = (k - 1) >> 1
                if hp_t[par] <= t:
                    break
                hp_t[k] = hp_t[par]; hp_s[k] = hp_s[par]; k = par
            hp_t[k] = t; hp_s[k] = s
    e_idx = -1; in_slice = False
    prev = np.int64(-1) << 62
    best0 = -1; best1 = -1; best2 = -1
    n_bad = 0; n_hits = 0
    while hn > 0:
        t = hp_t[0]; s = hp_s[0]
        i = pos[s]; pos[s] = i + 1
        # replace the root with the stream's next hit, or with the last heap element
        if pos[s] < s_off[s + 1]:
            nt = ts[pos[s]]; ns = s
        else:
            hn -= 1
            if hn == 0:
                nt = t; ns = s   # nothing left to sift; process this hit below
            else:
                nt = hp_t[hn]; ns = hp_s[hn]
        if hn > 0:
            k = 0
            while True:
                l = 2 * k + 1
                if l >= hn:
                    break
                r = l + 1
                m = l
                if r < hn and hp_t[r] < hp_t[l]:
                    m = r
                if hp_t[m] >= nt:
                    break
                hp_t[k] = hp_t[m]; hp_s[k] = hp_s[m]; k = m
            hp_t[k] = nt; hp_s[k] = ns
        # ---- builder
        if t - prev > window:
            in_slice = t >= t0 and t < t1
            if in_slice:
                e_idx += 1
                if e_idx >= cap:
                    return e_idx, n_hits, n_bad, 1
                best0 = -1; best1 = -1; best2 = -1
                ch_out[e_idx, 0] = 0; ch_out[e_idx, 1] = 0; ch_out[e_idx, 2] = 0
                t_out[e_idx, 0] = 0; t_out[e_idx, 1] = 0; t_out[e_idx, 2] = 0
                for a in range(n_aux):
                    aux_out[a, e_idx] = False
        prev = t
        if not in_slice:
            continue
        n_hits += 1
        b = s_board[s]; c = np.int64(ch[i])
        if b < 0 or b >= n_boards or c >= n_chan:
            n_bad += 1; continue
        g = lut[b, c]
        if g < 0:
            n_bad += 1; continue
        pl = plane_of[g]
        if pl >= 0:
            en = np.int64(e[i])
            if pl == 0:
                if en > best0:
                    best0 = en; ch_out[e_idx, 0] = amp_of[g]; t_out[e_idx, 0] = t
            elif pl == 1:
                if en > best1:
                    best1 = en; ch_out[e_idx, 1] = amp_of[g]; t_out[e_idx, 1] = t
            else:
                if en > best2:
                    best2 = en; ch_out[e_idx, 2] = amp_of[g]; t_out[e_idx, 2] = t
        else:
            for a in range(n_aux):
                if aux_gch[a] == g:
                    aux_out[a, e_idx] = True
    return e_idx + 1, n_hits, n_bad, 0


def merge_build(streams, cmap, lut, window_units, to_ns, t0=None, t1=None, n_ch=16):
    """streams: list of (board_id, ts, ch, e) per board, hits in raw file order (any number
    of files concatenated). Returns (ch, tof_ns, aux, n_events, n_unmapped, n_hits)."""
    from detector import AUX_COUNTERS
    if t0 is None:
        t0 = np.int64(-1) << 62
    if t1 is None:
        t1 = np.int64(1) << 62
    ts_l, ch_l, e_l, off_l, board_l = [], [], [], [], []
    base = 0
    for bid, ts, ch, e in streams:
        if ts.size == 0:
            continue
        ts2, ch2, e2, off = partition_by_channel(np.ascontiguousarray(ts, np.int64),
                                                 np.ascontiguousarray(ch, np.uint8),
                                                 np.ascontiguousarray(e, np.uint16), n_ch)
        ts_l.append(ts2); ch_l.append(ch2); e_l.append(e2)
        off_l.append(off[:-1] + base); board_l.append(np.full(n_ch, bid, np.int64))
        base += ts2.size
    aux_names = [n for n in AUX_COUNTERS if n in cmap.aux_index]
    aux = {n: None for n in AUX_COUNTERS}
    if not ts_l:
        return np.zeros((0, 3), np.int32), np.zeros(0), aux, 0, 0, 0
    ts = np.concatenate(ts_l); ch = np.concatenate(ch_l); e = np.concatenate(e_l)
    s_off = np.concatenate(off_l + [np.asarray([base], np.int64)])
    s_board = np.concatenate(board_l)
    aux_gch = np.asarray([cmap.aux_index[n] for n in aux_names], np.int64)
    args = (ts, ch, e, s_off, s_board, np.ascontiguousarray(lut, np.int64),
            np.ascontiguousarray(cmap.plane, np.int64), np.ascontiguousarray(cmap.amp, np.int64),
            aux_gch, np.int64(window_units), np.int64(t0), np.int64(t1))
    cap = max(ts.size // 4, 1024)
    while True:
        ch_out = np.zeros((cap, 3), np.int32); t_out = np.zeros((cap, 3), np.int64)
        aux_out = np.zeros((aux_gch.size, cap), np.bool_)
        n_ev, n_hits, n_bad, overflow = _merge_build_kernel(*args, ch_out, t_out, aux_out)
        if not overflow:
            break
        cap = ts.size + 1
    ch_out = ch_out[:n_ev]; t_out = t_out[:n_ev]; aux_out = aux_out[:, :n_ev]
    both = (ch_out[:, 0] > 0) & (ch_out[:, 2] > 0)
    tof_ns = np.where(both, (t_out[:, 2] - t_out[:, 0]) * to_ns, 0.0)
    for k, n in enumerate(aux_names):
        aux[n] = aux_out[k]
    return ch_out, tof_ns, aux, int(n_ev), int(n_bad), int(n_hits)


# ------------------------------------------------------------------ self test
def _selftest():
    import time
    import pipeline as pl
    from detector import AUX_COUNTERS, ChannelMap, parse_planes
    rng = np.random.default_rng(7)
    cmap = ChannelMap(parse_planes("deployed"), 16, tuple(AUX_COUNTERS))
    lut = cmap.lookup_table()
    n = 3_000_000
    ts = np.sort(rng.integers(0, 3_000_000_000_000, n)).astype(np.int64)      # 3 s of ps
    ts[rng.integers(0, n, n // 50)] = ts[rng.integers(0, n, n // 50)]          # some exact ties
    ts.sort()
    board = rng.integers(0, 7, n).astype(np.int16); board[rng.integers(0, n, 1000)] = 9   # some unmapped
    chan = rng.integers(0, 16, n).astype(np.uint8)
    energy = rng.integers(0, 3000, n).astype(np.uint16); energy[::7] = 1500      # ties in energy
    a = {"Timestamp": ts, "Board": board, "Channel": chan, "Energy": energy}
    W = 100_000.0
    res = pl.Result()
    t0 = time.perf_counter(); ch0, tof0, aux0, nev0 = pl.build_events(a, cmap, lut, W, 1e-3, res); t_np = time.perf_counter() - t0
    order = np.arange(n, dtype=np.int64)
    build_events(ts[:1000], board[:1000], chan[:1000], energy[:1000], order[:1000], cmap, lut, W, 1e-3)  # compile
    t0 = time.perf_counter(); ch1, tof1, aux1, nev1, nbad1, nh1 = build_events(ts, board, chan, energy, order, cmap, lut, W, 1e-3); t_nb = time.perf_counter() - t0
    assert nev0 == nev1, (nev0, nev1)
    assert np.array_equal(ch0, ch1), "WTA channels differ"
    assert np.allclose(tof0, tof1), "tof differs"
    for k in AUX_COUNTERS:
        if aux0[k] is None:
            assert aux1[k] is None
        else:
            assert np.array_equal(aux0[k], aux1[k]), k
    assert res.unmapped == nbad1 and nh1 == n
    # slice membership: events with first hit in [1 s, 2 s) equal the numpy events filtered the same way
    starts = pl._starts_new_event(ts, W); first = ts[starts]
    keep = (first >= 1_000_000_000_000) & (first < 2_000_000_000_000)
    ch2, tof2, aux2, nev2, _, _ = build_events(ts, board, chan, energy, order, cmap, lut, W, 1e-3,
                                               1_000_000_000_000, 2_000_000_000_000)
    assert nev2 == int(keep.sum()) and np.array_equal(ch0[keep], ch2) and np.allclose(tof0[keep], tof2)
    print(f"build_events: {nev0:,} events from {n:,} hits identical; numpy {t_np*1e9/n:.1f} ns/hit, "
          f"numba {t_nb*1e9/n:.1f} ns/hit ({t_np/t_nb:.1f}x)")
    # fused merge+build: six boards, per-channel sorted streams in aggregate-like file order
    ts_u = np.unique(ts)                        # unique times so the order is unambiguous
    m = ts_u.size
    board_u = rng.integers(0, 6, m).astype(np.int16); chan_u = rng.integers(0, 16, m).astype(np.uint8)
    energy_u = rng.integers(0, 3000, m).astype(np.uint16); energy_u[::5] = 1500
    streams = []
    for b in range(6):
        sel = np.flatnonzero(board_u == b)
        # file order: 1000-hit blocks per channel interleaved, each channel sorted
        order = np.lexsort((ts_u[sel], sel // 1000, chan_u[sel]))
        order = sel[np.argsort(np.lexsort((ts_u[sel], chan_u[sel])) // 1, kind="stable")]  # placeholder
        # simplest faithful model: sort by (block, channel, time)
        blk = np.arange(sel.size) // 4096
        o = np.lexsort((ts_u[sel], chan_u[sel], blk))
        idx = sel[o]
        streams.append((b, ts_u[idx], chan_u[idx], energy_u[idx]))
    a2 = {"Timestamp": ts_u, "Board": board_u, "Channel": chan_u, "Energy": energy_u}
    res2 = pl.Result()
    t0 = time.perf_counter(); c0, f0, x0, n0 = pl.build_events(a2, cmap, lut, W, 1e-3, res2); t_ref = time.perf_counter() - t0
    merge_build([(b, t[:100], c[:100], e[:100]) for b, t, c, e in streams], cmap, lut, W, 1e-3)  # compile
    t0 = time.perf_counter(); c1, f1, x1, n1, nb1, nh1 = merge_build(streams, cmap, lut, W, 1e-3); t_mb = time.perf_counter() - t0
    t0 = time.perf_counter(); o = np.argsort(ts_u); t_sort = time.perf_counter() - t0
    assert n0 == n1 and np.array_equal(c0, c1) and np.allclose(f0, f1), "merge_build differs from build_events"
    for k in AUX_COUNTERS:
        if x0[k] is not None:
            assert np.array_equal(x0[k], x1[k]), k
    print(f"merge_build: {n1:,} events from {m:,} hits on 96 streams identical; "
          f"numpy sort+build {(t_sort + t_ref)*1e9/m:.1f} ns/hit, fused heap merge+build {t_mb*1e9/m:.1f} ns/hit")


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
    lo, hi = int(ts[n // 3]), int(ts[2 * n // 3])
    ts2, ch2, e2 = decode_range(w, ix2, lo, hi)
    m = (d["ts_ps"] >= lo) & (d["ts_ps"] < hi)
    assert np.array_equal(ts2, d["ts_ps"][m]) and np.array_equal(e2, d["qlong"][m])
    print(f"{path.split('/')[-1]}: {n:,} hits identical; numpy index+decode {(t_ix + t_np) * 1e9 / n:.1f} ns/hit, "
          f"numba {t_nb * 1e9 / n:.1f} ns/hit ({(t_ix + t_np) / t_nb:.1f}x); time-window decode identical")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
    for arg in sys.argv[1:]:
        if arg.endswith(".fsu"):
            _check_file(arg)
