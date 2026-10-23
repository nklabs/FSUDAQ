# Tests

Run before taking data, and after any change to the board, readout or online code.

    make -C tests                       # test_settings and test_daq (needs the CAEN headers and Qt 6)
    python3 tests/test_online.py        # the online dashboard's file following

No hardware is needed: `fake_caen.cpp` stands in for libCAENDigitizer. Its boards keep their
registers in memory, every register starting at a sentinel value, and they can generate DPP-PSD
data the way odin's boards write it.

| test | what it checks |
|---|---|
| `test_settings` | odin's six boards (identities from `data/odin-runB`) opened "w/ settings": every read/write register on the board equals the file, and FSUDAQ's memory equals the board. Wrong files (size, firmware, channel count) are refused without side effects. |
| `test_daq` | FSUDAQ itself, off-screen, against six fake boards: open w/ settings; a missing settings file or a register that does not read back blocks Start; a saved run is on disk byte for byte across roll-over, including what the boards still held at Stop (also when the flush releases it late); the master starts last and stops first; a board that cannot start starts none; a read failure, a read longer than the buffer, a file that cannot be opened or cannot grow stops the run with one alarm and a stop record that says why; a repeated series (controls locked in the pause, Stop in the pause, Stop in a run, a failure or a problem found at the stop ends it); a readout slow or stuck at Stop; "Overwrite" undone, "Go back" keeps the number; a board also on USB, or that does not reopen; SIGTERM or the window closed during a run; the scope leaves Save Data alone. |
| `test_online.py` | the dashboard's loop on a fake clock against simulated boards: DAQ counts exact; finder + late (+ before a mid-run start) = every hit; every rate shown is the true count of its second; pauses (held, and longer than `--hold-s`), roll-over, damaged data, counter gaps and wrap, an old aggregate written again, another data format, a run recorded again, a start mid-run or after the run, a board without data, the analysis falling behind, offsets, hits exactly at a slice boundary, a worker killed. |

## What only the hardware can show

The fake library models the registers and the data stream, not CAEN's library or firmware.
Check these on odin once, with the real boards, and note the result:

1. **Open "w/ settings"**: the log says "every register reads back as in the file" for all six
   boards. A register listed as differing is either a bit the board does not keep (tell us; it
   then stays a warning) or a real problem.
2. **Stop drains the boards**: at Stop each board is stopped, its aggregates still being filled are
   released with a Forced Data Flush (0x8040), and it is read until empty (three empty reads 50 ms
   apart). Check with a pulser of known rate on a quiet channel and a large number of events per
   aggregate: the events in the files must equal the pulses sent up to the stop.
3. **Synchronized boards** (Sync Boards, master = the first board): the stop of the master stops
   the others through the sync line; the drain then reads each board until it is empty.
4. **Readout buffer**: the console line "the library's readout buffer ... is smaller than one block
   transfer" means the library sized its buffer from settings it no longer chooses; FSUDAQ then
   uses the larger size. Note it if it appears.
5. **Ctrl-C in the terminal** during a test run: the run is stopped and recorded, FSUDAQ closes.
6. **Online dashboard**: start a run with the dashboard, check the banner (placeholders until
   `online/experiment.json` is filled), that the status reads "running" (not "falling behind")
   at beam rates, that Stop turns it to "run stopped: everything processed" within seconds, and
   that "late hits" stays small (a channel pair that fills its 1023-event buffer slowly writes
   late; fewer events per aggregate for quiet channels shortens it).
