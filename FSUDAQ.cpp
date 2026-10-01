#include "FSUDAQ.h"

#include <QWidget>
#include <QVBoxLayout>
#include <QHBoxLayout>
#include <QGroupBox>
#include <QDateTime>
#include <QLabel>

#include <QScrollBar>
#include <QCoreApplication>
#include <QDialog>
#include <QFileDialog>
#include <QInputDialog>
#include <QScrollArea>
#include <QProcess>
#include <QMessageBox>
#include <QIntValidator>
#include <QUrl>
#include <QNetworkRequest>
#include <QNetworkReply>
#include <QElapsedTimer>

#include "analyzers/CoincidentAnalyzer.h"
#include "analyzers/SplitPoleAnalyzer.h"
#include "analyzers/EncoreAnalyzer.h"
#include "analyzers/MUSICAnalyzer.h"
#include "analyzers/NeutronGamma.h"
#include "analyzers/Cross.h"

std::vector<std::string> onlineAnalyzerList = {"Coincident","Splie-Pole", "Encore", "MUSICS", "Neutron-Gamma", "Cross"};

FSUDAQ::FSUDAQ(QWidget *parent) : QMainWindow(parent){
  DebugPrint("%s", "FSUDAQ");
  setWindowTitle("FSU DAQ");
  setGeometry(500, 100, 1100, 600);

  digi = nullptr;
  nDigi = 0;
  isACQStarted= false;

  scalar = nullptr;
  scope = nullptr;
  digiSettings = nullptr;
  singleHistograms = nullptr;
  onlineAnalyzer = nullptr;
  runTimer = new QTimer();
  runTimer->setTimerType(Qt::PreciseTimer); // the default coarse timer may fire up to 5 % early or late (3 s on a 60 s run)
  runClockTimer = new QTimer(this);
  runClockTimer->setInterval(100);
  connect(runClockTimer, &QTimer::timeout, this, [=](){
    if( lbRunTime ) lbRunTime->setText(runClockLabel + "   <b>" + ElapsedText(runClock.elapsed()) + "</b>");
  });
  lbRunTime = nullptr;
  breakAutoRepeat = true;
  needManualComment = true;
  runRecord = nullptr;
  model = nullptr;
  influx = nullptr;
  scalarCount = 0;
  
  QWidget * mainLayoutWidget = new QWidget(this);
  setCentralWidget(mainLayoutWidget);
  QVBoxLayout * layoutMain = new QVBoxLayout(mainLayoutWidget);
  mainLayoutWidget->setLayout(layoutMain);

  {//^=======================
    QGroupBox * box = new QGroupBox("Digitizer(s)", mainLayoutWidget);
    layoutMain->addWidget(box);
    QGridLayout * layout = new QGridLayout(box);

    // where "w/ settings" reads the Digi-<serial>_<DPP>.bin files from (the data path is only for output)
    {
      QWidget * row = new QWidget(this);
      QHBoxLayout * hl = new QHBoxLayout(row);
      hl->setContentsMargins(0, 0, 0, 0);
      QLabel * lbSettingsPath = new QLabel("Settings Path : ", this);
      leSettingsPath = new QLineEdit(this);
      leSettingsPath->setReadOnly(true);
      leSettingsPath->setToolTip("Folder with the Digi-<serial>_<DPP>.bin settings files. \"w/ settings\" programs the boards from these files when the digitizers are opened.");
      bnSetSettingsPath = new QPushButton("Set Path", this);
      connect(bnSetSettingsPath, &QPushButton::clicked, this, &FSUDAQ::OpenSettingsPath);
      hl->addWidget(lbSettingsPath);
      hl->addWidget(leSettingsPath, 1);
      hl->addWidget(bnSetSettingsPath);
      layout->addWidget(row, 0, 0, 1, 4);
    }

    // link type and settings handling are chosen first; the button does the opening
    cbOpenDigitizers = new RComboBox(this);
    cbOpenDigitizers->addItem("via Optical link / USB", 1);
    cbOpenDigitizers->addItem("via A4818(s) (a4818_list.txt)", 4);
    cbOpenDigitizers->setToolTip("How the boards are connected. A4818 needs the PIDs in a4818_list.txt next to the program.");
    layout->addWidget(cbOpenDigitizers, 1, 0);
    
    cbOpenMethod = new RComboBox(this);
    cbOpenMethod->addItem("w/o settings", 0);
    cbOpenMethod->addItem("w/ settings", 1);
    cbOpenMethod->addItem("default Program", 2);
    cbOpenMethod->setCurrentIndex(1); // open with the settings files of the settings path by default
    cbOpenMethod->setToolTip("w/ settings: load Digi-<serial>_<DPP>.bin from the settings path and program the boards with it (default). w/o settings: open and read the boards as they are. default Program: write the built-in defaults.");
    layout->addWidget(cbOpenMethod, 2, 0);

    // one button: green "Open Digitizers" while closed, red "Close Digitizers" while open
    // (WaitForDigitizersOpen sets the look); disabled while it works, so a second click
    // during the search cannot close half-opened boards
    bnOpenDigitizers = new QPushButton("Open Digitizers", this);
    bnOpenDigitizers->setStyleSheet("background-color: green;");
    layout->addWidget(bnOpenDigitizers, 3, 0);
    connect(bnOpenDigitizers, &QPushButton::clicked, this, [this](){
      bnOpenDigitizers->setEnabled(false);
      if( digi == nullptr ) OpenDigitizers(); else CloseDigitizers();
      bnOpenDigitizers->setEnabled(true);
    });

    bnDigiSettings = new QPushButton("Digitizers Settings", this);
    layout->addWidget(bnDigiSettings, 1, 1);
    connect(bnDigiSettings, &QPushButton::clicked, this, &FSUDAQ::OpenDigiSettings);

    bnOpenScope = new QPushButton("Open Scope", this);
    layout->addWidget(bnOpenScope, 2, 1);
    connect(bnOpenScope, &QPushButton::clicked, this, &FSUDAQ::OpenScope);

    cbAnalyzer = new RComboBox(this);
    layout->addWidget(cbAnalyzer, 1, 2);
    cbAnalyzer->addItem("Choose Online Analyzer", -1);
    for( int i = 0; i < (int) onlineAnalyzerList.size() ; i++) cbAnalyzer->addItem(onlineAnalyzerList[i].c_str(), i);
    connect(cbAnalyzer, &RComboBox::currentIndexChanged, this, &FSUDAQ::OpenAnalyzer);

    // no "Online Histograms" window: it fills its plots from a worker thread while the GUI thread
    // draws them, and that race corrupted the heap mid-run (abort in Histogram1D::Fill, 1 Oct 2026).
    // Spectra belong in the online dashboard, which reads the files in its own process.

    bnSync = new QPushButton("Sync Boards", this);
    layout->addWidget(bnSync, 3, 1);
    connect(bnSync, &QPushButton::clicked, this, &FSUDAQ::SetSyncMode);

    bnDashboard = new QPushButton("Online Dashboard", this);
    bnDashboard->setToolTip("Start the Python online analysis on the data path (online/online_dashboard.py) and open it in the browser.");
    layout->addWidget(bnDashboard, 1, 3);
    connect(bnDashboard, &QPushButton::clicked, this, &FSUDAQ::OpenDashboard);
    dashboardProc = nullptr;
    net = new QNetworkAccessManager(this);

    chkAutoDashboard = new QCheckBox("Start dashboard with run", this);
    chkAutoDashboard->setChecked(true);
    chkAutoDashboard->setToolTip("Before a run starts, start the online dashboard if it is not running yet (a running one is left as it is). Off: runs start without it.");
    connect(chkAutoDashboard, &QCheckBox::toggled, this, &FSUDAQ::SaveProgramSettings);
    layout->addWidget(chkAutoDashboard, 2, 3);

  }

  {//^====================== influx and Elog
    QGroupBox * otherBox = new QGroupBox("Database and Elog", mainLayoutWidget);
    layoutMain->addWidget(otherBox);
    QGridLayout * layout = new QGridLayout(otherBox);
    layout->setVerticalSpacing(1);

    int rowID = 0;
    bnLock = new QPushButton("Unlock", this);
    bnLock->setChecked(true);
    layout->addWidget(bnLock, rowID, 0);

    QLabel * lbInfluxIP = new QLabel("Influx IP : ", this);
    lbInfluxIP->setAlignment(Qt::AlignRight | Qt::AlignCenter);
    layout->addWidget(lbInfluxIP, rowID, 1);

    leInfluxIP = new QLineEdit(this);
    leInfluxIP->setReadOnly(true);
    layout->addWidget(leInfluxIP, rowID, 2);

    QLabel * lbDatabaseName = new QLabel("Database Name : ", this);
    lbDatabaseName->setAlignment(Qt::AlignRight | Qt::AlignCenter);
    layout->addWidget(lbDatabaseName, rowID, 3);

    leDatabaseName = new QLineEdit(this);
    leDatabaseName->setReadOnly(true);
    layout->addWidget(leDatabaseName, rowID, 4);

    chkInflux = new QCheckBox("Enable", this);
    chkInflux->setChecked(true);
    layout->addWidget(chkInflux, rowID, 5);
    
    rowID ++;
    QLabel * lbElogIP = new QLabel("Elog IP : ", this);
    lbElogIP->setAlignment(Qt::AlignRight | Qt::AlignCenter);
    layout->addWidget(lbElogIP, rowID, 1);

    leElogIP = new QLineEdit(this);
    leElogIP->setReadOnly(true);
    layout->addWidget(leElogIP, rowID, 2);

    QLabel * lbElogName = new QLabel("Elog Name : ", this);
    lbElogName->setAlignment(Qt::AlignRight | Qt::AlignCenter);
    layout->addWidget(lbElogName, rowID, 3);

    leElogName = new QLineEdit(this);
    leElogName->setReadOnly(true);
    layout->addWidget(leElogName, rowID, 4);

    chkElog = new QCheckBox("Enable", this);
    chkElog->setChecked(true);
    layout->addWidget(chkElog, rowID, 5);

    connect(bnLock, &QPushButton::clicked, this, &FSUDAQ::SetAndLockInfluxElog);

  }

  {//^====================== ACQ control
    QGroupBox * box = new QGroupBox("ACQ Control", mainLayoutWidget);
    layoutMain->addWidget(box);
    QGridLayout * layout = new QGridLayout(box);

    int rowID = 0;
    //------------------------------------------
    lbDataPath = new QLabel("Data Path : ", this);
    lbDataPath->setAlignment(Qt::AlignRight | Qt::AlignCenter);
    leDataPath = new QLineEdit(this);
    leDataPath->setReadOnly(true);
    leDataPath->setToolTip("Where saved runs go (run folders, RunTimeStamp, lastRun.sh), and where Open Record reads. Not used while Save Data is off.");
    bnSetDataPath = new QPushButton("Set Path", this);
    connect(bnSetDataPath, &QPushButton::clicked, this, &FSUDAQ::OpenDataPath);

    QPushButton * bnOpenRecord = new QPushButton("Open Record", this);
    connect(bnOpenRecord, &QPushButton::clicked, this, &FSUDAQ::OpenRecord);

    layout->addWidget(lbDataPath, rowID, 0);
    layout->addWidget(leDataPath, rowID, 1, 1, 5);
    layout->addWidget(bnSetDataPath, rowID, 6);
    layout->addWidget(bnOpenRecord, rowID, 7);

    //------------------------------------------
    rowID ++;
    QLabel * lbPrefix = new QLabel("Prefix : ", this);
    lbPrefix->setAlignment(Qt::AlignRight | Qt::AlignCenter);
    lePrefix = new QLineEdit(this);
    lePrefix->setAlignment(Qt::AlignHCenter);
    connect(lePrefix, &QLineEdit::textChanged, this, [=](){
      lePrefix->setStyleSheet("color:blue;");
    });
    connect(lePrefix, &QLineEdit::returnPressed, this, &FSUDAQ::SaveLastRunFile);

    QLabel * lbRunID = new QLabel("Run No. :", this);
    lbRunID->setAlignment(Qt::AlignRight | Qt::AlignCenter);
    leRunID = new QLineEdit(this);
    leRunID->setReadOnly(true);
    leRunID->setAlignment(Qt::AlignHCenter);
    leRunID->setValidator(new QIntValidator(0, 999999, this));
    leRunID->setToolTip("Next run number. Editable when auto-increment is off.");

    chkSaveData = new QCheckBox("Save Data", this);
    connect(chkSaveData, &QCheckBox::toggled, this, &FSUDAQ::UpdateDataPathEnabled);

    chkAutoIncrement = new QCheckBox("Auto-increment run no.", this);
    chkAutoIncrement->setChecked(true);
    chkAutoIncrement->setToolTip("Checked: each saved run takes the next number. Unchecked: the run number field is editable and a run refuses to start if files for that number already exist.");
    connect(chkAutoIncrement, &QCheckBox::toggled, this, [=](bool checked){
      leRunID->setReadOnly(checked);
      if( checked ) leRunID->setText(QString::number(runID));
      SaveProgramSettings();
    });

    bnStartACQ = new QPushButton("Start ACQ", this);
    connect( bnStartACQ, &QPushButton::clicked, this, &FSUDAQ::AutoRun);
    bnStopACQ = new QPushButton("Stop ACQ", this);
    connect( bnStopACQ, &QPushButton::clicked, this, [=](){
        if( runTimer->isActive() ){
          runTimer->stop();
          runTimer->disconnect(runTimerConnection);
        }else{
          breakAutoRepeat = true;
          runTimer->disconnect(runTimerConnection);
        }
        needManualComment = true;
        StopACQ();
    });

    layout->addWidget(lbPrefix, rowID, 0);
    layout->addWidget(lePrefix, rowID, 1);
    layout->addWidget(lbRunID, rowID, 2);
    layout->addWidget(leRunID, rowID, 3);
    layout->addWidget(chkSaveData, rowID, 4);
    layout->addWidget(chkAutoIncrement, rowID, 5);
    layout->addWidget(bnStartACQ, rowID, 6);
    layout->addWidget(bnStopACQ, rowID, 7);

    //------------------------------------------
    rowID ++;
    QLabel * lbRunLength = new QLabel("Run length : ", this);
    lbRunLength->setAlignment(Qt::AlignRight | Qt::AlignCenter);
    sbRunTimeMin = new RSpinBox(this, 1);
    sbRunTimeMin->setRange(0, 100000);
    sbRunTimeMin->setSingleStep(1);
    sbRunTimeMin->setValue(0);
    sbRunTimeMin->setSuffix(" min");
    sbRunTimeMin->setSpecialValueText("until Stop");
    sbRunTimeMin->setToolTip("The run stops by itself after this many minutes (decimals allowed). 0 = run until Stop is pressed.");

    chkRepeatRun = new QCheckBox("Repeat", this);
    chkRepeatRun->setToolTip("When the time is up, wait the pause and start the next run, until Stop is pressed.");
    connect(chkRepeatRun, &QCheckBox::toggled, this, [=](bool checked){
      sbRepeatPauseSec->setEnabled(checked && chkRepeatRun->isEnabled()); // the pause only matters when repeating
    });

    sbRepeatPauseSec = new RSpinBox(this, 0);
    sbRepeatPauseSec->setRange(0, 3600);
    sbRepeatPauseSec->setSingleStep(1);
    sbRepeatPauseSec->setValue(10);
    sbRepeatPauseSec->setSuffix(" s pause");
    sbRepeatPauseSec->setToolTip("Pause between repeated runs.");
    connect(sbRepeatPauseSec, &RSpinBox::editingFinished, this, &FSUDAQ::SaveProgramSettings);

    EnableRunLengthControls(false); // enabled once the digitizers are open, locked while a run is going

    layout->addWidget(lbRunLength, rowID, 0);
    layout->addWidget(sbRunTimeMin, rowID, 1);
    layout->addWidget(chkRepeatRun, rowID, 2);
    layout->addWidget(sbRepeatPauseSec, rowID, 3);

    QLabel * lbFileSize = new QLabel("File size : ", this);
    lbFileSize->setAlignment(Qt::AlignRight | Qt::AlignCenter);
    sbFileSizeMB = new RSpinBox(this, 0);
    sbFileSizeMB->setRange(10, 1000000);
    sbFileSizeMB->setSingleStep(10);
    sbFileSizeMB->setValue(MaxSaveFileSize / 1024 / 1024);
    sbFileSizeMB->setSuffix(" MB");
    sbFileSizeMB->setToolTip("Each board's data file is closed and the next index opened once it exceeds this size.");
    connect(sbFileSizeMB, &RSpinBox::editingFinished, this, &FSUDAQ::SaveProgramSettings);

    layout->addWidget(lbFileSize, rowID, 4);
    layout->addWidget(sbFileSizeMB, rowID, 5);

    //------------------------------------------
    rowID ++;
    QLabel * lbComment = new QLabel("Run Comment : ", this);
    lbComment->setAlignment(Qt::AlignRight | Qt::AlignCenter);

    leComment = new QLineEdit(this);
    leComment->setReadOnly(true);

    chkSkipComment = new QCheckBox("Skip comment dialogs", this);
    chkSkipComment->setToolTip("Start and stop runs without asking for a comment; the record gets \"no comment\".");

    bnOpenScaler = new QPushButton("Run Monitor", this);
    bnOpenScaler->setToolTip("Rates, elapsed time and file sizes per board (upstream calls this window the Scalar).");
    connect(bnOpenScaler, &QPushButton::clicked, this, &FSUDAQ::OpenScalar);

    layout->addWidget(lbComment, rowID, 0);
    layout->addWidget(leComment, rowID, 1, 1, 7);
    layout->addWidget(chkSkipComment, rowID - 1, 6);

    layout->addWidget(bnOpenScaler, rowID - 1, 7);

    layout->setColumnStretch(0, 1);
    layout->setColumnStretch(1, 2);
    layout->setColumnStretch(2, 1);
    layout->setColumnStretch(3, 1);
    layout->setColumnStretch(4, 1);
    layout->setColumnStretch(5, 1);
    layout->setColumnStretch(6, 3);
    layout->setColumnStretch(7, 3);

  }


  {//^===================== Log Msg

    logMsgHTMLMode = true;
    QGroupBox * box3 = new QGroupBox("Log Message", mainLayoutWidget);
    layoutMain->addWidget(box3);
    layoutMain->setStretchFactor(box3, 1);
    QVBoxLayout * layout3 = new QVBoxLayout(box3);
    logInfo = new QPlainTextEdit(this);
    logInfo->setReadOnly(true);
    QFont font; 
    font.setFamily("Courier New");
    logInfo->setFont(font);
    layout3->addWidget(logInfo);

  }

  LogMsg("<font style=\"color: blue;\"><b>Welcome to FSU DAQ.</b></font>");

  rawDataPath = "";
  settingsPath = "";
  prefix = "temp";
  runID = 0;
  elogID = 0;
  elogName = "";
  elogUser = "";
  elogPWD = "";
  influxIP = "";
  dataBaseName = "";
  influxToken = "";
  programSettingsFilePath = QDir::current().absolutePath() + "/programSettings.txt";
  LoadProgramSettings();
  UpdateDataPathEnabled();

  //=========== disable widget
  WaitForDigitizersOpen(true);

  SetUpInflux();

  CheckElog();

  LogMsg("====== <font style=\"color: blue;\"><b>FSU DAQ is ready.</b></font> ======");

}

