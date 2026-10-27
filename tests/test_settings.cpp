// Do the settings in a board's settings file end up on the board?
//
// For each settings file, a fake board (tests/fake_caen.cpp) with the real board's identity
// is opened the way FSUDAQ opens it "w/ settings", then every read/write register on the
// board is compared with the file, and with FSUDAQ's in-memory copy (what the GUI shows and
// what the run snapshot records).
//
//   test_settings DIR    DIR holds Digi-<serial>_PSD.bin files (default tests/data/odin-runB)

#include "ClassDigitizer.h"
#include "fake_caen.h"

#include <dirent.h>
#include <unistd.h>
#include <algorithm>
#include <set>

// Read/write board registers deliberately not taken from the settings file: VME-bus and
// interrupt plumbing, the fan, and the board ID that FSUDAQ sets from the firmware type.
static const std::set<uint32_t> NotFromFile = {
  0x8168, // FanSpeedControl
  0xEF08, // BoardID: written at open, (DPP type & 0xF); the data format depends on it
  0xEF0C, // MCSTBaseAddressAndControl
  0xEF10, // RelocationAddress
  0xEF14, // InterruptStatusID
  0xEF18, // InterruptEventNumber
  0xEF20, // Scratch
  0xEF00, // ReadoutControl: the CAEN library sets it at open (bus-error enable for block transfers)
};

static int nFail = 0;
#define CHECK(cond, ...) do{ if( !(cond) ){ nFail++; printf("   FAIL  "); printf(__VA_ARGS__); printf("\n"); } }while(0)

static uint32_t FileWord(const std::vector<uint32_t> & file, Reg reg, int ch) { return file[reg.Index(ch)]; }

static void CheckBoard(const std::string & path, int serial, int port) {
  std::vector<uint32_t> file(SETTINGSIZE);
  FILE * f = fopen(path.c_str(), "rb");
  if( !f || fread(file.data(), 4, SETTINGSIZE, f) != SETTINGSIZE ){ printf("cannot read %s\n", path.c_str()); nFail++; if( f ) fclose(f); return; }
  fclose(f);

  Reg chMaskReg = DPP::RegChannelEnableMask;
  bool eightCh = (FileWord(file, chMaskReg, 0) & 0xFF00) == 0;
  fake_caen_add_board(1, port, 0, serial, eightCh ? "DT5730" : "V1730", path.c_str());

  //---- what FSUDAQ::OpenDigitizers does for "w/ settings"
  Digitizer digi(0, port);
  CHECK(digi.IsConnected(), "Digi-%d did not open", serial);
  if( !digi.IsConnected() ) return;
  std::vector<std::string> problems;
  int nProblem = digi.LoadAndProgramSettings(path, problems);
  CHECK(nProblem == 0 && digi.IsProgrammedFromFile(), "Digi-%d: LoadAndProgramSettings reported %d problem(s)", serial, nProblem);
  for( auto & p : problems ) printf("         %s\n", p.c_str());
  digi.SetTrace(false);

  printf("== Digi-%d (%s, %d ch)\n", serial, eightCh ? "DT5730" : "V1730", digi.GetNumInputCh());

  auto Compare = [&](Reg reg, int ch, const char * kind){
    uint32_t want = FileWord(file, reg, ch);
    uint32_t addr = reg.ActualAddress(ch);
    if( reg == DPP::BoardConfiguration ) want &= ~(1u << 16); // FSUDAQ always opens with traces off
    uint32_t onBoard = fake_caen_reg(serial, addr);
    uint32_t inMemory = digi.GetSettingFromMemory(reg, ch);
    if( fake_caen_is_sentinel(addr, onBoard) ){
      CHECK(false, "%s 0x%04X %-28s not written (file 0x%08X)", kind, addr, reg.GetNameChar(), want);
    }else{
      CHECK(onBoard == want, "%s 0x%04X %-28s board 0x%08X, file 0x%08X", kind, addr, reg.GetNameChar(), onBoard, want);
    }
    CHECK(inMemory == onBoard, "%s 0x%04X %-28s memory 0x%08X, board 0x%08X", kind, addr, reg.GetNameChar(), inMemory, onBoard);
  };

  for( const Reg & r : RegisterBoardList_PHAPSD ){
    if( r.GetRWType() != RW::ReadWrite || NotFromFile.count(r.GetAddress()) ) continue;
    if( digi.GetModelType() == ModelTypeCode::DT && r.GetAddress() == 0x81C4 ) continue; // not on desktop boards
    Compare(r, 0, "board  ");
  }
  for( int ch = 0; ch < digi.GetNumInputCh(); ch++ ){
    for( const Reg & r : RegisterChannelList_PSD ){
      if( r.GetRWType() != RW::ReadWrite ) continue;
      if( r.IsCoupled() && ch % 2 ) continue;
      char kind[16]; snprintf(kind, sizeof kind, "ch %2d  ", ch);
      Compare(r, ch, kind);
    }
  }

  fake_caen_stats st;
  fake_caen_get_stats(serial, &st);
  CHECK(st.resets == 0, "Digi-%d was reset %d time(s) while loading its settings", serial, st.resets);
  CHECK(st.libAggregation == 0, "Digi-%d: the library chose the aggregation itself %d time(s)", serial, st.libAggregation);
}

