#!/usr/bin/env python3
"""Online track finder on the merged hit stream: bunch building fused with a numba fast path,
and the reference finder (finder/online_finder.py, unchanged) for every bunch the fast path
does not settle.

    python finder_fast.py --selftest              # fast path vs reference, bunch by bunch

The reference is exact but runs at Python speed. The kernel builds each bunch's readings and
candidates with the reference's arithmetic in the reference's order (clusters, one / split / sub
readings, map lookup, time and light tests, a code counted once) and decides every bunch with no
candidate or one candidate itself, filling the histograms as the bunch closes; no per-bunch or
per-hit array the size of the slice is made. Only a bunch with two or more candidates (the set
search) or more readings than the kernel holds is exported, and the reference decides it.

Inputs the reference expects (INTERFACE.md), and where they come from here:
  channel   cabling: (board, digitizer channel) -> (plane, 1-based amplifier) or a counter
  light     Energy (Qlong) x MeVee per count from the gains file. WITHOUT gains the light
            tests are switched off the reference's own way (pair rule "never", K = inf):
            every cluster reads as one track, nothing is split, no multitrack by light.
  time      Timestamp - per-channel offset (time-offsets file), in ns
  gate      the event-building window; there is no bunch clock in the data yet
  counters  veto / cherenkov / lucite channels from the cabling; absent = None
"""
from __future__ import annotations

import csv
import json
import math
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
FINDER_DIR = os.path.join(HERE, "finder")
sys.path.insert(0, FINDER_DIR)
import online_finder as of                    # noqa: E402  the reference, unchanged

try:
    from numba import njit
    HAVE_NUMBA = True
except ImportError:                           # pragma: no cover
    HAVE_NUMBA = False

    def njit(*a, **k):
        return (lambda f: f) if not (a and callable(a[0])) else a[0]

STATUS = ("none", "incomplete", "unique", "tracks", "ambiguous", "multitrack", "dropped", "veto", "electron")
ST = {s: i for i, s in enumerate(STATUS)}
SLOW = -1
COUNTERS = ("veto", "cherenkov", "lucite")
RULE = {"never": 0, "light": 1, "always": 2}
K_ONE = 0                                     # reading kinds in the fast path
K_SPLIT = 1
K_SUB = 2
RMAX = 64                                     # readings per plane the fast path holds

# histograms (shared by the kernel and the Python path, so both fill the same bins)
P_EDGES = np.linspace(0, 1000, 101)           # MeV/c, accepted tracks
RES_EDGES = np.linspace(-15, 15, 121)         # ns: measured minus expected flight time between planes
DT_EDGES = np.linspace(-15, 15, 61)           # t2 - t1, ns
R_EDGES = np.linspace(0, 4, 81)               # reading light / one track's light
NCL = 6                                       # cluster size 1..5, 6+ in the last slot
NCAND = 8                                     # candidates per bunch 0..6, 7+
NTRK = 5                                      # tracks per accepted bunch 0..3, 4+
NHIT = 41                                     # hits per bunch 0..39, 40+
MAXB = 1024                                   # tracker hits one bunch can hold


# ---------------------------------------------------------------- tables
class Tables:
    """The reference's tables for one setting, plus flat arrays for the kernel."""

    def __init__(self, setting="high", map_path=None, calibrated=False):
        T = of.load(setting, here=FINDER_DIR, map_path=map_path)
        if not calibrated:                    # no light scale: switch the light tests off
            T["pair_rule"] = ["never"] * 3
            T["k"] = math.inf
        self.T, self.setting, self.calibrated = T, setting, calibrated
        meta = json.load(open(os.path.join(FINDER_DIR, "handoff_meta.json")))
        self.threshold = float(meta["threshold_mevee"])
        n = [max(max(T["gang"][i]), max((c[i] for c in T["map"]), default=0)) + 1 for i in range(3)]
        self.lut = np.full(n, -1, np.int32)
        self.map_p = np.zeros(len(T["map"]), np.float64)
        self.map_n = len(T["map"])
        for j, (code, (p, _s, _n)) in enumerate(T["map"].items()):
            self.lut[code] = j
            self.map_p[j] = p
        self.gang = np.zeros((3, max(n)), np.int64)
        for i in range(3):
            for ch, g in T["gang"][i].items():
                self.gang[i, ch] = g
        self.sigma_t = np.zeros(9, np.float64)
        for g, v in T["sigma_t"].items():
            self.sigma_t[g] = v
        self.light_p = np.asarray(T["light_p"], np.float64)
        self.light_q = np.asarray(T["light_q"], np.float64)
        self.s_mm = np.asarray(T["s_mm"], np.float64)
        self.rule = np.asarray([RULE[r] for r in T["pair_rule"]], np.int64)
        self.k = float(T["k"])
        self.has_nsig = T["nsig"] is not None
        self.nsig = float(T["nsig"]) if self.has_nsig else 0.0
        self.max_cands = int(T["max_cands"])
        self.max_combos = int(T["max_combos"])

    def kernel_args(self):
        return (self.lut, self.map_p, self.gang, self.sigma_t, self.light_p, self.light_q, self.s_mm,
                self.rule, self.k, self.has_nsig, self.nsig, self.max_cands, self.max_combos)


