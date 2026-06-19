/*
 * qic/qic.c
 *
 * Tapewyrm QIC-80 graft, #included INTO src/floppy.c.
 *
 * This is free and unencumbered software released into the public domain.
 * See the file COPYING for more details, or visit <http://unlicense.org>.
 *
 * ------------------------------------------------------------------------
 * WHAT THIS IS
 * ------------------------------------------------------------------------
 * Tapewyrm reuses Greaseweazle's control loop and its (now vestigial, for our
 * purposes) MFM flux-recording engine to read QIC-80 floppy-tape cartridges.
 * Rather than build a parallel application behind an abstraction layer, the QIC
 * verbs and the tape capture are GRAFTED directly onto GW's machinery:
 *
 *   - The QIC command/status channel (QIC-117) is emitted by re-using GW's own
 *     STEP-pulse primitives: write_pin(step,..), the dir/step machinery, the
 *     TRK0/INDEX reads (get_trk0()/get_index()), delay_us(), time_now().
 *   - The tape flux capture re-uses GW's flux read engine WHOLESALE: the same
 *     tim_rdata/dma_rdata input-capture front-end, the same rdata_encode_flux()
 *     byte encoder, the same ST_read_flux state machine and u_buf[] USB pump.
 *     A free-running tape capture is, structurally, a GW flux read with no index
 *     limit and no tick deadline -- so we drive floppy_read_prep() with both
 *     limits cleared and let GW's loop stream until the host stops it.
 *
 * Because GW's flux engine and pin macros are `static` inside floppy.c, this
 * file is #included into floppy.c (see the include site near process_command())
 * so it can see those statics directly. There is no gw_stubs.h abstraction any
 * more -- everything binds to the REAL GW primitives.
 *
 * DESIGN.md references: §4 (architecture), §5.3 (verbs), §5.4 (capture),
 * §7.2 (markers), §13.1 (command table), §13.3 (wire protocol).
 *
 * TODO(bench) markers below flag the genuinely hardware-timing-faithful pieces
 * that must be confirmed against a real drive + scope (DESIGN.md §13.6):
 * the exact QIC-117 pulse envelope, the free-running-capture DMA edge behaviour,
 * and the INDEX-per-segment handling.
 */

#include "protocol.h"

/* ======================================================================== *
 *  QIC command numbers (grafted onto GW's process_command() switch)
 *  ----------------------------------------------------------------------- *
 *  GW's own commands occupy 0..CMD_MAX (== 22). We do NOT preserve GW's wire
 *  protocol for external compatibility, so collisions don't matter -- but for
 *  clarity (and so a stock `gw` host simply gets ACK_BAD_COMMAND) we place the
 *  QIC verbs in a high, clearly-separate range. These mirror the host-side
 *  TW_TXN_* transaction opcodes (inc/protocol.h) but are issued as ordinary GW
 *  command packets {cmd, len, payload...}.
 * ======================================================================== */
/* Sourced from the generated contract (inc/protocol.h) so the host and firmware
 * can never drift (DESIGN.md §12.4). The host calls the "emit N pulses + report"
 * verb COMMAND_TXN; in the firmware it is the STEP-pulse engine. */
#define CMD_QIC_INFO        TW_TXN_INFO
#define CMD_QIC_SET_TIMING  TW_TXN_SET_TIMING
#define CMD_QIC_PULSES      TW_TXN_COMMAND_TXN
#define CMD_QIC_WAIT_READY  TW_TXN_WAIT_READY
#define CMD_QIC_CAPTURE     TW_TXN_CAPTURE

/* ======================================================================== *
 *  QIC-117 timing envelope (DESIGN.md §2.1 Table 1, §5.3)
 *  ----------------------------------------------------------------------- *
 *  Configured by CMD_QIC_SET_TIMING from the host DriveProfile; defaults track
 *  QIC-117 Rev J Table 1. All values microseconds unless noted.
 * ======================================================================== */
