#!/usr/bin/env python3
"""Per-channel monitoring for the dashboard's DAQ tab: everything keyed by digitizer channel,
so it needs no cabling and works whatever is plugged in where.

Per channel (slot = board index x 16 + channel), for hits in a slice [t0, t1):
  counts    hits
  q         pulse height (Qlong) over the full 16-bit range: slot 0 = Q 0, then log-spaced integer bins
            from 1 to 65534 (one count wide at the bottom), last slot = 65535 (saturated, or a negative
            integral on the DPP-PSD)
  iat       time since the previous hit on the same channel, log bins 1 ns .. 1 s
            (shows the trigger hold-off as a cut, pile-up, and a pulser as a peak)
  dt        time to every hit of the reference channel within +-DT_W ns (t_channel - t_ref)
  spectrum  rate spectrum: hits counted in 10 us bins over fixed 0.33 s windows (aligned to the
            absolute time, so slices do not change them), |FFT|^2 / hits. Random hits give a flat 1;
            a pulser or a modulated beam shows as peaks. 12.2 Hz bins up to 50 kHz.

    python daq_hist.py --selftest        # kernel against plain numpy on synthetic hits
"""
from __future__ import annotations

import math
import sys

import numpy as np

try:
    from numba import njit
except ImportError:                           # pragma: no cover
    def njit(*a, **k):
        return (lambda f: f) if not (a and callable(a[0])) else a[0]

