/*
 * flux_capture.h — free-running flux capture engine (DESIGN.md §5.4, §13.4).
 *
 * Pipeline:  RDATA -> capture front-end -> GW flux encoder -> ring buffer
 *            -> USB streamer  (with a marker injector merging into the stream)
 *
 * KEY DIFFERENCE from stock GW: the capture is FREE-RUNNING — armed/disarmed on
 * command, NOT gated to one index-to-index revolution. Tape asserts INDEX once
 * per SEGMENT (not per revolution), so the engine RECORDS each INDEX edge as a
 * SEGMENT marker rather than gating on it (§2.2, §5.4).
 *
 * The flux engine is a LEASE HOLDER (ARB_HOLDER_FLUX): transactions.c grants the
 * lease for the WHOLE capture session and registers cap_teardown() as the
 * arbiter teardown hook, so every exit (normal/abort/overflow/USB-loss/watchdog)
 * routes through the Quiesce funnel and stops the tape before releasing (§5.2).
 *
 * Backpressure policy (§5.4): never silently drop flux. Sustained near-overflow
 * -> emit EVENT{overflow} + trigger a clean abort through the arbiter funnel.
 */
#ifndef TAPEWYRM_FLUX_CAPTURE_H
#define TAPEWYRM_FLUX_CAPTURE_H

#include <stdint.h>
#include <stdbool.h>

#include "arbiter.h" /* arb_reason_t for the teardown hook */
#include "markers.h" /* mk_accounting_t */

#ifdef __cplusplus
extern "C" {
#endif

/* Parameters captured at arm time, mostly to stamp SESSION_START (§7.2). */
typedef struct {
    uint16_t rate;       /* declared bitcell rate (kbit/s), from drive config */
    uint16_t tpt;        /* tape track this pass records                      */
    uint8_t  direction;  /* 0 = forward (even track) / 1 = reverse (odd)      */
    uint16_t pass_id;    /* which pass of this track                          */
} cap_params_t;

/*
 * Arm the capture engine: emit SESSION_START, reset accounting, arm the INDEX
 * EXTI, and start the free-running front-end. Caller (transactions.c) must have
 * already taken the lease (ARB_HOLDER_FLUX) and issued the motion command.
 *
 * Returns true on success. After this, cap_pump() must be called repeatedly to
 * move flux from the front-end through the encoder/ring to USB.
 */
bool cap_arm(const cap_params_t *params);

/*
 * Pump one iteration of the pipeline: drain freshly-measured intervals, encode
 * them to GW flux bytes, push to USB, run the backpressure check, and service
 * the out-of-band ABORT/USB-loss dead-man. Returns false when the session has
 * ended (the arbiter has quiesced) — the caller's pump loop then exits.
 *
 * This is the cooperative heartbeat of a held capture; it is the ONLY place the
 * capture session is responsive (§5.2: CAP_HELD responds to exactly one
 * out-of-band input, ABORT/STOP, surfaced here).
 */
bool cap_pump(void);

/*
 * Disarm the front-end cleanly: halt capture, flush the ring, emit END with the
 * given reason + accounting. This is invoked by cap_teardown() inside the
 * arbiter Quiesce funnel; do NOT call it directly to end a session — call
 * arb_quiesce()/arb_on_abort() so the tape is stopped first (§5.2 I5).
 */
void cap_disarm(arb_reason_t reason);

/*
 * INDEX edge ISR hook (registered via gw_pin_irq_set on GW_PIN_INDEX). On each
 * hardware INDEX edge it records a SEGMENT marker (tick delta since the previous
 * segment + running index). ISR-safe: it only timestamps + flags; the marker is
 * emitted from cap_pump() to keep ISR work minimal. (§5.4)
 *
 * Declared here so transactions.c can wire it; defined in flux_capture.c.
 */
void cap_on_index(void);

/*
 * Arbiter teardown hook (arb_teardown_fn). Registered with arb_grant() when the
 * capture lease is taken. The arbiter calls this inside Quiesce, still holding
 * the lease: it hands flux->verbs (stop arming/sampling), the caller-supplied
 * STOP is issued by transactions.c's wrapper, the buffer is flushed and END
 * emitted. See transactions.c for how STOP_TAPE is issued before release.
 */
void cap_teardown(arb_reason_t reason, void *ctx);

#ifdef __cplusplus
}
#endif

#endif /* TAPEWYRM_FLUX_CAPTURE_H */
