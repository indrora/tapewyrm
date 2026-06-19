/*
 * markers.h — tape marker injection on the GW opcode-escape channel.
 *
 * DESIGN.md references: §5.4 (marker injector), §7.2 (marker opcodes),
 *                       §13.3 (capture stream), §13.4 (module manifest).
 *
 * Markers ride the GW opcode-escape channel (same mechanism GW uses for its
 * index opcode) so they can NEVER be misread as flux. The marker CODES
 * (TW_MARK_*) and the little-endian payload LAYOUTS below are the firmware<->host
 * CONTRACT — they must stay byte-for-byte identical to what the host parses in
 * host/tapewyrm/rawflux/container.py. Only the opcode-escape *framing* (the
 * escape byte + stuffing) is bench-dependent (§13.6 item 1).
 *
 * Payload layouts (little-endian) — keep in sync with container.py:
 *   SESSION_START : rate:u16, clock:u32, tpt:u16, direction:u8, pass_id:u16   (11 bytes)
 *   SEGMENT       : ticks:u32, index:u32                                       (8 bytes)
 *   EVENT         : code:u8                                                     (1 byte)
 *   END           : reason:u8, flux_count:u32, byte_count:u32, checksum:u32     (13 bytes)
 *   HEARTBEAT     : (empty)
 */
#ifndef TAPEWYRM_MARKERS_H
#define TAPEWYRM_MARKERS_H

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

#include "protocol.h" /* TW_MARK_*, TW_EVT_*, TW_END_* */
#include "gw_stubs.h"

#ifdef __cplusplus
extern "C" {
#endif

/*
 * Running accounting for a capture run. mk_* update this so the END marker can
 * carry honest counts (§5.4 termination/accounting). The checksum is the
 * additive sum of *flux data bytes only* (NOT marker bytes), masked to 32 bits
 * — this is the exact rule host container.py verify() checks.
 */
typedef struct {
    uint32_t flux_count;  /* number of flux transitions (intervals) encoded   */
    uint32_t byte_count;  /* number of flux DATA bytes emitted (markers excl.) */
    uint32_t checksum;    /* additive sum of flux data bytes & 0xFFFFFFFF      */
} mk_accounting_t;

/* Zero the accounting at the start of a capture run. */
void mk_reset(mk_accounting_t *acc);

/*
 * Account for `n` freshly-emitted flux DATA bytes (call from the encoder path,
 * AFTER they are written into the stream, passing the actual bytes so the
 * additive checksum matches the host). `bytes` may be NULL only if n==0.
 */
void mk_account_flux(mk_accounting_t *acc, const uint8_t *bytes, size_t n);

/* Account for one captured flux transition (interval). */
void mk_account_flux_transition(mk_accounting_t *acc);

/* ---- marker emitters: write one opcode-escape-framed marker to USB ---- */

/* SESSION_START — at arm; anchors t=0 and tags the run's place in the stack. */
void mk_session_start(gw_usb_ctx_t *usb,
                      uint16_t rate, uint32_t clock,
                      uint16_t tpt, uint8_t direction, uint16_t pass_id);

/* SEGMENT — one per hardware INDEX edge (from cap_on_index). `ticks` is the
 * sample-clock delta since the previous segment; `index` is the running count. */
void mk_segment(gw_usb_ctx_t *usb, uint32_t ticks, uint32_t index);

/* EVENT — an observed bus/drive event (TW_EVT_*). */
void mk_event(gw_usb_ctx_t *usb, uint8_t code);

/* END — at disarm; seals the run with reason (TW_END_*) + counts + checksum
 * taken from `acc`. A run with no valid END is flagged truncated by the host. */
void mk_end(gw_usb_ctx_t *usb, uint8_t reason, const mk_accounting_t *acc);

/* HEARTBEAT — coarse keepalive across long erased stretches (demoted §7.2). */
void mk_heartbeat(gw_usb_ctx_t *usb);

#ifdef __cplusplus
}
#endif

#endif /* TAPEWYRM_MARKERS_H */
