/*
 * gw_stubs.h — Greaseweazle primitive seam for the Tapewyrm QIC layer.
 *
 * DESIGN.md references: §3 (hardware), §4 (architecture), §5 (firmware design),
 *                       §12.5 (vendor seam), §13.4 (module manifest), §13.6 (bench seams).
 *
 * PURPOSE
 * -------
 * The Tapewyrm QIC firmware (arbiter / verbs / flux_capture / markers /
 * transactions) is a *hard fork* layered ABOVE the Greaseweazle (GW) v4.1
 * application entry point (AT32F403, Cortex-M4). It reuses GW's flux timer/DMA,
 * USB CDC transport, GPIO/clock, and bootloader **untouched** (§12.5). Those
 * primitives are NOT vendored into this repo yet, so this header declares every
 * GW symbol the QIC layer leans on as an `extern` stub with an opaque handle
 * typedef, so the skeleton compiles standalone.
 *
 * Every declaration below is a BENCH SEAM. At vendoring time, bind each one to
 * the real GW function/file named in its `TODO(bench)` comment, then DELETE the
 * stub (or wrap it in `#ifndef TW_USE_REAL_GW`). The signatures here are the
 * *contract the QIC layer expects*; the real GW names may differ slightly and
 * the adapter is where they reconcile.
 *
 * Naming convention: the bench-seam stubs use a `gw_*` prefix to make the
 * "reused GW primitive" boundary visible at every call site. None of the QIC
 * `.c` files touch a hardware register directly — they only call `gw_*`.
 *
 * TODO(bench) §13.6 item 2: the *exact* GW integration points (how the command
 * handler dispatches, how read-stop control maps onto ABORT) are the single
 * biggest unknown. Treat the function shapes below as provisional until the GW
 * firmware tree is read.
 */
#ifndef TAPEWYRM_GW_STUBS_H
#define TAPEWYRM_GW_STUBS_H

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ======================================================================== *
 *  Opaque handle typedefs
 *  ----------------------------------------------------------------------- *
 *  The QIC layer holds these by value but never dereferences them; the real
 *  GW types are concrete (timer instances, DMA descriptors, GPIO ports). At
 *  vendoring time, typedef these to the real GW structs.
 * ======================================================================== */

/* TODO(bench): bind to GW's flux input-capture timer context (GW: the TIM +
 * DMA setup used by the flux read path, e.g. `struct flux`/timer regs in
 * greaseweazle-firmware src/floppy.c / target flux code). */
typedef struct gw_flux_ctx gw_flux_ctx_t;

/* TODO(bench): bind to GW's USB CDC-ACM endpoint / stream context (GW:
 * src/usb/ + the bulk/CDC write path used by the flux streamer). */
typedef struct gw_usb_ctx gw_usb_ctx_t;

/* GPIO line identity. In real GW this resolves to a (port, pin) pair via the
 * board pin map. TODO(bench): replace this enum with GW's pin handle / the
 * v4.1 board definition (GW: src/board/ or the target's pins header). The
 * line set below is the QIC-117 subset of the 34-pin Shugart bus (§3, §2.1). */
typedef enum {
    GW_PIN_DRIVE_SELECT = 0, /* DSEL — owned solely by the arbiter (§5.2)        */
    GW_PIN_STEP,             /* STEP out — the QIC-117 command line (§2.1)       */
    GW_PIN_DIR,              /* DIR  out — direction (kept benign for QIC)       */
    GW_PIN_MOTOR,           /* MOTOR out — motor enable                          */
    GW_PIN_WGATE,           /* WGATE out — write gate (held inactive: read-only) */
    GW_PIN_TRK0,            /* TRK0 in  — the QIC-117 level-sensitive return     */
    GW_PIN_INDEX,           /* INDEX in — per-segment cue on tape (§2.2)         */
    GW_PIN_RDATA,           /* RDATA in — flux (handled by the capture timer)    */
    GW_PIN__COUNT
} gw_pin_t;

/* ======================================================================== *
 *  Timing / watchdog primitives
 * ======================================================================== */

/* Busy-wait for `us` microseconds. The QIC pulse cadence and report-bit windows
 * (§5.3) need sub-millisecond precision, so this must be a calibrated spin/timer
 * delay, NOT a scheduler sleep.
 * TODO(bench): bind to GW's microsecond delay (GW: `delay_us()` in src/util.c /
 * the SysTick or DWT cycle-counter helper). */
