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
#include <sys/stat.h>
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
      if( r.IsCoupled() && !r.IsPerChannel() && ch % 2 ) continue;
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

// The DC offset goes over the channel's SPI bus: FSUDAQ waits for the bus as CoMPASS does, with no
// fixed pause after each register, so back-to-back DC offsets all arrive.
static void CheckSPIWait(const std::string & dir) {
  printf("== DC offsets written back to back\n");
  const int serial = 30598, port = 6, node = 1;   // daisy-chained behind the clock test's board
  fake_caen_set_spi_busy_ms(5);
  fake_caen_add_board(1, port, node, serial, "DT5730", (dir + "/Digi-30508_PSD.bin").c_str());
  Digitizer digi(node, port);
  CHECK(digi.IsConnected(), "Digi-%d did not open", serial);
  if( !digi.IsConnected() ) return;

  digi.WriteRegister(DPP::ChannelDCOffset, 0x1111, 0);
  digi.WriteRegister(DPP::ChannelDCOffset, 0x2222, 0);
  digi.WriteRegister(DPP::ChannelDCOffset, 0x3333, -1);
  bool ok = fake_caen_reg(serial, 0x1098) == 0x3333 && fake_caen_reg(serial, 0x1798) == 0x3333 && fake_caen_dropped_dc_writes(serial) == 0;
  CHECK(ok, "with the SPI wait: ch0 0x%X, ch7 0x%X, %d DC offset(s) lost", fake_caen_reg(serial, 0x1098), fake_caen_reg(serial, 0x1798), fake_caen_dropped_dc_writes(serial));
  if( ok ) printf("   ok    one channel twice, then all channels: every DC offset arrived\n");

  //---- the control: without the wait the fake board loses the second write
  int keep = Digitizer::spiWaitTimeoutMs;
  Digitizer::spiWaitTimeoutMs = 0;
  usleep(10000);
  digi.WriteRegister(DPP::ChannelDCOffset, 0x4444, 0);
  digi.WriteRegister(DPP::ChannelDCOffset, 0x5555, 0);
  CHECK(fake_caen_dropped_dc_writes(serial) > 0, "control: a DC offset written to a busy channel was not lost (the fake does not model it)");
  if( fake_caen_dropped_dc_writes(serial) > 0 ) printf("   ok    control: without the wait the second DC offset is lost\n");
  Digitizer::spiWaitTimeoutMs = keep;
}

// DPP-PSD 0x1n84 is per channel, only bits [7:0] are common to the couple (firmware > 136.9). FSUDAQ
// used to write and read it on the even channel only, so after a power cycle the odd channels kept
// extras option 0 (baseline) while FSUDAQ showed option 2: Digi-1399 and 30505 at PSI, 28 Oct 2026.
static void CheckPerChannelCouple(const std::string & dir) {
  printf("== DPP Algorithm Control 2 (0x1n84) on odd channels\n");
  const int serial = 30597, port = 7, node = 1;   // daisy-chained behind the refused-files board, like 1399
  fake_caen_add_board(1, port, node, serial, "DT5730", (dir + "/Digi-30508_PSD.bin").c_str());
  for( int ch = 1; ch < 8; ch += 2 ) fake_caen_set_reg(serial, 0x1084 + (ch << 8), 0);   // as after a power cycle
  Digitizer digi(node, port);
  CHECK(digi.IsConnected(), "Digi-%d did not open", serial);
  if( !digi.IsConnected() ) return;
  digi.SetTrace(false);

  std::vector<std::string> problems;
  int n = digi.LoadAndProgramSettings(dir + "/Digi-30508_PSD.bin", problems);
  CHECK(n == 0, "loading the settings reported %d problem(s)", n);
  bool ok = true;
  for( int ch = 0; ch < 8; ch++ ){
    uint32_t onBoard = fake_caen_reg(serial, 0x1084 + (ch << 8));
    if( onBoard != 0x200 ){ ok = false; CHECK(false, "ch%d 0x1%d84 = 0x%08X on the board, not 0x200 (extras option 2)", ch, ch, onBoard); }
  }
  if( ok ) printf("   ok    odd channels left at extras option 0 are programmed to option 2 like the even ones\n");

  //---- one channel's own bits stay its own; the couple bits follow in both, on the board and in memory
  digi.SetBits(DPP::PSD::DPPAlgorithmControl2_G, DPP::PSD::Bit_DPPAlgorithmControl2::ExtraWordOption, 0, 2);
  digi.SetBits(DPP::PSD::DPPAlgorithmControl2_G, {3, 0}, 5, 3);
  uint32_t b2 = fake_caen_reg(serial, 0x1284), b3 = fake_caen_reg(serial, 0x1384);
  uint32_t m2 = digi.GetSettingFromMemory(DPP::PSD::DPPAlgorithmControl2_G, 2), m3 = digi.GetSettingFromMemory(DPP::PSD::DPPAlgorithmControl2_G, 3);
  ok = b2 == 0x005 && b3 == 0x205 && m2 == b2 && m3 == b3;
  CHECK(ok, "ch2 extras 0 then ch3 couple bits 5: board ch2 0x%X ch3 0x%X, memory ch2 0x%X ch3 0x%X (want 0x5, 0x205)", b2, b3, m2, m3);
  if( ok ) printf("   ok    ch2's extras option is ch2's alone; ch3's couple trigger bits reach ch2 too\n");

  //---- a write to an odd channel that does not arrive is reported
  fake_caen_drop_write(serial, 0x1384);
  fake_caen_set_reg(serial, 0x1384, 0);
  problems.clear();
  n = digi.LoadAndProgramSettings(dir + "/Digi-30508_PSD.bin", problems);
  std::string named;
  for( auto & p : problems ) if( p.find("0x1384") != std::string::npos ) named = p;
  CHECK(n > 0 && !named.empty(), "a lost write to ch3 0x1384 was not reported (%d problem(s))", n);
  if( !named.empty() ) printf("   ok    a lost write to ch3 is reported: %s\n", named.c_str());
  fake_caen_drop_write(serial, 0);
}

