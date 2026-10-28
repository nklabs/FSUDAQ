// FSUDAQ's run control, run off-screen against six fake boards (tests/fake_caen.cpp).
//
// Opens the digitizers "w/ settings" from tests/data/odin-runB, takes runs and checks what
// ends up on disk against what the boards handed out, the run records, the run numbers and
// the dialogs FSUDAQ shows (answered automatically: the default button, else the first).
//
//   QT_QPA_PLATFORM=offscreen test_daq [fixture dir]

#include <QApplication>
#include <QDialog>
#include <QDir>
#include <QElapsedTimer>
#include <QFile>
#include <QLabel>
#include <QFileInfo>
#include <QMessageBox>
#include <QPushButton>
#include <QTextStream>
#include <QTimer>
#include <QScrollArea>
#include <QMap>
#include <thread>

#include <functional>
#include <unistd.h>
#include <sys/stat.h>
#include <sys/resource.h>
#include <csignal>

#include "FSUDAQ.h"
#include "fake_caen.h"

static int nFail = 0;
#define CHECK(cond, ...) do{ if( !(cond) ){ nFail++; printf("   FAIL  "); printf(__VA_ARGS__); printf("\n"); fflush(stdout); } }while(0)
#define OK(...) do{ printf("   ok    "); printf(__VA_ARGS__); printf("\n"); fflush(stdout); }while(0)

static const int Serials[6] = {15880, 15879, 58918, 62839, 1443, 30508}; // port order on odin
static QStringList dialogs;   // "title: text" of every dialog answered
static QMap<QString, QString> answers;   // dialog title -> the button to press (else the default)

class FSUDAQTest {
public:
  QString fixtures, work, dataPath, settingsPath;
  FSUDAQ * w = nullptr;

  static void Pump(int ms){
    QElapsedTimer t; t.start();
    while( t.elapsed() < ms ){ QCoreApplication::processEvents(QEventLoop::AllEvents, 5); usleep(2000); }
  }
  static bool WaitFor(std::function<bool()> cond, int ms){
    QElapsedTimer t; t.start();
    while( t.elapsed() < ms ){ if( cond() ) return true; QCoreApplication::processEvents(QEventLoop::AllEvents, 5); usleep(2000); }
    return cond();
  }
  static int Dialogs(const QString & title){ int n = 0; for( auto & d : dialogs ) if( d.startsWith(title + ":") ) n++; return n; }

  // six boards like odin's, generating eventsPerCouple events per couple and aggregate
  void AddBoards(int eventsPerCouple = 100, double aggPerMs = 2){
    fake_caen_reset_all();
    for( int i = 0; i < 6; i++ ){
      const char * model = Serials[i] == 1443 ? "DT5730B" : Serials[i] == 30508 ? "DT5730S" : "VX1730SB";   // as on odin
      QString image = fixtures + "/Digi-" + QString::number(Serials[i]) + "_PSD.bin";
      fake_caen_add_board(1, i, 0, Serials[i], model, image.toStdString().c_str());
      fake_caen_set_synthetic(Serials[i], eventsPerCouple, aggPerMs);
    }
  }

  void NewWorkDir(){
    work = QDir::tempPath() + "/fsudaq_test_daq_" + QString::number(getpid());
    QDir(work).removeRecursively();
    dataPath = work + "/data";
    settingsPath = work + "/settings";
    QDir().mkpath(dataPath);
    QDir().mkpath(settingsPath);
    for( int s : Serials ) QFile::copy(fixtures + "/Digi-" + QString::number(s) + "_PSD.bin", settingsPath + "/Digi-" + QString::number(s) + "_PSD.bin");
    QFile f(work + "/programSettings.txt");
    f.open(QIODevice::WriteOnly | QIODevice::Text);
    QTextStream out(&f);
    out << dataPath << "\n\n\n\n\n\n\n\n\n";   // data path; no influx, no elog
    out << "settingsPath=" << settingsPath << "\nautoIncrementRunID=1\nautoDashboard=0\n";
    f.close();
    QDir::setCurrent(work);
  }

  void NewFSUDAQ(){
    delete w;
    w = new FSUDAQ();
    w->chkSkipComment->setChecked(true);
    w->chkAutoDashboard->setChecked(false);
    w->chkAutoIncrement->setChecked(true);   // an earlier scenario may have left it off
  }

  void Open(){
    w->cbOpenMethod->setCurrentIndex(w->cbOpenMethod->findData(1)); // w/ settings
    w->OpenDigitizers();
    w->UpdateRunControls();
  }

  // the run folder's data files of one board, concatenated, as 32-bit words
  std::vector<uint32_t> Saved(const QString & runDir, int serial){
    QStringList files = QDir(runDir).entryList(QStringList() << ("*_" + QString::number(serial) + "_PSD_2_*.fsu"), QDir::Files, QDir::Name);
    std::vector<uint32_t> all;
    for( auto & name : files ){
      QFile f(runDir + "/" + name);
      f.open(QIODevice::ReadOnly);
      QByteArray b = f.readAll();
      size_t n = b.size() / 4;
      size_t old = all.size();
      all.resize(old + n);
      memcpy(all.data() + old, b.constData(), n * 4);
    }
    return all;
  }
  int NumFiles(const QString & runDir, int serial){
    return QDir(runDir).entryList(QStringList() << ("*_" + QString::number(serial) + "_PSD_2_*.fsu"), QDir::Files).size();
  }
  QStringList RecordLines(){
    QFile f(dataPath + "/RunTimeStamp.dat");
    if( !f.open(QIODevice::ReadOnly | QIODevice::Text) ) return QStringList();
    return QString(f.readAll()).split("\n", Qt::SkipEmptyParts);
  }
  // every board: the run folders, concatenated, are exactly what the board handed out, and the
  // board was read out to the end: nothing it had taken was dropped or cleared
  void CheckAllSaved(const QStringList & runDirs, const char * what){
    for( int s : Serials ){
      std::vector<uint32_t> saved;
      for( auto & d : runDirs ){ auto part = Saved(d, s); saved.insert(saved.end(), part.begin(), part.end()); }
      const uint32_t * out; size_t n = fake_caen_handed_out(s, &out);
      fake_caen_stats st; fake_caen_get_stats(s, &st);
      CHECK(n > 0, "%s: Digi-%d handed out no data", what, s);
      CHECK(saved.size() == n && memcmp(saved.data(), out, n * 4) == 0, "%s: Digi-%d saved %zu words, handed out %zu (or they differ)", what, s, saved.size(), n);
      CHECK(st.aggRead == st.aggAcquired, "%s: Digi-%d took %ld aggregates, %ld were read", what, s, st.aggAcquired, st.aggRead);
      CHECK(st.aggCleared == 0, "%s: Digi-%d: %ld aggregates were cleared without being read", what, s, st.aggCleared);
      CHECK(!st.running, "%s: Digi-%d still running", what, s);
      CHECK(st.flushes > 0, "%s: Digi-%d was stopped without a forced data flush (its last aggregate stays on the board)", what, s);
    }
  }

