#ifndef FSUDAQ_H
#define FSUDAQ_H

#include <QMainWindow>
#include <QWidget>
#include <QPushButton>
#include <QPlainTextEdit>
#include <QLineEdit>
#include <QLabel>
#include <QComboBox>
#include <QCheckBox>
#include <QGridLayout>
#include <QElapsedTimer>
#include <QNetworkAccessManager>
#include <QMessageBox>
#include <QJsonObject>

#include "ClassDigitizer.h"
#include "CustomThreads.h"
#include "CustomWidgets.h"
#include "Scope.h"
#include "DigiSettingsPanel.h"
#include "SingleSpectra.h"
#include "ClassInfluxDB.h"
#include "analyzers/Analyser.h"

class ScalarWorker; //Forward declaration
class QScrollArea;

//^#===================================================== FSUDAQ
class FSUDAQ : public QMainWindow{
  Q_OBJECT
  friend class FSUDAQTest; // tests/test_daq.cpp drives the run control without a display
public:
  FSUDAQ(QWidget *parent = nullptr);
  ~FSUDAQ();

  void closeEvent(QCloseEvent * event){
    // a run (or a series) going: ask, then stop it properly first, files closed and recorded
    if( (inDataRun || series.active) && !closingBySignal ){
      if( QMessageBox::question(this, "Close FSUDAQ", "A run is going. Stop it and close FSUDAQ?",
                                QMessageBox::Yes | QMessageBox::No, QMessageBox::No) != QMessageBox::Yes ){
        event->ignore();
        return;
      }
    }
    StopEverything("FSUDAQ was closed");
    WaitForLed(3000);
    if( scope ) {
      delete scope;
      scope = nullptr;
    }
    if( digiSettings ) {
      delete digiSettings;
      digiSettings = nullptr;
    }
    if( singleHistograms ) {
      delete singleHistograms;
      singleHistograms = nullptr;
    }
    if( onlineAnalyzer ) {
      delete onlineAnalyzer;
      onlineAnalyzer = nullptr;
    }
    event->accept();
  }

public slots:
  void UpdateScalar();
  void ShutdownOnSignal(int sig); // SIGINT/SIGTERM/SIGHUP: stop a run properly, then quit
  void StopEverything(const QString & why); // the scope's acquisition, a run (recorded with why) and a series

private slots:

  void OpenDataPath();
  void OpenSettingsPath();     // folder with the Digi-<serial>_<DPP>.bin files read by "w/ settings"
  void UpdateDataPathEnabled(); // the data path field is greyed while Save Data is off
  void OpenRecord();
  void UpdateRecord();
  void LoadProgramSettings();
  void SaveProgramSettings();
  void LoadLastRunFile();
  void SaveLastRunFile();

  void OpenDigitizers();
  void CloseDigitizers();
  void WaitForDigitizersOpen(bool onOff);

  void SetupScalar();
  void CleanUpScalar();
  void OpenScalar();

  void StartACQ();
  void StopACQ();
  void AutoRun();
  bool CommentDialog(bool isStartRun);
  QString RunFolder() const;            // <data path>/<prefix>_<run>, where this run's files go
  QStringList ExistingRunFiles() const; // files already carrying this prefix and run number
  void WriteRunTimestamp(bool isStartRun, const QString & timeStamp);

  void OpenScope();

  void OpenDigiSettings();

  void OpenDashboard();         // button: start the dashboard (asks during a run) and open the browser
  bool StartDashboardProcess(); // start online/online_dashboard.py if it is not running; true if it runs
  void AskDashboardToOpenPage(); // HTTP GET /open on the running dashboard: it opens the browser from its own process
  void TellDashboard(const QString & what, const QString & folder, int attempt = 0); // HTTP GET /run at the start of a saving run, /stopped at its end

  void UpdateAllPanels(int panelID);

  void SetUpInflux();

  void CheckElog();
  void WriteElog(QString htmlText, QString subject, QString category, int runNumber);
  void AppendElog(QString appendHtmlText);

  void SetSyncMode();

  void SetAndLockInfluxElog();

private:

  Digitizer ** digi;
  unsigned int nDigi;
  bool isACQStarted;
  bool openedWithSettings;      // opened "w/ settings": every board must hold its settings file before a run
  QStringList BoardsNotProgrammed() const;  // boards opened "w/ settings" that are not programmed from a file

  QString programSettingsFilePath;
  QString rawDataPath;
  QString settingsPath;         // digitizer settings files; empty in programSettings.txt = the data path
  QStringList boardOrder;       // serials in rack order (programSettings.txt boardOrder=): the Run Monitor and the dashboard show the boards so
  std::vector<unsigned int> RackOrder() const; // the open boards (digi indices) in boardOrder, those not in it after, in port order
  QString prefix;
  unsigned int runID;
  int elogID;

  RComboBox * cbOpenDigitizers;
  RComboBox * cbOpenMethod;

  QPushButton * bnOpenDigitizers;
  QPushButton * bnOpenScope;
  QPushButton * bnDigiSettings;
  //QPushButton * bnAnalyzer;