// The ADCs of the x725/x730 boards that are not "S" versions are calibrated at each start, as CoMPASS does
// (UM3148: "the DT725S/DT730S digitizer versions do not need calibration").
static void CheckADCCalibration(const std::string & dir) {
  printf("== ADC calibration\n");
  const int serial = 30596, port = 5, node = 1;
  struct { const char * model; bool needs; } models[] = {{"DT5730B", true}, {"DT5725B", true}, {"DT5730S", false}, {"VX1730SB", false}};
  bool namesOk = true;
  for( auto & m : models ){
    fake_caen_reset_all();
    fake_caen_add_board(1, port, node, serial, m.model, (dir + "/Digi-30508_PSD.bin").c_str());
    Digitizer digi(node, port);
    if( digi.NeedsADCCalibration() != m.needs ){ namesOk = false; CHECK(false, "%s: NeedsADCCalibration() = %d", m.model, !m.needs); }
  }
  if( namesOk ) printf("   ok    DT5730B, DT5725B calibrated; DT5730S, VX1730SB not\n");

  //---- right after a DC offset write: the calibration waits for the SPI bus, then for every channel
  fake_caen_reset_all();
  fake_caen_add_board(1, port, node, serial, "DT5730B", (dir + "/Digi-30508_PSD.bin").c_str());
  fake_caen_set_spi_busy_ms(5);
  fake_caen_set_calibrate_ms(30);
  Digitizer digi(node, port);
  CHECK(digi.IsConnected(), "Digi-%d did not open", serial);
  if( !digi.IsConnected() ) return;
  digi.WriteRegister(DPP::ChannelDCOffset, 0x8000, -1);
  std::string why;
  int ms = digi.CalibrateADC(why);
  fake_caen_stats st; fake_caen_get_stats(serial, &st);
  bool ok = ms >= 30 && st.calibrations == 1 && fake_caen_lost_calibrations(serial) == 0;
  CHECK(ok, "calibration after a DC offset write: %d ms (%s), %d calibration(s), %d lost", ms, why.c_str(), st.calibrations, fake_caen_lost_calibrations(serial));
  if( ok ) printf("   ok    after a DC offset write: waited for the SPI bus, done in %d ms on every channel\n", ms);

  //---- one that never finishes is reported, within the time-out
  int keep = Digitizer::adcCalibrationTimeoutMs;
  Digitizer::adcCalibrationTimeoutMs = 100;
  fake_caen_fail(serial, "calibrate");
  ms = digi.CalibrateADC(why);
  CHECK(ms < 0 && why.find("calibration not done") != std::string::npos, "a calibration that never finishes: %d ms, \"%s\"", ms, why.c_str());
  if( ms < 0 ) printf("   ok    one that never finishes is reported: %s\n", why.c_str());
  fake_caen_fail(serial, "");
  Digitizer::adcCalibrationTimeoutMs = keep;
  fake_caen_set_calibrate_ms(20);
  fake_caen_set_spi_busy_ms(2);
}

