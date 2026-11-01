Patch 3 INSTALLED by MR 29 Sep 20:42 CEST (loaded srcversion 144B4E806B2D75FB1429922;
a3818.c.v1-patch2 and .orig-1.6.12 kept; README-ODIN-PATCH.txt updated; dkms installed).
FSUDAQ: mid-run dashboard start now asks (Yes/No, default No) instead of refusing, so the
trigger can be tested deliberately: look for "a3818: dispatch_pkt: link 4 does not exist"
in dmesg afterwards.

### 29 Sep 21:0x: "digitizers not loading" after patch 4 — NOT the patch: stale CAEN library locks
Probe (rebuilt /tmp/linkprobe/probe after the reboot wiped /tmp): ports 0, 4, 5 fine; opens on
links 1-3 blocked for ever in user space (futex_wait, 0 ioctls reached the driver, no RX
timeouts). Cause: CAENVMELib per-link mutexes in /dev/shm/CAENV_LCK_* left locked by the
FSUDAQ that aborted at 20:45 inside library calls on those links; they outlive the process,
hence "persisted across sessions". Fix: `rm /dev/shm/CAENV_LCK_*` with no CAEN program
running -> all six boards answer (no PLL loss, run 0). Both launchers (`FSUDAQ`, new
`FSUDAQ-gdb`) now clear them automatically; stdout line-buffered in main(). Patch 4 confirmed
inert so far (no "truncating" messages). Recovery recipe after ANY DAQ crash: close every CAEN
program, `rm /dev/shm/CAENV_LCK_*`, then reopen; power-cycle only if a board still does not
answer the probe.

### 1 Nov: settings sets, run protocol, Start check (branch feature/run-settings, not yet on odin)
Settings live in named sets: folders of `Digi-<serial>_<DPP>.bin` plus `run_protocol.json` beside the
active one (`settingsPath`, e.g. .../PSI_2026_DAQtest_settings/cosmics; programSettings.txt unchanged).
Open choices: "w/ settings set" (default), "w/ settings from a run..." (snapshots and protocol of a run;
a board without a snapshot from the set, warned), "w/o settings". Save, Live Setting Update and
"Save to set" always write the board's file in the active set (a run snapshot or another file loaded
is never written); new "Save as new set..." and "Save protocol to set". Run protocol per set; Repeat is
never restored. Start compares boards (and the set's protocol fields) with the set and asks once for
new differences: default "Start anyway (this run only)"; series runs log only. Every saved run folder
now also holds `<prefix>_<run>_run_protocol.json` and `<prefix>_<run>_programSettings.txt` (token and
password masked); snapshots are read back after calibration and derived registers (PHA fine gain,
QDC aggregation). Settings files are written atomically (through symlinks, mode kept). Live Setting
Update no longer writes read-only registers into the file. Check on the hardware: tests/README.md item 8.
