/*
 * transactions.c — USB transaction dispatch (DESIGN.md §5.1, §13.3).
 *
 * Maps the §13.3 transaction table onto the arbiter / verbs / flux modules:
 *
 *   INFO         -> model/mcu/fw/sram + caps + proto version (capability gate)
 *   SET_TIMING   -> qic_set_timing (idle-only)
 *   SELECT       -> drive-select hint (idle-only)
 *   COMMAND_TXN  -> lease -> verbs (n pulses [+ report clock]) -> quiesce -> resp
 *   WAIT_READY   -> qic_wait_ready
 *   CAPTURE      -> lease (whole session) -> motion -> arm flux; main loop pumps
 *   ABORT/STOP   -> OUT-OF-BAND; handled in cap_pump(), not dispatched here
 *
 * Request/response payloads are little-endian, matching the host link layer.
 * TODO(bench) §13.6 item 2: byte-level framing aligns with GW's existing command
 * framing once the GW handler integration is settled; the (de)serialization here
 * uses the field layouts from §13.3 and is provisional on that.
 */

#include "transactions.h"
#include "arbiter.h"
#include "verbs.h"
#include "flux_capture.h"
#include "markers.h"
#include "gw_stubs.h"
#include "protocol.h"

/* QIC-117 motion command numbers used by the capture wrapper (§13.1).
 * These are the ONLY command numbers the firmware knows by name; everything else
 * is verbatim from the host (§4.1 decision 2). STOP_TAPE is named because the
 * Quiesce funnel must be able to stop the tape itself on a dead-man trigger when
 * the host is gone (§5.2). */
#define QIC_CMD_STOP_TAPE 18u /* command 18 = 18 STEP pulses (§13.1) */
#define QIC_CMD_PAUSE     3u  /* command 3  = 3 STEP pulses (fallback stop) */

/* ----------------------------------------------------------------------- */

/* State carried across the CAPTURE arm -> pump -> teardown lifecycle. */
static struct {
    bool active;
    cap_params_t params;
} g_cap_session;

/* Little-endian readers (host writes LE per §13.3). */
static uint16_t rd_u16(const uint8_t *p) {
    return (uint16_t)(p[0] | ((uint16_t)p[1] << 8));
}
static uint32_t rd_u32(const uint8_t *p) {
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
           ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}
static size_t wr_u16(uint8_t *p, uint16_t v) {
    p[0] = (uint8_t)(v & 0xFF);
    p[1] = (uint8_t)((v >> 8) & 0xFF);
    return 2;
}
static size_t wr_u32(uint8_t *p, uint32_t v) {
    p[0] = (uint8_t)(v & 0xFF);
    p[1] = (uint8_t)((v >> 8) & 0xFF);
    p[2] = (uint8_t)((v >> 16) & 0xFF);
    p[3] = (uint8_t)((v >> 24) & 0xFF);
    return 4;
}

static void respond(const uint8_t *buf, size_t len) {
    gw_usb_ctx_t *usb = gw_usb_get();
    (void)gw_usb_write(usb, buf, len);
}

/* A one-byte status response: 0 = ok, nonzero = error. */
static void respond_status(uint8_t st) {
    respond(&st, 1);
}

/* ----------------------------- INFO ----------------------------- */

static void handle_info(void) {
    /*
     * DeviceInfo{model, mcu, fw, sram, caps, proto_ver} (§13.3). The host uses
     * the caps bitmask + proto version as a capability gate (§6A.2) and refuses
     * to run against stock GW firmware.
     *
     * Strings are fixed identifiers here; TODO(bench): pull the real model/MCU
     * id and SRAM size from GW's board/target info rather than hard-coding.
     */
    static const char model[] = "Greaseweazle v4.1 (Tapewyrm)";
    static const char mcu[]   = "AT32F403";
    static const char fw[]    = "tapewyrm-skeleton";

    uint8_t resp[128];
    size_t n = 0;

    resp[n++] = (uint8_t)TW_PROTO_VERSION;

    /* caps bitmask: this firmware provides all three (§13.3 INFO). */
    uint32_t caps = TW_CAP_VERBS | TW_CAP_CAPTURE | TW_CAP_MARKERS;
    n += wr_u32(resp + n, caps);

    /* SRAM bytes (224 kB on current firmware, §3). TODO(bench): query GW. */
    n += wr_u32(resp + n, 224u * 1024u);

    /* flux sample clock (host needs it to convert ticks->time). */
    n += wr_u32(resp + n, gw_flux_sample_hz());

    /* Length-prefixed strings: model, mcu, fw. */
    const char *strs[3] = { model, mcu, fw };
    for (int s = 0; s < 3; s++) {
        const char *p = strs[s];
        size_t slen = 0;
        while (p[slen] != '\0') {
            slen++;
        }
        if (n + 1 + slen > sizeof resp) {
            break; /* defensive */
        }
        resp[n++] = (uint8_t)slen;
        for (size_t i = 0; i < slen; i++) {
            resp[n++] = (uint8_t)p[i];
        }
    }

    respond(resp, n);
}

