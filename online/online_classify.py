#!/usr/bin/env python3
"""
Online pion classifier. Standard library only.

Triplet (ch0, ch1, ch2) -> momentum, via one hash lookup. No fit, no iteration.
A triplet ABSENT from the map is one no single track can make: absence is a veto.

Momentum comes from the triplet and NOTHING else. The map is particle-agnostic -- an
electron and a pion of the same charge and momentum bend identically -- so the momentum
is reported whatever the counters said. The class only says how much to trust it.

Any counter may be None = no information (not installed / not read out). None NEVER
sets a class; it is not the same as False.
"""
import csv
import math

SETTING = 'high'            # which T1/T2 position is installed. Per run, not per event.
TOF_BASELINE_CM = 96.589    # z(T2) - z(T0)
TOF_SIGMA_NS = 0.150        # must match the front end producing tof_ns
TOF_N_SIGMA = 4.0
PION_MASS_MEV = 139.57039
C_CM_PER_NS = 29.9792458


def load_map(path='momentum_map.csv', setting=SETTING):
    """{(ch0, ch1, ch2): (p_mev, sigma_mev, ok)}. Load once at startup."""
    table = {}
    with open(path, newline='') as f:
        for r in csv.DictReader(f):
            if r['setting'] != setting:
                continue
            table[(int(r['ch0']), int(r['ch1']), int(r['ch2']))] = (
                float(r['p_mev']), float(r['sigma_mev']), r['ok'] == '1')
    return table


def decode_wta(adc, threshold):
    """Largest amplifier wins; 0 if it is under threshold. Channels are 1-based.
    Online is winner-takes-all, so a channel number IS an amplifier number.
    Skip this if the firmware already reports a channel."""
    if not adc:
        return 0
    i = max(range(len(adc)), key=lambda k: adc[k])
    return i + 1 if adc[i] >= threshold else 0


def classify(ch0, ch1, ch2, table, coll_veto=None, cg_electron=None, lucite=None,
             edge_high_p=None, edge_low_p=None, bunch_multi=None, tof_ns=None):
    """-> (cls, p_mev, sigma_mev, tags). p and sigma are None when the triplet misses."""
    tags = []
    if lucite is not None:
        tags.append('LUCITE_YES' if lucite else 'LUCITE_NO')
    if edge_high_p:
        tags.append('ESCAPED_HIGH_P')
    if edge_low_p:
        tags.append('ESCAPED_LOW_P')

    hit = table.get((ch0, ch1, ch2))
    if hit is None:
        # No momentum. The number of planes that fired says which kind of miss it is.
        # UNPHYSICAL_3 is the interesting one: three planes, but no single track can
        # produce that combination -- accidental, scatter, decay kink, or two tracks.
        n = (ch0 > 0) + (ch1 > 0) + (ch2 > 0)
        return ('EMPTY', 'SINGLE', 'DOUBLE', 'UNPHYSICAL_3')[n], None, None, tags

    p, sigma, ok = hit
    if not ok:
        tags.append('LOW_STATS')      # few calibration tracks; sigma is the geometric
                                      # floor rather than a measured width

    # TOF: the only thing that separates a proton from a stiff pion. Compare the
    # measured time against the time a PION of this momentum would have taken.
    # ONE-SIDED: only a LATE arrival is heavier. Early means lighter (an electron),
    # which is the Cherenkov's job.
    heavy = False
    if tof_ns is not None and tof_ns > 0:
        beta_pi = p / math.sqrt(p * p + PION_MASS_MEV * PION_MASS_MEV)
        t_pi = TOF_BASELINE_CM / (beta_pi * C_CM_PER_NS)
        heavy = (tof_ns - t_pi) > TOF_N_SIGMA * TOF_SIGMA_NS
        if heavy:
            tags.append('TOF_HEAVY')

    # Class precedence = how badly the momentum is compromised.
    # scattered off the jaws > mixture of tracks > wrong mass > wrong species > pion
    if coll_veto:
        cls = 'COLLIMATOR'
    elif bunch_multi:
        cls = 'MULTITRACK'
    elif heavy:
        cls = 'HEAVY'
    elif cg_electron:
        cls = 'ELECTRON'
    else:
        cls = 'PION'
    return cls, p, sigma, tags


if __name__ == '__main__':
    m = load_map()
    print(f'{len(m)} triplets, setting {SETTING}')
    for ev in ((8, 26, 37, {}),
               (8, 26, 37, {'cg_electron': True}),
               (8, 26, 0, {}),
               (7, 33, 49, {})):
        cls, p, s, tags = classify(ev[0], ev[1], ev[2], m, **ev[3])
        print(f'  {ev[0]:>3},{ev[1]:>3},{ev[2]:>3}  {cls:13s} '
              f'p={"-" if p is None else f"{p:7.1f}"}  {",".join(tags)}')
