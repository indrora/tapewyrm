/*
 * flux_capture.c — free-running flux capture pipeline (DESIGN.md §5.4).
 *
 *   RDATA -> gw_flux input-capture -> gw_flux_encode_interval -> ring buffer
 *         -> gw_usb_write   (markers injected via markers.c on the same stream)
 *
 * The ring buffer (SRAM) absorbs host stalls; it does NOT keep pace with the
 * drive (the HS USB link is far faster than QIC-80's ~500 kbit/s flux, §3/§5.4).
 * Its real job is to give the backpressure check headroom so a sustained host
 * stall trips a CLEAN overflow->abort instead of corrupting the stream.
 *
 * TODO(bench) §13.6 item 1: gw_flux_encode_interval()/gw_flux_opcode_escape()
 * are stubs. The exact GW flux byte encoding + long-flux continuation escape +
 * opcode-escape channel values must be read from greaseweazle-firmware before
 * this produces decodable flux. The PIPELINE STRUCTURE here is real; the bytes
 * are placeholders.
 *
 * TODO(bench) §13.6 item 2: how cap_arm/disarm map onto GW's flux read arm/
 * read-stop control, and how the front-end is shared with stock GW's revolution-
 * gated read, is a GW-integration unknown.
 */

#include "flux_capture.h"
#include "arbiter.h"
#include "markers.h"
#include "verbs.h"
#include "gw_stubs.h"
#include "../protocol.h"

/* Ring buffer size: a chunk of the v4.1's 224 kB SRAM (§3). The exact split
 * with GW's own buffers is a vendoring decision. TODO(bench): size against GW's
 * SRAM map so we don't collide with its flux buffers. */
#ifndef CAP_RING_BYTES
#define CAP_RING_BYTES (32u * 1024u)
#endif

/* Near-overflow watermark: if free ring space stays below this across enough
 * consecutive pumps, declare sustained overflow (§5.4). */
#ifndef CAP_LOW_WATER
#define CAP_LOW_WATER (CAP_RING_BYTES / 8u)
#endif

/* How many consecutive low-water pumps count as "sustained" (vs a transient
 * stall the buffer should absorb). TODO(bench): tune against real host timing. */
#ifndef CAP_OVERFLOW_STREAK
#define CAP_OVERFLOW_STREAK 64u
#endif

/* Intervals drained per pump. */
#define CAP_DRAIN_CHUNK 64u

/* Max flux bytes one encoded interval can produce (long-flux continuation can
 * expand). Conservative upper bound for the staging buffer. TODO(bench): set
 * from GW's actual worst-case encoding. */
#define CAP_ENC_MAX 8u

/* ----------------------------------------------------------------------- */

static struct {
    bool armed;
    gw_flux_ctx_t *flux;
    gw_usb_ctx_t *usb;

    /* SRAM ring of encoded flux DATA bytes awaiting USB. */
    uint8_t ring[CAP_RING_BYTES];
    volatile uint32_t head; /* write index (producer: pump)  */
    volatile uint32_t tail; /* read index  (consumer: USB)   */

    mk_accounting_t acc;

    /* SEGMENT bookkeeping (driven by cap_on_index in ISR context). */
    volatile uint32_t seg_pending;   /* count of INDEX edges not yet emitted */
    volatile uint32_t seg_last_us;   /* timestamp of previous INDEX edge     */
    uint32_t seg_index;              /* running segment index                */

    /* Backpressure streak counter. */
    uint32_t low_water_streak;

    /* Latched overflow request from the backpressure check (acted on in pump). */
    bool overflow_requested;
} g_cap;

/* ---- ring helpers (single producer / single consumer) ---- */

static uint32_t ring_used(void) {
    return (g_cap.head - g_cap.tail) % CAP_RING_BYTES;
}

static uint32_t ring_free(void) {
    /* keep one slot empty to distinguish full from empty */
    return (CAP_RING_BYTES - 1u) - ring_used();
}

static bool ring_push(const uint8_t *buf, uint32_t n) {
    if (ring_free() < n) {
        return false; /* caller must treat as backpressure, never drop flux */
    }
    for (uint32_t i = 0; i < n; i++) {
        g_cap.ring[g_cap.head] = buf[i];
        g_cap.head = (g_cap.head + 1u) % CAP_RING_BYTES;
    }
    return true;
}

/* Drain the ring to USB. Returns bytes actually pushed (limited by TX free
 * space => natural backpressure surface). */