static std::vector<uint32_t> ReadImage(const std::string & path){
  std::vector<uint32_t> w(SETTINGSIZE, 0);
  FILE * f = fopen(path.c_str(), "rb");
  if( !f || fread(w.data(), 4, SETTINGSIZE, f) != SETTINGSIZE ){ printf("cannot read %s\n", path.c_str()); nFail++; }
  if( f ) fclose(f);
  return w;
}
static bool CopyFile(const std::string & from, const std::string & to){
  std::vector<uint32_t> w = ReadImage(from);
  FILE * f = fopen(to.c_str(), "wb");
  if( !f ) return false;
  bool ok = fwrite(w.data(), 4, SETTINGSIZE, f) == SETTINGSIZE;
  return (fclose(f) == 0) && ok;
}

// every read-only (status, identity) register of the DPP type's lists
static std::vector<std::pair<Reg, int>> ReadOnlyRegisters(int dpp, int nCh){
  std::vector<std::pair<Reg, int>> list;
  const std::vector<Reg> & board = dpp == V1740_DPP_QDC_CODE ? RegisterBoardList_QDC : RegisterBoardList_PHAPSD;
  const std::vector<Reg> & chans = dpp == V1740_DPP_QDC_CODE ? RegisterChannelList_QDC : dpp == V1730_DPP_PSD_CODE ? RegisterChannelList_PSD : RegisterChannelList_PHA;
  for( const Reg & r : board ) if( r.GetRWType() == RW::ReadONLY ) list.push_back({r, 0});
  for( int ch = 0; ch < nCh; ch++ ) for( const Reg & r : chans ) if( r.GetRWType() == RW::ReadONLY ) list.push_back({r, ch});
  return list;
}

// a board's identity set by hand, to run the settings comparison of every DPP type without a board
class OfflineDigitizer : public Digitizer {
public:
  OfflineDigitizer(int dpp, int nCh, int nRegCh){ DPPType = dpp; NumInputCh = nCh; NumRegChannel = nRegCh; ModelType = ModelTypeCode::VME; }
  std::vector<std::pair<Reg, int>> Programmed(){ return ProgrammedRegisters(); }
};