FSUDAQ::~FSUDAQ(){
  DebugPrint("%s", "FSUDAQ");
  if( scalar ) {
    scalarTimingThread->Stop();
    scalarTimingThread->quit();
    scalarTimingThread->exit();
    // scalarTimer->stop();
    // if( scalarThread->isRunning() ){
    //   scalarThread->quit();
    //   scalarThread->exit();
    // }
    CleanUpScalar();
    //don't need to delete scalar, it is managed by this
  }

  if( digi ) CloseDigitizers();
  SaveProgramSettings();

  if( dashboardProc && dashboardProc->state() != QProcess::NotRunning ){
    dashboardProc->terminate();
    if( !dashboardProc->waitForFinished(3000) ) dashboardProc->kill();
  }

  if( scope ) delete scope;

  if( singleHistograms ) delete singleHistograms;

  if( onlineAnalyzer ) delete onlineAnalyzer;

  if( digiSettings ) delete digiSettings;


  delete influx;

  printf("-------- remove %s\n", DAQLockFile);
  remove(DAQLockFile);

}

//***************************************************************
//***************************************************************
void FSUDAQ::OpenSettingsPath(){
  DebugPrint("%s", "FSUDAQ");
  QString dir = QFileDialog::getExistingDirectory(this, "Folder with the digitizer settings files", settingsPath.isEmpty() ? rawDataPath : settingsPath);
  if( dir.isEmpty() ) return;   // cancelled: keep the current path
  settingsPath = dir;
  leSettingsPath->setText(settingsPath);
  LogMsg("Settings path : <b>" + settingsPath + "</b> (used the next time the digitizers are opened w/ settings)");
  SaveProgramSettings();
}

void FSUDAQ::UpdateDataPathEnabled(){
  const bool on = chkSaveData->isChecked();
  lbDataPath->setEnabled(on);
  leDataPath->setEnabled(on);   // the Set Path button stays usable: Save Data needs a path before it can be ticked
}

void FSUDAQ::OpenDataPath(){
  DebugPrint("%s", "FSUDAQ");
  QFileDialog fileDialog(this);
  fileDialog.setFileMode(QFileDialog::Directory);
  int result = fileDialog.exec();

  //qDebug() << fileDialog.selectedFiles();
  if( result > 0 ) {
    leDataPath->setText(fileDialog.selectedFiles().at(0));
    rawDataPath = leDataPath->text();
  }else{
    leDataPath->clear();
    rawDataPath = "";
  }

  if( !rawDataPath.isEmpty() ) chkSaveData->setEnabled(true);

  SaveProgramSettings();

  LoadLastRunFile();

}

void FSUDAQ::OpenRecord(){
  DebugPrint("%s", "FSUDAQ");
  QString filePath = leDataPath->text() + "/RunTimeStamp.dat";

  if( runRecord == nullptr ){
    runRecord = new QMainWindow(this);
    runRecord->setGeometry(0,0, 500, 500);
    runRecord->setWindowTitle("Run Record");

    QWidget * widget = new QWidget(runRecord);
    runRecord->setCentralWidget(widget);

    QVBoxLayout * layout = new QVBoxLayout(widget);
    widget->setLayout(layout);
    
    QLabel * lbFilePath = new QLabel(widget);
    lbFilePath->setText(filePath);
    layout->addWidget(lbFilePath);

    tableView = new QTableView(widget);
    layout->addWidget(tableView);

    model = new QStandardItemModel(runRecord);
    tableView->setModel(model);

  }

  UpdateRecord();
  runRecord->show();

}

void FSUDAQ::UpdateRecord(){
  DebugPrint("%s", "FSUDAQ");
  if( !runRecord ) return;

  QString filePath = leDataPath->text() + "/RunTimeStamp.dat";
  model->clear();
  
  if (!filePath.isEmpty()) {
    QFile file(filePath);
    if (file.open(QIODevice::ReadOnly | QIODevice::Text)) {

      QTextStream stream(&file);
      while (!stream.atEnd()) {
        QString line = stream.readLine();
        QStringList fields = line.split('|');

        QList<QStandardItem*> items;
        for (const QString& field : fields) {
          items.append(new QStandardItem(field));
        }
        model->appendRow(items);
      }

      file.close();
      tableView->resizeColumnsToContents();
    }
  }

  tableView->scrollToBottom();
}

void FSUDAQ::LoadProgramSettings(){
  DebugPrint("%s", "FSUDAQ");
  LogMsg("Loading <b>" + programSettingsFilePath + "</b> for Program Settings.");
  QFile file(programSettingsFilePath);

  if( !file.open(QIODevice::Text | QIODevice::ReadOnly) ) {
    LogMsg("<b>" + programSettingsFilePath + "</b> not found.");
  }else{

    QTextStream in(&file);
    QString line = in.readLine();

    int count = 0;
    while( !line.isNull()){
      if( line.left(6) == "//----") break;

      if( count == 0 ) rawDataPath = line;
      if( count == 1 ) influxIP = line;
      if( count == 2 ) dataBaseName = line;
      if( count == 3 ) influxToken = line;
      if( count == 4 ) elogIP = line;
      if( count == 5 ) elogPort = line;
      if( count == 6 ) elogName = line;
      if( count == 7 ) elogUser = line;
      if( count == 8 ) elogPWD = line;
      if( count >= 9 ) { // key=value lines, order free, absent in files written by older versions
        int eq = line.indexOf("=");
        QString key = line.left(eq).trimmed();
        QString value = line.mid(eq + 1).trimmed();
        if( eq > 0 && key == "autoIncrementRunID" ) {
          // the toggled slot saves the settings file, which must not happen while it is being read
          QSignalBlocker blocker(chkAutoIncrement);
          chkAutoIncrement->setChecked(value.toInt() != 0);
          leRunID->setReadOnly(chkAutoIncrement->isChecked());
        }
        if( eq > 0 && key == "maxFileSizeMB" && value.toInt() > 0 ) sbFileSizeMB->setValue(value.toInt());
        if( eq > 0 && key == "repeatPauseSec" ) sbRepeatPauseSec->setValue(value.toInt());
        if( eq > 0 && key == "autoDashboard" ) { QSignalBlocker blocker(chkAutoDashboard); chkAutoDashboard->setChecked(value.toInt() != 0); }
        if( eq > 0 && key == "settingsPath" ) settingsPath = value;
      }

      count ++;
      line = in.readLine();
    }

    if( settingsPath.isEmpty() ) settingsPath = rawDataPath; // files written before the settings path existed
    //looking for the lastRun.sh for 
    leDataPath->setText(rawDataPath);
    leSettingsPath->setText(settingsPath);
    leInfluxIP->setText(influxIP);
    leDatabaseName->setText(dataBaseName);
    leElogIP->setText(elogIP);
    leElogName->setText(elogName);

    logMsgHTMLMode = false;
    LogMsg(" Raw Data Path : " + rawDataPath);
    LogMsg(" Settings Path : " + settingsPath);
    LogMsg("     Influx IP : " + influxIP);
    LogMsg(" Database Name : " + dataBaseName);
    LogMsg("Database Token : " + maskText(influxToken));
    LogMsg("       Elog IP : " + elogIP);
    LogMsg("     Elog Port : " + elogPort);
    LogMsg("     Elog Name : " + elogName);
    LogMsg("     Elog User : " + maskText(elogUser));
    LogMsg("      Elog PWD : " + maskText(elogPWD));
    logMsgHTMLMode = true;

    //check is rawDataPath exist, if not, create one
    QDir rawDataDir;
    if( !rawDataDir.exists(rawDataPath ) ) {
      if( rawDataDir.mkdir(rawDataPath) ){
          LogMsg("Created folder <b>" + rawDataPath + "</b> for storing root files.");
      }else{
          LogMsg("<font style=\"color:red;\"><b>" + rawDataPath + "</b> Raw data folder cannot be created. Access right problem? </font>" );
      }
      }else{
        LogMsg("<b>" + rawDataPath + "</b> already exist." );
    }
    LoadLastRunFile();
  }

}

void FSUDAQ::SaveProgramSettings(){
  DebugPrint("%s", "FSUDAQ");
  rawDataPath = leDataPath->text();

  QFile file(programSettingsFilePath);
  
  file.open(QIODevice::Text | QIODevice::WriteOnly);

  file.write((rawDataPath+"\n").toStdString().c_str());
  file.write((influxIP+"\n").toStdString().c_str());
  file.write((dataBaseName+"\n").toStdString().c_str());
  file.write((influxToken+"\n").toStdString().c_str());
  file.write((elogIP+"\n").toStdString().c_str());
  file.write((elogPort+"\n").toStdString().c_str());
  file.write((elogName+"\n").toStdString().c_str());
  file.write((elogUser+"\n").toStdString().c_str());
  file.write((elogPWD+"\n").toStdString().c_str());
  file.write(("autoIncrementRunID=" + QString::number(chkAutoIncrement->isChecked() ? 1 : 0) + "\n").toStdString().c_str());
  file.write(("maxFileSizeMB=" + QString::number((int) sbFileSizeMB->value()) + "\n").toStdString().c_str());
  file.write(("repeatPauseSec=" + QString::number((int) sbRepeatPauseSec->value()) + "\n").toStdString().c_str());
  file.write(("settingsPath=" + settingsPath + "\n").toStdString().c_str());
  file.write(("autoDashboard=" + QString::number(chkAutoDashboard->isChecked() ? 1 : 0) + "\n").toStdString().c_str());
  file.write("//------------end of file.\n");
  
  file.close();
  LogMsg("Saved program settings to <b>"+ programSettingsFilePath + "<b>.");

}

