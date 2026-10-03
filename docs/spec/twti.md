# TWS-2: TWTI, the Tapewyrm Tape Image format, and TWTZ, its Zstandard-compressed form

```text
Tapewyrm Specification TWS-2
Title:     TWTI, the Tapewyrm Tape Image format, and TWTZ,
           its Zstandard-compressed form
Category:  Standards Track (Tapewyrm)
Status:    Draft
Date:      2026-10-03
Author:    Tapewyrm contributors
License:   The Unlicense (public domain)
```

## Abstract

This document specifies TWTI, the logical tape image that Tapewyrm builds
from raw flux captures of QIC-40, QIC-80, QIC-3010 and QIC-3020 floppy-interface
tapes, and TWTZ, the same byte stream compressed with Zstandard. A TWTI file
holds every segment of one tape cartridge in segment order, after sector
placement, bad-sector-map exclusion and Reed-Solomon correction, together
with a per-segment record of how much of the segment was recovered and a JSON
header carrying the tape's geometry, its format parameter record and the
provenance of the captures it was built from.

## Status of This Memo

This is a Tapewyrm project specification, not an IETF document. It borrows
the layout and conventions of an RFC for clarity only. It is normative for
Tapewyrm implementations: software that writes or reads TWTI or TWTZ files
conforms to this document.

## Table of Contents

