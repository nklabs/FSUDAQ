// Fake libCAENDigitizer: see fake_caen.h.
#include "CAENDigitizer.h"
#include "CAENDigitizerType.h"
#include "fake_caen.h"

#include <chrono>
#include <cstdarg>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <climits>
#include <unistd.h>
#include <map>
#include <string>
#include <vector>

namespace {

struct Board {
  int link, num, node, serial;
  int usbAlias = -1;                     // also reachable on this USB link
  std::string model;
  int nCh;
  bool open = false;
  std::map<uint32_t, uint32_t> reg;      // registers written (or identity registers)
  std::vector<uint32_t> image;           // identity source: an FSUDAQ settings file
  fake_caen_stats st{};
  std::string fail;
  uint32_t allocated = 0;                // the readout buffer size last handed out
  // data source
  std::vector<uint32_t> words;
  std::vector<size_t> aggStart;          // word offset of each board aggregate
  double aggPerMs = 0;
  std::chrono::steady_clock::time_point t0;
  long startIndex = 0;
  int synthEvents = 0;                   // > 0: generate the data
  long held = 0;                         // after a stop: aggregates still being filled, read only after a forced data flush
  int readDelayUs = 0;                   // time a ReadData with data takes
  std::vector<uint32_t> handedOut;       // everything ReadData gave
};

std::vector<Board> boards;

uint32_t Sentinel(uint32_t address) { return 0xDEAD0000u | ((address >> 2) & 0xFFFF); }

unsigned short ImageIndex(uint32_t a) { // FSUDAQ's Reg::Index for an actual address
  if( a < 0x8000 ) return a / 4;
  if( a < 0xF000 ) return (a & 0x0FFF) / 4;
  return ((a & 0x0FFF) + 0x0200) / 4;
}

bool IsReadOnly(uint32_t a) {
  if( a >= 0xF000 ) return true;
  if( a >= 0x1000 && a < 0x2000 ){
    uint32_t off = a & 0xFF;
    return off == 0x88 || off == 0x8C || off == 0xA8 || off == 0xEC;
  }
  switch(a){
    case 0x8104: case 0x8124: case 0x812C: case 0x8140: case 0x814C:
    case 0x8158: case 0x8178: case 0xEF04: return true;
  }
  return false;
}

bool IsWriteOnlyAction(uint32_t a) {
  switch(a){
    case 0x809C: case 0x80BC: case 0x8108: case 0x813C: case 0x81C0:
    case 0xEF24: case 0xEF28: case 0xEF34: return true;
  }
  if( a >= 0x1000 && a < 0x2000 ){ uint32_t off = a & 0xFF; return off == 0xC0 || off == 0x40; } // sw trigger, forced flush
  return false;
}

// 0x8020-0x80FF write the same register of every channel (0x1n20-0x1nFF), except the few
// board registers in that range.
bool IsBroadcast(uint32_t a) { return a >= 0x8020 && a < 0x8100 && !IsWriteOnlyAction(a); }

Board * FromHandle(int handle) {
  if( handle < 0 || handle >= (int) boards.size() || !boards[handle].open ) return nullptr;
  return &boards[handle];
}
Board * FromSerial(int serial) {
  for( auto & b : boards ) if( b.serial == serial ) return &b;
  return nullptr;
}

long Acquired(Board & b) {
  long n = b.startIndex;
  if( b.st.running ){
    double ms = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - b.t0).count();
    n += (long)(ms * b.aggPerMs);
  }
  long total = b.synthEvents > 0 ? LONG_MAX : (long) b.aggStart.size();
  return n < total ? n : total;
}

void Clear(Board & b) {
  b.held = 0;
  long acq = b.st.running ? Acquired(b) : b.st.aggAcquired;
  if( acq > b.st.aggRead ){ b.st.aggCleared += acq - b.st.aggRead; b.st.aggRead = acq; }
}

void ResetRegisters(Board & b) {
  Clear(b);
  b.reg.clear();
  b.st.resets++;
}

uint32_t Read(Board & b, uint32_t a) {
  if( a == 0x8104 ) return (1u << 8) | (b.st.running ? (1u << 2) : 0) | (b.st.aggAcquired > b.st.aggRead ? (1u << 3) : 0);
  if( IsReadOnly(a) ){
    unsigned short i = ImageIndex(a);
    return i < b.image.size() ? b.image[i] : 0;
  }
  auto it = b.reg.find(a);
  return it == b.reg.end() ? Sentinel(a) : it->second;
}

