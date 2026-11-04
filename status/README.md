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

A big lamp per card on top (fixed order: Cherenkov, DAQ, Trackers, Online analysis, PLC, ROS), the
cards below in three columns: Cherenkov | DAQ over Trackers | Online analysis over PLC over ROS (one
column on a narrow window). Skins: Panel, Console (graphite and amber), Minimal; `?skin=console` in
the link sets it.

    python3 status/status_server.py          # on odin; standard library only

The same page in a terminal, htop-style: `python3 status/tui.py` (`--url http://odin.psi.ch:8060` from
elsewhere). A lamp per card, the cards with a dot per item in the page's colours (greyed when the server
stops answering), Enter for an item's details, and the past hour of every PLC reading as a sparkline: from
pion-ros's topic plots on romulus (`--plots`), since the status server keeps only the latest value. Read-only.

Anyone on the beamline network can look: http://odin.psi.ch:8060/ (status) and
http://odin.psi.ch:8060/limits (limits and names, view only). Limits are saved only from odin
itself or through an ssh tunnel (a quiet window, as for the online dashboard):

    ssh -N -o LogLevel=QUIET -L 8060:localhost:8060 beamtime@odin.psi.ch

then http://localhost:8060/limits. `--bind 127.0.0.1` keeps the whole page to odin and tunnels. Lines move
within their card by dragging; the order is kept in the browser ("reset lines" restores it).

| card | items | from |
|---|---|---|
| Cherenkov | pressure (ai0), stack and floor gas sensors (ai1, ai2), Cherenkov temperatures (tc6, tc7), each with its change over 10 s / 1 min / 10 min | PLC inputs, `limits.json` |
| DAQ | FSUDAQ running, the last run (running, stopped, problems, no stop), board clocks, the boards (one line; a line per board not writing), a failed LED command, disk (with a bar), data rate | `/proc`, the newest run folder's `<run>_run.json`, the online dashboard, the data path |
| Online analysis | the dashboard's status, health, lag; placeholders in `online/experiment.json` | `GET localhost:8050/state` |
| Trackers | the six tracker thermocouples as one line (tc0/1 tracker 0, tc2/3 tracker 1, tc4/5 tracker 2; each in the details; those reading within 0-100 °C counted as connected), the T0, T1 + T2 and ESPEON currents (Trumeters, ai5-ai7), LED relay live | PLC inputs, pion-ros |
| PLC | heartbeat counting, status age, outputs | pion-ros `GET :8770/status` |
| PLC inputs (not named) | the raw value of every input not named yet | PLC |
| ROS | pion-ros answering, plc_status, rosbag2_recorder | pion-ros |

## Limits and names (`/limits`)

Each PLC input (`ai0`-`ai7`, `tc0`-`tc7`) can be named, put on a card, scaled (value = raw x scale +
offset, with a unit) and given rules. A rule raises the input's level to notice (to read, not a
safety matter: blue) or, for safety, warn (little warning), bad (big warning) or critical (massive
warning) when the value is at or above, above or below a
number, more than a number above the room temperature, or changes by at least an amount within a time; a rule
can apply in one mode only. The modes (Cherenkov: normal / full of isobutane) are set there too.

Room temperature is the mean of the inputs listed under `room` (ticked under "Room temperature" on the page;
by default tc6 and tc7, the Cherenkov thermocouples, which read the hall), counting only those that read: not
open, within their expected range, not marked not connected. With none reading, a "more than room +" rule says
it is not checked and raises nothing.

An input's `connected` is yes (judged by its rules), no (shown as not connected, blue, whatever it reads: an open
thermocouple reads believable but meaningless values) or auto: connected while it reads within its expected range,
not connected outside it.

The defaults (the PLC as labelled on 31 Oct 2026):

- Gas sensors (RKI M2A, ai1 and ai2; 100 %LEL = 1.6 % isobutane in air): at or above 5 %LEL warn, 10 bad,
  100 critical; expected range 0-100.
- Cherenkov pressure (WIKA WUC-10, ai0, psig): above 2 warn, always; in "full of isobutane", also below 0.3,
  or a change of 0.05 or more within an hour; expected range -14.7 to 100.
- Thermocouples (°C): above 80 bad; an open input reads 3276.7 or a noisy staircase of about 110-520. The
  Cherenkov's (tc6, tc7): connected, expected range -50 to 1300; they are the room temperature. The six on the
  trackers (tc0-tc5): auto, expected range 0-100 (an open input reads above it), more than 10 above the room warns
  (4 Nov 2026). A real tracker reading above 100 °C would show as not connected: it passes the 10-above-room
  warning and the 80 °C alarm on the way. Trumeters (ai5-ai7): expected range 0-5. ai3 and ai4 unused.

A safety input with no value (the PLC status missing or stale, the heartbeat stuck, NaN from the PLC, an
open thermocouple, or not assigned to an input) is a warning, not grey: its alarms cannot go off. The
limits page refuses what would silence an alarm or stop the gathering (scale 0, blank thresholds or
settings, decimals outside 0-6, a rule that is not a rule), and a save from a page loaded before someone
else's save (it would undo theirs, the mode included): reload and make the change again. A reading is
shown with as many decimals as it takes not to look like a threshold it has not reached (4.96, not 5.0).
If the gathering fails, or the page loses the server, every lamp and dot turns grey with the time of
the last good gathering.

Saving writes `status/limits.json` and appends the whole set with the time to
`status/limits_log.jsonl`, so what was in force when stays on record. The monitor keeps each input's
values for an hour (more if a rule looks further back) in memory. A change rule compares the mean of
the latest 60 s with the mean of 60 s one rule interval earlier (single readings are noisy: the pressure's
spread is about 0.1 psig at rest), and looks only at the time since its mode was set. After a restart,
a mode change, or a change of an input's source or scaling, it has less history than its interval and
says so on the line ("only N min of history so far").

Each named input also shows its change over 10 s, 1 min and 10 min (Δ10s, Δ1m, Δ10m), each the mean of the
latest 4 / 12 / 60 s against the mean of the same span one interval earlier, so a noisy reading does not
swing it; "–" until the history reaches back far enough (after a restart: 14 s, 72 s, 11 min).

Tests: `python3 tests/test_status.py`.
