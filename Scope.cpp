#include "Scope.h"

#include <QApplication>
#include <QBoxLayout>
#include <QDateTime>
#include <QDir>
#include <QFile>
#include <QFileDialog>
#include <QFileInfo>
#include <QLabel>
#include <QLegendMarker>
#include <QScreen>
#include <QStyle>
#include <QTextStream>
#include <cmath>

QString Scope::saveDir;

namespace {
  typedef std::pair<unsigned short, unsigned short> Bits;
  uint32_t ClearField(uint32_t v, Bits b){ return v & ~(((1u << b.first) - 1) << b.second); }
  uint32_t SetField(uint32_t v, Bits b, uint32_t x){ return ClearField(v, b) | ((x & ((1u << b.first) - 1)) << b.second); }

  /// DPP-PSD: CoMPASS's Analog Trace 1 (0 Input, 1 CFD) and 2 (0 None, 1 Baseline, 2 CFD) <-> 0x8000 bits [13:11],
  /// probe * 2 + dual trace (UM4380): Input | CFD (single trace), Input + Baseline, CFD + Baseline, Input + CFD
  int PSDProbeField(int a1, int a2){
    if( a2 == 0 ) return a1 == 0 ? 0 : 2;
    if( a2 == 1 ) return a1 == 0 ? 1 : 3;
    return 5;
  }
  void PSDTraces(int field, int & a1, int & a2){
    switch( field ){
      case 1: a1 = 0; a2 = 1; break;
      case 2: a1 = 1; a2 = 0; break;
      case 3: a1 = 1; a2 = 1; break;
      case 5: a1 = 0; a2 = 2; break;
      default: a1 = 0; a2 = 0; break;
    }
  }
  const int PHANoSecondTrace = 99; // PHA "Analog trace 2 = None": dual trace off

  void SelectData(RComboBox * cb, int data){
    for( int i = 0; i < cb->count(); i++ ) if( cb->itemData(i).toInt() == data ){ cb->setCurrentIndex(i); return; }
    cb->setCurrentIndex(-1);
  }
  void Fill(RComboBox * cb, const std::vector<std::pair<std::string, unsigned int>> & list){
    cb->clear();
    for( auto & x : list ) cb->addItem(QString::fromStdString(x.first), x.second);
  }
}