struct qic_timing {
    uint16_t pulse_us;          /* STEP asserted width                        */
    uint16_t inter_pulse_us;    /* gap between pulses within a train (~step interval) */
    uint16_t terminate_gap_us;  /* gap that ENDS a command train (> ~2.9 ms)  */
    uint16_t tack_us;           /* max wait for the ACK bit (~2.5 ms)         */
    uint16_t tbit_us;           /* max wait per report data bit (~900 us)     */
    uint8_t  report_on_index;   /* 1 = wait on INDEX edge per bit; 0 = settle */
};

static struct qic_timing qic_timing = {
    /* QIC-117 Rev J Table 1 nominal envelope.
     * TODO(bench) §13.6 item 3: the real per-drive pulse cadence + report
     * strategy (INDEX-edge vs fixed-settle) come from scoping the target drive
     * and arrive via CMD_QIC_SET_TIMING; these are only the safe defaults. */
    .pulse_us         = 5,      /* short, clean STEP pulse                    */
    .inter_pulse_us   = 2000,   /* ~2.0 ms step interval (0.9-2.1 ms)         */
    .terminate_gap_us = 3000,   /* > 2.9 ms ends the command train            */
    .tack_us          = 2500,   /* TACK ~2.5 ms for the ACK bit               */
    .tbit_us          = 900,    /* TBIT ~900 us per report data bit           */
    .report_on_index  = 0,      /* default fixed-settle; bench may flip to 1  */
};

/* REPORT_NEXT_BIT is QIC-117 command 2 == 2 STEP pulses (§2.1, §13.1). This is
 * the one place the verbs engine is not purely verbatim: the host asks for
 * report_bits, and the engine knows REPORT_NEXT_BIT advances one bit. */
#define QIC_CMD_REPORT_NEXT_BIT_PULSES 2u

/* QIC-117 motion command numbers the firmware must know BY NAME so the safe-stop
 * path can halt the tape itself on a dead-man trigger (host gone). Everything
 * else is verbatim from the host (§4.1 decision 2). */
#define QIC_CMD_STOP_TAPE 18u   /* command 18 = 18 STEP pulses (§13.1)        */

/* ======================================================================== *
 *  QIC verbs engine (command/status channel, §5.3) -- bound to GW primitives
 *  ----------------------------------------------------------------------- *
 *  STEP is driven through GW's write_pin(step,..). On GW the floppy output pins
 *  are active-low (O_TRUE==0); write_pin() takes the LOGICAL level and the
 *  polarity inversion lives in GW's GPO_bus/O_TRUE macros, so TRUE == asserted.
 *  TRK0/INDEX are read through GW's get_trk0()/get_index() (LOW == asserted on
 *  the open-collector bus; these return the raw pin level -- see note below).
 *
 *  TODO(bench) §13.6 item 4: confirm the 34-pin open-collector polarity end to
 *  end -- that asserting STEP via write_pin(step,TRUE) pulls the bus low at the
 *  drive, and that get_trk0()/get_index() LOW corresponds to the QIC "active"
 *  report/cue level. The host bit_order/report_strategy in the DriveProfile is
 *  the other half of this and is tuned on the bench.
 * ======================================================================== */

/* Emit exactly one STEP pulse, then idle for `gap_us`.
 * Reuses GW's write_pin(step,..) + delay_us() directly. */
static void qic_step_pulse(uint16_t gap_us)
{
    write_pin(step, TRUE);              /* assert STEP (active level)          */
    delay_us(qic_timing.pulse_us);
    write_pin(step, FALSE);             /* release STEP                        */
    delay_us(gap_us);
}

/* Emit `n` STEP pulses at the configured cadence, verbatim, then hold the
 * terminating gap (§5.3). Intra-train gaps stay under the command time-out so
 * an isolated slow pulse can't read as Soft Reset (§2.1 hazard); the final gap
 * is the terminating gap that ends the command train. n==0 emits nothing. */
static void qic_pulses(uint8_t n)
{
    unsigned int i;
    for (i = 0; i < n; i++) {
        bool_t last = (i + 1u == n);
        qic_step_pulse(last ? qic_timing.terminate_gap_us
                            : qic_timing.inter_pulse_us);
        /* Long trains (n up to ~32) at ~2 ms each are slow; keep the watchdog
         * happy and defer its deadline while we make progress. */
        watchdog_kick();
    }
    /* Defensive: never leave STEP stuck asserted between commands (§5.3). */
    write_pin(step, FALSE);
}

