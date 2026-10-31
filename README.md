# Discord 

https://discord.gg/xVsRhNZF8G

# This fork

Prepared for data taking with six DPP-PSD boards on optical links (Oct 2026). Differences from
upstream that matter at the console:

- **Settings.** "w/ settings" (the default) loads `Digi-<serial>_<DPP>.bin` from the settings path,
  writes every register that shapes the acquisition and reads every one back. A board whose file is
  missing or refused, or a register that does not read back as the file has it, blocks Start (the
  dialog names it). Removed because they did not do what they said: "default Program", Program
  Default (board and channel), Autoset Reading Conf., Copy Board, SW decimation. The settings panel
  is locked during a run and during the pause of a repeated series.
- **Runs.** Each run has its own folder `<data path>/<prefix>_<run>/` (data files and the boards'
  settings as read back at the start). A run starts on every board or on none. At Stop every board
  is read out to the end before the files close. A board that stops answering or a file that cannot
  be written stops the run with one alarm, and the stop comment in RunTimeStamp says why. Timed and
  repeated runs: run length, repeat and pause on the main window. Ctrl-C, kill, a closed terminal or
  closing the window stop a run properly. **Board clocks:** each board's PLL lock (0x8104 bit 7) and a
  lock lost since the last check (0x8178 bit 4) are read after the open, at Start and at Stop (never
  during a run) and shown on the main window. A board not locked refuses Start; one that lost its lock
  and locked again starts with a warning (its 16 ns phase may have changed); a loss during a run is a
  stop problem. Both are in the run file (`clock`, `stop.clock`) and on the dashboard. Start the program with `./FSUDAQ` (log in log/) or
  `./FSUDAQ-gdb` (backtrace on a crash).
- **Online dashboard** (online/README.md) instead of the online histograms and analyzers.
- **Setup and tests:** `./install.sh --check` reports what is missing, `./install.sh` installs it;
  `make -C tests` and `python3 tests/test_online.py` run the tests (tests/README.md, which also lists
  what to check once on the hardware).

# Introduction

This is a DAQ for 1st gen CAEN digitizer for 

- x725, x725S, x730 with PHA and PSD firmware.
- V1740 with QDC firmware

It has scope (updated every half-sec), allow full control of the digitizer (except LVDS), and allow saving waveform.

It can be connected to InfluxDB v1.8+ and Elog.

(Upstream:) each channel has its own 1D histogram in the "Online Histograms" panel. In this fork that
panel and the online analyzer selection are removed (they filled plots from a worker thread while the
GUI drew them, which corrupted the heap mid-run); the online dashboard replaces them.

## Wiki
https://fsunuc.physics.fsu.edu/wiki/index.php/CAEN_digitizer

# Online analyzer (upstream; not reachable from the GUI in this fork)
A Multi-builder (event builder that can build event across multiple digitizer) is made. It has normal event building code and also a backward event building code that build events from the latest data up to certain amont of event.

A 1-D and 2-D histogram is avalible. In the 2-D histogram, graphical cuts can be created and rename.

An online analyzer class is created as a template for online analysis. An example is the SplitPoleAnalyzer.h. It demo a 2-D histogram and a 1-D histogram, and the way to output the rates of cuts to influxDB.

<span style="color:red;">Notice that, when the FSUDAQ is started, the online analyzer is not created, no event will be built. Once the online anlyzer is created and opened, event will be built, even the window is closed. </span>

## Create a custom online analyzer

Under the analyzer folder, there are few examples can be followed. Teh idea is create a derivative class based on the Analyzer.h. To implement the new online analyzer, user need to modify a few things:
- add the code file into FSUDAQ_At.pro
- add the header to the top of FSUDAQ.cpp
- edit the vector onlienAnalyzerList at th etop of FSUDAQ.cpp
- edit the FSUDAQ::OpenAnalyzer()

after that, we need to update the makefile by

```sh
>qmake6 FSUDAQ_Qt6.pro
```

and then recompile by
```sh
>make 
```

# Operation

When programSettings.txt is presented in the same folder as the FSUDAQ_Qt, the program will load it can config the following 

