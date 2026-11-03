# FSUDAQ online analysis

`online_dashboard.py` follows the run FSUDAQ is writing, merges the boards' `.fsu`
streams in time, builds bunches, runs the pion-spectrometer track finder on every bunch
and serves a dashboard at http://localhost:8050/ (http://odin.psi.ch:8050/ for anyone
on the network; the controls answer odin and ssh tunnels only). The **Online Dashboard**
button in FSUDAQ shows it, and starts it on the current data path first if nothing answers
on port 8050. It runs in its own session, so it keeps running when FSUDAQ closes; the next
FSUDAQ finds it on its port. Its output goes to `online/dashboard.log`; to stop it,
`pkill -f online_dashboard.py`.

**runs ▾** on the page lists the recorded runs in the data path (newest first, with their
start, comment, size and summary). **summary** shows that run's summary in this browser only,
under a banner "Recorded run … — summary, not live" with **Back to live**; everyone else keeps
the live view, and none of the live checks is applied to it (`#summary=<run folder>` in the link
opens it). **re-read (odin)** (odin and ssh tunnels only) reads a run folder whole again, also
one given by its path, and shows it to every viewer; **back to live** returns to the run being
taken, and a run FSUDAQ starts takes the dashboard back to live by itself. The **size** slider
scales text and plots (kept in the browser; `?size=1.5` in the link sets it).

**Clear accumulated** (odin and ssh tunnels only) starts everything the live page counts again: the
per-channel counts and histograms, the finder's results, the RF timing, and the rate charts (total,
per channel, bunches per second), which go on from the next whole second of data. The run summary
is not cleared: it counts the whole run and records the Clear. Opened from another machine the page
is view only and says so next to the button.