//^========================================================
//^========================================================
Scope::Scope(Digitizer ** digi, unsigned int nDigi, ReadDataThread ** readDataThread, QMainWindow * parent) : QMainWindow(parent){
  DebugPrint("%s", "Scope");
  this->digi = digi;
  this->nDigi = nDigi;
  this->readDataThread = readDataThread;

  setWindowTitle("Scope");
  setWindowFlags( this->windowFlags() & ~Qt::WindowCloseButtonHint );

  QRect screenGeo = QGuiApplication::primaryScreen()->availableGeometry();
  setGeometry(0, 0, std::min(1400, screenGeo.width() - 50), std::min(850, screenGeo.height() - 50));

  enableSignalSlot = false;
  isACQStarted = false;
  soleRun = false;
  soleDigi = 0;
  soleCh = 0;
  for( int i = 0; i < MaxNDigitizer; i++ ){ boardRuns[i] = false; startedAlone[i] = false; }
  cbBaselineAvg = nullptr;
  sbFixedBaseline = nullptr;
  cbChargeSens = nullptr;
  haveLastTrace = false;
  refreshPaused = false;
  singleArmed = false;
  dataYMin = 0; dataYMax = 0x3FFF; dataXMax = 1000;
  persistNext = 0;

  ID = 0;
  tick2ns = digi[ID]->GetTick2ns();

  //*================================== plot
  plot = new RChart();
  plot->setAnimationOptions(QChart::NoAnimation);
  xAxis = new QValueAxis();
  yAxis = new QValueAxis();
  xAxis->setTitleText("Time [ns]");
  xAxis->setLabelFormat("%.0f");
  xAxis->setTickCount(11);
  yAxis->setLabelFormat("%.0f");
  yAxis->setTickCount(9);
  plot->addAxis(xAxis, Qt::AlignBottom);
  plot->addAxis(yAxis, Qt::AlignLeft);

  auto attach = [&](QAbstractSeries * s, bool inLegend){
    plot->addSeries(s);
    s->attachAxis(xAxis);
    s->attachAxis(yAxis);
    if( !inLegend ) for( QLegendMarker * m : plot->legend()->markers(s) ) m->setVisible(false);
  };

  // drawn first, so beneath the traces: the gates (in the colours of the digital traces that show them), then the persistence
  QColor gateColor[2] = {QColor(Qt::darkYellow), QColor(Qt::darkGreen)};
  gateColor[0].setAlpha(45);
  gateColor[1].setAlpha(45);
  for( int i = 0; i < 2; i++ ){
    gateUpper[i] = new QLineSeries(plot);
    gateLower[i] = new QLineSeries(plot);
    gateArea[i] = new QAreaSeries(gateUpper[i], gateLower[i]);
    gateArea[i]->setPen(Qt::NoPen);
    gateArea[i]->setBrush(gateColor[i]);
    attach(gateArea[i], false);
  }
  for( int i = 0; i < MaxPersistTrace; i++ ){
    persistTrace[i] = new QLineSeries();
    persistTrace[i]->setPen(QPen(QColor(220, 0, 0, 45), 1));
    attach(persistTrace[i], false);
    persistTrace[i]->setVisible(false);
  }

  const QPen pens[MaxNumberOfTrace] = {QPen(Qt::red, 2), QPen(Qt::blue, 2), QPen(Qt::darkYellow, 1), QPen(Qt::darkGreen, 1), QPen(Qt::darkMagenta, 1)};
  for( int i = 0; i < MaxNumberOfTrace; i++ ){
    dataTrace[i] = new QLineSeries();
    dataTrace[i]->setPen(pens[i]);
    dataTrace[i]->setName("Trace " + QString::number(i));
    attach(dataTrace[i], true);
  }
  thresholdLine = new QLineSeries();
  thresholdLine->setName("Threshold");
  thresholdLine->setPen(QPen(Qt::black, 1, Qt::DashLine));
  attach(thresholdLine, true);

  //*================================== UI
  QWidget * layoutWidget = new QWidget(this);
  setCentralWidget(layoutWidget);
  QVBoxLayout * layout = new QVBoxLayout(layoutWidget);

  //================ run control, the plot's icon bar (CoMPASS: Single, Start/Stop, Unzoom, Snapshot)
  QHBoxLayout * top = new QHBoxLayout();
  layout->addLayout(top);

  bnScopeStart = new QPushButton("Start", this);
  bnScopeStart->setToolTip("Start the boards for the scope (the x725/x730 that are not S versions calibrate their ADCs first, as CoMPASS).");
  top->addWidget(bnScopeStart);
  connect(bnScopeStart, &QPushButton::clicked, this, [=](){ StartScope(); });

  bnScopeStop = new QPushButton("Stop", this);
  top->addWidget(bnScopeStop);
  connect(bnScopeStop, &QPushButton::clicked, this, &Scope::StopScope);

  chkSoleRun = new QCheckBox("Only this channel", this);
  chkSoleRun->setToolTip("Run only the selected board, with only the selected channel enabled and self-triggering.");
  top->addWidget(chkSoleRun);

  top->addSpacing(16);

  auto toolButton = [&](const QString & text, QStyle::StandardPixmap icon, const QString & tip){
    QToolButton * b = new QToolButton(this);
    b->setText(text);
    b->setIcon(style()->standardIcon(icon));
    b->setToolButtonStyle(Qt::ToolButtonTextBesideIcon);
    b->setToolTip(tip);
    top->addWidget(b);
    return b;
  };
  bnSingle    = toolButton("Single", QStyle::SP_MediaSkipForward, "Show the next waveform, then hold it.");
  bnRefresh   = toolButton("Stop", QStyle::SP_MediaPause, "Stop / start the refresh of the plot (the boards keep running).");
  bnUnzoom    = toolButton("Unzoom", QStyle::SP_BrowserReload, "Back to the whole waveform (or double-click the plot). Zoom: drag up or down over the plot.");
  bnSnapshot  = toolButton("Snapshot", QStyle::SP_DialogSaveButton, "Save the plot as a PNG picture.");
  bnSaveTrace = toolButton("Save trace", QStyle::SP_FileIcon, "Save the waveform on the plot as a CSV table.");

  connect(bnSingle, &QToolButton::clicked, this, [=](){ singleArmed = true; SetRefreshPaused(false); });
  connect(bnRefresh, &QToolButton::clicked, this, [=](){ singleArmed = false; SetRefreshPaused(!refreshPaused); });
  connect(bnUnzoom, &QToolButton::clicked, this, [=](){ plot->zoomReset(); ApplyAutoRange(); });
  connect(bnSnapshot, &QToolButton::clicked, this, &Scope::SaveSnapshot);
  connect(bnSaveTrace, &QToolButton::clicked, this, &Scope::SaveTraceCSV);

  top->addSpacing(16);

  chkAutoTrigger = new QCheckBox("Auto trigger", this);
  chkAutoTrigger->setToolTip("A software trigger to the selected channel every " + QString::number(AutoTriggerMiliSec) + " ms, in OR with its self-trigger,\n"
                             "as CoMPASS's Auto trigger (an oscilloscope's force trigger): shows the baseline with no pulse over the threshold.");
  top->addWidget(chkAutoTrigger);
  connect(chkAutoTrigger, &QCheckBox::toggled, this, [=](){ UpdateAutoTrigger(); });

  top->addWidget(new QLabel("Persistence", this));
  cbPersist = new RComboBox(this);
  cbPersist->addItem("Off", 0);
  cbPersist->addItem("10", 10);
  cbPersist->addItem("20", 20);
  cbPersist->addItem(QString::number(MaxPersistTrace), MaxPersistTrace);
  cbPersist->setToolTip("Also draw this many earlier waveforms of analog trace 1, faded.");
  top->addWidget(cbPersist);
  connect(cbPersist, &RComboBox::currentIndexChanged, this, [=](){ ClearPersistence(); });

  top->addWidget(new QLabel("Y", this));
  cbYUnit = new RComboBox(this);
  cbYUnit->addItem("lsb", 0);
  cbYUnit->addItem("mV", 1);
  cbYUnit->setToolTip("mV: lsb x input range / 2^14. The input scale is not calibrated (CoMPASS manual): for relative amplitudes only.");
  top->addWidget(cbYUnit);
  connect(cbYUnit, &RComboBox::currentIndexChanged, this, [=](){ ClearPersistence(); if( haveLastTrace ) DrawTrace(lastTrace, false); });

  chkAutoY = new QCheckBox("Auto Y", this);
  chkAutoY->setChecked(false);   // the whole ADC range, steady; zoom with the mouse (an axis fitted to each waveform jumps)
  chkAutoY->setToolTip("Fit the y axis to each waveform; off: the whole ADC range (zoom by dragging up or down over the plot).");
  top->addWidget(chkAutoY);
  connect(chkAutoY, &QCheckBox::toggled, this, [=](){ if( haveLastTrace ) DrawTrace(lastTrace, false); else ApplyAutoRange(); });

  top->addStretch(1);

  top->addWidget(new QLabel("Run", this));
  runStatus = new QPushButton("", this);
  runStatus->setEnabled(false);
  runStatus->setFixedSize(QSize(20,20));
  top->addWidget(runStatus);

  top->addWidget(new QLabel("Trigger rate [Hz]", this));
  leTriggerRate = new QLineEdit(this);
  leTriggerRate->setAlignment(Qt::AlignRight);
  leTriggerRate->setReadOnly(true);
  leTriggerRate->setMaximumWidth(110);
  top->addWidget(leTriggerRate);

  //================ the source and the traces (CoMPASS: above the waveform plot)
  QHBoxLayout * src = new QHBoxLayout();
  layout->addLayout(src);

  src->addWidget(new QLabel("Source", this));
  cbScopeDigi = new RComboBox(this);
  for(unsigned int i = 0; i < nDigi; i++) cbScopeDigi->addItem("Digi-" + QString::number(digi[i]->GetSerialNumber()), i);
  src->addWidget(cbScopeDigi);
  cbScopeCh = new RComboBox(this);
  for( int i = 0; i < digi[ID]->GetNumInputCh(); i++) cbScopeCh->addItem("Ch-" + QString::number(i));
  src->addWidget(cbScopeCh);
  src->addSpacing(16);

  auto traceBox = [&](const QString & label, RComboBox * & cb){
    src->addWidget(new QLabel(label, this));
    cb = new RComboBox(this);
    cb->setSizeAdjustPolicy(QComboBox::AdjustToContents);
    src->addWidget(cb);
    connect(cb, &RComboBox::currentIndexChanged, this, [=](){ if( enableSignalSlot ) WriteTraceSelection(cb); });
  };
  traceBox("Analog trace 1", cbAnaTrace1);
  traceBox("Analog trace 2", cbAnaTrace2);
  traceBox("Digital trace 1", cbDigiTrace1);
  traceBox("Digital trace 2", cbDigiTrace2);
  src->addStretch(1);

  connect(cbScopeDigi, &RComboBox::currentIndexChanged, this, [=](int index){
    if( !enableSignalSlot ) return;
    const bool restart = isACQStarted && soleRun;   // only that board and channel run: start again with the new one
    if( restart ) StopScope();
    if( isACQStarted ) readDataThread[ID]->SetAutoTrigger(-1);

    ID = index;
    tick2ns = digi[ID]->GetTick2ns();

    enableSignalSlot = false;
    cbScopeCh->clear();
    for( int i = 0; i < digi[ID]->GetNumInputCh(); i++) cbScopeCh->addItem("Ch-" + QString::number(i));
    enableSignalSlot = true;

    ClearTraces();
    ClearPersistence();
    BuildPanels();
    if( !IsRunning(ID) ) ReadSettingsFromBoard();
    UpdateAutoTrigger();
    if( restart ) StartScope();
  });

  connect(cbScopeCh, &RComboBox::currentIndexChanged, this, [=](){
    if( !enableSignalSlot ) return;
    const bool restart = isACQStarted && soleRun;
    if( restart ) StopScope();
    ClearTraces();
    ClearPersistence();
    if( !IsRunning(ID) ) ReadSettingsFromBoard(); else UpdatePanelFromMomeory();
    UpdateAutoTrigger();
    if( restart ) StartScope();
  });

  //================ the plot, and the channel's parameters in CoMPASS's tabs beside it
  QHBoxLayout * mid = new QHBoxLayout();
  layout->addLayout(mid, 1);

  plotView = new RChartView(plot, this);
  plotView->setRubberBand(QChartView::VerticalRubberBand); // a drag zooms the y axis only; the time axis stays whole
  plotView->viewport()->installEventFilter(this);           // double-click: unzoom
  mid->addWidget(plotView, 1);

  QVBoxLayout * side = new QVBoxLayout();
  mid->addLayout(side);

  tabs = new QTabWidget(this);
  tabs->setMinimumWidth(340);
  side->addWidget(tabs);

  QLabel * lbApply = new QLabel("Enter or a new choice applies the value to the board, also while the scope runs (the board restarts).", this);
  lbApply->setWordWrap(true);
  lbApply->setMaximumWidth(340);
  side->addWidget(lbApply);

  bnReadSettingsFromBoard = new QPushButton("Read settings from board", this);
  side->addWidget(bnReadSettingsFromBoard);
  connect(bnReadSettingsFromBoard, &QPushButton::clicked, this, &Scope::ReadSettingsFromBoard);

  QPushButton * bnClearBuffer = new QPushButton("Clear board buffer", this);
  side->addWidget(bnClearBuffer);
  connect(bnClearBuffer, &QPushButton::clicked, this, [=](){ digi[ID]->WriteRegister(DPP::SoftwareClear_W, 1);});

  QPushButton * bnClearMemory = new QPushButton("Clear Memory", this);
  side->addWidget(bnClearMemory);
  connect(bnClearMemory, &QPushButton::clicked, this, [=](){
    // not while the scope runs: its readout thread copies from this memory (the board mutexes are per
    // file, so the lock below does not keep it out)
    if( isACQStarted ){ SendLogMsg("Stop the scope before clearing the memory."); return; }
    digiMTX[ID].lock();
    digi[ID]->GetData()->ClearData();
    digiMTX[ID].unlock();
  });

  side->addStretch(1);

  QPushButton * bnClose = new QPushButton("Close", this);
  side->addWidget(bnClose);
  connect(bnClose, &QPushButton::clicked, this, &Scope::close);

  //================ hints
  QLabel * lbhints = new QLabel("Drag up or down over the plot to zoom the y axis; double-click, Unzoom or right-click to go back; the wheel zooms both axes; 'r' restores, '+/-' zoom, arrows pan.  "
                                "Up to " + QString::number(MaxDisplayTraceTimeLength / 1000.) + " µs drawn, updates every " + QString::number(ScopeUpdateMiliSec / 1000.) + " s.", this);
  layout->addWidget(lbhints);

  bnScopeStart->setEnabled(true);
  bnScopeStart->setStyleSheet("background-color: green;");
  bnScopeStop->setEnabled(false);
  bnScopeStop->setStyleSheet("");

  enableSignalSlot = true;
  BuildPanels();
  ApplyAutoRange();

  // UpdateScope() touches widgets and chart series, so it must run in the GUI
  // thread. It used to run in a worker thread (ScopeWorker), which is undefined
  // behaviour in Qt and corrupted the heap ("malloc(): unaligned tcache chunk
  // detected" inside QCss::Parser from the worker thread, 29 Sep 2026).
  scopeTimer = new QTimer(this);
  connect(scopeTimer, &QTimer::timeout, this, &Scope::UpdateScope);

}