  QPushButton * bnOpenScaler;
  QPushButton * bnStartACQ;
  QPushButton * bnStopACQ;
  QPushButton * bnSync;

  QPushButton * bnDashboard;
  QCheckBox * chkAutoDashboard; // start the dashboard before a run if it is not up yet
  QProcess * dashboardProc;
  QNetworkAccessManager * net;

  QTimer * runTimer;            // a timed run ends when it fires
  QTimer * pauseTimer;          // the pause before the next run of a repeated series
  bool needManualComment;
  bool inDataRun;               // between StartACQ and StopACQ of a run (the scope's acquisition is not one)
  bool scopeShown;              // the scope window is open (from OpenScope until it closes)
  QString abortComment;         // the stop comment when FSUDAQ stops a run because a board failed
  struct Series {               // a timed (and maybe repeated) run started by Start ACQ, fixed when it starts
    bool active = false;
    bool repeat = false;
    bool save = false;
    qint64 runMs = 0;
    qint64 pauseMs = 0;
  } series;
  void UpdateRunControls();     // enable the run widgets for the present state (open, running, pausing, scope)
  bool lastStopHadProblems;     // the run just stopped had failures (a series then ends)
  bool closingBySignal = false; // ShutdownOnSignal: close without asking
  std::vector<bool> boardStuck; // a board whose readout is stuck in the CAEN library: nothing more until FSUDAQ restarts
  QString replacedDir;          // "Overwrite": the old files of the run number, set aside until the run has started
  QStringList StuckBoards() const;
  void RunTimeUp();
  void PauseOver();
  void StopPressed();
  void EndSeries(const QString & why);
  QStringList RunFailures() const;      // boards that stopped reading or could not save since the run started
  void AbortRun(const QStringList & failures);

  //@----- influx
  InfluxDB * influx;

  QString influxIP;
  QString dataBaseName;
  QLineEdit * leInfluxIP;
  QLineEdit * leDatabaseName;
  QPushButton * bnLock;
  QString influxToken;
  short scalarCount;
  
  //@----- Elog
  QString elogIP;
  QString elogPort;
  QString elogName;
  QString elogUser;
  QString elogPWD;
  QLineEdit * leElogIP;
  QLineEdit * leElogName;

  QCheckBox * chkInflux;
  QCheckBox * chkElog;

  //@----- log msg
  QPlainTextEdit * logInfo;
  void LogMsg(QString msg);
  bool logMsgHTMLMode = true;

  //@-----
  QLineEdit * leDataPath;
  QLabel * lbDataPath;
  QPushButton * bnSetDataPath;
  QLineEdit * leSettingsPath;
  QPushButton * bnSetSettingsPath;
  QLineEdit * lePrefix;
  QLineEdit * leComment;
  QLineEdit * leRunID;

  QCheckBox * chkSaveData;
  QCheckBox * chkAutoIncrement;
  QCheckBox * chkSkipComment;
  RSpinBox  * sbRunTimeMin;      // run length in minutes, 0 = until Stop
  QCheckBox * chkRepeatRun;      // start the next run when the time is up
  RSpinBox  * sbRepeatPauseSec;  // pause between repeated runs
  RSpinBox  * sbFileSizeMB;      // roll-over size of the per-board data files
  void EnableRunLengthControls(bool enable);
  QString RunLengthText() const; // e.g. "30 min" or "0.5 min"

  QString startComment;
  QString stopComment;

  //@----- LED trigger: a light on the detectors, switched through the PLC in every run, so that
  //       the boards' start phase against their 16 ns clock can be measured from it
  QCheckBox * chkLed;
  RSpinBox  * sbLedDelaySec;     // on this long after the boards started
  RSpinBox  * sbLedHoldSec;      // then off this long after it went on
  QLabel    * lbLedStatus;
  QString ledRelayURL;           // the pion-ros plc_command node's HTTP relay (programSettings.txt)
  QString ledOnCommand;
  QString ledOffCommand;
  QTimer * ledOnTimer;
  QTimer * ledOffTimer;
  struct LedEvent {              // one command of this run, for the run file
    QString what, command, sent, answered, reply, error;
    QString switched;            // could the PLC have switched: "yes", "no" (refused, or never reached it), "unknown" (e.g. no answer in time)
    qint64 runMs = -1;           // since the boards started (the run clock)
    bool done = false, ok = false;
  };
  QList<LedEvent> ledEvents;
  int ledRun = 0;                // which run the events belong to: a late answer of the previous run is not this run's
  bool ledOnSent = false;        // this run asked for the LED on: it is switched off at the stop
  int ledPending = 0;            // requests not answered yet
  void StartLedCycle();          // at the start of a run: the on and off timers
  void LedCommand(const QString & what); // "on" / "off": POST to the relay, the answer into ledEvents
  void EndLedCycle();            // at the stop: the timers end, the LED off if this run switched it on
  void WaitForLed(int ms);       // closing: let a last "off" be answered
  void EnableLedControls(bool enable);

