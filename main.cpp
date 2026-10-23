#include <QApplication>
#include <QMessageBox>
#include <QProcess>
#include <QPushButton>
#include <QFile>
#include <QLocale>
#include <QSocketNotifier>

#include "FSUDAQ.h"

#include <QObject>
#include <QDebug>

#include <sys/resource.h>

#include <csignal>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <execinfo.h>
#include <unistd.h>

// On SIGSEGV: say where it happened and dump this thread's stack to stderr (the
// launcher script logs stderr), then let the default action run so the kernel logs
// the fault and a real core file is written (ulimit -c is raised by the launcher).
static void crashHandler(int sig, siginfo_t * info, void *) {
    char line[128];
    int n = snprintf(line, sizeof(line), "\nSignal received: %d (fault address %p), backtrace:\n", sig, info ? info->si_addr : nullptr);
    if( write(STDERR_FILENO, line, n) < 0 ) {}
    void * frames[64];
    int nFrames = backtrace(frames, 64);
    backtrace_symbols_fd(frames, nFrames, STDERR_FILENO);
    signal(sig, SIG_DFL);
    raise(sig);
}

// Ctrl-C, kill and a closed terminal: the handler only writes the signal number to a pipe; the
// GUI thread reads it and stops the run properly (boards read out, files closed, recorded).
static int signalPipe[2] = {-1, -1};
static void shutdownHandler(int sig) {
    unsigned char s = (unsigned char) sig;
    if( write(signalPipe[1], &s, 1) < 0 ) {}
}

int main(int argc, char *argv[]){

    setvbuf(stdout, nullptr, _IOLBF, 0);   // line-buffered even into a pipe (launcher, gdb), so the log is live

    {
        // the first backtrace() loads libgcc (and allocates); do it now, not in the crash handler,
        // where a crash inside malloc would make it hang
        void * frame[1];
        backtrace(frame, 1);

        struct sigaction sa;
        memset(&sa, 0, sizeof(sa));
        sa.sa_sigaction = crashHandler;
        sa.sa_flags = SA_SIGINFO | SA_RESETHAND;
        sigaction(SIGSEGV, &sa, nullptr);
        sigaction(SIGBUS, &sa, nullptr);
        sigaction(SIGFPE, &sa, nullptr);

        // a launcher's "| tee" that dies must not kill the DAQ at its next printf
        signal(SIGPIPE, SIG_IGN);
    }

    setpriority(PRIO_PROCESS, 0, -20);

    // CustomApplication a(argc, argv);
    QApplication a(argc, argv);

    // Set Locale
    QLocale::setDefault(QLocale::system());

    // Set Lock file
    bool isLock = false;
    int pid = 0;
    QFile lockFile(DAQLockFile);
    if( lockFile.open(QIODevice::Text | QIODevice::ReadOnly) ){
        QTextStream in(&lockFile);
        QString line = in.readLine();
        isLock = line.toInt();
        lockFile.close();
    }
    QFile pidFile(PIDFile);
    if( pidFile.open(QIODevice::Text | QIODevice::ReadOnly)){
        QTextStream in(&pidFile);
        QString line = in.readLine();
        pid = line.toInt();
        pidFile.close();
    }
    if( isLock ) {
        qDebug() << "The DAQ program is already opened. PID is " + QString::number(pid) + ", and delete the " + DAQLockFile ;
        QMessageBox msgBox;
        msgBox.setWindowTitle("Oopss....");
        msgBox.setText("The DAQ program is already opened, or crashed perviously. \nPID is " + QString::number(pid) + "\n You can kill the procee by \"kill -9 <pid>\" and delete the " + DAQLockFile + "\n or click the \"Kill\" button");
        msgBox.setIcon(QMessageBox::Information);
    
        QPushButton * kill = msgBox.addButton("Kill and Open New", QMessageBox::AcceptRole);
        msgBox.setStandardButtons(QMessageBox::Ok);
        msgBox.setDefaultButton(QMessageBox::Ok);
        msgBox.exec();
        if(msgBox.clickedButton() == kill){
            remove(DAQLockFile);
            QProcess::execute("kill", QStringList() << "-9" << QString::number(pid));
        }else{
            return 0;
        }
    }
    lockFile.open(QIODevice::Text | QIODevice::WriteOnly);
    lockFile.write( "1" );
    lockFile.close();
    pidFile.open(QIODevice::Text | QIODevice::WriteOnly);
    pidFile.write(  QString::number(QCoreApplication::applicationPid() ).toStdString().c_str() );
    pidFile.close();

    FSUDAQ w;
    w.show();

    if( pipe(signalPipe) == 0 ){
        struct sigaction sa;
        memset(&sa, 0, sizeof(sa));
        sa.sa_handler = shutdownHandler;
        sa.sa_flags = SA_RESTART;
        sigaction(SIGINT, &sa, nullptr);
        sigaction(SIGTERM, &sa, nullptr);
        sigaction(SIGHUP, &sa, nullptr);
        QSocketNotifier * notifier = new QSocketNotifier(signalPipe[0], QSocketNotifier::Read, &a);
        QObject::connect(notifier, &QSocketNotifier::activated, &w, [&w, notifier](){
            unsigned char s = 0;
            if( read(signalPipe[0], &s, 1) != 1 ) return;
            notifier->setEnabled(false); // once; a second Ctrl-C while stopping changes nothing
            w.ShutdownOnSignal(s);
        });
    }

    return a.exec();
}