Scope::~Scope(){
  DebugPrint("%s", "Scope");
  scopeTimer->stop();
  // the chart, its series and axes belong to the chart view, a child of this window
}

//*=======================================================
//*======================================================= running the boards

bool Scope::StartBoard(int i, bool alone){
  Digitizer * d = digi[i];
  const int dpp = d->GetDPPType();

  // The scope's settings go to the board only (written with isSave2MemAndFile = false): the settings
  // memory and file keep the saved ones, and StopBoard writes those back.
  d->WriteRegister(DPP::BoardConfiguration, SetField(d->GetSettingFromMemory(DPP::BoardConfiguration), DPP::Bit_BoardConfig::RecordTrace, 1), -1, false);

  if( alone ){
    // started without the others: it must not wait for a start from another board (S-IN or TRG-IN start)
    uint32_t acq = d->GetSettingFromMemory(DPP::AcquisitionControl);
    d->WriteRegister(DPP::AcquisitionControl, SetField(acq, DPP::Bit_AcquistionControl::StartStopMode, 0), -1, false);
  }

  if( soleRun && i == soleDigi ){
    if( dpp == DPPTypeCode::DPP_PHA_CODE || dpp == DPPTypeCode::DPP_PSD_CODE ){
      const bool pha = dpp == DPPTypeCode::DPP_PHA_CODE;
      // self-trigger on, no coincidence, no couple trigger logic; only this channel enabled
      uint32_t alg = d->GetSettingFromMemory(DPP::DPPAlgorithmControl, soleCh);
      alg = ClearField(alg, pha ? DPP::Bit_DPPAlgorithmControl_PHA::TriggerMode : DPP::Bit_DPPAlgorithmControl_PSD::TriggerMode);
      alg = ClearField(alg, pha ? DPP::Bit_DPPAlgorithmControl_PHA::DisableSelfTrigger : DPP::Bit_DPPAlgorithmControl_PSD::DisableSelfTrigger);
      const Reg alg2Reg = pha ? DPP::PHA::DPPAlgorithmControl2_G : DPP::PSD::DPPAlgorithmControl2_G;
      uint32_t alg2 = d->GetSettingFromMemory(alg2Reg, soleCh);
      alg2 = ClearField(alg2, DPP::PSD::Bit_DPPAlgorithmControl2::LocalShapeTriggerMode);  // same bits in PHA
      alg2 = ClearField(alg2, DPP::PSD::Bit_DPPAlgorithmControl2::LocalTrigValidMode);
      d->WriteRegister(DPP::DPPAlgorithmControl, alg, soleCh, false);
      d->WriteRegister(alg2Reg, alg2, soleCh, false);
      d->WriteRegister(DPP::RegChannelEnableMask, 1u << soleCh, -1, false);
    }
    if( dpp == DPPTypeCode::DPP_QDC_CODE ){
      const int grp = soleCh / 8;
      uint32_t alg = d->GetSettingFromMemory(DPP::QDC::DPPAlgorithmControl, grp);
      d->WriteRegister(DPP::QDC::DPPAlgorithmControl, ClearField(alg, DPP::QDC::Bit_DPPAlgorithmControl::TriggerMode), grp, false); // self-trigger
      d->WriteRegister(DPP::QDC::GroupEnableMask, 1u << grp, -1, false);
      uint32_t sub = 1u << (soleCh % 8);
      if( grp == 0 ) sub |= 0x1; // must include the first subchannel
      d->WriteRegister(DPP::QDC::SubChannelMask, sub, grp, false);
    }
  }

  d->WriteRegister(DPP::SoftwareClear_W, 1);
  readDataThread[i]->SetScopeMode(true);
  readDataThread[i]->SetSaveData(false);
  readDataThread[i]->SetAutoTrigger(-1);

  boardRuns[i] = true;
  startedAlone[i] = alone;
  if( !d->StartACQ() ){
    SendLogMsg("<font style=\"color: red;\">Digi-" + QString::number(d->GetSerialNumber()) + ": the scope could not start it.</font>");
    StopBoard(i);
    return false;
  }
  readDataThread[i]->Start();
  return true;
}

void Scope::StopBoard(int i){
  Digitizer * d = digi[i];
  const int dpp = d->GetDPPType();

  readDataThread[i]->SetAutoTrigger(-1);
  if( readDataThread[i]->isRunning() ){
    readDataThread[i]->Stop();
    readDataThread[i]->quit();
    readDataThread[i]->wait();
  }
  readDataThread[i]->SetScopeMode(false);

  digiMTX[i].lock();
  d->StopACQ();
  digiMTX[i].unlock();

  // the saved settings back on the board
  d->WriteRegister(DPP::BoardConfiguration, d->GetSettingFromMemory(DPP::BoardConfiguration), -1, false);
  if( startedAlone[i] ) d->WriteRegister(DPP::AcquisitionControl, d->GetSettingFromMemory(DPP::AcquisitionControl), -1, false);

  if( soleRun && i == soleDigi ){
    if( dpp == DPPTypeCode::DPP_PHA_CODE || dpp == DPPTypeCode::DPP_PSD_CODE ){
      const Reg alg2Reg = dpp == DPPTypeCode::DPP_PHA_CODE ? DPP::PHA::DPPAlgorithmControl2_G : DPP::PSD::DPPAlgorithmControl2_G;
      d->WriteRegister(DPP::DPPAlgorithmControl, d->GetSettingFromMemory(DPP::DPPAlgorithmControl, soleCh), soleCh, false);
      d->WriteRegister(alg2Reg, d->GetSettingFromMemory(alg2Reg, soleCh), soleCh, false);
      d->WriteRegister(DPP::RegChannelEnableMask, d->GetSettingFromMemory(DPP::RegChannelEnableMask), -1, false);
    }
    if( dpp == DPPTypeCode::DPP_QDC_CODE ){
      const int grp = soleCh / 8;
      d->WriteRegister(DPP::QDC::DPPAlgorithmControl, d->GetSettingFromMemory(DPP::QDC::DPPAlgorithmControl, grp), grp, false);
      d->WriteRegister(DPP::QDC::GroupEnableMask, d->GetSettingFromMemory(DPP::QDC::GroupEnableMask), -1, false);
      d->WriteRegister(DPP::QDC::SubChannelMask, d->GetSettingFromMemory(DPP::QDC::SubChannelMask, grp), grp, false);
    }
  }

  boardRuns[i] = false;
  startedAlone[i] = false;
}

void Scope::ApplyChange(const std::function<void()> & write){
  // the board takes no settings while it runs (Digitizer::WriteRegister refuses them): stop it, write, start it again
  const bool restart = IsRunning(ID);
  if( restart ) StopBoard(ID);
  write();
  if( restart ){
    StartBoard(ID, true);
    UpdateAutoTrigger();
    ClearPersistence();          // those were taken with the old settings
    lastTrace.absIndex = -1;     // the restart begins the board's memory again
  }
}

void Scope::UpdateAutoTrigger(){
  const int ch = chkAutoTrigger->isChecked() ? cbScopeCh->currentIndex() : -1;
  for( unsigned int i = 0; i < nDigi; i++ ) if( IsRunning(i) ) readDataThread[i]->SetAutoTrigger(i == ID ? ch : -1);
}

