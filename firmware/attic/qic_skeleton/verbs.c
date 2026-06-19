/*
 * verbs.c — QIC verbs engine (DESIGN.md §5.3).
 *
 * Verbatim pulse emit + N+2 args + report-bit clock + wait-ready. No QIC-117
 * command meaning is encoded here; the engine only knows the pulse cadence and
 * the report framing.
 *
 * Bus access is entirely through gw_stubs (gw_pin_write/read on STEP/TRK0/INDEX,
 * gw_delay_us, gw_now_us). The arbiter has already granted the lease and
 * asserted drive-select before any of these run.
 *
 * The QIC-117 REPORT_NEXT_BIT command is "2 step pulses" (command 2). We hard-
 * code that count here because it is part of the report-framing protocol, not a
 * host-supplied verb — the host asks for `report_bits`, the engine knows how to
 * clock them. This is the one place the engine is not purely verbatim, and it is
 * deliberate (§5.3).
 */

#include "verbs.h"
#include "gw_stubs.h"

/* QIC-117: REPORT_NEXT_BIT is command 2 == 2 STEP pulses (§2.1, §13.1). */
#define QIC_CMD_REPORT_NEXT_BIT_PULSES 2u

static qic_timing_t g_timing;
static bool g_timing_set;

void qic_timing_defaults(qic_timing_t *out) {
    if (out == NULL) {
        return;
    }
    /* QIC-117 Rev J Table 1 nominal envelope (§2.1, §5.3). */
    out->pulse_us = 5;             /* STEP asserted width: a short, clean pulse */
    out->inter_pulse_us = 2000;    /* ~2.0 ms step interval (0.9-2.1 ms)        */
    out->terminate_gap_us = 3000;  /* > 2.9 ms ends the command train           */
    out->tack_us = 2500;           /* TACK ~2.5 ms for the ACK bit              */
    out->tbit_us = 900;            /* TBIT ~900 µs per report data bit          */
    out->report_on_index = 0;      /* default fixed-settle; bench may flip to 1 */
}

void qic_set_timing(const qic_timing_t *t) {
    if (t == NULL) {
        return;
    }
    g_timing = *t;
    g_timing_set = true;
}

const qic_timing_t *qic_get_timing(void) {
    if (!g_timing_set) {
        qic_timing_defaults(&g_timing);
        g_timing_set = true;
    }
    return &g_timing;
}

/* Emit exactly one STEP pulse: assert for pulse_us, then idle for the given
 * inter-pulse gap. STEP is active-low on the open-collector bus; gw_pin_write
 * takes the LOGICAL level and the polarity lives in the stub (§gw_stubs). */
static void step_pulse(const qic_timing_t *t, uint16_t gap_us) {
    gw_pin_write(GW_PIN_STEP, true);   /* assert */
    gw_delay_us(t->pulse_us);
    gw_pin_write(GW_PIN_STEP, false);  /* release */
    gw_delay_us(gap_us);
}

void qic_pulses(uint8_t n) {
    const qic_timing_t *t = qic_get_timing();

    /* Emit n pulses grouped under the command time-out: every intra-train gap is
     * inter_pulse_us (kept < command time-out so an isolated slow pulse can't be
     * misread as Soft Reset — §2.1 hazard). The LAST pulse is followed by the
     * terminating gap to end the command train. */
    for (uint8_t i = 0; i < n; i++) {
        bool last = (i + 1u == n);
        step_pulse(t, last ? t->terminate_gap_us : t->inter_pulse_us);
        /* Long trains can be slow (n up to ~32); keep the watchdog happy. */
        gw_watchdog_kick();
    }

    /* n == 0: nothing to emit. We do NOT assert TRK0 (it's an input we only
     * read) and we leave STEP released — never leave a line stuck (§5.3). */

    /* Defensive: ensure STEP is released after any train. */
    gw_pin_write(GW_PIN_STEP, false);
}

void qic_arg(uint8_t value) {
    /* N+2 form: value + 2 pulses (§2.1). Guard the +2 against u8 wrap; QIC
     * argument nibbles are small (<= 15 per nibble, §13.1) so value+2 fits, but
     * cap defensively at the 32-pulse train limit. */
    uint16_t pulses = (uint16_t)value + 2u;
    if (pulses > 0xFF) {
        pulses = 0xFF;
    }
    qic_pulses((uint8_t)pulses);
}

