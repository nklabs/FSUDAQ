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
over and reported. The page greys out with NO CONNECTION when the analysis stops answering.

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
(late channels, pauses, roll-over, damage, a run recorded again, a killed worker) and checks
every number against the hits written.
