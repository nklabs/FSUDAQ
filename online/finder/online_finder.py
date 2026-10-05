#!/usr/bin/env python3
"""
Online track finder, reference implementation. Standard library only.

One BUNCH in, a decision out. The whole array is read together: a pion is spoiled only if the bunch's full
set of hits cannot reconstruct it uniquely, so the finder works on every hit of every plane at once.

    1  cluster each plane: adjacent fired channels form one cluster
    2  read each cluster as hits ("readings"): a 1-2 channel cluster is one hit at its largest channel (WTA);
       a 2-channel cluster may ALSO be two hits, one per channel, if its light says two tracks; a 3+ channel
       cluster is read as every channel and every adjacent pair
    3  every one-reading-per-plane combination whose channel triplet is in the momentum map is a CANDIDATE if
         - its light is consistent: no reading brighter than K x one track's light, except a 2-hit split
         - its time differences match one track of the map momentum: |t1 - t0 - dt01(p)| <= NSIG sigma01,
           the same for t2 - t1 (sigma from the channels' ganging)
    4  find the largest sets of candidates that can all be real at once (no shared hit, unless that hit's light
       says two tracks, and then no more tracks share it than its light holds)
         exactly one such set -> every candidate in it is ACCEPTED (status 'unique' or 'tracks')
         several sets         -> 'ambiguous' (nothing accepted; every candidate is returned for the record)
         one set, but a hit brighter than K x one track that no other track explains -> 'multitrack'
    5  counters: entrance veto fired on an accepted bunch -> 'veto'; gas Cherenkov fired -> 'electron'.
       The lucite bit is a TAG only (recorded, never rejects).

Momentum comes from the triplet only (the map is particle-agnostic). Any counter may be None = no information;
None never rejects.

    python3 online_finder.py            # loads the map for SETTING and runs two example bunches
"""
import csv
import itertools
import json
import math
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SETTING = "high"            # which T1/T2 position is installed. Per RUN, not per event.


# ---------------------------------------------------------------- tables (load once at startup)
def load(setting=SETTING, here=HERE, map_path=None):
    """-> dict with the map {(ch0, ch1, ch2): (p, sigma, n_cal)}, channel gang widths, the one-track light
    table and the finder constants, for ONE setting. Settings alternating spill by spill: load both,
    {s: load(s) for s in ("high", "low")}, and pass the spill's table to find().
    map_path: any map with the columns setting, ch0, ch1, ch2, p_mev, sigma_mev [, n_cal]. Rows with n_cal below
    the finder's map_min_n are not loaded; a map without n_cal (e.g. from beam calibration) is used as is."""
    meta = json.load(open(os.path.join(here, "handoff_meta.json")))
    min_n = meta["finder"]["map_min_n"]
    table = {}
    with open(map_path or os.path.join(here, "momentum_map.csv"), newline="") as f:
        for r in csv.DictReader(f):
            if r["setting"] != setting:
                continue
            n_cal = int(r["n_cal"]) if r.get("n_cal") not in (None, "") else None
            if n_cal is not None and n_cal < min_n:
                continue
            table[(int(r["ch0"]), int(r["ch1"]), int(r["ch2"]))] = (float(r["p_mev"]), float(r["sigma_mev"]), n_cal)
    gang = {0: {}, 1: {}, 2: {}}
    with open(os.path.join(here, "channels.csv"), newline="") as f:
        for r in csv.DictReader(f):
            if r["setting"] == setting:
                gang[int(r["plane"])][int(r["ch"])] = int(r["gang_width"])
    light_p, light_q = [], []
    with open(os.path.join(here, "pion_light.csv"), newline="") as f:
        for r in csv.DictReader(f):
            light_p.append(float(r["p_mev"])); light_q.append(float(r["light_mevee"]))
    c = meta["finder"]
    return dict(map=table, gang=gang, light_p=light_p, light_q=light_q, s_mm=meta["planes_s_mm"][setting],
                k=c["two_track_k"], nsig=c["dt_nsig"], sigma_t={int(g): v for g, v in c["sigma_t_ns_by_gang"].items()},
                pair_rule=c["pair_rule"], max_combos=c["max_combos"], max_cands=c["max_cands"])


def one_track_light(T, p):
    """mean light [MeVee] of one pion of momentum p straight through a bar centre (linear in the table)."""
    P, Q = T["light_p"], T["light_q"]
    if p <= P[0]:
        return Q[0]
    if p >= P[-1]:
        return Q[-1]
    lo, hi = 0, len(P) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        lo, hi = (mid, hi) if P[mid] <= p else (lo, mid)
    f = (p - P[lo]) / (P[hi] - P[lo])
    return Q[lo] + f * (Q[hi] - Q[lo])