// The check at Start: the board's settings against its settings file, the read-only registers left out (they are
// the board's to report: temperatures, status, firmware), for every DPP type
static void CheckSettingsDifferences(const std::string & dir) {
  printf("== board settings against the settings file, read-only registers left out\n");
  struct { int dpp; int nCh, nRegCh; const char * name; } types[] = {{V1730_DPP_PHA_CODE, 16, 16, "DPP-PHA"}, {V1730_DPP_PSD_CODE, 16, 16, "DPP-PSD"}, {V1740_DPP_QDC_CODE, 64, 8, "DPP-QDC"}};
  for( auto & t : types ){
    OfflineDigitizer d(t.dpp, t.nCh, t.nRegCh);
    std::vector<unsigned int> ref(SETTINGSIZE, 0);
    std::set<unsigned short> programmedIdx;
    auto programmed = d.Programmed();
    for( auto & [r, ch] : programmed ){ Reg x = r; programmedIdx.insert(x.Index(ch < 0 ? 0 : ch)); }
    int nRO = 0, roShown = 0;
    for( auto & [r, ch] : ReadOnlyRegisters(t.dpp, t.nRegCh) ){
      Reg x = r;
      if( programmedIdx.count(x.Index(ch)) ) continue;   // the same word as a setting: compared as that setting
      d.SetSettingToMemory(r, 0x5A5A5A5A, ch);
      auto diff = d.SettingsDifferences(ref);
      if( !diff.empty() ){ roShown++; CHECK(false, "%s: read-only %s ch%d listed: %s", t.name, r.GetNameChar(), ch, diff[0].c_str()); }
      d.SetSettingToMemory(r, 0, ch);
      nRO++;
    }
    int nRW = 0, rwMissed = 0;
    for( auto & [r, ch] : programmed ){
      if( r.GetRWType() != RW::ReadWrite || r == DPP::LVDSIOData ) continue;
      if( t.dpp == V1740_DPP_QDC_CODE && r == DPP::QDC::OverThresholdWidth ) continue;   // this offline board's firmware lacks it
      d.SetSettingToMemory(r, 0x00A50000, ch < 0 ? 0 : ch);
      auto diff = d.SettingsDifferences(ref);
      bool named = diff.size() == 1 && diff[0].find(r.GetName()) != std::string::npos;
      if( !named ){ rwMissed++; CHECK(false, "%s: read/write %s ch%d: %zu line(s)%s%s", t.name, r.GetNameChar(), ch, diff.size(), diff.empty() ? "" : ": ", diff.empty() ? "" : diff[0].c_str()); }
      d.SetSettingToMemory(r, 0, ch < 0 ? 0 : ch);
      nRW++;
    }
    if( roShown == 0 && rwMissed == 0 ) printf("   ok    %s: %d read-only registers never listed, each of %d read/write settings listed by name\n", t.name, nRO, nRW);
  }

  //---- a real (fake) board: a temperature and a status changing are no edit; a threshold is, named with both values
  const int serial = 30595, port = 4, node = 1;
  fake_caen_add_board(1, port, node, serial, "DT5730", (dir + "/Digi-30508_PSD.bin").c_str());
  Digitizer digi(node, port);
  CHECK(digi.IsConnected(), "Digi-%d did not open", serial);
  if( !digi.IsConnected() ) return;
  std::vector<std::string> problems;
  CHECK(digi.LoadAndProgramSettings(dir + "/Digi-30508_PSD.bin", problems) == 0, "loading reported problems");
  std::vector<unsigned int> image;
  CHECK(Digitizer::ReadSettingsImage(dir + "/Digi-30508_PSD.bin", image) == 0, "ReadSettingsImage failed");
  fake_caen_set_reg(serial, 0x10A8, 0x3C);   // ch0 ADC temperature
  fake_caen_set_reg(serial, 0x812C, 1234);   // events stored
  digi.ReadAllSettingsFromBoard(true);
  auto diff = digi.SettingsDifferences(image);
  CHECK(diff.empty(), "temperature/status changes listed: %s", diff.empty() ? "" : diff[0].c_str());
  Reg thr = DPP::PSD::TriggerThreshold;
  uint32_t fileThr = image[thr.Index(3)];
  digi.WriteRegister(DPP::PSD::TriggerThreshold, fileThr + 7, 3);
  digi.ReadAllSettingsFromBoard(true);
  diff = digi.SettingsDifferences(image);
  char want[120]; snprintf(want, sizeof want, "TriggerThreshold ch3: file 0x%08X, board 0x%08X", fileThr, fileThr + 7);
  CHECK(diff.size() == 1 && diff[0].find(want) != std::string::npos, "threshold edit: %zu line(s): %s", diff.size(), diff.empty() ? "" : diff[0].c_str());
  if( diff.size() == 1 ) printf("   ok    a board: temperature and status ignored, the edit named: %s\n", diff[0].c_str());
  std::string why;
  std::vector<unsigned int> none;
  CHECK(Digitizer::ReadSettingsImage(dir + "/none.bin", none, &why) == -1 && !why.empty(), "a missing file read as an image");
}