- (line 1) raw data path, where the data will be stored.
- (line 2) the influxDB v1.8+ IP
- (line 3) the database name
- (line 4) the influxDB token (for v2.0+)
- (line 5) the elog IP
- (line 6) the elog port
- (line 7) the elog logbook name
- (line 8) elog user name
- (line 9) elog user password
- then `key=value` lines, in any order: `settingsPath` (folder of the `Digi-*.bin` files),
  `autoIncrementRunID`, `maxFileSizeMB` (roll-over size, up to 4000), `repeatPauseSec`,
  `autoDashboard`, `boardOrder`, and the LED trigger's `ledTrigger`, `ledDelaySec`, `ledHoldSec`,
  `ledRelayURL`, `ledOnCommand`, `ledOffCommand` (see below). FSUDAQ writes the file back when
  these change.

If no programSettings.txt is found. The program can still search for all digitizers that connected using optical cable. 

Missing the raw data path will disable save data run, but still can start the ACQ. Missing InfluxDB (elog) variables will disable influxDB (elog). 

## Keyboard control

- F4 open any drop-down list
- tab for next element
- crtl+tab for previous element
- space for "press a button"
- alt + tab for switching different windows
- up/down arrow for increase/decrease number (<span style="color:blue">Blue Tex</span> = value not set, <span style="color:red">Red Tex</span> = value cannot set)

## Data folder

User must setup the data path for data take. Without the data path, user still can run the DAQ. Inside the data path, when a run is started there are

- RunTimestamp.dat  <- this store the timestamps and comments for all runs
- RunTimestamp.csv  <- csv format for the RunTimestamp.dat, easy for inport to excel
- lastRun.sh <- some data for bash script
- `<prefix>_<run>/` <- one folder per run, with
  - `<prefix>_<run>_<serial>_<DPP>_<tick>_<index>.fsu` <- data files, a new index at the roll-over size
  - `<prefix>_<run>_<serial>_<DPP>.bin` <- each board's settings, read back from the board at the start
  - `<prefix>_<run>_run.json` <- the run file: everything about the run that is not data (below)

The `.bin` snapshots are the boards' register memory as FSUDAQ loads it; the run file is the same
read-back in words, with the rest of the run's settings, so a run can be understood later without
FSUDAQ. It is written when the run starts, again at each LED answer and at the stop, always whole:

| key | what |
|---|---|
| `run`, `prefix`, `folder` | which run |
| `start`, `stop` | times (ISO 8601 with the UTC offset), comments, run time in ms, problems found at the stop, bytes saved per board; `stop` is `null` while the run is going (or if FSUDAQ died in it) |
| `led` | the LED trigger's settings, and each command sent: when (`sent`, and `run_ms` since the boards started), the PLC's `reply`, whether it was `ok`, and whether it could have `switched` the LED (`yes`; `no`: refused or never reached the PLC; `unknown`: e.g. no answer in time) |
| `run_control` | run length, repeat and pause, file size, settings path, board order, elog |
| `fsudaq` | the branch and commit of the source tree FSUDAQ ran from, and when its binary was built |
| `boards` | per board in rack order: serial, model, DPP, channels, its `.bin` snapshot, and every register as `"<address> [chNN] <name>": "0x<hex> <decimal>"` |

About 25 kB per 16-channel board. Read it with `less`, or e.g.
`python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["led"])' <file>`.

## LED trigger

A light on the detectors, powered through the Slowtronics PLC, gives every board the same hits at
the start of each run, so each board's start phase against its 16 ns clock can be measured. With
**LED trigger** ticked (it then reads "enabled"), in every run FSUDAQ switches it on *delay* seconds after the boards
started and off again *hold* seconds later, and off at the stop if the run ends first. It is never
left on by a run that ends, a Stop, a closed window or a signal.