# ---------------------------------------------------------------- cabling, gains, offsets
def default_cabling(serials, n_channels, planes=(16, 35, 59), counters=COUNTERS):
    """Placeholder: tracker channels in plane order across the boards in the given order, then
    the counters. Rows: (serial, channel, kind, plane, ch) with kind 'trk' or a counter name."""
    slots = [(s, c) for s in serials for c in range(n_channels.get(s, 16))]
    want = [("trk", p, ch) for p, n in enumerate(planes) for ch in range(1, n + 1)] + [(c, -1, 0) for c in counters]
    if len(want) > len(slots):
        lost = want[len(slots):]
        print(f"default cabling: {len(want)} channels wanted, {len(slots)} on the boards; not read out: "
              + ", ".join(f"T{p}:{ch}" if k == "trk" else k for k, p, ch in lost), file=sys.stderr)
    return [(s, c, kind, p, ch) for (s, c), (kind, p, ch) in zip(slots, want)]


def load_cabling(path):
    """CSV with columns serial, channel, kind (trk / veto / cherenkov / lucite), plane, ch."""
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            rows.append((int(r["serial"]), int(r["channel"]), r["kind"].strip(),
                         int(r.get("plane") or -1), int(r.get("ch") or 0)))
    return rows


def load_per_channel(path, column):
    """{(serial, channel): value} from a CSV with columns serial, channel, <column>."""
    out = {}
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            out[(int(r["serial"]), int(r["channel"]))] = float(r[column])
    return out


class Channels:
    """Per (board index, digitizer channel): what it is, its light scale and its time offset."""

    def __init__(self, serials, rows, gains=None, offsets_ns=None, board_channels=16):
        self.serials = list(serials)
        bidx = {s: i for i, s in enumerate(self.serials)}
        nb = len(self.serials)
        self.board_channels = board_channels
        self.plane = np.full((nb, board_channels), -2, np.int64)    # -2 unused, -1 counter
        self.ch = np.zeros((nb, board_channels), np.int64)
        self.counter = np.full((nb, board_channels), -1, np.int64)
        self.gain = np.ones((nb, board_channels), np.float64)
        self.toff = np.zeros((nb, board_channels), np.float64)
        self.rows = rows
        self.installed = np.zeros(len(COUNTERS), np.bool_)
        for s, c, kind, p, ch in rows:
            if s not in bidx or not (0 <= c < board_channels):
                continue
            b = bidx[s]
            if kind == "trk":
                self.plane[b, c] = p; self.ch[b, c] = ch
            elif kind in COUNTERS:
                k = COUNTERS.index(kind)
                self.plane[b, c] = -1; self.counter[b, c] = k; self.installed[k] = True
        if gains:
            for (s, c), v in gains.items():
                if s in bidx and 0 <= c < board_channels:
                    self.gain[bidx[s], c] = v
        if offsets_ns:
            for (s, c), v in offsets_ns.items():
                if s in bidx and 0 <= c < board_channels:
                    self.toff[bidx[s], c] = v
        # occupancy index: one slot per (board, channel)
        self.n_slots = nb * board_channels


# ---------------------------------------------------------------- kernel
@njit(cache=True, nogil=True)
def _one_track_light(P, Q, p):
    if p <= P[0]:
        return Q[0]
    if p >= P[P.size - 1]:
        return Q[Q.size - 1]
    lo = 0; hi = P.size - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if P[mid] <= p:
            lo = mid
        else:
            hi = mid
    f = (p - P[lo]) / (P[hi] - P[lo])
    return Q[lo] + f * (Q[hi] - Q[lo])


@njit(cache=True, nogil=True)
def _bin(x, lo, hi, n):
    if not (x >= lo and x < hi):
        return -1
    return int((x - lo) / (hi - lo) * n)


@njit(cache=True, nogil=True)
def _scratch():
    return (np.empty((3, MAXB), np.int64), np.empty((3, MAXB + 1), np.int64), np.zeros(3, np.int64),
            np.zeros(3, np.int64), np.zeros(3, np.int64), np.zeros((3, RMAX), np.int64), np.zeros((3, RMAX), np.int64),
            np.zeros((3, RMAX)), np.zeros((3, RMAX)), np.zeros((3, RMAX)), np.zeros((3, RMAX)))