static uint32_t ring_drain_to_usb(void) {
    uint32_t pushed = 0;
    uint32_t avail = ring_used();
    while (avail > 0) {
        /* contiguous span up to wrap */
        uint32_t span = CAP_RING_BYTES - g_cap.tail;
        if (span > avail) {
            span = avail;
        }
        size_t took = gw_usb_write(g_cap.usb, &g_cap.ring[g_cap.tail], span);
        if (took == 0) {
            break; /* TX full: leave the rest in the ring (backpressure) */
        }
        g_cap.tail = (g_cap.tail + (uint32_t)took) % CAP_RING_BYTES;
        pushed += (uint32_t)took;
        avail -= (uint32_t)took;
        if ((uint32_t)took < span) {
            break; /* short write => TX nearly full */
        }
    }
    return pushed;
}

/* ---- pipeline ---- */

bool cap_arm(const cap_params_t *params) {
    if (params == NULL) {
        return false;
    }

    g_cap.flux = gw_flux_get();
    g_cap.usb = gw_usb_get();
    g_cap.head = 0;
    g_cap.tail = 0;
    g_cap.seg_pending = 0;
    g_cap.seg_index = 0;
    g_cap.seg_last_us = gw_now_us();
    g_cap.low_water_streak = 0;
    g_cap.overflow_requested = false;
    mk_reset(&g_cap.acc);

    /* SESSION_START anchors t=0 and tags the run's place in the stack (§7.2). */
    mk_session_start(g_cap.usb,
                     params->rate, gw_flux_sample_hz(),
                     params->tpt, params->direction, params->pass_id);

    /* Arm the INDEX EXTI -> cap_on_index, and start the free-running front-end. */
    gw_pin_irq_set(GW_PIN_INDEX, cap_on_index);
    gw_flux_arm(g_cap.flux);

    g_cap.armed = true;
    return true;
}

/* Encode + enqueue a batch of intervals. Returns false on a ring-push failure
 * (the batch could not be buffered => backpressure, surfaced to the pump). */
static bool encode_intervals(const uint32_t *ivals, size_t count) {
    for (size_t i = 0; i < count; i++) {
        uint8_t enc[CAP_ENC_MAX];
        /* TODO(bench) §13.6 item 1: real GW flux byte encoding. */
        size_t n = gw_flux_encode_interval(ivals[i], enc, sizeof enc);
        if (n == 0) {
            /* encoder declined (shouldn't happen with CAP_ENC_MAX sizing); skip
             * accounting so counts stay honest, and signal backpressure. */
            return false;
        }
        if (!ring_push(enc, (uint32_t)n)) {
            return false; /* no room — do NOT drop; let pump trip overflow */
        }
        /* Account AFTER a successful push so byte_count/checksum match what the
         * host will actually receive (checksum is over flux DATA bytes). */
        mk_account_flux(&g_cap.acc, enc, n);
        mk_account_flux_transition(&g_cap.acc);
    }
    return true;
}

/* Emit any SEGMENT markers queued by the INDEX ISR (kept out of ISR context). */
static void flush_segment_markers(void) {
    while (g_cap.seg_pending > 0) {
        /* Atomically take one pending edge. Single-core; a brief race with the
         * ISR only affects ordering of the running index, not correctness.
         * TODO(bench): wrap in a critical section if INDEX can pre-empt here. */
        g_cap.seg_pending--;
        uint32_t now = gw_now_us();
        uint32_t ticks = (uint32_t)(now - g_cap.seg_last_us);
        g_cap.seg_last_us = now;
        g_cap.seg_index++;
        mk_segment(g_cap.usb, ticks, g_cap.seg_index);
    }
}

/* Backpressure check (§5.4): a SUSTAINED low-water condition (not a transient
 * stall) means the host can't keep up and the buffer is about to overflow.
 * Latch an overflow request; the pump emits EVENT{overflow} and aborts. */
static void backpressure_check(void) {
    if (ring_free() < CAP_LOW_WATER) {
        if (g_cap.low_water_streak < 0xFFFFFFFFu) {
            g_cap.low_water_streak++;
        }
        if (g_cap.low_water_streak >= CAP_OVERFLOW_STREAK) {
            g_cap.overflow_requested = true;
        }
    } else {
        g_cap.low_water_streak = 0; /* recovered: transient stall absorbed */
    }
}