// Live Setting Update writes settings, never status registers; a load (from a run snapshot) never writes the file
// it loads; a snapshot is recognised and a board's settings file can be pointed elsewhere without writing it
static void CheckSettingsFileWrites(const std::string & dir) {
  printf("== what reaches the settings file\n");
  std::string tmp = "/tmp/fsudaq_test_files_" + std::to_string(getpid());
  mkdir(tmp.c_str(), 0755);
  mkdir((tmp + "/run_007").c_str(), 0755);
  const std::string file = tmp + "/Digi-30594_PSD.bin";
  const std::string snapshot = tmp + "/run_007/run_007_30594_PSD.bin";
  CopyFile(dir + "/Digi-30508_PSD.bin", file);
  CopyFile(dir + "/Digi-30508_PSD.bin", snapshot);

  const int serial = 30594, port = 4, node = 2;
  fake_caen_add_board(1, port, node, serial, "DT5730", (dir + "/Digi-30508_PSD.bin").c_str());
  Digitizer digi(node, port);
  CHECK(digi.IsConnected(), "Digi-%d did not open", serial);
  if( !digi.IsConnected() ) return;

  //---- a load with Live Setting Update on: the file loaded is not written (the board's temperatures differ from it)
  fake_caen_set_reg(serial, 0x10A8, 0x41);
  digi.SetSettingFileUpdate(true);
  std::vector<std::string> problems;
  std::vector<uint32_t> before = ReadImage(snapshot);
  CHECK(digi.LoadAndProgramSettings(snapshot, problems) == 0, "loading the snapshot reported problems");
  CHECK(ReadImage(snapshot) == before, "the snapshot was written while it was loaded (Live Setting Update on)");
  CHECK(digi.GetLoadedFromFile() == snapshot, "loaded-from not recorded");

  //---- pointed at the settings folder: Live Setting Update goes off, nothing is written
  std::vector<uint32_t> fileBefore = ReadImage(file);
  digi.SetSettingFileTarget(file);
  CHECK(digi.GetSettingFileName() == file && digi.IsSettingFileExist(), "target not set");
  CHECK(!digi.IsSettingFileUpdate() && !digi.SettingFileProblem().empty(), "Live Setting Update still on after the target moved");
  CHECK(ReadImage(file) == fileBefore && ReadImage(snapshot) == before, "a file was written when the target moved");
  CHECK(digi.GetLoadedFromFile() == snapshot, "loaded-from changed by the target");
  if( nFail == 0 ) printf("   ok    loaded from a snapshot with Live Setting Update on: snapshot untouched; target moved, update off\n");

  //---- Live Setting Update on: a setting reaches the file, a status read does not
  digi.SetSettingFileUpdate(true);
  Reg thr = DPP::PSD::TriggerThreshold, temp = DPP::ChannelADCTemperature_R, stored = DPP::EventStored_R;
  digi.WriteRegister(DPP::PSD::TriggerThreshold, 0x123, 1);
  fake_caen_set_reg(serial, 0x11A8, 0x42);   // ch1 temperature
  fake_caen_set_reg(serial, 0x812C, 99);
  uint32_t t = digi.ReadRegister(DPP::ChannelADCTemperature_R, 1);
  digi.ReadAllSettingsFromBoard(true);
  std::vector<uint32_t> now = ReadImage(file);
  CHECK(t == 0x42 && digi.GetSettingFromMemory(DPP::ChannelADCTemperature_R, 1) == 0x42, "the temperature read did not reach memory");
  CHECK(now[thr.Index(1)] == 0x123, "Live Setting Update did not write the threshold (0x%X)", now[thr.Index(1)]);
  bool roKept = true;
  for( auto & [r, ch] : ReadOnlyRegisters(V1730_DPP_PSD_CODE, digi.GetNumInputCh()) ){
    Reg x = r;
    if( now[x.Index(ch)] != fileBefore[x.Index(ch)] ){ roKept = false; CHECK(false, "read-only %s ch%d written to the file: 0x%X -> 0x%X", r.GetNameChar(), ch, fileBefore[x.Index(ch)], now[x.Index(ch)]); }
  }
  CHECK(now[temp.Index(1)] == fileBefore[temp.Index(1)] && now[stored.Index(0)] == fileBefore[stored.Index(0)], "temperature or events stored in the file");
  CHECK(ReadImage(snapshot) == before, "the snapshot was written");
  if( roKept ) printf("   ok    Live Setting Update: the threshold written, no read-only register (temperatures, status, firmware)\n");

  //---- run snapshots recognised by name and folder
  struct { const char * path; bool is; } names[] = {
    {"/d/run_007/run_007_30594_PSD.bin", true}, {"/d/test_054/test_054_1443_PHA.bin", true}, {"/d/a_b_12/a_b_12_15880_PSD.bin", true},
    {"/d/settings/Digi-30594_PSD.bin", false}, {"/d/run_007/Digi-30594_PSD.bin", false}, {"/d/run_007/run_008_30594_PSD.bin", false},
    {"/d/run/run_30594_PSD.bin", false}, {"/d/run_007/run_007_30594_PSD.fsu", false}, {"/d/run_007/run_007_x_PSD.bin", false}, {"run_007_1_PSD.bin", false}};
  bool namesOk = true;
  for( auto & n : names ) if( Digitizer::IsRunSnapshotPath(n.path) != n.is ){ namesOk = false; CHECK(false, "IsRunSnapshotPath(%s) = %d", n.path, !n.is); }
  if( namesOk ) printf("   ok    run snapshots recognised: <run>/<run>_<serial>_<DPP>.bin only\n");

  //---- a settings file behind a symlink, with its own mode: replaced through the link, which stays; the mode kept
  const std::string real = tmp + "/real.bin", link = tmp + "/link.bin";
  CopyFile(file, real);
  chmod(real.c_str(), 0640);
  CHECK(symlink(real.c_str(), link.c_str()) == 0, "cannot make the symlink");
  bool saved = digi.SaveAllSettingsAsBin(link);
  struct stat ls, rs;
  lstat(link.c_str(), &ls); stat(real.c_str(), &rs);
  CHECK(saved && S_ISLNK(ls.st_mode) && (rs.st_mode & 07777) == 0640 && ReadImage(real) == std::vector<uint32_t>(digi.GetSettings(), digi.GetSettings() + SETTINGSIZE),
        "save through a symlink: link kept %d, mode %o", S_ISLNK(ls.st_mode), rs.st_mode & 07777);
  if( saved && S_ISLNK(ls.st_mode) && (rs.st_mode & 07777) == 0640 ) printf("   ok    a settings file behind a symlink: written through it, the link and the mode kept\n");
  remove(link.c_str()); remove(real.c_str());

  remove(file.c_str()); remove(snapshot.c_str()); rmdir((tmp + "/run_007").c_str()); rmdir(tmp.c_str());
}