void Write(Board & b, uint32_t a, uint32_t v) {
  b.st.writes++;
  if( IsReadOnly(a) ) return;
  if( a == 0xEF24 || a == 0xEF34 ){ ResetRegisters(b); return; }
  if( a == 0xEF28 ){ Clear(b); return; }
  if( a == 0x8040 || (a >= 0x1000 && a < 0x2000 && (a & 0xFF) == 0x40) ){ b.held = 0; b.st.flushes++; return; }   // DPP-PSD forced data flush
  if( IsWriteOnlyAction(a) ) return;
  if( IsBroadcast(a) ){
    for( int ch = 0; ch < b.nCh; ch++ ) b.reg[(a - 0x7000) + (ch << 8)] = v;
    return;
  }
  b.reg[a] = v;
}

// Board aggregate k of a generated stream: every couple has synthEvents events, 10 us apart.
void Generate(Board & b, long k, std::vector<uint32_t> & out) {
  int nCouple = b.nCh / 2, nEv = b.synthEvents;
  out.clear();
  out.push_back(0xA0000000u | (uint32_t)(4 + nCouple * (2 + 3 * nEv)));
  out.push_back((8u << 27) | ((1u << nCouple) - 1));            // board ID 8 (DPP-PSD), couple mask
  out.push_back((uint32_t) k & 0x7FFFFF);                        // board aggregate counter
  out.push_back((uint32_t)(k * 1000));                           // board time tag
  for( int c = 0; c < nCouple; c++ ){
    out.push_back(0x80000000u | (uint32_t)(2 + 3 * nEv));
    out.push_back(0x72000000u);                                  // time tag, extras (option 2), charges; no waveform
    for( int e = 0; e < nEv; e++ ){
      uint64_t ts = (uint64_t) k * nEv * 5000 + (uint64_t) e * 5000 + c; // 2 ns ticks
      out.push_back(((uint32_t)(e & 1) << 31) | (uint32_t)(ts & 0x7FFFFFFF));
      out.push_back((uint32_t)((ts >> 31) & 0xFFFF) << 16 | (uint32_t)(e & 0x3FF));
      out.push_back(((uint32_t)(1000 + c * 100 + e) << 16) | (uint32_t)(500 + e));
    }
  }
}

uint32_t BufferSize(Board & b) { // what FSUDAQ's CalByteForBuffer computes from the registers
  uint32_t numAggBLT = Read(b, 0xEF1C), chMask = Read(b, 0x8120), boardCfg = Read(b, 0x8000);
  uint64_t size = 0;
  for( int pCh = 0; pCh < b.nCh / 2; pCh++ ){
    if( (chMask & (3u << (2 * pCh))) == 0 ) continue;
    uint32_t evAgg = Read(b, 0x1034 + (pCh << 9)), recLen = Read(b, 0x1020 + (pCh << 9));
    size += 2 + (2 + ((boardCfg >> 17) & 1) + ((boardCfg >> 16) & 1) * recLen * 4) * (uint64_t) evAgg;
  }
  size = (size + 4) * numAggBLT * 4;
  return size > 0xFFFFFFFFull ? 0 : (uint32_t) size;
}

} // namespace

//======================================================================= test helpers
extern "C" int fake_caen_add_board(int link, int num, int node, int serial, const char * model, const char * image) {
  Board b;
  b.link = link; b.num = num; b.node = node; b.serial = serial; b.model = model;
  b.nCh = (b.model == "DT5730") ? 8 : 16;
  if( image ){
    FILE * f = fopen(image, "rb");
    if( !f ) return -1;
    b.image.resize(2048);
    size_t n = fread(b.image.data(), 4, 2048, f);
    fclose(f);
    if( n != 2048 ) return -1;
  }
  boards.push_back(b);
  return 0;
}

extern "C" void fake_caen_add_usb_alias(int serial, int usbLink) {
  Board * b = FromSerial(serial);
  if( b ) b->usbAlias = usbLink;
}

extern "C" int fake_caen_set_data(int serial, const char * fsuFile, double aggPerMs) {
  Board * b = FromSerial(serial);
  if( !b ) return -1;
  FILE * f = fopen(fsuFile, "rb");
  if( !f ) return -1;
  const size_t maxWords = 8u << 20; // the first 32 MB is plenty for a test
  b->words.resize(maxWords);
  size_t n = fread(b->words.data(), 4, maxWords, f);
  fclose(f);
  b->words.resize(n);
  b->aggStart.clear();
  for( size_t i = 0; i < n; ){
    uint32_t size = b->words[i] & 0x0FFFFFFF;
    if( (b->words[i] >> 28) != 0xA || size == 0 || i + size > n ) break;
    b->aggStart.push_back(i);
    i += size;
  }
  b->aggPerMs = aggPerMs;
  b->st.aggTotal = (long) b->aggStart.size();
  return 0;
}