//*=======================================================
//*=======================================================
void Scope::StartScope(){
  DebugPrint("%s", "Scope");
  if( !digi || isACQStarted ) return;

  soleRun = chkSoleRun->isChecked();
  soleDigi = ID;
  soleCh = cbScopeCh->currentIndex();

  std::vector<int> boards;
  if( soleRun ){
    boards.push_back(ID);
  }else{
    for( int i = (int)nDigi - 1; i >= 0; i-- ) if( !digi[i]->IsBoardDisabled() ) boards.push_back(i); // the first (the master) last, as a run
  }

  // the x725/x730 that are not S versions: calibrate the ADCs, as CoMPASS at every start
  for( int i : boards ){
    if( !digi[i]->NeedsADCCalibration() ) continue;
    std::string why;
    int ms = digi[i]->CalibrateADC(why);
    QString name = "Digi-" + QString::number(digi[i]->GetSerialNumber()) + " (" + QString::fromStdString(digi[i]->GetModelName()) + ")";
    if( ms >= 0 ) SendLogMsg(name + ": ADCs calibrated in " + QString::number(ms) + " ms.");
    else SendLogMsg("<font style=\"color: orange;\">" + name + ": ADC calibration failed (" + QString::fromStdString(why) + ").</font>");
  }

  isACQStarted = true;
  int nStarted = 0;
  for( int i : boards ){
    SendLogMsg("Digi-" + QString::number(digi[i]->GetSerialNumber()) + " is starting ACQ for the scope." );
    if( StartBoard(i, soleRun) ) nStarted ++;
  }
  if( nStarted == 0 ){
    isACQStarted = false;
    SendLogMsg("<font style=\"color: red;\">The scope started no board.</font>");
    return;
  }
  UpdateAutoTrigger();

  lastTrace.absIndex = -1;
  scopeTimer->start(ScopeUpdateMiliSec);

  bnScopeStart->setEnabled(false);
  bnScopeStart->setStyleSheet("");
  bnScopeStop->setEnabled(true);
  bnScopeStop->setStyleSheet("background-color: red;");
  EnableControl(false);

  emit TellACQOnOff(true);
  emit UpdateOtherPanels();
}

void Scope::StopScope(){
  DebugPrint("%s", "Scope");
  if( !digi ) return;

  scopeTimer->stop();
  for( unsigned int i = 0; i < nDigi; i++ ) if( boardRuns[i] ) StopBoard(i);

  runStatus->setStyleSheet("");
  bnScopeStart->setEnabled(true);
  bnScopeStart->setStyleSheet("background-color: green;");
  bnScopeStop->setEnabled(false);
  bnScopeStop->setStyleSheet("");
  EnableControl(true);

  isACQStarted = false;
  emit TellACQOnOff(false);
  emit UpdateOtherPanels();
}

void Scope::EnableControl(bool enable){
  // the parameters stay: they are applied while running too
  chkSoleRun->setEnabled(enable);
  bnReadSettingsFromBoard->setEnabled(enable);
}

//*=======================================================
//*======================================================= the plot

void Scope::UpdateScope(){
  DebugPrint("%s", "Scope");
  if( !digi ) return;

  const int ch = cbScopeCh->currentIndex();

  uint32_t acqStatus = digi[ID]->GetACQStatusFromMemory();
  runStatus->setStyleSheet( IsRunning(ID) && ((acqStatus >> 2) & 0x1) ? "background-color : green;" : "");

  // The readout thread copies the newest trace of each channel after every decode
  // (ReadDataThread::CopyScopeTraces); draw from that copy.
  ReadDataThread::ScopeTrace trace;
  const bool valid = IsRunning(ID) && readDataThread[ID]->GetScopeTrace(ch, trace);
  const bool fresh = valid && std::chrono::steady_clock::now() - trace.when < std::chrono::milliseconds(ScopeTraceStaleMiliSec);

  if( !fresh || trace.trigRate == 0 ){
    leTriggerRate->setStyleSheet("font-weight : bold; color : red;");
    leTriggerRate->setText("No Trigger");
  }else{
    leTriggerRate->setStyleSheet("");
    leTriggerRate->setText(QString::number(trace.trigRate));
  }

  if( refreshPaused || !valid ) return;
  if( haveLastTrace && trace.absIndex == lastTrace.absIndex ) return; // nothing new: the last one stays, as in CoMPASS

  lastTrace = trace;
  haveLastTrace = true;
  DrawTrace(trace, true);

  if( singleArmed ){
    singleArmed = false;
    SetRefreshPaused(true);
  }
}

double Scope::YScale() const{
  if( cbYUnit->currentData().toInt() != 1 || digi[ID]->GetDPPType() == DPPTypeCode::DPP_QDC_CODE ) return 1.0;
  const double vpp = digi[ID]->GetSettingFromMemory(DPP::InputDynamicRange, cbScopeCh->currentIndex()) == 1 ? 0.5 : 2.0;
  return vpp * 1000. / 16384.;
}

void Scope::DrawTrace(const ReadDataThread::ScopeTrace & trace, bool isNew){
  if( plot->animationOptions() != QChart::NoAnimation ) plot->setAnimationOptions(QChart::NoAnimation); // the chart view switches it on after a zoom

  const int dpp = digi[ID]->GetDPPType();
  const int ch = cbScopeCh->currentIndex();
  const double ys = YScale();

  // sample spacing: dual trace interleaves the two analog traces (half rate each); the DPP-PHA digital
  // probes go with them, the DPP-PSD ones stay at the full rate
  const bool dual = !trace.wf2.empty() && dpp != DPPTypeCode::DPP_QDC_CODE;
  double aStep = tick2ns * (dual ? 2 : 1);
  double dStep = dpp == DPPTypeCode::DPP_PSD_CODE ? tick2ns : aStep;
  if( dpp == DPPTypeCode::DPP_QDC_CODE ) aStep = dStep = tick2ns;
  const double xLimit = MaxDisplayTraceTimeLength;

  // persistence: the waveform on the plot joins the faded ones
  const int nPersist = cbPersist->currentData().toInt();
  if( isNew && nPersist > 0 && dataTrace[0]->count() > 0 ){
    persistTrace[persistNext]->replace(dataTrace[0]->points());
    persistTrace[persistNext]->setVisible(true);
    for( QLegendMarker * m : plot->legend()->markers(persistTrace[persistNext]) ) m->setVisible(false); // shown again with the series
    persistNext = (persistNext + 1) % nPersist;
  }

  double yMin = 1e9, yMax = -1e9, xMax = 0;
  auto analog = [&](const std::vector<short> & w){
    QVector<QPointF> v;
    for( size_t i = 0; i < w.size() && i * aStep <= xLimit; i++ ){
      const double y = w[i] * ys;
      v.append(QPointF(i * aStep, y));
      yMin = std::min(yMin, y); yMax = std::max(yMax, y); xMax = std::max(xMax, i * aStep);
    }
    return v;
  };
  QVector<QPointF> a1 = analog(trace.wf1);
  QVector<QPointF> a2 = analog(trace.wf2);
  if( yMin > yMax ){ yMin = 0; yMax = 1; }
  for( int i = 0; i < MaxPersistTrace; i++ ){   // the range holds the faded waveforms too
    if( !persistTrace[i]->isVisible() ) continue;
    for( const QPointF & p : persistTrace[i]->points() ){ yMin = std::min(yMin, p.y()); yMax = std::max(yMax, p.y()); }
  }
  dataXMax = std::max(xMax, 10.0);

  // the threshold (DPP-PSD, analog trace 1 = Input): over the baseline, on the side of the polarity
  thresholdLine->clear();
  if( dpp == DPPTypeCode::DPP_PSD_CODE && cbAnaTrace1->currentData().toInt() == 0 && !trace.wf1.empty() ){
    const uint32_t alg = digi[ID]->GetSettingFromMemory(DPP::DPPAlgorithmControl, ch);
    const bool negative = Digitizer::ExtractBits(alg, DPP::Bit_DPPAlgorithmControl_PSD::Polarity);
    const int preSamples = digi[ID]->GetSettingFromMemory(DPP::PreTrigger, ch) * DPP::PreTrigger.GetPartialStep() / (dual ? 2 : 1);
    double base = 0;
    if( cbAnaTrace2->currentData().toInt() == 1 && !trace.wf2.empty() ){
      base = trace.wf2[std::min((size_t) std::max(preSamples, 0), trace.wf2.size() - 1)];
    }else if( Digitizer::ExtractBits(alg, DPP::Bit_DPPAlgorithmControl_PSD::BaselineAvg) == 0 ){
      base = digi[ID]->GetSettingFromMemory(DPP::PSD::FixedBaseline, ch);
    }else{
      const size_t n = std::min(trace.wf1.size(), (size_t) std::max(4, preSamples / 2));
      for( size_t i = 0; i < n; i++ ) base += trace.wf1[i];
      base /= n;
    }
    const double thr = digi[ID]->GetSettingFromMemory(DPP::PSD::TriggerThreshold, ch);
    const double level = (base + (negative ? -thr : thr)) * ys;
    thresholdLine->append(0, level);
    thresholdLine->append(dataXMax, level);
    yMin = std::min(yMin, level); yMax = std::max(yMax, level);
  }
  thresholdLine->setVisible(thresholdLine->count() > 0);

  // the y range of the analog part: the waveform, or the whole ADC range
  double fullLow = 0, fullHigh = 0x3FFF;
  if( dpp == DPPTypeCode::DPP_PHA_CODE ){ fullLow = -0x1FFF; fullHigh = 0x1FFF; }
  if( dpp == DPPTypeCode::DPP_QDC_CODE ) fullHigh = 0xFFF;
  double low, high;
  if( chkAutoY->isChecked() ){
    const double span = std::max(yMax - yMin, 20 * ys);
    low = yMin - 0.05 * span;
    high = yMax + 0.05 * span;
  }else{
    low = fullLow * ys;
    high = fullHigh * ys;
  }

  // the digital traces: logic lanes under the analog part
  const std::vector<bool> * dw[4] = {&trace.dwf1, &trace.dwf2, &trace.dwf3, &trace.dwf4};
  const int nDigital = dpp == DPPTypeCode::DPP_QDC_CODE ? 4 : 2;
  const double lane = 0.07 * (high - low), gap = 0.03 * (high - low);
  QVector<QPointF> d[4];
  for( int k = 0; k < nDigital; k++ ){
    const double base = low - (k + 1) * (lane + gap);
    for( size_t i = 0; i < dw[k]->size() && i * dStep <= xLimit; i++ ) d[k].append(QPointF(i * dStep, base + ((*dw[k])[i] ? lane : 0)));
  }
  dataYMin = low - nDigital * (lane + gap) - gap;
  dataYMax = high;

  // the gates, shaded over the analog part, from the digital traces that show them
  // (DPP-PSD: Long gate on digital trace 1, Short gate on digital trace 2; DPP-QDC: the gate)
  for( int g = 0; g < 2; g++ ){
    gateUpper[g]->clear();
    gateLower[g]->clear();
    const std::vector<bool> * w = nullptr;
    if( dpp == DPPTypeCode::DPP_PSD_CODE && g == 0 && cbDigiTrace1->currentData().toInt() == 0 ) w = &trace.dwf1;
    if( dpp == DPPTypeCode::DPP_PSD_CODE && g == 1 && cbDigiTrace2->currentData().toInt() == 0 ) w = &trace.dwf2;
    if( dpp == DPPTypeCode::DPP_QDC_CODE && g == 0 ) w = &trace.dwf1;
    if( !w ) continue;
    int first = -1, last = -1;
    for( size_t i = 0; i < w->size(); i++ ) if( (*w)[i] ){ if( first < 0 ) first = i; last = i; }
    if( first < 0 ) continue;
    const double x0 = first * dStep, x1 = std::min((last + 1) * dStep, xLimit);
    gateUpper[g]->append(x0, high); gateUpper[g]->append(x1, high);
    gateLower[g]->append(x0, low);  gateLower[g]->append(x1, low);
  }

  dataTrace[0]->replace(a1);
  dataTrace[1]->replace(a2);
  if( dpp == DPPTypeCode::DPP_QDC_CODE ){
    for( int k = 0; k < 4; k++ ) dataTrace[1 + k]->replace(d[k]);
  }else{
    dataTrace[2]->replace(d[0]);
    dataTrace[3]->replace(d[1]);
    dataTrace[4]->clear();
  }

  yAxis->setTitleText(ys == 1.0 ? "ADC [lsb]" : "Amplitude [mV] (not calibrated)");
  yAxis->setLabelFormat(ys == 1.0 ? "%.0f" : "%.1f");
  if( !plot->isZoomed() ) ApplyAutoRange();
}

