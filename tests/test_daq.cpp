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
#include <QInputDialog>
#include <QPushButton>
#include <QTextStream>
#include <QTimer>
#include <QScrollArea>
#include <QMap>
#include <QTcpServer>
#include <QTcpSocket>
#include <QJsonDocument>
#include <QJsonObject>
#include <QJsonArray>
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

// a stand-in for the pion-ros plc_command relay: answers each POST as the PLC does
// ("SLOWTRONICS,DQ1,ON" -> "DQ1,ON", or its version string to a command it does not take)
class FakeRelay {
public:
  QTcpServer server;
  QStringList commands;
  bool confirm = true;
  bool Listen(){
    if( !server.listen(QHostAddress::LocalHost, 0) ) return false;
    QObject::connect(&server, &QTcpServer::newConnection, [this](){
      while( QTcpSocket * s = server.nextPendingConnection() ){
        QObject::connect(s, &QTcpSocket::readyRead, s, [this, s](){
          QByteArray buf = s->property("buf").toByteArray() + s->readAll();
          s->setProperty("buf", buf);
          const int end = buf.indexOf("\r\n\r\n");
          if( end < 0 ) return;
          int length = 0;
          for( const QByteArray & line : buf.left(end).split('\n') ) if( line.toLower().startsWith("content-length:") ) length = line.mid(15).trimmed().toInt();
          if( buf.size() < end + 4 + length ) return;
          const QString command = QString::fromUtf8(buf.mid(end + 4, length)).trimmed().toUpper();
          commands << command;
          const QString reply = confirm ? command.section(',', 1) : "SLOWTRONICS,ver:0.0.1";
          const QByteArray body = QJsonDocument(QJsonObject{{"command", command}, {"plc_resp", reply}, {"confirmed", confirm}}).toJson(QJsonDocument::Compact);
          s->write("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: " + QByteArray::number(body.size()) + "\r\n\r\n" + body);
          s->disconnectFromHost();
        });
        QObject::connect(s, &QTcpSocket::disconnected, s, &QObject::deleteLater);
      }
    });
    return true;
  }
  QString URL() const { return "http://127.0.0.1:" + QString::number(server.serverPort()) + "/command"; }
};

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

  void NewWorkDir(const QString & keys = ""){
    delete w;   // first: its destructor saves programSettings.txt and the run protocol into the folders made here
    w = nullptr;
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
    out << "settingsPath=" << settingsPath << "\nautoIncrementRunID=1\nautoDashboard=0\n" << keys;
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
    CHECK(w->openSettingsDir == settingsPath, "opened with %s, not the settings set %s", qPrintable(w->openSettingsDir), qPrintable(settingsPath));
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

    // the next number already on disk (a stale lastRun.sh): skipped, never asked about; run 1 untouched (and read-only)
    int failBefore = nFail;
    const QStringList run1Files = QDir(runDir).entryList(QDir::Files, QDir::Name);
    CHECK(!(QFileInfo(runDir + "/temp_001_15880_PSD.bin").permissions() & (QFileDevice::WriteOwner | QFileDevice::WriteGroup | QFileDevice::WriteOther))
          && !(QFileInfo(runDir + "/temp_001_run_protocol.json").permissions() & QFileDevice::WriteOwner), "run 1's snapshot or protocol copy is writable");
    w->runID = 0; w->leRunID->setText("0");
    dialogs.clear();
    w->AutoRun();
    CHECK(w->inDataRun && dialogs.isEmpty() && w->runID == 2 && w->leRunID->text() == "2", "number on disk: run %d as %u (field \"%s\"), dialogs %s",
          w->inDataRun, w->runID, qPrintable(w->leRunID->text()), qPrintable(dialogs.join(" | ")));
    Pump(100); w->StopPressed();
    CHECK(QDir(runDir).entryList(QDir::Files, QDir::Name) == run1Files && Log().contains("Run-001 already has files"), "run 1's files changed, or the skip not logged");
    CHECK(RecordLines().size() == 4 && RecordLines()[2].startsWith("Start Run | 2 |"), "run record: %s", qPrintable(RecordLines().join(" / ")));
    if( nFail == failBefore ) OK("run number on disk: skipped to run 2 unasked; run 1's files untouched and read-only");
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
    // a number typed by hand that is on disk: asked, "Go back" (the default) keeps the files and the number
    dialogs.clear();
    w->leRunID->setText("7");
    w->AutoRun();
    CHECK(!w->inDataRun && Dialogs("Run number already used") == 1 && w->leRunID->text() == "7" && QDir(runDir).entryList(QDir::Files, QDir::Name) == before,
          "typed number on disk: run %d, field \"%s\", dialogs %s", w->inDataRun, qPrintable(w->leRunID->text()), qPrintable(dialogs.join(" | ")));
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

  //---- the scope (Scope.cpp)
  Scope * ScopeOn(int serial, int ch){
    w->OpenScope();
    Scope * s = w->scope;
    s->cbScopeDigi->setCurrentIndex(s->cbScopeDigi->findText("Digi-" + QString::number(serial)));
    s->cbScopeCh->setCurrentIndex(ch);
    return s;
  }
  Digitizer * Digi(int serial){
    for( unsigned int i = 0; i < w->nDigi; i++ ) if( w->digi[i]->GetSerialNumber() == serial ) return w->digi[i];
    return nullptr;
  }
  static void Choose(RComboBox * cb, int data){ cb->setCurrentIndex(cb->findData(data)); }
  // the setting word saved in the board's settings file
  static uint32_t InFile(Digitizer * d, Reg reg, int ch = 0){
    QFile f(QString::fromStdString(d->GetSettingFileName()));
    if( !f.open(QIODevice::ReadOnly) ) return 0xDEADBEEF;
    QByteArray all = f.readAll();
    const int index = reg.Index(ch);
    if( index < 0 || index * 4 + 4 > all.size() ) return 0xDEADBEEF;
    uint32_t v; memcpy(&v, all.constData() + index * 4, 4);
    return v;
  }

  void ScopeTraceSelection(){
    printf("== scope: the trace selectors set the DPP-PSD probes (0x8000 [13:11], [25:23], [28:26])\n");
    NewWorkDir(); AddBoards(); NewFSUDAQ();
    Open();
    Scope * s = ScopeOn(1443, 0);
    Digitizer * d = Digi(1443);
    d->SetSettingFileUpdate(true);   // "Live Setting Update": every saved change goes to the file at once
    // analog trace 1 (0 Input, 1 CFD), analog trace 2 (0 None, 1 Baseline, 2 CFD) -> probe * 2 + dual trace (UM4380)
    struct { int a1, a2, field; } cases[] = {{0, 1, 1}, {0, 2, 5}, {1, 0, 2}, {1, 1, 3}, {0, 0, 0}};
    for( auto & c : cases ){
      Choose(s->cbAnaTrace2, c.a2);
      Choose(s->cbAnaTrace1, c.a1);
      const uint32_t board = fake_caen_reg(1443, 0x8000);
      CHECK(((board >> 11) & 7) == (uint32_t) c.field, "%s + %s: bits [13:11] = %u, want %d", qPrintable(s->cbAnaTrace1->currentText()),
            qPrintable(s->cbAnaTrace2->currentText()), (board >> 11) & 7, c.field);
      CHECK(board == d->GetSettingFromMemory(DPP::BoardConfiguration), "board 0x%08X, memory 0x%08X", board, d->GetSettingFromMemory(DPP::BoardConfiguration));
    }
    Choose(s->cbAnaTrace2, 2);   // Input + CFD
    Choose(s->cbAnaTrace1, 1);   // CFD: no "CFD + CFD", trace 2 gives way
    CHECK(s->cbAnaTrace2->currentData().toInt() == 0 && ((fake_caen_reg(1443, 0x8000) >> 11) & 7) == 2, "CFD + CFD: trace 2 %s, bits %u",
          qPrintable(s->cbAnaTrace2->currentText()), (fake_caen_reg(1443, 0x8000) >> 11) & 7);
    Choose(s->cbDigiTrace1, 7);
    Choose(s->cbDigiTrace2, 3);
    CHECK(((fake_caen_reg(1443, 0x8000) >> 23) & 7) == 7 && ((fake_caen_reg(1443, 0x8000) >> 26) & 7) == 3,
          "digital probes: 0x%08X", fake_caen_reg(1443, 0x8000));
    s->close();
    if( nFail == 0 ) OK("five analog choices, CFD + CFD avoided, both digital probes; memory follows");
    w->CloseDigitizers();
  }

  void ScopeChangesWhileRunning(){
    printf("== scope: waveforms drawn; a threshold changed while it runs reaches the board; auto trigger; its own settings never reach the settings\n");
    NewWorkDir(); AddBoards(5, 2); NewFSUDAQ();
    Open();
    Digitizer * d = Digi(58918);
    d->SetSettingFileUpdate(true);   // "Live Setting Update": every saved change goes to the file at once
    d->WriteRegister(DPP::AcquisitionControl, 1, -1);   // started by S-IN, as a slave
    const uint32_t cfg = d->GetSettingFromMemory(DPP::BoardConfiguration);
    Scope * s = ScopeOn(58918, 3);
    s->StartScope();
    CHECK(s->isACQStarted, "scope not started");
    CHECK((fake_caen_reg(58918, 0x8000) >> 16) & 1, "traces not recorded on the board");
    CHECK(d->GetSettingFromMemory(DPP::BoardConfiguration) == cfg && InFile(d, DPP::BoardConfiguration) == cfg, "the scope's trace bit reached the settings");
    CHECK(WaitFor([&](){ return s->dataTrace[0]->count() > 0; }, 5000), "no waveform drawn");
    CHECK(s->dataTrace[2]->count() > 0 && s->dataTrace[3]->count() > 0, "digital traces not drawn");
    CHECK(s->gateUpper[0]->count() == 2 && s->gateUpper[1]->count() == 2, "gates not shaded (%d, %d points)", (int) s->gateUpper[0]->count(), (int) s->gateUpper[1]->count());
    CHECK(s->thresholdLine->count() == 2, "no threshold line");

    Scope::Param * thr = nullptr;
    for( auto & p : s->params ) if( p.sb && p.reg.GetAddress() == DPP::PSD::TriggerThreshold.GetAddress() ) thr = &p;
    CHECK(thr, "no threshold control");
    if( thr ){
      thr->sb->setValue(321);
      emit thr->sb->returnPressed();
      CHECK(fake_caen_reg(58918, 0x1360) == 321 && d->GetSettingFromMemory(DPP::PSD::TriggerThreshold, 3) == 321,
            "threshold: board %u, memory %u", fake_caen_reg(58918, 0x1360), d->GetSettingFromMemory(DPP::PSD::TriggerThreshold, 3));
      fake_caen_stats st;
      fake_caen_get_stats(58918, &st);
      CHECK(st.running && s->IsRunning(s->ID), "not running again after the change");
      CHECK((fake_caen_reg(58918, 0x8100) & 3) == 0 && (d->GetSettingFromMemory(DPP::AcquisitionControl) & 3) == 1,
            "restarted alone: board start mode %u (want software), memory %u", fake_caen_reg(58918, 0x8100) & 3, d->GetSettingFromMemory(DPP::AcquisitionControl) & 3);
      s->lastTrace.absIndex = -1;
      s->dataTrace[0]->clear();
      CHECK(WaitFor([&](){ return s->dataTrace[0]->count() > 0; }, 5000), "no waveform after the restart");
    }

    fake_caen_stats a0, b0, a1, b1;
    fake_caen_get_stats(58918, &a0); fake_caen_get_stats(15880, &b0);
    s->chkAutoTrigger->setChecked(true);
    Pump(400);
    s->chkAutoTrigger->setChecked(false);
    fake_caen_get_stats(58918, &a1); fake_caen_get_stats(15880, &b1);
    CHECK(a1.swTriggers - a0.swTriggers >= 5 && b1.swTriggers == b0.swTriggers, "auto trigger: %d on the board shown, %d on another",
          a1.swTriggers - a0.swTriggers, b1.swTriggers - b0.swTriggers);

    // Force: one software trigger; a flush after it only when the board's Automatic Data Flush (0x8000 bit 0) is off
    Pump(100);
    fake_caen_get_stats(58918, &a0);
    s->bnForce->click();
    Pump(200);
    fake_caen_get_stats(58918, &a1);
    CHECK(a1.swTriggers - a0.swTriggers == 1 && a1.flushes == a0.flushes, "Force, auto flush on: %d triggers, %d flushes (want 1, 0)",
          a1.swTriggers - a0.swTriggers, a1.flushes - a0.flushes);

    s->StopScope();
    CHECK(fake_caen_reg(58918, 0x8000) == cfg && fake_caen_reg(58918, 0x8100) == d->GetSettingFromMemory(DPP::AcquisitionControl),
          "after the stop: 0x8000 0x%08X (saved 0x%08X), 0x8100 0x%X", fake_caen_reg(58918, 0x8000), cfg, fake_caen_reg(58918, 0x8100));
    s->close();
    if( nFail == 0 ) OK("traces, gates and threshold drawn; threshold written while running; auto trigger on the board shown; settings as saved after the stop");
    w->CloseDigitizers();
  }

  void ScopeForceFlush(){
    printf("== scope Force on a board without Automatic Data Flush: the trigger, then a flush of that channel\n");
    int nFail0 = nFail;
    NewWorkDir(); AddBoards(5, 0); NewFSUDAQ();   // no data: only the forced events
    Open();
    Digitizer * d = Digi(58918);
    const uint32_t cfg = d->GetSettingFromMemory(DPP::BoardConfiguration);
    CHECK(cfg & 1, "fixture without Automatic Data Flush (0x%08X)", cfg);
    Scope * s = ScopeOn(58918, 3);
    d->SetSettingToMemory(DPP::BoardConfiguration, cfg & ~1u);   // after opening the scope (it reads the board), before the start (refused while it runs)
    s->StartScope();
    fake_caen_stats a0, a1;
    Pump(100);
    fake_caen_get_stats(58918, &a0);
    s->bnForce->click();
    Pump(200);
    fake_caen_get_stats(58918, &a1);
    CHECK(a1.swTriggers - a0.swTriggers == 1 && a1.flushes - a0.flushes == 1, "Force, auto flush off: %d triggers, %d flushes (want 1, 1)",
          a1.swTriggers - a0.swTriggers, a1.flushes - a0.flushes);
    s->StopScope();
    s->close();
    d->SetSettingToMemory(DPP::BoardConfiguration, cfg);
    if( nFail == nFail0 ) OK("one trigger, one flush");
    w->CloseDigitizers();
  }

  // the scope at 15880 ch 2: dual trace (Input + CFD), and a threshold of 777 written while it runs
  void ScopeEdits(Scope * s){
    Choose(s->cbAnaTrace1, 0);
    Choose(s->cbAnaTrace2, 2);
    s->StartScope();
    for( auto & p : s->params ) if( p.sb && p.reg.GetAddress() == DPP::PSD::TriggerThreshold.GetAddress() ){ p.sb->setValue(777); emit p.sb->returnPressed(); }
  }
  void ScopeChangesUndone(){
    printf("== scope: what it changed on the boards is undone when it closes (window, Ctrl-C), and never reaches the settings file\n");
    int nFail0 = nFail;
    for( int viaSignal = 0; viaSignal < 2; viaSignal++ ){
      NewWorkDir(); AddBoards(5, 2); NewFSUDAQ();
      Open();
      Digitizer * d = Digi(15880);
      d->SetSettingFileUpdate(true);   // "Live Setting Update"
      const uint32_t cfg = d->GetSettingFromMemory(DPP::BoardConfiguration), thr = d->GetSettingFromMemory(DPP::PSD::TriggerThreshold, 2);
      CHECK(((cfg >> 11) & 7) != 5 && thr != 777, "fixture already has the scope's values (0x%X, %u)", cfg, thr);
      Scope * s = ScopeOn(15880, 2);
      ScopeEdits(s);
      CHECK(s->isACQStarted && fake_caen_reg(15880, 0x1260) == 777 && d->GetSettingFromMemory(DPP::PSD::TriggerThreshold, 2) == 777,
            "threshold not applied: board %u, memory %u", fake_caen_reg(15880, 0x1260), d->GetSettingFromMemory(DPP::PSD::TriggerThreshold, 2));
      CHECK(((d->GetSettingFromMemory(DPP::BoardConfiguration) >> 11) & 7) == 5, "dual trace not applied (0x%X)", d->GetSettingFromMemory(DPP::BoardConfiguration));
      CHECK(InFile(d, DPP::BoardConfiguration) == cfg && InFile(d, DPP::PSD::TriggerThreshold, 2) == thr, "scope change in the settings file while open: 0x%X, %u",
            InFile(d, DPP::BoardConfiguration), InFile(d, DPP::PSD::TriggerThreshold, 2));
      if( viaSignal ) w->ShutdownOnSignal(SIGINT); else s->close();
      const char * how = viaSignal ? "Ctrl-C" : "closed";
      CHECK(!s->isACQStarted && !d->IsRunning(), "%s: still running", how);
      CHECK(fake_caen_reg(15880, 0x8000) == cfg && d->GetSettingFromMemory(DPP::BoardConfiguration) == cfg && InFile(d, DPP::BoardConfiguration) == cfg,
            "%s: 0x8000 board 0x%X, memory 0x%X, file 0x%X (was 0x%X)", how, fake_caen_reg(15880, 0x8000), d->GetSettingFromMemory(DPP::BoardConfiguration), InFile(d, DPP::BoardConfiguration), cfg);
      CHECK(fake_caen_reg(15880, 0x1260) == thr && d->GetSettingFromMemory(DPP::PSD::TriggerThreshold, 2) == thr && InFile(d, DPP::PSD::TriggerThreshold, 2) == thr,
            "%s: threshold board %u, memory %u, file %u (was %u)", how, fake_caen_reg(15880, 0x1260), d->GetSettingFromMemory(DPP::PSD::TriggerThreshold, 2), InFile(d, DPP::PSD::TriggerThreshold, 2), thr);
      CHECK(!d->IsSettingFileHeld(), "%s: settings file still held", how);
      d->WriteRegister(DPP::PSD::TriggerThreshold, 555, 2);   // after the scope, changes reach the file again
      CHECK(InFile(d, DPP::PSD::TriggerThreshold, 2) == 555, "%s: file not updated after the scope (%u)", how, InFile(d, DPP::PSD::TriggerThreshold, 2));
      w->CloseDigitizers();
    }
    if( nFail == nFail0 ) OK("board, memory and file as before the scope, after a close and after Ctrl-C; the file untouched meanwhile");
  }

  void ScopeOnlyThisChannel(){
    printf("== scope: \"Only this channel\" runs one board and channel, and gives the board its settings back\n");
    NewWorkDir(); AddBoards(5, 2); NewFSUDAQ();
    Open();
    Digitizer * d = Digi(62839);
    d->SetSettingFileUpdate(true);   // "Live Setting Update": every saved change goes to the file at once
    d->WriteRegister(DPP::AcquisitionControl, 1, -1);
    Scope * s = ScopeOn(62839, 5);
    s->chkSoleRun->setChecked(true);
    s->StartScope();
    fake_caen_stats st, other;
    fake_caen_get_stats(62839, &st); fake_caen_get_stats(15880, &other);
    CHECK(st.running && !other.running, "running: this board %d, another %d", st.running, other.running);
    CHECK(fake_caen_reg(62839, 0x8120) == (1u << 5) && d->GetSettingFromMemory(DPP::RegChannelEnableMask) == 0xFFFF,
          "enable mask: board 0x%X, memory 0x%X", fake_caen_reg(62839, 0x8120), d->GetSettingFromMemory(DPP::RegChannelEnableMask));
    CHECK((fake_caen_reg(62839, 0x8100) & 3) == 0, "not started by software");
    s->cbScopeCh->setCurrentIndex(6);
    CHECK(s->isACQStarted && fake_caen_reg(62839, 0x8120) == (1u << 6), "channel 6: started %d, mask 0x%X", s->isACQStarted, fake_caen_reg(62839, 0x8120));
    s->StopScope();
    struct { uint32_t address; Reg reg; int ch; } regs[] = {{0x8000, DPP::BoardConfiguration, 0}, {0x8100, DPP::AcquisitionControl, 0},
      {0x8120, DPP::RegChannelEnableMask, 0}, {0x1680, DPP::DPPAlgorithmControl, 6}, {0x1684, DPP::PSD::DPPAlgorithmControl2_G, 6},
      {0x1580, DPP::DPPAlgorithmControl, 5}, {0x1584, DPP::PSD::DPPAlgorithmControl2_G, 5}};
    for( auto & r : regs ){
      const uint32_t mem = d->GetSettingFromMemory(r.reg, r.ch);
      CHECK(fake_caen_reg(62839, r.address) == mem && InFile(d, r.reg, r.ch) == mem, "0x%04X after the stop: board 0x%X, memory 0x%X, file 0x%X",
            r.address, fake_caen_reg(62839, r.address), mem, InFile(d, r.reg, r.ch));
    }
    s->close();
    if( nFail == 0 ) OK("one board, one channel, software start; switched channel; every register as saved after the stop");
    w->CloseDigitizers();
  }

  QJsonObject RunFile(const QString & runDir, const QString & name){
    QFile f(runDir + "/" + name);
    if( !f.open(QIODevice::ReadOnly) ) return QJsonObject();
    QJsonParseError err;
    QJsonDocument doc = QJsonDocument::fromJson(f.readAll(), &err);
    CHECK(err.error == QJsonParseError::NoError, "%s is not JSON: %s", qPrintable(name), qPrintable(err.errorString()));
    return doc.object();
  }

  void LedTriggerAndRunFile(){
    printf("== LED trigger: on after the delay, off after the hold or at the stop; everything in the run file\n");
    int nFail0 = nFail;
    NewWorkDir(); AddBoards(5, 2); NewFSUDAQ();
    FakeRelay relay;
    CHECK(relay.Listen(), "the fake relay cannot listen");
    const QString relayURL = relay.URL();
    w->ledRelayURL = relayURL;
    w->chkLed->setChecked(true);
    w->sbLedDelaySec->setValue(0.3);
    w->sbLedHoldSec->setValue(0.5);
    Open();
    w->chkSaveData->setChecked(true);
    dialogs.clear();

    // a run longer than delay + hold: on at 0.3 s, off at 0.8 s, both answered
    w->AutoRun();
    CHECK(w->inDataRun, "the run did not start (%s)", qPrintable(dialogs.join(" | ")));
    QString runDir = w->RunFolder();
    CHECK(QFile::exists(runDir + "/temp_001_run.json"), "no run file at the start");
    WaitFor([&](){ return w->ledEvents.size() == 2 && w->ledEvents[1].done; }, 3000);
    CHECK(relay.commands == QStringList({"SLOWTRONICS,DQ1,ON", "SLOWTRONICS,DQ1,OFF"}), "relay got: %s", qPrintable(relay.commands.join(" | ")));
    if( w->ledEvents.size() == 2 ){
      CHECK(w->ledEvents[0].runMs >= 290 && w->ledEvents[0].runMs < 600, "on sent at %lld ms, expected 300", (long long) w->ledEvents[0].runMs);
      CHECK(w->ledEvents[1].runMs >= 790 && w->ledEvents[1].runMs < 1100, "off sent at %lld ms, expected 800", (long long) w->ledEvents[1].runMs);
      CHECK(w->ledEvents[0].ok && w->ledEvents[1].ok, "answers not ok: %s / %s", qPrintable(w->ledEvents[0].error), qPrintable(w->ledEvents[1].error));
    }
    QJsonObject live = RunFile(runDir, "temp_001_run.json");
    CHECK(live["led"].toObject()["events"].toArray().size() == 2 && live["stop"].isNull(), "run file during the run: %d LED events, stop %s",
          (int) live["led"].toObject()["events"].toArray().size(), live["stop"].isNull() ? "absent" : "present");
    w->StopPressed();
    CHECK(relay.commands.size() == 2, "a second off at the stop: %s", qPrintable(relay.commands.join(" | ")));
    QJsonObject rf = RunFile(runDir, "temp_001_run.json");
    QJsonObject led = rf["led"].toObject();
    CHECK(rf["run"].toInt() == 1 && rf["prefix"].toString() == "temp" && !rf["start"].toObject()["time"].toString().isEmpty(), "run file head: run %d, prefix %s",
          rf["run"].toInt(), qPrintable(rf["prefix"].toString()));
    CHECK(rf["stop"].toObject()["run_ms"].toInteger() > 800 && rf["stop"].toObject().contains("bytes_saved"), "no stop in the run file");
    CHECK(led["enabled"].toBool() && led["delay_s"].toDouble() == 0.3 && led["hold_s"].toDouble() == 0.5 && led["on_command"].toString() == "slowtronics,dq1,on",
          "LED settings in the run file: %s", QJsonDocument(led).toJson(QJsonDocument::Compact).constData());
    QJsonArray ev = led["events"].toArray();
    CHECK(ev.size() == 2 && ev[0].toObject()["what"].toString() == "on" && ev[0].toObject()["reply"].toString() == "DQ1,ON" && ev[1].toObject()["ok"].toBool(),
          "LED events in the run file: %s", QJsonDocument(ev).toJson(QJsonDocument::Compact).constData());
    QJsonArray boards = rf["boards"].toArray();
    CHECK(boards.size() == 6, "%d boards in the run file", (int) boards.size());
    for( const QJsonValue & b : boards ){
      QJsonObject o = b.toObject();
      Digitizer * d = Digi(o["serial"].toInt());
      if( !d ){ CHECK(false, "a board %d not open", o["serial"].toInt()); continue; }
      QJsonObject regs = o["registers"].toObject();
      const uint32_t cfg = d->GetSettingFromMemory(DPP::BoardConfiguration);
      const uint32_t thr = d->GetSettingFromMemory(DPP::PSD::TriggerThreshold, 3);
      QString cfgText = regs["0x8000 BoardConfiguration"].toString(), thrText = regs["0x1360 ch03 " + QString::fromStdString(DPP::PSD::TriggerThreshold.GetName())].toString();
      CHECK(cfgText.section(' ', 1).toUInt() == cfg && thrText.section(' ', 1).toUInt() == thr && regs.size() > 200,   // 244 on an 8-channel board, 432 on a 16-channel one
            "Digi-%d registers: %lld of them, BoardConfiguration \"%s\" (memory %u), ch3 threshold \"%s\" (memory %u)", o["serial"].toInt(),
            (long long) regs.size(), qPrintable(cfgText), cfg, qPrintable(thrText), thr);
      CHECK(QFile::exists(runDir + "/" + o["settings_snapshot"].toString()), "Digi-%d: no snapshot %s", o["serial"].toInt(), qPrintable(o["settings_snapshot"].toString()));
    }
    CHECK(QFileInfo(runDir + "/temp_001_run.json").size() < 400 * 1024, "run file of %lld bytes", (long long) QFileInfo(runDir + "/temp_001_run.json").size());
    CHECK(rf["fsudaq"].toObject()["source"].toString().contains(' '), "no source commit: \"%s\"", qPrintable(rf["fsudaq"].toObject()["source"].toString()));

    // a run that ends before the LED is due: nothing sent
    relay.commands.clear();
    w->sbLedDelaySec->setValue(2);
    w->AutoRun();
    Pump(300);
    w->StopPressed();
    Pump(200);
    CHECK(relay.commands.isEmpty(), "sent in a run shorter than the delay: %s", qPrintable(relay.commands.join(" | ")));

    // a run that ends while the LED is on: off at the stop, recorded in the run file once answered
    w->sbLedDelaySec->setValue(0.2);
    w->sbLedHoldSec->setValue(30);
    w->AutoRun();
    runDir = w->RunFolder();
    WaitFor([&](){ return relay.commands.size() == 1; }, 2000);
    w->StopPressed();
    WaitFor([&](){ return w->ledPending == 0; }, 3000);
    CHECK(relay.commands == QStringList({"SLOWTRONICS,DQ1,ON", "SLOWTRONICS,DQ1,OFF"}), "LED on through the stop: %s", qPrintable(relay.commands.join(" | ")));
    ev = RunFile(runDir, "temp_003_run.json")["led"].toObject()["events"].toArray();
    CHECK(ev.size() == 2 && ev[1].toObject()["ok"].toBool() && ev[1].toObject()["run_ms"].toInteger() < 2000, "off at the stop in the run file: %s",
          QJsonDocument(ev).toJson(QJsonDocument::Compact).constData());

    // the PLC does not take the command: an error in the log and the run file, the run goes on, no dialog
    relay.commands.clear();
    relay.confirm = false;
    w->sbLedHoldSec->setValue(0.3);
    w->AutoRun();
    runDir = w->RunFolder();
    WaitFor([&](){ return w->ledEvents.size() == 2 && w->ledEvents[1].done; }, 3000);
    CHECK(w->inDataRun, "a refused LED command stopped the run");
    CHECK(w->ledEvents.size() == 2 && !w->ledEvents[0].ok && w->ledEvents[0].error.contains("ver:0.0.1"), "refused command: %s",
          w->ledEvents.isEmpty() ? "no event" : qPrintable(w->ledEvents[0].error));
    CHECK(w->ledEvents.size() == 2 && w->ledEvents[0].switched == "no" && w->ledEvents[1].switched == "no", "refused: switched %s, %s",
          w->ledEvents.isEmpty() ? "-" : qPrintable(w->ledEvents[0].switched), w->ledEvents.size() < 2 ? "-" : qPrintable(w->ledEvents[1].switched));
    w->StopPressed();
    relay.confirm = true;
    CHECK(!w->logInfo->toPlainText().contains("may still be on"), "\"may still be on\" logged although no on was taken");

    // on taken, then the relay goes away: the failed off warns that the LED may still be on
    w->AutoRun();
    runDir = w->RunFolder();
    WaitFor([&](){ return !w->ledEvents.isEmpty() && w->ledEvents[0].done; }, 3000);
    relay.server.close();
    WaitFor([&](){ return w->ledEvents.size() == 2 && w->ledEvents[1].done; }, 8000);
    CHECK(w->ledEvents.size() == 2 && w->ledEvents[0].switched == "yes" && w->ledEvents[1].switched == "no" && w->logInfo->toPlainText().contains("may still be on"),
          "on taken, off failed: switched %s, %s; warned %d", w->ledEvents.isEmpty() ? "-" : qPrintable(w->ledEvents[0].switched),
          w->ledEvents.size() < 2 ? "-" : qPrintable(w->ledEvents[1].switched), w->logInfo->toPlainText().contains("may still be on"));
    w->StopPressed();
    ev = RunFile(runDir, "temp_005_run.json")["led"].toObject()["events"].toArray();
    CHECK(ev.size() == 2 && ev[0].toObject()["switched"].toString() == "yes" && ev[1].toObject()["switched"].toString() == "no",
          "switched in the run file: %s", QJsonDocument(ev).toJson(QJsonDocument::Compact).constData());

    // nobody listening: the same, with the reason
    w->AutoRun();
    WaitFor([&](){ return w->ledEvents.size() == 2 && w->ledEvents[1].done; }, 8000);
    CHECK(w->inDataRun && w->ledEvents.size() == 2 && !w->ledEvents[0].ok && w->ledEvents[0].error.startsWith("no answer")
          && w->ledEvents[0].error.contains("is pion-ros running") && w->ledEvents[0].switched == "no", "relay down: %s (switched %s)",
          w->ledEvents.isEmpty() ? "no event" : qPrintable(w->ledEvents[0].error), w->ledEvents.isEmpty() ? "-" : qPrintable(w->ledEvents[0].switched));
    CHECK(w->lbLedStatus->text().contains("failed"), "status shows \"%s\"", qPrintable(w->lbLedStatus->text()));
    w->StopPressed();
    CHECK(dialogs.isEmpty(), "dialogs: %s", qPrintable(dialogs.join(" | ")));

    // off: nothing sent, the run file says so; the settings are kept for the next start
    w->chkLed->setChecked(false);
    CHECK(w->chkLed->text() == "disabled" && !w->sbLedDelaySec->isEnabled() && !w->sbLedHoldSec->isEnabled(),
          "LED unticked: reads '%s', delay box %d, hold box %d", qPrintable(w->chkLed->text()), w->sbLedDelaySec->isEnabled(), w->sbLedHoldSec->isEnabled());
    w->AutoRun();
    runDir = w->RunFolder();
    Pump(500);
    w->StopPressed();
    CHECK(w->ledEvents.isEmpty() && !RunFile(runDir, "temp_007_run.json")["led"].toObject()["enabled"].toBool(), "LED off, yet %lld events", (long long) w->ledEvents.size());
    w->chkLed->setChecked(true);
    CHECK(w->chkLed->text() == "enabled" && w->sbLedDelaySec->isEnabled() && w->sbLedHoldSec->isEnabled(),
          "LED ticked: reads '%s', delay box %d, hold box %d", qPrintable(w->chkLed->text()), w->sbLedDelaySec->isEnabled(), w->sbLedHoldSec->isEnabled());
    w->sbLedDelaySec->setValue(4.5);
    w->sbLedHoldSec->setValue(12);
    w->SaveProgramSettings();
    w->CloseDigitizers();
    NewFSUDAQ();
    CHECK(w->chkLed->text() == "enabled", "after a restart the LED box reads '%s'", qPrintable(w->chkLed->text()));
    CHECK(w->chkLed->isChecked() && w->sbLedDelaySec->value() == 4.5 && w->sbLedHoldSec->value() == 12 && w->ledRelayURL == relayURL,
          "after a restart: LED %d, delay %g, hold %g, relay %s", w->chkLed->isChecked(), w->sbLedDelaySec->value(), w->sbLedHoldSec->value(), qPrintable(w->ledRelayURL));
    if( nFail == nFail0 ) OK("on at 0.3 s and off at 0.8 s, or off at the stop, or nothing before the delay; refused and unanswered commands logged, the run goes on; run file with settings, LED events and %d boards' registers; settings kept", (int) boards.size());
  }

  //=================================================================== settings sets, run settings, run protocol
  QString Log(){ return w->logInfo->toPlainText(); }
  static QByteArray FileBytes(const QString & path){ QFile f(path); if( !f.open(QIODevice::ReadOnly) ) return QByteArray(); return f.readAll(); }
  static bool WriteBytes(const QString & path, const QByteArray & b){ QFile f(path); return f.open(QIODevice::WriteOnly) && f.write(b) == b.size(); }
  static uint32_t Word(const QString & path, Reg reg, int ch){
    QByteArray b = FileBytes(path);
    uint32_t v = 0xFFFFFFFF;
    if( b.size() == SETTINGSIZE * 4 ) memcpy(&v, b.constData() + reg.Index(ch) * 4, 4);
    return v;
  }
  static void SetWord(const QString & path, Reg reg, int ch, uint32_t v){
    QByteArray b = FileBytes(path);
    memcpy(b.data() + reg.Index(ch) * 4, &v, 4);
    WriteBytes(path, b);
  }
  static QJsonObject ReadJson(const QString & path){ return QJsonDocument::fromJson(FileBytes(path)).object(); }
  static uint32_t ThrAddr(int ch){ Reg r = DPP::PSD::TriggerThreshold; return r.ActualAddress(ch); }
  void ParseProgramSettings(const QString & keys){
    delete w; w = nullptr;
    QFile f(work + "/programSettings.txt");
    f.open(QIODevice::WriteOnly | QIODevice::Text);
    f.write((dataPath + "\n\n\n\n\n\n\n\n\n" + keys).toUtf8());
    f.close();
    NewFSUDAQ();
  }

  void SettingsSets(){
    printf("== settings sets: listed beside the active one, opened with the one chosen, its run protocol loaded\n");
    int nFail0 = nFail;
    NewWorkDir(); AddBoards();
    const QString noise = work + "/noise_run";
    Reg thr = DPP::PSD::TriggerThreshold;
    QDir().mkpath(noise); QDir().mkpath(work + "/empty"); QDir().mkpath(work + "/notes");
    WriteBytes(work + "/notes/readme.txt", "not a set");
    for( int s : Serials ) QFile::copy(settingsPath + "/Digi-" + QString::number(s) + "_PSD.bin", noise + "/Digi-" + QString::number(s) + "_PSD.bin");
    SetWord(noise + "/Digi-15880_PSD.bin", thr, 0, 0x44);
    Reg boardConfig = DPP::BoardConfiguration;
    SetWord(noise + "/Digi-15879_PSD.bin", boardConfig, 0, Word(noise + "/Digi-15879_PSD.bin", boardConfig, 0) | (1u << 16));   // saved with traces on
    NewFSUDAQ();
    CHECK(FSUDAQ::SettingsSets(work) == QStringList({"noise_run", "settings"}), "sets listed: %s", qPrintable(FSUDAQ::SettingsSets(work).join(",")));
    CHECK(w->cbSettingsSet->count() == 2 && w->cbSettingsSet->currentText() == "settings" && w->cbOpenMethod->itemText(w->cbOpenMethod->findData(1)) == "w/ settings set: settings",
          "set chooser: %d items, current %s", w->cbSettingsSet->count(), qPrintable(w->cbSettingsSet->currentText()));
    QJsonObject p = w->ProtocolJson();
    p["maxFileSizeMB"] = 700; p["runLengthMin"] = 2.5;
    WriteBytes(noise + "/run_protocol.json", QJsonDocument(p).toJson());
    QMap<int, QByteArray> noiseBefore;
    for( int s : Serials ) noiseBefore[s] = FileBytes(noise + "/Digi-" + QString::number(s) + "_PSD.bin");

    w->cbSettingsSet->setCurrentIndex(w->cbSettingsSet->findText("noise_run"));
    CHECK(w->settingsPath == noise && FileBytes(work + "/programSettings.txt").contains(("settingsPath=" + noise + "\n").toUtf8()), "set chosen: settingsPath %s", qPrintable(w->settingsPath));
    dialogs.clear();
    Open();
    CHECK(dialogs.isEmpty() && w->BoardsNotProgrammed().isEmpty() && w->openSettingsDir == noise, "open with noise_run: dialogs %s", qPrintable(dialogs.join(" | ")));
    CHECK(fake_caen_reg(15880, ThrAddr(0)) == 0x44, "Digi-15880 not programmed from noise_run");
    CHECK(w->sbFileSizeMB->value() == 700 && w->sbRunTimeMin->value() == 2.5 && Log().contains("Run protocol of set noise_run taken") && Log().contains("maxFileSizeMB: "),
          "the set's run protocol not taken: file size %g", w->sbFileSizeMB->value());
    CHECK(Log().contains("Digi-15880: programmed from settings set noise_run, " + noise + "/Digi-15880_PSD.bin"), "the log does not name the set and file");
    for( unsigned int i = 0; i < w->nDigi; i++ )
      CHECK(QString::fromStdString(w->digi[i]->GetSettingFileName()) == noise + "/Digi-" + QString::number(w->digi[i]->GetSerialNumber()) + "_PSD.bin", "Digi-%d's file %s", w->digi[i]->GetSerialNumber(), w->digi[i]->GetSettingFileName().c_str());
    //---- a file saved with traces on: no question at Start (every open switches them off)
    w->chkSaveData->setChecked(true);
    w->sbRunTimeMin->setValue(0);
    dialogs.clear();
    w->AutoRun();
    CHECK(w->inDataRun && dialogs.isEmpty(), "traces on in the file: run %d, dialogs %s", w->inDataRun, qPrintable(dialogs.join(" | ")));
    Pump(100); w->StopPressed();
    //---- Save and Load start at the board's file in the set
    w->OpenDigiSettings();
    CHECK(w->digiSettings->settingsDir == noise
          && DigiSettingsPanel::DefaultSettingsFile(QString::fromStdString(w->digi[0]->GetSettingFileName()), w->digiSettings->settingsDir, "x.bin") == noise + "/Digi-15880_PSD.bin"
          && DigiSettingsPanel::DefaultSettingsFile("", "/s", "Digi-1_PSD.bin") == "/s/Digi-1_PSD.bin", "Save/Load default path");
    w->CloseDigitizers();
    for( int s : Serials ) CHECK(FileBytes(noise + "/Digi-" + QString::number(s) + "_PSD.bin") == noiseBefore[s], "noise_run's Digi-%d file was written", s);

    //---- a set without a run protocol: the values kept, nothing written into it
    w->cbSettingsSet->setCurrentIndex(w->cbSettingsSet->findText("settings"));
    Open();
    CHECK(Log().contains("Settings set settings has no run protocol yet") && !QFileInfo::exists(settingsPath + "/run_protocol.json") && w->sbFileSizeMB->value() == 700,
          "set without a run protocol: file size %g, file %s", w->sbFileSizeMB->value(), QFileInfo::exists(settingsPath + "/run_protocol.json") ? "created" : "absent");
    w->CloseDigitizers();

    //---- programSettings.txt as before: settingsPath is the set; empty means the data path
    ParseProgramSettings("settingsPath=\n");
    CHECK(w->settingsPath == dataPath, "empty settingsPath: %s", qPrintable(w->settingsPath));
    {
      QString why;
      CHECK(w->SettingsRoot().isEmpty() && w->cbSettingsSet->count() == 1 && !w->SaveAsNewSet("x", why) && why.contains("no settings root"),
            "the data path as a set: root \"%s\", %d entries, %s", qPrintable(w->SettingsRoot()), w->cbSettingsSet->count(), qPrintable(why));
    }
    ParseProgramSettings("settingsPath=" + noise + "/\nrepeatRun=1\n");
    CHECK(w->settingsPath == noise && w->cbSettingsSet->currentText() == "noise_run" && w->SettingsRoot() == work, "settingsPath=%s/ read as %s", qPrintable(noise), qPrintable(w->settingsPath));
    CHECK(!w->chkRepeatRun->isChecked(), "Repeat restored at program start");

    //---- the run protocol only after a successful open; Repeat never restored
    p["repeatRun"] = true; p["maxFileSizeMB"] = 900;
    WriteBytes(noise + "/run_protocol.json", QJsonDocument(p).toJson());
    fake_caen_reset_all();   // no board answers
    const double sizeBefore = w->sbFileSizeMB->value();
    w->OpenDigitizers();
    CHECK(w->digi == nullptr && w->sbFileSizeMB->value() == sizeBefore, "a failed open took the run protocol: file size %g", w->sbFileSizeMB->value());
    AddBoards();
    Open();
    CHECK(w->sbFileSizeMB->value() == 900 && !w->chkRepeatRun->isChecked() && Log().contains("repeat is never taken from a file"), "open: file size %g, repeat %d", w->sbFileSizeMB->value(), w->chkRepeatRun->isChecked());
    w->CloseDigitizers();
    if( nFail == nFail0 ) OK("2 sets listed (empty and file-less folders not), the chosen one opened with its run protocol, traces in a file no question, a set without a protocol untouched; no sets from the data path; protocol only after an open, Repeat never restored");
  }

  void SaveAsNewSet(){
    printf("== Save as new set: the boards (read back) and the run protocol into a new folder beside the set, made active; the old set untouched\n");
    int nFail0 = nFail;
    NewWorkDir(); AddBoards(); NewFSUDAQ();
    Open();
    Reg thr = DPP::PSD::TriggerThreshold;
    QMap<int, QByteArray> before;
    for( int s : Serials ) before[s] = FileBytes(settingsPath + "/Digi-" + QString::number(s) + "_PSD.bin");
    w->digi[0]->WriteRegister(DPP::PSD::TriggerThreshold, 0x5B, 1);   // an edit not in the set
    w->digi[0]->SetSettingFileUpdate(true);                          // the read-back must still not write the old set
    QString why;
    const int dirsBefore = QDir(work).entryList(QDir::Dirs | QDir::NoDotAndDotDot | QDir::Hidden).size();
    for( const QString & bad : {QString(""), QString("a/b"), QString(".."), QString(".hidden"), QString("x..y"), QString("settings"), QString("data")} ){
      bool ok = w->SaveAsNewSet(bad, why);
      CHECK(!ok && !why.isEmpty(), "name \"%s\" accepted", qPrintable(bad));
    }
    CHECK(QDir(work).entryList(QDir::Dirs | QDir::NoDotAndDotDot | QDir::Hidden).size() == dirsBefore && w->settingsPath == settingsPath, "a refused name left a folder or changed the set");

    const QString noise = work + "/noise_run";
    CHECK(w->SaveAsNewSet("noise_run", why), "noise_run not saved: %s", qPrintable(why));
    QStringList files = QDir(noise).entryList(QDir::Files);
    CHECK(files.size() == 7 && files.contains("run_protocol.json"), "noise_run holds %s", qPrintable(files.join(",")));
    CHECK(Word(noise + "/Digi-15880_PSD.bin", thr, 1) == 0x5B, "the new set does not have the board's value");
    CHECK(ReadJson(noise + "/run_protocol.json")["maxFileSizeMB"].toInt() == (int) w->sbFileSizeMB->value(), "run protocol not in the new set");
    for( int s : Serials ) CHECK(FileBytes(settingsPath + "/Digi-" + QString::number(s) + "_PSD.bin") == before[s], "the old set's Digi-%d file changed", s);
    CHECK(w->settingsPath == noise && w->openSettingsDir == noise && w->cbSettingsSet->currentText() == "noise_run" && Log().contains("New settings set noise_run"), "noise_run not made active");
    for( unsigned int i = 0; i < w->nDigi; i++ )
      CHECK(QString::fromStdString(w->digi[i]->GetSettingFileName()) == noise + "/Digi-" + QString::number(w->digi[i]->GetSerialNumber()) + "_PSD.bin", "Digi-%d still writes %s", w->digi[i]->GetSerialNumber(), w->digi[i]->GetSettingFileName().c_str());
    CHECK(w->digi[0]->IsSettingFileUpdate(), "Live Setting Update went off although the new file holds the board");
    w->digi[0]->WriteRegister(DPP::PSD::TriggerThreshold, 0x5C, 1);
    CHECK(Word(noise + "/Digi-15880_PSD.bin", thr, 1) == 0x5C && FileBytes(settingsPath + "/Digi-15880_PSD.bin") == before[15880], "Live Setting Update after the new set");
    w->digi[0]->SetSettingFileUpdate(false);
    CHECK(QDir(work).entryList(QStringList() << ".*partial*", QDir::Dirs | QDir::Hidden).isEmpty(), "a partial set was left");
    CHECK(!w->SaveAsNewSet("noise_run", why), "a set saved over an existing one");

    //---- through the button's question
    answers["Save as new set"] = "pulser";
    w->PromptSaveAsNewSet();
    answers.clear();
    CHECK(QFileInfo::exists(work + "/pulser/Digi-15880_PSD.bin") && w->settingsPath == work + "/pulser", "the question did not make pulser");

    //---- a set that cannot be written: nothing made, the set as it was
    chmod(qPrintable(work), 0555);
    bool made = w->SaveAsNewSet("blocked", why);
    chmod(qPrintable(work), 0755);
    CHECK(!made && !QFileInfo::exists(work + "/blocked") && w->settingsPath == work + "/pulser", "unwritable root: made %d (%s)", made, qPrintable(why));
    w->CloseDigitizers();
    if( nFail == nFail0 ) OK("names checked, the boards' values and the protocol in the new set, the old set byte for byte, Live Setting Update follows, nothing half-made");
  }

  void PanelKeepsTheSet(){
    printf("== Load/Save in Digitizer Settings: another file is programmed or copied, the board's file stays in the active set\n");
    int nFail0 = nFail;
    NewWorkDir(); AddBoards(); NewFSUDAQ();
    const QString other = work + "/noise_run";
    Reg thr = DPP::PSD::TriggerThreshold;
    QDir().mkpath(other);
    QFile::copy(settingsPath + "/Digi-15880_PSD.bin", other + "/Digi-15880_PSD.bin");
    SetWord(other + "/Digi-15880_PSD.bin", thr, 0, 0x44);
    const QByteArray otherBytes = FileBytes(other + "/Digi-15880_PSD.bin");
    const QString setFile = settingsPath + "/Digi-15880_PSD.bin";
    Open();
    w->OpenDigiSettings();
    w->digiSettings->ID = 0;
    CHECK(w->digiSettings->LoadSettingFile(other + "/Digi-15880_PSD.bin"), "Load from another set failed");
    CHECK(fake_caen_reg(15880, ThrAddr(0)) == 0x44 && QString::fromStdString(w->digi[0]->GetLoadedFromFile()) == other + "/Digi-15880_PSD.bin"
          && QString::fromStdString(w->digi[0]->GetSettingFileName()) == setFile && Log().contains("loaded from " + other + "/Digi-15880_PSD.bin"),
          "Load from another set: board 0x%X, file %s", fake_caen_reg(15880, ThrAddr(0)), w->digi[0]->GetSettingFileName().c_str());
    w->digiSettings->chkCoupledSettingFile->setChecked(true);
    w->digi[0]->WriteRegister(DPP::PSD::TriggerThreshold, 0x45, 1);
    CHECK(FileBytes(other + "/Digi-15880_PSD.bin") == otherBytes, "Live Setting Update wrote the other set");
    CHECK(FileBytes(setFile) == QByteArray((const char *) w->digi[0]->GetSettings(), SETTINGSIZE * 4), "the active set's file is not the board's image");
    CHECK(w->digiSettings->SaveSettingFile(work + "/export.bin", 0) && FileBytes(work + "/export.bin").size() == SETTINGSIZE * 4
          && QString::fromStdString(w->digi[0]->GetSettingFileName()) == setFile && w->digi[0]->IsSettingFileUpdate(), "Save to another path was not a copy");
    w->digiSettings->chkCoupledSettingFile->setChecked(false);
    w->CloseDigitizers();

    //---- opened without settings: Start does not compare (nor offer to save) a board that holds what it held before
    w->cbOpenMethod->setCurrentIndex(w->cbOpenMethod->findData(0));
    w->OpenDigitizers();
    w->UpdateRunControls();
    w->chkSaveData->setChecked(true);
    const QByteArray setBefore = FileBytes(setFile);
    w->digi[0]->WriteRegister(DPP::PSD::TriggerThreshold, 0x46, 2);
    dialogs.clear();
    w->AutoRun();
    CHECK(w->inDataRun && dialogs.isEmpty() && w->BoardsNotProgrammed().isEmpty() && Log().contains("opened without settings, not compared"), "w/o settings: run %d, dialogs %s", w->inDataRun, qPrintable(dialogs.join(" | ")));
    Pump(100); w->StopPressed();
    CHECK(FileBytes(setFile) == setBefore, "the set's file changed after a w/o settings run");
    w->CloseDigitizers();
    w->cbOpenMethod->setCurrentIndex(w->cbOpenMethod->findData(1));

    //---- a set file behind a symlink, with its own mode: replaced through the link, mode kept
    const QString real = work + "/real_protocol.json", link = work + "/link_protocol.json";
    WriteBytes(real, "{}");
    chmod(qPrintable(real), 0640);
    QFile::link(real, link);
    CHECK(FSUDAQ::WriteFileAtomic(link, "{\"a\": 1}") && QFileInfo(link).isSymLink() && FileBytes(real) == "{\"a\": 1}"
          && (QFileInfo(real).permissions() & (QFileDevice::ReadOther | QFileDevice::WriteGroup)) == 0 && (QFileInfo(real).permissions() & QFileDevice::ReadGroup),
          "atomic write through a symlink: link %d", QFileInfo(link).isSymLink());
    if( nFail == nFail0 ) OK("Load from another set programs it, the set stays the target (Live Setting Update writes the set whole, never the other); Save elsewhere is a copy; w/o settings not compared; symlink and mode kept");
  }

  void UnsavedEditsAtStart(){
    printf("== Start with settings not in the set: never asked, logged and marked on Save to set; Save to set writes the set in place\n");
    int nFail0 = nFail;
    NewWorkDir(); AddBoards(20, 1); NewFSUDAQ();
    Open();
    w->chkSaveData->setChecked(true);
    CHECK(w->SaveSetProtocol(settingsPath), "run protocol not saved to the set");
    const QString file0 = settingsPath + "/Digi-15880_PSD.bin", proto = settingsPath + "/run_protocol.json";
    const QByteArray fileBefore = FileBytes(file0);
    Reg thr = DPP::PSD::TriggerThreshold;
    const uint32_t fileThr = Word(file0, thr, 2);
    auto StartStop = [&](){ w->AutoRun(); QString d = w->RunFolder(); if( w->inDataRun ){ Pump(100); w->StopPressed(); } return d; };
    auto Marked = [&](){ return w->bnSaveToSet->text().endsWith("*"); };
    auto Tip = [&](){ return w->bnSaveToSet->toolTip(); };

    fake_caen_set_reg(15880, 0x10A8, 0x3C);   // a temperature: never a difference
    dialogs.clear();
    w->AutoRun();
    CHECK(w->inDataRun && dialogs.isEmpty() && !Marked(), "nothing edited: run %d, dialogs %s, button \"%s\"", w->inDataRun, qPrintable(dialogs.join(" | ")), qPrintable(w->bnSaveToSet->text()));
    Pump(100); w->StopPressed();

    //---- an edit: no question; the run starts with it and its snapshot has it, the set not; logged, Save to set marked
    w->digi[0]->WriteRegister(DPP::PSD::TriggerThreshold, 0x55, 2);
    dialogs.clear();
    QString runDir = StartStop();
    CHECK(dialogs.isEmpty() && Marked() && Tip().contains("last Start: Digi-15880 ("), "an edit: dialogs %s, button \"%s\"", qPrintable(dialogs.join(" | ")), qPrintable(w->bnSaveToSet->text()));
    CHECK(Word(runDir + "/" + QFileInfo(runDir).fileName() + "_15880_PSD.bin", thr, 2) == 0x55 && FileBytes(file0) == fileBefore, "snapshot or set wrong after the start");
    CHECK(Log().contains(QString::asprintf("Digi-15880: 0x%04X TriggerThreshold ch2: file 0x%08X, board 0x%08X", ThrAddr(2), fileThr, 0x55u)) && Log().contains("this run starts with them"),
          "the register is not named in the log");
    //---- a per-run protocol field: no difference, in the run's copy
    w->sbLedDelaySec->setValue(3.5);
    dialogs.clear();
    runDir = StartStop();
    CHECK(dialogs.isEmpty() && Tip().contains("last Start: Digi-15880 (") && ReadJson(runDir + "/" + QFileInfo(runDir).fileName() + "_run_protocol.json")["ledDelaySec"].toDouble() == 3.5,
          "per-run field: dialogs %s, tooltip %s", qPrintable(dialogs.join(" | ")), qPrintable(Tip().left(80)));
    //---- a set field of the protocol, and 32 registers: logged (capped), both marked
    w->sbFileSizeMB->setValue(300);
    w->digi[0]->WriteRegister(DPP::PSD::ShortGateWidth, 0x21, -1);
    w->digi[0]->WriteRegister(DPP::PSD::LongGateWidth, 0x61, -1);
    dialogs.clear();
    StartStop();
    CHECK(dialogs.isEmpty() && Tip().contains("last Start: Digi-15880, the run protocol ("), "protocol field: dialogs %s, tooltip %s", qPrintable(dialogs.join(" | ")), qPrintable(Tip().left(80)));
    CHECK(Log().contains("Digi-15880: ... and 13 more") && Log().contains("maxFileSizeMB: set "), "the log is not capped or lacks the protocol field");
    //---- Live Setting Update on: the check still compares the board with the file (and does not write it first)
    fake_caen_set_reg(15880, ThrAddr(5), 0x99);   // changed behind FSUDAQ's back
    w->digi[0]->SetSettingFileUpdate(true);
    const uint32_t fileThr5 = Word(file0, thr, 5);
    dialogs.clear();
    StartStop();
    w->digi[0]->SetSettingFileUpdate(false);
    CHECK(dialogs.isEmpty() && Log().contains("Live Setting Update is on") && Log().contains(QString::asprintf("TriggerThreshold ch5: file 0x%08X, board 0x%08X", fileThr5, 0x99u))
          && Word(file0, thr, 5) == fileThr5, "Live Setting Update: dialogs %s, file 0x%X", qPrintable(dialogs.join(" | ")), Word(file0, thr, 5));

    //---- Save to set into a folder that cannot be written: nothing saved, said, still marked
    chmod(qPrintable(settingsPath), 0555);
    dialogs.clear();
    CHECK(!w->SaveToSet() && dialogs.isEmpty() && Marked() && Log().contains("Save to set settings: not saved"), "unwritable set: dialogs %s", qPrintable(dialogs.join(" | ")));
    chmod(qPrintable(settingsPath), 0755);
    CHECK(FileBytes(file0) == fileBefore, "the unwritable set changed");
    //---- Save to set: the boards (read back) and the protocol, in place, unasked; the next Start finds nothing
    dialogs.clear();
    CHECK(w->SaveToSet() && dialogs.isEmpty() && !Marked(), "Save to set: dialogs %s, button \"%s\"", qPrintable(dialogs.join(" | ")), qPrintable(w->bnSaveToSet->text()));
    CHECK(Word(file0, thr, 2) == 0x55 && Word(file0, thr, 5) == 0x99 && ReadJson(proto)["maxFileSizeMB"].toInt() == 300
          && FileBytes(file0) == QByteArray((const char *) w->digi[0]->GetSettings(), SETTINGSIZE * 4), "Save to set: file 0x%X, protocol %d", Word(file0, thr, 2), ReadJson(proto)["maxFileSizeMB"].toInt());
    dialogs.clear();
    StartStop();
    CHECK(dialogs.isEmpty() && !Marked(), "not as in the set after saving: %s", qPrintable(Tip().left(80)));
    //---- Digitizer Settings' Save to set: this board's file in the set, no question
    w->digi[0]->WriteRegister(DPP::PSD::TriggerThreshold, 0x57, 4);
    w->OpenDigiSettings();
    w->digiSettings->ID = 0;
    dialogs.clear();
    w->digiSettings->SaveSettingToSet();
    CHECK(dialogs.isEmpty() && Word(file0, thr, 4) == 0x57, "the panel's Save to set: dialogs %s, file 0x%X", qPrintable(dialogs.join(" | ")), Word(file0, thr, 4));

    //---- a series: the next run never waits on a question
    w->sbRunTimeMin->setDecimals(3);
    w->sbRunTimeMin->setValue(0.01);
    w->chkRepeatRun->setChecked(true);
    w->sbRepeatPauseSec->setValue(1);
    dialogs.clear();
    w->AutoRun();
    const unsigned int first = w->runID;
    CHECK(w->inDataRun && dialogs.isEmpty(), "series: first run %d, dialogs %s", w->inDataRun, qPrintable(dialogs.join(" | ")));
    CHECK(WaitFor([&](){ return w->pauseTimer->isActive(); }, 3000), "no pause");
    w->digi[1]->WriteRegister(DPP::PSD::TriggerThreshold, 0x66, 0);   // an edit in the pause (the panel is locked; a script is not)
    CHECK(WaitFor([&](){ return w->inDataRun && w->runID == first + 1; }, 3000), "the next run of the series did not start");
    const QString edited1 = "Digi-" + QString::number(w->digi[1]->GetSerialNumber());
    CHECK(dialogs.isEmpty() && Log().contains(edited1 + ": not as in the settings set") && Tip().contains("last Start: " + edited1 + " ("), "series: dialogs %s", qPrintable(dialogs.join(" | ")));
    w->StopPressed();
    Pump(1500);
    w->sbRunTimeMin->setValue(0);
    w->chkRepeatRun->setChecked(false);
    w->CloseDigitizers();
    if( nFail == nFail0 ) OK("never asked; differences logged (capped) and marked on Save to set; per-run field never; Live Setting Update compared; Save to set writes boards and protocol in place, Digitizer Settings' too; a series starts and logs");
  }

  void SettingsFromRun(){
    printf("== open with the settings of a run: the run chosen (newest first), snapshots never written, a board without one from the set\n");
    int nFail0 = nFail;
    NewWorkDir(); AddBoards(20, 1); NewFSUDAQ();
    Open();
    w->chkSaveData->setChecked(true);
    auto Run = [&](uint32_t threshold, int fileMB){
      w->sbFileSizeMB->setValue(fileMB);
      w->digi[0]->WriteRegister(DPP::PSD::TriggerThreshold, threshold, 0);
      w->AutoRun();
      CHECK(w->inDataRun, "run with threshold 0x%X did not start (%s)", threshold, qPrintable(dialogs.join(" | ")));
      QString dir = w->RunFolder();
      Pump(100); w->StopPressed();
      return dir;
    };
    const QString run1 = Run(0x31, 100), run2 = Run(0x32, 200);
    const QString name1 = QFileInfo(run1).fileName(), name2 = QFileInfo(run2).fileName();
    w->CloseDigitizers();
    QFile::remove(run2 + "/" + name2 + "_62839_PSD.bin");
    const QByteArray set30508 = FileBytes(settingsPath + "/Digi-30508_PSD.bin");
    QFile::remove(settingsPath + "/Digi-30508_PSD.bin");   // the set lacks a board the run has

    QList<FSUDAQ::SettingsRun> runs = FSUDAQ::RunsWithSnapshots(dataPath);
    CHECK(runs.size() == 2 && runs[0].name == name2 && runs[1].name == name1 && runs[0].boards == 5 && runs[1].boards == 6,
          "runs with snapshots: %lld, first %s", (long long) runs.size(), runs.isEmpty() ? "-" : qPrintable(runs[0].name));

    w->sbFileSizeMB->setValue(300);
    const QString snap2 = run2 + "/" + name2 + "_15880_PSD.bin";
    const QByteArray snap2Bytes = FileBytes(snap2);
    const QByteArray protoBytes2 = FileBytes(run2 + "/" + name2 + "_run_protocol.json");

    //---- the default: the newest run
    w->cbOpenMethod->setCurrentIndex(w->cbOpenMethod->findData(2));
    dialogs.clear();
    w->OpenDigitizers();
    w->UpdateRunControls();
    CHECK(Dialogs("Settings from a run") == 1 && dialogs.join("|").contains("Settings from a run: " + name2), "the run question: %s", qPrintable(dialogs.join(" | ")));
    CHECK(w->nDigi == 6 && w->openedFromRun == name2, "opened from %s", qPrintable(w->openedFromRun));
    CHECK(fake_caen_reg(15880, ThrAddr(0)) == 0x32, "Digi-15880 holds threshold 0x%X, run %s had 0x32", fake_caen_reg(15880, ThrAddr(0)), qPrintable(name2));
    CHECK(Dialogs("Settings files") == 1 && w->BoardsNotProgrammed().isEmpty(), "missing snapshot: dialogs %s, not programmed %s", qPrintable(dialogs.join(" | ")), qPrintable(w->BoardsNotProgrammed().join(",")));
    const QString log = Log();
    CHECK(log.contains("Opening with the settings from run " + name2) && log.contains("Digi-15880: programmed from run " + name2 + ", " + snap2)
          && log.contains("Digi-62839: run " + name2 + " has no settings snapshot of it") && log.contains("Digi-62839: programmed from settings set settings, " + settingsPath + "/Digi-62839_PSD.bin"),
          "the log does not say where each board's settings came from");
    for( unsigned int i = 0; i < w->nDigi; i++ ){
      const int s = w->digi[i]->GetSerialNumber();
      const QString target = settingsPath + "/Digi-" + QString::number(s) + "_PSD.bin";
      const QString from = s == 62839 ? target : run2 + "/" + name2 + "_" + QString::number(s) + "_PSD.bin";
      CHECK(QString::fromStdString(w->digi[i]->GetSettingFileName()) == target && QString::fromStdString(w->digi[i]->GetLoadedFromFile()) == from, "Digi-%d: settings file %s, programmed from %s", s, w->digi[i]->GetSettingFileName().c_str(), w->digi[i]->GetLoadedFromFile().c_str());
    }
    CHECK(w->sbFileSizeMB->value() == 200 && log.contains("maxFileSizeMB: 300 -> 200") && !QFileInfo::exists(settingsPath + "/run_protocol.json"),
          "run protocol of the run: file size %g, set protocol %s", w->sbFileSizeMB->value(), QFileInfo::exists(settingsPath + "/run_protocol.json") ? "written" : "absent");
    //---- edits go to the set's file, never to the run
    Reg thr = DPP::PSD::TriggerThreshold;
    w->OpenDigiSettings();
    w->digiSettings->ID = 0;
    w->digiSettings->chkCoupledSettingFile->setChecked(true);   // as the panel does it: the whole file first
    w->digi[0]->WriteRegister(DPP::PSD::TriggerThreshold, 0x39, 1);
    w->digiSettings->chkCoupledSettingFile->setChecked(false);
    CHECK(FileBytes(snap2) == snap2Bytes && FileBytes(run2 + "/" + name2 + "_run_protocol.json") == protoBytes2, "a file of run %s was written", qPrintable(name2));
    CHECK(Word(settingsPath + "/Digi-15880_PSD.bin", thr, 1) == 0x39
          && FileBytes(settingsPath + "/Digi-15880_PSD.bin") == QByteArray((const char *) w->digi[0]->GetSettings(), SETTINGSIZE * 4), "Live Setting Update: the set's file is not the board's whole image");
    //---- the board the set lacks: compared with its snapshot, marked; Save to set puts it into the set, never into the run
    dialogs.clear();
    w->AutoRun();
    const QString run3 = w->RunFolder();
    Pump(100); w->StopPressed();
    CHECK(dialogs.isEmpty() && w->bnSaveToSet->toolTip().contains("Digi-30508") && Log().contains("Digi-30508: not in the set (" + settingsPath + "/Digi-30508_PSD.bin); compared with " + run2),
          "the board without a file in the set: dialogs %s", qPrintable(dialogs.join(" | ")));
    CHECK(w->SaveToSet() && FileBytes(settingsPath + "/Digi-30508_PSD.bin").size() == SETTINGSIZE * 4 && FileBytes(run2 + "/" + name2 + "_30508_PSD.bin") != QByteArray()
          && FileBytes(snap2) == snap2Bytes, "Save to set did not put Digi-30508 into the set, or wrote the run");
    (void) set30508;
    QJsonObject rf = RunFile(run3, QFileInfo(run3).fileName() + "_run.json");
    QJsonObject rc = rf["run_control"].toObject();
    CHECK(rc["opened_with"].toString() == "settings from run " + name2 && rc["settings_set"].toObject()["name"].toString() == "settings" && rc["settings_path"].toString() == settingsPath,
          "run file: opened_with \"%s\"", qPrintable(rc["opened_with"].toString()));
    w->CloseDigitizers();

    //---- another run chosen, its run protocol missing: kept, and said
    QFile::remove(run1 + "/" + name1 + "_run_protocol.json");
    answers["Settings from a run"] = name1;
    dialogs.clear();
    w->OpenDigitizers();
    w->UpdateRunControls();
    CHECK(w->openedFromRun == name1 && fake_caen_reg(15880, ThrAddr(0)) == 0x31 && Dialogs("Settings files") == 0, "run %s chosen: opened from %s, threshold 0x%X", qPrintable(name1), qPrintable(w->openedFromRun), fake_caen_reg(15880, ThrAddr(0)));
    CHECK(Log().contains("Run " + name1 + " has no run protocol"), "missing run protocol not said");
    answers.clear();
    w->CloseDigitizers();

    //---- no run with snapshots: nothing opened
    NewWorkDir(); AddBoards(); NewFSUDAQ();
    w->cbOpenMethod->setCurrentIndex(w->cbOpenMethod->findData(2));
    dialogs.clear();
    w->OpenDigitizers();
    CHECK(w->digi == nullptr && Dialogs("Settings from a run") == 1, "no run: digi %p, dialogs %s", (void *) w->digi, qPrintable(dialogs.join(" | ")));
    w->cbOpenMethod->setCurrentIndex(w->cbOpenMethod->findData(1));
    if( nFail == nFail0 ) OK("newest run offered first and named; boards from its snapshots, the one without from the set (warned); protocol restored, not saved; run files untouched; a board the set lacks marked, Save to set puts it there");
  }

  void RunCopies(){
    printf("== the run folder: programSettings.txt (secrets masked) and the run protocol beside the snapshots\n");
    int nFail0 = nFail;
    NewWorkDir(); AddBoards();
    delete w; w = nullptr;
    {
      QFile f(work + "/programSettings.txt");
      f.open(QIODevice::WriteOnly | QIODevice::Text);
      f.write((dataPath + "\n\n\nSECRETTOKEN123\n\n\n\n\nPWD456\nsettingsPath=" + settingsPath + "\nautoIncrementRunID=1\nautoDashboard=0\n").toUtf8());
    }
    NewFSUDAQ();
    Open();
    w->chkSaveData->setChecked(true);
    dialogs.clear();
    w->AutoRun();
    CHECK(w->inDataRun, "the run did not start (%s)", qPrintable(dialogs.join(" | ")));
    const QString runDir = w->RunFolder(), name = QFileInfo(runDir).fileName();
    Pump(100); w->StopPressed();
    const QByteArray copy = FileBytes(runDir + "/" + name + "_programSettings.txt");
    CHECK(!copy.isEmpty() && !copy.contains("SECRETTOKEN123") && !copy.contains("PWD456") && copy.count("masked in the run's copy") == 2
          && copy.startsWith((dataPath + "\n").toUtf8()) && copy.contains(("settingsPath=" + settingsPath + "\n").toUtf8()), "programSettings copy:\n%s", copy.constData());
    QJsonObject proto = ReadJson(runDir + "/" + name + "_run_protocol.json");
    QJsonObject rf = RunFile(runDir, name + "_run.json");
    QJsonObject rc = rf["run_control"].toObject(), led = rf["led"].toObject();
    QJsonObject protoNoFormat = proto; protoNoFormat.remove("format");
    CHECK(proto["format"].toString() == "FSUDAQ run protocol 1" && rc["run_protocol"].toObject() == protoNoFormat && rc["run_protocol_file"].toString() == name + "_run_protocol.json", "the run protocol copy and the run file differ");
    CHECK(proto["maxFileSizeMB"].toInt() == rc["file_size_mb"].toInt() && proto["runLengthMin"].toDouble() == rc["run_length_min"].toDouble()
          && proto["ledTrigger"].toBool() == led["enabled"].toBool() && proto["ledDelaySec"].toDouble() == led["delay_s"].toDouble() && proto["ledHoldSec"].toDouble() == led["hold_s"].toDouble(),
          "run protocol copy disagrees with run_control/led");
    QJsonObject b0 = rf["boards"].toArray().first().toObject();
    CHECK(!b0["settings_file"].toString().isEmpty() && b0["programmed_from"].toString() == b0["settings_file"].toString() && rc["opened_with"].toString() == "settings set settings"
          && rc["settings_set"].toObject()["path"].toString() == settingsPath, "run file: settings_file %s, programmed_from %s, opened_with %s", qPrintable(b0["settings_file"].toString()), qPrintable(b0["programmed_from"].toString()), qPrintable(rc["opened_with"].toString()));

    //---- the copy cannot be made: no run, as for a snapshot
    const unsigned int before = w->runID;
    chmod(qPrintable(work + "/programSettings.txt"), 0);
    dialogs.clear();
    w->AutoRun();
    chmod(qPrintable(work + "/programSettings.txt"), 0644);
    CHECK(!w->inDataRun && Dialogs("Run not started") == 1 && dialogs.join(" ").contains("programSettings.txt") && w->runID == before,
          "unreadable programSettings.txt: run %d, dialogs %s", w->inDataRun, qPrintable(dialogs.join(" | ")));
    w->CloseDigitizers();
    if( nFail == nFail0 ) OK("token and password masked, the rest as on disk; run protocol copy = run file's; set and source in the run file; a copy that cannot be made stops the start");
  }

  void BoardClocks(){
    printf("== board clocks (PLL): checked after the open, at Start and at Stop; a board not locked refuses Start; a loss in the run is a stop problem\n");
    int nFail0 = nFail;
    NewWorkDir(); AddBoards(3, 1); NewFSUDAQ();
    Open();
    w->chkSaveData->setChecked(true);
    CHECK(w->lbPllStatus->text().startsWith("all locked"), "after the open: \"%s\"", qPrintable(w->lbPllStatus->text()));
    const int sn = w->digi[1]->GetSerialNumber();
    const QString name = "Digi-" + QString::number(sn);

    // not locked now: no run, one dialog naming the board
    dialogs.clear();
    fake_caen_set_pll(sn, 0, 0);
    w->AutoRun();
    CHECK(!w->inDataRun && dialogs.size() == 1 && dialogs[0].contains(name) && dialogs[0].contains("not locked"),
          "board not locked: run %d, dialogs %s", w->inDataRun, qPrintable(dialogs.join(" | ")));
    CHECK(w->lbPllStatus->text().contains("NOT LOCKED: " + name), "status \"%s\"", qPrintable(w->lbPllStatus->text()));

    // locked again after a loss: the run starts with a warning; a loss in the run: one dialog at the stop, both in the run file
    dialogs.clear();
    fake_caen_set_pll(sn, 1, 1);
    w->AutoRun();
    CHECK(w->inDataRun && dialogs.isEmpty(), "locked again: run %d, dialogs %s", w->inDataRun, qPrintable(dialogs.join(" | ")));
    const QString runDir = w->RunFolder();
    Pump(200);
    fake_caen_set_pll(sn, 1, 1);
    w->StopPressed();
    CHECK(dialogs.size() == 1 && dialogs[0].contains(name + " lost its clock lock"), "lost in the run: dialogs %s", qPrintable(dialogs.join(" | ")));
    QJsonObject rf = RunFile(runDir, QFileInfo(runDir).fileName() + "_run.json");
    const QJsonObject atStart = rf["clock"].toObject()["start"].toObject(), atStop = rf["stop"].toObject()["clock"].toObject();
    CHECK(atStart["lost_since_last_check"].toArray() == QJsonArray({name}) && atStart["unlocked"].toArray().isEmpty()
          && atStop["lost_since_last_check"].toArray() == QJsonArray({name}),
          "run file clock: start %s, stop %s", QJsonDocument(atStart).toJson(QJsonDocument::Compact).constData(), QJsonDocument(atStop).toJson(QJsonDocument::Compact).constData());

    // all well again: no dialog, all locked
    dialogs.clear();
    w->AutoRun();
    Pump(200);
    w->StopPressed();
    CHECK(dialogs.isEmpty() && w->lbPllStatus->text().startsWith("all locked"), "a good run: dialogs %s, status \"%s\"",
          qPrintable(dialogs.join(" | ")), qPrintable(w->lbPllStatus->text()));
    w->CloseDigitizers();
    CHECK(w->lbPllStatus->text().startsWith("not checked"), "after the close: \"%s\"", qPrintable(w->lbPllStatus->text()));
    if( nFail == nFail0 ) OK("not locked: Start refused, named; lost and locked again: the run starts; lost in the run: one dialog; both in the run file; reset at the close");
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
      if( b ) dialogs.last() += " => " + b->text().remove('&');
      if( b ) b->click(); else box->done(0);
    }else if( auto * in = qobject_cast<QInputDialog *>(m) ){
      // a choice: the item starting with the answer for its title, else the one preselected
      if( answers.contains(in->windowTitle()) ){
        if( in->comboBoxItems().isEmpty() ) in->setTextValue(answers[in->windowTitle()]);   // a name asked for
        for( const QString & item : in->comboBoxItems() ) if( item.startsWith(answers[in->windowTitle()] + " ") ) in->setTextValue(item);
      }
      dialogs << in->windowTitle() + ": " + in->textValue();
      printf("         [dialog] %s: %s\n", qPrintable(in->windowTitle()), qPrintable(in->textValue()));
      in->accept();
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
  t.ScopeTraceSelection();
  t.ScopeChangesWhileRunning();
  t.ScopeForceFlush();
  t.ScopeOnlyThisChannel();
  t.ScopeChangesUndone();
  t.StartStopOrder();
  t.DrainWaitsForFlushedData();
  t.ProblemAtStopEndsSeries();
  t.FailureInSeriesEndsSeries();
  t.DataFileCannotGrow();
  t.ReadLongerThanBuffer();
  t.LedTriggerAndRunFile();
  t.BoardClocks();
  t.SettingsSets();
  t.SaveAsNewSet();
  t.PanelKeepsTheSet();
  t.UnsavedEditsAtStart();
  t.SettingsFromRun();
  t.RunCopies();

  delete t.w;
  QDir(t.work).removeRecursively();
  printf("\n%s: %d problem(s)\n", nFail ? "FAILED" : "PASSED", nFail);
  return nFail ? 1 : 0;
}
