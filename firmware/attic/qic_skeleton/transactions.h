/*
 * transactions.h — USB transaction dispatch (DESIGN.md §5.1, §13.3, §13.4).
 *
 * The host's only door to the device. Hooks into GW's command handler and
 * routes each Tapewyrm transaction (TW_TXN_*) to the arbiter + verbs + flux
 * modules. The host never touches a pin; it issues atomic, transaction-oriented
 * requests (§5.1) and the firmware owns the bus.
 *
 * Wire protocol (§13.3): host->device request frames {opcode:u8, len:u16,
 * payload}; device->host response frames likewise; CAPTURE switches to a
 * continuous stream. ABORT/STOP is OUT-OF-BAND (not a queued txn) — it arrives
 * via the control channel and is handled inside cap_pump() (§5.2), not here.
 */
#ifndef TAPEWYRM_TRANSACTIONS_H
#define TAPEWYRM_TRANSACTIONS_H

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Initialize the transaction layer (arbiter + default timing). Call once at
 * firmware start after gw_stubs are bound. */
void txn_init(void);

/*
 * Dispatch one host->device transaction. `opcode` is a TW_TXN_* code; `payload`
 * / `len` are the request body. Responses are written back via the GW USB TX
 * path (gw_usb_write); CAPTURE takes over the stream until the session ends.
 *
 * Returns true if the opcode was recognized and handled (a malformed payload is
 * still "handled" — it returns an error response), false for an unknown opcode
 * (so GW's own handler can claim it — the two command sets coexist, §12.5).
 *
 * TODO(bench) §13.6 item 2: the exact hook into GW's command handler (is this
 * called from a dispatch switch? a registered callback?) is a GW-integration
 * unknown. The shape here — "claim known opcodes, defer the rest" — is the
 * intended posture; wire it to GW's real handler at vendoring time.
 */
bool txn_dispatch(uint8_t opcode, const uint8_t *payload, size_t len);

/*
 * Run the capture pump to completion. Called by the firmware main loop while a
 * capture session holds the lease (the CAPTURE transaction returns after arming
 * and the main loop drives this until the session quiesces). Exposed so the
 * integration layer can interleave it with GW's loop. No-op if no capture is
 * active.
 */
void txn_capture_service(void);

#ifdef __cplusplus
}
#endif

#endif /* TAPEWYRM_TRANSACTIONS_H */