// A file that does not fit the board is refused, leaves memory alone and is not adopted as the
// board's settings file (which "Live Setting Update" would then write to).
static void CheckRefused(const std::string & dir) {
  printf("== files that must be refused\n");
  std::string tmp = "/tmp/fsudaq_test_settings_" + std::to_string(getpid());
  std::vector<uint32_t> file(SETTINGSIZE);
  FILE * f = fopen((dir + "/Digi-15880_PSD.bin").c_str(), "rb");
  if( !f || fread(file.data(), 4, SETTINGSIZE, f) != SETTINGSIZE ){ printf("cannot read Digi-15880_PSD.bin\n"); nFail++; if( f ) fclose(f); return; }
  fclose(f);

  fake_caen_add_board(1, 7, 0, 30508, "DT5730", (dir + "/Digi-30508_PSD.bin").c_str());
  Digitizer digi(0, 7);

  auto Refused = [&](const char * what, std::vector<uint32_t> content, size_t nWords){
    FILE * out = fopen(tmp.c_str(), "wb");
    fwrite(content.data(), 4, nWords, out);
    fclose(out);
    uint32_t before = digi.GetSettingFromMemory(DPP::RegChannelEnableMask);
    std::vector<std::string> problems;
    int n = digi.LoadAndProgramSettings(tmp, problems);
    CHECK(n == -1 && !problems.empty(), "%s: not refused", what);
    CHECK(!digi.IsProgrammedFromFile(), "%s: board counted as programmed from the file", what);
    CHECK(digi.GetSettingFileName() != tmp, "%s: adopted as the board's settings file", what);
    CHECK(digi.GetSettingFromMemory(DPP::RegChannelEnableMask) == before, "%s: memory changed", what);
    if( !problems.empty() ) printf("   ok    %s: %s\n", what, problems[0].c_str());
  };

  Refused("16-channel file on an 8-channel board", file, SETTINGSIZE);
  Refused("truncated file", file, SETTINGSIZE / 2);
  std::vector<uint32_t> pha = file;
  Reg amc = DPP::AMCFirmwareRevision_R;
  pha[amc.Index(0)] = (pha[amc.Index(0)] & ~0xFF00u) | (0x8Bu << 8);
  Refused("DPP-PHA file on a DPP-PSD board", pha, SETTINGSIZE);
  CHECK(!digi.IsSettingFileExist(), "a refused file left a settings file attached to the board");

  // missing file
  std::vector<std::string> problems;
  CHECK(digi.LoadAndProgramSettings(tmp + ".none", problems) == -1, "missing file: not refused");
  remove(tmp.c_str());
}