void FSUDAQ::LoadLastRunFile(){
  DebugPrint("%s", "FSUDAQ");
  QFile file(rawDataPath + "/lastRun.sh");

  if( !file.open(QIODevice::Text | QIODevice::ReadOnly) ) {
    LogMsg("<b>" + rawDataPath + "/lastRun.sh</b> not found.");
    runID = 0;
    prefix = "temp";
    leRunID->setText(QString::number(runID));
    lePrefix->setText(prefix);
  }else{

    QTextStream in(&file);
    QString line = in.readLine();

    int count = 0;
    while( !line.isNull()){

      int index = line.indexOf("=");
      QString haha = line.mid(index+1).remove(" ");

      //qDebug() << haha;

      switch (count){
        case 0 : prefix = haha; break;
        case 1 : runID = haha.toInt(); break;
        case 2 : elogID = haha.toInt(); break;
      }

      count ++;
      line = in.readLine();
    }

    lePrefix->setText(prefix);
    lePrefix->setStyleSheet("");
    leRunID->setText(QString::number(runID));

  }

}

void FSUDAQ::SaveLastRunFile(){
  DebugPrint("%s", "FSUDAQ");
  QFile file(rawDataPath + "/lastRun.sh");

  prefix = lePrefix->text();
  lePrefix->setStyleSheet("");

  file.open(QIODevice::Text | QIODevice::WriteOnly);
  file.write(("prefix=" + prefix + "\n").toStdString().c_str());
  file.write(("runID=" + QString::number(runID) + "\n").toStdString().c_str());
  file.write(("elogID=" + QString::number(elogID) + "\n").toStdString().c_str());
  file.write("//------------end of file.");
  
  file.close();
  LogMsg("Saved program settings to <b>"+ rawDataPath + "/lastRun.sh<b>.");

}

//***************************************************************
//***************************************************************
void FSUDAQ::OpenDigitizers(){
  DebugPrint("%s", "FSUDAQ");
  if( digi != nullptr ) return; // already open

  // placeholder for USB
  // if( cbOpenDigitizers->currentData().toInt() == 3 ) {
  //   return;
  // }

  QStringList a4818PIDs;
  if( cbOpenDigitizers->currentData().toInt() == 4 ) {

    QString a4818Path = QDir::current().absolutePath() + "/a4818_list.txt";
    LogMsg("Looking <b>" + a4818Path + "</b>");

    QFile file(a4818Path);

    if( !file.open(QIODevice::Text | QIODevice::ReadOnly) ) {
      LogMsg("<b>" + a4818Path + "</b> not found.");
      LogMsg("Please create such file and put the a4818 PIDs inseperate lines.");
      return;
    }else{
      QTextStream in(&file);
      QString line = in.readLine();

      while( !line.isNull()){
        a4818PIDs.push_back(line);
        line = in.readLine();
      }
    }

    if( a4818PIDs.isEmpty()){
      LogMsg("<b>" + a4818Path + "</b> is empty.");
      return;
    }else{

      if( a4818PIDs.size() > MaxNPorts){
        LogMsg("There are more than "+ QString::number(MaxNPorts) + " a4818, please edit the MaxNPorts in macro.h and recompile.");
      }

    }
  }

  if( cbOpenDigitizers->currentData().toInt() == 4 ) {
    LogMsg("Searching digitizers via A4818 .....Please wait");
  }else{
    LogMsg("Searching digitizers via optical link or USB .....Please wait");
  }

  logMsgHTMLMode = false;
  nDigi = 0;
  std::vector<std::pair<int, int>> portList; //boardID, portID

  if( cbOpenDigitizers->currentData().toInt() == 4 ) { //for A4818
    
    for( int i = 0; i < std::min((int)a4818PIDs.size(), MaxNPorts); i++){
      int port = a4818PIDs.at(i).toInt();
      
      for( int board = 0; board < MaxNBoards; board ++){ /// max number of diasy chain
        Digitizer dig;
        dig.OpenDigitizer(board, port);
        if( dig.IsConnected() ){
          nDigi++;
          portList.push_back(std::pair(board, port));
          LogMsg(QString("... Found at port: %1, board: %2. SN: %3 %4").arg(port).arg(board).arg(dig.GetSerialNumber(), 3, 10, QChar(' ')).arg(dig.GetDPPString().c_str()));
        }
        dig.CloseDigitizer();
        QCoreApplication::processEvents(); //to prevent Qt said application not responding.
      }
    }

  }else{ // optical fiber

    for(int port = 0; port < MaxNPorts; port++){

      for( int board = 0; board < MaxNBoards; board ++){ /// max number of diasy chain
        Digitizer dig;
        dig.OpenDigitizer(board, port);
        if( dig.IsConnected() ){
          nDigi++;
          portList.push_back(std::pair(board, port));
          LogMsg(QString("... Found at port: %1, board: %2. SN: %3 %4").arg(port).arg(board).arg(dig.GetSerialNumber(), 3, 10, QChar(' ')).arg(dig.GetDPPString().c_str()));
        }
        dig.CloseDigitizer();
        QCoreApplication::processEvents(); //to prevent Qt said application not responding.
      }
    }
  }
  logMsgHTMLMode = true;

  if( nDigi == 0 ) {
    LogMsg(QString("Done searching. No digitizer found from port 0 to ") +  QString::number(MaxNPorts) + " and board 0 to " + QString::number(MaxNBoards) + ".");
    return;
  }else{
    if( cbOpenMethod->currentData().toInt() == 0 ) LogMsg(QString("Done seraching. Found %1 digitizer(s). Opening digitizer(s)....").arg(nDigi));
    if( cbOpenMethod->currentData().toInt() == 1 ) LogMsg(QString("Done searching. Found %1 digitizer(s). Opening digitizer(s) and loading settings files....").arg(nDigi));
    if( cbOpenMethod->currentData().toInt() == 2 ) LogMsg(QString("Done searching. Found %1 digitizer(s). Opening digitizer(s) and programming defaults....").arg(nDigi));    
  }
  
  digi = new Digitizer * [nDigi];
  readDataThread = new ReadDataThread * [nDigi];

  for( unsigned int i = 0; i < nDigi; i++){
    digi[i] = new Digitizer(portList[i].first, portList[i].second);
    //digi[i]->Reset();

    if( cbOpenMethod->currentData().toInt() == 2 ) {
      digi[i]->ProgramBoard();
    }

    ///============== load settings 
    if( cbOpenMethod->currentData().toInt() <= 1 ){
      QString fileName = settingsPath + "/Digi-" + QString::number(digi[i]->GetSerialNumber()) + "_" + QString::fromStdString(digi[i]->GetData()->DPPTypeStr) + ".bin";
      QFile file(fileName);
      if( !file.open(QIODevice::Text | QIODevice::ReadOnly) ) {

        if( digi[i]->GetDPPType() == V1730_DPP_PHA_CODE ) {
          //digi[i]->ProgramBoard_PHA();
          //LogMsg("<b>" + fileName + "</b> not found. Program predefined PHA settings.");
          LogMsg("<b>" + fileName + "</b> not found.");
        }
        if( digi[i]->GetDPPType() == V1730_DPP_PSD_CODE ){
          //digi[i]->ProgramBoard_PSD();
          //LogMsg("<b>" + fileName + "</b> not found. Program predefined PSD settings.");
          LogMsg("<b>" + fileName + "</b> not found.");
        }
        if( digi[i]->GetDPPType() == V1740_DPP_QDC_CODE ){
          //digi[i]->ProgramBoard_QDC();
          //LogMsg("<b>" + fileName + "</b> not found. Program predefined PSD settings.");
          LogMsg("<b>" + fileName + "</b> not found.");
        }
      }else{
        LogMsg("Found <b>" + fileName + "</b> for digitizer settings.");
        
        if( cbOpenMethod->currentData().toInt() == 1 ){
          if( digi[i]->LoadSettingBinaryToMemory(fileName.toStdString().c_str()) == 0 ){
            LogMsg("Loaded settings file <b>" + fileName + "</b> for Digi-" + QString::number(digi[i]->GetSerialNumber()));
            digi[i]->ProgramSettingsToBoard();
          }else{
            LogMsg("Fail to Loaded settings file " + fileName + " for Digi-" + QString::number(digi[i]->GetSerialNumber()));
          }
        }else{
          LogMsg("Save the setting file path, but not load.");
          digi[i]->SetSettingBinaryPath(fileName.toStdString());
        }
        
      }
    }    
    digi[i]->ReadAllSettingsFromBoard(true);

    //===== set no trace, even when FSQDAQ segfault at scope, the digitizer will save no trace
    digi[i]->SetTrace(false);
    // if( digi[i]->GetDPPType() == V1730_DPP_PHA_CODE) digi[i]->WriteRegister(DPP::BoardConfiguration, 0xE8915);

    readDataThread[i] = new ReadDataThread(digi[i], i);
    connect(readDataThread[i], &ReadDataThread::sendMsg, this, &FSUDAQ::LogMsg);
    
    QCoreApplication::processEvents(); //to prevent Qt said application not responding.
  }

  LogMsg("====== <font style=\"color: blue;\"><b>" + QString("Done. Opened %1 digitizer(s).").arg(nDigi) + "</b></font> =====");

  WaitForDigitizersOpen(false);
  bnStartACQ->setStyleSheet("background-color: green;");
  bnStopACQ->setEnabled(false);
  bnStopACQ->setStyleSheet("");

  bnSync->setEnabled( nDigi >= 1  );

  if( rawDataPath == "" ) {
    chkSaveData->setChecked(false);
    chkSaveData->setEnabled(false);
  }

  SetupScalar();

}

void FSUDAQ::CloseDigitizers(){
  LogMsg("FSUDAQ::Closing Digitizer(s)....");

  if( scope ) {
    scope->close();
    delete scope;
    scope = nullptr;
  }

  // scalarTimer->stop();
  // if( scalarThread->isRunning() ){
  //   scalarThread->quit();
  //   scalarThread->exit();
  // }
  scalarTimingThread->Stop();
  if( scalarTimingThread->isRunning() ){
    scalarTimingThread->quit();
    scalarTimingThread->exit();
  }
  if( scalar ) CleanUpScalar();

  if( onlineAnalyzer ){
    onlineAnalyzer->close();
    delete onlineAnalyzer;
    onlineAnalyzer = nullptr;
  }

  if( singleHistograms ){
    singleHistograms->close();
    delete singleHistograms;
    singleHistograms = nullptr;
  }

  if( digiSettings ){
    digiSettings->close();
    delete digiSettings;
    digiSettings = nullptr;
  }

  if( digi == nullptr ) return;

  for(unsigned int i = 0; i < nDigi; i ++){
    readDataThread[i]->Stop();
    readDataThread[i]->quit();
    readDataThread[i]->wait();
    delete readDataThread[i];
    printf(" readDataThread[%d] is deleted.\n", i);
  }

  delete [] readDataThread;
  readDataThread = nullptr;

  for(unsigned int i = 0; i < nDigi; i ++){
    digi[i]->StopACQ();
    digi[i]->CloseDigitizer();
    delete digi[i];
  }
  delete [] digi;
  digi = nullptr;

  LogMsg("Done. Closed " + QString::number(nDigi) + " Digitizer(s).");
  nDigi = 0;

  WaitForDigitizersOpen(true);
  bnStartACQ->setStyleSheet("");
  bnStopACQ->setStyleSheet("");

  printf("End of FSUDAQ::%s\n", __func__);

}

void FSUDAQ::WaitForDigitizersOpen(bool onOff){
  DebugPrint("%s", "FSUDAQ");
  // bnOpenDigitizers->setEnabled(onOff);

  cbOpenDigitizers->setEnabled(onOff);
  bnOpenDigitizers->setText(onOff ? "Open Digitizers" : "Close Digitizers");
  bnOpenDigitizers->setStyleSheet(onOff ? "background-color: green;" : "background-color: red;");
  cbOpenMethod->setEnabled(onOff);
  bnSetSettingsPath->setEnabled(onOff);

  bnOpenScope->setEnabled(!onOff);
  bnDigiSettings->setEnabled(!onOff);
  bnOpenScaler->setEnabled(!onOff);
  bnStartACQ->setEnabled(!onOff);
  bnStopACQ->setEnabled(!onOff);
  bnStopACQ->setStyleSheet("");
  chkSaveData->setEnabled(!onOff);
  cbAnalyzer->setEnabled(!onOff);

  EnableRunLengthControls(!onOff);
  bnSync->setEnabled(false);

}