  //=================================================================== scenarios
  void OpenWithSettings(){
    printf("== open w/ settings\n");
    NewWorkDir(); AddBoards(); NewFSUDAQ();
    dialogs.clear();
    Open();
    CHECK(w->nDigi == 6, "opened %u boards, expected 6", w->nDigi);
    CHECK(dialogs.isEmpty(), "dialogs at open: %s", qPrintable(dialogs.join(" | ")));
    CHECK(w->BoardsNotProgrammed().isEmpty(), "not programmed: %s", qPrintable(w->BoardsNotProgrammed().join(",")));
    CHECK(w->bnStartACQ->isEnabled(), "Start not enabled after a good open");
    if( nFail == 0 ) OK("six boards programmed from their files, Start enabled");
    w->CloseDigitizers();
  }

  void MissingSettingsFile(){
    printf("== a settings file is missing\n");
    NewWorkDir(); AddBoards(); NewFSUDAQ();
    QFile::remove(settingsPath + "/Digi-62839_PSD.bin");
    dialogs.clear();
    Open();
    CHECK(Dialogs("Settings files") == 1, "no settings dialog at open (%s)", qPrintable(dialogs.join(" | ")));
    CHECK(w->BoardsNotProgrammed() == QStringList("Digi-62839"), "not-programmed list: %s", qPrintable(w->BoardsNotProgrammed().join(",")));
    w->chkSaveData->setChecked(true);
    unsigned int runBefore = w->runID;
    w->AutoRun();
    CHECK(!w->inDataRun, "a run started with a board not programmed");
    CHECK(Dialogs("Run not started") == 1, "no refusal dialog");
    CHECK(w->runID == runBefore, "run number moved from %u to %u", runBefore, w->runID);
    CHECK(QDir(dataPath).entryList(QStringList() << "temp_*", QDir::Dirs).isEmpty(), "a run folder was created");
    for( int s : Serials ){ fake_caen_stats st; fake_caen_get_stats(s, &st); CHECK(!st.running && st.aggRead == 0, "Digi-%d ran", s); }
    if( nFail == 0 ) OK("Start refused, nothing written, run number kept");
    w->CloseDigitizers();
  }

  void SavedRunIsComplete(){
    printf("== a saved run: every byte the boards took is on disk, across file roll-over\n");
    NewWorkDir(); AddBoards(100, 2); NewFSUDAQ();
    for( int s : Serials ) fake_caen_set_read_delay(s, 1000); // the readout falls behind: the boards hold data at Stop
    Open();
    w->chkSaveData->setChecked(true);
    w->sbFileSizeMB->setValue(10);
    dialogs.clear();
    w->AutoRun();
    CHECK(w->inDataRun, "the run did not start (%s)", qPrintable(dialogs.join(" | ")));
    Pump(1500);
    QString runDir = w->RunFolder();
    fake_caen_stats st; fake_caen_get_stats(15880, &st);
    long behind = st.aggAcquired - st.aggRead;
    CHECK(behind > 0, "the readout did not fall behind; the test cannot see data left on the board");
    {
      usleep(200000);   // no event processing: the Run Monitor takes nothing meanwhile
      Data::RateCounters rc1, rc2;
      w->digi[0]->GetData()->TakeRateCounters(rc1);
      w->digi[0]->GetData()->TakeRateCounters(rc2);
      uint64_t n1 = 0, n2 = 0;
      for( int ch = 0; ch < MaxNChannels; ch++ ){ n1 += rc1.counts[ch]; n2 += rc2.counts[ch]; }
      CHECK(n1 > 0 && n2 < n1, "rate counters not zeroed when taken: %lu events, then %lu", (unsigned long) n1, (unsigned long) n2);
    }
    w->StopPressed();
    CHECK(!w->inDataRun, "still in a run after Stop");
    CheckAllSaved(QStringList() << runDir, "run 1");
    for( unsigned int i = 0; i < w->nDigi; i++ ){
      int s = w->digi[i]->GetSerialNumber();
      qint64 onDisk = 0;
      for( auto & f : QDir(runDir).entryList(QStringList() << ("*_" + QString::number(s) + "_PSD_2_*.fsu"), QDir::Files) ) onDisk += QFileInfo(runDir + "/" + f).size();
      CHECK((qint64) w->digi[i]->GetData()->GetTotalFileSize() == onDisk, "Digi-%d: %lu bytes counted, %lld on disk", s, (unsigned long) w->digi[i]->GetData()->GetTotalFileSize(), (long long) onDisk);
    }
    w->StopACQ(); w->StopPressed();   // nothing running: no second stop record
    CHECK(RecordLines().size() == 2, "a stop with nothing running was recorded: %lld lines", (long long) RecordLines().size());
    CHECK(NumFiles(runDir, 15880) >= 2, "no roll-over: %d file(s) for Digi-15880", NumFiles(runDir, 15880));
    QStringList rec = RecordLines();
    CHECK(rec.size() == 2 && rec[0].startsWith("Start Run | 1 |") && rec[1].startsWith(" Stop Run | 1 |"), "run record: %s", qPrintable(rec.join(" / ")));
    for( int s : Serials ) CHECK(QFile::exists(runDir + "/temp_001_" + QString::number(s) + "_PSD.bin"), "no settings snapshot for Digi-%d", s);
    CHECK(dialogs.isEmpty(), "dialogs: %s", qPrintable(dialogs.join(" | ")));
    if( nFail == 0 ) OK("6 boards, %d files for Digi-15880, record and snapshots in place; %ld aggregates were still on Digi-15880 at Stop", NumFiles(runDir, 15880), behind);

    // the next number already on disk (a stale lastRun.sh): "Go back" (the default) gives the number back
    int failBefore = nFail;
    w->runID = 0; w->leRunID->setText("0");
    dialogs.clear();
    w->AutoRun();
    CHECK(!w->inDataRun && Dialogs("Run number already used") == 1, "no question, or the run started over run 1");
    CHECK(w->runID == 0 && w->leRunID->text() == "0", "after Go back the run number is %u (field \"%s\"), expected 0", w->runID, qPrintable(w->leRunID->text()));
    CHECK(RecordLines().size() == 2, "Go back was recorded");
    if( nFail == failBefore ) OK("run number on disk: Go back keeps run 1's files and gives the number back");
    w->CloseDigitizers();
  }