extern void gw_delay_us(uint32_t us);

/* Free-running monotonic microsecond clock, for timeouts and SEGMENT tick
 * deltas. Wraps at 2^32 us (~71 min) — callers use unsigned subtraction.
 * TODO(bench): bind to GW's running timer (GW: `time_now()`/`stk_now()` style
 * helper in src/time.c, or the same counter the flux timer samples). */
extern uint32_t gw_now_us(void);

/* The flux-capture sample clock in Hz (ticks per second of the input-capture
 * timer). Stamped into SESSION_START so the host can convert ticks->time.
 * TODO(bench): bind to GW's flux timer clock (GW: the TIM input clock the flux
 * read path reports in its INFO/flux-status). */
extern uint32_t gw_flux_sample_hz(void);

/* Kick the hardware watchdog so a wedged operation eventually resets the MCU
 * rather than leaving the tape running. Called from long QIC loops and the
 * capture pump (§5.2 dead-man).
 * TODO(bench): bind to GW's IWDG kick (GW: `iwdg` / watchdog reset in
 * src/main.c or target init). */
extern void gw_watchdog_kick(void);

/* ======================================================================== *
 *  GPIO primitives (drive-select, STEP out, TRK0/INDEX in)
 *  ----------------------------------------------------------------------- *
 *  §3 verify item: confirm TRK0/INDEX land on pollable GPIO/EXTI on v4.1 and
 *  that the STEP output buffer swings the bus at the chosen cadence. The QIC
 *  layer assumes 34-pin OPEN-COLLECTOR semantics (§2, §3, §13.6 item 4):
 *  asserting a line pulls it LOW; "level" here is the LOGICAL active level, and
 *  the polarity inversion lives inside these stubs, not in the QIC code.
 * ======================================================================== */

/* Drive a digital output line to a logical level (true = asserted/active).
 * TODO(bench): bind to GW's pin write — the same primitive behind `gw pin set`
 * (GW: `gpio_write_pin()` in src/stm32/gpio.c or target GPIO, via the board pin
 * map). Confirm open-collector polarity here (§13.6 item 4). */
extern void gw_pin_write(gw_pin_t pin, bool active);

/* Read a digital input line, returning its logical level (true = asserted).
 * Used to sample TRK0 (report bits) and poll INDEX/ready.
 * TODO(bench): bind to GW's pin read — the primitive behind `gw pin get`
 * (GW: `gpio_read_pin()` in src/stm32/gpio.c). */
extern bool gw_pin_read(gw_pin_t pin);

/* Configure an input line for edge interrupts (the INDEX EXTI feeding
 * cap_on_index). Pass NULL to disarm. The ISR runs in interrupt context and
 * must be ISR-safe.
 * TODO(bench): bind to GW's EXTI setup (GW: src/stm32/exti.c or the index-pulse
 * handling in the flux read path; on tape INDEX fires per *segment*, §2.2/§5.4). */
extern void gw_pin_irq_set(gw_pin_t pin, void (*handler)(void));

/* ======================================================================== *
 *  Flux capture front-end (timer input-capture + DMA) — reused from GW
 *  ----------------------------------------------------------------------- *
 *  KEY DIFFERENCE from stock GW (§5.4): Tapewyrm runs the capture FREE-RUNNING
 *  — armed/disarmed on command, NOT gated to one index-to-index revolution.
 *  TODO(bench) §13.6 item 2: determine whether this is a new command or a flag
 *  on GW's existing flux read, and how arm/disarm maps onto GW's read-stop.
 * ======================================================================== */

/* Allocate / get the singleton flux-capture context. */
extern gw_flux_ctx_t *gw_flux_get(void);

/* Arm the input-capture timer + DMA to measure inter-transition intervals on
 * RDATA in sample-clock ticks, free-running. After this, drained intervals
 * appear via gw_flux_read_intervals().
 * TODO(bench): bind to GW's flux-read arm (GW: the TIM/DMA start in the flux
 * read command handler). */
extern void gw_flux_arm(gw_flux_ctx_t *ctx);

