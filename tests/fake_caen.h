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
  int flushes;         // forced data flushes (0x8040 / 0x1n40 written)
  int running;
  long startSeq;       // order of the last SWStart / SWStop over all boards (1, 2, ...; 0 = never)
  long stopSeq;
} fake_caen_stats;

// Add a board. link: 0 = USB (num = USB link), 1 = optical (num = port, node = daisy-chain index).
// model: "V1730" (16 ch) or "DT5730" (8 ch). image: an 8192-byte FSUDAQ settings file of the real
// board to take the identity registers from (may be NULL). Returns 0 on success.
int  fake_caen_add_board(int link, int num, int node, int serial, const char * model, const char * image);
// The board also answers on this USB link (a desktop board with its USB cable plugged in too).
void fake_caen_add_usb_alias(int serial, int usbLink);
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
// Make the next calls fail: what = "malloc" (MallocReadoutBuffer), "write" (WriteRegister),
// "read" (ReadData), "hang" (ReadData blocks until cleared, like a call stuck in the driver),
// "reopen" (the board opens once, then no more), "stopread" (ReadData fails once the board is
// stopped: a problem only the stop finds), "overread" (ReadData says it read more than the buffer
// holds), 0 or "" to clear.
void fake_caen_fail(int serial, const char * what);
// A write to this address returns success but is lost (a write dropped on the link); 0 = none.
void fake_caen_drop_write(int serial, uint32_t address);
// A DT5730 whose PLL reference changes (0x8100 bit 6, or a reset while on CLK-IN) is settling for
// this long: 0x8104 shows it not ready, and any access but the status registers (0x8104, 0xEF04,
// 0x8178) hangs it for good, like the boards at PSI on 27 Oct 2026. Default 30 ms.
void fake_caen_set_clock_settle_ms(int ms);
int  fake_caen_is_hung(int serial);
// At a stop the board holds nHeld aggregates still being filled (default 1, generated data only);
// they are read only after a forced data flush, which makes them readable one every latencyMs
// (default 0: all at once).
void fake_caen_set_flush(int serial, int nHeld, int latencyMs);
// Forget what the board handed out, cleared and flushed so far (a refused start that ran it briefly).
void fake_caen_forget(int serial);

#ifdef __cplusplus
}
#endif
#endif