/* Poll TRK0 for `want` within `window_us`. Returns TRUE if observed in time. */
static bool_t qic_wait_trk0(bool_t want, uint32_t window_us)
{
    time_t start = time_now();
    for (;;) {
        /* GW get_trk0() returns the raw pin level; QIC "active" on the
         * open-collector bus is LOW. We compare against the asserted level so
         * callers reason in logical terms. TODO(bench) §13.6 item 4. */
        bool_t active = (get_trk0() == LOW);
        if (active == want)
            return TRUE;
        if (time_since(start) >= (int32_t)time_us(window_us))
            return FALSE;
        watchdog_kick();
    }
}

/* Sample one report data bit off TRK0 (LSB-first across the caller's loop).
 * With report_on_index, wait for the INDEX cue that a bit is ready (bounded by
 * TBIT); otherwise a fixed settle within TBIT (the value is latched at command
 * receipt, so loop jitter is harmless -- §2.1/§5.3). Sets *timed_out. */
static bool_t qic_sample_report_bit(bool_t *timed_out)
{
    *timed_out = FALSE;
    if (qic_timing.report_on_index) {
        time_t start = time_now();
        while (get_index() != LOW) { /* INDEX active == LOW (open-collector) */
            if (time_since(start) >= (int32_t)time_us(qic_timing.tbit_us)) {
                *timed_out = TRUE;
                break;
            }
        }
    } else {
        delay_us(qic_timing.tbit_us);
    }
    return (get_trk0() == LOW); /* TRK0 active == LOW carries the bit */
}

/* Result of a report transaction (§5.3). */
struct qic_report {
    bool_t ack;        /* first bit: TRUE = ok, FALSE = reset/hw failure       */
    bool_t final_ok;   /* last bit: TRUE = ok, FALSE = error mid-report        */
    uint16_t bits;     /* up to 16 data bits, LSB-first                        */
    uint8_t nbits;     /* how many data bits were clocked                      */
    bool_t timed_out;  /* a TACK/TBIT window expired                           */
};

/* Run a report transaction: the report command has ALREADY been emitted by the
 * caller (via qic_pulses). Read ACK within TACK, then for each of `report_bits`
 * data bits emit REPORT_NEXT_BIT and sample TRK0 within TBIT (LSB-first), then
 * read the Final bit. The whole loop is device-side -> immune to USB jitter. */
static void qic_report(uint8_t report_bits, struct qic_report *out)
{
    unsigned int i;
    bool_t timed_out;

    out->ack = FALSE;
    out->final_ok = FALSE;
    out->bits = 0;
    out->nbits = 0;
    out->timed_out = FALSE;

    if (report_bits > 16)
        report_bits = 16; /* Report Error Code is the widest at 2 bytes */

    /* ACK bit: must appear TRUE within TACK; FALSE => reset/hardware failure. */
    if (!qic_wait_trk0(TRUE, qic_timing.tack_us)) {
        out->timed_out = TRUE;
        return;
    }
    out->ack = TRUE;

    for (i = 0; i < report_bits; i++) {
        bool_t bit;
        qic_pulses(QIC_CMD_REPORT_NEXT_BIT_PULSES);
        bit = qic_sample_report_bit(&timed_out);
        if (timed_out) {
            out->timed_out = TRUE;
            out->nbits = i;
            return;
        }
        if (bit)
            out->bits |= (uint16_t)(1u << i); /* LSB-first */
        out->nbits = (uint8_t)(i + 1u);
        watchdog_kick();
    }

    /* Final bit: clock once more and sample; FALSE => error occurred. */
    qic_pulses(QIC_CMD_REPORT_NEXT_BIT_PULSES);
    {
        bool_t fin = qic_sample_report_bit(&timed_out);
        out->final_ok = timed_out ? FALSE : fin;
        out->timed_out = out->timed_out || timed_out;
    }
}

