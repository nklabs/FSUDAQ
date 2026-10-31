# Experiment status

What is going on under the hood, on one page: a dot per item (green as it should be, grey a plain
fact, hollow no data, yellow look at it, red wrong, flashing act now), one line of text, and the
details behind it on a click. Read-only: it never talks to the digitizers, and changes nothing in
FSUDAQ, the online dashboard or pion-ros.

    python3 status/status_server.py          # on odin; standard library only

It serves on odin's localhost only. From a PC, through its own ssh tunnel (a quiet window, as for the
online dashboard):

    ssh -N -o LogLevel=QUIET -L 8060:localhost:8060 beamtime@129.129.141.162

then http://localhost:8060/ (status) and http://localhost:8060/limits (limits and names).

| card | items | from |
|---|---|---|
| DAQ | FSUDAQ running, the last run (running, stopped, stopped with problems, no stop), board clocks, boards writing, disk, data rate | `/proc`, the newest run folder's `<run>_run.json`, the data path |
| Online analysis | the dashboard's status, health, lag; placeholders in `online/experiment.json` | `GET localhost:8050/state` |
| LED trigger | enabled, the last command and the PLC's answer, the relay | run file, pion-ros |
| PLC | heartbeat counting, status age, outputs | pion-ros `GET :8770/status` |
| Cherenkov (and any group named on /limits) | each named input, judged by its rules | PLC inputs, `limits.json` |
| PLC inputs (not named) | the raw value of every input not named yet | PLC |
| ROS | pion-ros answering, plc_status, rosbag2_recorder | pion-ros |

## Limits and names (`/limits`)

Each PLC input (`ai0`-`ai7`, `tc0`-`tc7`) can be named, put on a card, scaled (value = raw x scale +
offset, with a unit) and given rules. A rule raises the input's level to warn (little warning),
bad (big warning) or critical (massive warning) when the value is at or above, above or below a
number, or changes by at least an amount within a time; a rule can apply in one mode only. The
modes (Cherenkov: normal / full of isobutane) are set there too.

The defaults (inputs not assigned yet):

- Isobutane monitors 1 and 2: at or above 5 %LEL warn, 10 bad, 100 critical.
- Cherenkov pressure: above 2 psi warn, always; in "full of isobutane", also below 0.3 psi, or a
  change of 0.05 psi or more within an hour.

Saving writes `status/limits.json` and appends the whole set with the time to
`status/limits_log.jsonl`, so what was in force when stays on record. The monitor keeps each input's
values for an hour (more if a rule looks further back) in memory: after a restart, a change rule
sees only what came since. A change of an input's source or scaling starts its history again.

Tests: `python3 tests/test_status.py`.