/* -------------------------- SET_TIMING -------------------------- */

static void handle_set_timing(const uint8_t *payload, size_t len) {
    /* idle-only (§13.3). */
    if (!arb_is_idle()) {
        respond_status(1);
        return;
    }
    /* TimingParams layout (LE): pulse_us:u16, inter_pulse_us:u16,
     * terminate_gap_us:u16, tack_us:u16, tbit_us:u16, report_on_index:u8 (11). */
    if (len < 11) {
        respond_status(2);
        return;
    }
    qic_timing_t t;
    t.pulse_us         = rd_u16(payload + 0);
    t.inter_pulse_us   = rd_u16(payload + 2);
    t.terminate_gap_us = rd_u16(payload + 4);
    t.tack_us          = rd_u16(payload + 6);
    t.tbit_us          = rd_u16(payload + 8);
    t.report_on_index  = payload[10];
    qic_set_timing(&t);
    respond_status(0);
}

/* ---------------------------- SELECT ---------------------------- */

static void handle_select(const uint8_t *payload, size_t len) {
    (void)payload;
    (void)len;
    /* idle-only (§13.3). SelectHint carries optional sticky-select / unit hints;
     * the arbiter owns the actual drive-select assertion. For the skeleton we
     * accept the hint and store nothing observable. TODO(bench): apply phantom/
     * soft-select unit + sticky policy from the hint. */
    if (!arb_is_idle()) {
        respond_status(1);
        return;
    }
    respond_status(0);
}

/* ------------------------- COMMAND_TXN -------------------------- */

/* Teardown hook for a bounded command op: nothing extra to do — the verbs op
 * already returned the bus to quiescent (STEP released, TRK0 only ever read). */
static void cmd_teardown(arb_reason_t reason, void *ctx) {
    (void)reason;
    (void)ctx;
}

static void handle_command_txn(const uint8_t *payload, size_t len) {
    /* request {cmd_n:u8, report_bits:u8} (§13.3). */
    if (len < 2) {
        respond_status(2);
        return;
    }
    uint8_t cmd_n = payload[0];
    uint8_t report_bits = payload[1];

    /* Take the lease (verbs holder, assert select). One holder at a time (§5.2).
     * If a capture holds the lease, this fails — the host shouldn't issue a
     * command txn mid-capture (it would deadlock the held lease, §5.2). */
    if (!arb_grant(ARB_HOLDER_VERBS, cmd_teardown, NULL, /*select_active=*/true)) {
        respond_status(3); /* busy */
        return;
    }

    /* Emit the command verbatim. */
    qic_pulses(cmd_n);

    /* Optionally clock the report off TRK0, device-side (§5.3). */
    qic_report_t rep;
    rep.ack = false;
    rep.final_ok = false;
    rep.bits = 0;
    rep.nbits = 0;
    rep.timed_out = false;
    if (report_bits > 0) {
        qic_report(report_bits, &rep);
    }

    /* Release through the funnel (bounded op: normal completion). */
    arb_quiesce(ARB_REASON_NORMAL);

    /* response {ack:bit, bits:u16, final:bit} packed as: flags:u8, bits:u16.
     * flags bit0 = ack, bit1 = final_ok, bit2 = timed_out. */
    uint8_t resp[4];
    size_t n = 0;
    uint8_t flags = 0;
    if (rep.ack)       flags |= 0x01;
    if (rep.final_ok)  flags |= 0x02;
    if (rep.timed_out) flags |= 0x04;
    resp[n++] = flags;
    n += wr_u16(resp + n, rep.bits);
    resp[n++] = rep.nbits;
    respond(resp, n);
}

/* -------------------------- WAIT_READY -------------------------- */

static void handle_wait_ready(const uint8_t *payload, size_t len) {
    /* request {timeout_s:u16} (§13.3). */
    if (len < 2) {
        respond_status(2);
        return;
    }
    uint16_t timeout_s = rd_u16(payload);

    /* wait-ready needs the bus (it polls INDEX). Take the lease briefly. */
    if (!arb_grant(ARB_HOLDER_VERBS, cmd_teardown, NULL, /*select_active=*/true)) {
        respond_status(3);
        return;
    }
    bool ready = qic_wait_ready((uint32_t)timeout_s * 1000u);
    arb_quiesce(ARB_REASON_NORMAL);

    /* response {status:u8}: 0 = ready, 1 = timed out. */
    respond_status(ready ? 0u : 1u);
}

/* ---------------------------- CAPTURE --------------------------- */

/*
 * Capture teardown wrapper. Registered as the arbiter teardown hook for the
 * capture lease. CRITICAL ORDERING (§5.2 I5 — stop first, always):
 *   1. issue STOP_TAPE via the verbs engine, INSIDE the still-held lease, so the
 *      tape is stopped BEFORE the lease is dropped (a dropped lease with tape
 *      rolling risks spooling into EOT/BOT — the broken-tape hazard);
 *   2. then cleanly terminate the flux side (cap_disarm: flush + END).
 * This runs for EVERY capture exit (normal/abort/overflow/USB-loss/watchdog)
 * because they all funnel through arb_quiesce() (§5.2).
 *
 * On USB loss the STOP still goes out on the bus (it's a pin operation, host-
 * independent); only the END marker can't reach the vanished host, so the run
 * is flagged truncated but the tape is safely stopped.
 */