/* Poll the ready/INDEX line until ready or `timeout_ms` elapses. Motion ops run
 * seconds-to-minutes (§2.1) so the timeout is generous; the watchdog backstops a
 * wedged drive. INDEX active (LOW) is treated as the ready cue (§2.1).
 *
 * The deadline is counted in 1 ms chunks rather than as one giant tick deadline:
 * time_ms() of a multi-minute timeout (Logical Forward <=650 s, Seek Load Point
 * ~670 s, §13.1) overflows the 32-bit STK tick type, so a single
 * time_since(start) >= time_ms(timeout_ms) comparison would be wrong. Polling per
 * millisecond keeps each tick delta well inside int32 range.
 *
 * TODO(bench) §13.6 item 3: confirm the ready line + edge-vs-level strategy per
 * drive, and that the 1 ms poll granularity is fine enough not to miss a brief
 * ready cue (a level-sensitive INDEX makes that a non-issue; an edge cue may want
 * a faster inner poll). */
static bool_t qic_wait_ready(uint32_t timeout_ms)
{
    uint32_t ms;
    for (ms = 0; ms < timeout_ms; ms++) {
        time_t t0 = time_now();
        do {
            if (get_index() == LOW)
                return TRUE;
        } while (time_since(t0) < (int32_t)time_ms(1));
        watchdog_kick();
    }
    /* Final check so a 0 ms timeout still samples once. */
    return (get_index() == LOW);
}

/* ======================================================================== *
 *  Tape markers (DESIGN.md §5.4, §7.2) -- injected into GW's flux stream
 *  ----------------------------------------------------------------------- *
 *  Markers ride GW's opcode-escape channel: a 0xFF byte introduces an opcode in
 *  the flux stream (see rdata_encode_flux(): 0xFF FLUXOP_INDEX ..). GW's flux
 *  opcodes are 1..3; our marker codes are 0xF0..0xF4 (inc/protocol.h), so the
 *  two never collide. The host parser (host/tapewyrm/rawflux/container.py) reads
 *
 *      0xFF  <marker_code>  <len:u8>  <payload...>      (ESC == 0xFF)
 *
 *  and a literal 0xFF flux byte is stuffed as 0xFF 0xFF. GW never emits a raw
 *  0xFF flux byte (its single-byte flux values are 1..249, two-byte lead-ins are
 *  250..254, and 255 is the opcode escape), so no extra stuffing is needed for
 *  GW-produced flux. The serialization helpers below are byte-for-byte the same
 *  layout the host parses (little-endian) -- this is the firmware<->host contract.
 *
 *  Markers are written straight into GW's u_buf[] ring via the same u_prod
 *  cursor rdata_encode_flux() uses, so they interleave atomically with flux and
 *  are streamed out by the existing floppy_read() USB pump. Marker loss is
 *  acceptable (the host tolerates missing SEGMENT markers and flags a missing
 *  END as truncated); flux loss is not -- but writing into the same ring means a
 *  marker only fails to land if the whole read is already overflowing.
 * ======================================================================== */

/* Running accounting for the END marker (counts + checksum), matching the host
 * container.py verify(): byte_count over flux DATA bytes, checksum = additive
 * sum of those bytes & 0xFFFFFFFF, flux_count = number of intervals encoded. */
static struct {
    uint32_t flux_count;
    uint32_t byte_count;
    uint32_t checksum;
    bool_t   active;       /* a capture session is armed                      */
    uint16_t rate;         /* stamped into SESSION_START                      */
    uint16_t tpt;
    uint8_t  direction;
    uint16_t pass_id;
    unsigned int seg_index; /* running SEGMENT index                          */
    uint32_t byte_budget;  /* self-terminate after this many flux DATA bytes  */
                           /* (0 = unbounded: host stops the stream)          */
    bool_t need_session_start; /* emit SESSION_START on the first read pump    */
} qic_cap;

/* Append one raw byte into GW's u_buf[] ring (same cursor as rdata_encode_flux).
 * Marker bytes are NOT counted in the flux-data accounting (only flux is). */
static void qic_buf_put(uint8_t b)
{
    u_buf[U_MASK(u_prod++)] = b;
}

static void qic_buf_put_u16(uint16_t v)
{
    qic_buf_put((uint8_t)(v & 0xff));
    qic_buf_put((uint8_t)(v >> 8));
}

