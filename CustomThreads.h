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
  }
  void Stop() { this->stop = true;}
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

    stop = false;
    readCount = 0;
    if( isScope ){ QMutexLocker locker(&scopeMTX); for( auto & t : scopeTrace ) t = ScopeTrace(); }
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
        digi->GetData()->DecodeBuffer(!isScope, 0);
        if( isSaveData ) digi->GetData()->SaveData();
        if( isScope ) CopyScopeTraces(digi->GetData());
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