@njit(cache=True, nogil=True)
def _fast_bunch(nb, bp, bc, bq, bt, lut, map_p, gang, sigma_t, light_p, light_q, s_mm, rule, k, has_nsig, nsig,
                max_cands, max_combos, veto, cher, cl_size, trk, W):
    """One bunch: bp/bc/bq/bt[:nb] = plane, channel, light, time of its tracker hits.
    Fills cl_size[3, NCL] for every cluster. Returns a status index, or SLOW when the reference
    has to decide. For an accepted single track, trk = [p, R0, R1, R2, res01, res12, dt12, ncand] with
    res = measured minus expected flight time between the planes, in ns."""
    # scratch (allocated once by the caller): W = (idx, cstart, cnt, ncl, nr, r_code, r_kind, r_light, r_pair, r_t, r_sg)
    idx, cstart, cnt, ncl, nr, r_code, r_kind, r_light, r_pair, r_t, r_sg = W
    # per plane: hits sorted by (channel, light, time), as the reference's sorted(hits)
    for p in range(3):
        cnt[p] = 0; ncl[p] = 0; nr[p] = 0
    for j in range(nb):
        p = bp[j]
        idx[p, cnt[p]] = j
        cnt[p] += 1
    for p in range(3):
        m = cnt[p]
        for a in range(1, m):                 # insertion sort, bunches are small
            x = idx[p, a]; b = a - 1
            while b >= 0:
                y = idx[p, b]
                if bc[y] < bc[x] or (bc[y] == bc[x] and (bq[y] < bq[x] or (bq[y] == bq[x] and bt[y] <= bt[x]))):
                    break
                idx[p, b + 1] = y; b -= 1
            idx[p, b + 1] = x
    # clusters: adjacent channels (a repeated channel starts a new cluster, as in the reference)
    for p in range(3):
        m = cnt[p]
        if m == 0:
            continue
        cstart[p, 0] = 0; ncl[p] = 1
        for a in range(1, m):
            if bc[idx[p, a]] != bc[idx[p, a - 1]] + 1:
                cstart[p, ncl[p]] = a; ncl[p] += 1
        cstart[p, ncl[p]] = m
        for c in range(ncl[p]):
            size = cstart[p, c + 1] - cstart[p, c]
            cl_size[p, min(size, NCL) - 1] += 1
    if cnt[0] == 0 or cnt[1] == 0 or cnt[2] == 0:
        return 1                              # incomplete
    # readings, in the reference's order: per cluster, n <= 2: one (+ split 0, split 1);
    # n >= 3: every channel, then every adjacent pair
    for p in range(3):
        for c in range(ncl[p]):
            s0 = cstart[p, c]; n = cstart[p, c + 1] - s0
            if n <= 2:
                if nr[p] + 3 > RMAX:
                    return SLOW
                a = idx[p, s0]
                if n == 1:
                    r_code[p, nr[p]] = bc[a]; r_light[p, nr[p]] = 0.0 + bq[a]; r_kind[p, nr[p]] = K_ONE
                    r_pair[p, nr[p]] = 0.0 + bq[a]; r_t[p, nr[p]] = (0.0 + bt[a]) / 1
                    r_sg[p, nr[p]] = sigma_t[gang[p, bc[a]]] / math.sqrt(1.0)
                    nr[p] += 1
                else:
                    b = idx[p, s0 + 1]
                    pl = (0.0 + bq[a]) + bq[b]
                    r_code[p, nr[p]] = bc[a] if bq[a] >= bq[b] else bc[b]
                    r_light[p, nr[p]] = pl; r_kind[p, nr[p]] = K_ONE; r_pair[p, nr[p]] = pl
                    r_t[p, nr[p]] = ((0.0 + bt[a]) + bt[b]) / 2
                    r_sg[p, nr[p]] = sigma_t[max(gang[p, bc[a]], gang[p, bc[b]])] / math.sqrt(2.0)
                    nr[p] += 1
                    if rule[p] != 0:
                        for kk in range(2):
                            h = a if kk == 0 else b
                            r_code[p, nr[p]] = bc[h]; r_light[p, nr[p]] = bq[h]; r_kind[p, nr[p]] = K_SPLIT
                            r_pair[p, nr[p]] = pl; r_t[p, nr[p]] = bt[h]
                            r_sg[p, nr[p]] = sigma_t[gang[p, bc[h]]] / math.sqrt(1.0)
                            nr[p] += 1
            else:
                if nr[p] + 2 * n - 1 > RMAX:
                    return SLOW
                for kk in range(n):
                    h = idx[p, s0 + kk]
                    r_code[p, nr[p]] = bc[h]; r_light[p, nr[p]] = bq[h]; r_kind[p, nr[p]] = K_SUB
                    r_pair[p, nr[p]] = 0.0; r_t[p, nr[p]] = bt[h]
                    r_sg[p, nr[p]] = sigma_t[gang[p, bc[h]]] / math.sqrt(1.0)
                    nr[p] += 1
                for kk in range(n - 1):
                    h = idx[p, s0 + kk]; h2 = idx[p, s0 + kk + 1]
                    r_code[p, nr[p]] = bc[h] if bq[h] >= bq[h2] else bc[h2]
                    r_light[p, nr[p]] = bq[h] + bq[h2]; r_kind[p, nr[p]] = K_SUB
                    r_pair[p, nr[p]] = 0.0; r_t[p, nr[p]] = 0.5 * (bt[h] + bt[h2])
                    r_sg[p, nr[p]] = sigma_t[max(gang[p, bc[h]], gang[p, bc[h2]])] / math.sqrt(2.0)
                    nr[p] += 1
    if nr[0] * nr[1] * nr[2] > max_combos:
        return 6                              # dropped: more combinations than the finder will try
    # candidates, in itertools.product order, a code counted once; two candidates go to the reference
    seen0 = np.int64(0); ns = 0
    ncand = 0
    c0 = 0; c1 = 0; c2 = 0; cp = 0.0; cL = 0.0
    for i0 in range(nr[0]):
        for i1 in range(nr[1]):
            for i2 in range(nr[2]):
                a0 = r_code[0, i0]; a1 = r_code[1, i1]; a2 = r_code[2, i2]
                key = (a0 * 4096 + a1) * 4096 + a2
                if ns > 0 and seen0 == key:
                    continue
                if a0 >= lut.shape[0] or a1 >= lut.shape[1] or a2 >= lut.shape[2]:
                    continue
                mi = lut[a0, a1, a2]
                if mi < 0:
                    continue
                pm = map_p[mi]
                L = _one_track_light(light_p, light_q, pm)
                if has_nsig:
                    beta = pm / math.hypot(pm, 139.57)
                    d0 = (s_mm[1] - s_mm[0]) / (beta * 299.792458)
                    d1 = (s_mm[2] - s_mm[1]) / (beta * 299.792458)
                    if (abs(r_t[1, i1] - r_t[0, i0] - d0) > nsig * math.hypot(r_sg[0, i0], r_sg[1, i1])
                            or abs(r_t[2, i2] - r_t[1, i1] - d1) > nsig * math.hypot(r_sg[1, i1], r_sg[2, i2])):
                        continue
                ok = True
                ii = (i0, i1, i2)
                for p in range(3):
                    if r_kind[p, ii[p]] == K_SPLIT:
                        ok = ok and (rule[p] == 2 or (rule[p] == 1 and r_pair[p, ii[p]] / L > k))
                if ok:
                    seen0 = key; ns += 1
                    ncand += 1
                    if ncand == 1:
                        c0 = i0; c1 = i1; c2 = i2; cp = pm; cL = L
                    else:
                        return SLOW           # two candidates: the set search is the reference's
    trk[7] = ncand
    if ncand == 0:
        return 0                              # none
    # one candidate: one set; multitrack if a non-split reading is brighter than K tracks
    cc = (c0, c1, c2)
    for p in range(3):
        if r_kind[p, cc[p]] != K_SPLIT and r_light[p, cc[p]] / cL > k:
            return 5                          # multitrack
    if veto:
        return 7
    if cher:
        return 8
    trk[0] = cp
    for p in range(3):
        trk[1 + p] = r_light[p, cc[p]] / cL
    beta = cp / math.hypot(cp, 139.57)
    d0 = (s_mm[1] - s_mm[0]) / (beta * 299.792458)
    d1 = (s_mm[2] - s_mm[1]) / (beta * 299.792458)
    trk[4] = r_t[1, c1] - r_t[0, c0] - d0
    trk[5] = r_t[2, c2] - r_t[1, c1] - d1
    trk[6] = r_t[2, c2] - r_t[1, c1]
    return 2                                  # unique


