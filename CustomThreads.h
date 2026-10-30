#ifndef CUSTOMTHREADS_H
#define CUSTOMTHREADS_H

#include <QThread>
#include <QMutex>
#include <QMutexLocker>
#include <vector>
#include <algorithm>
#include <chrono>
#include <signal.h>
#include <pthread.h>
#include <QWaitCondition>
#include <QMessageBox>
#include <QCoreApplication>
#include <atomic>

#include "macro.h"
#include "ClassDigitizer.h"

static QMutex digiMTX[MaxNBoards * MaxNPorts];

//^#===================================================== ReadData Thread
class ReadDataThread : public QThread {
  Q_OBJECT
public:
  ReadDataThread(Digitizer * dig, int digiID, QObject * parent = 0) : QThread(parent){ 
    this->digi = dig;
    this->ID = digiID;
    isSaveData = false;
    isScope = false;
    readCount = 0;
    stop = false;
    drain = false;
    failed = false;
  }
  /// start the readout. Use this, not start(): the stop request is cleared here, before the thread
  /// runs; cleared in run(), a Stop() right after the start was lost and the thread never ended
  void Start() { stop = false; drain = false; failed = false; boardStopped = false; drainCut = false; failReason.clear(); start(); }
  void Stop() { drain = false; this->stop = true;}          /// leave at once (also ends a drain); what the board still holds is not read
  void StopAfterDrain() { drain = true; this->stop = true; }  /// stop the board (from this thread), read and save what it still holds, then leave
  bool Failed() const { return failed; }                     /// the last run of this thread ended on a read error, not on Stop
  bool BoardStopped() const { return boardStopped; }         /// in a drain: this thread has stopped its board
  bool DrainCut() const { return drainCut; }                 /// the last drain ended before the board was empty: data left on it
  QString FailReason() const { return failReason; }
  void SetSaveData(bool onOff)  {this->isSaveData = onOff;}
  void SetScopeMode(bool onOff) {this->isScope = onOff;}
  /// the scope's auto trigger, as CoMPASS's (an oscilloscope's force trigger): a software trigger to channel ch
  /// every AutoTriggerMiliSec while the board is read, in OR with the self-triggers; -1 = off
  void SetAutoTrigger(int ch) {this->autoTriggerCh = ch;}
  /// the scope's Force: one software trigger to channel ch at the next read
  void ForceTrigger(int ch) {this->forceTriggerCh = ch;}

  void SetReadCountZero() {readCount = 0;}
  unsigned long GetReadCount() const {return readCount;}

  // Newest trace of each channel, copied by this thread right after each decode in scope mode,
  // while it holds the board's data anyway. The scope draws from this copy and never reads
  // the Data ring the decoder writes (that race corrupted the heap, 29 Sep 2026).
  struct ScopeTrace {
    bool valid = false;
    long absIndex = -1;        // ring position of the copied event (LoopIndex * size + DataIndex)
    float trigRate = 0;
    std::chrono::steady_clock::time_point when{};
    std::vector<short> wf1, wf2;
    std::vector<bool>  dwf1, dwf2, dwf3, dwf4;
  };
  bool GetScopeTrace(int ch, ScopeTrace & out){
    if( ch < 0 || ch >= MaxNChannels ) return false;
    QMutexLocker locker(&scopeMTX);
    out = scopeTrace[ch];
    return out.valid;
  }

