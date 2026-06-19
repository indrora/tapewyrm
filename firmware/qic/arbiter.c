/*
 * arbiter.c — bus arbiter lease state machine (DESIGN.md §5.2).
 *
 * Implements the single-lease, single-grantor, single-release-funnel model.
 * The arbiter owns drive-select (GW_PIN_DRIVE_SELECT) and is the ONLY module
 * that asserts/deasserts it. The verbs and flux engines never touch select.
 *
 * Re-entrancy / ISR note: dead-man triggers (arb_on_abort/watchdog/usb_loss/
 * overflow) may be reached from interrupt or pump context. They all collapse to
 * arb_quiesce(), whose first action is the idempotent guard (I4), so a second
 * trigger mid-teardown is a safe no-op. This is a single-core Cortex-M4 with a
 * cooperative main loop; arb_quiesce() is not designed to be pre-empted by a
 * second arb_quiesce() on the same call stack, hence the guard rather than a
 * lock. TODO(bench): if any trigger fires from a true ISR that can pre-empt the
 * main-loop teardown, gate the guard with a critical section (GW: __disable_irq
 * / target critical-section helper).
 */

#include "arbiter.h"
#include "gw_stubs.h"

/* assert(): freestanding-friendly. In a debug build (§12.1, 3 Mbaud logging)
 * this can route to a GW panic/log; here it compiles to a trap-or-noop so the
 * skeleton builds standalone. TODO(bench): bind to GW's assert/panic. */
#ifndef TW_ASSERT
#ifdef NDEBUG
#define TW_ASSERT(cond) ((void)0)
#else
#define TW_ASSERT(cond) do { if (!(cond)) { for (;;) { /* trap */ } } } while (0)
#endif
#endif

/* ----------------------------------------------------------------------- */

static struct {
    arb_state_t state;
    arb_holder_t holder;
    arb_teardown_fn teardown;
    void *ctx;
    bool select_active; /* whether THIS lease asserted drive-select */
} g_arb;

static void arb_select(bool active) {
    /* I2: drive-select is owned here and nowhere else. */
    gw_pin_write(GW_PIN_DRIVE_SELECT, active);
}

void arb_init(void) {
    g_arb.state = ARB_IDLE;
    g_arb.holder = ARB_HOLDER_NONE;
    g_arb.teardown = NULL;
    g_arb.ctx = NULL;
    g_arb.select_active = false;
    arb_select(false); /* ensure deselected at boot */
}

arb_state_t arb_state(void) { return g_arb.state; }
arb_holder_t arb_holder(void) { return g_arb.holder; }
bool arb_is_idle(void) { return g_arb.state == ARB_IDLE; }

bool arb_grant(arb_holder_t holder, arb_teardown_fn teardown, void *ctx,
               bool select_active) {
    /* I1: one holder at a time. A grant is only legal from IDLE. */
    if (g_arb.state != ARB_IDLE || g_arb.holder != ARB_HOLDER_NONE) {
        return false;
    }
    TW_ASSERT(holder == ARB_HOLDER_VERBS || holder == ARB_HOLDER_FLUX);

    /* Transient GRANT: validate, assert select, hand off. */
    g_arb.state = ARB_GRANT;
    g_arb.holder = holder;
    g_arb.teardown = teardown;
    g_arb.ctx = ctx;
    g_arb.select_active = select_active;

    if (select_active) {
        arb_select(true); /* I2: select asserted only while a lease is held */
    }

    /* Branch on type into the held state (§5.2). */
    g_arb.state = (holder == ARB_HOLDER_FLUX) ? ARB_CAP_HELD : ARB_CMD_HELD;
    return true;
}

void arb_quiesce(arb_reason_t reason) {
    /* I4: idempotent. A second trigger while quiescing — or when already idle —
     * is a no-op. This is what makes the dead-man triggers safe to fire
     * redundantly (e.g. watchdog races a normal completion). */
    if (g_arb.state == ARB_QUIESCE || g_arb.state == ARB_IDLE) {
        return;
    }

    /* Must be in a held state to quiesce (GRANT is transient and never blocks
     * here; if we ever observe GRANT it means a grant was interrupted — treat
     * it like a held state and tear down). */
    TW_ASSERT(g_arb.state == ARB_CMD_HELD ||
              g_arb.state == ARB_CAP_HELD ||
              g_arb.state == ARB_GRANT);

    g_arb.state = ARB_QUIESCE;

    /*
     * Step 2-4 (§5.2): run the holder's safe teardown INSIDE the still-held
     * lease. For a capture this hands flux->verbs, issues STOP_TAPE/PAUSE
     * BEFORE releasing (I5 — stop first, always), flushes the buffer, and emits
     * the END marker. For a bounded command op it is typically a no-op.
     *
     * The arbiter does not itself know how to talk to the drive; it delegates
     * the safe-stop to the holder's teardown so the funnel stays engine-agnostic
     * while still guaranteeing every path stops the tape.
     */
    if (g_arb.teardown != NULL) {
        g_arb.teardown(reason, g_arb.ctx);
    }

    /* Step 5: restore drive-select, release the lease, -> IDLE (I2, I3). */
    if (g_arb.select_active) {
        arb_select(false);
    }
    g_arb.holder = ARB_HOLDER_NONE;
    g_arb.teardown = NULL;
    g_arb.ctx = NULL;
    g_arb.select_active = false;
    g_arb.state = ARB_IDLE;

    /* Kick the watchdog: we reached a known-safe state. */
    gw_watchdog_kick();
}

/* ---- dead-man / fault triggers: every one funnels through arb_quiesce ---- */

void arb_on_abort(void) {
    /* ABORT is valid only during a held state; harmless no-op otherwise. */
    arb_quiesce(ARB_REASON_ABORT);
}

void arb_on_watchdog(void) {
    arb_quiesce(ARB_REASON_WATCHDOG);
}

void arb_on_usb_loss(void) {
    /* Safety-critical: stop the tape even though the host is gone (§5.2). */
    arb_quiesce(ARB_REASON_USB_LOSS);
}

void arb_on_overflow(void) {
    /* Never silently drop flux: a sustained near-overflow trips a clean abort
     * through the same funnel (§5.4). The EVENT{overflow} marker is emitted by
     * the flux engine before it calls this, so the host learns why. */
    arb_quiesce(ARB_REASON_OVERFLOW);
}