bool Scope::eventFilter(QObject * watched, QEvent * event){
  if( watched == plotView->viewport() && event->type() == QEvent::MouseButtonDblClick ){
    plot->zoomReset();
    ApplyAutoRange();
    return true;
  }
  return QMainWindow::eventFilter(watched, event);
}

void Scope::ApplyAutoRange(){
  // ticks at round values (1, 2 or 5 x 10^n), about ten over the range
  auto roundTicks = [](QValueAxis * axis, double low, double high){
    const double raw = (high - low) / 10;
    const double p = std::pow(10, std::floor(std::log10(raw > 0 ? raw : 1)));
    const double step = raw <= p ? p : raw <= 2 * p ? 2 * p : raw <= 5 * p ? 5 * p : 10 * p;
    axis->setTickType(QValueAxis::TicksDynamic);
    axis->setTickAnchor(0);
    axis->setTickInterval(step);
    axis->setRange(low, high);
  };
  roundTicks(xAxis, 0, dataXMax);
  roundTicks(yAxis, dataYMin, dataYMax);
}

void Scope::ClearTraces(){
  for( int i = 0; i < MaxNumberOfTrace; i++){
    if( dataTrace[i] && dataTrace[i]->count() > 0 ) dataTrace[i]->clear();
  }
  thresholdLine->clear();
  for( int g = 0; g < 2; g++ ){ gateUpper[g]->clear(); gateLower[g]->clear(); }
  haveLastTrace = false;
  lastTrace = ReadDataThread::ScopeTrace();
}

void Scope::ClearPersistence(){
  for( int i = 0; i < MaxPersistTrace; i++ ){
    persistTrace[i]->clear();
    persistTrace[i]->setVisible(false);
  }
  persistNext = 0;
}

void Scope::SetRefreshPaused(bool paused){
  refreshPaused = paused;
  bnRefresh->setText(paused ? "Start" : "Stop");
  bnRefresh->setIcon(style()->standardIcon(paused ? QStyle::SP_MediaPlay : QStyle::SP_MediaPause));
}

void Scope::SaveSnapshot(){
  if( saveDir.isEmpty() ) saveDir = QDir::homePath();
  const QString name = QString("Digi-%1_ch%2_%3.png").arg(digi[ID]->GetSerialNumber()).arg(cbScopeCh->currentIndex()).arg(QDateTime::currentDateTime().toString("yyyyMMdd_HHmmss"));
  const QString file = QFileDialog::getSaveFileName(this, "Save the plot", saveDir + "/" + name, "PNG picture (*.png)");
  if( file.isEmpty() ) return;
  saveDir = QFileInfo(file).absolutePath();
  if( plotView->grab().save(file, "PNG") ) SendLogMsg("Scope: plot saved to " + file + ".");
  else SendLogMsg("<font style=\"color: red;\">Scope: could not save " + file + ".</font>");
}

void Scope::SaveTraceCSV(){
  if( !haveLastTrace ){ SendLogMsg("Scope: no waveform on the plot to save."); return; }
  if( saveDir.isEmpty() ) saveDir = QDir::homePath();
  const QString name = QString("Digi-%1_ch%2_%3.csv").arg(digi[ID]->GetSerialNumber()).arg(cbScopeCh->currentIndex()).arg(QDateTime::currentDateTime().toString("yyyyMMdd_HHmmss"));
  const QString fileName = QFileDialog::getSaveFileName(this, "Save the waveform", saveDir + "/" + name, "CSV table (*.csv)");
  if( fileName.isEmpty() ) return;
  saveDir = QFileInfo(fileName).absolutePath();

  QFile file(fileName);
  if( !file.open(QIODevice::WriteOnly | QIODevice::Text) ){ SendLogMsg("<font style=\"color: red;\">Scope: could not write " + fileName + ".</font>"); return; }
  QTextStream out(&file);

  const ReadDataThread::ScopeTrace & t = lastTrace;
  const int dpp = digi[ID]->GetDPPType();
  const bool dual = !t.wf2.empty() && dpp != DPPTypeCode::DPP_QDC_CODE;
  const int aEvery = (dpp == DPPTypeCode::DPP_PSD_CODE && dual) ? 2 : 1;  // analog samples come every aEvery rows
  const double rowStep = dpp == DPPTypeCode::DPP_PHA_CODE ? tick2ns * (dual ? 2 : 1) : tick2ns;
  const std::vector<bool> * dw[4] = {&t.dwf1, &t.dwf2, &t.dwf3, &t.dwf4};
  const int nDigital = dpp == DPPTypeCode::DPP_QDC_CODE ? 4 : 2;

  out << "# Digi-" << digi[ID]->GetSerialNumber() << " (" << QString::fromStdString(digi[ID]->GetModelName()) << ") ch " << cbScopeCh->currentIndex()
      << ", saved " << QDateTime::currentDateTime().toString(Qt::ISODate) << ", analog in lsb\n";
  out << "time_ns," << TraceName(0) << "," << TraceName(1);
  for( int k = 0; k < nDigital; k++ ) out << "," << TraceName(2 + k);
  out << "\n";
  size_t rows = std::max(t.wf1.size(), t.wf2.size()) * aEvery;
  for( int k = 0; k < nDigital; k++ ) rows = std::max(rows, dw[k]->size());
  for( size_t r = 0; r < rows; r++ ){
    out << r * rowStep;
    const bool analogRow = r % aEvery == 0;
    const size_t a = r / aEvery;
    out << "," << (analogRow && a < t.wf1.size() ? QString::number(t.wf1[a]) : "");
    out << "," << (analogRow && a < t.wf2.size() ? QString::number(t.wf2[a]) : "");
    for( int k = 0; k < nDigital; k++ ) out << "," << (r < dw[k]->size() ? QString::number((int)(*dw[k])[r]) : "");
    out << "\n";
  }
  SendLogMsg("Scope: waveform saved to " + fileName + ".");
}