  void run(){

    // Keep asynchronous signals away from this thread. A process-directed signal (SIGCHLD
    // when a child such as the browser opener exits, SIGPIPE, ...) is delivered to any
    // thread that does not block it; if that thread is inside a blocking CAEN driver call
    // the wait is interrupted and both drivers mishandle it (a5818 aborts the DMA, a3818
    // leaves the packet buffered), so every board read fails at once (29 Sep 2026).
    {
      sigset_t set;
      sigemptyset(&set);
      sigaddset(&set, SIGCHLD); sigaddset(&set, SIGPIPE); sigaddset(&set, SIGHUP);
      sigaddset(&set, SIGUSR1); sigaddset(&set, SIGUSR2); sigaddset(&set, SIGALRM);
      sigaddset(&set, SIGINT);  sigaddset(&set, SIGTERM);   // the main thread still gets these
      pthread_sigmask(SIG_BLOCK, &set, nullptr);
    }

    readCount = 0;
    flushCh = -1;
    forceTriggerCh = -1;
    if( isScope ){ QMutexLocker locker(&scopeMTX); for( auto & t : scopeTrace ) t = ScopeTrace(); }
    clock_gettime(CLOCK_REALTIME, &t0);
    // ta = t0;
    t1 = t0;

    digiMTX[ID].lock();
    digi->ReadACQStatus();
    digiMTX[ID].unlock();

    printf("ReadDataThread for digi-%d running.\n", digi->GetSerialNumber());
    int emptyReads = 0;                                   // reads in a row that found nothing, once the board is stopped
    std::chrono::steady_clock::time_point drainStart{}, lastData{};
    do{
      
      // drain is set before stop: whoever sees stop also sees drain (both atomic)
      if( stop && !drain ) break;                         // Stop(): leave at once
      if( drain && !boardStopped ){
        // the board is stopped here, by the thread that reads it: no CAEN call on this board from
        // another thread while it reads (the board mutexes are per file, they do not exclude this one)
        digiMTX[ID].lock();
        digi->SWStopACQ();
        digiMTX[ID].unlock();
        boardStopped = true;
        drainStart = lastData = std::chrono::steady_clock::now();
      }

      {
        const int forceCh = forceTriggerCh.exchange(-1);
        if( !boardStopped ){
          const auto now = std::chrono::steady_clock::now();
          // a board without Automatic Data Flush (0x8000 bit 0) holds a lone triggered event until its aggregate
          // fills: flush it once the event is recorded
          if( flushCh >= 0 && now >= flushAt ){
            digiMTX[ID].lock();
            digi->ForceDataFlush(flushCh);
            digiMTX[ID].unlock();
            flushCh = -1;
          }
          int ch = forceCh;
          if( ch < 0 && autoTriggerCh >= 0 && now - lastAutoTrigger >= std::chrono::milliseconds(AutoTriggerMiliSec) ) ch = autoTriggerCh;
          if( ch >= 0 ){
            digiMTX[ID].lock();
            const bool sent = digi->SendSoftwareTrigger(ch);
            const bool autoFlush = Digitizer::ExtractBits(digi->GetSettingFromMemory(DPP::BoardConfiguration), DPP::Bit_BoardConfig::EnableAutoDataFlush);
            digiMTX[ID].unlock();
            if( forceCh < 0 ) lastAutoTrigger = now;
            if( sent && !autoFlush ){ flushCh = ch; flushAt = now + std::chrono::milliseconds(1); }
          }
        }
      }

      digiMTX[ID].lock();
      int ret = digi->ReadData();
      uint32_t nByte = digi->GetData()->nByte;
      digiMTX[ID].unlock();
      readCount ++;

      if( stop && !drain ) break;

      if( ret == CAEN_DGTZ_Success ){
        if( nByte > 0 ){
          emptyReads = 0;
          lastData = std::chrono::steady_clock::now();
          digiMTX[ID].lock();
          digi->GetData()->DecodeBuffer(!isScope, 0);
          if( isSaveData ) digi->GetData()->SaveData();
          if( isScope ) CopyScopeTraces(digi->GetData());
          digiMTX[ID].unlock();
        }else if( boardStopped ){
          // empty three times, 50 ms apart: the stopped board has nothing left (the flush written at
          // the stop releases the aggregates still being filled)
          if( ++emptyReads >= 3 ) break;
          usleep(50000);
        }
        // a board read steadily is never cut off; one that stops giving data without ever reading empty,
        // or a drain beyond 2 min, is: the rest stays on the board and the stop says so
        const auto t = std::chrono::steady_clock::now();
        if( boardStopped && (t - lastData > std::chrono::seconds(5) || t - drainStart > std::chrono::seconds(120)) ){
          failReason = "data were left on the board at the stop (the readout after the stop did not end)";
          drainCut = true;
          printf("\033[31mDigi-%d: %s.\033[0m\n", digi->GetSerialNumber(), failReason.toStdString().c_str());
          break;
        }

      }else{
        printf("ReadDataThread::%s------------ ret : %d \n", __func__, ret);
        digiMTX[ID].lock();
        digi->StopACQ();
        if( ret == CAEN_DGTZ_OutOfMemory ){
          digi->WriteRegister(DPP::SoftwareClear_W, 1);
          digi->GetData()->ClearData();
        }
        digiMTX[ID].unlock();
        failReason = "board read failed (CAEN error " + QString::number(ret) + ")";
        failed = true;
        emit sendMsg("<font style=\"color: red;\">Digi-" + QString::number(digi->GetSerialNumber()) + " ACQ off: " + failReason + ".</font>");
        stop = true;
        break;
      }

      clock_gettime(CLOCK_REALTIME, &t2);
      if( t2.tv_sec - t1.tv_sec > 1 ){
        digiMTX[ID].lock();
        digi->ReadACQStatus();
        digiMTX[ID].unlock();
        t1 = t2;
        // QCoreApplication::processEvents();
      }

      // if( isSaveData && !stop ) {
      //   clock_gettime(CLOCK_REALTIME, &tb);
      //   if( tb.tv_sec - ta.tv_sec > 2 ) {
      //     digiMTX[ID].lock();
      //     emit sendMsg("FileSize ("+ QString::number(digi->GetSerialNumber()) +"): " +  QString::number(digi->GetData()->GetTotalFileSize()/1024./1024., 'f', 4) + " MB [" + QString::number(tb.tv_sec-t0.tv_sec) + " sec]");
      //     //emit sendMsg("FileSize ("+ QString::number(digi->GetSerialNumber()) +"): " +  QString::number(digi->GetData()->GetTotalFileSize()/1024./1024., 'f', 4) + " MB [" + QString::number(tb.tv_sec-t0.tv_sec) + " sec] (" + QString::number(readCount) + ")");
      //     digiMTX[ID].unlock();
      //     // readCount = 0;
      //     ta = tb;
      //   }
      // }
    }while(true); // every way out is above: Stop(), the end of a drain, a read error
    printf("ReadDataThread for digi-%d stopped.\n", digi->GetSerialNumber());
  }
signals:
  void sendMsg(const QString &msg);
private:
  void CopyScopeTraces(Data * data){
    QMutexLocker locker(&scopeMTX);
    const auto now = std::chrono::steady_clock::now();
    const int nCh = std::min(digi->GetNumInputCh(), (int) MaxNChannels);
    for( int ch = 0; ch < nCh; ch++){
      ScopeTrace & t = scopeTrace[ch];
      t.trigRate = data->TriggerRate[ch];
      const int index = data->GetDataIndex(ch);
      if( index < 0 ) continue;
      const long absIndex = data->GetAbsDataIndex(ch);
      if( t.valid && absIndex == t.absIndex ) continue;   // no new event on this channel
      t.valid = true;
      t.absIndex = absIndex;
      t.when = now;
      t.wf1  = data->Waveform1[ch][index];
      t.wf2  = data->Waveform2[ch][index];
      t.dwf1 = data->DigiWaveform1[ch][index];
      t.dwf2 = data->DigiWaveform2[ch][index];
      t.dwf3 = data->DigiWaveform3[ch][index];
      t.dwf4 = data->DigiWaveform4[ch][index];
    }
  }
  QMutex scopeMTX;
  ScopeTrace scopeTrace[MaxNChannels];

