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

What the finder needs that is not in the data yet (the dashboard says which are placeholders):
`--cabling` (serial, channel -> plane and amplifier, or veto / cherenkov / lucite; default a
sequential placeholder), `--gains` (MeVee per count; without it the light tests are off:
pair rule "never", K = inf), `--time-offsets` (ns per channel) and `--setting high|low`.

    ./setup-venv.sh                      # once: numpy, numba, inotify into online/venv
    python online_dashboard.py --data-path /path/to/data          # what the button runs
    python online_dashboard.py --run RUNDIR --replay               # play a finished run at real-time pace
    python pipeline_fsu.py RUNDIR --workers 16 --slice-s 3          # offline: whole run, parallel
    python fsu_fast.py --selftest RUNDIR/*_000.fsu                  # check the numba kernels

Files: `fsu.py` (format decoder), `fsu_fast.py` (numba kernels), `finder/` and
`finder_fast.py` (the track finder, online), `pipeline.py` / `detector.py` /
`online_classify.py` / `momentum_map.*` (the earlier event building and classifier, still
used by the offline driver `pipeline_fsu.py`), `dashboard.html` (the page; no external
resources).

The file following borrows from A. Sampat's `compass-pseudo-online.py`: inotify
CREATE/CLOSE_WRITE announce new and finished files, the growing file is polled for
data, and a closed file that has been fully decoded is never touched again.