def dt_expected(T, p):
    """one pion of momentum p: expected (t1 - t0, t2 - t1) in ns, from the planes' positions along the arm."""
    beta = p / math.hypot(p, 139.57)
    s = T["s_mm"]
    return ((s[1] - s[0]) / (beta * 299.792458), (s[2] - s[1]) / (beta * 299.792458))


# ---------------------------------------------------------------- 1 clusters
def clusters(hits):
    """hits: [(ch, q_mevee, t_ns)] of ONE plane (fired channels, above threshold, inside the gate).
    -> clusters of adjacent channels, in channel order: dict(chans, q, ts, t = mean time)."""
    out = []
    for ch, q, t in sorted(hits):
        if out and ch == out[-1]["chans"][-1] + 1:
            c = out[-1]
            c["chans"].append(ch); c["q"].append(q); c["ts"].append(t)
        else:
            out.append(dict(chans=[ch], q=[q], ts=[t]))
    for c in out:
        c["t"] = sum(c["ts"]) / len(c["ts"])
    return out


# ---------------------------------------------------------------- 2 readings
def readings(cl, rule):
    """ways to read one plane's clusters as hits. kind: one (the cluster as one track), split (one channel of a
    2-channel cluster as its own hit), sub (a channel or adjacent pair inside a 3+ channel cluster)."""
    out = []
    for ic, c in enumerate(cl):
        ch, q, n = c["chans"], c["q"], len(c["chans"])

        def code(pos):                       # WTA: the larger-light channel of the reading
            return ch[pos[0]] if len(pos) == 1 or q[pos[0]] >= q[pos[1]] else ch[pos[1]]
        if n <= 2:
            pos = tuple(range(n))
            out.append(dict(ic=ic, pos=pos, code=code(pos), light=sum(q), kind="one", pair_light=sum(q), t=c["t"]))
            if n == 2 and rule != "never":
                for k in (0, 1):
                    out.append(dict(ic=ic, pos=(k,), code=ch[k], light=q[k], kind="split", pair_light=sum(q), t=c["ts"][k]))
        else:
            for k in range(n):
                out.append(dict(ic=ic, pos=(k,), code=ch[k], light=q[k], kind="sub", t=c["ts"][k]))
            for k in range(n - 1):
                out.append(dict(ic=ic, pos=(k, k + 1), code=code((k, k + 1)), light=q[k] + q[k + 1], kind="sub",
                                t=0.5 * (c["ts"][k] + c["ts"][k + 1])))
    return out


# ---------------------------------------------------------------- 3 candidates
def candidates(T, cls):
    """-> (list of candidates, number of combinations), or (None, n) when there are more than max_combos."""
    per = [readings(cls[i], T["pair_rule"][i]) for i in range(3)]
    n = len(per[0]) * len(per[1]) * len(per[2])
    if n == 0:
        return [], 0
    if n > T["max_combos"]:
        return None, n
    k, out, seen = T["k"], [], set()
    for combo in itertools.product(*per):
        code = tuple(x["code"] for x in combo)
        if code in seen or code not in T["map"]:
            continue
        p = T["map"][code][0]
        L = one_track_light(T, p)
        if T["nsig"] is not None:
            d = dt_expected(T, p)
            sg = [T["sigma_t"][max(T["gang"][i][cls[i][x["ic"]]["chans"][j]] for j in x["pos"])] / math.sqrt(len(x["pos"]))
                  for i, x in enumerate(combo)]
            if (abs(combo[1]["t"] - combo[0]["t"] - d[0]) > T["nsig"] * math.hypot(sg[0], sg[1])
                    or abs(combo[2]["t"] - combo[1]["t"] - d[1]) > T["nsig"] * math.hypot(sg[1], sg[2])):
                continue
        ok, R = True, []
        for i, x in enumerate(combo):
            R.append(x["light"] / L)
            if x["kind"] == "split":
                rule = T["pair_rule"][i]
                ok &= rule == "always" or (rule == "light" and x["pair_light"] / L > k)
        if ok:
            seen.add(code)
            out.append(dict(code=code, readings=combo, R=R, L=L, p=p, sigma=T["map"][code][1], n_cal=T["map"][code][2]))
    return out, n


# ---------------------------------------------------------------- 4 which candidates can all be real
def shares_ok(T, c1, c2):
    for i in range(3):
        r1, r2 = c1["readings"][i], c2["readings"][i]
        if r1["ic"] != r2["ic"] or not (set(r1["pos"]) & set(r2["pos"])):
            continue
        if r1["pos"] != r2["pos"] or r1["kind"] == "split" or r2["kind"] == "split":
            return False
        if r1["light"] / min(c1["L"], c2["L"]) <= T["k"]:
            return False
    return True