  void BoardThatCannotStart(){
    printf("== a board without a readout buffer: no board starts\n");
    NewWorkDir(); AddBoards(); NewFSUDAQ();
    Open();
    w->chkSaveData->setChecked(true);
    fake_caen_fail(1443, "malloc");
    dialogs.clear();
    w->AutoRun();
    CHECK(!w->inDataRun, "the run started");
    CHECK(Dialogs("Run not started") == 1, "no refusal dialog (%s)", qPrintable(dialogs.join(" | ")));
    for( int s : Serials ){ fake_caen_stats st; fake_caen_get_stats(s, &st); CHECK(!st.running, "Digi-%d is running", s); }
    CHECK(w->runID == 0, "run number consumed: %u", w->runID);
    CHECK(QDir(dataPath).entryList(QStringList() << "temp_*", QDir::Dirs).isEmpty(), "files of the refused run were left");
    CHECK(RecordLines().isEmpty(), "the refused run was recorded");
    for( int s : Serials ) fake_caen_forget(s);   // Digi-30508 (started before 1443) ran briefly; that was cleared on purpose
    fake_caen_fail(1443, "");
    dialogs.clear();
    w->AutoRun();
    CHECK(w->inDataRun && w->runID == 1, "after the failure cleared, the run did not start as run 1");
    Pump(300);
    QString runDir = w->RunFolder();
    w->StopPressed();
    CheckAllSaved(QStringList() << runDir, "run after refused start");
    if( nFail == 0 ) OK("refused, nothing left running or written; the next start is run 1");
    w->CloseDigitizers();
  }

  // the DT5730B (not an "S" version) is calibrated at every start, as CoMPASS does; the S boards never
  void ADCCalibrationAtStart(){
    printf("== ADC calibration of the boards that are not S versions at each start\n");
    NewWorkDir(); AddBoards(); NewFSUDAQ();
    Open();
    w->chkSaveData->setChecked(true);
    dialogs.clear();
    for( int run = 1; run <= 2; run++ ){
      w->AutoRun();
      CHECK(w->inDataRun, "run %d did not start (%s)", run, qPrintable(dialogs.join(" | ")));
      Pump(100);
      w->StopPressed();
      for( int s : Serials ){
        fake_caen_stats st; fake_caen_get_stats(s, &st);
        int want = s == 1443 ? run : 0;
        CHECK(st.calibrations == want, "after run %d Digi-%d was calibrated %d time(s), expected %d", run, s, st.calibrations, want);
      }
    }
    CHECK(fake_caen_lost_calibrations(1443) == 0, "a calibration of Digi-1443 started while its SPI bus was busy");

    //---- one that never finishes: no run
    int keep = Digitizer::adcCalibrationTimeoutMs;
    Digitizer::adcCalibrationTimeoutMs = 100;
    fake_caen_fail(1443, "calibrate");
    unsigned int runBefore = w->runID;
    int foldersBefore = QDir(dataPath).entryList(QStringList() << "temp_*", QDir::Dirs).size();
    dialogs.clear();
    w->AutoRun();
    CHECK(!w->inDataRun, "a run started although Digi-1443 did not finish its calibration");
    CHECK(Dialogs("Run not started") == 1 && dialogs.join(" ").contains("ADC calibration failed"), "no refusal naming the calibration (%s)", qPrintable(dialogs.join(" | ")));
    for( int s : Serials ){ fake_caen_stats st; fake_caen_get_stats(s, &st); CHECK(!st.running, "Digi-%d is running", s); }
    CHECK(w->runID == runBefore, "run number moved from %u to %u", runBefore, w->runID);
    CHECK(QDir(dataPath).entryList(QStringList() << "temp_*", QDir::Dirs).size() == foldersBefore, "the refused run left a folder");
    fake_caen_fail(1443, "");
    Digitizer::adcCalibrationTimeoutMs = keep;
    if( nFail == 0 ) OK("Digi-1443 calibrated at each start, the S boards never; a calibration that does not finish stops the start");
    w->CloseDigitizers();
  }

  // the Run Monitor's board blocks wrap to the window width: never a sideways scroll while one block fits
  void RunMonitorWraps(){
    printf("== Run Monitor: board blocks re-flow with the window width\n");
    NewWorkDir(); AddBoards(); NewFSUDAQ();
    Open();
    CHECK(w->scalar && w->scalarBoards.size() == 6, "Run Monitor not set up for 6 boards");
    if( !w->scalar || w->scalarBoards.size() != 6 ) return;
    w->scalar->show();
    Pump(100);
    const int blockW = w->scalarBoards[0]->width();
    QStringList seen;
    for( int width : {4000, 1400, 900, 500, blockW / 2} ){
      w->scalar->resize(width, 900);
      Pump(150);
      QWidget * vp = w->scalarScroll->viewport();
      const bool fits = vp->width() >= blockW + 20;
      int perRow = w->scalarColumns;
      QRect prev;
      for( size_t i = 0; i < w->scalarBoards.size(); i++ ){
        QRect r(w->scalarBoards[i]->mapTo(w->scalarScroll->widget(), QPoint(0, 0)), w->scalarBoards[i]->size());
        if( fits ) CHECK(r.right() < vp->width(), "width %d: Digi block %zu ends at x=%d, beyond the visible %d", width, i, r.right(), vp->width());
        if( i > 0 ){
          bool sameRow = (int) i % perRow != 0;
          CHECK(sameRow ? (r.left() > prev.right() && r.top() == prev.top()) : (r.top() > prev.top()), "width %d: block %zu not after block %zu in its row (%d,%d after %d,%d)", width, i, i - 1, r.left(), r.top(), prev.left(), prev.top());
        }
        prev = r;
      }
      int hMax = w->scalarScroll->horizontalScrollBar()->maximum();
      if( fits ) CHECK(hMax == 0, "width %d: sideways scrolling (%d px) although a block (%d px) fits", width, hMax, blockW);
      seen << QString("%1 px: %2 per row").arg(width).arg(perRow);
    }
    if( nFail == 0 ) OK("%s", qPrintable(seen.join(", ")));
    w->scalar->hide();
    w->CloseDigitizers();
  }