The commands go by HTTP to the plc_command node of pion-ros on romulus, which holds the PLC's
command connection (`ledRelayURL`, default `http://129.129.141.236:8770/command`; the node only
takes commands from the hosts in its `http_allowed_hosts`). Nothing is spawned during a run. The
PLC's DQ1 is the LED: `ledOnCommand=slowtronics,dq1,on`, `ledOffCommand=slowtronics,dq1,off`.
A command that is refused or unanswered is logged in red and recorded in the run file; the run
goes on. The switching is good to a few tens of ms (FSUDAQ, ROS, the PLC's scan): the LED's own
hits give the ns timing, the run file's `run_ms` only where to look. The online dashboard shows the
run file (DAQ tab, "Run and LED trigger") and marks the LED commands on its rate plots.

# ToDo

- Gaussians fitting for 1D Histogra
- Save Histogram?

# Required / Development enviroment

Ubuntu 22.04

- CAENVMELib v3.3 +
- CAENCOmm v1.5.3 +
- CAENDigitizer v2.17.1 +
- CAEN A3818 Driver v1.6.8 +
- CAENUSBdrv v1.5.5 + (for V57XX digitizer with USB connection)

- qt6-base-dev
- libqt6charts6-dec
- libcurl4-openssl-dev
- elog

The CAEN Libraries need to download and install manually. The other libraries can be installed using the following command:

`sudo apt install qt6-base-dev libqt6charts6-dev libcurl4-openssl-dev elog`

The elog installed using apt is 3.1.3. If a higher version is needed. Please go to https://elog.psi.ch/elog/

The libcurl4 is need for pushing data to InfluxDB v1.8+

InfluxDB can be installed via `sudo apt install influxdb` for v1.8. the v2.0+ can be manually installed from the InfluxDB webpage https://docs.influxdata.com/influxdb/v2/.

The QCustomPlot (https://www.qcustomplot.com/index.php/introduction) source files are already included in the repository.

## For A4818 optical-USB 

need to install the A4818 driver.

Make sure connect the optical fiber before switch on the digitizer(s). If unplug the optical fiber and reconnect, need to restart the digitizer(s).

## For A5818 PCI gen 3

need to install CAENVMELib v4.0 +

## For Raspberry Pi installation

All required CAEN Libraries support ARM archetect, so installation of those would not be a problem.

THe libqt6charts6-dev should be replaced by qt6-chart-dev, and the elog need to be installed manually (or can be skipped)

`sudo apt install qt6-base-dev qt6-chart-dev libcurl4-openssl-dev`

I tested with a Raspberry Pi 5 with 8 GB + A4818 optical-USB adaptor. it works.

# Compile

## in case the *.pro not exist or modified
use `qmake6 -project ` to generate the *.pro

in the *.pro, add 

- ` QT += core widgets charts printsupport network`
- ` LIBS += -lCAENDigitizer -lcurl`

## if *.pro exist

then run ` qmake6 *.pro` it will generate Makefile

then  ` make`

if you want to use GDB debugger, in the *.pro file add

` QMAKE_CXXFLAGS += -g`


# Auxillary programs (e.g. Event Builder)

There is a folder Aux, this folder contains many auxillary programs, such as EventBuilder. User can `make` under the folder to compile the programs.

# Enable Core dump 

The program has abort handler to save core dump.

first, enable the gdb in compilation by edit the FSUDAQ_Qt6.pro by commen out the following lines:
```sh
QMAKE_CXXFLAGS += -g
QMAKE_CXXFLAGS_RELEASE = -O0
QMAKE_CFLAGS_RELEASE = -O0
```

second, ensure the core dump file has unlimited size and set the core dump file name
```sh
>ulimit -c unlimited
>echo "core.%e.%p" | sudo tee /proc/sys/kernel/core_pattern
```


# Known Issues

* If accessing the database takes too long, recommend to disable the database.
* Pile up rate is not accurate for very high input rate ( > 60 kHz ).
* When using the scope, the Agg/Read must be smaller than 512.
* Events/Agg, Agg Organization and Max Aggregates per transfer come from the settings file as they are (this fork does not let the CAEN library choose them). For PHA firmware, when the trigger rate changes, the Events per Agg may need to be changed.
* The Agg Organization, Event per Agg, Record Length are strongly correlated. Some settings are invalid and will cause the digitizer goes crazy.
* (Fixed in this fork: loading settings writes every register that shapes the acquisition and checks it reads back.)
* Sometimes, the buffer is not in time order, and make the trigger/Accept rate to be nagative. This is nothing to do with the program but the digitizer settings. Recommand reporgram the digitizer.
* For 1740 QDC, RecordLenght is board setting, but readout is indivuial group.
* For PHA, the trapezoid scaling and fine-gain register are calculated before ACQ start.
* For 1740D QDC, when 1st grouped channel is enabled, the 0th-channel must be enabled, otherwise, there is a ReadData error and the ACQ will stop. 

# Known Bugs

* EventBuilder will crash when trigger rate of the data is very high.