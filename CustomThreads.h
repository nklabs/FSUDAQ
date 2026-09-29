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

#include "macro.h"
#include "ClassDigitizer.h"

// A mutex that hands over to a waiting thread instead of letting the thread that just
// released it take it straight back. The readout thread locks and unlocks its board
// mutex back to back (board read, decode, next read, ...) and QMutex is not fair: a
// GUI thread that waits in the kernel loses that race every time, so with a plain
// QMutex the scope timer never got the lock while the readout thread was busy and the
// whole GUI froze (29 Sep 2026). Threads queue on the turnstile before taking the data
// mutex, so a waiter that holds the turnstile is next and the releasing thread queues
// behind it. Only lock()/unlock() are needed by the code base.
class HandoffMutex {
public:
  void lock()   { turnstile.lock(); data.lock(); turnstile.unlock(); }
  void unlock() { data.unlock(); }
private:
  QMutex turnstile;
  QMutex data;
};

// One mutex per board, shared by every translation unit. This used to be `static`,
// which gave each .cpp file its own private copy, so the readout thread and the
// GUI code were never actually locking the same mutex.
inline HandoffMutex digiMTX[MaxNBoards * MaxNPorts];

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
  }
  void Stop() { this->stop = true;}
  void SetSaveData(bool onOff)  {this->isSaveData = onOff;}
  void SetScopeMode(bool onOff) {this->isScope = onOff;}

  void SetReadCountZero() {readCount = 0;}
  unsigned long GetReadCount() const {return readCount;}

  // Newest trace of each channel, copied out of Data after every decode in scope mode.
  // The scope draws from this copy instead of locking digiMTX, which this thread holds
  // for the whole board read (up to ~100 ms per 8 MB read at the link limit).
  struct ScopeTrace {
    unsigned long count = 0;   // traces decoded for this channel so far, 0 = none yet
    float trigRate = 0;
    std::chrono::steady_clock::time_point when{};   // when the trace was copied out of Data
    std::vector<short> wf1, wf2;
    std::vector<bool>  dwf1, dwf2, dwf3, dwf4;
    long long AgeMs() const { return std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::steady_clock::now() - when).count(); }
  };
  bool GetScopeTrace(int ch, ScopeTrace & out){
    if( ch < 0 || ch >= MaxNChannels ) return false;
    QMutexLocker locker(&scopeMTX);
    out = scopeTrace[ch];
    return out.count > 0;
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

    stop = false;
    readCount = 0;
    if( isScope ) { QMutexLocker locker(&scopeMTX); for( auto & t : scopeTrace ) t = ScopeTrace(); }
    clock_gettime(CLOCK_REALTIME, &t0);
    // ta = t0;
    t1 = t0;

    digiMTX[ID].lock();
    digi->ReadACQStatus();
    digiMTX[ID].unlock();

    printf("ReadDataThread for digi-%d running.\n", digi->GetSerialNumber());
    do{
      
      if( stop) break;

      digiMTX[ID].lock();
      int ret = digi->ReadData();
      digiMTX[ID].unlock();
      readCount ++;

      if( stop) break;

      if( ret == CAEN_DGTZ_Success && !stop){
        digiMTX[ID].lock();
        Data * data = digi->GetData();
        data->SetTraceOnlyLastEvent(isScope);   // scope: one trace per channel block, the other traces are skipped
        data->DecodeBuffer(!isScope, 0);
        if( isSaveData ) data->SaveData();
        if( isScope ) PublishScopeTraces(data);
        digiMTX[ID].unlock();

      }else{
        printf("ReadDataThread::%s------------ ret : %d \n", __func__, ret);
        digiMTX[ID].lock();
        digi->StopACQ();
        if( ret == CAEN_DGTZ_OutOfMemory ){
          digi->WriteRegister(DPP::SoftwareClear_W, 1);
          digi->GetData()->ClearData();
        }
        digiMTX[ID].unlock();
        emit sendMsg("Digi-" + QString::number(digi->GetSerialNumber()) + " ACQ off.");
        stop = true;
        break;
      }

      clock_gettime(CLOCK_REALTIME, &t2);
      if( t2.tv_sec - t1.tv_sec > 1 ){
        digiMTX[ID].lock();
        digi->ReadACQStatus();
        digiMTX[ID].unlock();
        t2 = t1;
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
    }while(!stop);
    digiMTX[ID].lock();
    digi->GetData()->SetTraceOnlyLastEvent(false);
    digiMTX[ID].unlock();
    printf("ReadDataThread for digi-%d stopped.\n", digi->GetSerialNumber());
  }
signals:
  void sendMsg(const QString &msg);
private:
  // Called with digiMTX[ID] held, right after a decode in scope mode.
  void PublishScopeTraces(Data * data){
    QMutexLocker locker(&scopeMTX);
    const int nCh = std::min(digi->GetNumInputCh(), (int) MaxNChannels);
    for( int ch = 0; ch < nCh; ch++){
      ScopeTrace & t = scopeTrace[ch];
      t.trigRate = data->TriggerRate[ch];
      if( t.count == data->ScopeTraceCount[ch] ) continue;   // no new trace for this channel
      t.count = data->ScopeTraceCount[ch];
      t.when  = std::chrono::steady_clock::now();
      t.wf1  = data->ScopeWaveform1[ch];
      t.wf2  = data->ScopeWaveform2[ch];
      t.dwf1 = data->ScopeDigiWaveform1[ch];
      t.dwf2 = data->ScopeDigiWaveform2[ch];
      t.dwf3 = data->ScopeDigiWaveform3[ch];
      t.dwf4 = data->ScopeDigiWaveform4[ch];
    }
  }
  QMutex scopeMTX;
  ScopeTrace scopeTrace[MaxNChannels];

  Digitizer * digi; 
  bool stop;
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
