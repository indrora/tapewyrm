/*
 * arbiter.h — bus arbiter lease state machine (DESIGN.md §5.2, §13.4).
 *
 * The LEASE is the exclusive right to drive the bus (toggle a pin or stream
 * flux). ONE holder at a time; the arbiter is the sole owner of drive-select and
 * the sole grantor. This is what makes "never stream flux while pulsing STEP" a
 * STRUCTURAL guarantee rather than a host convention (§4.1).
 *
 * State machine (§5.2):
 *
 *      ARB_IDLE ──grant──► ARB_GRANT ──┬─► ARB_CMD_HELD ─┐
 *         ▲                            └─► ARB_CAP_HELD ─┤
 *         │                                              │
 *         └────────────── ARB_QUIESCE ◄─────────────────┘
 *
 *   IDLE       no lease, bus quiescent; config serviced here.
 *   GRANT      transient: validate txn, assert select, hand lease to one engine.
 *   CMD_HELD   verbs engine owns the bus for a BOUNDED op (pulses/report/wait).
 *   CAP_HELD   flux engine holds the lease for the WHOLE host-paced stream;
 *              responsive to exactly one out-of-band input: ABORT/STOP.
 *   QUIESCE    THE RELEASE FUNNEL: every exit from a held state (normal, error,
 *              abort, watchdog, fault, USB-loss) routes here for the SAME safe
 *              teardown before releasing.
 *
 * INVARIANTS (encoded as asserts in arbiter.c):
 *   I1  At most one lease holder at any time.
 *   I2  Drive-select is owned by the arbiter; asserted only while a lease is
 *       held; restored (deselect) on every release.
 *   I3  Every release passes through the single Quiesce funnel — no lease leaks,
 *       tape is never left running.
 *   I4  Quiesce is IDEMPOTENT: a second trigger while quiescing is a no-op.
 *   I5  Before releasing, the funnel issues STOP_TAPE/PAUSE (stop first, always)
 *       so the lease is never dropped with tape rolling (broken-tape hazard).
 */
#ifndef TAPEWYRM_ARBITER_H
#define TAPEWYRM_ARBITER_H

#include <stdint.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Lease states (§5.2). */
typedef enum {
    ARB_IDLE = 0,
    ARB_GRANT,
    ARB_CMD_HELD,
    ARB_CAP_HELD,
    ARB_QUIESCE,
} arb_state_t;

/* Who the lease is granted to. */
typedef enum {
    ARB_HOLDER_NONE = 0,
    ARB_HOLDER_VERBS,   /* command/status channel (bounded op) */
    ARB_HOLDER_FLUX,    /* data channel (whole capture session) */
} arb_holder_t;

/* Why a held state is being torn down — routed through Quiesce. Maps onto the
 * TW_END_* reason carried in the END marker for a capture (§13.3). */
typedef enum {
    ARB_REASON_NORMAL = 0,  /* op completed cleanly                          */
    ARB_REASON_ABORT,       /* host out-of-band ABORT/STOP                   */
    ARB_REASON_ERROR,       /* drive/protocol error (e.g. broken-tape err 10)*/
    ARB_REASON_WATCHDOG,    /* guard timeout / max duration / byte budget    */
    ARB_REASON_OVERFLOW,    /* flux ring near-overflow (never drop flux)     */
    ARB_REASON_EOT,         /* end-of-tape reached                           */
    ARB_REASON_USB_LOSS,    /* USB suspend/disconnect dead-man (safety-crit) */
} arb_reason_t;

/*
 * Quiesce callback: the held engine's safe-teardown hook, invoked INSIDE the
 * still-held lease before drive-select is restored. For a capture this is where
 * flux->verbs hands off, STOP_TAPE is issued, the buffer is flushed, and the END
 * marker is emitted (§5.2 step 2-4). For a bounded command op it is typically a
 * no-op (the op already returned the bus to quiescent).
 *
 * MUST be idempotent and MUST NOT itself call arb_quiesce() (re-entrancy is
 * handled by the arbiter's I4 guard). It receives the teardown reason so it can
 * choose the END reason and decide whether STOP must be issued.
 */
typedef void (*arb_teardown_fn)(arb_reason_t reason, void *ctx);

/* Initialize the arbiter to IDLE with no lease and drive-select deselected.
 * Call once at firmware start, after gw_stubs are bound. */
void arb_init(void);

/* Current state / holder (for assertions and INFO/diagnostics). */
arb_state_t arb_state(void);
arb_holder_t arb_holder(void);
bool arb_is_idle(void);

/*
 * Take the lease for `holder` and run its teardown via `teardown(ctx)` when the
 * held state later exits through Quiesce. `select_active` chooses whether to
 * assert drive-select for this lease (normally true; sticky-select optimization
 * may keep it asserted across back-to-back command txns — §5.2).
 *
 * Returns true on success (state -> ARB_GRANT -> CMD_HELD/CAP_HELD). Returns
 * false if a lease is already held (I1) — the caller must not touch the bus.
 *
 * Transitions to ARB_CMD_HELD for ARB_HOLDER_VERBS, ARB_CAP_HELD for
 * ARB_HOLDER_FLUX.
 */
bool arb_grant(arb_holder_t holder, arb_teardown_fn teardown, void *ctx,
               bool select_active);

/*
 * THE single release path (§5.2 invariant I3). Routes any held state to a safe
 * teardown and back to IDLE:
 *   1. (idempotent guard) if already quiescing/idle, no-op.
 *   2. run the holder's teardown(reason) inside the held lease (stop/flush/END).
 *   3. restore drive-select.
 *   4. drop the lease -> ARB_IDLE.
 * Safe to call from normal completion, error paths, and dead-man triggers.
 */
void arb_quiesce(arb_reason_t reason);

/* ---- dead-man / fault triggers — all funnel through arb_quiesce() ---- */

/* Out-of-band ABORT/STOP arrived during a capture (§5.1/§5.2). No-op if idle. */
void arb_on_abort(void);

/* Guard timeout / max-duration / byte-budget watchdog fired (§5.2). */
void arb_on_watchdog(void);

/* USB suspend/disconnect detected — the SAFETY-CRITICAL case and the strongest
 * reason the device, not the host, owns the stop (§5.2). No-op if idle. */
void arb_on_usb_loss(void);

/* Flux ring near-overflow — emit EVENT{overflow} (caller) then clean abort. */
void arb_on_overflow(void);

#ifdef __cplusplus
}
#endif

#endif /* TAPEWYRM_ARBITER_H */
