/*
 * verbs.h — QIC verbs engine: the command/status channel (DESIGN.md §5.3, §13.4).
 *
 * VERBATIM passthrough (§4.1 decision 2): the engine emits command NUMBERS as
 * STEP pulse counts without knowing their meaning. All QIC-117 semantics live
 * host-side. This engine only knows pulses, the N+2 argument form, the report
 * bit clock, and wait-ready.
 *
 * It runs entirely device-side so the report-bit loop is immune to USB jitter
 * (§5.3); the drive latches the report value at command receipt.
 *
 * The engine is a LEASE HOLDER (ARB_HOLDER_VERBS): transactions.c grants the
 * lease before calling these and quiesces after. These functions assume the
 * bus is already theirs and drive-select is asserted.
 */
#ifndef TAPEWYRM_VERBS_H
#define TAPEWYRM_VERBS_H

#include <stdint.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/*
 * Timing envelope, configured by SET_TIMING (§5.1, §13.3) from the host
 * DriveProfile. All values are microseconds unless noted. Defaults track
 * QIC-117 Rev J Table 1 (§2.1):
 *   - step interval ~2.0 ms (0.9-2.1 ms)
 *   - terminating gap > 2.9 ms ends the command (but pulses MUST stay grouped
 *     under the ~2.5 ms command time-out, else an isolated slow pulse reads as
 *     Soft Reset — §2.1 hazard)
 *   - report ACK within TACK ~2.5 ms; each report bit within TBIT ~900 µs.
 *
 * TODO(bench) §13.6 item 3: the real per-drive envelope (and whether to wait on
 * the INDEX edge vs a fixed settle) comes from characterizing the target drive.
 */
typedef struct {
    uint16_t pulse_us;          /* STEP pulse asserted width                  */
    uint16_t inter_pulse_us;    /* gap between pulses within a train (~step interval) */
    uint16_t terminate_gap_us;  /* gap that ENDS a command train (> ~2.9 ms)  */
    uint16_t tack_us;           /* max wait for the ACK bit (~2.5 ms)         */
    uint16_t tbit_us;           /* max wait per report data bit (~900 µs)     */
    uint8_t  report_on_index;   /* 1 = wait on INDEX edge for each bit; 0 = fixed settle */
} qic_timing_t;

/* Result of a report transaction (§5.3). */
typedef struct {
    bool ack;          /* first bit: TRUE = ok, FALSE = reset/hardware failure */
    bool final_ok;     /* last bit: TRUE = ok, FALSE = error occurred mid-report */
    uint16_t bits;     /* up to 16 data bits, LSB-first (Report Error Code = 2 bytes) */
    uint8_t nbits;     /* how many data bits were clocked                      */
    bool timed_out;    /* a TACK/TBIT window expired                           */
} qic_report_t;

/* Install / read the active timing envelope. SET_TIMING is idle-only (§13.3),
 * but the struct is read on every pulse, so it lives here. */
void qic_set_timing(const qic_timing_t *t);
const qic_timing_t *qic_get_timing(void);
void qic_timing_defaults(qic_timing_t *out);

/*
 * Emit `n` STEP pulses at the configured cadence, verbatim, then hold the
 * terminating gap (§5.3). Constraints enforced here:
 *   - pulses stay grouped under the command time-out (inter_pulse_us is bounded
 *     so an isolated slow pulse can't read as Soft Reset — §2.1 hazard);
 *   - TRK0 is an INPUT and is never asserted by us (we only ever read it);
 *   - the train is followed by terminate_gap_us so the drive sees the command end.
 *
 * `n == 0` is legal (e.g. Soft Select sends a literal 20 via this; an argument
 * value of 0 is sendable as the N+2 form, see qic_arg). A train longer than 32
 * pulses is ignored by the drive (§13.1); we still emit verbatim.
 */
void qic_pulses(uint8_t n);

/*
 * Emit an argument operand in N+2 pulse form (value + 2 pulses), as a SEPARATE
 * pulse train following the command (§2.1, §5.3). N+2 ensures an argument can
 * never collide with single-pulse Soft Reset and that zero is sendable.
 *
 * NOTE: Soft Select's literal-20 form is NOT an argument — the host sends it as
 * a plain command via qic_pulses(20); this function is only for argument trains.
 */
void qic_arg(uint8_t value);

/*
 * Run a report transaction: the report command has ALREADY been emitted (via
 * qic_pulses) by the caller. This reads the ACK within TACK, then for each of
 * `report_bits` data bits issues REPORT_NEXT_BIT (2 pulses) and samples TRK0
 * within TBIT (LSB-first), then reads the Final bit. Result is written to *out.
 *
 * `report_bits` is 0..16. The whole loop is device-side (§5.3).
 */
void qic_report(uint8_t report_bits, qic_report_t *out);

/*
 * Poll the ready/INDEX line until ready or `timeout_ms` elapses. Returns true
 * if ready was observed. Motion ops are seconds-to-minutes (§2.1) so the
 * timeout is generous and motion-scaled; the watchdog backstops a wedged drive.
 * Prefer waiting on the ready/INDEX edge over a blind delay (§5.3).
 */
bool qic_wait_ready(uint32_t timeout_ms);

#ifdef __cplusplus
}
#endif

#endif /* TAPEWYRM_VERBS_H */
