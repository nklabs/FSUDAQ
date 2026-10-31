# FSUDAQ online analysis

`online_dashboard.py` follows the run FSUDAQ is writing, merges the boards' `.fsu`
streams in time, builds bunches, runs the pion-spectrometer track finder on every bunch
and serves a dashboard at http://localhost:8050/. The **Online Dashboard** button in
FSUDAQ starts it on the current data path and opens the browser.

The track finder is the handoff in `finder/` (`online_finder.py`, `INTERFACE.md`, the
momentum map, channel table, one-track light and constants), kept unchanged as the
reference. `finder_fast.py` builds the bunches in numba and decides every bunch with no
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
over and reported, so are aggregates missing from a file (gaps in the aggregate counter). The page
greys out with NO CONNECTION when the analysis stops answering.

What the status line means:

| status | |
|---|---|
| running | data are being written and the analysis keeps up |
| falling behind: reading the files / the DAQ part / every worker is busy | data come in faster than they are analysed; the page lags, nothing is lost or counted late for it |
| no new data for N s (a pause?) | nothing written for `--stall-s` (5 s): the boards may still hold partly filled buffers, so the finder waits up to `--hold-s` (30 s) |
| no new data for N s: ... | waited `--hold-s`: the finder went on; hits still in the buffers come late when the beam is back |
| run stopped: ... | FSUDAQ said the run stopped (its files are closed): everything is processed at once |
| last run (finished before the dashboard started) | started after the run ended: the run is read whole |

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
mid-run or after the run, the analysis falling behind, a killed worker, FSUDAQ's run file) and checks every number,
and every rate shown, against the hits written.