QString Scope::TraceName(int i) const{
  QString n = dataTrace[i]->name();
  n.replace(",", " ");
  return n.isEmpty() ? "trace" + QString::number(i) : n;
}

//*=======================================================
//*======================================================= the parameters

int Scope::RegCh(int ch) const{
  return digi[ID]->GetDPPType() == DPPTypeCode::DPP_QDC_CODE ? ch / 8 : ch;
}

QWidget * Scope::NewTab(const QString & name, QGridLayout * & grid){
  QWidget * page = new QWidget(tabs);
  grid = new QGridLayout(page);
  grid->setColumnStretch(1, 1);
  grid->setRowStretch(100, 1);   // the controls at the top
  tabs->addTab(page, name);
  return page;
}

void Scope::AddSpin(QGridLayout * grid, const QString & label, Param::Kind kind, const Reg & reg, Bits bits){
  const int row = grid->count() / 2;
  QLabel * lb = new QLabel(label, grid->parentWidget());
  lb->setAlignment(Qt::AlignRight | Qt::AlignVCenter);
  grid->addWidget(lb, row, 0);

  Param p(kind, reg, bits);
  p.sb = new RSpinBox(grid->parentWidget());
  switch( kind ){
    case Param::Time: {
      const double unit = bits.first > 0 ? tick2ns : std::abs(reg.GetPartialStep()) * tick2ns;
      const double maxRaw = bits.first > 0 ? (1u << bits.first) - 1 : reg.GetMaxBit();
      p.sb->setMinimum(0);
      p.sb->setMaximum(maxRaw * unit);
      p.sb->setSingleStep(unit);
    } break;
    case Param::Raw:
      p.sb->setMinimum(0);
      p.sb->setMaximum(reg.GetMaxBit() > 0 ? reg.GetMaxBit() : 0xFFFF);
      p.sb->setSingleStep(1);
      break;
    case Param::DCOffset:
      p.sb->setDecimals(2);
      p.sb->setMinimum(0);
      p.sb->setMaximum(100);
      p.sb->setSingleStep(1);
      break;
    case Param::Combo: break;
  }
  p.sb->SetToolTip();
  grid->addWidget(p.sb, row, 1);

  params.push_back(p);
  const size_t index = params.size() - 1;
  RSpinBox * sb = p.sb;
  connect(sb, &RSpinBox::valueChanged, this, [=](){ if( enableSignalSlot ) sb->setStyleSheet("color:blue"); });
  connect(sb, &RSpinBox::returnPressed, this, [=](){ if( enableSignalSlot ) WriteParam(params[index]); });
}

void Scope::AddCombo(QGridLayout * grid, const QString & label, const Reg & reg, Bits bits, const std::vector<std::pair<std::string, unsigned int>> & list){
  const int row = grid->count() / 2;
  QLabel * lb = new QLabel(label, grid->parentWidget());
  lb->setAlignment(Qt::AlignRight | Qt::AlignVCenter);
  grid->addWidget(lb, row, 0);

  Param p(Param::Combo, reg, bits);
  p.cb = new RComboBox(grid->parentWidget());
  Fill(p.cb, list.empty() ? reg.GetComboList() : list);
  grid->addWidget(p.cb, row, 1);

  params.push_back(p);
  const size_t index = params.size() - 1;
  connect(p.cb, &RComboBox::currentIndexChanged, this, [=](){ if( enableSignalSlot ) WriteParam(params[index]); });
}

void Scope::WriteParam(Param & p){
  const int ch = cbScopeCh->currentIndex();
  const int rch = RegCh(ch);
  const Reg reg = RegOf(p, ch);
  const int wch = reg.GetAddress() >= 0x8000 ? -1 : rch;

  uint32_t value = 0;
  QString shown;
  switch( p.kind ){
    case Param::Time: {
      const double step = p.sb->singleStep();
      if( step > 1 ) p.sb->setValue(std::round(p.sb->value() / step) * step);
      const double unit = p.bits.first > 0 ? 1 : std::abs(reg.GetPartialStep());
      value = (uint32_t) std::lround(p.sb->value() / tick2ns / unit);
      shown = QString::number(p.sb->value()) + " ns";
    } break;
    case Param::Raw:
      value = (uint32_t) std::lround(p.sb->value());
      shown = QString::number(value);
      break;
    case Param::DCOffset:
      value = uint16_t((1.0 - p.sb->value() / 100.) * 0xFFFF);
      shown = QString::number(p.sb->value()) + " %";
      break;
    case Param::Combo:
      if( p.cb->currentIndex() < 0 ) return;
      value = p.cb->currentData().toUInt();
      shown = p.cb->currentText();
      break;
  }

  uint32_t full = value;
  if( p.bits.first > 0 ) full = SetField(digi[ID]->GetSettingFromMemory(reg, wch < 0 ? 0 : wch), p.bits, value);

  bool ok = false;
  ApplyChange([&](){
    digiMTX[ID].lock();
    digi[ID]->WriteRegister(reg, full, wch);
    ok = digi[ID]->GetErrorCode() == CAEN_DGTZ_Success;
    digiMTX[ID].unlock();
  });

  QString msg = QString::fromStdString(reg.GetName()) + "|DIG:" + QString::number(digi[ID]->GetSerialNumber()) + ",CH:" + QString::number(ch)
              + " = " + shown + " | 0x" + QString::number(full, 16) + (IsRunning(ID) ? " (board restarted)" : "");
  QWidget * w = p.sb ? (QWidget *) p.sb : (QWidget *) p.cb;
  if( ok ){
    SendLogMsg(msg + " | OK.");
    w->setStyleSheet("");
    UpdatePanelFromMomeory();   // the values that follow from this one (fixed baseline, the coarse gain's list)
    emit UpdateOtherPanels();
  }else{
    SendLogMsg(msg + " | Fail.");
    w->setStyleSheet("color:red;");
  }
}

void Scope::ShowParam(Param & p){
  const int ch = cbScopeCh->currentIndex();
  const Reg reg = RegOf(p, ch);
  const int rch = reg.GetAddress() >= 0x8000 ? 0 : RegCh(ch);
  uint32_t v = digi[ID]->GetSettingFromMemory(reg, rch);
  if( p.bits.first > 0 ) v = Digitizer::ExtractBits(v, p.bits);

  switch( p.kind ){
    case Param::Time: {
      const double unit = p.bits.first > 0 ? 1 : std::abs(reg.GetPartialStep());
      p.sb->setValue(v * unit * tick2ns);
    } break;
    case Param::Raw: p.sb->setValue(v); break;
    case Param::DCOffset: p.sb->setValue((1.0 - v * 1.0 / 0xFFFF) * 100); break;
    case Param::Combo: SelectData(p.cb, v); break;
  }
  if( p.sb ) p.sb->setStyleSheet("");
  if( p.cb ) p.cb->setStyleSheet("");
}

void Scope::BuildPanels(){
  const bool saved = enableSignalSlot;
  enableSignalSlot = false;

  params.clear();
  cbBaselineAvg = nullptr;
  sbFixedBaseline = nullptr;
  cbChargeSens = nullptr;
  while( tabs->count() > 0 ){
    QWidget * page = tabs->widget(0);
    tabs->removeTab(0);
    delete page;
  }

  const int dpp = digi[ID]->GetDPPType();
  if( dpp == DPPTypeCode::DPP_PHA_CODE ) SetUpTabs_PHA();
  if( dpp == DPPTypeCode::DPP_PSD_CODE ) SetUpTabs_PSD();
  if( dpp == DPPTypeCode::DPP_QDC_CODE ) SetUpTabs_QDC();
  BuildTraceSelectors();
  cbYUnit->setEnabled(dpp != DPPTypeCode::DPP_QDC_CODE);

  enableSignalSlot = saved;
  UpdatePanelFromMomeory();
}