/* Wait for TRK0 to read `want` within `window_us`, polling. Returns true if the
 * level was observed before the window expired. */
static bool wait_trk0(bool want, uint16_t window_us) {
    uint32_t start = gw_now_us();
    for (;;) {
        if (gw_pin_read(GW_PIN_TRK0) == want) {
            return true;
        }
        if ((uint32_t)(gw_now_us() - start) >= window_us) {
            return false;
        }
    }
}

/* Sample one report data bit. With report_on_index, wait for the INDEX cue that
 * a bit is now on TRK0 (§2.1: INDEX cues "a report bit is now on TRK0"); else
 * use a fixed settle (report_settle within TBIT). Returns the sampled level;
 * sets *timed_out if the window expired. */
static bool sample_report_bit(const qic_timing_t *t, bool *timed_out) {
    *timed_out = false;
    if (t->report_on_index) {
        /* Wait for INDEX asserted (bit ready), bounded by TBIT. */
        uint32_t start = gw_now_us();
        while (!gw_pin_read(GW_PIN_INDEX)) {
            if ((uint32_t)(gw_now_us() - start) >= t->tbit_us) {
                *timed_out = true;
                break;
            }
        }
    } else {
        /* Fixed settle: the value is latched, so a delay within TBIT suffices. */
        gw_delay_us(t->tbit_us);
    }
    /* TRK0 carries the bit, LSB-first across the loop. The drive latched the
     * value at command receipt, so host/loop jitter is harmless (§2.1, §5.3). */
    return gw_pin_read(GW_PIN_TRK0);
}

void qic_report(uint8_t report_bits, qic_report_t *out) {
    const qic_timing_t *t = qic_get_timing();

    if (out == NULL) {
        return;
    }
    out->ack = false;
    out->final_ok = false;
    out->bits = 0;
    out->nbits = 0;
    out->timed_out = false;

    if (report_bits > 16) {
        report_bits = 16; /* up to 16 bits (Report Error Code = 2 bytes) */
    }

    /* ACK bit: must appear TRUE within TACK; FALSE => reset/hardware failure. */
    if (!wait_trk0(true, t->tack_us)) {
        out->timed_out = true;
        out->ack = false; /* never saw a true ACK within TACK */
        return;
    }
    out->ack = true;

    /* Clock each data bit: REPORT_NEXT_BIT (2 pulses), then sample TRK0 within
     * TBIT, LSB-first (§5.3). */
    for (uint8_t i = 0; i < report_bits; i++) {
        qic_pulses(QIC_CMD_REPORT_NEXT_BIT_PULSES);
        bool timed_out = false;
        bool bit = sample_report_bit(t, &timed_out);
        if (timed_out) {
            out->timed_out = true;
            /* keep what we have; host treats a short report as suspect */
            out->nbits = i;
            return;
        }
        if (bit) {
            out->bits |= (uint16_t)(1u << i); /* LSB-first */
        }
        out->nbits = (uint8_t)(i + 1u);
        gw_watchdog_kick();
    }

    /* Final bit: clock once more and read; FALSE => an error occurred mid-report
     * (§2.1). We use the same REPORT_NEXT_BIT cadence to advance to the Final
     * position, then sample. */
    qic_pulses(QIC_CMD_REPORT_NEXT_BIT_PULSES);
    {
        bool timed_out = false;
        bool fin = sample_report_bit(t, &timed_out);
        if (timed_out) {
            out->timed_out = true;
            out->final_ok = false;
        } else {
            out->final_ok = fin;
        }
    }
}

bool qic_wait_ready(uint32_t timeout_ms) {
    /* Poll the ready line until ready or timeout. The drive cues readiness on
     * INDEX in its idle states (§2.1); we treat an asserted INDEX/ready as
     * "ready". TODO(bench) §13.6 item 3: confirm which line the target drive
     * presents as ready and whether to edge-detect vs level-poll. */
    uint32_t start = gw_now_us();
    uint32_t timeout_us = timeout_ms * 1000u; /* may wrap for very long motion */

    for (;;) {
        if (gw_pin_read(GW_PIN_INDEX)) {
            return true;
        }
        /* Unsigned elapsed handles the 71-min wrap of gw_now_us(); for motion
         * timeouts longer than that the watchdog is the real backstop. */
        if ((uint32_t)(gw_now_us() - start) >= timeout_us) {
            return false;
        }
        gw_watchdog_kick();
    }
}