//***************************************************************
//***************************************************************
void FSUDAQ::SetupScalar(){
  DebugPrint("%s", "FSUDAQ");
  // printf("%s\n", __func__);

  scalar = new QMainWindow(this);
  scalar->setWindowTitle("Run Monitor (Scalar)");

  QScrollArea * scopeScroll = new QScrollArea(scalar);
  scalar->setCentralWidget(scopeScroll);
  scopeScroll->setWidgetResizable(true);
  scopeScroll->setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Expanding);

  QWidget * layoutWidget = new QWidget(scalar);
  scopeScroll->setWidget(layoutWidget);

  // the status lines sit above the board grid, not in its columns, so their text cannot widen
  // a board column; both are as wide as the grid and kept at the top left
  QVBoxLayout * outerLayout = new QVBoxLayout(layoutWidget);
  QWidget * block = new QWidget(layoutWidget);
  outerLayout->addWidget(block, 0, Qt::AlignTop | Qt::AlignLeft);
  QVBoxLayout * blockLayout = new QVBoxLayout(block);
  blockLayout->setContentsMargins(0, 0, 0, 0);
  QGridLayout * topLayout = new QGridLayout();
  topLayout->setColumnStretch(0, 1);
  blockLayout->addLayout(topLayout);

  scalarLayout = new QGridLayout();
  scalarLayout->setSpacing(0);
  scalarLayout->setHorizontalSpacing(3);
  blockLayout->addLayout(scalarLayout);

  leTrigger = nullptr;
  leAccept = nullptr;
  leDead = nullptr;

  lbLastUpdateTime = nullptr;
  lbScalarACQStatus = nullptr;
  lbTotalFileSize = nullptr;

  scalarTimingThread = new TimingThread(scalar);
  scalarTimingThread->SetWaitTimeinSec(ScalarUpdateinMiliSec / 1000.);
  connect(scalarTimingThread, &TimingThread::timeUp, this, &FSUDAQ::UpdateScalar);

  // scalarThread = new QThread(this);
  // scalarWorker = new ScalarWorker(this);
  // scalarWorker->moveToThread(scalarThread);

  // scalarTimer = new QTimer(this);
  // connect( scalarTimer, &QTimer::timeout, scalarWorker, &ScalarWorker::UpdateScalar);

  // scalarThread->start();

  unsigned short maxNChannel = 0;
  for( unsigned int k = 0; k < nDigi; k ++ ){
    if( digi[k]->GetNumInputCh() > maxNChannel ) maxNChannel = digi[k]->GetNumInputCh();
  }

  // fixed cell widths: the columns never resize with the text; the board summary above them
  // uses rows rather than width, and its labels ignore their width so they cannot widen a column
  const int cellW[3] = {72, 72, 88};        // Counts/s, Input/s, Dead %
  const int boardW = cellW[0] + cellW[1] + cellW[2] + 2 * 3;
  const int headRow = 10;                   // column titles; channels follow

  const int gapW = 18;                      // empty column between boards

  scalar->setGeometry(0, 0, 60 + nDigi * (boardW + gapW + 6), 160 + (headRow + maxNChannel) * 25);

  if( lbLastUpdateTime == nullptr ){
    lbLastUpdateTime = new QLabel("Last update : NA", scalar);
    lbScalarACQStatus = new QLabel("ACQ status", scalar);
    lbTotalFileSize = new QLabel("Total File Size", scalar);
    lbRunTime = new QLabel("no run yet", scalar);
    QFont clockFont = lbRunTime->font();
    clockFont.setPointSizeF(clockFont.pointSizeF() * 1.3);
    lbRunTime->setFont(clockFont);
    lbRunTime->setToolTip("Elapsed acquisition time, h:mm:ss.ms, from the moment the boards were started; frozen at the value when the run stopped.");
  }

  // run clock and ACQ status on the left; last update and the totals on the right
  lbRunTime->setAlignment(Qt::AlignLeft | Qt::AlignVCenter);
  topLayout->addWidget(lbRunTime, 0, 0);
  lbScalarACQStatus->setAlignment(Qt::AlignLeft | Qt::AlignVCenter);
  topLayout->addWidget(lbScalarACQStatus, 1, 0);
  lbLastUpdateTime->setAlignment(Qt::AlignRight | Qt::AlignVCenter);
  topLayout->addWidget(lbLastUpdateTime, 0, 1);
  lbTotalFileSize->setAlignment(Qt::AlignRight | Qt::AlignVCenter);
  topLayout->addWidget(lbTotalFileSize, 1, 1);

  ///==== create the header row
  int rowID = headRow;
  for( int ch = 0; ch < maxNChannel; ch++){

    if( ch == 0 ){
      QLabel * lbCH_H = new QLabel("Ch", scalar); 
      lbCH_H->setAlignment(Qt::AlignCenter);
      scalarLayout->addWidget(lbCH_H, rowID, 0);
    }  

    rowID ++;
    QLabel * lbCH = new QLabel(QString::number(ch), scalar);
    lbCH->setAlignment(Qt::AlignCenter);
    scalarLayout->addWidget(lbCH, rowID, 0);
  }

  ///===== per board: name and run status, headline rates, diagnostics, then Counts/s, Input/s, Dead % per channel
  leTrigger = new QLineEdit**[nDigi];
  leAccept = new QLineEdit**[nDigi];
  leDead = new QLineEdit**[nDigi];
  deadWin.assign(nDigi, std::vector<DeadTimeWindow>());
  QFont smallFont = scalar->font();
  smallFont.setPointSizeF(smallFont.pointSizeF() * 0.85);
  for( unsigned int iDigi = 0; iDigi < nDigi; iDigi++){
    rowID = 2;
    const int col = 4 * iDigi + 1;
    if( iDigi + 1 < nDigi ) scalarLayout->setColumnMinimumWidth(col + 3, gapW);
    uint32_t chMask =  digi[iDigi]->GetRegChannelMask();
    deadWin[iDigi].assign(digi[iDigi]->GetNumInputCh(), DeadTimeWindow());

    QWidget * hBox = new QWidget(scalar);
    QHBoxLayout * hBoxLayout = new QHBoxLayout(hBox);
    hBox->setFixedWidth(boardW);
    scalarLayout->addWidget(hBox, rowID, col, 1, 3);

    QLabel * lbDigi = new QLabel("<b>Digi-" + QString::number(digi[iDigi]->GetSerialNumber()) + "</b>", scalar); 
    lbDigi->setAlignment(Qt::AlignCenter);
    hBoxLayout->addStretch(1);
    hBoxLayout->addWidget(lbDigi);

    runStatus[iDigi] = new QPushButton("", scalar);
    runStatus[iDigi]->setEnabled(false);
    runStatus[iDigi]->setFixedSize(QSize(20,20));
    runStatus[iDigi]->setToolTip("ACQ RUN On/OFF");
    runStatus[iDigi]->setToolTipDuration(-1);
    hBoxLayout->addWidget(runStatus[iDigi]);
    hBoxLayout->addStretch(1);

    // board summary, one quantity per row: name under the Counts/s column, value right-aligned
    // over Input/s and Dead %, so the numbers line up with the channel columns below
    const char * keys[4] = {"Counts/s", "Input/s", "Dead", "Read"};
    const char * tips[4] = {"events recorded per second, all channels, pile-up included",
                            "triggers the channels saw, recorded or not (from the board's 1024-trigger flags)",
                            "share of the input that was not recorded (trigger hold-off, full memory); LOST = the board flagged lost triggers",
                            "bytes read from the board per second, which is the write rate when saving"};
    for( int k = 0; k < 4; k++ ){
      rowID = 3 + k;
      QLabel * lbKey = new QLabel(keys[k], scalar);
      lbKey->setAlignment(Qt::AlignLeft | Qt::AlignVCenter);
      lbKey->setSizePolicy(QSizePolicy::Ignored, QSizePolicy::Preferred);
      lbKey->setToolTip(tips[k]);
      scalarLayout->addWidget(lbKey, rowID, col);
      lbBoardValue[iDigi][k] = new QLabel("", scalar);
      lbBoardValue[iDigi][k]->setAlignment(Qt::AlignRight | Qt::AlignVCenter);
      lbBoardValue[iDigi][k]->setSizePolicy(QSizePolicy::Ignored, QSizePolicy::Preferred);
      lbBoardValue[iDigi][k]->setTextFormat(Qt::RichText);
      lbBoardValue[iDigi][k]->setToolTip(tips[k]);
      scalarLayout->addWidget(lbBoardValue[iDigi][k], rowID, col + 1, 1, 2);
    }

    // file size and readout diagnostics: small and grey; problems get their own red row
    rowID = 7;
    lbFileSize[iDigi] = new QLabel("", scalar);
    lbFileSize[iDigi]->setFont(smallFont);
    lbFileSize[iDigi]->setStyleSheet("color: gray;");
    lbFileSize[iDigi]->setAlignment(Qt::AlignRight | Qt::AlignVCenter);
    lbFileSize[iDigi]->setSizePolicy(QSizePolicy::Ignored, QSizePolicy::Preferred);
    lbFileSize[iDigi]->setToolTip("data written for this run by this board");
    scalarLayout->addWidget(lbFileSize[iDigi], rowID, col, 1, 3);

    rowID = 8;
    lbAggCount[iDigi] = new QLabel("", scalar);
    lbAggCount[iDigi]->setFont(smallFont);
    lbAggCount[iDigi]->setStyleSheet("color: gray;");
    lbAggCount[iDigi]->setAlignment(Qt::AlignRight | Qt::AlignVCenter);
    lbAggCount[iDigi]->setSizePolicy(QSizePolicy::Ignored, QSizePolicy::Preferred);
    lbAggCount[iDigi]->setToolTip("board aggregates decoded and read calls in the last second");
    scalarLayout->addWidget(lbAggCount[iDigi], rowID, col, 1, 3);

    rowID = 9;
    lbProblems[iDigi] = new QLabel("", scalar);
    lbProblems[iDigi]->setFont(smallFont);
    lbProblems[iDigi]->setAlignment(Qt::AlignRight | Qt::AlignVCenter);
    lbProblems[iDigi]->setSizePolicy(QSizePolicy::Ignored, QSizePolicy::Preferred);
    lbProblems[iDigi]->setToolTip("cut: reads that ended inside an aggregate (online decode only; the file is complete)\n"
                                  "bad-ch: aggregates with an impossible channel number\n"
                                  "NOT SAVED: buffers that could not be written to the file");
    scalarLayout->addWidget(lbProblems[iDigi], rowID, col, 1, 3);

    rowID = headRow;
    const char * heads[3] = {"Counts/s", "Input/s", "Dead %"};
    for( int k = 0; k < 3; k++ ){
      QLabel * lb = new QLabel(heads[k], scalar);
      lb->setAlignment(Qt::AlignCenter);
      lb->setFixedWidth(cellW[k]);
      scalarLayout->addWidget(lb, rowID, col + k);
    }

    leTrigger[iDigi] = new QLineEdit *[digi[iDigi]->GetNumInputCh()];
    leAccept[iDigi] = new QLineEdit *[digi[iDigi]->GetNumInputCh()];
    leDead[iDigi] = new QLineEdit *[digi[iDigi]->GetNumInputCh()];

    for( int ch = 0; ch < digi[iDigi]->GetNumInputCh(); ch++){
      rowID ++;
      QLineEdit ** cells[3] = { &leTrigger[iDigi][ch], &leAccept[iDigi][ch], &leDead[iDigi][ch] };
      for( int k = 0; k < 3; k++ ){
        QLineEdit * le = new QLineEdit(scalar);
        le->setReadOnly(true);
        le->setFixedSize(cellW[k], 25);
        le->setAlignment(Qt::AlignRight);
        if( k == 1 ) le->setStyleSheet("background-color: #F0F0F0;");
        *cells[k] = le;
        scalarLayout->addWidget(le, rowID, col + k);
      }
      bool on;
      if( digi[iDigi]->IsInputChEqRegCh() ){
        on = (chMask >> ch) & 0x1;
      }else{
        on = (chMask >> (ch/digi[iDigi]->GetNumRegChannels())) & 0x1;
      }
      leTrigger[iDigi][ch]->setEnabled(on);
      leAccept[iDigi][ch]->setEnabled(on);
      leDead[iDigi][ch]->setEnabled(on);
    }
  }

}