extern "C" int fake_caen_set_synthetic(int serial, int eventsPerCouple, double aggPerMs) {
  Board * b = FromSerial(serial);
  if( !b || eventsPerCouple <= 0 ) return -1;
  b->synthEvents = eventsPerCouple;
  b->aggPerMs = aggPerMs;
  return 0;
}

extern "C" void fake_caen_set_read_delay(int serial, int microseconds) {
  Board * b = FromSerial(serial);
  if( b ) b->readDelayUs = microseconds;
}

extern "C" size_t fake_caen_handed_out(int serial, const uint32_t ** words) {
  Board * b = FromSerial(serial);
  if( !b ){ *words = nullptr; return 0; }
  *words = b->handedOut.data();
  return b->handedOut.size();
}

extern "C" void fake_caen_reset_all(void) { boards.clear(); }

extern "C" uint32_t fake_caen_reg(int serial, uint32_t address) {
  Board * b = FromSerial(serial);
  return b ? Read(*b, address) : 0;
}

extern "C" void fake_caen_set_reg(int serial, uint32_t address, uint32_t value) {
  Board * b = FromSerial(serial);
  if( b ) b->reg[address] = value;
}

extern "C" int fake_caen_get_stats(int serial, fake_caen_stats * stats) {
  Board * b = FromSerial(serial);
  if( !b ) return -1;
  *stats = b->st;
  if( b->st.running ) stats->aggAcquired = Acquired(*b);
  return 0;
}

extern "C" int fake_caen_is_sentinel(uint32_t address, uint32_t value) { return value == Sentinel(address); }

extern "C" void fake_caen_fail(int serial, const char * what) {
  Board * b = FromSerial(serial);
  if( b ) b->fail = what ? what : "";
}

//======================================================================= CAEN API
#define BOARD_OR_FAIL Board * b = FromHandle(handle); if( !b ) return CAEN_DGTZ_InvalidHandle;

CAEN_DGTZ_ErrorCode CAEN_DGTZ_OpenDigitizer(CAEN_DGTZ_ConnectionType LinkType, int LinkNum, int ConetNode, uint32_t VMEBaseAddress, int * handle) {
  (void) VMEBaseAddress;
  for( size_t i = 0; i < boards.size(); i++ ){
    Board & b = boards[i];
    bool match = (LinkType == CAEN_DGTZ_USB && ((b.link == 0 && b.num == LinkNum) || b.usbAlias == LinkNum))
              || (LinkType == CAEN_DGTZ_OpticalLink && b.link == 1 && b.num == LinkNum && b.node == ConetNode);
    if( !match ) continue;
    if( b.open ) return CAEN_DGTZ_DigitizerAlreadyOpen;
    b.open = true;
    *handle = (int) i;
    return CAEN_DGTZ_Success;
  }
  return CAEN_DGTZ_CommError;
}

CAEN_DGTZ_ErrorCode CAEN_DGTZ_CloseDigitizer(int handle) { BOARD_OR_FAIL; b->open = false; return CAEN_DGTZ_Success; }

CAEN_DGTZ_ErrorCode CAEN_DGTZ_GetInfo(int handle, CAEN_DGTZ_BoardInfo_t * info) {
  BOARD_OR_FAIL;
  memset(info, 0, sizeof(*info));
  snprintf(info->ModelName, sizeof(info->ModelName), "%s", b->model.c_str());
  info->Model = (b->model == "DT5730") ? CAEN_DGTZ_DT5730 : CAEN_DGTZ_V1730;
  info->Channels = b->nCh;
  info->FamilyCode = CAEN_DGTZ_XX730_FAMILY_CODE;
  uint32_t amc = Read(*b, 0x108C);
  snprintf(info->ROC_FirmwareRel, sizeof(info->ROC_FirmwareRel), "4.29 - build fake");
  snprintf(info->AMC_FirmwareRel, sizeof(info->AMC_FirmwareRel), "%u.%u - build fake", (amc >> 8) & 0xFF, amc & 0xFF);
  info->SerialNumber = b->serial;
  info->ADC_NBits = 14;
  return CAEN_DGTZ_Success;
}