  //@----- run file: <prefix>_<run>_run.json in the run folder, everything about the run that is not data
  QString runFilePath;           // empty: no saved run yet
  QJsonObject runFileHead;       // what is known at the start
  QJsonObject runFileStop;       // filled at the stop
  void StartRunFile(const QString & runDir, const QString & runIDStr);
  void SaveRunFile();            // (re)written at the start, at each LED answer and at the stop
  static QJsonObject BoardSettingsJson(Digitizer * d); // the registers in memory (read back at the start), by name
  static QString SourceCommit(); // the git commit of the source tree FSUDAQ runs from

  //@----- Scalar
  QMainWindow  * scalar;
  QGridLayout * scalarLayout;          // the board blocks, as many per row as the window is wide (ReflowScalar)
  QScrollArea * scalarScroll;
  std::vector<QWidget *> scalarBoards; // one block per board: its name, summary rows and channels
  int scalarColumns;                   // board blocks per row now
  void ReflowScalar();
  TimingThread * scalarTimingThread;
  // QThread * scalarThread;
  // ScalarWorker * scalarWorker;
  // QTimer * scalarTimer;


  // TimingThread * scalarThread;
  QLineEdit  *** leTrigger; // need to delete manually
  QLineEdit  *** leAccept; // need to delete manually (now: Input/s)
  QLineEdit  *** leDead;   // need to delete manually (Missed %)
  QLineEdit  *** leLost;   // need to delete manually (Lost %)
  // Input, missed and lost from the board's flags (bit 13 every 1024 triggers, bit 12 every 1024 lost
  // triggers, bit 15 on the first event after a loss): accumulated over a window that grows until it
  // holds enough flags (per second is far too coarse at low rates); the last complete window is shown.
  struct DeadTimeWindow {
    enum State { Wait, Valid, Low, BadFlags, OtherStep }; // OtherStep: flags every 128 or 8192 triggers, not 1024
    uint64_t flagsTotal = 0;     // 1024-trigger flags since the start (the board's counter starts at zero)
    uint64_t sinceFlag = 0;      // events recorded after the newest flag
    double msSinceFlag = 0;      // time since the refresh that brought the newest flag
    // open window: from flag number startF to the newest flag
    uint64_t startF = 0, wCounts = 0, lostFlags = 0, lostNFlags = 0; double ms = 0;
    // last complete window
    double inputRate = -1, lostRate = -1, missed = -1, lost = -1;
    bool lostSeen = false;
    State state = Wait;
  };

  std::vector<std::vector<DeadTimeWindow>> deadWin;
  QPushButton * runStatus[MaxNDigitizer];
  QLabel * lbLastUpdateTime;
  QLabel * lbScalarACQStatus;
  QLabel * lbBoardValue[MaxNDigitizer][5]; // board summary: Counts/s, Input/s, Missed, Lost, read MB/s
  QLabel * lbProblems[MaxNDigitizer];      // cut reads, bad channels, NOT SAVED; empty when fine
  QLabel * lbAggCount[MaxNDigitizer]; // aggregates decoded / read calls, per refresh
  QLabel * lbFileSize[MaxNDigitizer];
  QLabel * lbTotalFileSize;           // totals over the boards: size, events/s, MB/s
  QElapsedTimer scalarClock;          // time between scalar refreshes, for the rates
  QLabel * lbRunTime;                 // run number and elapsed acquisition time
  QElapsedTimer runClock;             // started when the boards start
  QTimer * runClockTimer;             // refreshes lbRunTime every 100 ms
  QString runClockLabel;              // "Run 7" / "Run (not saved)" / "Scope"
  void StartRunClock(const QString & what);
  void StopRunClock();
  static QString ElapsedText(qint64 ms); // h:mm:ss.mmm
  static QString RateText(double perSecond); // 1234567 -> "1.23 M", 12345 -> "12.3 k"
  static QString DeadText(double fraction);  // missed fraction, one decimal
  static QString LostText(double fraction, bool seen); // lost fraction; "> 0" when only the bit-15 flag says so

  //@----- Run Record
  QMainWindow * runRecord;
  QStandardItemModel *model;
  QTableView * tableView;

  //@----- ACQ
  ReadDataThread ** readDataThread;
  
  //@----- Scope
  Scope * scope;

  //@----- DigiSettings
  DigiSettingsPanel * digiSettings;

  //@----- SingleSpectra
  SingleSpectra * singleHistograms;

  //@----- Analyzer
  Analyzer * onlineAnalyzer;
  
  QString maskText(const QString &password) {
    if (password.length() <= 3) {
      return password; // No masking needed for short passwords
    } else if (password.length() <= 10) {
      QString maskedPassword = password.left(3);
      maskedPassword += QString("*").repeated(password.length() - 3);
      return maskedPassword;
    } else {
      return password.left(3) + QString("*").repeated(7);
    }
  }


};

//^======================== Scalar Worker

// class ScalarWorker : public QObject{
//   Q_OBJECT
// public:
//   ScalarWorker(FSUDAQ * parent): SS(parent){}

// public slots:
//   void UpdateScalar(){
//     SS->UpdateScalar();
//     emit workDone();
//   }

// signals:
//   void workDone();

// private:
//   FSUDAQ * SS;
// };


#endif // MAINWINDOW_H