void FSUDAQ::UpdateScalar(){

  DebugPrint("%s", "FSUDAQ");

  // qDebug() << __func__ << "| thread:" << QThread::currentThreadId();

  // printf("================== FSUDAQ::%s\n", __func__);

  if( digi == nullptr ) return;
  if( scalar == nullptr ) return;
  //if( !scalar->isVisible() ) return;
  
  // digi[0]->GetData()->PrintAllData();

  // lbLastUpdateTime->setText("Last update: " + QDateTime::currentDateTime().toString("MM.dd hh:mm:ss"));
  lbLastUpdateTime->setText(QDateTime::currentDateTime().toString("MM/dd hh:mm:ss"));
  scalarCount ++;

  // interval since the previous refresh; the first refresh after a start has no interval
  qint64 dtMs = scalarClock.isValid() ? scalarClock.restart() : -1;
  if( !scalarClock.isValid() ) scalarClock.start();

  uint64_t totalFileSize = 0;
  double totalEventRate = 0, totalBytesRate = 0;
  for( unsigned int iDigi = 0; iDigi < nDigi; iDigi++){
    // printf("======== digi-%d\n", iDigi);
    if( digi[iDigi]->IsBoardDisabled() ) continue;

    uint32_t acqStatus = digi[iDigi]->GetACQStatusFromMemory();
    //printf("Digi-%d : acq on/off ? : %d \n", digi[iDigi]->GetSerialNumber(), (acqStatus >> 2) & 0x1 );
    if( ( acqStatus >> 2 ) & 0x1 ){
      if( runStatus[iDigi]->styleSheet() == "") runStatus[iDigi]->setStyleSheet("background-color : green;");
    }else{
      if( runStatus[iDigi]->styleSheet() != "") runStatus[iDigi]->setStyleSheet("");
    }

    if(digiSettings && digiSettings->isVisible() && digiSettings->GetTabID() == iDigi) digiSettings->UpdateACQStatus(acqStatus);

    // digiMTX[iDigi].lock();

    Data * data = digi[iDigi]->GetData();

    // readout diagnostics, grey; problems in red
    lbAggCount[iDigi]->setText(QString::number(data->AggCount) + " agg · " + QString::number(readDataThread[iDigi]->GetReadCount()) + " reads");
    QStringList problems;
    if( data->DecodeTruncated  > 0 ) problems << "cut " + QString::number(data->DecodeTruncated);   // reads that ended inside an aggregate (online decode only; the file is complete)
    if( data->DecodeBadChannel > 0 ) problems << "bad-ch " + QString::number(data->DecodeBadChannel);
    if( data->SaveFailed > 0 ) problems << "<b>NOT SAVED " + QString::number(data->SaveFailed) + "</b>";
    lbProblems[iDigi]->setText(problems.isEmpty() ? "" : "<font color=red>" + problems.join(" · ") + "</font>");
    readDataThread[iDigi]->SetReadCountZero();
    lbFileSize[iDigi]->setText("written " + QString::number(data->GetTotalFileSize()/1024./1024., 'f', 1) + " MB");

    // per channel: this interval's counts, and the dead-time window
    const int nCh = digi[iDigi]->GetNumInputCh();
    const bool flagsOK = data->lossFlagsAvailable;
    double boardCounts = 0, boardWinCounts = 0, boardWinInput = 0; bool boardLost = false, boardWinValid = false;
    for( int i = 0; i < nCh; i++){
      DeadTimeWindow & w = deadWin[iDigi][i];
      const uint32_t cnt = data->CountsSinceRate[i];
      if( dtMs <= 0 ){ w = DeadTimeWindow(); continue; }   // first refresh after a start: no interval yet
      boardCounts += cnt;
      w.counts += cnt; w.flags += data->Flag1024SinceRate[i]; w.lostFlags += data->LostFlagSinceRate[i]; w.ms += dtMs;
      if( w.flags >= 50 || (w.ms >= 60000 && w.flags >= 10) ){   // enough flags for about ±2 % (at least ±30 % after a minute at a low rate)
        w.inputRate = w.flags * 1024. * 1000. / w.ms;
        w.dead = w.inputRate > 0 ? std::max(0.0, 1.0 - (w.counts * 1000. / w.ms) / w.inputRate) : -1;
        w.lostSeen = w.lostFlags > 0;
        w.valid = true;
        w.counts = 0; w.flags = 0; w.lostFlags = 0; w.ms = 0;
      }else if( w.ms >= 60000 ){   // under ~10 flags a minute (< ~170 triggers/s): too few to say, and dead time is negligible there
        w.valid = false; w.counts = 0; w.flags = 0; w.lostFlags = 0; w.ms = 0;
      }
      if( w.valid && w.inputRate > 0 ){ boardWinInput += w.inputRate; boardWinCounts += w.inputRate * (1 - w.dead); boardWinValid = true; }
      if( w.valid && w.lostSeen ) boardLost = true;
    }

    if( dtMs > 0 ){
      double countsRate = boardCounts * 1000. / dtMs;
      double bytesRate = data->ReadBytesSinceRate * 1000. / dtMs;
      totalEventRate += countsRate;
      totalBytesRate += bytesRate;
      lbBoardValue[iDigi][0]->setText("<b>" + RateText(countsRate) + "</b>");
      if( !flagsOK ){
        lbBoardValue[iDigi][1]->setText("n/a");
        lbBoardValue[iDigi][2]->setText("n/a");
      }else if( !boardWinValid ){
        lbBoardValue[iDigi][1]->setText("…");
        lbBoardValue[iDigi][2]->setText("…");
      }else{
        const double bd = boardWinInput > 0 ? std::max(0.0, 1.0 - boardWinCounts / boardWinInput) : 0;
        const QString col = boardLost || bd > 0.20 ? "red" : (bd > 0.02 ? "#c77700" : "");
        const QString dead = DeadText(bd) + (boardLost ? " LOST" : "");
        lbBoardValue[iDigi][1]->setText(RateText(boardWinInput));
        lbBoardValue[iDigi][2]->setText(col.isEmpty() ? dead : "<font color=" + col + "><b>" + dead + "</b></font>");
      }
      lbBoardValue[iDigi][3]->setText(QString::number(bytesRate / 1e6, 'f', 1) + " MB/s");
    }else{
      for( int k = 0; k < 4; k++ ) lbBoardValue[iDigi][k]->setText("-");
    }

    for( int i = 0; i < nCh; i++){
      if( digi[iDigi]->GetInputChannelOnOff(i) == false ) continue;
      const DeadTimeWindow & w = deadWin[iDigi][i];
      const double countsRate = dtMs > 0 ? data->CountsSinceRate[i] * 1000. / dtMs : -1;
      leTrigger[iDigi][i]->setText(countsRate >= 0 ? RateText(countsRate) : "");
      if( !flagsOK ){
        leAccept[iDigi][i]->setText("n/a");
        leDead[iDigi][i]->setText("n/a");
        leDead[iDigi][i]->setStyleSheet("");
      }else if( !w.valid ){
        leAccept[iDigi][i]->setText(dtMs > 0 ? "…" : "");
        leDead[iDigi][i]->setText(dtMs > 0 ? "…" : "");
        leDead[iDigi][i]->setStyleSheet("");
      }else{
        leAccept[iDigi][i]->setText(RateText(w.inputRate));
        leDead[iDigi][i]->setText(DeadText(w.dead) + (w.lostSeen ? " LOST" : ""));
        leDead[iDigi][i]->setStyleSheet(w.lostSeen || w.dead > 0.20 ? "background-color: #f4b4b4;" : (w.dead > 0.02 ? "background-color: #f8dc9c;" : ""));
      }
      if( influx && chkInflux->isChecked() && countsRate >= 0 ){
        influx->AddDataPoint("TrigRate,Bd="+std::to_string(digi[iDigi]->GetSerialNumber()) + ",Ch=" + QString::number(i).rightJustified(2, '0').toStdString() + " value=" +  QString::number(countsRate, 'f', 2).toStdString());
      }
    }

    digi[iDigi]->GetData()->CalTriggerRate(); //this resets NumEventDecode, AggCount, EventsSinceRate, ReadBytesSinceRate and the Counts/flag counters
    if( chkSaveData->isChecked() ) totalFileSize += digi[iDigi]->GetData()->GetTotalFileSize();
    // digiMTX[iDigi].unlock();
    // printf("============= end of  FSUDAQ::%s\n", __func__);

  }

  QString totalStr = "Total : " + QString::number(totalFileSize/1024./1024., 'f', 1) + " MB";
  if( dtMs > 0 ) totalStr += "   |   <b>" + RateText(totalEventRate) + "</b> Counts/s   |   <b>" + QString::number(totalBytesRate / 1e6, 'f', 1) + "</b> MB/s";
  lbTotalFileSize->setText(totalStr);

  // repaint();
  // scalar->repaint();

  if( influx && chkInflux->isChecked() && scalarCount >= 3){
    if( chkSaveData->isChecked() ) {
      influx->AddDataPoint("RunID value=" + std::to_string(runID));
      influx->AddDataPoint("FileSize value=" + std::to_string(totalFileSize));
    }
    //nflux->PrintDataPoints();
    influx->WriteData(dataBaseName.toStdString());
    influx->ClearDataPointsBuffer();
    scalarCount = 0;
  }

  // printf("end of %s\n", __func__);
  
}

QString FSUDAQ::ElapsedText(qint64 ms){
  if( ms < 0 ) ms = 0;
  qint64 h = ms / 3600000; ms -= h * 3600000;
  qint64 m = ms / 60000;   ms -= m * 60000;
  qint64 s = ms / 1000;    ms -= s * 1000;
  return QString("%1:%2:%3.%4").arg(h).arg(m, 2, 10, QChar('0')).arg(s, 2, 10, QChar('0')).arg(ms, 3, 10, QChar('0'));
}

void FSUDAQ::StartRunClock(const QString & what){
  runClockLabel = what + " started " + QDateTime::currentDateTime().toString("hh:mm:ss") + ", elapsed";
  runClock.start();
  if( lbRunTime ) lbRunTime->setText(runClockLabel + "   <b>0:00:00.000</b>");
  runClockTimer->start();
}

void FSUDAQ::StopRunClock(){
  if( !runClockTimer->isActive() ) return;
  runClockTimer->stop();
  qint64 ms = runClock.elapsed();
  if( lbRunTime ) lbRunTime->setText(runClockLabel.replace(", elapsed", "") + ", stopped after   <b>" + ElapsedText(ms) + "</b>");
  LogMsg("Acquisition time " + ElapsedText(ms) + " (h:mm:ss.ms)");
}

QString FSUDAQ::DeadText(double fraction){
  if( fraction < 0 ) return "-";
  if( fraction < 0.02 ) return "< 2 %";   // the 1024-trigger flags resolve about 2 % (one flag came every 1040 triggers in tests)
  return QString::number(fraction * 100, 'f', 1) + " %";
}

QString FSUDAQ::RateText(double perSecond){
  if( perSecond >= 1e6 ) return QString::number(perSecond / 1e6, 'f', 2) + " M";
  if( perSecond >= 1e3 ) return QString::number(perSecond / 1e3, 'f', 1) + " k";
  return QString::number(perSecond, 'f', 0);
}

void FSUDAQ::CleanUpScalar(){
  DebugPrint("%s", "FSUDAQ");
  if( scalar == nullptr) return;

  scalar->close();

  if( leTrigger == nullptr ) return;

  for( unsigned int i = 0; i < nDigi; i++){
    for( int ch = 0; ch < digi[i]->GetNumInputCh(); ch ++){
      delete leTrigger[i][ch];
      delete leAccept[i][ch];
      delete leDead[i][ch];
    }
    delete [] leTrigger[i];
    delete [] leAccept[i];
    delete [] leDead[i];

  }
  delete [] leTrigger;
  delete [] leAccept;
  delete [] leDead;
  leTrigger = nullptr;
  leAccept = nullptr;
  leDead = nullptr;
  deadWin.clear();

  //Clean up QLabel
  QList<QLabel *> labelChildren = scalar->findChildren<QLabel *>();
  for( int i = 0; i < labelChildren.size(); i++) delete labelChildren[i];

  printf("---- end of %s \n", __func__);

}

void FSUDAQ::OpenScalar(){
  DebugPrint("%s", "FSUDAQ");
  scalar->show();
}

//***************************************************************
//***************************************************************
void FSUDAQ::StartACQ(){
  DebugPrint("%s", "FSUDAQ");
  if( digi == nullptr ) return;

  // the prefix typed in the field applies to this run (it used to take effect one run late)
  if( chkSaveData->isChecked() && lePrefix->text() != prefix ) {
    prefix = lePrefix->text();
    lePrefix->setStyleSheet("");
  }

  bool commentResult = true;
  if( chkSaveData->isChecked()) commentResult = CommentDialog(true);
  if( commentResult == false) return;

  QString runIDStr = QString::number(runID).rightJustified(3, '0');
  QString runDir = RunFolder();

  if( chkSaveData->isChecked() ) {
    // Data files are opened with overwrite, so never start a run whose number is already on disk.
    // This matters when auto-increment is off and a number is typed in by hand, but it also
    // catches a stale lastRun.sh.
    QStringList existing = ExistingRunFiles();
    if( !existing.isEmpty() ){
      QMessageBox box(this);
      box.setIcon(QMessageBox::Warning);
      box.setWindowTitle("Run number already used");
      box.setText("Run-" + runIDStr + " with prefix \"" + prefix + "\" already has " + QString::number(existing.size()) + " file(s) in\n" + runDir + "\n\ne.g. " + existing.first());
      box.setInformativeText("Go back to choose another run number, or delete those files and record this run under the same number.");
      QPushButton * back = box.addButton("Go back", QMessageBox::RejectRole);
      QPushButton * over = box.addButton("Overwrite files and continue", QMessageBox::DestructiveRole);
      box.setDefaultButton(back);
      box.exec();
      if( box.clickedButton() != over ){
        LogMsg("<font style=\"color: red;\">Start Run-" + runIDStr + " not started: " + QString::number(existing.size()) + " file(s) for this prefix and run number already exist in " + runDir + ".</font>");
        if( chkAutoIncrement->isChecked() ) runID --;
        leRunID->setText(QString::number(runID));
        return;
      }
      // remove everything of that run, including files with indices a new run would not reach
      int removed = 0;
      for( const QString & f : existing ){
        if( QFile::remove(runDir + "/" + f) ) removed ++;
        else LogMsg("<font style=\"color: red;\">Cannot remove " + runDir + "/" + f + "</font>");
      }
      LogMsg("<font style=\"color: orange;\">Run-" + runIDStr + ": " + QString::number(removed) + " existing file(s) removed on request; the run is recorded under the same number.</font>");
    }
    // one folder per run for the data files and the settings snapshots; the run
    // record files (RunTimeStamp.dat/.csv, lastRun.sh) stay in the data path
    if( !QDir().mkpath(runDir) ){
      QMessageBox::warning(this, "Cannot create run folder", "Cannot create\n" + runDir + "\n\nAccess rights? The run was not started.");
      LogMsg("<font style=\"color: red;\">Start Run-" + runIDStr + " refused: cannot create " + runDir + ".</font>");
      if( chkAutoIncrement->isChecked() ) runID --;
      leRunID->setText(QString::number(runID));
      return;
    }
    LogMsg("<font style=\"color: orange;\">===================== <b>Start a new Run-" + QString::number(runID) + "</b></font>");
    LogMsg("Run folder : <b>" + runDir + "</b>");
    TellDashboardRunFolder(runDir);
    WriteRunTimestamp(true, QDateTime::currentDateTime().toString("yyyy.MM.dd hh:mm:ss"));
  }else{
    LogMsg("<font style=\"color: orange;\">===================== <b>Start a non-save Run</b></font>");
  }

  //assume master board is the 0-th board
  for( int i = (int) nDigi-1; i >= 0 ; i--){
    if( digi[i]->IsBoardDisabled() ) continue;
    if( chkSaveData->isChecked() ) {
      std::string runSettingName =  (runDir + "/" + prefix + "_" + runIDStr + "_" + QString::number(digi[i]->GetSerialNumber())).toStdString();
      runSettingName += "_" + digi[i]->GetData()->DPPTypeStr + ".bin";
      digi[i]->SaveAllSettingsAsTextForRun(runSettingName);
      digi[i]->GetData()->SetMaxFileSize((uint64_t) sbFileSizeMB->value() * 1024 * 1024);
      if( digi[i]->GetData()->OpenSaveFile((runDir + "/" + prefix + "_" + runIDStr).toStdString()) == false ) {
        LogMsg("Cannot open save file : " + QString::fromStdString(digi[i]->GetData()->GetOutFileName() ) + ". Probably read-only?");
       continue; 
      };
    }
    readDataThread[i]->SetSaveData(chkSaveData->isChecked());
    LogMsg("Digi-" + QString::number(digi[i]->GetSerialNumber()) + " is starting ACQ." );
    digi[i]->WriteRegister(DPP::SoftwareClear_W, 1);

    digi[i]->StartACQ();

    readDataThread[i]->start();
  }
  if( chkSaveData->isChecked() ) SaveLastRunFile();

  // printf("------------ wait for 2 sec \n");
  // usleep(1000*1000);
  // printf("------------ Go! \n");
  // for( unsigned int i = 0; i < nDigi; i++) readDataThread[i]->go();

  // if( scalar ) scalarTimer->start(ScalarUpdateinMiliSec); 
  if( scalar ) scalarTimingThread->start();

  if( !scalar->isVisible() ) {
    scalar->show();
  }else{
    scalar->activateWindow();
  }
  lbScalarACQStatus->setText("<font style=\"color: green;\"><b>ACQ On</b></font>");

  if( singleHistograms ) singleHistograms->startTimer();
  if( onlineAnalyzer ) onlineAnalyzer->startTimer();

  bnStartACQ->setEnabled(false);
  bnOpenDigitizers->setEnabled(false); // "Close Digitizers" only while no run is going
  bnStartACQ->setStyleSheet("");
  bnStopACQ->setEnabled(true);
  bnStopACQ->setStyleSheet("background-color: red;");
  bnOpenScope->setEnabled(false);
  EnableRunLengthControls(false);
  sbFileSizeMB->setEnabled(false);
  bnDashboard->setEnabled(true); // during a run: re-opens a running dashboard, or asks before starting one
  bnSync->setEnabled(false);

  if( digiSettings ) digiSettings->EnableButtons(false);


  {//^=== elog and database
    if( influx && chkInflux->isChecked() ){
      influx->AddDataPoint("RunID value=" + std::to_string(runID));
      if( !elogName.isEmpty() ) influx->AddDataPoint("SavingData,ExpName=" +  elogName.toStdString() + " value=1");
      influx->WriteData(dataBaseName.toStdString());
      influx->ClearDataPointsBuffer();
    }

    if( elogID > 0 && chkElog->isChecked() && chkSaveData->isChecked() ){
      QString msg = "================================= Run-" + QString::number(runID).rightJustified(3, '0') + "<p>" 
                    + QDateTime::currentDateTime().toString("MM.dd hh:mm:ss") + "<p>"
                    + startComment + "<p>"
                    "---------------------------------<p>";
      WriteElog(msg, "Run Log", "Run", runID);
    }
  }

  isACQStarted = true;
  chkSaveData->setEnabled(false);
  // bnDigiSettings->setEnabled(false);

  StartRunClock(chkSaveData->isChecked() ? "Run " + QString::number(runID) : "Run (not saved)");

}

