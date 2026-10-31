# Experiment status

What is going on under the hood, on one page: a dot per item, one line of text, and the details
behind it on a click. Read-only: it never talks to the digitizers, and changes nothing in FSUDAQ,
the online dashboard or pion-ros.

| colour | meaning |
|---|---|
| green | as it should be |
| grey | a plain fact, not running, or no data |
| blue | information to read: sensors not connected, placeholders in experiment.json, a non-safety input out of range |
| pink | a data problem that is not a safety matter: run problems, board clocks, a board not writing, the analysis, disk, the LED, ROS |
| yellow, red (flashing; a ring = act now) | **safety only**: the gas sensors, the Cherenkov pressure and the temperatures by their rules; a safety sensor reading outside its range (yellow); the PLC status, which carries those readings, missing or stale (red) |

A big lamp per card on top (fixed order: Cherenkov, DAQ, Online analysis, Trackers, PLC, ROS), the
cards below in the same order. Skins: Panel, Retro, Minimal.

    python3 status/status_server.py          # on odin; standard library only

It serves on odin's localhost only. From a PC, through its own ssh tunnel (a quiet window, as for the
online dashboard):

    ssh -N -o LogLevel=QUIET -L 8060:localhost:8060 beamtime@129.129.141.162

then http://localhost:8060/ (status) and http://localhost:8060/limits (limits and names). Lines move
within their card by dragging; the order is kept in the browser ("reset lines" restores it).

| card | items | from |
|---|---|---|
| Cherenkov | pressure (ai0), stack and floor gas sensors (ai1, ai2), Cherenkov temperatures (tc6, tc7), each with its change over 10 s / 1 min / 10 min | PLC inputs, `limits.json` |
| DAQ | FSUDAQ running, the last run (running, stopped, problems, no stop), board clocks, the boards (one line; a line per board not writing), a failed LED command, disk (with a bar), data rate | `/proc`, the newest run folder's `<run>_run.json`, the online dashboard, the data path |
| Online analysis | the dashboard's status, health, lag; placeholders in `online/experiment.json` | `GET localhost:8050/state` |
| Trackers | the six tracker thermocouples as one line (tc0/1 tracker 0, tc2/3 tracker 1, tc4/5 tracker 2; each in the details), the T0, T1 + T2 and ESPEON currents (Trumeters, ai5-ai7), LED relay live | PLC inputs, pion-ros |
| PLC | heartbeat counting, status age, outputs | pion-ros `GET :8770/status` |
| PLC inputs (not named) | the raw value of every input not named yet | PLC |
| ROS | pion-ros answering, plc_status, rosbag2_recorder | pion-ros |

## Limits and names (`/limits`)

Each PLC input (`ai0`-`ai7`, `tc0`-`tc7`) can be named, put on a card, scaled (value = raw x scale +
offset, with a unit) and given rules. A rule raises the input's level to notice (to read, not a
safety matter: blue) or, for safety, warn (little warning), bad (big warning) or critical (massive
warning) when the value is at or above, above or below a
number, or changes by at least an amount within a time; a rule can apply in one mode only. The
modes (Cherenkov: normal / full of isobutane) are set there too.

The defaults (the PLC as labelled on 31 Oct 2026):

- Gas sensors (RKI M2A, ai1 and ai2; 100 %LEL = 1.6 % isobutane in air): at or above 5 %LEL warn, 10 bad,
  100 critical; expected range 0-100.
- Cherenkov pressure (WIKA WUC-10, ai0, psig): above 2 warn, always; in "full of isobutane", also below 0.3,
  or a change of 0.05 or more within an hour; expected range -14.7 to 100.
- Thermocouples (°C): expected range -40 to 80 (an open input reads a noisy staircase of 120-520, or 3276.7);
  no rules yet. Trumeters (ai5-ai7): expected range 0-5. ai3 and ai4 unused.

Saving writes `status/limits.json` and appends the whole set with the time to
`status/limits_log.jsonl`, so what was in force when stays on record. The monitor keeps each input's
values for an hour (more if a rule looks further back) in memory: after a restart, a change rule
sees only what came since. A change of an input's source or scaling starts its history again.

Each named input also shows its change over 10 s, 1 min and 10 min (Δ10s, Δ1m, Δ10m), each the mean of the
latest 4 / 12 / 60 s against the mean of the same span one interval earlier, so a noisy reading does not
swing it; "–" until the history reaches back far enough (after a restart: 14 s, 72 s, 11 min).

Tests: `python3 tests/test_status.py`.