  // boardOrder (programSettings.txt) puts the boards in rack order: the Run Monitor's blocks and the
  // dashboard's board list follow it; boards not in it come after, in port order
  void RackOrder(){
    printf("== boards in rack order (boardOrder)\n");
    NewWorkDir(); AddBoards(); NewFSUDAQ();
    w->boardOrder = QStringList() << "1443" << "30508" << "99999" << "58918";
    Open();
    QStringList order;
    for( unsigned int i : w->RackOrder() ) order << QString::number(w->digi[i]->GetSerialNumber());
    const QString want = "1443,30508,58918,15880,15879,62839";
    CHECK(order.join(",") == want, "RackOrder %s, expected %s", qPrintable(order.join(",")), qPrintable(want));
    QStringList blocks;
    for( QWidget * b : w->scalarBoards ){
      for( QLabel * l : b->findChildren<QLabel *>() ) if( l->text().contains("Digi-") ){ blocks << QString(l->text()).remove("<b>").remove("</b>").remove("Digi-"); break; }
    }
    CHECK(blocks.join(",") == want, "Run Monitor blocks %s, expected %s", qPrintable(blocks.join(",")), qPrintable(want));
    if( nFail == 0 ) OK("RackOrder and the Run Monitor: %s (99999 not open, ignored)", qPrintable(want));
    w->CloseDigitizers();
  }

  void ReadFailureStopsRun(){
    printf("== a board stops answering mid-run: the run is stopped and says why\n");
    NewWorkDir(); AddBoards(); NewFSUDAQ();
    Open();
    w->chkSaveData->setChecked(true);
    dialogs.clear();
    w->AutoRun();
    Pump(500);
    fake_caen_fail(58918, "read");
    bool stopped = WaitFor([&](){ return !w->inDataRun; }, 5000);
    CHECK(stopped, "the run went on after Digi-58918 stopped answering");
    CHECK(Dialogs("Run stopped") == 1 && dialogs.size() == 1, "not exactly one alarm (%s)", qPrintable(dialogs.join(" | ")));
    QStringList rec = RecordLines();
    CHECK(rec.size() == 2 && rec[1].contains("Run stopped by FSUDAQ") && rec[1].contains("58918"), "stop record: %s", qPrintable(rec.join(" / ")));
    if( nFail == 0 ) OK("stopped within %s, alarm shown, the record says why", "5 s");
    fake_caen_fail(58918, "");
    w->CloseDigitizers();
  }

  void WriteFailureStopsRun(){
    printf("== the next data file cannot be opened: the run is stopped and says why\n");
    NewWorkDir(); AddBoards(100, 2); NewFSUDAQ();
    Open();
    w->chkSaveData->setChecked(true);
    w->sbFileSizeMB->setValue(10);
    dialogs.clear();
    w->AutoRun();
    QString runDir = w->RunFolder();
    chmod(runDir.toStdString().c_str(), 0555);   // no new file can be created in the run folder
    bool stopped = WaitFor([&](){ return !w->inDataRun; }, 8000);
    chmod(runDir.toStdString().c_str(), 0755);
    CHECK(stopped, "the run went on without saving");
    CHECK(Dialogs("Run stopped") == 1 && dialogs.size() == 1, "not exactly one alarm (%s)", qPrintable(dialogs.join(" | ")));
    QStringList rec = RecordLines();
    CHECK(rec.size() == 2 && rec[1].contains("cannot open the next data file"), "stop record: %s", qPrintable(rec.join(" / ")));
    if( nFail == 0 ) OK("stopped, alarm shown, the record says why");

    // the next run, not saved: the old write failure is not this run's
    w->chkSaveData->setChecked(false);
    dialogs.clear();
    w->AutoRun();
    Pump(2500);
    CHECK(w->inDataRun, "a non-saved run after the write failure was stopped (%s)", qPrintable(dialogs.join(" | ")));
    w->StopPressed();
    CHECK(dialogs.isEmpty(), "dialogs: %s", qPrintable(dialogs.join(" | ")));
    if( nFail == 0 ) OK("the next, non-saved run runs: the old failure is not held against it");
    w->CloseDigitizers();
  }

  void CloseWindowDuringRun(){
    printf("== the window closed during a run: the run is read out and recorded, nothing aborts\n");
    NewWorkDir(); AddBoards(100, 2); NewFSUDAQ();
    for( int s : Serials ) fake_caen_set_read_delay(s, 1000);
    Open();
    w->chkSaveData->setChecked(true);
    dialogs.clear();
    w->AutoRun();
    Pump(800);
    QString runDir = w->RunFolder();
    w->close();                      // asked: the default is No
    CHECK(w->inDataRun && Dialogs("Close FSUDAQ") == 1, "closing during a run was not asked about, or stopped the run");
    dialogs.clear();
    answers["Close FSUDAQ"] = "Yes";
    w->close();                      // closeEvent, answered Yes
    answers.clear();
    CHECK(Dialogs("Close FSUDAQ") == 1, "no question before closing");
    dialogs.removeAll(dialogs.value(0));
    CHECK(!w->inDataRun, "still in a run after the window closed");
    CheckAllSaved(QStringList() << runDir, "window closed");
    QStringList rec = RecordLines();
    CHECK(rec.size() == 2 && rec[1].contains("FSUDAQ was closed"), "stop record: %s", qPrintable(rec.join(" / ")));
    CHECK(dialogs.isEmpty(), "dialogs: %s", qPrintable(dialogs.join(" | ")));
    delete w; w = nullptr;           // the destructor closes the digitizers (and waits for the Run Monitor's thread)
    if( nFail == 0 ) OK("all data saved, stop record says why, the window's destructor completes");
  }