void FSUDAQ::StopACQ(){
  DebugPrint("%s", "FSUDAQ");

  QCoreApplication::processEvents();

  if( digi == nullptr ) return;

  // Stop the boards and close the files before anything else, so the recorded
  // stop time is when acquisition ended and the files stop growing while the
  // operator types the stop comment (the comment dialog comes further down).
  bnStopACQ->setEnabled(false);

  for( unsigned int i = 0; i < nDigi; i++){
    if( digi[i]->IsBoardDisabled() ) continue;
    readDataThread[i]->Stop();
    readDataThread[i]->quit();
    readDataThread[i]->wait();
    digiMTX[i].lock();
    digi[i]->StopACQ();
    digiMTX[i].unlock();
    if( chkSaveData->isChecked() ) digi[i]->GetData()->CloseSaveFile();
    LogMsg("Digi-" + QString::number(digi[i]->GetSerialNumber()) + " ACQ is stopped." );
    QCoreApplication::processEvents();
  }

  QString stopTime = QDateTime::currentDateTime().toString("yyyy.MM.dd hh:mm:ss");

  if( scalarTimingThread->isRunning()){
    scalarTimingThread->Stop();
    scalarTimingThread->quit();
    scalarTimingThread->wait();
  }
  scalarClock.invalidate(); // the next run's first refresh has no interval to rate over

  // if( scalar ) scalarTimer->stop();
  if( singleHistograms ) singleHistograms->stopTimer();
  if( onlineAnalyzer ) onlineAnalyzer->stopTimer();
  
  lbScalarACQStatus->setText("<font style=\"color: red;\"><b>ACQ Off</b></font>");

  bnStartACQ->setEnabled(true);
  bnOpenDigitizers->setEnabled(true); // "Close Digitizers" only while no run is going
  bnStartACQ->setStyleSheet("background-color: green;");
  bnStopACQ->setEnabled(false);
  bnStopACQ->setStyleSheet("");
  bnOpenScope->setEnabled(true);
  EnableRunLengthControls(true);
  sbFileSizeMB->setEnabled(true);
  bnDashboard->setEnabled(true);
  bnSync->setEnabled(true);

  if( scalar ){
    for( unsigned int iDigi = 0; iDigi < nDigi; iDigi ++){
      uint32_t acqStatus = digi[iDigi]->ReadRegister(DPP::AcquisitionStatus_R);
      if( ( acqStatus >> 2 ) & 0x1 ){
        runStatus[iDigi]->setStyleSheet("background-color : green;");
      }else{
        runStatus[iDigi]->setStyleSheet("");
      }
      QCoreApplication::processEvents();
    }
  }

  if( digiSettings ) {
    digiSettings->EnableButtons(true);
    digiSettings->ReadSettingsFromBoard();
  }

  if( chkSaveData->isChecked() ) {
    CommentDialog(false); // acquisition has already stopped; Cancel only means "no comment"
    LogMsg("===================== Stop Run-" + QString::number(runID));
    WriteRunTimestamp(false, stopTime);
  }else{
    LogMsg("===================== Stop a non-save Run");
  }

  {//^=== elog and database
    if( influx && chkInflux->isChecked() && elogName != "" ) {
      if( !elogName.isEmpty() ) influx->AddDataPoint("SavingData,ExpName=" +  elogName.toStdString() + " value=0");
      influx->WriteData(dataBaseName.toStdString());
      influx->ClearDataPointsBuffer();
    }

    if( elogID > 0 && chkElog->isChecked() && chkSaveData->isChecked()){
      QString msg = QDateTime::currentDateTime().toString("MM.dd hh:mm:ss") + "<p>" + stopComment + "<p>";
      uint64_t totalFileSize = 0;
      for(unsigned int i = 0 ; i < nDigi; i++){
        uint64_t fileSize = digi[i]->GetData()->GetTotalFileSize();
        totalFileSize += fileSize;
        msg += "Digi-" + QString::number(digi[i]->GetSerialNumber()) + " Size : " + QString::number(fileSize/1024./1024., 'f', 2) + " MB<p>";
      }

      msg += "..... Total File Size : " + QString::number(totalFileSize/1024./1024., 'f', 2) + "MB<p>" +              
             "=================================<p>";  
      AppendElog(msg);
    }
  }

  chkSaveData->setEnabled(true);
  // bnDigiSettings->setEnabled(true);
  isACQStarted = false;

  StopRunClock();

  repaint();
  // printf("================ end of %s \n", __func__);

}

void FSUDAQ::EnableRunLengthControls(bool enable){
  sbRunTimeMin->setEnabled(enable);
  chkRepeatRun->setEnabled(enable);
  sbRepeatPauseSec->setEnabled(enable && chkRepeatRun->isChecked());
}

QString FSUDAQ::RunLengthText() const {
  return QString::number(sbRunTimeMin->value(), 'g', 6) + " min";
}

void FSUDAQ::AutoRun(){
  DebugPrint("%s", "FSUDAQ");
  runTimer->disconnect(runTimerConnection);

  // the dashboard is started before the boards, never while they run
  if( chkAutoDashboard->isChecked() && !rawDataPath.isEmpty() && digi != nullptr ){
    if( dashboardProc && dashboardProc->state() != QProcess::NotRunning ) AskDashboardToOpenPage();
    else StartDashboardProcess();   // it opens the browser itself once its server is up
  }

  const qint64 runTimeMs = qRound64(sbRunTimeMin->value() * 60. * 1000.);
  if( runTimeMs <= 0 ){ // until Stop
    StartACQ();
    return;
  }

  //---- timed run, optionally repeated
  const bool repeat = chkRepeatRun->isChecked();
  const qint64 pauseMs = qRound64(sbRepeatPauseSec->value() * 1000.);

  needManualComment = true;
  StartACQ();
  if( !isACQStarted ) return; // start was cancelled or refused; nothing to time

  runTimerConnection =  connect( runTimer, &QTimer::timeout, this, [=](){
    needManualComment = false;
    LogMsg("Time Up, Stopping ACQ...");
    StopACQ();
    if( repeat ){

      bnStartACQ->setEnabled(false);
      bnOpenDigitizers->setEnabled(false); // "Close Digitizers" only while no run is going
      bnStartACQ->setStyleSheet("");
      bnStopACQ->setEnabled(true);
      bnStopACQ->setStyleSheet("background-color : red;");

      LogMsg("Wait for " + QString::number(pauseMs / 1000.) + " sec for next Run ...." );
      QElapsedTimer elapsedTimer;
      elapsedTimer.invalidate();
      elapsedTimer.start();
      while( elapsedTimer.elapsed() < pauseMs) {
        
        if( breakAutoRepeat ) {
          LogMsg("Break Auto repeat.");
          bnStartACQ->setEnabled(true);
          bnOpenDigitizers->setEnabled(true); // "Close Digitizers" only while no run is going
          bnStartACQ->setStyleSheet("background-color : green");
          bnStopACQ->setEnabled(false);
          bnStopACQ->setStyleSheet("");
          return;
        }
        QCoreApplication::processEvents();
      }
      
      needManualComment = false;
      StartACQ();
      if( !isACQStarted ) { // e.g. files for the next run number already exist
        LogMsg("Auto repeat stopped: the next run could not be started.");
        bnStartACQ->setEnabled(true);
        bnOpenDigitizers->setEnabled(true); // "Close Digitizers" only while no run is going
        bnStartACQ->setStyleSheet("background-color : green");
        bnStopACQ->setEnabled(false);
        bnStopACQ->setStyleSheet("");
        return;
      }
      runTimer->setSingleShot(true);
      runTimer->start(runTimeMs);
    }
  });

  runTimer->setSingleShot(true);
  runTimer->start(runTimeMs);
  breakAutoRepeat = false;

}

void FSUDAQ::SetSyncMode(){
  DebugPrint("%s", "FSUDAQ");
  QDialog dialog;
  dialog.setWindowTitle("Board Synchronization");

  QVBoxLayout * layout = new QVBoxLayout(&dialog);

  QLabel * lbInfo1 = new QLabel("This will reset 0x8100 and 0x811C \nMaster must be the 1st board.\n (could be 100 ticks offset)", &dialog);
  lbInfo1->setStyleSheet("color : red;");

  QPushButton * bnNoSync = new QPushButton("No Sync");
  QPushButton * bnMethod1 = new QPushButton("Software TRG-OUT --> TRG-IN ");
  QPushButton * bnMethod2 = new QPushButton("Software TRG-OUT --> S-IN ");
  QPushButton * bnMethod3 = new QPushButton("External --> 1st S-IN,\nTRG-OUT --> S-IN ");
  QPushButton * bnMethod4 = new QPushButton("External All S-IN ");

  layout->addWidget(lbInfo1, 0);
  layout->addWidget( bnNoSync, 2);
  layout->addWidget(bnMethod1, 3);
  layout->addWidget(bnMethod2, 4);
  layout->addWidget(bnMethod3, 5);
  layout->addWidget(bnMethod4, 6);

  bnNoSync->setFixedHeight(40);
  bnMethod1->setFixedHeight(40);
  bnMethod2->setFixedHeight(40);
  bnMethod3->setFixedHeight(40);
  bnMethod4->setFixedHeight(40);

  connect(bnNoSync, &QPushButton::clicked, [&](){ /// No Sync
    LogMsg("Set No Sync across digitizers.");
    LogMsg("Software start ACQ, internal clock.");
    for(unsigned int i = 0; i < nDigi; i++){
      digi[i]->WriteRegister(DPP::AcquisitionControl, 0);
      digi[i]->WriteRegister(DPP::FrontPanelIOControl, 0);
    }
    if( digiSettings && digiSettings->isVisible() ) digiSettings->UpdatePanelFromMemory();
    dialog.accept();
  });
  
  connect(bnMethod1, &QPushButton::clicked, [&](){ /// Software TRG-OUT --> TRG-IN
    LogMsg("Set Software TRG-OUT -> TRG-IN");
    LogMsg("Set master saftware ACQ, internal clock.");
    LogMsg("Set slaves TRG-IN, external clock");
    digi[0]->WriteRegister(DPP::AcquisitionControl, 0);
    digi[0]->WriteRegister(DPP::FrontPanelIOControl, 0x10000); //RUN
    for(unsigned int i = 1; i < nDigi; i++){
      digi[i]->WriteRegister(DPP::AcquisitionControl, 0x42);
      digi[i]->WriteRegister(DPP::FrontPanelIOControl, 0x10000); // S-IN
    }
    if( digiSettings && digiSettings->isVisible() ) digiSettings->UpdatePanelFromMemory();
    dialog.accept();
  });
  
  connect(bnMethod2, &QPushButton::clicked, [&](){ /// Software TRG-OUT --> S-IN
    LogMsg("Set Software TRG-OUT -> S-IN");
    LogMsg("Set master saftware ACQ, internal clock.");
    LogMsg("Set slaves S-IN, external clock");
    digi[0]->WriteRegister(DPP::AcquisitionControl, 0);
    digi[0]->WriteRegister(DPP::FrontPanelIOControl, 0x10000); //RUN
    for(unsigned int i = 1; i < nDigi; i++){
      digi[i]->WriteRegister(DPP::AcquisitionControl, 0x41);
      digi[i]->WriteRegister(DPP::FrontPanelIOControl, 0x30000); // S-IN
    }
    if( digiSettings && digiSettings->isVisible() ) digiSettings->UpdatePanelFromMemory();
    dialog.accept();
  });  

  connect(bnMethod3, &QPushButton::clicked, [&](){ ///External TRG-OUT --> S-IN
    LogMsg("Set master External -> S-IN, slave TRG-OUT -> S-IN");
    LogMsg("Set master external S-IN, internal clock.");
    LogMsg("Set slaves S-IN, external clock");
    digi[0]->WriteRegister(DPP::AcquisitionControl, 0x01);
    for(unsigned int i = 0; i < nDigi; i++){
      digi[i]->WriteRegister(DPP::AcquisitionControl, 0x41);
      digi[i]->WriteRegister(DPP::FrontPanelIOControl, 0x30000); // S-IN
    }
    if( digiSettings && digiSettings->isVisible() ) digiSettings->UpdatePanelFromMemory();
    dialog.accept();
  });

  connect(bnMethod4, &QPushButton::clicked, [&](){ /// External All S-IN
    LogMsg("Set all External -> S-IN");
    LogMsg("Set master internal clock, slaves external clock");
    digi[0]->WriteRegister(DPP::AcquisitionControl, 0x01);
    for(unsigned int i = 1; i < nDigi; i++){
      digi[i]->WriteRegister(DPP::AcquisitionControl, 0x41);
    }
    if( digiSettings && digiSettings->isVisible() ) digiSettings->UpdatePanelFromMemory();
    dialog.accept();
  });

  dialog.exec();

}

