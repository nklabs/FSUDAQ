# FSUDAQ online analysis

`online_dashboard.py` follows the run FSUDAQ is writing, merges the boards' `.fsu`
streams in time, builds events, classifies them with the pion-spectrometer momentum
map and serves a dashboard at http://localhost:8050/. The **Online Dashboard**
button in FSUDAQ starts it on the current data path and opens the browser.

    ./setup-venv.sh                      # once: numpy, numba, inotify into online/venv
    python online_dashboard.py --data-path /path/to/data          # what the button runs
    python online_dashboard.py --run RUNDIR --replay               # play a finished run at real-time pace
    python pipeline_fsu.py RUNDIR --workers 16 --slice-s 3          # offline: whole run, parallel
    python fsu_fast.py --selftest RUNDIR/*_000.fsu                  # check the numba kernels

Files: `fsu.py` (format decoder), `fsu_fast.py` (numba kernels), `pipeline.py` /
`detector.py` / `online_classify.py` / `momentum_map.*` (event building and
classification, shared with the offline pipeline), `pipeline_fsu.py` (offline driver),
`dashboard.html` (the page; no external resources).

The file following borrows from A. Sampat's `compass-pseudo-online.py`: inotify
CREATE/CLOSE_WRITE announce new and finished files, the growing file is polled for
data, and a closed file that has been fully decoded is never touched again.