void Scope::SetUpTabs_PSD(){
  using namespace DPP;
  QGridLayout * g;

  NewTab("Input", g);
  AddSpin (g, "Record length [ns]", Param::Time, RecordLength_G);
  AddSpin (g, "Pre-trigger [ns]", Param::Time, PreTrigger);
  AddCombo(g, "Polarity", DPPAlgorithmControl, Bit_DPPAlgorithmControl_PSD::Polarity, Bit_DPPAlgorithmControl_PSD::ListPolarity);
  AddCombo(g, "N samples baseline", DPPAlgorithmControl, Bit_DPPAlgorithmControl_PSD::BaselineAvg, Bit_DPPAlgorithmControl_PSD::ListBaselineAvg);
  cbBaselineAvg = params.back().cb;
  AddSpin (g, "Fixed baseline [lsb]", Param::Raw, PSD::FixedBaseline);
  sbFixedBaseline = params.back().sb;
  AddSpin (g, "DC offset [%]", Param::DCOffset, DPP::ChannelDCOffset);
  AddCombo(g, "Input dynamic", DPP::InputDynamicRange, {0, 0}, {});

  NewTab("Discriminator", g);
  AddCombo(g, "Discriminator mode", DPPAlgorithmControl, Bit_DPPAlgorithmControl_PSD::DiscriminationMode, {{"Leading edge", 0}, {"CFD", 1}});
  AddSpin (g, "CFD delay [ns]", Param::Time, PSD::CFDSetting, PSD::Bit_CFDSetting::CFDDealy);
  AddCombo(g, "CFD fraction", PSD::CFDSetting, PSD::Bit_CFDSetting::CFDFraction, PSD::Bit_CFDSetting::ListCFDFraction);
  AddCombo(g, "Input smoothing", PSD::DPPAlgorithmControl2_G, PSD::Bit_DPPAlgorithmControl2::InputSmoothing, PSD::Bit_DPPAlgorithmControl2::ListInputSmoothing);
  AddSpin (g, "Threshold [lsb]", Param::Raw, PSD::TriggerThreshold);
  AddSpin (g, "Trigger holdoff [ns]", Param::Time, PSD::TriggerHoldOffWidth);

  NewTab("QDC", g);
  AddCombo(g, "Energy coarse gain", DPPAlgorithmControl, Bit_DPPAlgorithmControl_PSD::ChargeSensitivity, Bit_DPPAlgorithmControl_PSD::ListChargeSensitivity_2Vpp);
  cbChargeSens = params.back().cb;
  AddSpin (g, "Gate [ns]", Param::Time, PSD::LongGateWidth);
  AddSpin (g, "Short gate [ns]", Param::Time, PSD::ShortGateWidth);
  AddSpin (g, "Pre-gate [ns]", Param::Time, PSD::GateOffset);
}

void Scope::SetUpTabs_PHA(){
  using namespace DPP;
  QGridLayout * g;

  NewTab("Input", g);
  AddSpin (g, "Record length [ns]", Param::Time, RecordLength_G);
  AddSpin (g, "Pre-trigger [ns]", Param::Time, PreTrigger);
  AddCombo(g, "Polarity", DPPAlgorithmControl, Bit_DPPAlgorithmControl_PHA::Polarity, {{"Positive", 0}, {"Negative", 1}});
  AddCombo(g, "N samples baseline", DPPAlgorithmControl, Bit_DPPAlgorithmControl_PHA::BaselineAvg, Bit_DPPAlgorithmControl_PHA::ListBaselineAvg);
  AddSpin (g, "DC offset [%]", Param::DCOffset, DPP::ChannelDCOffset);
  AddCombo(g, "Input dynamic", DPP::InputDynamicRange, {0, 0}, {});

  NewTab("Discriminator", g);
  AddSpin (g, "Threshold [lsb]", Param::Raw, PHA::TriggerThreshold);
  AddSpin (g, "Input rise time [ns]", Param::Time, PHA::InputRiseTime);
  AddSpin (g, "Trigger holdoff [ns]", Param::Time, PHA::TriggerHoldOffWidth);
  AddCombo(g, "Fast discr. smoothing", PHA::RCCR2SmoothingFactor, {0, 0}, {});

  NewTab("Trapezoid", g);
  AddSpin (g, "Trap. rise time [ns]", Param::Time, PHA::TrapezoidRiseTime);
  AddSpin (g, "Trap. flat top [ns]", Param::Time, PHA::TrapezoidFlatTop);
  AddSpin (g, "Decay time [ns]", Param::Time, PHA::DecayTime);
  AddSpin (g, "Peaking time [ns]", Param::Time, PHA::PeakingTime);
  AddCombo(g, "N samples peak", DPPAlgorithmControl, Bit_DPPAlgorithmControl_PHA::PeakMean, {{"1 sample", 0}, {"4 samples", 1}, {"16 samples", 2}, {"64 samples", 3}});
  AddSpin (g, "Peak holdoff [ns]", Param::Time, PHA::PeakHoldOff);
}

void Scope::SetUpTabs_QDC(){
  using namespace DPP;
  QGridLayout * g;

  NewTab("Input", g);
  AddSpin (g, "Record length [ns]", Param::Time, QDC::RecordLength_W);
  AddSpin (g, "Pre-trigger [ns]", Param::Time, QDC::PreTrigger);
  AddCombo(g, "Polarity", QDC::DPPAlgorithmControl, QDC::Bit_DPPAlgorithmControl::Polarity, {{"Positive", 0}, {"Negative", 1}});
  AddSpin (g, "DC offset [%]", Param::DCOffset, QDC::DCOffset);

  NewTab("Discriminator", g);
  AddSpin (g, "Threshold [lsb]", Param::Raw, QDC::TriggerThreshold_sub0);
  params.back().regOf = [](int ch) -> Reg {
    switch( ch % 8 ){
      case 1: return QDC::TriggerThreshold_sub1;
      case 2: return QDC::TriggerThreshold_sub2;
      case 3: return QDC::TriggerThreshold_sub3;
      case 4: return QDC::TriggerThreshold_sub4;
      case 5: return QDC::TriggerThreshold_sub5;
      case 6: return QDC::TriggerThreshold_sub6;
      case 7: return QDC::TriggerThreshold_sub7;
    }
    return QDC::TriggerThreshold_sub0;
  };
  params.back().sb->setMaximum(0xFFF);

  NewTab("QDC", g);
  AddSpin (g, "Gate [ns]", Param::Time, QDC::GateWidth);
  AddSpin (g, "Pre-gate [ns]", Param::Time, QDC::GateOffset);
}

//*======================================================= the trace selectors

void Scope::BuildTraceSelectors(){
  using namespace DPP::Bit_BoardConfig;
  const int dpp = digi[ID]->GetDPPType();
  RComboBox * all[4] = {cbAnaTrace1, cbAnaTrace2, cbDigiTrace1, cbDigiTrace2};
  for( RComboBox * cb : all ){ cb->clear(); cb->setEnabled(true); }

  if( dpp == DPPTypeCode::DPP_PSD_CODE ){
    Fill(cbAnaTrace1, {{"Input", 0}, {"CFD / smoothed input", 1}});
    Fill(cbAnaTrace2, {{"None", 0}, {"Baseline", 1}, {"CFD / smoothed input", 2}});
    Fill(cbDigiTrace1, ListDigiProbe1_PSD);
    Fill(cbDigiTrace2, ListDigiProbe2_PSD);
  }
  if( dpp == DPPTypeCode::DPP_PHA_CODE ){
    Fill(cbAnaTrace1, ListAnaProbe1_PHA);
    cbAnaTrace2->addItem("None", PHANoSecondTrace);
    for( auto & x : ListAnaProbe2_PHA ) cbAnaTrace2->addItem(QString::fromStdString(x.first), x.second);
    Fill(cbDigiTrace1, ListDigiProbe1_PHA);
    Fill(cbDigiTrace2, ListDigiProbe2_PHA);
    cbDigiTrace2->setEnabled(false);
  }
  if( dpp == DPPTypeCode::DPP_QDC_CODE ){
    Fill(cbAnaTrace1, ListAnaProbe_QDC);
    cbAnaTrace2->addItem("N/A", 0);
    cbDigiTrace1->addItem("Gate, Trigger", 0);
    cbDigiTrace2->addItem("Trigger holdoff, Over threshold", 0);
    cbAnaTrace2->setEnabled(false);
    cbDigiTrace1->setEnabled(false);
    cbDigiTrace2->setEnabled(false);
  }
}

