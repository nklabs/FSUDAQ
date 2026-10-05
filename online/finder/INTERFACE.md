# Online track finder: interface

The online logic takes the fired channels of one bunch and returns the tracks it accepts. It knows nothing about
where the hits came from. It needs four things:
* the channel layout;
* a momentum map;
* one track's expected light;
* a handful of constants.

| file | contents |
|------|---|
| `online_finder.py` | reference implementation, about 300 lines, standard library only |
| `momentum_map.csv` | channel triplet -> momentum, both settings (high 3,420 rows, low 4,208) |
| `channels.csv` | per setting, plane and channel: gang width (bars per amplifier) and position span |
| `pion_light.csv` | light [MeVee] of one pion through a bar centre, vs momentum (20–1500 MeV/c) |
| `handoff_meta.json` | provenance, plane positions, threshold, gate, finder constants |

`python3 online_finder.py` runs three example bunches. The reference implementation was checked bunch by bunch
against the simulation's finder, and they agree on every bunch.

## Settings

The T1/T2 planes have two positions: `high` and `low`. The finder never decides the setting; the caller tells it.
* `load(setting)` builds the tables for one setting: that setting's map rows, its channel table and its plane
  positions.
* `find(T, ...)` uses whichever table it is given. Using the wrong setting's table returns confidently wrong
  momenta.
* **One setting per run:** `T = load("high")`.
* **Settings alternating spill by spill:** load both once, `T = {s: load(s) for s in ("high", "low")}`, and pass
  `T[spill_setting]` for each spill. The setting has to come with the spill, from the hardware state, not from
  the data.

Only the plane positions across the arm differ between the settings. The channel layout and gang widths are the
same, and so are the distances along the arm that the time test uses.

## Plugging in a new momentum map

`load(setting, map_path="new_map.csv")`. The map is the only piece that depends on calibration.

* **Required columns:** `setting, ch0, ch1, ch2, p_mev, sigma_mev`.
* **Optional:** `n_cal`, the number of calibration tracks behind the row. If it is present, rows below
  `finder.map_min_n` (5) are not loaded. If it is absent (for example, a map from beam calibration), every row
  is used.
* **Channel conventions:**
  * channel numbers are 1-based amplifier numbers, as in `channels.csv`;
  * a triplet's channel on each plane is the reading's **largest-light** channel (winner-takes-all).

  A map built any other way (an interpolated readout, a different ganging) needs the matching change in
  `readings()`.
* **The map defines what counts as a track.** A triplet absent from the map can never be a candidate. A map
  built at a different threshold, or with a different minimum count, changes the acceptance. It does not just
  change the momenta.
* **What else uses the map's momentum:** the light test (`pion_light.csv` at the map p) and the time test
  (β at the map p). Nothing else needs to change with a new map.
* If the channel layout itself changes, regenerate `channels.csv` too.

## Input per bunch

* For each plane, the **fired channels** `(ch, light, t)`:
  * channels are 1-based;
  * a channel is fired when its light is at or above the threshold;
  * light is in the units of `pion_light.csv` (MeVee);
  * time is in ns on a common clock, with cable delays removed;
  * only hits inside the gate are passed (`gate_ns` around the expected arrival at that plane).
* `veto`, `cherenkov`, `lucite`: True, False or **None**. None means no information (not installed, not read
  out). None never rejects, and it is not the same as False.

## What the finder does (one bunch)

1. **Cluster** each plane: adjacent fired channels form one cluster.
2. **Readings**: the ways a cluster can be hits.
   * A 1- or 2-channel cluster is one hit at its larger channel.
   * A 2-channel cluster can **also** be two hits, one per channel, but only if its summed light is more than
     `two_track_k` (1.4) × one track's light.
   * A 3+ channel cluster is read as every channel and every adjacent pair.
3. **Candidates**: every one-reading-per-plane combination whose triplet is in the map, and that passes both
   tests below.
   * **Time:** |t1 − t0 − Δs01/βc| ≤ `dt_nsig` × σ01 and |t2 − t1 − Δs12/βc| ≤ `dt_nsig` × σ12, with β from
     the map momentum and Δs the distances along the arm. Each reading's σ is `sigma_t_ns_by_gang` for its gang
     width, divided by √channels for a pair reading.
   * **Light:** a split reading is allowed only under the 2-track rule above.