CAEN_DGTZ_ErrorCode CAEN_DGTZ_WriteRegister(int handle, uint32_t Address, uint32_t Data) {
  BOARD_OR_FAIL;
  if( b->fail == "write" ) return CAEN_DGTZ_WriteDeviceRegisterFail;
  Write(*b, Address, Data);
  return CAEN_DGTZ_Success;
}

CAEN_DGTZ_ErrorCode CAEN_DGTZ_ReadRegister(int handle, uint32_t Address, uint32_t * Data) {
  BOARD_OR_FAIL;
  *Data = Read(*b, Address);
  return CAEN_DGTZ_Success;
}

CAEN_DGTZ_ErrorCode CAEN_DGTZ_Reset(int handle) { BOARD_OR_FAIL; ResetRegisters(*b); return CAEN_DGTZ_Success; }

CAEN_DGTZ_ErrorCode CAEN_DGTZ_SetChannelDCOffset(int handle, uint32_t channel, uint32_t Tvalue) {
  BOARD_OR_FAIL; Write(*b, 0x1098 + (channel << 8), Tvalue); return CAEN_DGTZ_Success;
}
CAEN_DGTZ_ErrorCode CAEN_DGTZ_SetChannelEnableMask(int handle, uint32_t mask) { BOARD_OR_FAIL; Write(*b, 0x8120, mask); return CAEN_DGTZ_Success; }
CAEN_DGTZ_ErrorCode CAEN_DGTZ_GetChannelEnableMask(int handle, uint32_t * mask) { BOARD_OR_FAIL; *mask = Read(*b, 0x8120); return CAEN_DGTZ_Success; }

CAEN_DGTZ_ErrorCode CAEN_DGTZ_SetIOLevel(int handle, CAEN_DGTZ_IOLevel_t level) {
  BOARD_OR_FAIL; uint32_t v = Read(*b, 0x811C); Write(*b, 0x811C, (v & ~1u) | (level == CAEN_DGTZ_IOLevel_TTL)); return CAEN_DGTZ_Success;
}
CAEN_DGTZ_ErrorCode CAEN_DGTZ_SetExtTriggerInputMode(int handle, CAEN_DGTZ_TriggerMode_t mode) {
  BOARD_OR_FAIL; uint32_t v = Read(*b, 0x810C) & ~(1u << 30);
  if( mode == CAEN_DGTZ_TRGMODE_ACQ_ONLY || mode == CAEN_DGTZ_TRGMODE_ACQ_AND_EXTOUT ) v |= 1u << 30;
  Write(*b, 0x810C, v); return CAEN_DGTZ_Success;
}
CAEN_DGTZ_ErrorCode CAEN_DGTZ_SetAcquisitionMode(int handle, CAEN_DGTZ_AcqMode_t mode) {
  BOARD_OR_FAIL; uint32_t v = Read(*b, 0x8100); Write(*b, 0x8100, (v & ~3u) | ((uint32_t) mode & 3u)); return CAEN_DGTZ_Success;
}
CAEN_DGTZ_ErrorCode CAEN_DGTZ_SetRunSynchronizationMode(int handle, CAEN_DGTZ_RunSyncMode_t mode) { (void) mode; BOARD_OR_FAIL; return CAEN_DGTZ_Success; }
CAEN_DGTZ_ErrorCode CAEN_DGTZ_SetDPPAcquisitionMode(int handle, CAEN_DGTZ_DPP_AcqMode_t mode, CAEN_DGTZ_DPP_SaveParam_t param) {
  (void) mode; (void) param; BOARD_OR_FAIL; return CAEN_DGTZ_Success;
}

CAEN_DGTZ_ErrorCode CAEN_DGTZ_SetNumEventsPerAggregate(int handle, uint32_t numEvents, ...) {
  BOARD_OR_FAIL; Write(*b, 0x8034, numEvents); return CAEN_DGTZ_Success;
}
CAEN_DGTZ_ErrorCode CAEN_DGTZ_GetNumEventsPerAggregate(int handle, uint32_t * numEvents, ...) {
  BOARD_OR_FAIL; va_list ap; va_start(ap, numEvents); int ch = va_arg(ap, int); va_end(ap);
  *numEvents = Read(*b, 0x1034 + ((ch & ~1) << 8)); return CAEN_DGTZ_Success;
}
CAEN_DGTZ_ErrorCode CAEN_DGTZ_GetRecordLength(int handle, uint32_t * size, ...) {
  BOARD_OR_FAIL; va_list ap; va_start(ap, size); int ch = va_arg(ap, int); va_end(ap);
  *size = Read(*b, 0x1020 + ((ch & ~1) << 8)); return CAEN_DGTZ_Success;
}