void Scope::ShowTraceSelection(){
  using namespace DPP::Bit_BoardConfig;
  const int dpp = digi[ID]->GetDPPType();
  const uint32_t bd = digi[ID]->GetSettingFromMemory(DPP::BoardConfiguration);

  if( dpp == DPPTypeCode::DPP_PSD_CODE ){
    int a1, a2;
    PSDTraces(Digitizer::ExtractBits(bd, AnalogProbe_PSD), a1, a2);
    SelectData(cbAnaTrace1, a1);
    SelectData(cbAnaTrace2, a2);
    SelectData(cbDigiTrace1, Digitizer::ExtractBits(bd, DigiProbel1_PSD));
    SelectData(cbDigiTrace2, Digitizer::ExtractBits(bd, DigiProbel2_PSD));
  }
  if( dpp == DPPTypeCode::DPP_PHA_CODE ){
    SelectData(cbAnaTrace1, Digitizer::ExtractBits(bd, AnalogProbe1));
    SelectData(cbAnaTrace2, Digitizer::ExtractBits(bd, DualTrace) ? (int) Digitizer::ExtractBits(bd, AnalogProbe2) : PHANoSecondTrace);
    SelectData(cbDigiTrace1, Digitizer::ExtractBits(bd, DigiProbel1_PHA));
    cbDigiTrace2->setCurrentIndex(0);
  }
  if( dpp == DPPTypeCode::DPP_QDC_CODE ){
    SelectData(cbAnaTrace1, Digitizer::ExtractBits(bd, AnalogProbe1));
  }

  // the legend
  if( dpp == DPPTypeCode::DPP_QDC_CODE ){
    dataTrace[0]->setName(cbAnaTrace1->currentText());
    const char * names[4] = {"Gate", "Trigger", "Trigger holdoff", "Over threshold"};
    for( int k = 0; k < 4; k++ ){ dataTrace[1 + k]->setName(names[k]); dataTrace[1 + k]->setVisible(true); }
  }else{
    dataTrace[0]->setName(cbAnaTrace1->currentText());
    dataTrace[1]->setName(cbAnaTrace2->currentText());
    dataTrace[1]->setVisible(cbAnaTrace2->currentText() != "None");
    dataTrace[2]->setName(cbDigiTrace1->currentText());
    dataTrace[3]->setName(cbDigiTrace2->currentText());
    dataTrace[4]->setVisible(false);
  }
  gateArea[0]->setName(dpp == DPPTypeCode::DPP_QDC_CODE ? "Gate" : "Long gate");
  gateArea[1]->setName("Short gate");
}

void Scope::WriteTraceSelection(QObject * from){
  using namespace DPP::Bit_BoardConfig;
  const int dpp = digi[ID]->GetDPPType();
  const uint32_t now = digi[ID]->GetSettingFromMemory(DPP::BoardConfiguration);
  uint32_t bd = now;

  if( dpp == DPPTypeCode::DPP_PSD_CODE ){
    int a1 = cbAnaTrace1->currentData().toInt(), a2 = cbAnaTrace2->currentData().toInt();
    if( a1 == 1 && a2 == 2 ){   // the firmware has no "CFD + CFD": the other trace gives way
      enableSignalSlot = false;
      if( from == cbAnaTrace1 ){ a2 = 0; SelectData(cbAnaTrace2, 0); } else { a1 = 0; SelectData(cbAnaTrace1, 0); }
      enableSignalSlot = true;
    }
    bd = SetField(bd, AnalogProbe_PSD, PSDProbeField(a1, a2));
    bd = SetField(bd, DigiProbel1_PSD, cbDigiTrace1->currentData().toUInt());
    bd = SetField(bd, DigiProbel2_PSD, cbDigiTrace2->currentData().toUInt());
  }
  if( dpp == DPPTypeCode::DPP_PHA_CODE ){
    bd = SetField(bd, AnalogProbe1, cbAnaTrace1->currentData().toUInt());
    const int a2 = cbAnaTrace2->currentData().toInt();
    bd = SetField(bd, DualTrace, a2 == PHANoSecondTrace ? 0 : 1);
    if( a2 != PHANoSecondTrace ) bd = SetField(bd, AnalogProbe2, a2);
    bd = SetField(bd, DigiProbel1_PHA, cbDigiTrace1->currentData().toUInt());
  }
  if( dpp == DPPTypeCode::DPP_QDC_CODE ){
    bd = SetField(bd, AnalogProbe1, cbAnaTrace1->currentData().toUInt());
  }
  if( bd == now ){ ShowTraceSelection(); return; }

  bool ok = false;
  ApplyChange([&](){
    digiMTX[ID].lock();
    digi[ID]->WriteRegister(DPP::BoardConfiguration, bd, -1);
    ok = digi[ID]->GetErrorCode() == CAEN_DGTZ_Success;
    digiMTX[ID].unlock();
  });
  SendLogMsg("BoardConfiguration|DIG:" + QString::number(digi[ID]->GetSerialNumber()) + " traces: " + cbAnaTrace1->currentText() + ", "
             + cbAnaTrace2->currentText() + ", " + cbDigiTrace1->currentText() + ", " + cbDigiTrace2->currentText()
             + " | 0x" + QString::number(bd, 16) + (ok ? " | OK." : " | Fail."));

  enableSignalSlot = false;
  ShowTraceSelection();
  enableSignalSlot = true;
  ClearTraces();
  ClearPersistence();
  if( ok ) emit UpdateOtherPanels();
}

//*=======================================================
//*=======================================================

void Scope::UpdatePanelFromMomeory(){
  const bool saved = enableSignalSlot;
  enableSignalSlot = false;

  const int ch = cbScopeCh->currentIndex();

  // DPP-PSD: the coarse gain's choices follow the input range
  if( cbChargeSens ){
    const bool half = digi[ID]->GetSettingFromMemory(DPP::InputDynamicRange, ch) == 1;
    Fill(cbChargeSens, half ? DPP::Bit_DPPAlgorithmControl_PSD::ListChargeSensitivity_p5Vpp : DPP::Bit_DPPAlgorithmControl_PSD::ListChargeSensitivity_2Vpp);
  }

  for( Param & p : params ) ShowParam(p);
  if( sbFixedBaseline && cbBaselineAvg ) sbFixedBaseline->setEnabled(cbBaselineAvg->currentData().toInt() == 0);
  ShowTraceSelection();

  tabs->setEnabled(digi[ID]->GetInputChannelOnOff(ch));

  enableSignalSlot = saved;
}

void Scope::ReadSettingsFromBoard(){
  DebugPrint("%s", "Scope");
  if( IsRunning(ID) ){ UpdatePanelFromMomeory(); return; } // a running board answers no reads; the memory has its settings

  const int ch = cbScopeCh->currentIndex();
  digi[ID]->ReadRegister(DPP::BoardConfiguration);

  if( digi[ID]->GetDPPType() == V1730_DPP_PHA_CODE ){
    const Reg regs[] = {DPP::RecordLength_G, DPP::PreTrigger, DPP::ChannelDCOffset, DPP::InputDynamicRange, DPP::DPPAlgorithmControl,
                        DPP::PHA::InputRiseTime, DPP::PHA::TriggerThreshold, DPP::PHA::TriggerHoldOffWidth, DPP::PHA::RCCR2SmoothingFactor,
                        DPP::PHA::TrapezoidRiseTime, DPP::PHA::TrapezoidFlatTop, DPP::PHA::DecayTime, DPP::PHA::PeakingTime, DPP::PHA::PeakHoldOff};
    for( const Reg & r : regs ) digi[ID]->ReadRegister(r, ch);
  }

  if( digi[ID]->GetDPPType() == V1730_DPP_PSD_CODE ){
    const Reg regs[] = {DPP::RecordLength_G, DPP::PreTrigger, DPP::ChannelDCOffset, DPP::InputDynamicRange, DPP::DPPAlgorithmControl,
                        DPP::PSD::DPPAlgorithmControl2_G, DPP::PSD::CFDSetting, DPP::PSD::TriggerThreshold, DPP::PSD::TriggerHoldOffWidth,
                        DPP::PSD::FixedBaseline, DPP::PSD::ShortGateWidth, DPP::PSD::LongGateWidth, DPP::PSD::GateOffset};
    for( const Reg & r : regs ) digi[ID]->ReadRegister(r, ch);
  }

  if( digi[ID]->GetDPPType() == V1740_DPP_QDC_CODE ){
    const int grp = ch / 8;
    digi[ID]->ReadQDCRecordLength();
    const Reg regs[] = {DPP::QDC::PreTrigger, DPP::QDC::DCOffset, DPP::QDC::DPPAlgorithmControl, DPP::QDC::GateOffset, DPP::QDC::GateWidth,
                        DPP::QDC::TriggerThreshold_sub0, DPP::QDC::TriggerThreshold_sub1, DPP::QDC::TriggerThreshold_sub2, DPP::QDC::TriggerThreshold_sub3,
                        DPP::QDC::TriggerThreshold_sub4, DPP::QDC::TriggerThreshold_sub5, DPP::QDC::TriggerThreshold_sub6, DPP::QDC::TriggerThreshold_sub7};
    for( const Reg & r : regs ) digi[ID]->ReadRegister(r, grp);
  }

  UpdatePanelFromMomeory();
}