  void RepeatedRuns(){
    printf("== repeated timed runs: locked controls in the pause, Stop Series ends it with no extra record\n");
    NewWorkDir(); AddBoards(20, 1); NewFSUDAQ();
    Open();
    w->chkSaveData->setChecked(true);
    w->sbRunTimeMin->setDecimals(3);
    w->sbRunTimeMin->setValue(0.01);   // 0.6 s
    w->chkRepeatRun->setChecked(true);
    w->sbRepeatPauseSec->setValue(1);
    dialogs.clear();
    w->AutoRun();
    QStringList runDirs;
    runDirs << w->RunFolder();
    CHECK(w->inDataRun && w->runID == 1, "run 1 did not start");
    CHECK(WaitFor([&](){ return w->pauseTimer->isActive(); }, 3000), "no pause after run 1");
    CHECK(!w->bnOpenScope->isEnabled() && !w->chkSaveData->isEnabled() && !w->chkRepeatRun->isEnabled() && !w->bnSetDataPath->isEnabled(),
          "controls not locked in the pause");
    CHECK(w->bnStopACQ->isEnabled() && w->bnStopACQ->text() == "Stop Series", "Stop Series not offered in the pause");
    w->OpenDigiSettings();               // opened in the pause: its controls are locked
    {
      QWidget * page = w->digiSettings->tabWidget->widget(0);
      QScrollArea * sa = qobject_cast<QScrollArea *>(page);
      CHECK(sa && sa->widget() && !sa->widget()->isEnabled(), "the settings panel opened in a series pause is not locked");
    }
    CHECK(WaitFor([&](){ return w->inDataRun; }, 3000) && w->runID == 2, "run 2 did not start after the pause");
    runDirs << w->RunFolder();
    CHECK(WaitFor([&](){ return w->pauseTimer->isActive(); }, 3000), "no pause after run 2");
    w->StopPressed();             // in the pause
    Pump(1500);
    CHECK(!w->inDataRun && !w->series.active && !w->pauseTimer->isActive(), "series still going after Stop Series");
    CHECK(w->runID == 2, "run number %u after two runs", w->runID);
    QStringList rec = RecordLines();
    CHECK(rec.size() == 4, "%lld record lines, expected 2 starts and 2 stops: %s", (long long) rec.size(), qPrintable(rec.join(" / ")));
    CheckAllSaved(runDirs, "series");
    CHECK(w->bnStartACQ->isEnabled() && w->bnOpenScope->isEnabled(), "controls not released after the series");
    CHECK(dialogs.isEmpty(), "dialogs: %s", qPrintable(dialogs.join(" | ")));
    if( nFail == 0 ) OK("two runs, pause locked, Stop Series clean, all data saved");

    // Stop pressed during a run of a series: one stop record, series over
    dialogs.clear();
    w->AutoRun();
    runDirs.clear(); runDirs << w->RunFolder();
    Pump(200);
    w->StopPressed();
    Pump(1500);
    CHECK(!w->inDataRun && !w->series.active, "series still going after Stop in a run");
    CHECK(RecordLines().size() == 6, "%lld record lines after the third run", (long long) RecordLines().size());
    if( nFail == 0 ) OK("Stop during a run of the series ends it with one stop record");
    w->CloseDigitizers();
  }

  void BoardAlsoOnUSB(){
    printf("== a DT5730 that also has its USB cable in: opened once, over its optical link\n");
    NewWorkDir(); AddBoards(); NewFSUDAQ();
    fake_caen_add_usb_alias(1443, 0);
    dialogs.clear();
    Open();
    CHECK(w->nDigi == 6, "opened %u boards, expected 6", w->nDigi);
    bool order = w->nDigi == 6;
    for( unsigned int i = 0; i < w->nDigi && i < 6; i++ ) if( w->digi[i]->GetSerialNumber() != Serials[i] ) order = false;
    CHECK(order, "boards not in port order");
    for( unsigned int i = 0; i < w->nDigi; i++ ){
      CHECK(w->digi[i]->GetLinkType() == CAEN_DGTZ_OpticalLink, "Digi-%d opened over link type %d", w->digi[i]->GetSerialNumber(), (int) w->digi[i]->GetLinkType());
    }
    CHECK(w->logInfo->toPlainText().contains("1443 also answers on USB link 0"), "the second link of Digi-1443 is not logged");
    CHECK(dialogs.isEmpty(), "dialogs: %s", qPrintable(dialogs.join(" | ")));
    if( nFail == 0 ) OK("six boards in port order, all optical, the USB link logged");
    w->CloseDigitizers();
  }

  void SignalStopsRunProperly(){
    printf("== SIGTERM during a run: the run is read out, recorded as stopped by the signal\n");
    NewWorkDir(); AddBoards(100, 2); NewFSUDAQ();
    for( int s : Serials ) fake_caen_set_read_delay(s, 1000);
    Open();
    w->chkSaveData->setChecked(true);
    dialogs.clear();
    w->AutoRun();
    Pump(800);
    QString runDir = w->RunFolder();
    w->ShutdownOnSignal(SIGTERM);   // what the signal pipe in main.cpp calls
    CHECK(!w->inDataRun, "still in a run after SIGTERM");
    CheckAllSaved(QStringList() << runDir, "SIGTERM");
    QStringList rec = RecordLines();
    CHECK(rec.size() == 2 && rec[1].contains("signal"), "stop record: %s", qPrintable(rec.join(" / ")));
    CHECK(dialogs.isEmpty(), "dialogs during the shutdown: %s", qPrintable(dialogs.join(" | ")));
    if( nFail == 0 ) OK("all data saved, record names the signal, no dialog in the way");
    w->CloseDigitizers();
  }

  void DroppedWriteBlocksStart(){
    printf("== a register write lost on the link (success returned): the board does not hold its file, no run\n");
    NewWorkDir(); AddBoards(); NewFSUDAQ();
    fake_caen_drop_write(15879, 0x136C);          // ch3 trigger threshold
    dialogs.clear();
    Open();
    CHECK(w->BoardsNotProgrammed() == QStringList("Digi-15879"), "not-programmed: %s", qPrintable(w->BoardsNotProgrammed().join(",")));
    CHECK(Dialogs("Settings files") == 1, "no dialog at open (%s)", qPrintable(dialogs.join(" | ")));
    w->chkSaveData->setChecked(true);
    w->AutoRun();
    CHECK(!w->inDataRun && Dialogs("Run not started") == 1, "a run started with a register that does not read back");
    if( nFail == 0 ) OK("the register that reads back wrong is named, and Start is refused");
    w->CloseDigitizers();
  }