void FSUDAQ::SetAndLockInfluxElog(){
  DebugPrint("%s", "FSUDAQ");
  if( leInfluxIP->isReadOnly() ){
    bnLock->setText("Lock and Set");

    leInfluxIP->setReadOnly(false);
    leDatabaseName->setReadOnly(false);
    leElogIP->setReadOnly(false);
    leElogName->setReadOnly(false);

    leInfluxIP->setEnabled(true);
    leDatabaseName->setEnabled(true);
    leElogIP->setEnabled(true);
    leElogName->setEnabled(true);

    leInfluxIP->setStyleSheet("color : blue;");
    leDatabaseName->setStyleSheet("color : blue;");
    leElogIP->setStyleSheet("color : blue;");
    leElogName->setStyleSheet("color : blue;");

  }else{
    bnLock->setText("Unlock");

    leInfluxIP->setReadOnly(true);
    leDatabaseName->setReadOnly(true);
    leElogIP->setReadOnly(true);
    leElogName->setReadOnly(true);

    leInfluxIP->setStyleSheet("");
    leDatabaseName->setStyleSheet("");
    leElogIP->setStyleSheet("");
    leElogName->setStyleSheet("");

    influxIP = leInfluxIP->text();
    dataBaseName = leDatabaseName->text();
    elogIP = leElogIP->text();
    elogName = leElogName->text();

    if( !influxIP.isEmpty() && !dataBaseName.isEmpty() ){
      QDialog dialog;
      dialog.setWindowTitle("Database Token");

      QVBoxLayout layout(&dialog);

      QLineEdit tokenLineEdit;
      tokenLineEdit.setFixedSize(1000, 20);
 
      tokenLineEdit.setText(influxToken);

      layout.addWidget(new QLabel("Only for version 2+, version 1+ can be skipped."));
      layout.addWidget(&tokenLineEdit);

      // Buttons for OK and Cancel
      QDialogButtonBox buttonBox(QDialogButtonBox::Ok | QDialogButtonBox::Cancel);
      layout.addWidget(&buttonBox);

      QObject::connect(&buttonBox, &QDialogButtonBox::accepted, &dialog, &QDialog::accept);
      QObject::connect(&buttonBox, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);

      dialog.resize(400, dialog.sizeHint().height()); // Set the width to 400 pixels

      // Show the dialog and get the result
      if (dialog.exec() == QDialog::Accepted) {
          influxToken = tokenLineEdit.text();
      }
    }

    if( !elogIP.isEmpty() && !elogName.isEmpty() ){
      QDialog dialog;
      dialog.setWindowTitle("ELog Login info.");

      QVBoxLayout layout(&dialog);
      QFormLayout formLayout;

      QLineEdit portLineEdit;
      QLineEdit usernameLineEdit;
      QLineEdit passwordLineEdit;
      //passwordLineEdit.setEchoMode(QLineEdit::Password);

      formLayout.addRow("Port:", &portLineEdit);
      formLayout.addRow("Username:", &usernameLineEdit);
      formLayout.addRow("Password:", &passwordLineEdit);

      portLineEdit.setText(elogPort);
      usernameLineEdit.setText(elogUser);
      passwordLineEdit.setText(elogPWD);

      layout.addLayout(&formLayout);

      // Buttons for OK and Cancel
      QDialogButtonBox buttonBox(QDialogButtonBox::Ok | QDialogButtonBox::Cancel);
      layout.addWidget(&buttonBox);

      QObject::connect(&buttonBox, &QDialogButtonBox::accepted, &dialog, &QDialog::accept);
      QObject::connect(&buttonBox, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);

      dialog.resize(400, dialog.sizeHint().height()); // Set the width to 400 pixels

      // Show the dialog and get the result
      if (dialog.exec() == QDialog::Accepted) {
        QString portNum = portLineEdit.text();
        QString username = usernameLineEdit.text();
        QString password = passwordLineEdit.text();

        // Check if username and password are not empty
        if (!portNum.isEmpty() &&  !username.isEmpty() && !password.isEmpty()) {
          elogPort = portNum;
          elogUser = username;
          elogPWD = password;

        } else {
          qDebug() << "Please enter both port, username, and password.";
        }
      }

    }

    SaveProgramSettings();

    SetUpInflux();
    CheckElog();    

  }
}

bool FSUDAQ::CommentDialog(bool isStartRun){
  DebugPrint("%s", "FSUDAQ");
  bool incremented = false;
  if( isStartRun ) {
    if( chkAutoIncrement->isChecked() ){
      runID ++;
      incremented = true;
    }else{
      runID = leRunID->text().toUInt(); // the operator chose the number
    }
  }
  QString runIDStr = QString::number(runID).rightJustified(3, '0');

  int result = QDialog::Rejected ;
  QLineEdit *lineEdit = new QLineEdit(this);

  // needManualComment is false for the automatic start/stop of timed runs;
  // the checkbox lets the operator skip the dialog for manual runs too.
  bool askForComment = needManualComment && !chkSkipComment->isChecked();

  if( askForComment ) {
    QDialog * dOpen = new QDialog(this);
    if( isStartRun ) {
      dOpen->setWindowTitle("Start Run Comment");
    }else{
      dOpen->setWindowTitle("Stop Run Comment");
    }
    dOpen->setWindowFlags(Qt::Dialog | Qt::WindowTitleHint | Qt::CustomizeWindowHint);
    dOpen->setMinimumWidth(600);
    connect(dOpen, &QDialog::finished, dOpen, &QDialog::deleteLater);

    QGridLayout * vlayout = new QGridLayout(dOpen);
    QLabel *label = new QLabel("Enter Run comment for <font style=\"color : red;\">Run-" +  runIDStr + "</font> : ", dOpen);    
    QPushButton *button1 = new QPushButton("OK", dOpen);
    QPushButton *button2 = new QPushButton("Cancel", dOpen);

    vlayout->addWidget(label, 0, 0, 1, 2);
    vlayout->addWidget(lineEdit, 1, 0, 1, 2);
    vlayout->addWidget(button1, 2, 0);
    vlayout->addWidget(button2, 2, 1);

    connect(button1, &QPushButton::clicked, dOpen, &QDialog::accept);
    connect(button2, &QPushButton::clicked, dOpen, &QDialog::reject);
    button1->setDefault(true); // Enter in the text field = OK, so an empty comment starts the run
    lineEdit->setFocus();
    result = dOpen->exec();
  }else{
    if( !needManualComment ){ // automatic start/stop of a timed run
      if( isStartRun ){
        lineEdit->setText("Auto Start, repeat every " + RunLengthText() + ".");
      }else{
        lineEdit->setText("Auto Stop, after " + RunLengthText() + ".");
      }
    } // otherwise the dialog was skipped: empty comment
    result = QDialog::Accepted;
  }

  if(result == QDialog::Accepted ){
    if( isStartRun ){
      startComment = lineEdit->text();
      if( startComment == "") startComment = "no comment";
      
      if( needManualComment && sbRunTimeMin->value() > 0 ){
        if( chkRepeatRun->isChecked() ) {
          startComment += ", repeat run of " + RunLengthText() + ", " + QString::number((int) sbRepeatPauseSec->value()) + " s pause.";
        }else{
          startComment += ", single run of " + RunLengthText() + ".";
        }
      }
      startComment = "Start Comment: " + startComment;
      leComment->setText(startComment);
      leRunID->setText(QString::number(runID));
    }else{
      stopComment = lineEdit->text();
      if( stopComment == "") stopComment = "no comment";
      stopComment = "Stop Comment: " + stopComment;
      leComment->setText(stopComment);
    }
    
  }else{

    if( isStartRun ){
      LogMsg("Start Run aborted. ");
      if( incremented ) runID --;
      leRunID->setText(QString::number(runID));
    }else{
      // the boards are already stopped when this dialog is shown, so Cancel cannot undo the stop
      LogMsg("Stop comment cancelled; recorded as \"no comment\".");
      stopComment = "Stop Comment: no comment";
      leComment->setText(stopComment);
    }
    return false;

  }

  return true;

}

QString FSUDAQ::RunFolder() const {
  return rawDataPath + "/" + prefix + "_" + QString::number(runID).rightJustified(3, '0');
}

QStringList FSUDAQ::ExistingRunFiles() const {
  // Everything StartACQ would write for this run starts with <prefix>_<run>_ :
  // the per-board settings snapshots (.bin) and the data files (.fsu).
  QDir dir(RunFolder());
  if( !dir.exists() ) return QStringList();
  QString pattern = prefix + "_" + QString::number(runID).rightJustified(3, '0') + "_*";
  return dir.entryList(QStringList() << pattern, QDir::Files, QDir::Name);
}

void FSUDAQ::WriteRunTimestamp(bool isStartRun, const QString & timeStamp){
  DebugPrint("%s", "FSUDAQ");
  QFile file(rawDataPath + "/RunTimeStamp.dat");
  
  QString dateTime = timeStamp;
  if( file.open(QIODevice::Text | QIODevice::WriteOnly | QIODevice::Append) ){

    if( isStartRun ){
      file.write(("Start Run | " + QString::number(runID) + " | " + dateTime + " | " + lePrefix->text() + " | " + startComment + "\n").toStdString().c_str());
    }else{
      file.write((" Stop Run | " + QString::number(runID) + " | " + dateTime + " | " + lePrefix->text() + " | " + stopComment + "\n").toStdString().c_str());
    }
    
    file.close();
  }


  QFile fileCSV(rawDataPath +  "/RunTimeStamp.csv");

  if( fileCSV.open(QIODevice::Text | QIODevice::WriteOnly | QIODevice::Append) ){

    QTextStream out(&fileCSV);

    if( isStartRun){
      out << QString::number(runID) + "," + dateTime + "," + startComment;
    }else{
      out << "," + dateTime + "," + stopComment + "\n";
    }

    fileCSV.close();
  }

  UpdateRecord();

}

//***************************************************************
//***************************************************************
void FSUDAQ::OpenDashboard(){
  DebugPrint("%s", "FSUDAQ");

  if( dashboardProc && dashboardProc->state() != QProcess::NotRunning ){
    AskDashboardToOpenPage();   // already running: it shows the page; FSUDAQ spawns nothing
    return;
  }
  if( isACQStarted ){
    // Starting a child process during a run twice coincided with a kernel panic in the
    // a3818 driver (a driver defect, patched on odin 29 Sep 2026: it now logs
    // "a3818: dispatch_pkt: link 4 does not exist" instead). Ask, and say what to check.
    int r = QMessageBox::question(this, "Online Dashboard",
              "Acquisition is running. Starting the dashboard now spawns a process while the optical links are busy, "
              "which on an unpatched a3818 driver crashed the host.\n\nThe dashboard keeps following the data path "
              "across runs, so the safe way is to start it before a run.\n\nStart it now anyway? (afterwards check "
              "dmesg for 'a3818: dispatch_pkt')", QMessageBox::Yes | QMessageBox::No, QMessageBox::No);
    if( r != QMessageBox::Yes ) return;
    LogMsg("<font style=\"color: orange;\">Dashboard started during acquisition on request; check dmesg for 'a3818: dispatch_pkt' afterwards.</font>");
  }
  StartDashboardProcess();     // it opens the browser itself once its server is up
}

void FSUDAQ::AskDashboardToOpenPage(){
  // Starting any child process from FSUDAQ while the boards stream breaks the board reads
  // (29 Sep 2026), so the browser is opened by the dashboard process on request.
  QNetworkReply * r = net->get(QNetworkRequest(QUrl("http://localhost:8050/open")));
  connect(r, &QNetworkReply::finished, r, &QNetworkReply::deleteLater);
}

void FSUDAQ::TellDashboardRunFolder(const QString & folder, int attempt){
  // The dashboard follows the run being recorded (data path + prefix + run number), also when a
  // run number is recorded again in the same folder. HTTP only: nothing is spawned during a run.
  QNetworkReply * r = net->get(QNetworkRequest(QUrl("http://localhost:8050/run?folder=" + QString::fromLatin1(QUrl::toPercentEncoding(folder)))));
  connect(r, &QNetworkReply::finished, this, [=](){
    // a dashboard started just before this run may not be listening yet: retry for a few seconds
    if( r->error() == QNetworkReply::ConnectionRefusedError && attempt < 10 && dashboardProc && dashboardProc->state() != QProcess::NotRunning ){
      QTimer::singleShot(1000, this, [=](){ TellDashboardRunFolder(folder, attempt + 1); });
    }
    r->deleteLater();
  });
}

bool FSUDAQ::StartDashboardProcess(){
  if( dashboardProc && dashboardProc->state() != QProcess::NotRunning ) return true;
  if( rawDataPath.isEmpty() ){
    LogMsg("<font style=\"color: red;\">Set the data path first; the dashboard follows the newest run folder in it.</font>");
    return false;
  }
  QString dir = QDir::current().absolutePath() + "/online";
  QString script = dir + "/online_dashboard.py";
  if( !QFile::exists(script) ){
    LogMsg("<font style=\"color: red;\">" + script + " not found.</font>");
    return false;
  }
  QString python = QFile::exists(dir + "/venv/bin/python") ? dir + "/venv/bin/python" : "python3";

  if( dashboardProc == nullptr ){
    dashboardProc = new QProcess(this);
    dashboardProc->setProcessChannelMode(QProcess::MergedChannels);
#if QT_VERSION >= QT_VERSION_CHECK(6, 6, 0)
    // vfork: no copy of this process's page tables (a fork() during a run stalls the
    // optical-link DMA copies into our buffers and overruns the driver); and the child
    // must not inherit the CAEN device descriptors or anything else of ours
    dashboardProc->setUnixProcessParameters(QProcess::UnixProcessFlag::UseVFork | QProcess::UnixProcessFlag::CloseFileDescriptors);
#endif
    connect(dashboardProc, &QProcess::readyReadStandardOutput, this, [=](){
      for( const QByteArray & line : dashboardProc->readAllStandardOutput().split('\n') ){
        if( !line.trimmed().isEmpty() ) LogMsg("[dashboard] " + QString::fromUtf8(line.trimmed()));
      }
    });
    connect(dashboardProc, &QProcess::finished, this, [=](int code, QProcess::ExitStatus){
      LogMsg("[dashboard] stopped (exit code " + QString::number(code) + ").");
    });
  }
  dashboardProc->setWorkingDirectory(dir);
  QStringList args = QStringList() << script << "--data-path" << rawDataPath << "--port" << "8050" << "--open-browser";
  if( chkSaveData->isChecked() && QDir(RunFolder()).exists() ) args << "--follow" << RunFolder(); // the current or last run; each new run is sent with GET /run
  dashboardProc->start(python, args);
  if( !dashboardProc->waitForStarted(3000) ){
    LogMsg("<font style=\"color: red;\">Cannot start " + python + " " + script + ".</font>");
    return false;
  }
  LogMsg("[dashboard] started with " + python + " on " + rawDataPath + " (http://localhost:8050/)");
  return true;
}