/* Disarm the front-end: stop the timer/DMA. Safe to call when already disarmed.
 * TODO(bench): bind to GW's flux-read stop (GW: read-stop control path). */
extern void gw_flux_disarm(gw_flux_ctx_t *ctx);

/* Drain up to `max` newly-measured inter-transition intervals (sample-clock
 * ticks) into `out`. Returns the count drained (0 if none ready). Non-blocking.
 * TODO(bench): bind to GW's DMA-ring drain (GW: the consumer side of the flux
 * input-capture DMA buffer). */
extern size_t gw_flux_read_intervals(gw_flux_ctx_t *ctx, uint32_t *out, size_t max);

/* ----------------------------------------------------------------------- *
 *  GW flux byte ENCODER (the single most important bench seam)
 *  TODO(bench) §13.6 item 1: the exact GW flux byte encoding — short intervals
 *  as direct bytes, the long-flux CONTINUATION escape, and the opcode/escape
 *  channel values — must be read from greaseweazle-firmware (the flux byte
 *  emit in its flux read path) before this runs. We stub the encoder so the
 *  pipeline structure is exercised; the byte values it emits are placeholders.
 * ----------------------------------------------------------------------- */

/* Encode one inter-transition interval (in sample-clock ticks) into the GW
 * on-wire flux byte form, appending to `out` (capacity `cap`). Returns bytes
 * written, or 0 if it would overflow `out`. Must handle the long-flux
 * continuation escape so tape dropouts / inter-record gaps are not saturated.
 * TODO(bench) §13.6 item 1: replace with GW's real flux byte emitter. */
extern size_t gw_flux_encode_interval(uint32_t ticks, uint8_t *out, size_t cap);

/* Emit the GW opcode-escape prefix byte that introduces an out-of-band opcode
 * in the flux stream (the channel markers ride on, §5.4/§7.2). markers.c uses
 * this so a marker can never be misread as flux.
 * TODO(bench) §13.6 item 1: must equal GW's real escape byte. The host mirror
 * (host/tapewyrm/rawflux/container.py) currently models this as 0xFF; keep the
 * two in lockstep so fixtures round-trip. */
extern uint8_t gw_flux_opcode_escape(void);

/* ======================================================================== *
 *  USB CDC stream (the real-time boundary, §4)
 *  ----------------------------------------------------------------------- *
 *  The flux streamer and the transaction layer both write here. On tape there
 *  is no deadline (we record, not feed an FDC), but sustained host stall must
 *  surface as backpressure so the flux engine can trip overflow->abort (§5.4).
 * ======================================================================== */

extern gw_usb_ctx_t *gw_usb_get(void);

/* Append `len` bytes to the device->host stream. Returns bytes accepted; a
 * short write (< len) signals backpressure (TX buffer near full). Never blocks
 * the caller indefinitely.
 * TODO(bench): bind to GW's USB bulk/CDC write (GW: src/usb/ TX path used by
 * the flux streamer). */
extern size_t gw_usb_write(gw_usb_ctx_t *ctx, const uint8_t *buf, size_t len);

/* Free space (bytes) currently available in the USB TX path. Used by the
 * backpressure / near-overflow check (§5.4).
 * TODO(bench): bind to GW's USB TX free-space query. */
extern size_t gw_usb_tx_free(gw_usb_ctx_t *ctx);

/* True while the USB link is up and configured. The capture dead-man polls
 * this; a transition to false triggers the USB-loss Quiesce (§5.2).
 * TODO(bench): bind to GW's USB configured/suspend state (GW: the device state
 * the CDC stack exposes; suspend & disconnect both count as "lost"). */
extern bool gw_usb_connected(gw_usb_ctx_t *ctx);

/* Pull the next pending out-of-band CONTROL request, if any (e.g. ABORT during
 * a held capture — §5.1/§5.2/§13.3: ABORT is NOT a queued txn). Returns true
 * and sets *opcode if one is pending. Polled inside the capture pump.
 * TODO(bench) §13.6 item 2: bind to whatever GW control channel carries an
 * out-of-band stop (GW: the read-stop / abort control the host tools send). */
extern bool gw_usb_poll_control(gw_usb_ctx_t *ctx, uint8_t *opcode);

#ifdef __cplusplus
}
#endif

#endif /* TAPEWYRM_GW_STUBS_H */