static void qic_buf_put_u32(uint32_t v)
{
    qic_buf_put((uint8_t)(v & 0xff));
    qic_buf_put((uint8_t)(v >> 8));
    qic_buf_put((uint8_t)(v >> 16));
    qic_buf_put((uint8_t)(v >> 24));
}

/* Emit a framed marker: ESC(0xFF) code len payload. Payloads are tiny and fixed
 * (max 13 bytes) so they always fit within the ring's headroom in practice. */
static void qic_marker_begin(uint8_t code, uint8_t plen)
{
    qic_buf_put(0xff);   /* GW opcode escape (host ESC) */
    qic_buf_put(code);   /* TW_MARK_* */
    qic_buf_put(plen);
}

/* SESSION_START: rate:u16, clock:u32, tpt:u16, direction:u8, pass_id:u16 (11). */
static void qic_mark_session_start(void)
{
    qic_marker_begin(TW_MARK_SESSION_START, 11);
    qic_buf_put_u16(qic_cap.rate);
    qic_buf_put_u32(gw_info.sample_freq); /* flux sample clock (Hz) */
    qic_buf_put_u16(qic_cap.tpt);
    qic_buf_put(qic_cap.direction);
    qic_buf_put_u16(qic_cap.pass_id);
}

/* SEGMENT: ticks:u32, index:u32 (8). One per hardware INDEX edge (= per tape
 * segment, §2.2). `ticks` is the sample-clock delta since the previous segment. */
static void qic_mark_segment(uint32_t ticks, uint32_t index)
{
    qic_marker_begin(TW_MARK_SEGMENT, 8);
    qic_buf_put_u32(ticks);
    qic_buf_put_u32(index);
}

/* EVENT: code:u8 (1). */
static void qic_mark_event(uint8_t code)
{
    qic_marker_begin(TW_MARK_EVENT, 1);
    qic_buf_put(code);
}

/* END: reason:u8, flux_count:u32, byte_count:u32, checksum:u32 (13). Seals the
 * run; a run with no valid END is flagged truncated by the host (still decodes). */
static void qic_mark_end(uint8_t reason)
{
    qic_marker_begin(TW_MARK_END, 13);
    qic_buf_put(reason);
    qic_buf_put_u32(qic_cap.flux_count);
    qic_buf_put_u32(qic_cap.byte_count);
    qic_buf_put_u32(qic_cap.checksum);
}

/* ======================================================================== *
 *  Free-running tape capture (DESIGN.md §5.4) -- reuses GW's flux read engine
 *  ----------------------------------------------------------------------- *
 *  The capture IS GW's flux read: same tim_rdata/dma_rdata input-capture front
 *  end, same rdata_encode_flux() byte encoder, same ST_read_flux state machine
 *  and u_buf[] USB pump (floppy_read()). The ONLY differences are:
 *    - it free-runs (no max_index, no tick deadline) -- a flux read with both
 *      limits cleared already does exactly this;
 *    - it brackets the stream with our typed SESSION_START / END markers and
 *      emits a typed SEGMENT marker per INDEX edge.
 *
 *  Per-segment SEGMENT markers are produced by qic_capture_on_index(), which is
 *  called from inside rdata_encode_flux()'s existing "we just passed the index
 *  mark" branch (see the graft hook in floppy.c). That branch already runs once
 *  per INDEX edge with the exact tick delta GW computes for its own FLUXOP_INDEX
 *  opcode -- we ride alongside it so segment boundaries arrive for free and use
 *  GW's own timing.
 *
 *  TODO(bench) §13.6 item 2: on tape INDEX fires per SEGMENT (not per rev); the
 *  edge handling here assumes GW's index detection + index_mask debounce behave
 *  the same for the ~2 ms segment cadence as for a 200 ms floppy revolution.
 *  Confirm the debounce (delay_params.index_mask) doesn't swallow segment edges.
 * ======================================================================== */

/* Account for flux bytes GW just wrote into u_buf[]. Called from the graft hook
 * in rdata_encode_flux() after each flux value is emitted, with the u_prod-style
 * cursor where the value started and the number of bytes written, so byte_count
 * / checksum match exactly what the host receives (flux DATA only -- markers are
 * written via qic_buf_* and deliberately excluded from this accounting). A
 * no-op unless a capture is armed, so the normal GW read path pays only a test. */