@njit(cache=True, nogil=True)
def _fill_track(trk, h_p, h_r, h_res, h_dt):
    b = _bin(trk[0], 0.0, 1000.0, h_p.size)
    if b >= 0:
        h_p[b] += 1
    for p in range(3):
        b = _bin(trk[1 + p], 0.0, 4.0, h_r.shape[1])
        if b >= 0:
            h_r[p, b] += 1
    for q in range(2):
        b = _bin(trk[4 + q], -15.0, 15.0, h_res.shape[1])
        if b >= 0:
            h_res[q, b] += 1
    b = _bin(trk[6], -15.0, 15.0, h_dt.size)
    if b >= 0:
        h_dt[b] += 1


@njit(cache=True, nogil=True)
def _bunch_kernel(ts, board, chan, energy, order, plane_of, ch_of, counter_of, gain, toff, installed, calibrated,
                  threshold, window, t0, t1,
                  lut, map_p, gang, sigma_t, light_p, light_q, s_mm, rule, k, has_nsig, nsig, max_cands, max_combos,
                  occ, h_nhit, h_status, h_p, h_r, h_res, h_dt, h_cl, h_ncand, h_ntrk, h_lucite, n_fast,
                  sl_start, sl_plane, sl_ch, sl_q, sl_t, sl_aux):
    """Build bunches from the time-ordered hits and decide each one as it closes. Bunches whose
    first hit is in [t0, t1) are kept. Slow bunches are written to the sl_* arrays (CSR);
    returns (n_bunches, n_hits_kept, n_unmapped, n_slow, n_slow_hits, n_overflow, full)."""
    nb_max = plane_of.shape[0]; nc_max = plane_of.shape[1]
    bp = np.empty(MAXB, np.int64); bc = np.empty(MAXB, np.int64)
    bq = np.empty(MAXB, np.float64); bt = np.empty(MAXB, np.float64)
    cl = np.zeros((3, NCL), np.int64)
    trk = np.zeros(8, np.float64)
    W = _scratch()
    nb = 0; aux = 0; nh = 0; over = False
    in_slice = False; first = np.int64(0)
    prev = np.int64(-1) << 62
    n_b = 0; n_hits = 0; n_bad = 0; n_slow = 0; n_sh = 0; n_over = 0
    cap_b = sl_start.size - 1; cap_h = sl_plane.size
    full = False
    n = order.size
    for j in range(n + 1):
        if j < n:
            i = order[j]; t = ts[i]
        else:
            t = prev + window + 1             # flush the last bunch
        if t - prev > window:
            if in_slice:                      # close the current bunch
                n_b += 1
                h_nhit[min(nh, h_nhit.size - 1)] += 1
                if over:
                    n_over += 1
                    h_status[6] += 1          # dropped: more hits than a bunch can hold
                else:
                    for x in range(3):
                        for y in range(NCL):
                            cl[x, y] = 0
                    for x in range(8):
                        trk[x] = 0.0
                    veto = installed[0] and (aux & 1) != 0
                    cher = installed[1] and (aux & 2) != 0
                    st = _fast_bunch(nb, bp, bc, bq, bt, lut, map_p, gang, sigma_t, light_p, light_q, s_mm, rule,
                                     k, has_nsig, nsig, max_cands, max_combos, veto, cher, cl, trk, W)
                    for x in range(3):
                        for y in range(NCL):
                            h_cl[x, y] += cl[x, y]
                    if st == SLOW:
                        if n_slow < cap_b and n_sh + nb <= cap_h:
                            sl_start[n_slow] = n_sh
                            for x in range(nb):
                                sl_plane[n_sh + x] = bp[x]; sl_ch[n_sh + x] = bc[x]
                                sl_q[n_sh + x] = bq[x]; sl_t[n_sh + x] = bt[x]
                            n_sh += nb
                            sl_aux[n_slow] = aux
                            n_slow += 1
                            sl_start[n_slow] = n_sh
                        else:
                            full = True
                    else:
                        n_fast[0] += 1
                        h_status[st] += 1
                        if st != 1:
                            h_ncand[min(int(trk[7]), h_ncand.size - 1)] += 1
                        if st == 2:
                            h_ntrk[1] += 1
                            _fill_track(trk, h_p, h_r, h_res, h_dt)
                            if installed[2]:
                                h_lucite[0 if (aux & 4) != 0 else 1] += 1
            if j == n:
                break
            in_slice = t >= t0 and t < t1
            first = t; nb = 0; aux = 0; nh = 0; over = False
        prev = t
        if not in_slice:
            continue
        nh += 1; n_hits += 1
        b = board[i]; c = chan[i]
        if b < 0 or b >= nb_max or c < 0 or c >= nc_max or plane_of[b, c] == -2:
            n_bad += 1; occ[occ.size - 1] += 1
            continue
        occ[b * nc_max + c] += 1
        p = plane_of[b, c]
        if p == -1:
            aux |= 1 << counter_of[b, c]
            continue
        q = energy[i] * gain[b, c] if calibrated else 1.0
        if calibrated and q < threshold:
            continue
        if nb >= MAXB:
            over = True
            continue
        bp[nb] = p; bc[nb] = ch_of[b, c]; bq[nb] = q
        bt[nb] = (t - first) * 1e-3 - toff[b, c]
        nb += 1
    return n_b, n_hits, n_bad, n_slow, n_sh, n_over, full


