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

### 3 Nov: a protocol lists its boards
`run_protocol.json` gains `"boards"` (serials, rack order) and `"disabledBoards"`. Opened w/ a protocol (or
from a run, with the run's list): a listed board not found is logged and shown once in a non-modal notice
("Boards not found"; runs go on, the protocol keeps it listed); an open board not listed is logged and an
unsaved change ("boards: +Digi-X (open, not listed)") until Save to protocol adds it; `disabledBoards` are
disabled after programming, and toggling one in Digitizer Settings (now showing that state) is an unsaved
change. A protocol without `boards` gets the boards opened, once, logged. A run's `_protocol.json`
records `boards`, `disabledBoards` and `boardsMissing` as at its start. `knownDigitizers.txt` (which ports
are opened) is unchanged. W/o settings nothing is compared. Test: ProtocolBoards.

### 3 Nov: protocols (the boards and every acquisition control), asked about at close
A settings set is now a **protocol**: its `run_protocol.json` also holds Save Data, the data path and
Start dashboard with run (`"format": "FSUDAQ run protocol 2"`), and `boardFiles` for a board whose file it
keeps outside its folder. Choosing a protocol (or starting FSUDAQ) takes its controls at once; opening
"w/ protocol" programs the boards and leaves the controls. programSettings.txt keeps only this computer's
settings and `settingsPath` (line 1 stays the data path); an older one's controls are still read and
added once to a protocol that lacks them. Start compares nothing and asks nothing: a saved run gets the
boards' snapshots and one `<prefix>_<run>_protocol.json`, the protocol as in the window (replacing
`_run_protocol.json` and `_programSettings.txt`; the run file's `run_control.protocol_file` points to it).
Unsaved changes (registers against the protocol's files, controls against run_protocol.json) mark
**Save to protocol \*** live and are asked about when the digitizers or FSUDAQ close or another protocol is
chosen: Save to <protocol> / Save as new protocol... / Discard / Cancel. Digitizer Settings' **Save copy
as...** asks whether the protocol is to use that file for the board. UI: "Settings set" -> "Protocol",
"w/ protocol", "Save to protocol", "Save as new protocol...". "w/ protocol from a run" reads older runs'
`_run_protocol.json` too; the dashboard's summary shows either.

### 3 Nov: saving without questions
Users keep their settings with the data and do not want to be asked at every Start. Start never asks about
settings not in the set: it logs them and marks the main window's **Save to set \*** (orange, tooltip lists
them). "Save protocol to set" and the Start question's "Save to set and start" / "Start anyway (this run only)"
become one **Save to set** button: the open boards (read back) and the run protocol into the active set, in
place, unasked. Digitizer Settings: **Save to set** writes the board's file in the set with no file dialog;
**Save copy as...** is the old dialog. With auto-increment, a run number already on disk is skipped (logged)
instead of asked; a number typed by hand still asks. A run's snapshots and copies (.bin, run_protocol.json,
programSettings.txt) are made read-only once the run is on.

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
