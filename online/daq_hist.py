#!/usr/bin/env python3
"""Per-channel monitoring for the dashboard's DAQ tab: everything keyed by digitizer channel,
so it needs no cabling and works whatever is plugged in where.

Per channel (slot = board index x 16 + channel), for hits in a slice [t0, t1):
  counts    hits
  q         pulse height (Qlong), one bin per count over the full 16-bit range (65535 = saturated, or
            a negative integral on the DPP-PSD). The page asks for any range and binning (rebin()).
  psd       (Qlong - Qshort) / Qlong against Qlong: log-spaced Qlong bins, 0.01 wide PSD bins over
            [0, 1] (1 in the last). Hits with Q = 0 or 65535 are left out (the page reports those), and
            so are hits with Qshort saturated (32767) or above Qlong (PSD < 0), counted in psd_out.
  iat       time since the previous hit on the same channel, log bins 1 ns .. 1 s
            (shows the trigger hold-off as a cut, pile-up, and a pulser as a peak)
  dt        time to every hit of the reference channel within +-DT_W ns (t_channel - t_ref)
  spectrum  rate spectrum: hits counted in 10 us bins over fixed 0.33 s windows (aligned to the
            absolute time, so slices do not change them), |FFT|^2 / hits. Random hits give a flat 1;
            a pulser or a modulated beam shows as peaks. 12.2 Hz bins up to 50 kHz.

The q and psd arrays are large and mostly empty: a worker sends only the filled bins (to_dict).

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

NQ = 65536                                    # Qlong, one bin per count
PX_EDGES = np.unique(np.round(np.logspace(0, np.log10(65535), 1025)).astype(np.int64))   # PSD map: Qlong 1 .. 65535
NPX = PX_EDGES.size - 1
PXLUT = np.zeros(65536, np.int64)
PXLUT[1:65535] = np.searchsorted(PX_EDGES, np.arange(1, 65535), side="right") - 1
NPY = 100                                     # PSD 0 .. 1 in 0.01 bins
QS_SAT = 0x7FFF                               # Qshort is 15 bits
LOG_EDGES = np.unique(np.round(np.logspace(0, np.log10(65535), 700)).astype(np.int64))   # the default log view
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
    ARRAYS = ("counts", "psd_out", "iat", "dt", "spec_p", "spec_n", "spec_tot_p", "spec_tot_n")
    SPARSE = ("q", "psd")
    SCALARS = ("n_seg",)

    def __init__(self, n_slots, big=np.int64):
        """big: dtype of q and psd (a worker's slice fits uint32; the run's sum gets int64)."""
        self.counts = np.zeros(n_slots, np.int64)
        self.q = np.zeros((n_slots, NQ), big)
        self.psd = np.zeros((n_slots, NPX, NPY), big)
        self.psd_out = np.zeros(n_slots, np.int64)
        self.iat = np.zeros((n_slots, IAT_EDGES.size - 1), np.int64)
        self.dt = np.zeros((n_slots, NDT), np.int64)
        self.spec_p = np.zeros((n_slots, NF), np.float64)
        self.spec_n = np.zeros(n_slots, np.float64)
        self.spec_tot_p = np.zeros(NF, np.float64)
        self.spec_tot_n = np.zeros(1, np.float64)
        self.n_seg = 0

    def add(self, o):
        """Add another DaqResult, or a to_dict() of one."""
        if not isinstance(o, dict):
            o = o.to_dict()
        for a in self.ARRAYS:
            getattr(self, a)[...] += o[a]
        for a in self.SPARSE:
            idx, cnt = o[a]
            getattr(self, a).reshape(-1)[idx] += cnt              # idx are unique
        for a in self.SCALARS:
            setattr(self, a, getattr(self, a) + o[a])
        return self

    def to_dict(self):
        d = {a: getattr(self, a) for a in self.ARRAYS}
        for a in self.SPARSE:
            flat = getattr(self, a).reshape(-1)
            idx = np.flatnonzero(flat)
            d[a] = (idx, flat[idx])
        d.update({a: getattr(self, a) for a in self.SCALARS})
        return d


@njit(cache=True, nogil=True)
def _kernel(ts, slot, e, qs, order, t0, t1, ref_ts, dt_w_ps, pxlut, counts, q, psd, psd_out, iat, dt):
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
        q[s, en] += 1
        if en > 0 and en < 65535:
            qsh = qs[i]
            if qsh < 32767:
                if qsh <= en:                                  # floor(100 PSD), PSD = 1 in the last bin
                    psd[s, pxlut[en], min((en - qsh) * 100 // en, 99)] += 1
                else:
                    psd_out[s] += 1
            else:
                psd_out[s] += 1
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


def process(ts, slot, e, qs, order, t0, t1, ref_slot, n_slots):
    """DaqResult for the hits whose time is in [t0, t1). e, qs: Qlong, Qshort. order sorts ts; hits
    outside the slice still count as the 'previous hit' of a channel. The reference channel's own hits
    are not paired with themselves."""
    r = DaqResult(n_slots, np.uint32)
    if order.size == 0:
        return r
    ts = np.ascontiguousarray(ts, np.int64); slot = np.ascontiguousarray(slot, np.int64)
    e = np.ascontiguousarray(e, np.int64); qs = np.ascontiguousarray(qs, np.int64)
    order = np.ascontiguousarray(order, np.int64)
    if ref_slot is not None and 0 <= ref_slot < n_slots:
        ref_ts = np.sort(ts[slot == ref_slot])
    else:
        ref_ts = np.zeros(0, np.int64)
    _kernel(ts, slot, e, qs, order, np.int64(t0), np.int64(t1), ref_ts, np.int64(DT_W_NS * 1000), PXLUT, r.counts,
            r.q, r.psd, r.psd_out, r.iat, r.dt)
    if ref_ts.size and 0 <= ref_slot < n_slots:
        r.dt[ref_slot, NDT // 2] -= int(np.count_nonzero((ref_ts >= t0) & (ref_ts < t1)))   # itself at 0
    _spectra(ts, slot, order, t0, t1, n_slots, r)
    return r


def q_edges(lo, hi, nb, log):
    """Integer bin edges for Qlong in [lo, hi): nb log-spaced bins (lo >= 1), or nb equal bins of a
    whole number of counts (the last edge may pass hi; it stops at 65536)."""
    lo = int(min(max(lo, 1 if log else 0), 65535)); hi = int(min(max(hi, lo + 1), 65536)); nb = int(min(max(nb, 1), 65536))
    if log:
        e = np.unique(np.round(np.geomspace(lo, hi, nb + 1)).astype(np.int64))
    else:
        w = max(1, -(-(hi - lo) // nb))
        e = np.minimum(lo + w * np.arange(-(-(hi - lo) // w) + 1, dtype=np.int64), 65536)
    return e


def rebin(q, edges):
    """Counts of q (slots x 65536, or one row) in the bins [edges[i], edges[i+1]), leaving out
    Q = 0 and Q = 65535 (the page reports those apart)."""
    q2 = np.atleast_2d(q)
    a, b = int(edges[0]), int(edges[-1])
    c = np.zeros((q2.shape[0], b - a + 1), np.int64)
    np.cumsum(q2[:, a:b], axis=1, out=c[:, 1:])
    out = c[:, edges[1:] - a] - c[:, edges[:-1] - a]
    if a == 0:
        out[:, 0] -= q2[:, 0]
    if b == 65536:
        out[:, -1] -= q2[:, 65535]
    return out if q.ndim == 2 else out[0]


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
    e[::11] = 0; e[::13] = 65535
    qs = (e * rng.uniform(0.0, 1.1, ts.size)).astype(np.int64)
    qs[::17] = 0; qs[::19] = QS_SAT; qs = np.minimum(qs, QS_SAT)
    order = np.argsort(ts, kind="stable")
    t0, t1 = SEG_PS // 2, T - SEG_PS // 4
    r = DaqResult(n_slots).add(process(ts, sl, e, qs, order, t0, t1, 5, n_slots).to_dict())   # through the sparse transfer
    ok = True
    # plain numpy reference
    m = (ts >= t0) & (ts < t1)
    ok &= np.array_equal(r.counts, np.bincount(sl[m], minlength=n_slots))
    for s in (0, 5, 9):
        mm = m & (sl == s)
        ok &= np.array_equal(r.q[s], np.bincount(e[mm], minlength=NQ))
        keep = mm & (e > 0) & (e < 65535)
        ok &= np.array_equal(rebin(r.q[s], LOG_EDGES), np.histogram(e[keep], LOG_EDGES)[0])
        ed = q_edges(0, 65536, 300, False)
        ok &= np.array_equal(rebin(r.q, ed)[s], np.histogram(e[keep], ed)[0])
        good = keep & (qs < QS_SAT) & (qs <= e)
        ref = np.zeros((NPX, NPY), np.int64)
        np.add.at(ref, (np.searchsorted(PX_EDGES, e[good], side="right") - 1,
                        np.minimum((e[good] - qs[good]) * NPY // e[good], NPY - 1)), 1)
        ok &= np.array_equal(r.psd[s], ref) and r.psd_out[s] == np.count_nonzero(keep & ~good)
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
    print(f"counts/q/psd/iat exact, rebinning exact, dt peak {peak:.2f} ns (37), 1 kHz line {line:.0f}x, random channel median "
          f"{np.median(s0):.2f} (1), {r.n_seg} windows")
    print("DAQ SELFTEST", "PASSED" if ok else "FAILED")
    return ok


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(0 if selftest() else 1)
    print(__doc__)