  void SlowStopReported(){
    printf("== a readout that does not end 10 s after the stop: the data left on the board are reported and recorded\n");
    NewWorkDir(); AddBoards(); NewFSUDAQ();
    Open();
    w->chkSaveData->setChecked(true);
    dialogs.clear();
    w->AutoRun();
    Pump(400);
    fake_caen_fail(1443, "hang");
    std::thread release([](){ std::this_thread::sleep_for(std::chrono::seconds(12)); fake_caen_fail(1443, ""); });
    w->StopPressed();
    release.join();
    QStringList rec = RecordLines();
    CHECK(rec.size() == 2 && rec[1].contains("1443") && rec[1].contains("data were left"), "stop record: %s", qPrintable(rec.join(" / ")));
    CHECK(Dialogs("Problems in the run") == 1, "no dialog (%s)", qPrintable(dialogs.join(" | ")));
    CHECK(w->StuckBoards().isEmpty() && w->bnStartACQ->isEnabled(), "the board counts as stuck although its readout ended");
    if( nFail == 0 ) OK("one dialog, the stop record says Digi-1443 left data on the board");
    w->CloseDigitizers();
  }

  void StuckBoardBlocksRuns(){
    printf("== a readout stuck in the CAEN library: recorded, no new run, close does not hang\n");
    NewWorkDir(); AddBoards(); NewFSUDAQ();
    Open();
    w->chkSaveData->setChecked(true);
    dialogs.clear();
    w->AutoRun();
    Pump(400);
    fake_caen_fail(58918, "hang");
    w->StopPressed();                  // 10 s + 5 s
    QStringList rec = RecordLines();
    CHECK(rec.size() == 2 && rec[1].contains("58918") && rec[1].contains("stuck"), "stop record: %s", qPrintable(rec.join(" / ")));
    CHECK(w->StuckBoards() == QStringList("Digi-58918"), "stuck boards: %s", qPrintable(w->StuckBoards().join(",")));
    CHECK(!w->bnStartACQ->isEnabled() && !w->bnOpenScope->isEnabled(), "Start or Scope enabled with a stuck board");
    dialogs.clear();
    w->AutoRun();
    CHECK(!w->inDataRun && Dialogs("Run not started") == 1, "a run started with a stuck board");
    QElapsedTimer t; t.start();
    w->CloseDigitizers();              // the stuck readout is left alone, nothing hangs
    CHECK(t.elapsed() < 15000, "closing took %lld ms", (long long) t.elapsed());
    fake_caen_fail(58918, "");         // let the thread go (it then ends on its own)
    Pump(500);
    if( nFail == 0 ) OK("recorded as stuck, Start/Scope refused, close returned in %lld ms", (long long) t.elapsed());
  }

  void OverwriteUndoneIfStartFails(){
    printf("== \"Overwrite files and continue\" and then the start fails: the old run is back\n");
    NewWorkDir(); AddBoards(); NewFSUDAQ();
    Open();
    w->chkSaveData->setChecked(true);
    w->chkAutoIncrement->setChecked(false);
    w->leRunID->setText("7");
    dialogs.clear();
    w->AutoRun(); Pump(300); w->StopPressed();
    QString runDir = w->RunFolder();
    QStringList before = QDir(runDir).entryList(QDir::Files, QDir::Name);
    qint64 bytes = 0; for( auto & f : before ) bytes += QFileInfo(runDir + "/" + f).size();
    fake_caen_fail(1443, "malloc");
    answers["Run number already used"] = "Overwrite files and continue";
    dialogs.clear();
    w->leRunID->setText("7");
    w->AutoRun();
    answers.clear();
    QStringList after = QDir(runDir).entryList(QDir::Files, QDir::Name);
    qint64 bytes2 = 0; for( auto & f : after ) bytes2 += QFileInfo(runDir + "/" + f).size();
    CHECK(!w->inDataRun && Dialogs("Run not started") == 1, "the run started, or no refusal");
    CHECK(after == before && bytes2 == bytes, "the old run's files are not back: %lld files %lld bytes, were %lld files %lld bytes",
          (long long) after.size(), (long long) bytes2, (long long) before.size(), (long long) bytes);
    CHECK(QDir(runDir).entryList(QStringList() << "replaced_*", QDir::Dirs).isEmpty(), "the aside folder is left");
    fake_caen_fail(1443, "");
    if( nFail == 0 ) OK("%lld files of run 7 back in place", (long long) after.size());
    w->CloseDigitizers();
  }

  void KnownBoards(){
    printf("== the boards of the last full scan are opened where they were; a missing one brings the full scan\n");
    NewWorkDir(); AddBoards(); NewFSUDAQ();
    const QString list = work + "/knownDigitizers.txt";
    auto Lines = [&](){ QFile f(list); if( !f.open(QIODevice::ReadOnly | QIODevice::Text) ) return -1;
                        int n = 0; for( auto & l : QString(f.readAll()).split("\n", Qt::SkipEmptyParts) ) if( !l.startsWith('#') ) n++; return n; };

    Open();   // no list yet: a full scan, which writes it
    CHECK(w->nDigi == 6, "first open: %u boards", w->nDigi);
    CHECK(Lines() == 6, "first open: knownDigitizers.txt has %d board(s), expected 6", Lines());
    int scanEmpty = fake_caen_empty_opens();
    CHECK(scanEmpty > 0, "the full scan probed no empty position (the fake does not count them)");
    w->CloseDigitizers();

    int before = fake_caen_empty_opens();
    Open();   // the known boards only
    CHECK(w->nDigi == 6, "second open: %u boards", w->nDigi);
    CHECK(fake_caen_empty_opens() == before, "second open probed %d empty position(s)", fake_caen_empty_opens() - before);
    bool order = w->nDigi == 6;
    for( unsigned int i = 0; i < w->nDigi && i < 6; i++ ) if( w->digi[i]->GetSerialNumber() != Serials[i] ) order = false;
    CHECK(order, "second open: boards not in port order");
    CHECK(w->BoardsNotProgrammed().isEmpty(), "second open: not programmed: %s", qPrintable(w->BoardsNotProgrammed().join(",")));
    if( nFail == 0 ) OK("second open: six boards where they were, no empty position probed (the scan probed %d)", scanEmpty);
    w->CloseDigitizers();

    int failBefore = nFail;
    fake_caen_fail(30508, "reopen");   // answers no more: off, or hung
    before = fake_caen_empty_opens();
    Open();
    CHECK(w->nDigi == 5, "a known board missing: %u boards", w->nDigi);
    CHECK(fake_caen_empty_opens() > before, "a known board missing: no full scan");
    CHECK(w->logInfo->toPlainText().contains("Not where the last full scan found it: Digi-30508"), "the missing board is not named");
    CHECK(Lines() == 6 && w->logInfo->toPlainText().contains("knownDigitizers.txt kept"), "the list lost the missing board (%d lines)", Lines());
    if( nFail == failBefore ) OK("a known board missing: named, every port scanned, five open, the list kept with all six");
    w->CloseDigitizers();

    failBefore = nFail;
    w->cbOpenDigitizers->setCurrentIndex(w->cbOpenDigitizers->findData(2));   // full scan
    Open();
    CHECK(w->nDigi == 5 && Lines() == 5, "full scan: %u boards, list of %d", w->nDigi, Lines());
    if( nFail == failBefore ) OK("full scan chosen: the list rewritten with the five boards found");
    w->cbOpenDigitizers->setCurrentIndex(w->cbOpenDigitizers->findData(1));
    w->CloseDigitizers();
    fake_caen_fail(30508, "");
  }