**Axes** of every plot but the pulse height on every channel and the side-by-side panels: click a plot, then
the wheel zooms x and shift+wheel y; drag across it to zoom into that span; over an axis the wheel zooms that
axis and dragging moves it; double-click: automatic again (as on the PION topic plots; until a plot is clicked
the wheel scrolls the page). **⚙** in a card's bar gives the ranges as numbers (empty: automatic). A live time
chart (total rate, a channel's rate, bunches per second) zoomed with the wheel keeps following the newest data.
The pulse height's x is its Qlong range (its own fields and drag), only its y here. Kept in this browser, per plot.

#### Beam vs map

The floor tool (pion-spectrometer-simulations `tool/`, **Publish to online**) writes what the live data
should look like for the current settings: `~/pion-spectrometer-simulations/tool/published/expected.json`
(format `floor-tool-expected/1`, its README "Publish to online"; `--beam-expected` gives another path). The
dashboard reads it again whenever it changes; without one the tab says "nothing published yet", and a
broken or foreign file is named with the reason. `GET /expected` (open to all, read-only) returns the
publish's head and the comparison; the **Beam vs map** tab shows it (live view only).

- **Comparable at all?** First, in red: the map the live finder loaded is hashed (sha256 after CRLF → LF,
  as the tool's `file_identity`) and compared with the publish's `map.finder_map.sha256_lf`; the
  published `setting` with the finder's; the published field with the field in the live map's
  `handoff_meta.json`. Any difference: the momenta are not comparable, and it says so. A publish that did
  not name the finder's map is not comparable either (its momentum was read with the tool's own map).
- **Hits per channel**, per plane: the live occupancy (the finder's hits per amplifier channel, 1-based)
  as a share of the plane, against the published `share`. Shape only: with the sequential placeholder
  cabling the channel numbers are not known to be the detector's, and it says so. Numbers: centroid and
  width (sd) in channels and in mm (centres of the published `x_lo_mm`/`x_hi_mm`), and the overlap of the
  two shapes (Σ min of the shares; 1 = the same).
- **Momentum**: live `hist_p` against the published `read_with_finder_map` histogram (same bins), each
  scaled to unit area, with mean and sd from the bin centres (both alike) and their difference. The live
  `hist_p` leaves out bunches the gas Cherenkov marked (status `electron`): when the Cherenkov is cabled in
  the live finder, the expectation is corrected to match, `per_beam_particle − by_kind["e-"]` (assumed: it
  marks every electron, `CHERENKOV_E_EFF` = 1, and no muon or pion); without it, no correction. The tab
  says which.
- The live numbers count since the reading began (or the last Clear). The tool's warnings and the
  publish's `meaning` texts are listed; the tool models no noise, pile-up, veto, time or light tests.

A recorded run's summary is not compared with the publish: today's publish need not be the settings of
that run, so the tab is hidden there.

#### RF timing

Each tracker hit's time after the accelerator RF, modulo the RF period (19.75 ns at PSI), against its Qlong,
per plane (T0 by default; **Plane** picks another): the time of flight modulo the accelerator cycle. The RF
reference is the digitizer channel the cabling gives kind `rf` (one channel; `62839,15,rf,,` now). It is no
particle, so its hits never reach the finder (no bunches, no "uncabled" count) and are left out of the total
and per-board rates (the DAQ tab draws the RF as a grey line and bar of its own, labelled `RF`); its own
channel histograms stay in the DAQ tab. In every slice, a hit's phase is taken between the RF triggers on either side of it,
with the local period from their spacing (the cycle count between them uses `RFTOF_HZ`, the RF on the
digitizers' clock); a hit before the first or after the last RF trigger of its slice uses the nearest pair,
and with fewer than two RF triggers in a slice its hits are counted as "without an RF phase". Times have the
cable offsets removed, as in the finder, so zero is the RF reference's own phase, not the production time;
**centre on the peak** turns the period so the fullest bin is in the middle. Qlong 0-8192 in 64-ADC bins
(higher: the top bin), 100 time bins (198 ps). Counts since the reading began or the last Clear.
`GET /rftof` (read-only, for anyone) returns them; the tab asks for it every 3 s while it is open.

### Run summary

**Only the dashboard on port 8050 (FSUDAQ's) writes summaries and backfills** by default: an
instance on another port writes nothing into the run folders unless started with `--summaries`.
Test instances: use a copy of the data, or `--no-summaries`.

When the dashboard has processed a run to its end (FSUDAQ said it stopped, its run file has the
stop, or no file was written for 5 min and FSUDAQ did not say the run goes on), it writes
`<run folder>/<run folder name>_summary.json`, whole (a hidden temporary file, then a rename; the
folder's modification time is kept, since the newest run folder is found by it). A run that
turns out to go on is written again at its end, and so is one whose run file changes after (FSUDAQ
records the stop and its comment after telling the dashboard). A failed write is shown at the top
of the page and tried again a minute later. `GET /summary?run=<run folder name>` serves it to
anyone (only a bare folder name directly in the data path is taken), with the run file as it is
now, and `stale` if the data files changed since; `summary: null` if there is none.

| key | what |
|---|---|
| `format`, `version`, `written_at`, `source` | `live` (a reading that began while the run was being taken: a mid-run start, and a re-read of the run being taken, too), `read_after_end` (began on a run already over: its stop recorded, or 5 min quiet; e.g. after a restart), `load` (re-read on odin of a run already over), `replay`, `backfill` (made afterwards) |
| `dashboard`, `reading` | the commit and host; when the reading began and whether from the run's start |
| `run` | name, number, prefix, start, stop, comments, `duration_s` (run file), LED, clock, `end`: `stopped` / `guess` (no stop recorded) / `replay` / `interrupted` (the next run started before the end was processed: what was processed is written, with `unread_bytes` of the data files' `bytes` not yet scanned and `finder_lag_s`; never for a look at another run, nor while the run is open) |
| `data` | files, bytes, `t0_s` (board-clock second of the first hit read: 0 of every time below), `first_s`, `last_s`, `span_s`, `rate_span_s` (the mean rates' span, without far-off times), hits counted, hits in the finder |
| `complete`, `incomplete`, `caveats` | whether this reading counted every hit of the run's files; why not (`started_mid_run`, `next_run_started`, `cleared`, `window_changed`, `tasks_lost`, `boards_not_analysed`, `analysis_errors`, `unreadable_files`); notes that do not make it incomplete (late hits, data problems in the files, no stop recorded, a reference change, `setup_unknown`, `problems_unwatched` (the page showed another run for a while), `config_not_json` (NaN/Infinity kept as text), `history_partial`: hits outside the history, from before a mid-run start or with far-off times) |
| `setup`, `setup_known`, `setup_at_run_time` | the analysis setup the summary was computed with: the experiment config file and its content, path and sha256 of cabling, gains, offsets, map and the finder's own files (`handoff_meta.json`: threshold, K, sigmas, plane positions; `channels.csv`; `pion_light.csv`), the analysis code's sha256 (`code`), beam setting, bunch window, reference slot, boards, waits, and `identity` (a hash of what changes the results). A live summary has the setup in force while the run was taken (`setup_known: true`); any other reading uses the setup of its own time: `setup_known: false`, `setup_at_run_time: "unknown"` and a caveat, unless it equals the live reading's (`previous_live.setup`) |
| `scope` | over what each part counts: per-channel hits always the whole reading; the finder since the last Clear or window change |
| `boards`, `channels` | hits, `mean_rate_hz` (hits / `rate_span_s`), late and early hits; per board also files, bytes, counter gaps, damaged bytes |
| `finder`, `histograms`, `occupancy` | the finder's counts by result, accepted tracks, setup and window; every histogram of the Tracker tab with its edges and unit |
| `history` | hits per board and the finder's bunches by result over the whole run, in 10 s steps (merged to at most 720 points); `covered_s` is the data time of each point, so count / `covered_s` is its rate |
| `problems_at_end`, `problems_during_reading` | what the page warned of at the end, and during the reading (first and last time, steps, episodes; at most 50 kinds) |
| `settings` | file names of the run file, FSUDAQ's per-run copies of the program settings and run protocol (when there), the boards' `.bin`; the setup files, window and waits used. No registers are copied |
| `previous_live` | when a later reading of the run (also of more files than it had: then `files_changed: true`) (backfill, re-read, replay, a restarted dashboard) replaced a summary: the most recent live summary's own knowledge, carried on: `source`, `written_at`, `reading`, `complete`, `incomplete`, `caveats`, `end`, `setup`, `problems_during_reading`, `problems_note`, `aggregate_events` |
| `backfill_tried` | a backfill re-read these files and was kept out (it covered no more): `fingerprint`, `at`, its `incomplete` reasons; not tried again until the files change |
| `fingerprint`, `units` | the data files' number and size it describes; units (times in s of data time, rates in Hz) |

A reading that is not live never replaces a live summary made with another setup. Older runs
without a summary get one between runs: only when FSUDAQ is not taking a run (no `/run` without
`/stopped` while FSUDAQ runs; the current and newest folders stopped or quiet for 5 min; and, from
the disk, neither the newest run folder nor the one with the highest number has a run file without
a stop that grew in the last 12 h while FSUDAQ runs: a dashboard restarted during a long beam pause
still holds off) and the
dashboard has nothing left to process, and only after 2 min of that. One run at a time, newest
first, a child process (`--summarise`, the same reading code) runs at nice 19 with idle I/O
priority and is killed the moment a run starts (no file is left half written; the run is tried
again later). Once every run has a summary, runs whose summary is incomplete (and not made by a
backfill) are read again whole, once per set of files, and only with the same setup as the live
reading's (else the run list says why): the new summary replaces the old one unless
it covers less (more reasons), and keeps what the live reading saw in `previous_live`. A summary
is never replaced by an incomplete one when it was complete. A run that cannot be read is skipped
with the reason, shown in the run list.
`--no-backfill` turns this off, `--no-summaries` all summary writing.

The track finder is the handoff in `finder/` (`online_finder.py`, `INTERFACE.md`, the
momentum map, channel table, one-track light and constants), kept unchanged as the
reference. The map installed is PSI's official one for 0.20 T, frozen default layout
(pion-spectrometer-simulations `handoff/B0.200`, built 30 Oct 2026): T0/T1/T2 = 8/56/46
channels. Another field: copy that handoff's `B<field>/` files over the ones in `finder/`.
`cabling.csv` puts those 110 channels in sequence over the boards in rack order (T0 on
15880 ch0-7, T1 from 15880 ch8 to the end of 62839, T2 from 65316 on), the Cherenkov on
Digi-1443 ch6-7. `finder_fast.py` builds the bunches in numba and decides every bunch with no
or one candidate itself, with the reference's arithmetic in the reference's order; a
bunch with two or more candidates goes to `online_finder.find()`. Both self tests compare
the two bunch by bunch and must report no difference:

    python finder_fast.py --selftest 50000      # per bunch: status, candidates, p, R, pulls
    python finder_fast.py --selftest-stream     # whole kernel: bunching, cabling, histograms

The **DAQ channels** tab needs no cabling: hits per channel, rate over time, and for the
picked channel the pulse height (full 16-bit Qlong kept per count; linear or log axis, drag to
zoom), a PSD map ((Qlong − Qshort)/Qlong against Qlong), time since the previous hit, time
to a reference channel and a rate spectrum; plus every channel's pulse height side by side.

The **Run and LED trigger** card shows FSUDAQ's run file (`<run folder>_run.json`, README
"Data folder"), read-only: start, stop, the boards' clock (PLL) lock at both (a loss shown at
the top too), the LED trigger's delay and hold, each LED command
with the PLC's answer, and the FSUDAQ commit. The LED is set in FSUDAQ, not here. The rate
plots mark when FSUDAQ sent the LED commands (dashed lines; placed assuming the boards'
clocks start at 0 at Start, and good to tens of ms: the LED's own hits give the exact
time). A failed LED command is shown at the top, an "off" with the warning that the LED may
still be on.

What the finder needs that is not in the data comes from `experiment.json` (copy
`experiment.example.json`), read again for every run: `setting` (high|low, the T1/T2 position),
`cabling` (CSV serial,channel,kind,plane,ch; kind trk/veto/cherenkov/lucite), `gains` (CSV
serial,channel,mevee_per_count), `time_offsets` (CSV serial,channel,offset_ns) and optionally
`map`. The command-line options of the same names override it. Anything missing is a
placeholder, and the page says so at the top; cabling rows that cannot be right (plane, channel
range, duplicates, boards not open) are listed and left out. Time offsets are removed before the
hits are bunched.

How the files are read (see the docstring of online_dashboard.py): a board writes a channel
pair's events when that pair's buffer is full, so a quiet channel reaches the disk late. Every
aggregate is counted on the DAQ tab exactly once whenever it arrives; the track finder takes time
slices only up to where every pair still writing is on disk, and a hit that arrives after its
time was processed is counted as late and shown, never dropped silently. Damaged data is stepped
over and reported, so are aggregates missing from a file (gaps in the aggregate counter), in one
line per board with its size (share of the board's data, and about how long). A small, isolated loss
(at most 3 aggregates and under 0.1 % of the board's data) is a grey note under the warnings, not a
red one: e.g. one aggregate the readout handed over as zeros (510 words of Digi-62839, 3 Nov 2026,
0.001 % of its data). The aggregates on either side of zero fill are read (8 zero words after an
aggregate: zero fill, not the rest of a cut-off one). The page greys out with NO CONNECTION when the
analysis stops answering.

What the status line means:

| status | |
|---|---|
| running | data are being written and the analysis keeps up |
| falling behind: reading the files / the DAQ part / every worker is busy | data come in faster than they are analysed; the page lags, nothing is lost or counted late for it |
| no new data for N s (a pause?) | nothing written for `--stall-s` (5 s): the boards may still hold partly filled buffers, so the finder waits up to `--hold-s` (30 s) |
| no new data for N s: ... | waited `--hold-s`: the finder went on; hits still in the buffers come late when the beam is back |
| run stopped: ... | FSUDAQ said the run stopped (its files are closed): everything is processed at once |
| last run (already finished when it was read) | its run file has the stop, or no file was written for 5 min: the run is read whole (a run that is written again goes back to running) |

Started during a run, the dashboard reads from then on (hits from before then that a slow pair
writes later are counted apart, not as late). The hit rate is the newest second whose hits are all
counted; a pair that has not written for `--hold-s` (or not yet) has no rate shown until it does.

    ./setup-venv.sh                      # once: numpy, numba into online/venv
    python online_dashboard.py --data-path /path/to/data          # what the button runs
    python online_dashboard.py --run RUNDIR --replay               # play a finished run at real-time pace
    python fsu_fast.py RUNDIR/*_000.fsu                             # check the numba decoder against fsu.py

Files: `fsu.py` (format decoder), `fsu_fast.py` (numba kernels), `finder/` and
`finder_fast.py` (the track finder, online), `daq_hist.py` (per-channel histograms),
`dashboard.html` (the page; no external resources). The earlier event building, classifier
and offline driver (pipeline_fsu.py, pipeline.py, detector.py, online_classify.py, merge.py
and their momentum_map.csv) were superseded by the finder in finder/ and removed; they are in
the history before Oct 2026.

Tests: `python3 ../tests/test_online.py` runs the file following against simulated boards
(late channels, pauses, roll-over, damage, counter gaps and wrap, a run recorded again, a start
mid-run or after the run, the analysis falling behind, a killed worker, FSUDAQ's run file, the run
summary and its backfill, GET /summary) and checks every number, and every rate shown, against the hits written.
