/*
 * markers.c — tape marker injection on the GW opcode-escape channel (§5.4, §7.2).
 *
 * The marker CODES and payload LAYOUTS are the firmware<->host contract; see
 * markers.h and host/tapewyrm/rawflux/container.py. The framing implemented here
 * mirrors the host model:
 *
 *     <escape> <marker_code> <len:u8> <payload...>
 *
 * where <escape> is gw_flux_opcode_escape() (host models it as 0xFF). A literal
 * flux byte equal to the escape is stuffed by the *flux encoder* path, not here
 * — markers.c only ever emits the escape as a deliberate opcode introducer.
 *
 * TODO(bench) §13.6 item 1: GW's real opcode-escape scheme may use a different
 * introducer byte and/or a multi-byte opcode form. When the GW firmware is read,
 * replace mk_frame_header() to match, and update container.py in lockstep. The
 * marker codes/payloads must NOT change.
 */

#include "markers.h"

/* Little-endian serialization helpers (host parses with struct '<...'). */
static size_t put_u8(uint8_t *p, uint8_t v) { p[0] = v; return 1; }

static size_t put_u16(uint8_t *p, uint16_t v) {
    p[0] = (uint8_t)(v & 0xFF);
    p[1] = (uint8_t)((v >> 8) & 0xFF);
    return 2;
}

static size_t put_u32(uint8_t *p, uint32_t v) {
    p[0] = (uint8_t)(v & 0xFF);
    p[1] = (uint8_t)((v >> 8) & 0xFF);
    p[2] = (uint8_t)((v >> 16) & 0xFF);
    p[3] = (uint8_t)((v >> 24) & 0xFF);
    return 4;
}

/*
 * Emit a fully-framed marker to USB. The whole frame is pushed in one
 * gw_usb_write() so a marker is never split across a backpressure boundary
 * (markers must arrive atomically to stay parseable). If the TX path cannot
 * accept the whole frame the marker is dropped at this layer — marker loss is
 * acceptable (the host tolerates missing SEGMENT/HEARTBEAT markers and flags a
 * missing END as truncated), whereas flux loss is not (§5.4). The flux engine's
 * separate near-overflow check is what protects the flux DATA stream.
 *
 * Max frame = escape(1) + code(1) + len(1) + payload(<=13) = 16 bytes.
 */
#define MK_MAX_PAYLOAD 13u
#define MK_MAX_FRAME (3u + MK_MAX_PAYLOAD)

static void mk_emit(gw_usb_ctx_t *usb, uint8_t code,
                    const uint8_t *payload, uint8_t plen) {
    uint8_t frame[MK_MAX_FRAME];
    size_t n = 0;

    if (plen > MK_MAX_PAYLOAD) {
        return; /* programmer error; never silently truncate a payload */
    }

    frame[n++] = gw_flux_opcode_escape(); /* TODO(bench): GW escape byte */
    frame[n++] = code;                    /* TW_MARK_* */
    frame[n++] = plen;
    for (uint8_t i = 0; i < plen; i++) {
        frame[n++] = payload[i];
    }

    /* Best-effort atomic push; drop on backpressure (see comment above). */
    (void)gw_usb_write(usb, frame, n);
}

/* ----------------------------- accounting ----------------------------- */

void mk_reset(mk_accounting_t *acc) {
    if (acc == NULL) {
        return;
    }
    acc->flux_count = 0;
    acc->byte_count = 0;
    acc->checksum = 0;
}

void mk_account_flux(mk_accounting_t *acc, const uint8_t *bytes, size_t n) {
    if (acc == NULL || n == 0) {
        return;
    }
    acc->byte_count += (uint32_t)n;
    /* Additive checksum over flux DATA bytes, masked to 32 bits — matches
     * host flux_checksum(): sum(data) & 0xFFFFFFFF. */
    for (size_t i = 0; i < n; i++) {
        acc->checksum = (acc->checksum + bytes[i]) & 0xFFFFFFFFu;
    }
}

void mk_account_flux_transition(mk_accounting_t *acc) {
    if (acc == NULL) {
        return;
    }
    acc->flux_count++;
}

/* ----------------------------- emitters ------------------------------- */

void mk_session_start(gw_usb_ctx_t *usb,
                      uint16_t rate, uint32_t clock,
                      uint16_t tpt, uint8_t direction, uint16_t pass_id) {
    /* layout: rate:u16, clock:u32, tpt:u16, direction:u8, pass_id:u16 (11) */
    uint8_t p[11];
    size_t n = 0;
    n += put_u16(p + n, rate);
    n += put_u32(p + n, clock);
    n += put_u16(p + n, tpt);
    n += put_u8(p + n, direction);
    n += put_u16(p + n, pass_id);
    mk_emit(usb, TW_MARK_SESSION_START, p, (uint8_t)n);
}

void mk_segment(gw_usb_ctx_t *usb, uint32_t ticks, uint32_t index) {
    /* layout: ticks:u32, index:u32 (8) */
    uint8_t p[8];
    size_t n = 0;
    n += put_u32(p + n, ticks);
    n += put_u32(p + n, index);
    mk_emit(usb, TW_MARK_SEGMENT, p, (uint8_t)n);
}

void mk_event(gw_usb_ctx_t *usb, uint8_t code) {
    /* layout: code:u8 (1) */
    uint8_t p[1];
    size_t n = put_u8(p, code);
    mk_emit(usb, TW_MARK_EVENT, p, (uint8_t)n);
}

void mk_end(gw_usb_ctx_t *usb, uint8_t reason, const mk_accounting_t *acc) {
    /* layout: reason:u8, flux_count:u32, byte_count:u32, checksum:u32 (13) */
    uint8_t p[13];
    size_t n = 0;
    n += put_u8(p + n, reason);
    n += put_u32(p + n, acc ? acc->flux_count : 0);
    n += put_u32(p + n, acc ? acc->byte_count : 0);
    n += put_u32(p + n, acc ? acc->checksum : 0);
    mk_emit(usb, TW_MARK_END, p, (uint8_t)n);
}

void mk_heartbeat(gw_usb_ctx_t *usb) {
    mk_emit(usb, TW_MARK_HEARTBEAT, NULL, 0);
}