static void qic_capture_account_range(uint32_t start_prod, uint32_t count)
{
    uint32_t i;
    if (!qic_cap.active)
        return;
    qic_cap.byte_count += count;
    for (i = 0; i < count; i++) {
        uint8_t b = u_buf[U_MASK(start_prod + i)];
        qic_cap.checksum = (qic_cap.checksum + b) & 0xffffffffu;
    }
}

static void qic_capture_account_transition(void)
{
    if (qic_cap.active)
        qic_cap.flux_count++;
}

/* True when a byte-budget capture has streamed its budget and should self-stop.
 * Checked by floppy_read() each pump; a budgeted capture then takes the clean
 * drain path (qic_capture_finish) so a valid END is delivered (§5.4). */
static bool_t qic_capture_budget_reached(void)
{
    return qic_cap.active && (qic_cap.byte_budget != 0)
        && (qic_cap.byte_count >= qic_cap.byte_budget);
}

/* SEGMENT-marker hook: called from rdata_encode_flux()'s index branch with the
 * tick delta GW already computed. Emits our typed SEGMENT marker into the SAME
 * u_buf[] stream (so it sits right next to GW's FLUXOP_INDEX opcode -- the host
 * cross-checks the two, §7.1). */
static void qic_capture_on_index(uint32_t ticks)
{
    if (!qic_cap.active)
        return;
    qic_cap.seg_index++;
    qic_mark_segment(ticks, qic_cap.seg_index);
}

/* Arm a capture: reset accounting and drive GW's flux read with NO limits
 * (free-running). Reuses floppy_read_prep() verbatim -- it arms tim_rdata/
 * dma_rdata, zeroes index.count, and sets floppy_state = ST_read_flux, after
 * which GW's floppy_read() pumps u_buf[] to USB exactly as for a normal read.
 *
 * SESSION_START is NOT written here: the CMD_QIC_CAPTURE ACK goes out via
 * floppy_end_command(), which zeroes u_cons/u_prod afterward. So we defer
 * SESSION_START to the first floppy_read() pump (qic_capture_emit_session_start_
 * if_needed), where it lands at the very front of the fresh stream (§7.2). */
static uint8_t qic_capture_arm(void)
{
    struct gw_read_flux rf = {
        .ticks = 0,            /* no tick deadline: free-running             */
        .max_index = 0,        /* no index limit: stream until stopped       */
        .max_index_linger = 0,
    };

    qic_cap.flux_count = 0;
    qic_cap.byte_count = 0;
    qic_cap.checksum = 0;
    qic_cap.seg_index = 0;
    qic_cap.need_session_start = TRUE;
    qic_cap.active = TRUE;

    return floppy_read_prep(&rf);
}

/* Emit SESSION_START (+ a MOTION_STARTED event) as the first bytes of the run,
 * once, at the start of streaming. Called from floppy_read()'s ST_read_flux
 * branch before rdata_encode_flux(), so it precedes all flux (§7.2 anchors t=0).
 * The marker bytes are deliberately excluded from the flux-data accounting. */
static void qic_capture_emit_session_start_if_needed(void)
{
    if (!qic_cap.active || !qic_cap.need_session_start)
        return;
    qic_cap.need_session_start = FALSE;
    qic_mark_session_start();
    qic_mark_event(TW_EVT_MOTION_STARTED);
}

/* ======================================================================== *
 *  QIC command handlers (grafted into process_command(), §13.3)
 *  ----------------------------------------------------------------------- *
 *  Each returns the response size to send (resp data placed from u_buf[2]) and
 *  sets u_buf[1] to a status, mirroring GW's own command handlers. The CAPTURE
 *  handler instead arms the reuse of GW's read path and returns via the read
 *  state machine (like CMD_READ_FLUX), so it does NOT go through the normal
 *  end-of-command ACK here.
 * ======================================================================== */