4. **Decide**: find the largest sets of candidates that can all be real at once. Two candidates can both be real
   if they share no hit. They may share a hit only if its light says two tracks, and then no more tracks may
   share it than its light holds.

   | outcome | status | accepted |
   |---|---|---|
   | exactly one such set | `unique` (1 track) or `tracks` (2+) | every candidate in the set |
   | several sets | `ambiguous` | nothing (all candidates returned for the record) |
   | one set, but a hit brighter than `two_track_k` × one track that no other track in the set explains | `multitrack` | nothing |
   | more than `max_combos` combinations or `max_cands` candidates | `dropped` | nothing |
   | no candidate | `none` (or `incomplete` if a plane has no hit) | nothing |

5. **Counters**, applied to a bunch that would be accepted:
   * the entrance veto fired → `veto`;
   * otherwise the gas Cherenkov fired → `electron`.

   The lucite bit is a **tag only** (`LUCITE_YES` / `LUCITE_NO`) and never rejects.

## Times: T2 may read earlier than T1

Yes, a real pion can have measured t2 < t1. The time test only asks that the measured difference agree with the
expected flight time within `dt_nsig` σ, and it allows negative differences.
* The flight time from T1 to T2 is short: about 0.9 ns at 400 MeV/c over Δs12 = 255 mm.
* Each channel's resolution is 1.25–5 ns, larger than that flight time.
* So t2 − t1 for a real pion is spread over about ±5 ns around +0.9 ns.

In simulation, about 30 % of accepted real pions read t2 < t1, and about 5–7 % read t1 < t0 (T0 to T1 is about
2.5 ns). **Do not add an ordering requirement**: it would throw away about a third of the pions and remove
almost none of the background.

## Output

`dict(status, tracks, candidates, tags)`. Each track or candidate carries:
* `p` and `sigma` (MeV/c);
* `n_cal` (None if the map has none);
* `code` (the channel triplet);
* `readings` (which channels, their light and time);
* `R`, each reading's light over one track's.

**Momentum comes from the triplet only.** The map is particle-agnostic: an electron and a pion of the same
momentum bend identically.

## Placeholders

These are not measured yet; replace them when they are:
* `sigma_t_ns_by_gang`: rise time / 4;
* the threshold;
* `gate_ns`;
* `two_track_k`;
* the map itself, which is simulation output until a beam calibration replaces it;
* `pion_light.csv`, which comes from an energy-loss model until the bars are calibrated in MeVee.

## Changes from the 14 interface (`online_classify.py`)

| | 14 | now |
|---|---|---|
| unit of decision | one event = one channel per plane (the plane's largest amplifier) | one bunch = every fired channel on every plane, read together |
| input per channel | channel number | channel number, light and time |
| decode | WTA over the whole plane | clusters; WTA inside each reading; a 2-channel cluster may be two hits by its light |
| valid triplet | in the map | in the map, consistent in light, and consistent in T1−T0 and T2−T1 |
| several valid triplets | not possible | one largest compatible set → all accepted; several → `ambiguous` |
| two tracks in a bunch | MULTITRACK from a T0 amplifier count | competition between candidates; two tracks that share no hit are both accepted |
| TOF / HEAVY | one-sided T0→T2 TOF test | removed |
| entrance veto, Cherenkov | classes, momentum still reported | reject the bunch (`veto`, `electron`) |
| edge paddles | tags | not in the online path |
| output | one class + p per event | a status per bunch + a list of accepted tracks |

Unchanged: momentum from one map lookup of the channel triplet; None means no information.

## Porting notes

* Load the map once per run, or once per setting. Per candidate it is one hash lookup. Pack the key as
  `ch0<<16 | ch1<<8 | ch2`; every channel number is < 64.
* Most bunches have one cluster per plane, which means one combination and no set search.
* The set search (`best_sets`) only runs with 2+ candidates, which happens in about 1 in 1,000 bunches with hits.
  It never sees more than `max_cands`.
* `round()` in the hit-sharing rule is Python's: round half to even. Match it, or note the difference.