# ---------------------------------------------------------------- driver
class Result:
    """Histograms and counters of one slice; add() merges another."""

    def __init__(self, n_slots):
        self.occ = np.zeros(n_slots + 1, np.int64)
        self.h_nhit = np.zeros(NHIT, np.int64)
        self.h_status = np.zeros(len(STATUS), np.int64)
        self.h_p = np.zeros(P_EDGES.size - 1, np.int64)
        self.h_r = np.zeros((3, R_EDGES.size - 1), np.int64)
        self.h_res = np.zeros((2, RES_EDGES.size - 1), np.int64)
        self.h_dt = np.zeros(DT_EDGES.size - 1, np.int64)
        self.h_cl = np.zeros((3, NCL), np.int64)
        self.h_ncand = np.zeros(NCAND, np.int64)
        self.h_ntrk = np.zeros(NTRK, np.int64)
        self.h_lucite = np.zeros(2, np.int64)
        self.n_fast = np.zeros(1, np.int64)
        self.n_bunches = 0; self.n_hits = 0; self.n_bad = 0; self.n_slow = 0; self.n_over = 0
        self.slow_s = 0.0

    ARRAYS = ("occ", "h_nhit", "h_status", "h_p", "h_r", "h_res", "h_dt", "h_cl", "h_ncand", "h_ntrk",
              "h_lucite", "n_fast")
    SCALARS = ("n_bunches", "n_hits", "n_bad", "n_slow", "n_over", "slow_s")

    def add(self, o):
        for a in self.ARRAYS:
            getattr(self, a)[...] += getattr(o, a)
        for a in self.SCALARS:
            setattr(self, a, getattr(self, a) + getattr(o, a))
        return self

    def to_dict(self):
        d = {a: getattr(self, a) for a in self.ARRAYS}
        d.update({a: getattr(self, a) for a in self.SCALARS})
        return d


def _track_from_reference(T, tables, hits, cand):
    """[p, R0, R1, R2, res01, res12, dt12] of a reference candidate, with the kernel's definitions."""
    p = cand["p"]
    d = of.dt_expected(T, p)
    t = [r["t"] for r in cand["readings"]]
    return np.asarray([p, cand["R"][0], cand["R"][1], cand["R"][2],
                       t[1] - t[0] - d[0], t[2] - t[1] - d[1], t[2] - t[1], 0.0], np.float64)