def best_sets(T, cands):
    n = len(cands)
    ok = [[i != j and shares_ok(T, cands[i], cands[j]) for j in range(n)] for i in range(n)]
    best, sets = 0, []

    def fits(S):
        use = {}
        for i in S:
            for p, r in enumerate(cands[i]["readings"]):
                if r["kind"] != "split":
                    use.setdefault((p, r["ic"], r["pos"]), []).append(i)
        for (p, ic, pos), who in use.items():
            if len(who) > 1:
                light = cands[who[0]]["readings"][p]["light"]
                if len(who) > max(1, round(light / min(cands[i]["L"] for i in who))):
                    return False
        return True

    def grow(S, rest):
        nonlocal best, sets
        if len(S) + len(rest) < best:
            return
        if not rest:
            if len(S) > best:
                best, sets = len(S), [S]
            elif len(S) == best and S:
                sets.append(S)
            return
        i, rest2 = rest[0], rest[1:]
        if fits(S + [i]):
            grow(S + [i], [j for j in rest2 if ok[i][j]])
        grow(S, rest2)

    grow([], list(range(n)))
    return sets


def unexplained_multi(T, S, cands):
    for i in S:
        for p, r in enumerate(cands[i]["readings"]):
            if r["kind"] == "split" or cands[i]["R"][p] <= T["k"]:
                continue
            if not any(j != i and cands[j]["readings"][p]["ic"] == r["ic"] and cands[j]["readings"][p]["pos"] == r["pos"]
                       for j in S):
                return True
    return False


# ---------------------------------------------------------------- one bunch
def find(T, hits, veto=None, cherenkov=None, lucite=None):
    """hits: {0: [(ch, q, t)], 1: [...], 2: [...]} fired channels per plane (1-based channels, light in MeVee,
    time in ns on a common clock). veto / cherenkov / lucite: True, False or None (no information).
    -> dict(status, tracks [accepted candidates], candidates [all valid ones], tags)."""
    tags = [] if lucite is None else ["LUCITE_YES" if lucite else "LUCITE_NO"]
    cls = [clusters(hits.get(i, [])) for i in range(3)]
    if any(len(c) == 0 for c in cls):
        return dict(status="incomplete", tracks=[], candidates=[], tags=tags)
    cands, n = candidates(T, cls)
    if cands is None:
        return dict(status="dropped", tracks=[], candidates=[], tags=tags)
    if not cands:
        return dict(status="none", tracks=[], candidates=[], tags=tags)
    if len(cands) > T["max_cands"]:
        return dict(status="dropped", tracks=[], candidates=cands, tags=tags)
    sets = best_sets(T, cands)
    if len(sets) != 1:
        return dict(status="ambiguous", tracks=[], candidates=cands, tags=tags)
    S = sets[0]
    if unexplained_multi(T, S, cands):
        return dict(status="multitrack", tracks=[], candidates=cands, tags=tags)
    status = "unique" if len(S) == 1 else "tracks"
    if veto:
        status = "veto"
    elif cherenkov:
        status = "electron"
    tracks = [cands[i] for i in S] if status in ("unique", "tracks") else []
    return dict(status=status, tracks=tracks, candidates=cands, tags=tags)


if __name__ == "__main__":
    T = load()
    print(f"{len(T['map'])} triplets in the map, setting {SETTING}")
    code = next(c for c, v in T["map"].items() if 380 < v[0] < 400 and v[2] >= 50)
    p = T["map"][code][0]
    d = dt_expected(T, p)
    L = one_track_light(T, p)
    clean = {0: [(code[0], L, 0.0)], 1: [(code[1], L, d[0])], 2: [(code[2], L, d[0] + d[1])]}
    r = find(T, clean, veto=False, cherenkov=False)
    print(f"  one pion on {code}:  {r['status']:10s} p = {r['tracks'][0]['p']:.1f} +- {r['tracks'][0]['sigma']:.1f} MeV/c")
    # a stray T2 hit that also makes a valid triplet with the pion's T0 and T1 hits (case 3): they compete
    alt = next(c for c in T["map"] if c[:2] == code[:2] and abs(c[2] - code[2]) > 1)
    stray = dict(clean); stray[2] = clean[2] + [(alt[2], L, d[0] + d[1])]
    r = find(T, stray, veto=False, cherenkov=False)
    print(f"  + a stray T2 hit on {alt[2]}: {r['status']:10s} ({len(r['candidates'])} valid candidates, nothing accepted)")
    r = find(T, clean, veto=True, cherenkov=False)
    print(f"  one pion, veto fired:  {r['status']:10s}")