  Digitizer * digi; 
  std::atomic<bool> stop;
  std::atomic<bool> drain;
  std::atomic<bool> failed;
  std::atomic<bool> boardStopped;   // this thread stopped its board for the drain
  std::atomic<bool> drainCut;       // the drain ended with data left on the board
  QString failReason;      // written before failed is set
  int ID;
  timespec ta, tb, t1, t2, t0;
  bool isSaveData;
  bool isScope;
  std::atomic<int> autoTriggerCh{-1};
  std::chrono::steady_clock::time_point lastAutoTrigger{};
  std::atomic<int> forceTriggerCh{-1};
  int flushCh = -1;                                   // channel to flush at flushAt, after a software trigger
  std::chrono::steady_clock::time_point flushAt{};
  unsigned long readCount; 
};

//^#======================================================= Timing Thread
class TimingThread : public QThread {
  Q_OBJECT
public:
  TimingThread(QObject * parent = 0 ) : QThread(parent){
    waitTime = 20; // multiple of 100 mili sec
    stop = false;
  }
  bool isStopped() const {return stop;}
  void Stop() { this->stop = true;}
  void SetWaitTimeinSec(float sec) {waitTime = sec * 10 ;}
  float GetWaitTimeinSec() const {return waitTime/10.;}
  void DoOnce() {emit timeUp();};
  void Start(){ stop = false; start(); } /// clears the stop request before the thread runs (run() must not: a Stop() right after start() would be lost)
  void run(){
    unsigned int count  = 0;
    do{
      usleep(100000);
      count ++;
      if( count % waitTime == 0){
        emit timeUp();
      }
    }while(!stop);
  }
signals:
  void timeUp();
private:
  std::atomic<bool> stop;
  unsigned int waitTime;
};

#endif