def decide_slow(tables, chans, res, sl_start, sl_plane, sl_ch, sl_q, sl_t, sl_aux, n_slow):
    """The reference decides the exported bunches; fills res like the kernel does."""
    import time as _time
    T = tables.T
    t_start = _time.perf_counter()
    inst = chans.installed
    for s in range(n_slow):
        a, b = sl_start[s], sl_start[s + 1]
        hits = {0: [], 1: [], 2: []}
        for x in range(a, b):
            hits[int(sl_plane[x])].append((int(sl_ch[x]), float(sl_q[x]), float(sl_t[x])))
        aux = int(sl_aux[s])
        veto = bool(aux & 1) if inst[0] else None
        cher = bool(aux & 2) if inst[1] else None
        luc = bool(aux & 4) if inst[2] else None
        r = of.find(T, hits, veto=veto, cherenkov=cher, lucite=luc)
        res.h_status[ST[r["status"]]] += 1
        if r["status"] != "incomplete":
            res.h_ncand[min(len(r["candidates"]), NCAND - 1)] += 1
        if r["tracks"]:
            res.h_ntrk[min(len(r["tracks"]), NTRK - 1)] += 1
            for c in r["tracks"]:
                _fill_track(_track_from_reference(T, tables, hits, c), res.h_p, res.h_r, res.h_res, res.h_dt)
            if luc is not None:
                res.h_lucite[0 if luc else 1] += 1
    res.slow_s += _time.perf_counter() - t_start


