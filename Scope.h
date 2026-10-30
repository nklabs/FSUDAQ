#ifndef SCOPE_H
#define SCOPE_H

#include <QMainWindow>
#include <QChart>
#include <QChartView>
#include <QSpinBox>
#include <QLabel>
#include <QPushButton>
#include <QToolButton>
#include <QCheckBox>
#include <QLineEdit>
#include <QComboBox>
#include <QGridLayout>
#include <QGroupBox>
#include <QTabWidget>
#include <QTimer>
#include <QLineSeries>
#include <QAreaSeries>
#include <QValueAxis>
#include <QRubberBand>
#include <QMouseEvent>
#include <QGestureEvent>

#include <functional>
#include <vector>

#include "macro.h"
#include "ClassDigitizer.h"
#include "CustomThreads.h"
#include "CustomWidgets.h"

//^====================================================
//^ The waveform inspector, laid out as CoMPASS's: the source (board, channel) and the four traces
//^ above the plot, the plot's icon bar (Single, Start/Stop refresh, Unzoom, Snapshot), and the
//^ channel's parameters in CoMPASS's tabs (Input, Discriminator, QDC / Trapezoid) beside it.
//^ The parameters can be changed while the scope runs: the board is stopped, written and restarted.
class Scope : public QMainWindow{
  Q_OBJECT
  friend class FSUDAQTest; // tests/test_daq.cpp

public:
  Scope(Digitizer ** digi, unsigned int nDigi, ReadDataThread ** readDataThread, QMainWindow * parent = nullptr);
  ~Scope();

  void closeEvent(QCloseEvent * event){
    if(isACQStarted) StopScope();
    emit CloseWindow();
    event->accept();
  }

  unsigned short ID; // the id of digi, index of cbScopeDigi
  bool isACQStarted;
  int tick2ns;

public slots:
  void StartScope();
  void StopScope();
  void UpdateScope();
  void ClearTraces();   // empty the plot (no data, channel or board switched)
  void ReadSettingsFromBoard();
  void UpdatePanelFromMomeory();

signals:
  void CloseWindow();
  void SendLogMsg(const QString &msg);
  void TellACQOnOff(const bool onOff);
  void UpdateOtherPanels();

private:

  Digitizer ** digi;
  unsigned short nDigi;
  ReadDataThread ** readDataThread;

  bool enableSignalSlot;

  //---- running the boards
  bool StartBoard(int iDigi, bool alone);  // scope settings on the board (not saved) and start it
  void StopBoard(int iDigi);               // stop it and write the board's saved settings back
  void ApplyChange(const std::function<void()> & write); // write now, or stop / write / restart a running board
  bool IsRunning(int iDigi) const { return isACQStarted && boardRuns[iDigi]; }
  void UpdateAutoTrigger();

  bool boardRuns[MaxNDigitizer];   // started by the scope
  bool startedAlone[MaxNDigitizer];// started on its own (software start), not with the others
  bool soleRun;                    // "Only this channel" when the scope started: the board and channel
  int  soleDigi;
  int  soleCh;

  //---- one parameter control: a register, or a field of one, of the selected channel
  struct Param {
    enum Kind { Time, Raw, DCOffset, Combo };
    Kind kind;
    Reg reg;
    std::pair<unsigned short, unsigned short> bits; // {0, 0}: the whole register
    std::function<Reg(int ch)> regOf;              // when the register depends on the channel (QDC thresholds)
    RSpinBox * sb = nullptr;
    RComboBox * cb = nullptr;
    Param(Kind k, const Reg & r, std::pair<unsigned short, unsigned short> b) : kind(k), reg(r), bits(b) {}
  };
  std::vector<Param> params;
  RComboBox * cbBaselineAvg;  // PSD: "Fixed" enables the fixed baseline
  RSpinBox  * sbFixedBaseline;
  RComboBox * cbChargeSens;   // PSD: its list follows the input range

  QWidget * NewTab(const QString & name, QGridLayout * & layout);
  void AddSpin (QGridLayout * layout, const QString & label, Param::Kind kind, const Reg & reg, std::pair<unsigned short, unsigned short> bits = {0, 0});
  void AddCombo(QGridLayout * layout, const QString & label, const Reg & reg, std::pair<unsigned short, unsigned short> bits,
                const std::vector<std::pair<std::string, unsigned int>> & list);
  Reg  RegOf(const Param & p, int ch) const { return p.regOf ? p.regOf(ch) : p.reg; }
  int  RegCh(int ch) const; // the register's channel index: the group for DPP-QDC
  void WriteParam(Param & p);
  void ShowParam(Param & p);

  void BuildPanels();        // the trace selectors and the tabs for the board's firmware
  void BuildTraceSelectors();
  void WriteTraceSelection(QObject * from);
  void ShowTraceSelection();
  void SetUpTabs_PHA();
  void SetUpTabs_PSD();
  void SetUpTabs_QDC();
  void EnableControl(bool enable);

  //---- drawing
  void DrawTrace(const ReadDataThread::ScopeTrace & trace, bool isNew);
  void ApplyAutoRange();
  bool eventFilter(QObject * watched, QEvent * event) override; // double-click on the plot: unzoom
  void ClearPersistence();
  void SetRefreshPaused(bool paused);
  void SaveSnapshot();
  void SaveTraceCSV();
  double YScale() const;     // LSB -> the y unit
  QString TraceName(int i) const;

  RChart * plot;
  RChartView * plotView;
  QValueAxis * xAxis;
  QValueAxis * yAxis;
  QLineSeries * dataTrace[MaxNumberOfTrace]; // 2 analog, 2 digi for PHA, PSD, 1 analog, 4 digi for QDC
  QLineSeries * persistTrace[MaxPersistTrace];
  int persistNext;
  QLineSeries * thresholdLine;
  QLineSeries * gateUpper[2];
  QLineSeries * gateLower[2];
  QAreaSeries * gateArea[2];   // the long and the short gate, from the digital traces that show them

  ReadDataThread::ScopeTrace lastTrace;  // the trace on the plot
  bool haveLastTrace;
  bool refreshPaused;
  bool singleArmed;
  double dataYMin, dataYMax, dataXMax;   // of the trace on the plot, for the automatic range

  //---- widgets
  RComboBox * cbScopeDigi;
  RComboBox * cbScopeCh;
  RComboBox * cbAnaTrace1;
  RComboBox * cbAnaTrace2;
  RComboBox * cbDigiTrace1;
  RComboBox * cbDigiTrace2;

  QPushButton * bnScopeStart;
  QPushButton * bnScopeStop;
  QCheckBox * chkSoleRun;
  QPushButton * runStatus;
  QLineEdit * leTriggerRate;

  QToolButton * bnSingle;
  QToolButton * bnRefresh;   // Start/Stop of the plot refresh
  QToolButton * bnUnzoom;
  QToolButton * bnSnapshot;
  QToolButton * bnSaveTrace;
  QCheckBox * chkAutoTrigger;
  QCheckBox * chkAutoY;
  RComboBox * cbPersist;
  RComboBox * cbYUnit;

  QTabWidget * tabs;
  QPushButton * bnReadSettingsFromBoard;

  QTimer * scopeTimer; // drives UpdateScope() in the GUI thread

  static QString saveDir;  // where the last snapshot or trace went

};

#endif