/* CMD_QIC_INFO: capability gate. Layout after ACK byte (u_buf[2]..):
 *   proto_ver:u8, caps:u32, sram_bytes:u32, sample_hz:u32. */
static unsigned int qic_cmd_info(void)
{
    unsigned int n = 2; /* u_buf[0]=cmd echo, u_buf[1]=ACK; payload from [2] */
    u_buf[n++] = (uint8_t)TW_PROTO_VERSION;
    /* caps: this firmware provides verbs + capture + markers. */
    *(uint32_t *)&u_buf[n] = TW_CAP_VERBS | TW_CAP_CAPTURE | TW_CAP_MARKERS;
    n += 4;
    *(uint32_t *)&u_buf[n] = (uint32_t)U_BUF_SZ; /* usable stream buffer bytes */
    n += 4;
    *(uint32_t *)&u_buf[n] = gw_info.sample_freq; /* flux sample clock (Hz) */
    n += 4;
    u_buf[1] = ACK_OKAY;
    return n;
}

/* CMD_QIC_SET_TIMING: idle-only. Payload (u_buf[2]..), little-endian:
 *   pulse_us:u16, inter_pulse_us:u16, terminate_gap_us:u16,
 *   tack_us:u16, tbit_us:u16, report_on_index:u8  (11 bytes). */
static void qic_cmd_set_timing(uint8_t len)
{
    if (len != 2 + 11) {
        u_buf[1] = ACK_BAD_COMMAND;
        return;
    }
    qic_timing.pulse_us         = *(uint16_t *)&u_buf[2];
    qic_timing.inter_pulse_us   = *(uint16_t *)&u_buf[4];
    qic_timing.terminate_gap_us = *(uint16_t *)&u_buf[6];
    qic_timing.tack_us          = *(uint16_t *)&u_buf[8];
    qic_timing.tbit_us          = *(uint16_t *)&u_buf[10];
    qic_timing.report_on_index  = u_buf[12];
    u_buf[1] = ACK_OKAY;
}

/* CMD_QIC_PULSES: emit cmd_n STEP pulses verbatim, optionally clock report_bits
 * off TRK0 device-side. Payload: cmd_n:u8, report_bits:u8.
 * Response after ACK (u_buf[2]..): flags:u8 (b0 ack, b1 final_ok, b2 timed_out),
 * bits:u16, nbits:u8. */
static unsigned int qic_cmd_pulses(uint8_t len)
{
    uint8_t cmd_n, report_bits, flags = 0;
    struct qic_report rep;
    unsigned int n = 2;

    if (len != 2 + 2) {
        u_buf[1] = ACK_BAD_COMMAND;
        return n;
    }
    cmd_n = u_buf[2];
    report_bits = u_buf[3];

    /* Take the bus: select the drive, emit the command, optionally clock the
     * report, then return the bus quiescent. drive_select() asserts the GW msel
     * line for the current unit; we reuse it so the QIC channel and GW share the
     * one drive-select owner. A capture must not be running (single bus owner).*/
    if (floppy_state != ST_command_wait || qic_cap.active) {
        u_buf[1] = ACK_BAD_COMMAND;
        return n;
    }

    qic_pulses(cmd_n);

    rep.ack = FALSE; rep.final_ok = FALSE; rep.bits = 0;
    rep.nbits = 0; rep.timed_out = FALSE;
    if (report_bits > 0)
        qic_report(report_bits, &rep);

    if (rep.ack)       flags |= 0x01;
    if (rep.final_ok)  flags |= 0x02;
    if (rep.timed_out) flags |= 0x04;
    u_buf[n++] = flags;
    *(uint16_t *)&u_buf[n] = rep.bits;
    n += 2;
    u_buf[n++] = rep.nbits;
    u_buf[1] = ACK_OKAY;
    return n;
}

/* CMD_QIC_WAIT_READY: poll INDEX/ready up to timeout_s seconds. Payload:
 * timeout_s:u16. Response (u_buf[2]..): status:u8 (0 ready, 1 timed out). */