  void BoardThatDoesNotReopen(){
    printf("== a board that answers the search but does not open again: left out, no crash\n");
    NewWorkDir(); AddBoards(); NewFSUDAQ();
    fake_caen_fail(30508, "reopen");
    Open();
    CHECK(w->nDigi == 5, "%u boards open", w->nDigi);
    CHECK(w->logInfo->toPlainText().contains("cannot be opened now"), "not said in the log");
    if( nFail == 0 ) OK("five boards open, the sixth said in the log");
    w->CloseDigitizers();
  }

  void StartStopOrder(){
    printf("== the master (first board) starts last and stops first; the others stop once it has stopped\n");
    NewWorkDir(); AddBoards(); NewFSUDAQ();
    fake_caen_set_synthetic(Serials[0], 100, 0.05);  // the master always has an aggregate waiting and
    fake_caen_set_read_delay(Serials[0], 30000);     // is in a 30 ms block transfer when Stop comes
    Open();
    w->chkSaveData->setChecked(true);
    dialogs.clear();
    w->AutoRun();
    Pump(300);
    QString runDir = w->RunFolder();
    w->StopPressed();
    fake_caen_stats m; fake_caen_get_stats(Serials[0], &m);
    for( int i = 1; i < 6; i++ ){
      fake_caen_stats st; fake_caen_get_stats(Serials[i], &st);
      CHECK(st.startSeq > 0 && st.startSeq < m.startSeq, "Digi-%d started after the master (%ld, master %ld)", Serials[i], st.startSeq, m.startSeq);
      CHECK(st.stopSeq > m.stopSeq, "Digi-%d stopped before the master (%ld, master %ld)", Serials[i], st.stopSeq, m.stopSeq);
    }
    CheckAllSaved(QStringList() << runDir, "start/stop order");
    if( nFail == 0 ) OK("master started last, stopped first, all data saved");
    w->CloseDigitizers();
  }

  void DrainWaitsForFlushedData(){
    printf("== after the flush the last aggregates come in late and one by one: the stop still reads them all\n");
    NewWorkDir(); AddBoards(); NewFSUDAQ();
    for( int s : Serials ) fake_caen_set_flush(s, 3, 70);   // readable 70, 140, 210 ms after the flush
    Open();
    w->chkSaveData->setChecked(true);
    dialogs.clear();
    w->AutoRun();
    Pump(500);
    QString runDir = w->RunFolder();
    w->StopPressed();
    CheckAllSaved(QStringList() << runDir, "late flush");
    CHECK(dialogs.isEmpty(), "dialogs: %s", qPrintable(dialogs.join(" | ")));
    if( nFail == 0 ) OK("every held aggregate read and saved");
    w->CloseDigitizers();
  }

  void ProblemAtStopEndsSeries(){
    printf("== a problem only the stop finds, in a timed series: one dialog, the series ends, no next run\n");
    NewWorkDir(); AddBoards(20, 1); NewFSUDAQ();
    Open();
    w->chkSaveData->setChecked(true);
    w->sbRunTimeMin->setDecimals(3);
    w->sbRunTimeMin->setValue(0.01);
    w->chkRepeatRun->setChecked(true);
    w->sbRepeatPauseSec->setValue(1);
    fake_caen_fail(62839, "stopread");   // the readout after the stop fails
    dialogs.clear();
    w->AutoRun();
    CHECK(w->inDataRun && w->runID == 1, "run 1 did not start");
    CHECK(WaitFor([&](){ return !w->inDataRun && !w->series.active; }, 5000), "the series did not end after the run with a problem");
    Pump(2000);                            // longer than the pause
    CHECK(!w->inDataRun && w->runID == 1 && !w->pauseTimer->isActive(), "a next run started (run %u)", w->runID);
    CHECK(Dialogs("Problems in the run") == 1 && dialogs.size() == 1, "not exactly one dialog (%s)", qPrintable(dialogs.join(" | ")));
    QStringList rec = RecordLines();
    CHECK(rec.size() == 2 && rec[1].contains("62839"), "stop record: %s", qPrintable(rec.join(" / ")));
    CHECK(w->bnStartACQ->isEnabled(), "Start not enabled after the series ended");
    fake_caen_fail(62839, "");
    if( nFail == 0 ) OK("the series ends at the run with the problem, recorded, one dialog");
    w->CloseDigitizers();
  }

  void FailureInSeriesEndsSeries(){
    printf("== a board stops answering in a timed series: the run is stopped, the series ends\n");
    NewWorkDir(); AddBoards(20, 1); NewFSUDAQ();
    Open();
    w->chkSaveData->setChecked(true);
    w->sbRunTimeMin->setDecimals(3);
    w->sbRunTimeMin->setValue(0.05);   // 3 s
    w->chkRepeatRun->setChecked(true);
    w->sbRepeatPauseSec->setValue(1);
    dialogs.clear();
    w->AutoRun();
    Pump(300);
    fake_caen_fail(58918, "read");
    CHECK(WaitFor([&](){ return !w->inDataRun; }, 5000), "the run went on");
    Pump(4500);                          // past the run time and the pause
    CHECK(!w->series.active && !w->inDataRun && w->runID == 1 && !w->pauseTimer->isActive(), "the series went on (run %u)", w->runID);
    CHECK(Dialogs("Run stopped") == 1 && dialogs.size() == 1, "not exactly one alarm (%s)", qPrintable(dialogs.join(" | ")));
    CHECK(RecordLines().size() == 2, "%lld record lines", (long long) RecordLines().size());
    CHECK(w->bnStartACQ->isEnabled(), "Start not enabled after the series ended");
    fake_caen_fail(58918, "");
    if( nFail == 0 ) OK("one alarm, the series ended, Start available");
    w->CloseDigitizers();
  }