// A DT5730 changing its PLL reference must not be touched until it is ready (PSI, 27 Oct 2026):
// the settings panel, a settings file and a reset all change it.
static void CheckClockChange(const std::string & dir) {
  printf("== changing the clock source of a DT5730\n");
  const int serial = 30599, port = 6;   // 30508's identity, its own serial (ports 0-5 and 7 are taken)
  fake_caen_set_clock_settle_ms(50);
  fake_caen_add_board(1, port, 0, serial, "DT5730", (dir + "/Digi-30508_PSD.bin").c_str());
  Digitizer digi(0, port);
  CHECK(digi.IsConnected(), "Digi-%d did not open", serial);
  if( !digi.IsConnected() ) return;

  auto ExtClock = [&](){ return (fake_caen_reg(serial, 0x8100) >> 6) & 1; };
  auto Answers = [&](const char * what){
    digi.ReadRegister(DPP::ChannelStatus_R, 0, false);
    bool ok = digi.GetErrorCode() == 0 && !fake_caen_is_hung(serial);
    CHECK(ok, "%s: the board stopped answering", what);
    if( ok ) printf("   ok    %s\n", what);
    return ok;
  };

  //---- the settings panel's PLL Ref. Clock
  digi.SetBits(DPP::AcquisitionControl, DPP::Bit_AcquistionControl::PLLRef, 1, 0);
  CHECK(ExtClock() == 1, "panel: CLK-IN not set");
  if( !Answers("panel: internal -> CLK-IN, then a channel register") ) return;
  digi.SetBits(DPP::AcquisitionControl, DPP::Bit_AcquistionControl::PLLRef, 0, 0);
  CHECK(ExtClock() == 0, "panel: internal clock not set");
  if( !Answers("panel: CLK-IN -> internal, then a channel register") ) return;

  //---- a settings file on CLK-IN loaded on a board on its internal clock
  std::vector<uint32_t> file(SETTINGSIZE);
  FILE * f = fopen((dir + "/Digi-30508_PSD.bin").c_str(), "rb");
  if( !f || fread(file.data(), 4, SETTINGSIZE, f) != SETTINGSIZE ){ printf("cannot read Digi-30508_PSD.bin\n"); nFail++; if( f ) fclose(f); return; }
  fclose(f);
  Reg acqCtrl = DPP::AcquisitionControl;
  file[acqCtrl.Index(0)] |= 0x40;
  std::string tmp = "/tmp/fsudaq_test_clock_" + std::to_string(getpid()) + ".bin";
  FILE * out = fopen(tmp.c_str(), "wb");
  fwrite(file.data(), 4, SETTINGSIZE, out);
  fclose(out);
  std::vector<std::string> problems;
  int n = digi.LoadAndProgramSettings(tmp, problems);
  for( auto & p : problems ) printf("         %s\n", p.c_str());
  CHECK(n == 0 && ExtClock() == 1, "file on CLK-IN: %d problem(s)", n);
  if( !Answers("file on CLK-IN loaded on a board on its internal clock") ){ remove(tmp.c_str()); return; }
  remove(tmp.c_str());

  //---- a reset of a board on CLK-IN (Program Default) puts it back on its internal clock
  digi.Reset();
  CHECK(ExtClock() == 0, "reset: still on CLK-IN");
  if( !Answers("reset of a board on CLK-IN, then a channel register") ) return;

  //---- the control: without the wait the fake board hangs, as the real one did
  int keep = Digitizer::clockSettleTimeoutMs;
  Digitizer::clockSettleTimeoutMs = 0;
  digi.SetBits(DPP::AcquisitionControl, DPP::Bit_AcquistionControl::PLLRef, 1, 0);
  digi.ReadRegister(DPP::ChannelStatus_R, 0, false);
  CHECK(fake_caen_is_hung(serial), "control: a board touched while settling did not hang (the fake does not model it)");
  if( fake_caen_is_hung(serial) ) printf("   ok    control: touched while settling, the board hangs\n");
  Digitizer::clockSettleTimeoutMs = keep;
}

int main(int argc, char ** argv) {
  Digitizer::programPauseMs = 0; // the fake boards need no pause between register writes
  std::string dir = argc > 1 ? argv[1] : "tests/data/odin-runB";
  std::vector<std::pair<int, std::string>> files;
  if( DIR * d = opendir(dir.c_str()) ){
    while( dirent * e = readdir(d) ){
      int serial;
      if( sscanf(e->d_name, "Digi-%d_PSD.bin", &serial) == 1 ) files.push_back({serial, dir + "/" + e->d_name});
    }
    closedir(d);
  }
  if( files.empty() ){ printf("no Digi-*_PSD.bin in %s\n", dir.c_str()); return 2; }
  std::sort(files.begin(), files.end());

  int port = 0;
  for( auto & [serial, path] : files ) CheckBoard(path, serial, port++);
  CheckRefused(dir);
  CheckClockChange(dir);

  printf("\n%s: %d problem(s) in %zu board(s)\n", nFail ? "FAILED" : "PASSED", nFail, files.size());
  return nFail ? 1 : 0;
}