void FSUDAQ::OpenScope(){
  DebugPrint("%s", "FSUDAQ");
  QCoreApplication::processEvents();
  if( scope == nullptr ) {
    scope = new Scope(digi, nDigi, readDataThread);
    connect(scope, &Scope::SendLogMsg, this, &FSUDAQ::LogMsg);
    
    connect(scope, &Scope::CloseWindow, this, [=](){
      bnStartACQ->setEnabled(true);
      bnOpenDigitizers->setEnabled(true); // "Close Digitizers" only while no run is going
      bnStartACQ->setStyleSheet("background-color: green;");
      bnStopACQ->setEnabled(false);  
      bnStopACQ->setStyleSheet("");
    });

    connect(scope, &Scope::TellACQOnOff, this, [=](bool onOff){

      isACQStarted = onOff;

      if( onOff ){
        if( influx && chkInflux->isChecked() && !elogName.isEmpty()) influx->AddDataPoint("SavingData,ExpName=" +  elogName.toStdString() + " value=1");
        
        if( scalar ){
          lbScalarACQStatus->setText("<font style=\"color: green;\"><b>ACQ On</b></font>");
          // scalarTimer->start(ScalarUpdateinMiliSec); 
          scalarTimingThread->start();
        }
        StartRunClock("Scope");

        if( singleHistograms ) singleHistograms->startTimer();
        if( onlineAnalyzer ) onlineAnalyzer->startTimer();

      }else{
        if( influx && chkInflux->isChecked() && !elogName.isEmpty()) influx->AddDataPoint("SavingData,ExpName=" +  elogName.toStdString() + " value=0");

        if( scalar ){
          lbScalarACQStatus->setText("<font style=\"color: red;\"><b>ACQ Off</b></font>");
          // scalarTimer->stop(); 
          scalarTimingThread->Stop();
          scalarTimingThread->quit();
          scalarTimingThread->wait();
        }
        StopRunClock();

        if( singleHistograms ) singleHistograms->stopTimer();
        if( onlineAnalyzer ) onlineAnalyzer->stopTimer();

      }

      if( digiSettings ) digiSettings->EnableButtons(!onOff);

    });

    connect(scope, &Scope::UpdateOtherPanels, this, [=](){ UpdateAllPanels(1); });

    scope->show();
  }else{
    scope->show();
    scope->UpdatePanelFromMomeory();
    scope->activateWindow();
  }

  bnStartACQ->setEnabled(false);
  bnOpenDigitizers->setEnabled(false); // "Close Digitizers" only while no run is going
  bnStartACQ->setStyleSheet("");
  bnStopACQ->setEnabled(false);  
  bnStopACQ->setStyleSheet("");
  chkSaveData->setChecked(false);

}

//***************************************************************
//***************************************************************
void FSUDAQ::OpenDigiSettings(){
  DebugPrint("%s", "FSUDAQ");
  if( digiSettings == nullptr ) {
    digiSettings = new DigiSettingsPanel(digi, nDigi, settingsPath);   // its save/load dialogs start in the settings path
    //connect(scope, &Scope::SendLogMsg, this, &FSUDAQ::LogMsg);
    connect(digiSettings, &DigiSettingsPanel::UpdateOtherPanels, this, [=](){ UpdateAllPanels(2); });
    connect(digiSettings, &DigiSettingsPanel::SendLogMsg, this, &FSUDAQ::LogMsg);

    digiSettings->show();
  }else{
    digiSettings->show();
    digiSettings->UpdatePanelFromMemory();
    digiSettings->activateWindow();
  }
}

//***************************************************************
//***************************************************************
//***************************************************************
//***************************************************************
void FSUDAQ::OpenAnalyzer(){
  DebugPrint("%s", "FSUDAQ");

  int id = cbAnalyzer->currentData().toInt();

  if( id < 0 ) return;

  if( onlineAnalyzer == nullptr ) {
    if( id == 0 ) onlineAnalyzer = new CoincidentAnalyzer(digi, nDigi, rawDataPath);
    if( id == 1 ) onlineAnalyzer = new SplitPole(digi, nDigi);
    if( id == 2 ) onlineAnalyzer = new Encore(digi, nDigi);
    if( id == 3 ) onlineAnalyzer = new MUSIC(digi, nDigi);
    if( id == 4 ) onlineAnalyzer = new NeutronGamma(digi, nDigi, rawDataPath);

    if( id == 5 ) onlineAnalyzer = new Cross(digi, nDigi);

    if( id >=  0 ) onlineAnalyzer->show();

    if( isACQStarted ) onlineAnalyzer->startTimer();

  }else{

    delete onlineAnalyzer;
  
    if( id == 0 ) onlineAnalyzer = new CoincidentAnalyzer(digi, nDigi, rawDataPath);
    if( id == 1 ) onlineAnalyzer = new SplitPole(digi, nDigi);
    if( id == 2 ) onlineAnalyzer = new Encore(digi, nDigi);
    if( id == 3 ) onlineAnalyzer = new MUSIC(digi, nDigi);
    if( id == 4 ) onlineAnalyzer = new NeutronGamma(digi, nDigi, rawDataPath);
    
    if( id == 5 ) onlineAnalyzer = new Cross(digi, nDigi);

    if( id >= 0 ){
      onlineAnalyzer->show();
      onlineAnalyzer->activateWindow();
      if( isACQStarted ) onlineAnalyzer->stopTimer();
    }
  }

  cbAnalyzer->setCurrentIndex(0);

}

//***************************************************************
//***************************************************************
void FSUDAQ::UpdateAllPanels(int panelID){
  DebugPrint("%s", "FSUDAQ");
  //panelID is the source panel that call
  // scope = 1;
  // digiSetting = 2;

  if( panelID == 1 ){ // from scope
    if( digiSettings && digiSettings->isVisible() ) digiSettings->UpdatePanelFromMemory();
    if( scalar ) {
      for( unsigned int iDigi = 0; iDigi < nDigi; iDigi++){
        if( digi[iDigi]->IsBoardDisabled() ) continue;

        uint32_t acqStatus = digi[iDigi]->GetACQStatusFromMemory(); 
        if( ( acqStatus >> 2 ) & 0x1 ){
          runStatus[iDigi]->setStyleSheet("background-color : green;");
        }else{
          runStatus[iDigi]->setStyleSheet("");
        }
      }
    }
  }

  if( panelID == 2 ){
    if(scope && scope->isVisible() ) scope->UpdatePanelFromMomeory();

    if(scalar) {
      for( unsigned int iDigi = 0; iDigi < nDigi; iDigi++){
        uint32_t chMask =  digi[iDigi]->GetRegChannelMask();
        uint32_t subChMask = 0;
        for( int ch = 0; ch < digi[iDigi]->GetNumInputCh(); ch++){
          // leTrigger[iDigi][i]->setEnabled( (chMask >> i) & 0x1 );
          // leAccept[iDigi][i]->setEnabled( (chMask >> i) & 0x1 );

          if( digi[iDigi]->IsInputChEqRegCh() ){
            leTrigger[iDigi][ch]->setEnabled( (chMask >> ch) & 0x1 );
            leAccept[iDigi][ch]->setEnabled( (chMask >> ch) & 0x1 );
          }else{
            int grpID = ch/digi[iDigi]->GetNumRegChannels();
            leTrigger[iDigi][ch]->setEnabled( (chMask >> grpID) & 0x1 );
            leAccept[iDigi][ch]->setEnabled( (chMask >> grpID) & 0x1 );

            if( (chMask >> grpID ) & 0x1 ){

              int subCh = ch%digi[iDigi]->GetNumRegChannels();
              if( subCh == 0 ) subChMask = digi[iDigi]->GetSettingFromMemory(DPP::QDC::SubChannelMask, grpID);

              leTrigger[iDigi][ch]->setEnabled( (subChMask >> subCh) & 0x1 );
              leAccept[iDigi][ch]->setEnabled( (subChMask >> subCh) & 0x1 );

            }
          }

        }
      } 
    }
  }

}

//***************************************************************
//***************************************************************
void FSUDAQ::SetUpInflux(){
  DebugPrint("%s", "FSUDAQ");
  if( influxIP == "" ) {
    LogMsg("<font style=\"color : red;\">Influx missing inputs. skip.</font>");
    leInfluxIP->setEnabled(false);
    leDatabaseName->setEnabled(false);
    return;
  }

  if( influx ) {
    delete influx;
    influx = nullptr;
  }

  influx = new InfluxDB(influxIP.toStdString(), false);

  if( influx->TestingConnection() ){
    LogMsg("<font style=\"color : green;\"> InfluxDB URL (<b>"+ influxIP + "</b>) is Valid. Version : " + QString::fromStdString(influx->GetVersionString())+ " </font>");

    if( influx->GetVersionNo() > 1 && influxToken.isEmpty() ) {
      LogMsg("<font style=\"color : red;\">A Token is required for accessing the database.</font>");
      delete influx;
      influx = nullptr;
      return;
    }

    influx->SetToken(influxToken.toStdString());

    //==== chck database exist
    influx->CheckDatabases();
    std::vector<std::string> databaseList = influx->GetDatabaseList();
    bool foundDatabase = false;
    for( int i = 0; i < (int) databaseList.size(); i++){
      if( databaseList[i] == dataBaseName.toStdString() ) foundDatabase = true;
      // LogMsg(QString::number(i) + "|" + QString::fromStdString(databaseList[i]));
    }
    if( foundDatabase ){
      LogMsg("<font style=\"color : green;\"> Database <b>" + dataBaseName + "</b> found.");
      influx->AddDataPoint("ProgramStart value=1");
      influx->WriteData(dataBaseName.toStdString());
      influx->ClearDataPointsBuffer();
      if( influx->IsWriteOK() ){
        LogMsg("<font style=\"color : green;\">test write database OK.</font>");
      }else{
        LogMsg("<font style=\"color : red;\">test write database FAIL.</font>");
      }
    }else{
      LogMsg("<font style=\"color : red;\"> Database <b>" + dataBaseName + "</b> NOT found.");
      delete influx;
      influx = nullptr;
    }
  }else{
    LogMsg("<font style=\"color : red;\"> InfluxDB URL (<b>"+ influxIP + "</b>) is NOT Valid </font>");
    delete influx;
    influx = nullptr;
  }

  if( influx == nullptr ){
    leInfluxIP->setEnabled(false);
    leDatabaseName->setEnabled(false);
  }

}

void FSUDAQ::CheckElog(){
  elogID = -1;
  DebugPrint("%s", "FSUDAQ");
  if( !chkElog->isChecked() ) {
    LogMsg("Elog is disabled. Please chick the checkbox and lock again to check elog connectivity.");
    leElogIP->setEnabled(false);
    leElogName->setEnabled(false);
    return;
  }

  LogMsg("---- Checking elog... please wait....");
  // printf("---- Checking elog... please wait....\n");
  if( elogIP != "" && elogName != "" &&  elogUser != "" && elogPWD != "" ){
    WriteElog("Testing communication.", "Testing communication.", "Other", 0);
    AppendElog("test append elog.");
  }else{
    LogMsg("<font style=\"color : red;\">Elog missing inputs. skip.</font>");
    leElogIP->setEnabled(false);
    leElogName->setEnabled(false);
    return;
  }

  if( elogID >= 0 ) {
    LogMsg("Elog testing OK.");
    // printf("Elog testing OK.\n");
    return;
  }

  //QMessageBox::information(nullptr, "Information", "Elog write Fail.\nPlease set the elog User and PWD in the programSettings.txt.\nline 6 = user.\nline 7 = pwd.");
  LogMsg("Elog testing Fail");
  // printf("Elog testing Fail\n");
  if( elogIP == "" ) LogMsg("no elog IP");
  if( elogName == "" ) LogMsg("no elog Name");
  if( elogUser == "" ) LogMsg("no elog User name. Please set it in the programSettings.txt, line 6.");
  if( elogPWD == "" ) LogMsg("no elog User pwd. Please set it in the programSettings.txt, line 7.");
  if( elogID < 0 ) LogMsg("Possible elog IP, Name, User name, pwd incorrect, or elog not installed.");
  leElogIP->setEnabled(false);
  leElogName->setEnabled(false);
  
}

void FSUDAQ::WriteElog(QString htmlText, QString subject, QString category, int runNumber){
  DebugPrint("%s", "FSUDAQ");
  //if( elogID < 0 ) return;
  if( !chkElog->isChecked() ) return;
  if( elogName == "" ) return;
  if( elogUser == "" ) return;
  if( elogPWD == "" ) return;
  QStringList arg;
  arg << "-h" << elogIP << "-p" << elogPort << "-l" << elogName << "-u" << elogUser << elogPWD << "-a" << "Author=FSUDAQ";
  if( runNumber > 0 ) arg << "-a" << "RunNo=" + QString::number(runNumber);
  if( category != "" ) arg << "-a" << "Category=" + category;
  arg << "-a" << "Subject=" + subject 
      << "-n " << "2" <<  htmlText  ;
  QProcess elogBash(this);
  elogBash.start("elog", arg); 
  elogBash.waitForFinished();
  QString output = QString::fromUtf8(elogBash.readAllStandardOutput());

  QRegularExpression regex("ID=(\\d+)");

  QRegularExpressionMatch match = regex.match(output);
  if (match.hasMatch()) {
    QString id = match.captured(1);
    elogID = id.toInt();
  } else {
    elogID = -1;
  }

}

void FSUDAQ::AppendElog(QString appendHtmlText){
  DebugPrint("%s", "FSUDAQ");
  if( !chkElog->isChecked() )return;
  if( elogID < 1 ) return;
  if( elogName == "" ) return;
  
  QProcess elogBash(this);
  QStringList arg;
  arg << "-h" << elogIP << "-p" << elogPort << "-l" << elogName << "-u" << elogUser << elogPWD << "-w" << QString::number(elogID);
  //retrevie the elog
  elogBash.start("elog", arg); 
  elogBash.waitForFinished();
  QString output = QString::fromUtf8(elogBash.readAllStandardOutput());
  //qDebug() << output;
  QString separator = "========================================";
  int index = output.indexOf(separator);
  if( index != -1){
    QString originalHtml = output.mid(index + separator.length());
    arg.clear();
    arg << "-h" << elogIP << "-p" << elogPort << "-l" << elogName << "-u" << elogUser << elogPWD << "-e" << QString::number(elogID)
        << "-n" << "2" << originalHtml + "<br>" + appendHtmlText;
    
    elogBash.start("elog", arg); 
    elogBash.waitForFinished();
    output = QString::fromUtf8(elogBash.readAllStandardOutput());
    index = output.indexOf("ID=");
    if( index != -1 ){
      elogID = output.mid(index+3).toInt();
    }else{
      elogID = -1;
    }
  }else{
    elogID = -1;
  }
}

//***************************************************************
//***************************************************************
void FSUDAQ::LogMsg(QString msg){
  DebugPrint("%s", "FSUDAQ");
  QString outputStr = QStringLiteral("[%1] %2").arg(QDateTime::currentDateTime().toString("MM.dd hh:mm:ss"), msg);
  if( logMsgHTMLMode ){ 
    logInfo->appendHtml(outputStr);
  }else{
    logInfo->appendPlainText(outputStr);
  }
  QScrollBar *v = logInfo->verticalScrollBar();
  v->setValue(v->maximum());
  //qDebug() << outputStr;
  logInfo->repaint();
}