bool cap_pump(void) {
    if (!g_cap.armed) {
        return false; /* session ended (arbiter quiesced) */
    }

    /* 1. Service the out-of-band dead-man inputs FIRST (§5.2: CAP_HELD responds
     *    to exactly one out-of-band input — ABORT — plus the USB-loss dead-man). */
    uint8_t ctl;
    if (gw_usb_poll_control(g_cap.usb, &ctl)) {
        if (ctl == TW_TXN_ABORT) {
            arb_on_abort(); /* -> Quiesce -> cap_teardown -> cap_disarm */
            return false;
        }
    }
    if (!gw_usb_connected(g_cap.usb)) {
        arb_on_usb_loss(); /* safety-critical: stop the tape (§5.2) */
        return false;
    }

    /* 2. Emit SEGMENT markers for INDEX edges seen since last pump. */
    flush_segment_markers();

    /* 3. Drain measured intervals -> encode -> ring. */
    uint32_t ivals[CAP_DRAIN_CHUNK];
    size_t got = gw_flux_read_intervals(g_cap.flux, ivals, CAP_DRAIN_CHUNK);
    if (got > 0) {
        if (!encode_intervals(ivals, got)) {
            /* ring could not absorb the batch this pass: treat as immediate
             * backpressure pressure (counts toward the streak), but DON'T drop —
             * the bytes stay in ivals only if encoded; here they're lost from
             * the front-end, so this path itself is an overflow condition. */
            g_cap.overflow_requested = true;
        }
    }

    /* 4. Push ring -> USB. */
    (void)ring_drain_to_usb();

    /* 5. Backpressure policy. */
    backpressure_check();
    if (g_cap.overflow_requested) {
        /* Never silently drop flux: announce, then clean-abort via the funnel. */
        mk_event(g_cap.usb, TW_EVT_OVERFLOW);
        arb_on_overflow(); /* -> Quiesce -> cap_teardown -> cap_disarm(OVERFLOW) */
        return false;
    }

    gw_watchdog_kick();
    return true;
}

/* Map an arbiter reason onto the END marker reason code (§13.3). */
static uint8_t end_reason_from(arb_reason_t reason) {
    switch (reason) {
    case ARB_REASON_NORMAL:   return TW_END_NORMAL;
    case ARB_REASON_ABORT:    return TW_END_ABORT;
    case ARB_REASON_OVERFLOW: return TW_END_OVERFLOW;
    case ARB_REASON_EOT:      return TW_END_EOT;
    case ARB_REASON_USB_LOSS: return TW_END_USB_LOSS;
    case ARB_REASON_WATCHDOG: return TW_END_WATCHDOG;
    case ARB_REASON_ERROR:    return TW_END_ABORT; /* error -> aborted run */
    default:                  return TW_END_ABORT;
    }
}

void cap_disarm(arb_reason_t reason) {
    if (!g_cap.armed) {
        return; /* idempotent: already disarmed */
    }

    /* Halt the front-end and the INDEX EXTI first (stop arming/sampling). */
    gw_pin_irq_set(GW_PIN_INDEX, NULL);
    gw_flux_disarm(g_cap.flux);

    /* Flush any remaining flux: drain front-end -> encode -> ring, then ring ->
     * USB until empty or TX refuses. On USB loss this flush can't complete; the
     * host then sees no valid END and flags the run truncated (still decodes). */
    for (;;) {
        uint32_t ivals[CAP_DRAIN_CHUNK];
        size_t got = gw_flux_read_intervals(g_cap.flux, ivals, CAP_DRAIN_CHUNK);
        if (got == 0) {
            break;
        }
        if (!encode_intervals(ivals, got)) {
            break; /* ring full and we're tearing down; stop trying */
        }
    }
    /* Emit any straggler SEGMENT markers, then drain the ring. */
    flush_segment_markers();
    while (ring_used() > 0) {
        uint32_t pushed = ring_drain_to_usb();
        if (pushed == 0) {
            break; /* TX won't accept more (e.g. USB gone) */
        }
    }

    /* Seal the run: END carries reason + counts + checksum (§5.4). */
    if (gw_usb_connected(g_cap.usb)) {
        mk_end(g_cap.usb, end_reason_from(reason), &g_cap.acc);
    }

    g_cap.armed = false;
}

void cap_on_index(void) {
    /* ISR context: do minimal work. Just flag a pending segment; the marker
     * (with tick delta + index) is emitted from cap_pump()/flush. (§5.4) */
    if (!g_cap.armed) {
        return;
    }
    g_cap.seg_pending++;
}

void cap_teardown(arb_reason_t reason, void *ctx) {
    (void)ctx;
    /* Invoked by the arbiter inside Quiesce, lease still held. The STOP_TAPE is
     * issued by transactions.c's capture wrapper (it owns the verbs engine and
     * knows the stop command); here we cleanly terminate the flux side: halt
     * front-end, flush, emit END. Ordering (stop-then-disarm vs disarm-then-stop)
     * is coordinated in transactions.c so the tape is stopped before release
     * (§5.2 I5). cap_disarm is idempotent so double-invocation is safe. */
    cap_disarm(reason);
}