// At Start the comparison leaves out what is not the file's to set: the trace bit (every open switches it off)
// and what FSUDAQ derives at each start (DPP-PHA trapezoid rescaling and fine gain, DPP-QDC aggregate
// organization); write-only registers are compared as last written, a register the firmware lacks not at all
static void CheckDerivedAtStart(const std::string & dir) {
  printf("== at Start: traces and derived registers are not differences, write-only ones are compared as written\n");
  int nFail0 = nFail;
  std::vector<unsigned int> ref(SETTINGSIZE, 0);
  auto Lines = [&](OfflineDigitizer & d, bool atStart){ return d.SettingsDifferences(ref, atStart).size(); };
  {
    OfflineDigitizer d(V1730_DPP_PHA_CODE, 16, 16);
    d.SetSettingToMemory(DPP::PHA::FineGain, 0x1234, 0);
    CHECK(Lines(d, true) == 0 && Lines(d, false) == 1, "PHA fine gain: %zu at Start, %zu at a load", Lines(d, true), Lines(d, false));
    d.SetSettingToMemory(DPP::PHA::FineGain, 0, 0);
    d.SetSettingToMemory(DPP::DPPAlgorithmControl, 0x3F, 3);
    CHECK(Lines(d, true) == 0 && Lines(d, false) == 1, "PHA trapezoid rescaling bits listed at Start");
    d.SetSettingToMemory(DPP::DPPAlgorithmControl, 0x13F, 3);
    CHECK(Lines(d, true) == 1, "PHA: another bit of DPP Algorithm Control not listed");
    d.SetSettingToMemory(DPP::DPPAlgorithmControl, 0, 3);
    d.SetSettingToMemory(DPP::BoardConfiguration, 1u << 16, 0);   // traces on, on the board
    CHECK(Lines(d, true) == 0 && Lines(d, false) == 1, "traces on listed at Start");
  }
  {
    OfflineDigitizer d(V1740_DPP_QDC_CODE, 64, 8);
    d.SetSettingToMemory(DPP::AggregateOrganization, 5, 0);
    CHECK(Lines(d, true) == 0 && Lines(d, false) == 1, "QDC aggregate organization listed at Start");
    d.SetSettingToMemory(DPP::AggregateOrganization, 0, 0);
    d.SetSettingToMemory(DPP::QDC::RecordLength_W, 0x40, 0);
    auto diff = d.SettingsDifferences(ref, true);
    CHECK(diff.size() == 1 && diff[0].find("write-only") != std::string::npos, "QDC record length (write-only) not compared as written");
    d.SetSettingToMemory(DPP::QDC::RecordLength_W, 0, 0);
    d.SetSettingToMemory(DPP::QDC::OverThresholdWidth, 7, 2);
    CHECK(Lines(d, true) == 0, "QDC over-threshold width compared on firmware without it");
  }

  //---- a DPP-PHA board started twice: the derived registers change on the board, never a difference; the run's
  //     snapshot (memory after the derivation) is what the board runs with
  std::vector<uint32_t> image = ReadImage(dir + "/Digi-30508_PSD.bin");
  Reg amc = DPP::AMCFirmwareRevision_R, rise = DPP::PHA::TrapezoidRiseTime, decay = DPP::PHA::DecayTime, fg = DPP::PHA::FineGain;
  for( int ch = 0; ch < 8; ch++ ){
    image[amc.Index(ch)] = (image[amc.Index(ch)] & ~0xFF00u) | (0x8Bu << 8);
    image[rise.Index(ch)] = 100;
    image[decay.Index(ch)] = 5000;
    image[fg.Index(ch)] = 0;
  }
  const std::string pha = "/tmp/fsudaq_test_pha_" + std::to_string(getpid()) + ".bin";
  { FILE * f = fopen(pha.c_str(), "wb"); fwrite(image.data(), 4, SETTINGSIZE, f); fclose(f); }
  const int serial = 30593, port = 4, node = 3;
  fake_caen_add_board(1, port, node, serial, "DT5730", pha.c_str());
  for( int ch = 0; ch < 8; ch++ ) fake_caen_set_reg(serial, 0x1040 + (ch << 8), 0);   // the fake takes 0x1n40 as the PSD flush and keeps no value: as the file
  Digitizer digi(node, port);
  CHECK(digi.IsConnected() && digi.GetDPPType() == V1730_DPP_PHA_CODE, "the DPP-PHA board did not open as PHA");
  if( !digi.IsConnected() ){ remove(pha.c_str()); return; }
  std::vector<std::string> problems;
  int n = digi.LoadAndProgramSettings(pha, problems);
  CHECK(n == 0, "loading the PHA file: %d problem(s)%s%s", n, problems.empty() ? "" : ": ", problems.empty() ? "" : problems[0].c_str());
  const uint32_t want = (uint32_t) (0xFFFF * pow(2, 18) / 100 / 5000);   // SHF = floor(log2(100 * 5000)) = 18
  for( int start = 1; start <= 2; start++ ){
    CHECK(digi.ApplyDerivedSettings() == 0, "start %d: derived writes failed", start);
    digi.ReadAllSettingsFromBoard(true);
    auto diff = digi.SettingsDifferences(image, true);
    CHECK(diff.empty(), "start %d: %zu difference(s): %s", start, diff.size(), diff.empty() ? "" : diff[0].c_str());
    CHECK(digi.GetSettingFromMemory(DPP::PHA::FineGain, 0) == want && fake_caen_reg(serial, fg.ActualAddress(0)) == want
          && (digi.GetSettingFromMemory(DPP::DPPAlgorithmControl, 0) & 0x3F) == 18, "start %d: fine gain 0x%X (want 0x%X), rescaling %u", start,
          digi.GetSettingFromMemory(DPP::PHA::FineGain, 0), want, digi.GetSettingFromMemory(DPP::DPPAlgorithmControl, 0) & 0x3F);
  }
  digi.WriteRegister(DPP::PHA::TrapezoidRiseTime, 200, 1);   // an edit: listed; the fine gain it changes is not
  digi.ApplyDerivedSettings();
  digi.ReadAllSettingsFromBoard(true);
  auto diff = digi.SettingsDifferences(image, true);
  CHECK(diff.size() == 1 && diff[0].find("TrapezoidRiseTime ch1") != std::string::npos, "PHA edit: %zu line(s)%s%s", diff.size(), diff.empty() ? "" : ": ", diff.empty() ? "" : diff[0].c_str());
  remove(pha.c_str());
  if( nFail == nFail0 ) printf("   ok    traces and derived fields ignored at Start (PHA, QDC), write-only compared as written; a PHA board started twice: no difference, snapshot = derived values\n");
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
  CheckSPIWait(dir);
  CheckPerChannelCouple(dir);
  CheckADCCalibration(dir);
  CheckSettingsDifferences(dir);
  CheckSettingsFileWrites(dir);
  CheckDerivedAtStart(dir);

  printf("\n%s: %d problem(s) in %zu board(s)\n", nFail ? "FAILED" : "PASSED", nFail, files.size());
  return nFail ? 1 : 0;
}