Q_EDGES = np.unique(np.round(np.logspace(0, np.log10(65535), 700)).astype(np.int64))   # 1 .. 65535
NQ = Q_EDGES.size + 1                         # [0] Q = 0, [1..] the log bins, [NQ - 1] Q = 65535
QLUT = np.empty(65536, np.int64)
QLUT[0] = 0
QLUT[1:65535] = np.searchsorted(Q_EDGES, np.arange(1, 65535), side="right")
QLUT[65535] = NQ - 1
IAT_EDGES = np.logspace(0, 9, 91)             # ns, 10 bins per decade
DT_W_NS = 200.0
NDT = 800                                     # 0.5 ns bins over +-200 ns
DT_EDGES = np.linspace(-DT_W_NS, DT_W_NS, NDT + 1)
BIN_PS = 10_000_000                           # 10 us
SEG = 1 << 15                                 # 0.32768 s per window
SEG_PS = SEG * BIN_PS
F_GROUP = 4                                   # rfft bins averaged per spectrum bin
NF = (SEG // 2) // F_GROUP                    # 4096 bins of 12.2 Hz up to 50 kHz
F_EDGES = np.arange(NF + 1) * F_GROUP / (SEG * BIN_PS * 1e-12) + 0.5 / (SEG * BIN_PS * 1e-12)


class DaqResult:
    ARRAYS = ("counts", "q", "iat", "dt", "spec_p", "spec_n", "spec_tot_p", "spec_tot_n")
    SCALARS = ("n_seg",)

    def __init__(self, n_slots):
        self.counts = np.zeros(n_slots, np.int64)
        self.q = np.zeros((n_slots, NQ), np.int64)
        self.iat = np.zeros((n_slots, IAT_EDGES.size - 1), np.int64)
        self.dt = np.zeros((n_slots, NDT), np.int64)
        self.spec_p = np.zeros((n_slots, NF), np.float64)
        self.spec_n = np.zeros(n_slots, np.float64)
        self.spec_tot_p = np.zeros(NF, np.float64)
        self.spec_tot_n = np.zeros(1, np.float64)
        self.n_seg = 0

    def add(self, o):
        for a in self.ARRAYS:
            getattr(self, a)[...] += o[a] if isinstance(o, dict) else getattr(o, a)
        for a in self.SCALARS:
            setattr(self, a, getattr(self, a) + (o[a] if isinstance(o, dict) else getattr(o, a)))
        return self

    def to_dict(self):
        d = {a: getattr(self, a) for a in self.ARRAYS}
        d.update({a: getattr(self, a) for a in self.SCALARS})
        return d


@njit(cache=True, nogil=True)
def _kernel(ts, slot, e, order, t0, t1, ref_ts, dt_w_ps, qlut, counts, q, iat, dt):
    n_slots = counts.size
    last = np.full(n_slots, np.int64(-1))
    lo = 0
    nref = ref_ts.size
    niat = iat.shape[1]
    ndt = dt.shape[1]
    for j in range(order.size):
        i = order[j]
        t = ts[i]; s = slot[i]
        if s < 0 or s >= n_slots:
            continue
        prev = last[s]
        last[s] = t
        if t < t0 or t >= t1:
            continue
        counts[s] += 1
        en = e[i]
        q[s, qlut[en if en < 65536 else 65535]] += 1
        if prev >= 0 and t > prev:
            x = math.log10((t - prev) * 1e-3) * 10.0      # 10 bins per decade from 1 ns
            if x >= 0.0 and x < niat:
                iat[s, int(x)] += 1
        if nref > 0:
            while lo < nref and ref_ts[lo] < t - dt_w_ps:
                lo += 1
            k = lo
            while k < nref and ref_ts[k] <= t + dt_w_ps:
                d = t - ref_ts[k]
                x = (d + dt_w_ps) * ndt / (2 * dt_w_ps)
                bx = int(x)
                if bx >= 0 and bx < ndt:
                    dt[s, bx] += 1
                k += 1


def process(ts, slot, e, order, t0, t1, ref_slot, n_slots):
    """DaqResult for the hits whose time is in [t0, t1). order sorts ts; hits outside the slice still
    count as the 'previous hit' of a channel. The reference channel's own hits are not paired with
    themselves."""
    r = DaqResult(n_slots)
    if order.size == 0:
        return r
    ts = np.ascontiguousarray(ts, np.int64); slot = np.ascontiguousarray(slot, np.int64)
    e = np.ascontiguousarray(e, np.int64); order = np.ascontiguousarray(order, np.int64)
    if ref_slot is not None and 0 <= ref_slot < n_slots:
        ref_ts = np.sort(ts[slot == ref_slot])
    else:
        ref_ts = np.zeros(0, np.int64)
    _kernel(ts, slot, e, order, np.int64(t0), np.int64(t1), ref_ts, np.int64(DT_W_NS * 1000), QLUT, r.counts, r.q,
            r.iat, r.dt)
    if ref_ts.size and 0 <= ref_slot < n_slots:
        r.dt[ref_slot, NDT // 2] -= int(np.count_nonzero((ref_ts >= t0) & (ref_ts < t1)))   # itself at 0
    _spectra(ts, slot, order, t0, t1, n_slots, r)
    return r


def _spectra(ts, slot, order, t0, t1, n_slots, r):
    """Rate spectra over the fixed 0.33 s windows that lie wholly inside [t0, t1)."""
    k0 = -(-int(t0) // SEG_PS); k1 = int(t1) // SEG_PS
    if k1 <= k0:
        return
    st = ts[order]; ss = slot[order]
    for k in range(k0, k1):
        a = np.searchsorted(st, k * SEG_PS); b = np.searchsorted(st, (k + 1) * SEG_PS)
        if b <= a:
            continue
        sl = ss[a:b]; tb = (st[a:b] - k * SEG_PS) // BIN_PS
        ok = (sl >= 0) & (sl < n_slots)
        M = np.bincount(sl[ok] * SEG + tb[ok], minlength=n_slots * SEG).reshape(n_slots, SEG).astype(np.float32)
        n_ch = M.sum(axis=1, dtype=np.float64)
        P = np.abs(np.fft.rfft(M, axis=1)) ** 2
        r.spec_p += P[:, 1:NF * F_GROUP + 1].reshape(n_slots, NF, F_GROUP).mean(axis=2)
        r.spec_n += n_ch
        tot = M.sum(axis=0)
        Pt = np.abs(np.fft.rfft(tot)) ** 2
        r.spec_tot_p += Pt[1:NF * F_GROUP + 1].reshape(NF, F_GROUP).mean(axis=1)
        r.spec_tot_n += n_ch.sum()
        r.n_seg += 1


def selftest():
    rng = np.random.default_rng(3)
    n_slots = 32
    # random hits on all channels, a 1 kHz pulser on slot 5, slot 9 delayed copies of slot 5 by 37 ns
    T = 3 * SEG_PS + SEG_PS // 3
    t_rand = rng.integers(0, T, 400_000); s_rand = rng.integers(0, n_slots, t_rand.size)
    t_puls = np.arange(1000, T, 1_000_000_000)               # 1 kHz
    t_del = t_puls + 37_000
    ts = np.concatenate([t_rand, t_puls, t_del]).astype(np.int64)
    sl = np.concatenate([s_rand, np.full(t_puls.size, 5), np.full(t_del.size, 9)]).astype(np.int64)
    e = rng.integers(0, 65536, ts.size).astype(np.int64)
    order = np.argsort(ts, kind="stable")
    t0, t1 = SEG_PS // 2, T - SEG_PS // 4
    r = process(ts, sl, e, order, t0, t1, 5, n_slots)
    ok = True
    # plain numpy reference
    m = (ts >= t0) & (ts < t1)
    ok &= np.array_equal(r.counts, np.bincount(sl[m], minlength=n_slots))
    qb = np.where(e == 0, 0, np.where(e == 65535, NQ - 1, np.searchsorted(Q_EDGES, e, side="right")))
    for s in (0, 5, 9):
        mm = m & (sl == s)
        ok &= np.array_equal(r.q[s], np.bincount(qb[mm], minlength=NQ))
        ok &= np.array_equal(r.q[s][1:-1], np.histogram(e[mm & (e > 0) & (e < 65535)], Q_EDGES)[0])
        tt = np.sort(ts[sl == s]); prev = np.concatenate([[-1], tt[:-1]])
        keep = (tt >= t0) & (tt < t1) & (prev >= 0)
        d = (tt[keep] - prev[keep]) * 1e-3
        ok &= np.array_equal(r.iat[s], np.histogram(d, IAT_EDGES)[0])
    # time difference: slot 9 is the pulser 37 ns later
    peak = DT_EDGES[np.argmax(r.dt[9])] + 0.25
    ok &= abs(peak - 37.0) < 0.6
    ok &= r.dt[5, NDT // 2] == 0                              # no self-pairs
    # spectrum: the pulser's line at 1 kHz (and harmonics) stands far above the random channels
    f = 0.5 * (F_EDGES[:-1] + F_EDGES[1:])
    s5 = r.spec_p[5] / max(r.spec_n[5], 1); s0 = r.spec_p[0] / max(r.spec_n[0], 1)
    line = s5[np.argmin(abs(f - 1000))]
    # 328 pulser hits among ~4100 per window: the line is diluted to ~73 x 328 / 4100 = 5.9 of the floor
    ok &= line > 4 and 0.5 < np.median(s0) < 1.5
    print(f"counts/q/iat exact, dt peak {peak:.2f} ns (37), 1 kHz line {line:.0f}x, random channel median "
          f"{np.median(s0):.2f} (1), {r.n_seg} windows")
    print("DAQ SELFTEST", "PASSED" if ok else "FAILED")
    return ok


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(0 if selftest() else 1)
    print(__doc__)