  void DataFileCannotGrow(){
    printf("== a data file that cannot grow (disk full): the run is stopped, the file ends on a whole read\n");
    NewWorkDir(); AddBoards(100, 2); NewFSUDAQ();
    Open();
    w->chkSaveData->setChecked(true);
    w->sbFileSizeMB->setValue(1000);
    struct rlimit old; getrlimit(RLIMIT_FSIZE, &old);
    struct rlimit lim = old; lim.rlim_cur = 48u << 20;   // no file grows past 48 MB: a write there fails (EFBIG)
    signal(SIGXFSZ, SIG_IGN);
    setrlimit(RLIMIT_FSIZE, &lim);
    dialogs.clear();
    w->AutoRun();
    QString runDir = w->RunFolder();
    bool stopped = WaitFor([&](){ return !w->inDataRun; }, 15000);
    setrlimit(RLIMIT_FSIZE, &old);
    signal(SIGXFSZ, SIG_DFL);
    CHECK(stopped, "the run went on without saving");
    CHECK(Dialogs("Run stopped") == 1 && dialogs.size() == 1, "not exactly one alarm (%s)", qPrintable(dialogs.join(" | ")));
    QStringList rec = RecordLines();
    CHECK(rec.size() == 2 && rec[1].contains("cannot write"), "stop record: %s", qPrintable(rec.join(" / ")));
    for( int s : Serials ){                // what is on disk is the start of what the board gave, cut at a whole read
      std::vector<uint32_t> saved = Saved(runDir, s);
      const uint32_t * out; size_t n = fake_caen_handed_out(s, &out);
      CHECK(saved.size() > 0 && saved.size() <= n && memcmp(saved.data(), out, saved.size() * 4) == 0, "Digi-%d: the file is not the start of the data (%zu of %zu words)", s, saved.size(), n);
    }
    if( nFail == 0 ) OK("stopped, the record says the file cannot be written, the files hold whole reads");
    w->CloseDigitizers();
  }

  void ReadLongerThanBuffer(){
    printf("== the library reports more bytes than the readout buffer holds: the run is stopped\n");
    NewWorkDir(); AddBoards(); NewFSUDAQ();
    Open();
    w->chkSaveData->setChecked(true);
    dialogs.clear();
    w->AutoRun();
    Pump(300);
    fake_caen_fail(1443, "overread");
    CHECK(WaitFor([&](){ return !w->inDataRun; }, 5000), "the run went on");
    CHECK(Dialogs("Run stopped") == 1 && dialogs.size() == 1, "not exactly one alarm (%s)", qPrintable(dialogs.join(" | ")));
    QStringList rec = RecordLines();
    CHECK(rec.size() == 2 && rec[1].contains("1443"), "stop record: %s", qPrintable(rec.join(" / ")));
    fake_caen_fail(1443, "");
    if( nFail == 0 ) OK("stopped with one alarm, the record names the board");
    w->CloseDigitizers();
  }

  void ScopeKeepsSaveData(){
    printf("== opening and closing the scope leaves Save Data as it was\n");
    NewWorkDir(); AddBoards(); NewFSUDAQ();
    Open();
    w->chkSaveData->setChecked(true);
    w->OpenScope();
    CHECK(!w->bnStartACQ->isEnabled(), "Start enabled with the scope open");
    w->scope->close();
    Pump(100);
    CHECK(w->chkSaveData->isChecked(), "Save Data was unticked by the scope");
    CHECK(w->bnStartACQ->isEnabled(), "Start not enabled after the scope closed");
    if( nFail == 0 ) OK("Save Data kept, Start back after the scope");
    w->CloseDigitizers();
  }
};

int main(int argc, char ** argv){
  setvbuf(stdout, nullptr, _IOLBF, 0);
  qputenv("QT_QPA_PLATFORM", "offscreen");
  QApplication app(argc, argv);

  Digitizer::programPauseMs = 0; // the fake boards need no pause between register writes
  FSUDAQTest t;
  t.fixtures = QDir(argc > 1 ? argv[1] : "tests/data/odin-runB").absolutePath();

  // answer every modal dialog: record it, press the default button (else the first)
  QTimer answer;
  QObject::connect(&answer, &QTimer::timeout, [](){
    QWidget * m = QApplication::activeModalWidget();
    if( !m ) return;
    if( auto * box = qobject_cast<QMessageBox *>(m) ){
      dialogs << box->windowTitle() + ": " + box->text();
      printf("         [dialog] %s: %s\n", qPrintable(box->windowTitle()), qPrintable(box->text().left(160).replace("\n", " ")));
      QAbstractButton * b = box->defaultButton();
      if( answers.contains(box->windowTitle()) ){
        for( QAbstractButton * x : box->buttons() ) if( x->text().remove('&') == answers[box->windowTitle()] ) b = x;
      }
      if( !b && !box->buttons().isEmpty() ) b = box->buttons().first();
      if( b ) b->click(); else box->done(0);
    }else if( auto * d = qobject_cast<QDialog *>(m) ){
      dialogs << d->windowTitle() + ": (dialog)";
      d->accept();
    }
  });
  answer.start(20);

  t.OpenWithSettings();
  t.MissingSettingsFile();
  t.SavedRunIsComplete();
  t.BoardThatCannotStart();
  t.ADCCalibrationAtStart();
  t.RunMonitorWraps();
  t.RackOrder();
  t.ReadFailureStopsRun();
  t.WriteFailureStopsRun();
  t.RepeatedRuns();
  t.BoardAlsoOnUSB();
  t.SignalStopsRunProperly();
  t.CloseWindowDuringRun();
  t.DroppedWriteBlocksStart();
  t.SlowStopReported();
  t.StuckBoardBlocksRuns();
  t.OverwriteUndoneIfStartFails();
  t.BoardThatDoesNotReopen();
  t.KnownBoards();
  t.ScopeKeepsSaveData();
  t.StartStopOrder();
  t.DrainWaitsForFlushedData();
  t.ProblemAtStopEndsSeries();
  t.FailureInSeriesEndsSeries();
  t.DataFileCannotGrow();
  t.ReadLongerThanBuffer();

  delete t.w;
  QDir(t.work).removeRecursively();
  printf("\n%s: %d problem(s)\n", nFail ? "FAILED" : "PASSED", nFail);
  return nFail ? 1 : 0;
}