static unsigned int qic_cmd_wait_ready(uint8_t len)
{
    uint16_t timeout_s;
    bool_t ready;
    unsigned int n = 2;

    if (len != 2 + 2) {
        u_buf[1] = ACK_BAD_COMMAND;
        return n;
    }
    timeout_s = *(uint16_t *)&u_buf[2];
    ready = qic_wait_ready((uint32_t)timeout_s * 1000u);
    u_buf[n++] = ready ? 0u : 1u;
    u_buf[1] = ACK_OKAY;
    return n;
}

/* CMD_QIC_CAPTURE: issue motion verbatim, then arm the free-running flux read.
 * Payload (little-endian):
 *   motion_n:u8, rate:u16, tpt:u16, direction:u8, pass_id:u16, byte_budget:u32
 *   (12 bytes).
 * `byte_budget` is the StopCond (§13.3): when non-zero the device self-terminates
 * after that many flux DATA bytes and delivers a clean END through the drain path
 * (qic_capture_finish). When zero the capture free-runs until the host stops the
 * stream out-of-band (BAUD_CLEAR_COMMS -> floppy_configure, §5.2). We do NOT
 * wait-ready/status after the motion command because a status report can swallow
 * the first segment (§2.1, §6.3). On success u_buf[1] is the floppy_read_prep()
 * ACK and the read state machine takes over, exactly like CMD_READ_FLUX. */
static void qic_cmd_capture(uint8_t len)
{
    uint8_t motion_n;

    if (len != 2 + 12) {
        u_buf[1] = ACK_BAD_COMMAND;
        return;
    }
    motion_n           = u_buf[2];
    qic_cap.rate        = *(uint16_t *)&u_buf[3];
    qic_cap.tpt         = *(uint16_t *)&u_buf[5];
    qic_cap.direction   = u_buf[7];
    qic_cap.pass_id     = *(uint16_t *)&u_buf[8];
    qic_cap.byte_budget = *(uint32_t *)&u_buf[10];

    if (floppy_state != ST_command_wait || qic_cap.active) {
        u_buf[1] = ACK_BAD_COMMAND;
        return;
    }

    /* Issue motion via the verbs engine (verbatim). For Logical Forward we arm
     * capture immediately afterward -- no wait-ready/status in between. */
    qic_pulses(motion_n);

    u_buf[1] = qic_capture_arm();
    if (u_buf[1] != ACK_OKAY)
        qic_cap.active = FALSE; /* arm failed: nothing to tear down */
}

/* Safe-stop the tape and seal the run. Called when a capture read terminates
 * (normal stop, overflow, no-index timeout) so the tape is always stopped and
 * an END marker is emitted. Mirrors the arbiter Quiesce funnel's "stop first,
 * always" (§5.2 I5): STOP_TAPE on the bus, then END into the stream.
 *
 * `reason` is a TW_END_* code. This runs from inside floppy_read()'s end paths
 * (see the graft hook in floppy.c) where the flux engine has already been
 * halted (floppy_flux_end()), so it is safe to drive STEP again here. */
static void qic_capture_finish(uint8_t reason)
{
    if (!qic_cap.active)
        return;

    /* Stop the tape FIRST -- dropping motion control with tape rolling risks
     * spooling into EOT/BOT (the broken-tape hazard, §5.2). STEP is free now
     * that the flux front-end has been stopped. */
    qic_pulses(QIC_CMD_STOP_TAPE);

    /* Seal the run with counts + checksum. Written into u_buf[] so it streams
     * out as the tail of the capture; the host's END verify() checks it. */
    qic_mark_end(reason);

    qic_cap.active = FALSE;
}

/* Safety-critical capture teardown for the out-of-band reset/clear-comms paths
 * (floppy_configure() / floppy_reset()): the host has stopped the stream (or the
 * link dropped), so the END marker cannot be delivered and u_buf[] is about to
 * be zeroed. We STILL stop the tape -- it is a pure bus operation, host-
 * independent, and the strongest reason the DEVICE owns the stop (§5.2): never
 * leave the tape rolling. The run is then flagged truncated by the host (no END)
 * but still decodes, because sectors self-locate (§5.4). */
static void qic_capture_abort_silent(void)
{
    if (!qic_cap.active)
        return;
    qic_pulses(QIC_CMD_STOP_TAPE);
    qic_cap.active = FALSE;
}
