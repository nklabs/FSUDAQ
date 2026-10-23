#ifndef FAKE_CAEN_H
#define FAKE_CAEN_H
// A stand-in for libCAENDigitizer that keeps each board's registers in memory, so the
// settings and readout code can be run and checked without hardware. Built as
// libCAENDigitizer.so in tests/, it exports the CAEN functions FSUDAQ calls plus the
// fake_caen_* helpers below for the tests.
//
// Every read/write register of a new board starts at a sentinel, 0xDEAD0000 | (address >> 2),
// standing for "whatever the board held before"; a register still holding its sentinel after
// FSUDAQ has programmed the board was never written. Identity registers (board info, firmware
// revisions) come from a settings snapshot of the real board.

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
  int resets;          // software resets and configuration reloads received
  int libAggregation;  // CAEN_DGTZ_SetDPPEventAggregation calls (the library choosing aggregation itself)
  int writes;          // register writes
  long aggTotal;       // board aggregates in the data source
  long aggAcquired;    // aggregates the board had taken when it was stopped (or so far)
  long aggRead;        // aggregates handed out by ReadData
  long aggCleared;     // aggregates discarded by ClearData or a reset
  int overflows;       // ReadData calls whose data did not fit the readout buffer
  int running;
} fake_caen_stats;

// Add a board. link: 0 = USB (num = USB link), 1 = optical (num = port, node = daisy-chain index).
// model: "V1730" (16 ch) or "DT5730" (8 ch). image: an 8192-byte FSUDAQ settings file of the real
// board to take the identity registers from (may be NULL). Returns 0 on success.
int  fake_caen_add_board(int link, int num, int node, int serial, const char * model, const char * image);
// Serve the board aggregates of an .fsu file from ReadData; aggPerMs: how fast the board "takes" them.
int  fake_caen_set_data(int serial, const char * fsuFile, double aggPerMs);
void fake_caen_reset_all(void);
uint32_t fake_caen_reg(int serial, uint32_t address);          // the register as the board holds it
void fake_caen_set_reg(int serial, uint32_t address, uint32_t value);
int  fake_caen_get_stats(int serial, fake_caen_stats * stats);
int  fake_caen_is_sentinel(uint32_t address, uint32_t value);
// Generate DPP-PSD data instead: every board aggregate holds eventsPerCouple events in each couple
// (3 words each: time tag, extras option 2, charges), like odin's files.
int  fake_caen_set_synthetic(int serial, int eventsPerCouple, double aggPerMs);
// Each ReadData that returns data takes this long, like a block transfer, so the readout can fall
// behind and the board holds data when it is stopped.
void fake_caen_set_read_delay(int serial, int microseconds);
// Every byte ReadData handed out since the board was added, to compare with the saved files.
size_t fake_caen_handed_out(int serial, const uint32_t ** words);
// Make the next calls fail: what = "malloc" (MallocReadoutBuffer), "write" (WriteRegister) or
// "read" (ReadData), 0 or "" to clear.
void fake_caen_fail(int serial, const char * what);

#ifdef __cplusplus
}
#endif
#endif
