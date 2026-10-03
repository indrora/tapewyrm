# TWS-1: TWRF, the Tapewyrm Raw Flux Capture Format

```text
Tapewyrm Specification TWS-1                     Tapewyrm contributors
Category: Standards Track (Tapewyrm)                     Status: Draft
License: The Unlicense (public domain)                 2026-10-03

        TWRF: The Tapewyrm Raw Flux Capture Format, Version 2
```

## Abstract

This document specifies TWRF, the file format in which Tapewyrm stores one
raw magnetic-flux capture of a QIC-40/80/3010/3020 floppy-interface tape
pass. A TWRF file is a fixed preamble, a JSON header that records how and
by what the flux was captured, and the verbatim Greaseweazle flux byte
stream as the device sent it, with Tapewyrm's in-band markers inside. It is
the first stage of the Tapewyrm pipeline and the input to the tape image
format [TWS-2].

## Status of This Memo

This is a Tapewyrm project specification, not an IETF document. It is
normative for Tapewyrm implementations: the reference writer (`tw dump`),
the reference readers (`tw convert`, `qicsilver identify`) and any third
party program that reads or writes TWRF files. Where this document and the
reference code disagree, the disagreement is a defect in one of them and
SHOULD be reported.

## Table of Contents

1. [Introduction](#1-introduction)
   1. [Requirements Language](#11-requirements-language)
   2. [Terminology](#12-terminology)
   3. [Conventions](#13-conventions)
2. [File Layout](#2-file-layout)
3. [Preamble](#3-preamble)
4. [Header](#4-header)
   1. [Encoding](#41-encoding)
   2. [Members](#42-members)
   3. [Drive Report Bytes](#43-drive-report-bytes)
   4. [Example Header](#44-example-header)
5. [Flux Body](#5-flux-body)
   1. [Flux Intervals](#51-flux-intervals)
   2. [Opcode Escapes](#52-opcode-escapes)
   3. [N28 Encoding](#53-n28-encoding)
   4. [Stream Terminator](#54-stream-terminator)
   5. [Decoding Algorithm](#55-decoding-algorithm)
6. [Markers](#6-markers)
   1. [Framing](#61-framing)
   2. [SESSION_START (0xF0)](#62-session_start-0xf0)
   3. [SEGMENT (0xF1)](#63-segment-0xf1)
   4. [EVENT (0xF2)](#64-event-0xf2)
   5. [END (0xF3)](#65-end-0xf3)
   6. [HEARTBEAT (0xF4)](#66-heartbeat-0xf4)
   7. [Marker Order](#67-marker-order)
7. [End of Data and Integrity](#7-end-of-data-and-integrity)
   1. [Determining the End of Data](#71-determining-the-end-of-data)
   2. [END Accounting and Checksum](#72-end-accounting-and-checksum)
   3. [Truncated Captures](#73-truncated-captures)
8. [Processing Rules](#8-processing-rules)
   1. [Writer Requirements](#81-writer-requirements)
   2. [Reader Requirements](#82-reader-requirements)
9. [Versioning and Extensibility](#9-versioning-and-extensibility)
10. [Security and Privacy Considerations](#10-security-and-privacy-considerations)
11. [IANA Considerations](#11-iana-considerations)
12. [References](#12-references)
    1. [Normative References](#121-normative-references)
    2. [Informative References](#122-informative-references)
- [Appendix A. Example](#appendix-a-example)
- [Appendix B. The dump.jsonl Sidecar](#appendix-b-the-dumpjsonl-sidecar)
- [Appendix C. Change Log](#appendix-c-change-log)
- [Author's Address](#authors-address)

## 1. Introduction

Tapewyrm reads QIC floppy-interface tapes through a Greaseweazle v4.1
running Tapewyrm firmware. The drive is put into a motion (normally
Logical Forward [QIC-117]) and the firmware streams every flux transition
it sees, using the Greaseweazle flux encoding [GW], until the tape stops.
TWRF records that stream unchanged. Nothing is decoded and nothing is
re-encoded: decoding (PLL, MFM, sector recovery) happens later, offline,
from the file.

A TWRF file holds exactly one pass: one continuous capture session. The
reference writer, `tw dump`, writes one Logical Forward pass over one tape
track per file.

The pipeline is: `tw dump` (TWRF, this document) -> `tw convert` (TWTI or
TWTZ, [TWS-2]) -> `qicsilver extract` (TWVL, [TWS-3]) -> `qicsilver tar`.

### 1.1. Requirements Language

The key words "MUST", "MUST NOT", "REQUIRED", "SHALL", "SHALL NOT",
"SHOULD", "SHOULD NOT", "RECOMMENDED", "NOT RECOMMENDED", "MAY", and
"OPTIONAL" in this document are to be interpreted as described in BCP 14
[RFC2119] [RFC8174] when, and only when, they appear in all capitals, as
shown here.

### 1.2. Terminology

Track
: One of the parallel longitudinal tracks of a QIC cartridge (28 on a
  QIC-80 DC2120). Tracks are recorded serpentine: even tracks toward
  physical EOT ("forward"), odd tracks toward physical BOT ("reverse").

TPT
: Track number, 0-based, as given to Seek Head to Track [QIC-117].

Segment
: The unit the drive finds on tape: 32 sectors of 1024 bytes recorded as
  one MFM burst [QIC-80]. During Logical Forward the drive pulses its INDEX
  line once at the start of every segment it finds.

Pass
: One capture session: from the moment the firmware arms the capture to
  the moment it stops. One TWRF file holds one pass.

Sample clock
: The Greaseweazle input-capture timer that timestamps flux transitions.
  72 MHz on a Greaseweazle v4.1.

Tick
: One period of the sample clock.

Interval
: The time, in ticks, between two consecutive flux transitions.

Flux body
: Everything in the file after the header (Section 5).

Marker
: A Tapewyrm in-band record inside the flux body (Section 6).

Drive report byte
: A raw value returned by one of the QIC-117 report commands (Report
  Drive Status, Report Drive Configuration, and so on) [QIC-117].

### 1.3. Conventions

All multi-byte integers are unsigned and little-endian unless stated
otherwise. Byte offsets are 0-based. Sizes are in bytes (octets). Hex
values are written `0xNN`. `u8`, `u16`, `u32` denote unsigned integers of
8, 16 and 32 bits. A range `a..b` is inclusive at both ends.

## 2. File Layout

```text
+------------------------------+  offset 0
| Preamble (10 bytes)          |  Section 3
+------------------------------+  offset 10
| Header (hlen bytes, JSON)    |  Section 4
+------------------------------+  offset 10 + hlen
| Flux body (to end of file)   |  Section 5
+------------------------------+
```

The flux body starts at offset `10 + hlen` and runs to the end of the
file. There is no trailer and no padding. The file's length is not
recorded anywhere; a writer streams flux to disk as it arrives.

## 3. Preamble

```text
 0                   1                   2                   3
 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|  'T' (0x54)   |  'W' (0x57)   |  'R' (0x52)   |  'F' (0x46)   |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|        version (u16)          |   hlen (u32, low 16 bits)     |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|   hlen (u32, high 16 bits)    |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
```

| Offset | Size | Field     | Value                                           |
|-------:|-----:|-----------|-------------------------------------------------|
| 0      | 4    | `magic`   | ASCII `TWRF` (`54 57 52 46`)                    |
| 4      | 2    | `version` | Format version, u16. This document: `2`         |
| 6      | 4    | `hlen`    | Length of the header in bytes, u32              |

The preamble is the struct `<4sHI` (no padding): 10 bytes.

## 4. Header

### 4.1. Encoding

The header is exactly `hlen` bytes of UTF-8 encoded JSON [RFC8259]
holding a single JSON object. It is not NUL-terminated and has no
trailing whitespace requirement.

The reference writer serializes with no insignificant whitespace (item
separator `,`, member separator `:`), ASCII-only output (non-ASCII
characters escaped as `\uXXXX`), and members in the order of the table in
Section 4.2. Readers MUST NOT depend on member order or whitespace.

### 4.2. Members

"Req." gives the requirement on a writer of this version (2). "Since"
gives the format version that introduced the member; it is history only,
since readers read version 2 alone (Section 9).

| Member                | JSON type        | Req.     | Since | Meaning |
|-----------------------|------------------|----------|:-----:|---------|
| `rate_kbps`           | integer          | REQUIRED | 1 | Data transfer rate of the recording, kbit/s: 250, 500, 1000 or 2000. The rate the decoder's PLL uses. Taken from Report Drive Configuration (Section 4.3); see also [TWS-2]. |
| `sample_clock_hz`     | integer          | REQUIRED | 1 | Sample clock of the capturing device, Hz (72000000 on a Greaseweazle v4.1). |
| `track`               | integer (signed) | REQUIRED | 1 | TPT of the pass, 0-based. `-1` means unknown (a probe capture that did not seek). |
| `direction`           | string           | REQUIRED | 1 | `"forward"` or `"reverse"`: the serpentine direction of `track` (even forward, odd reverse). For a non-track probe, the direction of tape motion. |
| `pass_id`             | integer          | REQUIRED | 1 | Writer-assigned pass number, u16 range. Distinguishes repeated passes over one track. `tw dump` writes `1`. |
| `utc`                 | string           | REQUIRED | 1 | Capture start time, ISO 8601 / [RFC3339] date-time with offset, e.g. `"2026-01-02T03:04:05+00:00"`. MAY be `""` if unknown. |
| `tape_format`         | integer          | REQUIRED | 1 | Recording format code: 0 unknown, 1 QIC-40, 2 QIC-80, 3 QIC-3020, 4 QIC-3010. These are the Report Tape Status bits 0-3 values [QIC-117]. |
| `segments_per_track`  | integer          | REQUIRED | 1 | Segments per track if known, else `0`. |
| `tracks`              | integer          | REQUIRED | 1 | Tracks on the cartridge if known, else `0`. |
| `sectors_per_segment` | integer          | REQUIRED | 1 | Sectors per segment. Always `32` for the formats in scope. |
| `device_serial`       | string           | REQUIRED | 1 | USB serial number string of the capturing Greaseweazle, `""` if unknown. |
| `physical_reverse`    | boolean          | REQUIRED | 1 | `true` if the pass was taken with Physical Reverse motion, so the flux is in time-reversed order relative to the recording and must be reversed offline before decoding. `false` for Logical Forward passes. |
| `drive_status`        | integer or null  | REQUIRED | 2 | Report Drive Status (QIC-117 command 6), 8 bits, read immediately before the pass. `null` if not read. |
| `drive_config`        | integer or null  | REQUIRED | 2 | Report Drive Configuration (command 8), 8 bits. `null` if the drive does not implement it. |
| `drive_rom`           | integer or null  | REQUIRED | 2 | Report ROM Version (command 9), 8 bits. `null` if not implemented. |
| `drive_vendor_id`     | integer or null  | REQUIRED | 2 | Report Vendor ID (command 32), 16 bits. `null` if not implemented. |
| `tape_status`         | integer or null  | REQUIRED | 2 | Report Tape Status (command 33), 8 bits. `null` if not implemented. |
| `tw_commit`           | string or null   | REQUIRED | 2 | Git commit (40 lowercase hex characters) of the host software that wrote the file, `null` if unknown. |
| `firmware_commit`     | string or null   | REQUIRED | 2 | Git commit (40 hex characters) of the device firmware, from its BUILD_INFO verb; `null` if unknown. |
| `firmware_dirty`      | boolean or null  | REQUIRED | 2 | `true` if the firmware was built from a modified tree, `null` if unknown. |

Every member is REQUIRED: a writer MUST write it and a reader MUST reject
a header that lacks it (Section 8.2). For the members typed "or null",
the member is present and its value MAY be `null`. All integers fit in a
signed 64-bit integer.

Notes:

1. `rate_kbps` is the authoritative bit rate. A reader MUST use it, not a
   rate implied by `tape_format`: drives record at non-standard rates (a
   QIC-3020 tape read by a QIC-3010 drive at 1000 kbit/s, for example).
2. `sample_clock_hz` duplicates the `clock` field of the SESSION_START
   marker (Section 6.2). They SHOULD be equal. When both are present and
   differ, a reader SHOULD use the SESSION_START value, which the firmware
   reported for the exact session.
3. `segments_per_track` and `tracks` are hints. `tw dump` writes `0` for
   both; geometry is recovered from the tape's own header segment [TWS-2].

### 4.3. Drive Report Bytes

The version 2 report members are the drive's raw report values, unmodified,
so that a capture is self-describing [QIC-117]. Their meaning:

```text
drive_status   (Report Drive Status, cmd 6; bits 1,5,6,7 valid only when ready)
   bit 0 ready         bit 4 new cartridge
   bit 1 error         bit 5 referenced
   bit 2 cartridge     bit 6 at physical BOT
   bit 3 write protect bit 7 at physical EOT

drive_config   (Report Drive Configuration, cmd 8)
   bits 3-4 rate: 00 = 250 kbit/s (or 4 Mbit/s on some drives),
                  01 = 2000, 10 = 500, 11 = 1000 kbit/s
   bit 6    extra-length tape
   bit 7    QIC-80 mode

drive_vendor_id (Report Vendor ID, cmd 32, 16 bits)
   bits 6-15 make, bits 0-5 model.
   Exception: the whole-word value 71 (0x0047) is Colorado Memory
   Systems' legacy ID and is not split.

tape_status    (Report Tape Status, cmd 33)
   bits 0-3 format (as tape_format)   bits 4-6 tape type   bit 7 wide
```

`drive_rom` is opaque. The reference writer derives `rate_kbps` from
`drive_config` bits 3-4 (code 00 read as 250) and falls back to 500 when
`drive_config` is `null`. It derives `tape_format` from `tape_status` bits
0-3, and, when that is 0 and `drive_config` bit 7 is set, writes 2
(QIC-80).

### 4.4. Example Header

A synthetic version 2 header (whitespace added for display; the writer
emits none):

```json
{
  "rate_kbps": 500,
  "sample_clock_hz": 72000000,
  "track": 4,
  "direction": "forward",
  "pass_id": 1,
  "utc": "2026-01-02T03:04:05+00:00",
  "tape_format": 2,
  "segments_per_track": 0,
  "tracks": 0,
  "sectors_per_segment": 32,
  "device_serial": "EXAMPLE0001",
  "physical_reverse": false,
  "drive_status": 37,
  "drive_config": 144,
  "drive_rom": 64,
  "drive_vendor_id": 258,
  "tape_status": 18,
  "tw_commit": "0000000000000000000000000000000000000000",
  "firmware_commit": "1111111111111111111111111111111111111111",
  "firmware_dirty": false
}
```

## 5. Flux Body

The flux body is the byte stream the Tapewyrm firmware sent over USB
during the pass, stored verbatim. Its encoding is the Greaseweazle
`CMD_READ_FLUX` stream encoding [GW] (Greaseweazle firmware by Keir
Fraser, `rdata_encode_flux()`), extended with Tapewyrm markers in the
opcode-escape space. Writers MUST NOT re-encode, filter, unstuff or
otherwise alter it.

### 5.1. Flux Intervals

Each flux transition is encoded as the interval since the previous
transition, in ticks:

| Bytes                    | Interval (ticks)                         | Range        |
|--------------------------|------------------------------------------|--------------|
| `b0` where `1 <= b0 <= 249` | `b0`                                  | 1..249       |
| `b0 b1` where `250 <= b0 <= 254`, `1 <= b1 <= 255` | `250 + (b0 - 250) * 255 + b1 - 1` | 250..1524 |
| `FF 02 N28 F9`           | `N28 + 249` (long interval)              | 1525..2^28+248 |

An interval of 0 ticks is not encoded and produces no transition.

The second byte `b1` of a two-byte interval can be `0xFF`. A reader MUST
therefore parse the stream sequentially, interval by interval. It MUST
NOT find opcode escapes or markers by scanning for `0xFF` bytes.

### 5.2. Opcode Escapes

A `0xFF` byte in the lead position introduces an opcode. The byte after it
selects the opcode:

| Bytes                   | Name          | Meaning |
|-------------------------|---------------|---------|
| `FF 01 N28`             | INDEX         | A hardware INDEX pulse, `N28` ticks after the sample cursor: the previous flux transition plus any dead-time SPACE since it. 6 bytes. |
| `FF 02 N28 F9`          | SPACE + 249   | A long interval (Section 5.1). 7 bytes. |
| `FF 02 N28` (next byte not `F9`) | SPACE (dead time) | `N28` ticks of no flux. Added to the next interval; not a transition. 6 bytes. |
| `FF F0`..`FF F4` ...    | Marker        | A Tapewyrm marker (Section 6). |
| `FF 03`                 | (ASTABLE)     | Greaseweazle write-only opcode. MUST NOT appear in a TWRF body. |
| `FF` other              | (reserved)    | MUST NOT appear. |

The firmware emits a dead-time SPACE (200 µs) whenever no transition has
arrived for 400 µs, so long silences (gaps, end of tape) appear as runs of
dead-time SPACEs.

A dead-time SPACE followed by a genuine 249-tick interval is byte-identical
to a long interval. Both decode to the same total time, so interval timing
is unaffected; only the END byte accounting (Section 7.2) can disagree in
that case. This ambiguity is inherited from [GW].

On tape, INDEX pulses during Logical Forward mark segment starts (one per
segment found). A ready drive also pulses INDEX about every 3 ms as a
"cue" until motion takes over; readers that count segments SHOULD ignore
INDEX pulses in the first 50 ms of the stream.

### 5.3. N28 Encoding

`N28` packs a 28-bit unsigned value `x` into 4 bytes, 7 bits per byte,
least significant group first, with bit 0 of every byte set to 1 (so no
`N28` byte is ever `0x00`):

```text
byte 0 = 1 | ((x <<  1) & 0xFE)
byte 1 = 1 | ((x >>  6) & 0xFE)
byte 2 = 1 | ((x >> 13) & 0xFE)
byte 3 = 1 | ((x >> 20) & 0xFE)

x = (b0 >> 1) | ((b1 & 0xFE) << 6) | ((b2 & 0xFE) << 13) | ((b3 & 0xFE) << 20)
```

### 5.4. Stream Terminator

A lead byte of `0x00` ends the stream (the Greaseweazle end-of-stream
NUL). The firmware sends it after the END marker when it drains a
finished capture. Bytes after the terminator, if any, are not part of the
stream.

### 5.5. Decoding Algorithm

Informative pseudocode of a conforming body parser (it matches the
reference parser, `tapewyrm_archive.twrf.parse_body`, which `tw` also
uses as `tapewyrm.codec.gwstream.parse`):

```text
i = 0; pending = 0; t = 0
while i < len(body):
    c = body[i]
    if c == 0x00:                      stop (terminated)
    elif c <= 249:                     emit(pending + c); i += 1
    elif c <= 254:                     need 2 bytes, else stop (cut)
                                       emit(pending + 250 + (c-250)*255 + body[i+1] - 1); i += 2
    else:  # 0xFF
        need body[i+1], else stop (cut)
        op = body[i+1]
        if op in (01, 02):             need 6 bytes, else stop (cut)
        if op == 01:                   index at t + pending + N28(i+2); i += 6
        elif op == 02:
            if body[i+6] == 0xF9:      emit(pending + N28(i+2) + 249); i += 7
            else:                      pending += N28(i+2); i += 6
        elif 0xF0 <= op <= 0xF4:       len = body[i+2]; marker(op, body[i+3 : i+3+len])
                                       i += 3 + len
        else:                          error: unknown opcode
emit(v): append interval v; t += v; pending = 0
```

`pending` left over at the end is trailing dead time. `t + pending` is
Greaseweazle's sample cursor, which an INDEX's `N28` counts from
(`index.rdata_cnt - prev` in `rdata_encode_flux()`).

The reference reader's marker and integrity helpers (`iter_markers`,
`flux_data_only`, and `RawFluxCapture.markers`, `segments`, `end_marker`
and `verify`) are all built on this one parser. Versions before
2026-10-03 instead scanned for `0xFF` and assumed a `0xFF 0xFF` stuffing
rule that no device ever used; on real captures they misread two-byte
intervals ending in `0xFF` and counted INDEX/SPACE argument bytes as flux
data, so `verify` failed. Files were not affected, only those helpers.

## 6. Markers

### 6.1. Framing

```text
+------+------+------+---------------------------+
| 0xFF | code | len  |  payload (len bytes)      |
+------+------+------+---------------------------+
   1      1      1          0..255
```

`code` is one of the marker codes below. `len` is the payload length, u8.
Payloads are little-endian. A reader MUST skip exactly `len` bytes of
payload, whatever the code's nominal payload length, so that a longer
payload from a later firmware does not desynchronise the parse. A reader
MUST treat a payload shorter than the nominal length as undecodable and
SHOULD ignore that marker's fields.

Marker codes are defined in `protocol/protocol.toml` [TW-PROTO], from
which the firmware's `protocol.h` and the host's `link/protocol.py` are
generated; the archive package keeps its own copy (`WireMarker`) and the
test suite asserts they are identical.

| Code   | Name          | Nominal payload | Emitted by current firmware |
|--------|---------------|----------------:|-----------------------------|
| `0xF0` | SESSION_START | 11              | Yes, once, first            |
| `0xF1` | SEGMENT       | 8               | Yes, one per INDEX pulse    |
| `0xF2` | EVENT         | 1               | Yes (MOTION_STARTED only)   |
| `0xF3` | END           | 13              | Yes, once, last             |
| `0xF4` | HEARTBEAT     | 0               | No (reserved, optional)     |

Marker bytes are not flux. They are excluded from the END accounting
(Section 7.2).

### 6.2. SESSION_START (0xF0)

```text
 0                   1                   2                   3
 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|          rate (u16)           |       clock (u32, low)        |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|      clock (u32, high)        |           tpt (u16)           |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|  direction    |         pass_id (u16)         |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
```

| Offset | Size | Field       | Meaning |
|-------:|-----:|-------------|---------|
| 0      | 2    | `rate`      | Rate in kbit/s, as passed by the host to CAPTURE |
| 2      | 4    | `clock`     | Sample clock, Hz |
| 6      | 2    | `tpt`       | Track number as passed by the host |
| 8      | 1    | `direction` | 0 forward, 1 reverse |
| 9      | 2    | `pass_id`   | Pass number as passed by the host |

`rate`, `tpt`, `direction` and `pass_id` echo the host's CAPTURE request
and SHOULD equal the header's `rate_kbps`, `track` (mod 2^16),
`direction` and `pass_id`. `clock` is the device's own value (Section
4.2, note 2). A reader needs only 6 payload bytes to read `clock`.

### 6.3. SEGMENT (0xF1)

| Offset | Size | Field   | Meaning |
|-------:|-----:|---------|---------|
| 0      | 4    | `ticks` | Ticks from the sample cursor (the previous flux transition, plus any dead-time SPACE since it) to this INDEX pulse. Not the time since the previous SEGMENT |
| 4      | 4    | `index` | Running INDEX count in this pass, starting at 1 |

The firmware emits one SEGMENT immediately after each INDEX opcode
(`FF 01 N28`), with `ticks` equal to that opcode's `N28` value. SEGMENT
therefore duplicates INDEX and adds a running count; a reader can use
either. The period between segments is the difference between
consecutive INDEX times (Section 5.5), not a SEGMENT field. Cue pulses (Section 5.2) also produce SEGMENT markers.

### 6.4. EVENT (0xF2)

| Offset | Size | Field  | Meaning |
|-------:|-----:|--------|---------|
| 0      | 1    | `code` | Event code |

| Code   | Name           | Meaning |
|--------|----------------|---------|
| `0x01` | MOTION_STARTED | The motion command was issued; emitted directly after SESSION_START |
| `0x02` | HOLE_EOT       | Reserved: EOT/BOT hole seen |
| `0x03` | OVERFLOW       | Reserved: buffer overflow |
| `0x04` | GAP            | Reserved: gap observed |

Readers MUST ignore unknown event codes.

### 6.5. END (0xF3)

| Offset | Size | Field        | Meaning |
|-------:|-----:|--------------|---------|
| 0      | 1    | `reason`     | Why the pass ended (below) |
| 1      | 4    | `flux_count` | Number of flux transitions encoded (Section 7.2) |
| 5      | 4    | `byte_count` | Number of flux data bytes (Section 7.2) |
| 9      | 4    | `checksum`   | Additive checksum of the flux data bytes (Section 7.2) |

| Reason | Name     | Meaning |
|--------|----------|---------|
| `0x00` | NORMAL   | A byte-budget capture reached its budget |
| `0x01` | ABORT    | Reserved: host abort |
| `0x02` | OVERFLOW | Reserved: device overflow |
| `0x03` | EOT      | Flux stopped for 1 s after at least one segment: the tape reached logical EOT (or halted). The normal end of a `tw dump` pass |
| `0x04` | USB_LOSS | Reserved: link lost |
| `0x05` | WATCHDOG | No segment was found within 15 s of arming |

The current firmware emits only NORMAL, EOT and WATCHDOG. An aborted,
overflowed or disconnected capture has no END at all (Section 7.3).

### 6.6. HEARTBEAT (0xF4)

Empty payload. A coarse keepalive across long stretches with no markers.
Defined for compatibility; the current firmware does not emit it. Readers
MUST accept and ignore it.

### 6.7. Marker Order

A complete pass from the current firmware has this shape:

```text
SESSION_START  EVENT(MOTION_STARTED)
{ flux intervals | dead-time SPACE | INDEX SEGMENT }*
END  NUL
```

SESSION_START and the MOTION_STARTED event precede all flux. END is the
last record before the terminator. Readers SHOULD NOT reject a stream
whose markers deviate from this shape; they SHOULD use the last END they
find.

## 7. End of Data and Integrity

### 7.1. Determining the End of Data

The flux body ends at the end of the file. Within it, the stream ends at
the first of:

1. a `0x00` lead byte (the terminator, Section 5.4): the normal end;
2. end of file in the middle of a record (a cut interval, opcode or
   marker): a truncated capture;
3. end of file on a record boundary with no terminator: a truncated
   capture.

The END marker seals the pass's accounting but is not itself the end of
the stream; the terminator follows it.

### 7.2. END Accounting and Checksum

The firmware counts, from SESSION_START onward:

- `flux_count`: the number of transitions encoded (one per one-byte,
  two-byte or long interval; not INDEX, not dead-time SPACE, not
  markers);
- `byte_count`: the number of bytes those intervals occupy in the stream:
  1 for a one-byte interval, 2 for a two-byte interval, 7 for a long
  interval (`FF 02 N28 F9`, all seven bytes);
- `checksum`: the sum of those same bytes, modulo 2^32.

INDEX opcodes, dead-time SPACE opcodes, markers and the terminator are not
counted.

```text
checksum = (sum of every flux data byte) & 0xFFFFFFFF
```

A capture verifies when a reader's own parse (Section 5.5) produces the
same `flux_count`, `byte_count` and `checksum` as the last END marker. The
checksum is an accounting check against lost or duplicated USB data, not
a cryptographic or error-correcting code.

### 7.3. Truncated Captures

A capture with no END marker is truncated: the device overflowed, the
host aborted it (probe captures stop this way), or the link dropped. A
truncated capture is not an error. Its flux up to the cut is valid and
decodes, because QIC sectors carry their own address and CRC; the
missing END only lowers confidence in the tail.

## 8. Processing Rules

### 8.1. Writer Requirements

1. A writer MUST write the preamble with magic `TWRF`, version `2`, and
   the exact byte length of the header.
2. A writer MUST write the header as a JSON object, UTF-8, containing
   every member of Section 4.2 with the JSON type given there. It SHOULD
   use the compact, ASCII-only serialization of Section 4.1.
3. A writer MUST NOT write members not defined by this document unless a
   later version of this document defines them.
4. A writer MUST append the device's flux stream byte-for-byte, without
   decoding, unstuffing, reordering or dropping any bytes, including
   markers and the terminator. It MAY write the stream in chunks as it
   arrives.
5. A writer MUST write one pass per file.
6. A writer SHOULD set `rate_kbps` from Report Drive Configuration
   (Section 4.3) and SHOULD record every report byte it can read.
7. A writer SHOULD name a Logical Forward track capture
   `track-NN.twrf`, where `NN` is the track number in decimal, zero-padded
   to at least two digits, and SHOULD place the captures of one tape in
   one directory.
8. A writer SHOULD keep a truncated capture rather than delete it.

### 8.2. Reader Requirements

1. A reader MUST reject a file shorter than 10 bytes or whose first four
   bytes are not `TWRF`. A reader MUST identify a TWRF file by this
   magic, never by its file name or suffix.
2. A reader MUST reject a file whose `version` it does not support.
   A reader conforming to this document MUST support version 2 and MUST
   reject every other version, version 1 included.
3. A reader MUST parse exactly `hlen` bytes as the header and MUST start
   the flux body at offset `10 + hlen`.
4. A reader MUST ignore header members it does not recognize.
5. A reader MUST reject a header that is not a JSON object, that lacks
   any member of Section 4.2, or whose member has a JSON type other than
   the one Section 4.2 gives. The error MUST name the file and SHOULD
   name the member. A reader MUST treat a `null` value as "not reported".
6. A reader MUST reject a header whose `direction` is not `"forward"` or
   `"reverse"`, or whose `tape_format` is not 0..4. It MUST NOT
   substitute a default for a missing or invalid member.
7. A reader MUST decode the body sequentially per Section 5.5 and MUST
   NOT locate markers by searching for `0xFF`.
8. A reader MUST NOT treat an unknown opcode as flux. It MAY stop or
   reject the file; the reference parser raises an error.
9. A reader MUST stop at the terminator and MUST accept a stream that
   ends without one, treating it as truncated.
10. A reader MUST NOT reject a truncated capture or one whose END
    accounting does not match. It SHOULD report the mismatch.
11. A reader MUST use `rate_kbps` for the data rate and SHOULD take the
    sample clock from SESSION_START (Section 4.2, note 2).
12. A reader SHOULD time-reverse the flux before decoding when
    `physical_reverse` is `true`, or refuse the file. The reference
    decoder currently does neither.

## 9. Versioning and Extensibility

`version` changes when a reader of the previous version could misread a
file of the new one, or when new header members are added. Readers keep
a list of readable versions (the reference reader: `(2,)`) and refuse
others.

- Version 1 had the twelve version 1 members of Section 4.2. It predates
  the first release and is not read; a version 1 capture is re-dumped.
- Version 2 added eight members, all nullable. The preamble and body are
  unchanged.

New header members MAY be added in a later version; readers ignore
members they do not know (Section 8.2, item 4). New marker codes MUST be
allocated in `protocol/protocol.toml`, SHOULD lie in `0xF0..0xFE`, and
MUST NOT collide with Greaseweazle opcodes (`0x01..0x03`). New EVENT codes and END
reasons MAY be added without a version change. A change to the body
encoding or to marker framing requires a new version.

## 10. Security and Privacy Considerations

**Privacy.** A TWRF file is a bit-exact image of a backup tape. It holds
everything the tape held: documents, mail, financial records, credentials,
personal photographs. Tapes recovered by Tapewyrm commonly come from
recyclers and estate lots, and belong to people who never agreed to their
being read. Implementations and users SHOULD treat every TWRF file as
personal data belonging to the tape's owner. They SHOULD NOT publish,
share or upload captures, or excerpts of their flux, decoded sectors,
file names or listings. Test fixtures and documentation examples SHOULD
be synthetic, as Appendix A is. Erasing a capture SHOULD be done with the
same care as the original medium. The header also holds a device serial
number and host and firmware commits, which identify the capturing
equipment.

**Untrusted sizes.** `hlen` is a u32 from the file. A reader SHOULD bound
it (a real header is under 1 KiB) and MUST NOT allocate more than the
file's remaining length for it. Marker `len` is a u8 and bounded. A
reader MUST bounds-check every multi-byte record against the end of the
body.

**JSON.** The header is untrusted input. Readers SHOULD use a JSON parser
with limits on nesting depth and number size, and MUST check member types
before use. Numbers in the header SHOULD be range-checked (for example,
`sample_clock_hz` of 0 would divide by zero when converting ticks to
time).

**Resource use.** A full QIC-80 track pass is roughly 60 MB of body and
tens of millions of intervals. Readers SHOULD stream rather than hold
every interval at once where memory is limited, and SHOULD bound the run
length of dead-time SPACEs they accumulate.

**Integrity.** The END checksum detects accidental loss only. It offers
no protection against deliberate modification.

## 11. IANA Considerations

This document has no IANA actions.

Informative: the file extension is `.twrf`. If a media type is wanted, the
unregistered `application/x-tapewyrm-twrf` is suggested; a registered name
would be `application/vnd.tapewyrm.twrf`. The magic number `TWRF` at
offset 0 identifies the format.

## 12. References

### 12.1. Normative References

[RFC2119]
: Bradner, S., "Key words for use in RFCs to Indicate Requirement
  Levels", BCP 14, RFC 2119, March 1997.

[RFC8174]
: Leiba, B., "Ambiguity of Uppercase vs Lowercase in RFC 2119 Key Words",
  BCP 14, RFC 8174, May 2017.

[RFC8259]
: Bray, T., Ed., "The JavaScript Object Notation (JSON) Data Interchange
  Format", STD 90, RFC 8259, December 2017.

[RFC3339]
: Klyne, G. and C. Newman, "Date and Time on the Internet: Timestamps",
  RFC 3339, July 2002.

[QIC-117]
: Quarter-Inch Cartridge Drive Standards, "QIC-117: Common Command Set
  Interface Specification for Flexible Disk Controller Based Minicartridge
  Tape Drives", Revision J. `docs/qic-standards/qic117j.pdf`.

[GW]
: Fraser, K., "Greaseweazle firmware", `CMD_READ_FLUX` stream encoding
  (`inc/cdc_acm_protocol.h`, `rdata_encode_flux()` in `src/floppy.c`),
  public domain. Vendored at `firmware/` (v1.6),
  <https://github.com/keirf/greaseweazle-firmware>.

[TW-PROTO]
: Tapewyrm, `protocol/protocol.toml`: wire-protocol opcodes, marker,
  event and END reason codes.

### 12.2. Informative References

[QIC-40]
: "QIC-40-MC: Serial Recorded Magnetic Tape Minicartridge for Information
  Interchange", Revision M. `docs/qic-standards/qic40m.pdf`.

[QIC-80]
: "QIC-80-MC: Serial Recorded Magnetic Tape Minicartridge for Information
  Interchange", Revision N. `docs/qic-standards/qic80n.pdf`.

[QIC-3010]
: "QIC-3010-MC", Revision H. `docs/qic-standards/qic3010h.pdf`.

[QIC-3020]
: "QIC-3020-MC", Revision H. `docs/qic-standards/qic3020h.pdf`.

[TWS-2]
: Tapewyrm contributors, "TWTI: the Tapewyrm Tape Image format (and
  TWTZ)", [twti.md](twti.md).

[TWS-3]
: Tapewyrm contributors, "TWVL: the Tapewyrm Volume format",
  [twvl.md](twvl.md).

[DESIGN]
: Tapewyrm, `docs/DESIGN.md`, Sections 5.4, 7.1 and 7.2.

## Appendix A. Example

A complete, synthetic version 2 file of 556 bytes, generated with the
reference writer (`tapewyrm_archive.twrf.write_preamble`) from made-up
data and checked with the reference parser (verified: 5 transitions, 6
data bytes, checksum `0x000004CA`). The header is the one in Section 4.4,
482 (`0x1E2`) bytes long, so the body starts at offset 492 (`0x1EC`).

Preamble and start of header:

```text
00000000: 54 57 52 46 02 00 e2 01 00 00 7b 22 72 61 74 65  TWRF......{"rate
          \---------/ \---/ \---------/ \-- header (482 bytes) ...
            magic     ver 2  hlen 482
...
000001e0: 69 72 74 79 22 3a 66 61 6c 73 65 7d              irty":false}
```

Body:

```text
offset  bytes                                 meaning
------  ------------------------------------  ------------------------------
0x1EC   ff f0 0b                              SESSION_START, len 11
0x1EF   f4 01                                   rate     = 500 kbit/s
0x1F1   00 a2 4a 04                             clock    = 72000000 Hz
0x1F5   04 00                                   tpt      = 4
0x1F7   00                                      direction= forward
0x1F8   01 00                                   pass_id  = 1
0x1FA   ff f2 01 01                           EVENT, len 1: MOTION_STARTED
0x1FE   90                                    interval 144 ticks (2.0 us)
0x1FF   d8                                    interval 216 ticks (3.0 us)
0x200   fb ff                                 interval 250+1*255+255-1 = 759
                                                (note second byte 0xFF)
0x202   ff 01 c9 01 01 01                     INDEX, N28 = 100 ticks after
                                                the last transition (t=1219)
0x208   ff f1 08                              SEGMENT, len 8
0x20B   64 00 00 00                             ticks = 100
0x20F   01 00 00 00                             index = 1
0x213   90                                    interval 144 ticks
0x214   ff 02 81 e1 01 01                     SPACE, N28 = 14400 (200 us),
                                                next byte not 0xF9: dead time
0x21A   d8                                    interval 14400 + 216 = 14616
0x21B   ff f3 0d                              END, len 13
0x21E   03                                      reason     = EOT
0x21F   05 00 00 00                             flux_count = 5
0x223   06 00 00 00                             byte_count = 6
0x227   ca 04 00 00                             checksum   = 0x4CA
0x22B   00                                    terminator
```

The flux data bytes are `90 d8 fb ff 90 d8`; their sum is 1226
(`0x4CA`). The intervals are 144, 216, 759, 144 and 14616 ticks.

The bytes `fb ff ff 01` at offset `0x200` show why Section 5.1 forbids
scanning for `0xFF`: the first `ff` belongs to a two-byte interval and the
second begins an INDEX opcode.

## Appendix B. The dump.jsonl Sidecar

`tw dump` appends one line to `dump.jsonl`, in the capture directory, after
each pass. Each line is a JSON object (JSON Lines). The file is a log, not
part of TWRF: readers of TWRF MUST NOT require it, and it is not
versioned. It is opened in append mode, so a directory dumped more than
once holds lines from every run.

| Member         | JSON type       | Meaning |
|----------------|-----------------|---------|
| `track`        | integer         | Track number of the pass |
| `path`         | string          | Path of the TWRF file as the writer named it |
| `bytes`        | integer         | Flux body length in bytes (excluding preamble and header) |
| `seconds`      | number          | Wall-clock duration of the pass, s, 1 decimal |
| `end_reason`   | string          | END reason name (Section 6.5), or `"none"` if no END |
| `verified`     | boolean         | The parse matched the END accounting (Section 7.2) |
| `tape_seconds` | number          | Sum of all intervals in seconds, 1 decimal |
| `index_pulses` | integer         | INDEX pulses after the 50 ms cue holdoff: segments found |
| `missing_est`  | integer         | Segments estimated missed from INDEX gaps longer than 1.5 x the median gap |
| `status_after` | integer         | Raw Report Drive Status after the pass |
| `error_after`  | integer or null | Latched QIC-117 error code after the pass, if any |
| `sectors`      | integer or null | With `--check`: sectors decoded; else `null` |
| `good`         | integer or null | With `--check`: sectors with ID and data CRC good |
| `segments`     | integer or null | With `--check`: distinct segments seen in sector IDs |

`tw dump` stops after a pass whose `end_reason` is not `EOT`, that does
not verify, whose `index_pulses` falls below 0.9 x the best earlier pass,
or whose `missing_est` exceeds 5 % of `index_pulses` (and, with `--check`,
whose good fraction is below 0.8). The line for that pass is still
written.

## Appendix C. Change Log

| Version | Date       | Change |
|--------:|------------|--------|
| 1       | 2026       | Initial format: preamble, twelve-member header, verbatim body. |
| 2       | 2026-10-01 | Header gains `drive_status`, `drive_config`, `drive_rom`, `drive_vendor_id`, `tape_status`, `tw_commit`, `firmware_commit`, `firmware_dirty` (all nullable). Layout unchanged. 2026-10-03, before release: every member is required and readers MUST reject a header lacking one (Section 8.2); version 1 is no longer read; headerless pre-TWRF streams are no longer accepted by any tool, and their appendix is removed. |

## Author's Address

Tapewyrm contributors
<https://github.com/indrora/tapewyrm>