static void capture_teardown(arb_reason_t reason, void *ctx) {
    (void)ctx;

    /* 1. Stop the tape first. The flux engine has been holding the lease; here
     * the bus is handed flux->verbs (the front-end is halted inside cap_disarm,
     * but we issue STOP before that so motion ceases ASAP). */
    qic_pulses(QIC_CMD_STOP_TAPE);

    /* 2. Terminate the flux stream: halt front-end, flush, emit END(reason). */
    cap_teardown(reason, NULL);

    g_cap_session.active = false;
}

static void handle_capture(const uint8_t *payload, size_t len) {
    /* request {motion_n:u8, stop:StopCond} (§13.3). StopCond carries the byte
     * budget / max-duration the watchdog enforces; plus run-tagging fields the
     * host stamps into SESSION_START. Layout (LE):
     *   motion_n:u8, rate:u16, tpt:u16, direction:u8, pass_id:u16,
     *   byte_budget:u32  (=> 12 bytes). */
    if (len < 12) {
        respond_status(2);
        return;
    }
    uint8_t motion_n   = payload[0];
    uint16_t rate      = rd_u16(payload + 1);
    uint16_t tpt       = rd_u16(payload + 3);
    uint8_t direction  = payload[5];
    uint16_t pass_id   = rd_u16(payload + 6);
    uint32_t byte_budget = rd_u32(payload + 8);
    (void)byte_budget; /* TODO(bench): feed into a watchdog byte-budget check */

    /* Take the lease for the WHOLE session (flux holder). The teardown hook is
     * capture_teardown (stop-then-flush). Select asserted for the session. */
    if (!arb_grant(ARB_HOLDER_FLUX, capture_teardown, NULL, /*select_active=*/true)) {
        respond_status(3); /* busy */
        return;
    }

    /* Issue motion via the verbs engine (verbatim). For Logical Forward we do
     * NOT wait-ready/status afterward — a status report can swallow the first
     * segment (§2.1, §5.2/§6.3). We arm capture immediately. */
    qic_pulses(motion_n);

    /* Arm the free-running flux engine; SESSION_START is emitted here. */
    g_cap_session.params.rate = rate;
    g_cap_session.params.tpt = tpt;
    g_cap_session.params.direction = direction;
    g_cap_session.params.pass_id = pass_id;

    if (!cap_arm(&g_cap_session.params)) {
        /* arm failed: tear down cleanly through the funnel. */
        arb_quiesce(ARB_REASON_ERROR);
        respond_status(4);
        return;
    }

    g_cap_session.active = true;

    /* Note motion started for the host. */
    mk_event(gw_usb_get(), TW_EVT_MOTION_STARTED);

    /* No discrete response: the CAPTURE transaction switches to a continuous
     * stream (§13.3). The main loop now drives txn_capture_service() until the
     * session quiesces (normal stop / abort / overflow / USB-loss). */
}

void txn_capture_service(void) {
    if (!g_cap_session.active) {
        return;
    }
    /* Pump returns false when the session has ended (arbiter quiesced via the
     * dead-man path) or when a normal stop is requested out-of-band. */
    if (!cap_pump()) {
        /* If the pump ended without the teardown having cleared the flag (e.g.
         * the arbiter quiesced and ran capture_teardown), make sure we don't
         * keep servicing a dead session. */
        if (arb_is_idle()) {
            g_cap_session.active = false;
        }
    }
}

/* --------------------------- dispatch -------------------------- */

void txn_init(void) {
    arb_init();
    qic_timing_t t;
    qic_timing_defaults(&t);
    qic_set_timing(&t);
    g_cap_session.active = false;
}

bool txn_dispatch(uint8_t opcode, const uint8_t *payload, size_t len) {
    switch (opcode) {
    case TW_TXN_INFO:
        handle_info();
        return true;
    case TW_TXN_SET_TIMING:
        handle_set_timing(payload, len);
        return true;
    case TW_TXN_SELECT:
        handle_select(payload, len);
        return true;
    case TW_TXN_COMMAND_TXN:
        handle_command_txn(payload, len);
        return true;
    case TW_TXN_WAIT_READY:
        handle_wait_ready(payload, len);
        return true;
    case TW_TXN_CAPTURE:
        handle_capture(payload, len);
        return true;
    case TW_TXN_ABORT:
        /* ABORT is documented as OUT-OF-BAND (handled in cap_pump via
         * gw_usb_poll_control, §5.2). If it nonetheless arrives through the
         * normal command path, honor it: route through the funnel. */
        arb_on_abort();
        return true;
    default:
        /* Not ours — let GW's own handler claim it (the two command sets coexist
         * so the board stays a dual citizen, §12.5). */
        return false;
    }
}