def process_sorted(tables, chans, ts, board, chan, energy, order, window_ps, t0=None, t1=None):
    """Bunches of the time-ordered hits (order sorts ts) whose first hit is in [t0, t1)."""
    if t0 is None:
        t0 = np.int64(-1) << 62
    if t1 is None:
        t1 = np.int64(1) << 62
    res = Result(chans.n_slots)
    n = order.size
    cap_b = max(1024, n // 50); cap_h = max(4096, n // 10)
    while True:
        sl_start = np.zeros(cap_b + 1, np.int64)
        sl_plane = np.zeros(cap_h, np.int64); sl_ch = np.zeros(cap_h, np.int64)
        sl_q = np.zeros(cap_h, np.float64); sl_t = np.zeros(cap_h, np.float64)
        sl_aux = np.zeros(cap_b, np.int64)
        r0 = Result(chans.n_slots)
        out = _bunch_kernel(np.ascontiguousarray(ts, np.int64), np.ascontiguousarray(board, np.int64),
                            np.ascontiguousarray(chan, np.int64), np.ascontiguousarray(energy, np.float64),
                            np.ascontiguousarray(order, np.int64), chans.plane, chans.ch, chans.counter,
                            chans.gain, chans.toff, chans.installed, tables.calibrated, tables.threshold,
                            np.int64(window_ps), np.int64(t0), np.int64(t1), *tables.kernel_args(),
                            r0.occ, r0.h_nhit, r0.h_status, r0.h_p, r0.h_r, r0.h_res, r0.h_dt, r0.h_cl,
                            r0.h_ncand, r0.h_ntrk, r0.h_lucite, r0.n_fast,
                            sl_start, sl_plane, sl_ch, sl_q, sl_t, sl_aux)
        n_b, n_hits, n_bad, n_slow, n_sh, n_over, full = out
        if not full:
            break
        cap_b = n + 1; cap_h = n + 1             # rare: more slow bunches than room, run again with room for all
    res.add(r0)
    res.n_bunches, res.n_hits, res.n_bad, res.n_slow, res.n_over = int(n_b), int(n_hits), int(n_bad), int(n_slow), int(n_over)
    decide_slow(tables, chans, res, sl_start, sl_plane, sl_ch, sl_q, sl_t, sl_aux, int(n_slow))
    return res


# ---------------------------------------------------------------- self test
@njit(cache=True)
def _fast_many(starts, bp_all, bc_all, bq_all, bt_all, aux_all, installed, lut, map_p, gang, sigma_t, light_p,
               light_q, s_mm, rule, k, has_nsig, nsig, max_cands, max_combos, st_out, trk_out):
    cl = np.zeros((3, NCL), np.int64)
    trk = np.zeros(8, np.float64)
    W = _scratch()
    for b in range(starts.size - 1):
        a = starts[b]; nb = starts[b + 1] - a
        for x in range(8):
            trk[x] = 0.0
        veto = installed[0] and (aux_all[b] & 1) != 0
        cher = installed[1] and (aux_all[b] & 2) != 0
        st_out[b] = _fast_bunch(nb, bp_all[a:a + nb], bc_all[a:a + nb], bq_all[a:a + nb], bt_all[a:a + nb], lut,
                                map_p, gang, sigma_t, light_p, light_q, s_mm, rule, k, has_nsig, nsig, max_cands,
                                max_combos, veto, cher, cl, trk, W)
        for x in range(8):
            trk_out[b, x] = trk[x]


def _synthetic_bunches(T, n, rng, calibrated):
    """Bunches around real map triplets: one or two tracks, neighbour channels (2-channel clusters),
    stray hits, missing planes, timing jitter, fluctuating light, random counters."""
    codes = [c for c, v in T["map"].items()]
    gang = T["gang"]
    out = []
    for _ in range(n):
        hits = {0: [], 1: [], 2: []}
        ntrk = rng.choice([0, 1, 1, 1, 1, 2, 2, 3])
        for _t in range(ntrk):
            code = codes[rng.integers(len(codes))]
            p = T["map"][code][0]
            d = of.dt_expected(T, p)
            L = of.one_track_light(T, p)
            tt = [0.0, d[0], d[0] + d[1]]
            for i in range(3):
                g = gang[i].get(code[i], 1)
                sg = T["sigma_t"][g] * rng.choice([0.5, 1.0, 1.0, 3.0])
                q = L * rng.gamma(4.0, 0.25) if calibrated else 1.0
                hits[i].append((code[i], q, tt[i] + rng.normal(0, sg)))
                if rng.random() < 0.35:          # the neighbour bar shares the light
                    nb = code[i] + (1 if rng.random() < 0.5 else -1)
                    if nb in gang[i]:
                        q2 = L * rng.gamma(2.0, 0.25) if calibrated else 1.0
                        hits[i].append((nb, q2, tt[i] + rng.normal(0, sg)))
        for _s in range(rng.choice([0, 0, 0, 1, 2])):        # strays
            i = int(rng.integers(3))
            ch = int(rng.integers(1, max(gang[i]) + 1))
            hits[i].append((ch, float(rng.gamma(2.0, 0.5)) if calibrated else 1.0, float(rng.normal(0, 6))))
        if rng.random() < 0.05:                  # a channel twice
            i = int(rng.integers(3))
            if hits[i]:
                ch, q, t = hits[i][0]
                hits[i].append((ch, q, t + 3.0))
        aux = int(rng.integers(8))
        out.append((hits, aux))
    return out


def selftest(n=200000, seed=1):
    rng = np.random.default_rng(seed)
    bad = 0
    for setting in ("high", "low"):
        for calibrated in (False, True):
            tb = Tables(setting, calibrated=calibrated)
            T = tb.T
            bunches = _synthetic_bunches(T, n, rng, calibrated)
            starts = [0]; bp = []; bc = []; bq = []; bt = []; auxv = []
            for hits, aux in bunches:
                for i in range(3):
                    for ch, q, t in hits[i]:
                        bp.append(i); bc.append(ch); bq.append(q); bt.append(t)
                starts.append(len(bp)); auxv.append(aux)
            installed = np.asarray([True, True, True])
            st = np.zeros(len(bunches), np.int64); trk = np.zeros((len(bunches), 8))
            _fast_many(np.asarray(starts, np.int64), np.asarray(bp, np.int64), np.asarray(bc, np.int64),
                       np.asarray(bq, np.float64), np.asarray(bt, np.float64), np.asarray(auxv, np.int64), installed,
                       *tb.kernel_args(), st, trk)
            n_fast = 0; mism = []
            for b, (hits, aux) in enumerate(bunches):
                r = of.find(T, hits, veto=bool(aux & 1), cherenkov=bool(aux & 2), lucite=bool(aux & 4))
                if st[b] == SLOW:
                    continue
                n_fast += 1
                okb = STATUS[st[b]] == r["status"]
                if okb and r["status"] != "incomplete":
                    okb = int(trk[b, 7]) == len(r["candidates"])
                if okb and r["status"] == "unique":
                    ref = _track_from_reference(T, tb, hits, r["tracks"][0])
                    okb = np.allclose(ref[:7], trk[b, :7], rtol=0, atol=1e-9)
                if not okb:
                    mism.append(b)
            bad += len(mism)
            sts = np.bincount([ST[of.find(T, h, veto=bool(a & 1), cherenkov=bool(a & 2), lucite=bool(a & 4))["status"]]
                               for h, a in bunches[:20000]], minlength=len(STATUS))
            print(f"{setting:4s} calibrated={calibrated!s:5s}: {n} bunches, fast path decided {n_fast} "
                  f"({100 * n_fast / n:.1f} %), mismatches {len(mism)}; reference statuses (first 20k): "
                  + ", ".join(f"{STATUS[i]} {c}" for i, c in enumerate(sts) if c))
            for b in mism[:5]:
                hits, aux = bunches[b]
                r = of.find(T, hits, veto=bool(aux & 1), cherenkov=bool(aux & 2), lucite=bool(aux & 4))
                print("   mismatch", b, hits, aux, "fast", STATUS[st[b]] if st[b] >= 0 else "SLOW", trk[b], "ref",
                      r["status"], len(r["candidates"]))
    print("SELFTEST", "PASSED" if bad == 0 else f"FAILED ({bad} mismatches)")
    return bad == 0


def selftest_stream(n=50000, seed=2):
    """The whole kernel (bunch building, cabling, gains, offsets, slow export, histograms) against
    bunches built in plain Python and decided by the reference alone: every histogram must agree."""
    rng = np.random.default_rng(seed)
    serials = [15880, 15879, 58918, 62839, 1443, 30508, 11111, 22222, 33333]   # 9 boards: 113 channels fit
    nch = {s: (8 if s in (1443, 30508) else 16) for s in serials}
    rows = default_cabling(serials, nch)
    ok_all = True
    for calibrated in (False, True):
        tb = Tables("high", calibrated=calibrated)
        T = tb.T
        gains = {(s, c): float(rng.uniform(0.002, 0.004)) for s, c, *_ in rows}
        offs = {(s, c): float(rng.normal(0, 2.0)) for s, c, *_ in rows}
        chans = Channels(serials, rows, gains if calibrated else None, offs)
        where = {}                               # (kind, plane, ch) -> (board index, channel)
        for s, c, kind, p, ch in rows:
            where[(kind, p, ch)] = (serials.index(s), c)
        bunches = _synthetic_bunches(T, n, rng, calibrated)
        ts = []; bd = []; chv = []; en = []
        t_bunch = 1_000_000
        window = 100_000                         # ps
        for hits, aux in bunches:
            hl = []
            for i in range(3):
                for ch, q, t in hits[i]:
                    if ("trk", i, ch) not in where:
                        continue
                    b, c = where[("trk", i, ch)]
                    e = max(1, int(round(q / gains[(serials[b], c)]))) if calibrated else int(rng.integers(100, 4000))
                    hl.append((t + offs[(serials[b], c)], b, c, e))
            for k, name in enumerate(COUNTERS):
                if aux & (1 << k):
                    b, c = where[(name, -1, 0)]
                    hl.append((float(rng.uniform(-5, 5)), b, c, 1000))
            if not hl:
                continue
            tmin = min(h[0] for h in hl)
            for t, b, c, e in hl:                # spread inside 60 ns, so a bunch stays one bunch
                ts.append(t_bunch + int(round((t - tmin + 20.0) * 1000))); bd.append(b); chv.append(c); en.append(e)
            t_bunch += int(rng.integers(250_000, 2_000_000))
        ts = np.asarray(ts, np.int64); bd = np.asarray(bd, np.int64); chv = np.asarray(chv, np.int64)
        en = np.asarray(en, np.float64)
        order = np.argsort(ts, kind="stable")
        res = process_sorted(tb, chans, ts, bd, chv, en, order, window)
        # the same bunches, built in Python and decided by the reference only
        ref = Result(chans.n_slots)
        o = order
        starts = np.flatnonzero(np.diff(np.concatenate([[ts[o[0]] - 2 * window], ts[o]])) > window)
        bounds = list(starts) + [o.size]
        for a, b in zip(bounds[:-1], bounds[1:]):
            first = ts[o[a]]
            hits = {0: [], 1: [], 2: []}; aux = 0
            ref.h_nhit[min(b - a, NHIT - 1)] += 1
            for x in o[a:b]:
                bi, c = int(bd[x]), int(chv[x])
                p = chans.plane[bi, c]
                ref.occ[bi * chans.board_channels + c] += 1
                if p == -1:
                    aux |= 1 << int(chans.counter[bi, c]); continue
                q = en[x] * chans.gain[bi, c] if calibrated else 1.0
                if calibrated and q < tb.threshold:
                    continue
                hits[int(p)].append((int(chans.ch[bi, c]), float(q), (ts[x] - first) * 1e-3 - chans.toff[bi, c]))
            r = of.find(T, hits, veto=bool(aux & 1), cherenkov=bool(aux & 2), lucite=bool(aux & 4))
            ref.h_status[ST[r["status"]]] += 1
            if r["status"] != "incomplete":
                ref.h_ncand[min(len(r["candidates"]), NCAND - 1)] += 1
            if r["tracks"]:
                ref.h_ntrk[min(len(r["tracks"]), NTRK - 1)] += 1
                for c in r["tracks"]:
                    _fill_track(_track_from_reference(T, tb, hits, c), ref.h_p, ref.h_r, ref.h_res, ref.h_dt)
                ref.h_lucite[0 if aux & 4 else 1] += 1
            for i in range(3):
                for cc in of.clusters(hits[i]):
                    ref.h_cl[i, min(len(cc["chans"]), NCL) - 1] += 1
        diffs = [a for a in ("occ", "h_nhit", "h_status", "h_p", "h_r", "h_res", "h_dt", "h_cl", "h_ncand", "h_ntrk",
                             "h_lucite") if not np.array_equal(getattr(res, a), getattr(ref, a))]
        ok = not diffs and res.n_bunches == len(bounds) - 1
        ok_all &= ok
        print(f"stream calibrated={calibrated!s:5s}: {res.n_bunches} bunches ({len(bounds) - 1} in Python), "
              f"{res.n_fast[0]} fast / {res.n_slow} reference ({res.slow_s:.2f} s), statuses "
              + ", ".join(f"{STATUS[i]} {c}" for i, c in enumerate(res.h_status) if c)
              + ("" if ok else f"  DIFFERENT: {diffs}"))
    print("STREAM SELFTEST", "PASSED" if ok_all else "FAILED")
    return ok_all


if __name__ == "__main__":
    if "--selftest-stream" in sys.argv:
        sys.exit(0 if selftest_stream() else 1)
    if "--selftest" in sys.argv:
        sys.exit(0 if selftest(int(sys.argv[sys.argv.index("--selftest") + 1]) if len(sys.argv) > sys.argv.index("--selftest") + 1 else 200000) else 1)
    print(__doc__)
