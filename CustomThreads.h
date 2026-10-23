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
  void Start() { stop = false; drain = false; failed = false; boardStopped = false; failReason.clear(); start(); }
  void Stop() { drain = false; this->stop = true;}          /// leave at once (also ends a drain); what the board still holds is not read
  void StopAfterDrain() { drain = true; this->stop = true; }  /// stop the board (from this thread), read and save what it still holds, then leave
  bool Failed() const { return failed; }                     /// the last run of this thread ended on a read error, not on Stop
  bool BoardStopped() const { return boardStopped; }         /// in a drain: this thread has stopped its board
  QString FailReason() const { return failReason; }
  void SetSaveData(bool onOff)  {this->isSaveData = onOff;}
  void SetScopeMode(bool onOff) {this->isScope = onOff;}

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
    if( isScope ){ QMutexLocker locker(&scopeMTX); for( auto & t : scopeTrace ) t = ScopeTrace(); }
    clock_gettime(CLOCK_REALTIME, &t0);
    // ta = t0;
    t1 = t0;

    digiMTX[ID].lock();
    digi->ReadACQStatus();
    digiMTX[ID].unlock();

    printf("ReadDataThread for digi-%d running.\n", digi->GetSerialNumber());
    int emptyReads = 0;                                   // reads in a row that found nothing, once the board is stopped
    std::chrono::steady_clock::time_point drainStart{};
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
        drainStart = std::chrono::steady_clock::now();
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
        if( boardStopped && std::chrono::steady_clock::now() - drainStart > std::chrono::seconds(5) ){
          printf("\033[31mDigi-%d: still reading data 5 s after the stop; the rest is left on the board.\033[0m\n", digi->GetSerialNumber());
          emit sendMsg("<font style=\"color: red;\">Digi-" + QString::number(digi->GetSerialNumber()) + ": still reading data 5 s after the stop; the rest is left on the board.</font>");
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
  QString failReason;      // written before failed is set
  int ID;
  timespec ta, tb, t1, t2, t0;
  bool isSaveData;
  bool isScope;
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
  void run(){
    unsigned int count  = 0;
    stop = false;
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
  bool stop;
  unsigned int waitTime;
};

#endif