// The library's automatic choice (values seen on the DT5730S in Sep 2026, commit 4cc1350).
CAEN_DGTZ_ErrorCode CAEN_DGTZ_SetDPPEventAggregation(int handle, int threshold, int maxsize) {
  (void) threshold; (void) maxsize; BOARD_OR_FAIL;
  b->st.libAggregation++;
  Write(*b, 0x8034, 256);
  Write(*b, 0x800C, 9);
  Write(*b, 0xEF1C, 512);
  return CAEN_DGTZ_Success;
}

CAEN_DGTZ_ErrorCode CAEN_DGTZ_GetDPP_SupportedVirtualProbes(int handle, int trace, int probes[], int * numProbes) {
  (void) trace; (void) probes; BOARD_OR_FAIL; *numProbes = 0; return CAEN_DGTZ_Success;
}

CAEN_DGTZ_ErrorCode CAEN_DGTZ_MallocReadoutBuffer(int handle, char ** buffer, uint32_t * size) {
  BOARD_OR_FAIL;
  if( b->fail == "malloc" ) return CAEN_DGTZ_OutOfMemory;
  uint32_t n = BufferSize(*b);
  if( n == 0 ) return CAEN_DGTZ_OutOfMemory;
  *buffer = (char *) malloc(n);
  if( !*buffer ) return CAEN_DGTZ_OutOfMemory;
  *size = b->allocated = n;
  return CAEN_DGTZ_Success;
}
CAEN_DGTZ_ErrorCode CAEN_DGTZ_FreeReadoutBuffer(char ** buffer) { free(*buffer); *buffer = nullptr; return CAEN_DGTZ_Success; }

CAEN_DGTZ_ErrorCode CAEN_DGTZ_SWStartAcquisition(int handle) {
  BOARD_OR_FAIL;
  if( b->st.running ) return CAEN_DGTZ_Success;
  b->st.running = 1;
  b->t0 = std::chrono::steady_clock::now();
  b->startIndex = b->st.aggAcquired;
  return CAEN_DGTZ_Success;
}
CAEN_DGTZ_ErrorCode CAEN_DGTZ_SWStopAcquisition(int handle) {
  BOARD_OR_FAIL;
  if( b->st.running ){
    b->st.aggAcquired = Acquired(*b); b->st.running = 0;
    b->held = (b->synthEvents > 0 && b->st.aggAcquired > b->st.aggRead) ? 1 : 0;   // the aggregate being filled
  }
  return CAEN_DGTZ_Success;
}
CAEN_DGTZ_ErrorCode CAEN_DGTZ_ClearData(int handle) { BOARD_OR_FAIL; Clear(*b); return CAEN_DGTZ_Success; }

// Hands out up to MaxAggregatePerBlockTransfer board aggregates the board has taken and not yet given.
CAEN_DGTZ_ErrorCode CAEN_DGTZ_ReadData(int handle, CAEN_DGTZ_ReadMode_t mode, char * buffer, uint32_t * bufferSize) {
  (void) mode; BOARD_OR_FAIL;
  if( b->fail == "read" ){ *bufferSize = 0; return CAEN_DGTZ_CommError; }
  long acq = b->st.running ? Acquired(*b) : b->st.aggAcquired - b->held;
  long perBLT = Read(*b, 0xEF1C) & 0x3FF;
  if( perBLT <= 0 ) perBLT = 1;
  uint32_t bytes = 0;
  long n = 0;
  std::vector<uint32_t> agg;
  while( b->st.aggRead + n < acq && n < perBLT ){
    long k = b->st.aggRead + n;
    if( b->synthEvents > 0 ) Generate(*b, k, agg);
    else agg.assign(b->words.begin() + b->aggStart[k], b->words.begin() + b->aggStart[k] + (b->words[b->aggStart[k]] & 0x0FFFFFFF));
    uint32_t size = (uint32_t) agg.size() * 4;
    if( bytes + size > b->allocated ){
      if( n == 0 ){ b->st.overflows++; *bufferSize = 0; return CAEN_DGTZ_OutOfMemory; }
      break;
    }
    memcpy(buffer + bytes, agg.data(), size);
    b->handedOut.insert(b->handedOut.end(), agg.begin(), agg.end());
    bytes += size;
    n++;
  }
  b->st.aggRead += n;
  *bufferSize = bytes;
  if( n > 0 && b->readDelayUs > 0 ) usleep(b->readDelayUs);
  return CAEN_DGTZ_Success;
}