- [1. Introduction](#1-introduction)
  - [1.1. Requirements Language](#11-requirements-language)
  - [1.2. Terminology](#12-terminology)
  - [1.3. Conventions](#13-conventions)
- [2. Format Overview](#2-format-overview)
- [3. Preamble](#3-preamble)
- [4. JSON Header](#4-json-header)
  - [4.1. Top-Level Object](#41-top-level-object)
  - [4.2. The geometry Object](#42-the-geometry-object)
  - [4.3. The qic80_header Object](#43-the-qic80_header-object)
  - [4.4. The drive Object](#44-the-drive-object)
  - [4.5. The sources Array](#45-the-sources-array)
  - [4.6. Complete Header Example](#46-complete-header-example)
- [5. Segment Table](#5-segment-table)
  - [5.1. Entry Layout](#51-entry-layout)
  - [5.2. Segment States](#52-segment-states)
  - [5.3. Excluded Sectors and Expected Length](#53-excluded-sectors-and-expected-length)
- [6. Segment Data Area](#6-segment-data-area)
  - [6.1. Locating a Segment](#61-locating-a-segment)
  - [6.2. Slot Contents](#62-slot-contents)
  - [6.3. Sparse Files](#63-sparse-files)
- [7. TWTZ: Zstandard-Compressed TWTI](#7-twtz-zstandard-compressed-twti)
- [8. Format Identification](#8-format-identification)
- [9. Processing Rules](#9-processing-rules)
  - [9.1. Writer Requirements](#91-writer-requirements)
  - [9.2. Reader Requirements](#92-reader-requirements)
- [10. Versioning and Extensibility](#10-versioning-and-extensibility)
- [11. Security and Privacy Considerations](#11-security-and-privacy-considerations)
  - [11.1. Privacy](#111-privacy)
  - [11.2. Malformed Input](#112-malformed-input)
  - [11.3. Decompression Bombs](#113-decompression-bombs)
  - [11.4. Temporary Files](#114-temporary-files)
- [12. IANA Considerations](#12-iana-considerations)
- [13. References](#13-references)
  - [13.1. Normative References](#131-normative-references)
  - [13.2. Informative References](#132-informative-references)
- [Appendix A. Examples](#appendix-a-examples)
- [Appendix B. Change Log](#appendix-b-change-log)
- [Author's Address](#authors-address)

## 1. Introduction

Tapewyrm recovers floppy-interface QIC tapes through a Greaseweazle in four
steps:

```text
tw dump -> TWRF -> tw convert -> TWTI/TWTZ -> qicsilver extract -> TWVL
        [TWS-1]                  [TWS-2]                        [TWS-3]
```

`tw dump` records each tape track as raw flux in a TWRF file [TWS-1].
`tw convert` decodes one or more TWRF captures to sectors, merges them,
locates the header segment, places every sector by its own address under the
header's geometry, applies the bad-sector map, Reed-Solomon corrects each
segment and writes a TWTI image (this document). `qicsilver extract` reads
the image and writes one TWVL file [TWS-3] per volume.

A TWTI image is the tape after the QIC layer: it knows segments, sectors and
the bad-sector map, but nothing about backup formats. Its fixed-stride data
area gives random access by segment number. TWTZ is a TWTI byte stream passed
through one Zstandard compressor, for moving and archiving images.

### 1.1. Requirements Language

The key words "MUST", "MUST NOT", "REQUIRED", "SHALL", "SHALL NOT",
"SHOULD", "SHOULD NOT", "RECOMMENDED", "NOT RECOMMENDED", "MAY", and
"OPTIONAL" in this document are to be interpreted as described in BCP 14
[RFC2119] [RFC8174] when, and only when, they appear in all capitals, as
shown here.

### 1.2. Terminology

Sector:
: 1024 bytes of user-visible data addressed by an MFM sector ID (FSD, FTK,
  FSC) [QIC-80].

Segment:
: 32 consecutive sectors, the unit of Reed-Solomon error correction: 29 data
  sectors and 3 ECC sectors when no sector is excluded [QIC-80].

Slot:
: A sector position within a segment, 0 through 31, equal to
  `(FSC - 1) mod 32`.

Segment number (SEG):
: The absolute logical segment number, counted from 0 at the start of tape
  track 0. `SEG = TPT * segments_per_track + TPS`.

Track, TPT:
: A tape track (not a floppy track), 0-based. Even tracks are recorded
  forward, odd tracks in reverse.

TPS:
: A segment's index within its tape track.

FSD, FTK, FSC:
: Floppy side, floppy track and floppy sector of a sector ID, the
  floppy-controller coordinates the tape drive emulates [QIC-80].

Header segment:
: The segment holding the format parameter record and bad-sector map
  [QIC-80] Section 7.1. A tape carries two copies.

Format parameter record (FPR):
: The first 256 bytes of sector 0 of the header segment.

Bad-sector map (BSM):
: The list in the header segment of sectors and whole segments that the
  formatter found unusable. Excluded sectors are not part of the segment's
  codeword or data.

Erasure:
: A sector of a segment's codeword that was not read with a good CRC and that
  Reed-Solomon decoding must rebuild.

Image:
: A TWTI file, or the TWTI byte stream inside a TWTZ file.

Writer, reader:
: Software that produces, respectively consumes, an image.

### 1.3. Conventions

- All multi-byte integers are unsigned and little-endian.
- Byte offsets are 0-based from the first byte of the TWTI byte stream.
- `u8`, `u16` and `u32` denote unsigned integers of 1, 2 and 4 bytes.
- KiB is 1024 bytes. MB is 10^6 bytes.
- Hexadecimal values are written `0x1F` or as space-separated byte pairs
  (`28 B5 2F FD`).
- In the bit-ruler diagrams, the ruler numbers bit positions across a
  32-bit row so as to show byte positions; it does not imply big-endian
  ("network") byte order. Every multi-byte field is little-endian.

## 2. Format Overview

A TWTI byte stream is four regions, back to back, with no padding or
alignment between them:

```text
+---------------------------+  offset 0
| Preamble (10 bytes)       |  Section 3
+---------------------------+  offset 10
| JSON header               |  Section 4
| (header_len bytes)        |
+---------------------------+  T = 10 + header_len
| Segment table             |  Section 5
| (segment_count x 8 bytes) |
+---------------------------+  D = T + 8 * segment_count
| Segment data area         |  Section 6
| (segment_count x 29696)   |
+---------------------------+  L = D + 29696 * segment_count
```

`L` is the total length of a well-formed TWTI byte stream. Entry `n` of the
segment table and slot `n` of the data area both describe segment number `n`.

## 3. Preamble

```text
 0                   1                   2                   3
 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|   0x54 'T'    |   0x57 'W'    |   0x54 'T'    |   0x49 'I'    |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|        version (u16)          |  header_len (u32, bytes 0-1)  |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
| header_len (u32, bytes 2-3)   |  JSON header ...
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
```

| Offset | Length | Field        | Value                                      |
|-------:|-------:|--------------|--------------------------------------------|
| 0      | 4      | magic        | ASCII `TWTI` (`54 57 54 49`)               |
| 4      | 2      | version      | Format version. This document defines 1.   |
| 6      | 4      | header_len   | Length in bytes of the JSON header.        |
| 10     | -      | header       | `header_len` bytes, Section 4.             |

## 4. JSON Header

The header is a single JSON object [RFC8259] encoded as UTF-8, occupying
exactly `header_len` bytes. It has no terminator and no padding. Whitespace
inside it is insignificant; the reference writer indents with one space per
level and emits ASCII only (non-ASCII characters escaped as `\uXXXX`).

Integers in the header are JSON numbers without fraction or exponent.
Members with "or null" in their type MAY be `null`, meaning "not known".

### 4.1. Top-Level Object

| Member         | JSON type      | Req.     | Description |
|----------------|----------------|----------|-------------|
| `format`       | string         | REQUIRED | The literal `"TWTI"`. Also `"TWTI"` inside a TWTZ file. |
| `version`      | integer        | REQUIRED | Equal to the preamble `version` (1). |
| `segment_count`| integer        | REQUIRED | Number of segment table entries and data slots, `N`. Equal to `geometry.tracks * geometry.segments_per_track` when `geometry` is present. |
| `segment_stride`| integer       | REQUIRED | Bytes per data slot. MUST be 29696 in version 1. |
| `geometry`     | object         | REQUIRED | The geometry used to place sectors, Section 4.2. |
| `qic80_header` | object         | REQUIRED | The decoded format parameter record of the header segment, Section 4.3. |
| `created`      | string         | OPTIONAL | When the image was written: an RFC 3339 [RFC3339] `date-time` in UTC with whole seconds, e.g. `"2026-10-03T12:00:00+00:00"`. |
| `tw_commit`    | string or null | OPTIONAL | Identifier of the converter build that wrote the image; for the reference implementation, a 40-character hexadecimal git commit. `null` when unknown. |
| `drive`        | object         | OPTIONAL | The tape drive's QIC-117 reports, Section 4.4. |
| `sources`      | array          | OPTIONAL | One object per capture the image was built from, in the order merged, Section 4.5. |

### 4.2. The geometry Object

The geometry under which sectors were placed. The reference writer derives
it from the format parameter record of the header segment.

| Member               | JSON type | Req.     | Description |
|----------------------|-----------|----------|-------------|
| `tracks`             | integer   | REQUIRED | Tape tracks on the cartridge. |
| `segments_per_track` | integer   | REQUIRED | Segments per tape track. |
| `sectors_per_segment`| integer   | REQUIRED | Sectors per segment; 32. |
| `ftk_per_side`       | integer   | REQUIRED | Floppy tracks per floppy side, the FPR's maximum floppy track plus one (255 for format code 4 [QIC-80]; 150 is observed for format code 5). Used as `SEG = 4 * (ftk_per_side * FSD + FTK) + (FSC - 1) div 32`. |

### 4.3. The qic80_header Object

The format parameter record (FPR) of the header segment as decoded by the
writer. Offsets are into sector 0 of the header segment, per [QIC-80]
Section 7.1; multi-byte fields on tape are little-endian. Packed dates are
stored as the raw 32-bit value from tape, undecoded. All members are
REQUIRED in images written by the reference writer; readers MUST tolerate
the absence of any member other than those they need (Section 9.2).

| Member               | JSON type | FPR offset | Description |
|----------------------|-----------|-----------:|-------------|
| `format_code`        | integer   | 4          | QIC format code (e.g. 4 = QIC-80 variable format; 2, 3, 5 = fixed formats). |
| `segments_per_track` | integer   | 24-25      | Segments per tape track. |
| `tracks`             | integer   | 26         | Tape tracks. |
| `max_fsd`            | integer   | 27         | Maximum floppy side. |
| `max_ftk`            | integer   | 28         | Maximum floppy track. |
| `max_fsc`            | integer   | 29         | Maximum floppy sector. |
| `tape_name`          | string    | 30-73      | Tape name, decoded as ASCII. |
| `format_date`        | integer   | 14-17      | Packed date of the most recent format. |
| `valid_signature`    | boolean   | 0-3        | True when bytes 0-3 are `55 AA 55 AA`. |
| `revision`           | integer   | 5          | QIC-80 revision byte (`0x0D` = Rev M, `0x0C` = Rev L, 0 = earlier). |
| `header_seg`         | integer   | 6-7        | Segment number of the header segment. |
| `dup_header_seg`     | integer   | 8-9        | Segment number of the duplicate header segment. |
| `first_data_seg`     | integer   | 10-11      | First data segment of the logical area (holds the volume table). |
| `last_data_seg`      | integer   | 12-13      | Last data segment of the logical area. |
| `write_date`         | integer   | 18-21      | Packed date of the most recent write or format. |
| `name_date`          | integer   | 74-77      | Packed date the tape name was written. |
| `reformat_error`     | boolean   | 128        | True when byte 128 is `0xFF` (fields lost to a re-format error). |
| `segments_written`   | integer   | 130-133    | Lifetime count of segments written, formatted or verified. |
| `initial_format_date`| integer   | 138-141    | Packed date of the first format. |
| `format_count`       | integer   | 142-143    | Number of times the tape was formatted. |
| `manufacturer`       | string    | 146-189    | Original manufacturer name or code (pre-formatted tapes; else empty). |
| `lot_code`           | string    | 190-233    | Original manufacturer lot code (pre-formatted tapes; else empty). |

These members are a convenience copy. The authoritative record is the header
segment's own data in the data area; a reader that needs the bad-sector map
MUST parse that segment, because the header does not carry the map.

### 4.4. The drive Object

The raw QIC-117 [QIC-117] report bytes of the drive that read the tape,
all eight members copied from the TWRF header (Section 4.5) of the first
source capture whose drive reported: one in which at least one of
`drive_status`, `drive_config`, `drive_rom`, `drive_vendor_id` and
`tape_status` is non-null. The presence of a member in a TWRF header is not a
report, since a TWRF v2 header carries every member and stores `null` for a
report the drive did not answer.
When present, all eight members are present; a value is `null` when the
drive did not report it or no capture recorded it, and all are `null` when no
source reported.

When two reporting sources disagree on `device_serial`, `drive_vendor_id` or
`tape_status` (both values non-null and non-empty), the captures came from
different capture devices, drives or cartridges. A writer keeps the first
reporting source and SHOULD warn its user. It records nothing further here:
every capture's reports remain in `sources`, from which a reader can detect
the disagreement itself.

| Member            | JSON type       | Description |
|-------------------|-----------------|-------------|
| `device_serial`   | string or null  | Serial number of the capture device (the Greaseweazle); `""` when unknown. |
| `drive_status`    | integer or null | Report Drive Status (command 6), 8 bits: bit 0 ready, 1 error, 2 cartridge present, 3 write protect, 4 new cartridge, 5 referenced, 6 at BOT, 7 at EOT. |
| `drive_config`    | integer or null | Report Drive Configuration (command 8), 8 bits: bits 3-4 rate (`00` 250 kbit/s or 4 Mbit/s by drive type, `01` 2 Mbit/s, `10` 500 kbit/s, `11` 1 Mbit/s), bit 6 extra length, bit 7 QIC-80 mode. |
| `drive_rom`       | integer or null | Report ROM Version (command 9). |
| `drive_vendor_id` | integer or null | Report Vendor ID (command 32), 16 bits: bits 6-15 make, bits 0-5 model; some drives report a legacy whole-word ID instead (e.g. 71). |
| `tape_status`     | integer or null | Report Tape Status (command 33), 8 bits: bits 0-3 format (1 QIC-40, 2 QIC-80, 3 QIC-3020, 4 QIC-3010), bits 4-6 tape type, bit 7 wide tape. |
| `rate_kbps`       | integer or null | Transfer rate of the capture, kbit/s. |
| `firmware_commit` | string or null  | Build identifier of the capture device's firmware. |

### 4.5. The sources Array

Each element describes one capture file the image was built from.

| Member     | JSON type | Req.     | Description |
|------------|-----------|----------|-------------|
| `file`     | string    | REQUIRED | The capture file, as the name of the directory that held it, `/`, and its file name (e.g. `jc-1998/track-00.twrf` for `/Users/x/captures/jc-1998/track-00.twrf`), or the bare file name when it was given without a directory. Never an absolute path, and nothing above that one directory (Section 11.1). |
| `verified` | boolean   | REQUIRED | True when the capture's flux stream parsed in agreement with its END marker [TWS-1]. |
| `sectors`  | integer   | REQUIRED | Sectors the converter recovered from this capture. |
| `twrf`     | object    | REQUIRED | The capture's TWRF header [TWS-1], every member as stored there. |

The members of `twrf` are defined by [TWS-1], not by this document.

### 4.6. Complete Header Example

The header of the synthetic image in Appendix A, byte for byte (1757 bytes):

```json
{
 "format": "TWTI",
 "version": 1,
 "created": "2026-10-03T12:00:00+00:00",
 "tw_commit": "0000000000000000000000000000000000000000",
 "segment_count": 4,
 "segment_stride": 29696,
 "geometry": {
  "tracks": 2,
  "segments_per_track": 2,
  "sectors_per_segment": 32,
  "ftk_per_side": 255
 },
 "qic80_header": {
  "format_code": 4,
  "segments_per_track": 2,
  "tracks": 2,
  "max_fsd": 0,
  "max_ftk": 0,
  "max_fsc": 128,
  "tape_name": "TWS-2 SYNTHETIC EXAMPLE",
  "format_date": 0,
  "valid_signature": true,
  "revision": 13,
  "header_seg": 0,
  "dup_header_seg": 1,
  "first_data_seg": 2,
  "last_data_seg": 3,
  "write_date": 0,
  "name_date": 0,
  "reformat_error": false,
  "segments_written": 0,
  "initial_format_date": 0,
  "format_count": 0,
  "manufacturer": "",
  "lot_code": ""
 },
 "drive": {
  "device_serial": "EXAMPLE0",
  "drive_status": 37,
  "drive_config": 144,
  "drive_rom": 106,
  "drive_vendor_id": 0,
  "tape_status": 18,
  "rate_kbps": 500,
  "firmware_commit": "1111111111111111111111111111111111111111"
 },
 "sources": [
  {
   "file": "example/track-00.twrf",
   "verified": true,
   "sectors": 128,
   "twrf": {
    "rate_kbps": 500,
    "sample_clock_hz": 72000000,
    "track": 0,
    "direction": "forward",
    "pass_id": 1,
    "utc": "2026-10-03T12:00:00+00:00",
    "tape_format": 2,
    "segments_per_track": 2,
    "tracks": 2,
    "sectors_per_segment": 32,
    "device_serial": "EXAMPLE0",
    "physical_reverse": false,
    "drive_status": 37,
    "drive_config": 144,
    "drive_rom": 106,
    "drive_vendor_id": 0,
    "tape_status": 18,
    "tw_commit": "0000000000000000000000000000000000000000",
    "firmware_commit": "1111111111111111111111111111111111111111",
    "firmware_dirty": false
   }
  }
 ]
}
```

## 5. Segment Table

The segment table starts at offset `T = 10 + header_len` and holds
`segment_count` entries of 8 bytes. Entry `n` is at `T + 8 * n` and describes
segment number `n`.

### 5.1. Entry Layout

```text
 0                   1                   2                   3
 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|  state (u8)   | erasures (u8) |        data_len (u16)         |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                     excluded_mask (u32)                       |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
```

| Offset | Length | Field         | Description |
|-------:|-------:|---------------|-------------|
| 0      | 1      | state         | Segment state, Section 5.2. |
| 1      | 1      | erasures      | Number of erasures in the segment's codeword: sectors Reed-Solomon rebuilt (CORRECTED) or could not (UNCORRECTABLE). 0 otherwise. |
| 2      | 2      | data_len      | Bytes of segment data stored at the start of the segment's slot. 0 to 29696. |
| 4      | 4      | excluded_mask | Bit `k` (value `1 << k`) set when the bad-sector map excludes slot `k` of this segment, `0 <= k <= 31`. |

### 5.2. Segment States

| Value | Name          | Meaning | `erasures` | `data_len` |
|------:|---------------|---------|------------|------------|
| 0     | MISSING       | No sector of the segment was read. | 0 | 0 |
| 1     | CLEAN         | Every sector of the codeword was read with a good CRC. | 0 | expected length |
| 2     | CORRECTED     | Reed-Solomon rebuilt 1 to 3 erased sectors; the data is corrected. | 1-3 | expected length |
| 3     | UNCORRECTABLE | More than 3 sectors of the codeword are bad or missing. The data is what was read, unreadable sectors zero-filled, uncorrected. | > 3 | as read |
| 4     | BAD           | The bad-sector map marks the whole segment unusable; the recording software skipped it. | 0 | 0 |
| 5-255 | -             | Reserved. | - | - |

"Expected length" is defined in Section 5.3.

Data rules:

- CLEAN and CORRECTED segments hold trustworthy data, `data_len` bytes.
- UNCORRECTABLE data MAY be partial or wrong. Readers MUST NOT present it as
  recovered data without saying so. The reference reader (`qicsilver
  extract`) treats an UNCORRECTABLE segment as lost and leaves a hole of the
  expected length in the volume stream.
- MISSING and BAD segments hold no data; `data_len` MUST be 0 and the slot
  MUST read as zeros.
- A BAD segment contributes no bytes to a volume's byte stream, because the
  writing software never wrote to it. A MISSING segment occupies its
  expected length.
- For BAD entries writers MUST set `excluded_mask` to 0 and readers MUST
  ignore it.

### 5.3. Excluded Sectors and Expected Length

The bad-sector map excludes individual sectors. An excluded sector is not
part of the segment's codeword: the remaining `32 - x` sectors (where `x` is
the number of excluded slots) form the codeword, whose last 3 are ECC. The
segment's data is therefore `29 - x` sectors, and its expected length is:

```text
expected_len = max(0, 32 - popcount(excluded_mask) - 3) * 1024
```

Writers MUST record the bad-sector map's mask for every non-BAD segment,
including MISSING ones: the map describes the tape, not what was read of it,
and it is the only way a reader can size the hole a MISSING segment leaves.
For CLEAN and CORRECTED segments `data_len` equals `expected_len`.

Images written before this rule (MISSING entries with mask 0) exist; readers
computing `expected_len` for them get 29696, which overstates the size of a
MISSING segment that had excluded sectors.

## 6. Segment Data Area

### 6.1. Locating a Segment

```text
SEGMENT_STRIDE = 29696                       (29 * 1024)
D              = 10 + header_len + 8 * segment_count
slot_offset(n) = D + n * SEGMENT_STRIDE      0 <= n < segment_count
data(n)        = bytes [slot_offset(n), slot_offset(n) + data_len(n))
```

The fixed stride gives random access: a reader MUST locate segment `n` by
this formula alone. SEGMENT_STRIDE is the size of a segment with no excluded
sectors (29 data sectors of 1024 bytes), so every segment's data fits its
slot.

### 6.2. Slot Contents

A slot holds the segment's data sectors, in slot order, skipping excluded
slots and the 3 ECC sectors, concatenated, followed by zero padding to
SEGMENT_STRIDE. Bytes `data_len` through 29695 of every slot MUST be zero.
The slots of MISSING and BAD segments are entirely zero.

### 6.3. Sparse Files

Most slots of a typical image are zero: a QIC-3020 tape has some 59,000
segments, so one captured track still makes an image of about 1.75 GB.

- A TWTI file MAY be a sparse file. Holes MUST read as zero bytes; a sparse
  and a dense TWTI file with the same content are the same image.
- Writers writing to a seekable file SHOULD leave every slot that is empty or
  entirely zero unwritten, and SHOULD then set the file length to `L`
  (Section 2) explicitly, since seeking past the end of a file does not
  extend it.
- Readers MUST NOT depend on whether a file is sparse.

Whether holes save space depends on the filesystem. NTFS makes holes only in
files flagged sparse; APFS has been observed to allocate fully files of 16 MiB
or less that have interior holes. Neither changes the bytes read.

## 7. TWTZ: Zstandard-Compressed TWTI

A TWTZ file is an entire TWTI byte stream (Sections 3 to 6, including every
zero-padded slot) compressed as Zstandard [RFC8878] data, exactly as
`.tar.zst` is to `.tar`. Consequently:

- A TWTZ file MUST consist of one or more Zstandard frames whose concatenated
  decompressed content is a complete TWTI byte stream of version 1 or later.
  Writers SHOULD produce a single frame.
- A TWTZ file MUST begin with a Zstandard frame magic number, the bytes
  `28 B5 2F FD` (0xFD2FB528 little-endian, [RFC8878] Section 3.1.1). Writers
  MUST NOT begin a TWTZ file with a skippable frame.
- `zstd -d image.twtz` MUST yield a valid TWTI file, and `zstd image.twti`
  MUST yield a valid TWTZ file.
- A reader identifies TWTZ by the zstd magic and MUST then find the TWTI
  preamble (Section 3) at the start of the decompressed stream; if it does
  not, the file is not a tape image and MUST be rejected.
- A TWTZ whose stream ends before a frame is complete is truncated and
  MUST be rejected, whatever it has decompressed so far. A complete stream
  that decompresses to fewer than `L` bytes holds a truncated TWTI and MUST
  be rejected likewise (Section 9.2, item 9); lengths in that check count
  decompressed bytes.

Zstandard streams do not support random access. A reader that needs random
access SHOULD decompress the stream once, sequentially, to temporary storage
and read that as a TWTI file. The reference reader decompresses to a
temporary sparse TWTI file, skipping all-zero 4 KiB blocks, and deletes it
when the image is closed.

The reference writer chooses TWTZ when the output file name ends in `.twtz`
(case-insensitive) and TWTI otherwise; that choice concerns only the writer
and does not bind readers (Section 8).

## 8. Format Identification

Readers MUST identify the format by the first four bytes of the file, never
by its file name or extension:

| First 4 bytes   | Format |
|-----------------|--------|
| `54 57 54 49`   | TWTI   |
| `28 B5 2F FD`   | TWTZ   |
| anything else   | not an image |

A renamed image MUST still open. A file name ending in `.twtz` that holds a
TWTI byte stream is a TWTI file, and the reverse.

## 9. Processing Rules

### 9.1. Writer Requirements

1. Writers MUST write the preamble with magic `TWTI`, version 1 and the exact
   byte length of the header.
2. The header MUST be a JSON object [RFC8259] in UTF-8 containing every
   member marked REQUIRED in Section 4. Writers SHOULD emit ASCII only.
3. `segment_count` MUST equal the number of table entries and data slots.
   `segment_stride` MUST be 29696.
4. Writers MUST write one table entry per segment, in segment order, with no
   gap between the header and the table or between the table and the data.
5. Writers MUST NOT write state values other than 0 to 4.
6. `data_len` MUST NOT exceed 29696. For CLEAN and CORRECTED segments it MUST
   equal the expected length (Section 5.3). For MISSING and BAD segments it
   MUST be 0.
7. Writers MUST record the bad-sector map's exclusions in `excluded_mask` for
   every segment other than BAD ones, including MISSING segments.
8. Every byte of a slot past `data_len` MUST be zero.
9. Writers MUST NOT write bytes after offset `L`. A sparse writer MUST set the
   file length to `L`.
10. Writers SHOULD leave all-zero slots unwritten when writing a TWTI file to
    a seekable file (Section 6.3).
11. A TWTZ writer MUST compress the entire TWTI byte stream, padding included
    (a compressor cannot seek over holes).
12. Writers MUST record each `sources[].file` as Section 4.5 describes: the
    name of the capture's directory and its file name only, derived from the
    path as given without resolving it, so that neither an absolute path nor
    the current directory's name is ever written.

### 9.2. Reader Requirements

1. Readers MUST identify the format by magic (Section 8).
2. Readers MUST reject a preamble whose version they do not implement
   (Section 10).
3. Readers MUST parse the header as JSON [RFC8259] and MUST reject a header
   that is not a JSON object, lacks `segment_count`, has a `segment_count`
   that is not a non-negative integer, or has a `format` member other than
   `"TWTI"`.
4. Readers MUST ignore header members they do not recognise, at every level
   of nesting.
5. Readers SHOULD NOT require OPTIONAL members, and a reader that needs only
   segment data (random access) needs only `segment_count`.
6. Readers MUST reject an image whose `segment_stride` member is present
   and not 29696. They MUST NOT use any stride other than 29696 for
   version 1.
7. Readers MUST reject an entry with a reserved state value, or MUST treat
   that segment as MISSING; they MUST NOT treat its data as recovered.
8. Readers MUST reject an entry whose `data_len` exceeds 29696, since its data
   would overlap the next slot.
9. Readers MUST reject a truncated file: one shorter than the 10-byte
   preamble, than `T` (the header), than `D` (the segment table) or than
   `L` (the data area), Section 2. They MUST check these lengths, in that
   order, before trusting `header_len` or `segment_count`, and before
   building any per-segment structure. The error MUST name the file and
   SHOULD say which part is cut short, the length that part needs and the
   length found. Readers MUST NOT open a truncated image partially or
   present any of it as segment data: a file cut short is a failed copy,
   and the remedy is to copy or convert it again. Truncation is a matter of
   the file's length only. A sparse file's holes count toward its length,
   so a sparse file whose length is `L` is complete however little of it
   is allocated on disk (Section 6.3).
10. Readers MUST treat holes in a sparse file as zeros (Section 6.3).
11. Readers SHOULD use the expected length (Section 5.3), not `data_len`, to
    size the gap that a MISSING or UNCORRECTABLE segment leaves in a stream of
    segment data.
12. Readers MAY ignore bytes after offset `L`.
13. A reader that uses a member of `qic80_header` (for example
    `first_data_seg`, to find the volume table, or `tape_name`) MUST reject
    the image when it needs that member and it is absent, when the member
    has the wrong JSON type (Section 11.2), or when a segment number is not
    in the range 0 to `segment_count - 1`. The error MUST name the file and
    the member. The check is made where the member is used, not when the
    image is opened: by rule 5 and Section 4.3, an image that lacks a member
    remains readable by readers that do not use it.

## 10. Versioning and Extensibility

The preamble `version` changes only for changes that a version 1 reader
would misread: a different preamble, entry layout, stride or state meaning.
The header's `version` member repeats it. Readers MUST refuse versions they
do not implement rather than guess.

The JSON header is the extension point. New members MAY be added without a
version change; readers ignore what they do not know (Section 9.2). New
members MUST NOT change the meaning of existing ones. Reserved state values
(5 to 255) MAY be assigned only by a new version of this document.

## 11. Security and Privacy Considerations

### 11.1. Privacy

The tapes Tapewyrm reads are other people's backups. The development
cartridges came from a PC recycler; any tape may hold personal documents,
mail, financial records, credentials and photographs of people who never
agreed to their recovery. A TWTI or TWTZ file holds that data, readable with
nothing but this document.

- Implementations and users SHOULD treat every image as personal data
  belonging to the tape's original owner.
- Images SHOULD NOT be published, uploaded to public issue trackers, attached
  to bug reports or committed to version control. Test material SHOULD be
  synthetic (Appendix A).
- The header also identifies the recovery: the capture device serial number,
  the capture file names in `sources[].file`, the tape name and timestamps.
  Writers record only each capture's directory name and file name, never an
  absolute path (Section 4.5), so the converting user's home directory and
  user name are not written; the directory name itself can still name the
  tape or its owner. Writers MAY omit or redact OPTIONAL members, and
  users SHOULD review the header before sharing even a header excerpt.
- Deleting an image does not erase it from backups or snapshots. Users
  SHOULD store images on encrypted storage and dispose of them as they would
  of the original tape.

### 11.2. Malformed Input

Tapewyrm's threat model does not include malicious inputs (Section 11.3):
the images it reads are the user's own. Damaged ones are another matter. A
copy that did not finish, a full disk or a stray edit leaves a file that is
truncated or malformed, and readers MUST reject such a file cleanly, with an
error that names the file (Section 9.2), never by crashing or by presenting
what they could read as the tape. In particular:

- `header_len` comes from the file and may be wrong. Readers MUST NOT
  allocate memory based on it without checking it against the file length
  (Section 9.2, item 9); the reference writer's headers are a few KiB.
- Readers MUST check the JSON type of each member they use. Integers
  outside the range of an IEEE 754 double [RFC7493] or of the field they
  describe MUST be rejected. Readers MAY also limit nesting depth, string
  length and number size; the reference reader does not.
- `segment_count` comes from the file and may be wrong. Readers MUST check
  `D + 29696 * segment_count` against the file length (Section 9.2, item 9)
  before trusting it, and MUST NOT allocate per-segment structures for a
  count the file cannot hold.
- Every segment number taken from the header (for example `header_seg`,
  `first_data_seg`) MUST be checked against `segment_count` before use.
- Segment data is tape content and MUST NOT be interpreted as anything but
  data by the reader of this format.

### 11.3. Decompression Bombs

A TWTZ file expands enormously by design: the zero-filled slots compress to
almost nothing, and the synthetic example in Appendix A is 1075 bytes
compressed and 120583 bytes decompressed. A crafted TWTZ could expand without
bound.

Tapewyrm's threat model does not include malicious inputs. The images it
reads are your own: made by `tw convert` from captures you took of tapes in
your hands. Decompression is therefore unbounded by design. The reference
reader caps neither the decompressed size nor the temporary storage it uses,
and a TWTZ that decompresses to more than its free space fails with the
operating system's out-of-space error. Readers MAY impose a limit of their
own. Should the threat model change to include images from untrusted
sources, this section will be revisited and a cap considered then.

### 11.4. Temporary Files

A TWTZ reader that decompresses to temporary storage writes the tape's
contents there in the clear. It SHOULD create the file readable only by the
current user, SHOULD place it on storage no more exposed than the source
file, and MUST delete it when the image is closed or the process exits.

## 12. IANA Considerations

This document has no IANA actions.

The following are informative suggestions for implementations and for
operating-system type registries, not registrations:

| Format | File extension | Suggested media type |
|--------|----------------|----------------------|
| TWTI   | `.twti`        | `application/x-tapewyrm-twti` (or `application/vnd.tapewyrm.twti`) |
| TWTZ   | `.twtz`        | `application/x-tapewyrm-twtz` (or `application/vnd.tapewyrm.twtz`) |

A TWTZ file is also valid `application/zstd` [RFC8878] data. Extensions are a
convenience for people; readers identify the format by magic (Section 8).

## 13. References

### 13.1. Normative References

[RFC2119]
: Bradner, S., "Key words for use in RFCs to Indicate Requirement Levels",
  BCP 14, RFC 2119, March 1997.

[RFC3339]
: Klyne, G. and C. Newman, "Date and Time on the Internet: Timestamps",
  RFC 3339, July 2002.

[RFC8174]
: Leiba, B., "Ambiguity of Uppercase vs Lowercase in RFC 2119 Key Words",
  BCP 14, RFC 8174, May 2017.

[RFC8259]
: Bray, T., Ed., "The JavaScript Object Notation (JSON) Data Interchange
  Format", STD 90, RFC 8259, December 2017.

[RFC8878]
: Collet, Y. and M. Kucherawy, Ed., "Zstandard Compression and the
  'application/zstd' Media Type", RFC 8878, February 2021.

[QIC-80]
: Quarter-Inch Cartridge Drive Standards, Inc., "QIC-80-MC: Serial Recorded
  Magnetic Tape Cartridge for Information Interchange", Revision N.
  `docs/qic-standards/qic80n.pdf`.

[QIC-117]
: Quarter-Inch Cartridge Drive Standards, Inc., "QIC-117: Common Command Set
  Interface Specification for Flexible Disk Controller Based Minicartridge
  Tape Drives", Revision J. `docs/qic-standards/qic117j.pdf`.

[TWS-1]
: Tapewyrm contributors, "TWRF, the Tapewyrm Raw Flux capture format",
  Tapewyrm Specification TWS-1. [twrf.md](twrf.md).

### 13.2. Informative References

[RFC7493]
: Bray, T., Ed., "The I-JSON Message Format", RFC 7493, March 2015.

[QIC-40]
: Quarter-Inch Cartridge Drive Standards, Inc., "QIC-40-MC", Revision M.
  `docs/qic-standards/qic40m.pdf`.

[QIC-3010]
: Quarter-Inch Cartridge Drive Standards, Inc., "QIC-3010-MC", Revision H.
  `docs/qic-standards/qic3010h.pdf`.

[QIC-3020]
: Quarter-Inch Cartridge Drive Standards, Inc., "QIC-3020-MC", Revision H.
  `docs/qic-standards/qic3020h.pdf`.

[TWS-3]
: Tapewyrm contributors, "TWVL, the Tapewyrm Volume format", Tapewyrm
  Specification TWS-3. [twvl.md](twvl.md).

## Appendix A. Examples

The example is synthetic. It was generated with the reference implementation
(`tapewyrm_archive.twti.TapeImage.save`) from made-up data: a toy cartridge of
2 tracks of 2 segments. It is not a real tape and copies nothing from one.

| Segment | State         | erasures | data_len | excluded_mask | Note |
|--------:|---------------|---------:|---------:|---------------|------|
| 0       | CLEAN (1)     | 0        | 29696    | `0x00000000`  | Starts with an FPR signature. |
| 1       | CORRECTED (2) | 1        | 28672    | `0x00000020`  | Slot 5 excluded: 28 data sectors. |
| 2       | MISSING (0)   | 0        | 0        | `0x00000080`  | Slot 7 excluded; expected length 28672. |
| 3       | BAD (4)       | 0        | 0        | `0x00000000`  | Whole segment bad per the map. |

Layout: `header_len` = 1757 (`0x6DD`); table at `T` = `0x6E7`; data at
`D` = `0x707`; slots at `0x707`, `0x7B07`, `0xEF07`, `0x16307`; `L` = 120583
(`0x1D707`).

### A.1. Preamble

```text
00000000: 5457 5449 0100 dd06 0000 7b0a 2022 666f  TWTI......{. "fo
          ^^^^^^^^^ ^^^^ ^^^^^^^^^ ^^...
          magic     ver  header_len=0x000006DD  JSON header begins
                    =1   (1757)
```

### A.2. End of Header and Segment Table

```text
000006d7: 7365 0a20 2020 7d0a 2020 7d0a 205d 0a7d  se.   }.  }. ].}
                                                ^^ last header byte '}'
000006e7: 0100 0074 0000 0000 0201 0070 2000 0000  ...t.......p ...
          |  | |  | |       | |  | |  | |       |
          |  | |  | |       | entry 1: state 2 CORRECTED, erasures 1,
          |  | |  | |       |   data_len 0x7000 (28672), mask 0x00000020
          entry 0: state 1 CLEAN, erasures 0, data_len 0x7400 (29696),
            mask 0
000006f7: 0000 0000 8000 0000 0400 0000 0000 0000  ................
          entry 2: state 0 MISSING, data_len 0, mask 0x00000080
                              entry 3: state 4 BAD, all else 0
```

### A.3. Segment Data

```text
00000707: 55aa 55aa 040d 0000 0000 0000 0000 0000  U.U.............
          slot 0 (CLEAN): data begins; 29696 bytes, no padding
00007b07: 0007 0e15 1c23 2a31 383f 464d 545b 6269  .....#*18?FMT[bi
          slot 1 (CORRECTED): 28672 bytes of data
0000eaf7: 9097 9ea5 acb3 bac1 c8cf d6dd e4eb f2f9  ................
                                                   last 16 data bytes
0000eb07: 0000 0000 0000 0000 0000 0000 0000 0000  ................
          slot 1 zero padding, 1024 bytes, to 0xEF07
0000ef07 .. 0001d706: slots 2 and 3, all zero (holes in a sparse file)
```

On a filesystem with holes, the sparse writer leaves slots 2 and 3 unwritten
and sets the length to 120583 with a truncate.

### A.4. The Same Image as TWTZ

Saved as `example.twtz`, the same image is 1075 bytes:

```text
00000000: 28b5 2ffd ....                           (./.
          ^^^^^^^^^ Zstandard frame magic
```

`zstd -d -c example.twtz` reproduces the 120583-byte TWTI stream above byte
for byte.

## Appendix B. Change Log

| Format version | Date       | Changes |
|---------------:|------------|---------|
| 1              | 2026-10-03 | Initial specification of TWTI version 1, as written by the reference implementation: preamble, JSON header, 8-byte segment table entries, 29696-byte fixed stride, sparse writing, TWTZ. MISSING entries carry the bad-sector map's mask (images from earlier builds of version 1 record 0 there). Same date, before release: readers MUST reject truncated and malformed files (Section 9.2); `sources[].file` records only the directory name and file name; `sources[].twrf` is always a full TWRF version 2 header (the headerless-stream form `{"rate_kbps": 500}` is gone); decompression is unbounded by design (Section 11.3); a reader that uses a `qic80_header` member MUST reject one that is absent, mistyped or out of range (Section 9.2, rule 13). |

## Author's Address

Tapewyrm contributors
https://github.com/indrora/tapewyrm
