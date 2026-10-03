# TWS-3: TWVL, the Tapewyrm Volume format

```text
Tapewyrm Specification TWS-3
Title:     TWVL: The Tapewyrm Volume Format
Category:  Standards Track (Tapewyrm)
Status:    Draft
Date:      2026-10-03
Author:    Tapewyrm contributors
License:   The Unlicense (public domain)
```

## Abstract

This document specifies TWVL, the file format Tapewyrm uses for one
backup volume (one QIC-113 File Set) recovered from a QIC-40, QIC-80,
QIC-3010 or QIC-3020 floppy tape. A TWVL file holds a short binary
preamble, a JSON header describing the volume, and the volume's
uncompressed bytes in their on-tape order, with every unrecovered byte
range recorded as a hole. It is the output of `qicsilver extract` and
the input of `qicsilver tar`.

## Status of This Memo

This is a Tapewyrm project specification. It is not an IETF document
and has not been reviewed by the IETF. It is normative for Tapewyrm
implementations and for any other software that reads or writes TWVL
files.

## Table of Contents

- [1. Introduction](#1-introduction)
  - [1.1. Requirements Language](#11-requirements-language)
  - [1.2. Terminology and Conventions](#12-terminology-and-conventions)
- [2. File Structure](#2-file-structure)
  - [2.1. Preamble](#21-preamble)
  - [2.2. Header](#22-header)
  - [2.3. Volume Bytes](#23-volume-bytes)
- [3. The JSON Header](#3-the-json-header)
  - [3.1. Top-Level Object](#31-top-level-object)
  - [3.2. The vtbl Object](#32-the-vtbl-object)
  - [3.3. The drive Object](#33-the-drive-object)
- [4. Volume Bytes](#4-volume-bytes)
  - [4.1. Volume Size](#41-volume-size)
  - [4.2. Uncompressed Volumes](#42-uncompressed-volumes)
  - [4.3. Compressed Volumes](#43-compressed-volumes)
  - [4.4. Locating the Directory Section](#44-locating-the-directory-section)
- [5. Holes and Lost Segments](#5-holes-and-lost-segments)
- [6. Processing Rules](#6-processing-rules)
  - [6.1. Writer Requirements](#61-writer-requirements)
  - [6.2. Reader Requirements](#62-reader-requirements)
- [7. Versioning and Extensibility](#7-versioning-and-extensibility)
- [8. Security and Privacy Considerations](#8-security-and-privacy-considerations)
  - [8.1. Untrusted Input](#81-untrusted-input)
  - [8.2. Privacy](#82-privacy)
- [9. IANA Considerations](#9-iana-considerations)
- [10. References](#10-references)
  - [10.1. Normative References](#101-normative-references)
  - [10.2. Informative References](#102-informative-references)
- [Appendix A. Examples](#appendix-a-examples)
- [Appendix B. Change Log](#appendix-b-change-log)
- [Author's Address](#authors-address)

## 1. Introduction

Tapewyrm recovers floppy tapes in four steps:

```text
tw dump  ->  TWRF  ->  tw convert  ->  TWTI/TWTZ  ->  qicsilver extract
         [TWS-1]                     [TWS-2]
         ->  TWVL  ->  qicsilver tar  ->  tar
           [TWS-3]
```

[TWS-1] (TWRF) holds raw flux; [TWS-2] (TWTI, TWTZ) holds a decoded,
error-corrected tape image, one entry per logical segment. This
document, [TWS-3], specifies the third stage: one file per entry of the
tape's volume table (VTBL, [QIC-80] section 8), holding that volume's
bytes.

Extraction is the QIC-113 layer. The extractor reads the volume table
from the image, decompresses each data segment's QIC-122 extent
([QIC-113] section 9, [QIC-122]) when the volume is compressed, and
places the resulting bytes at their uncompressed offsets. Segments that
could not be read leave holes, which are recorded in the header rather
than closed up, so every byte that was recovered stays at its true
offset.

TWVL does not reinterpret the volume. The volume bytes are a QIC-113
File Set (directory section and data section) exactly as the backup
software laid it out, minus the QIC-122 compression. Parsing the File
Set is a reader's business and is described here only informatively,
by reference to [QIC-113].

### 1.1. Requirements Language

The key words "MUST", "MUST NOT", "REQUIRED", "SHALL", "SHALL NOT",
"SHOULD", "SHOULD NOT", "RECOMMENDED", "NOT RECOMMENDED", "MAY", and
"OPTIONAL" in this document are to be interpreted as described in
BCP 14 [RFC2119] [RFC8174] when, and only when, they appear in all
capitals, as shown here.

### 1.2. Terminology and Conventions

- All multi-byte integers in the preamble are unsigned and
  little-endian. `u16` is 2 bytes, `u32` is 4 bytes.
- Byte offsets are 0-based. A range `[start, end)` contains the bytes
  at offsets `start` through `end - 1`; its length is `end - start`.
- Sizes are in bytes. 1 K means 1024 bytes.
- **Segment**: a QIC floppy-tape segment of 32 sectors of 1024 bytes,
  the last 3 of the sectors not excluded by the bad-sector map holding
  Reed-Solomon ECC. A segment with no excluded sectors carries
  29 x 1024 = 29696 data bytes.
- **Segment state**: the TWTI state of a segment, as defined in
  [TWS-2]: `CLEAN`, `CORRECTED`, `UNCORRECTABLE`, `MISSING` or `BAD`.
- **Volume**: one VTBL entry and the segments it spans, `start_seg`
  through `end_seg` inclusive.
- **Volume bytes**: the uncompressed byte stream of a volume as stored
  in a TWVL file (Section 4).
- **Hole**: a byte range of the volume bytes that no recovered segment
  supplied (Section 5).
- **Extent**: the content of one data segment of a compressed volume:
  an uncompressed offset followed by QIC-122 compression frames
  ([QIC-113] section 9).
- **Volume profile**: Tapewyrm's description, as data, of how a given
  backup program laid out VTBL bytes 57-127 (Section 3.2).
- JSON terms (object, member, array, number, string, `true`, `false`,
  `null`) are those of [RFC8259]. "Integer" means a JSON number with no
  fraction or exponent part.

## 2. File Structure

A TWVL file is three consecutive parts, with no padding between them
and nothing after the last:

```text
+----------------+---------------------------+-------------------------+
| Preamble       | Header                    | Volume Bytes            |
| 10 bytes       | header_length bytes       | to end of file          |
+----------------+---------------------------+-------------------------+
0                10                          10 + header_length
```

### 2.1. Preamble

```text
 0                   1                   2                   3
 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|      'T'      |      'W'      |      'V'      |      'L'      |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|           Version             |    Header Length (low 16)     |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|    Header Length (high 16)    |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
```

| Offset | Length | Field         | Value                                        |
|-------:|-------:|---------------|----------------------------------------------|
| 0      | 4      | Magic         | ASCII `TWVL` (`54 57 56 4C`)                 |
| 4      | 2      | Version       | u16; this document specifies version `1`     |
| 6      | 4      | Header Length | u32; length in bytes of the Header (2.2)     |

### 2.2. Header

The Header is `Header Length` bytes of UTF-8 encoded JSON [RFC8259]
whose top-level value is an object (Section 3). It is not
NUL-terminated and carries no padding.

The reference writer serializes with one-space indentation and escapes
all non-ASCII characters, so its headers are pure ASCII. Neither
property is normative.

### 2.3. Volume Bytes

The Volume Bytes run from offset `10 + Header Length` to the end of the
file. Their length is the volume size (Section 4.1). There is no length
field; the file's end delimits them.

## 3. The JSON Header

### 3.1. Top-Level Object

All members below are REQUIRED of a writer. A member whose value is
"or null" is still present, with the value `null`.

| Member              | JSON type                  | Description |
|---------------------|----------------------------|-------------|
| `format`            | string                     | The constant `"TWVL"`. |
| `version`           | integer                    | Format version; equal to the preamble Version (`1`). |
| `volume_index`      | integer                    | 0-based index of this volume's entry in the tape's volume table. The reference writer names the file `vol-NN.twvl` from it. |
| `tape_name`         | string                     | The tape name from the QIC-80 format parameter record (bytes 30-73 of the header segment), as recorded in the source image's `qic80_header.tape_name` ([TWS-2]). |
| `vtbl`              | object                     | The decoded volume table entry (Section 3.2). |
| `data_section_size` | integer or null            | File Set Data Section size from the VTBL entry (copy of `vtbl.data_section_size`). `null` when the volume profile does not define the field. |
| `dir_section_size`  | integer or null            | File Set Directory Section size from the VTBL entry (copy of `vtbl.dir_section_size`). `null` when the volume profile does not define the field. |
| `directory_offset`  | integer or null            | Offset in the volume bytes at which the File Set Directory Section starts, when the writer knows it exactly; otherwise `null` (Section 4.4). |
| `holes`             | array of [integer, integer]| Byte ranges `[start, end)` of the volume bytes that were not recovered (Section 5). Empty array when none. |
| `lost_segments`     | array of integer           | Segment numbers, within the source image, of the volume's segments that were not recovered (Section 5). Empty array when none. |
| `source_image`      | string                     | Path of the TWTI or TWTZ image the volume was extracted from, as given to the extractor. Informative only. |
| `drive`             | object or null             | The `drive` member of the source image's header ([TWS-2]), copied verbatim, or `null` if the image has none (Section 3.3). |

### 3.2. The vtbl Object

The `vtbl` object is the volume's 128-byte VTBL record ([QIC-80]
section 8), decoded. Only bytes 0-56 of a VTBL record have a meaning
fixed by [QIC-80]; when byte 56 bit 0 (vendor specific) is set, "only
this bit and the previous bytes" are defined. Bytes 57-127 are placed
differently by different backup programs. The extractor decodes them
through a volume profile, chosen by the user or by scoring every known
profile against the record (Tapewyrm's `qiclib.volume_profile`). A
member the chosen profile does not place is `null`.

Readers that need a field the profile could not supply, or that
distrust the profile's choice, SHOULD decode `raw` themselves.

All members are REQUIRED of a writer, in the sense of Section 3.1.

| Member                | JSON type              | VTBL bytes | Description |
|-----------------------|------------------------|-----------:|-------------|
| `signature`           | string                 | 0-3        | Entry signature, normally `"VTBL"`. Bytes decoded as ASCII; non-ASCII bytes become U+FFFD. |
| `start_seg`           | integer                | 4-5        | First segment of the volume (u16). |
| `end_seg`             | integer                | 6-7        | Last segment of the volume, inclusive (u16). |
| `description`         | string                 | 8-51       | Volume description: bytes up to the first NUL, decoded as ASCII (non-ASCII bytes become U+FFFD), trailing spaces removed. |
| `flags`               | integer                | 56         | Flag byte. Bit 0: vendor specific. Bit 4: segment spanning. Bit 5: Directory-Last ([QIC-113] section 7). |
| `os_type`             | integer or null        | profile    | Format and OS type (Rev N: byte 125; 1 = DOS). |
| `compressed`          | boolean or null        | profile    | Compression flag (Rev N: byte 124 bit 7). |
| `dir_section_size`    | integer or null        | profile    | Directory section size (Rev N: bytes 92-95). |
| `raw`                 | string                 | 0-127      | The whole 128-byte record as 256 lowercase hexadecimal digits. |
| `date`                | integer                | 52-55      | Volume date, packed QIC-80 "short date" (u32), undecoded. |
| `multi_cartridge_seq` | integer or null        | profile    | Cartridge sequence number (Rev N: byte 57). |
| `data_section_size`   | integer or null        | profile    | Data section size (Rev N: bytes 96-103, quadword; some software writes a doubleword). |
| `compression_code`    | integer or null        | profile    | QIC-123 compression code (Rev N: byte 124 bits 0-5; 1 = QIC-122). |
| `source_label`        | string or null         | profile    | Source drive label (Rev N: bytes 106-121), decoded like `description`. |
| `date_decoded`        | array of 6 integers, or null | 52-55 | `date` decoded as `[year, month, day, hour, minute, second]`, month 1-12 and day 1-31; `null` when `date` is `0` or `0xFFFFFFFF`. |

The packed short date ([QIC-80] section 7.1) is: bits 31-25 = year -
1970; bits 24-0 = `sc + 60*(mn + 60*(hr + 24*(dy + 31*mo)))`, with
`mo` 0-11 and `dy` 0-30.

`data_section_size` and `dir_section_size` appear both here and at the
top level. Writers MUST give them the same values in both places.

### 3.3. The drive Object

`drive` identifies the tape drive that read the tape, for provenance.
Its members are defined by [TWS-2]; the reference writer copies the
image's `drive` object unchanged. As of this document they are
`device_serial`, `drive_status`, `drive_config`, `drive_rom`,
`drive_vendor_id`, `tape_status`, `rate_kbps` and `firmware_commit`,
each possibly `null`. Readers MUST NOT require any of them.

## 4. Volume Bytes

The volume bytes are the volume's QIC-113 File Set
([QIC-113] section 7 for Basic-DOS, section 8 for Extended formats),
uncompressed, with:

- the File Set Directory Section and File Set Data Section in the order
  the backup software wrote them: directory first, or, when
  `vtbl.flags` bit 5 is set, data first ("Directory-Last");
- every QIC-122 extent already decompressed: a reader MUST NOT apply
  QIC-122 decompression to the volume bytes, whatever
  `vtbl.compressed` says;
- every hole (Section 5) filled with zero bytes.

Any multi-cartridge Link Section (`LTLT`, [QIC-113] section 7) the
software wrote is part of the volume bytes, as are Segment Gaps in
uncompressed volumes (Section 4.2).

### 4.1. Volume Size

The length of the volume bytes is determined by the writer as follows.

- **Uncompressed** (`vtbl.compressed` is `false`): the sum, over each
  segment `n` from `start_seg` to `end_seg` inclusive, of:
  - 0 if the segment is `BAD` (mapped out before the backup was
    written, so the software skipped it);
  - `(32 - x - 3) x 1024` if the segment is `MISSING` or
    `UNCORRECTABLE`, where `x` is the number of sectors the bad-sector
    map excludes from it (the TWTI entry's excluded mask, [TWS-2]);
  - the segment's recovered data length otherwise.
- **Compressed or unknown** (`vtbl.compressed` is `true` or `null`):
  `data_section_size + dir_section_size`, either taken as 0 when
  `null`.

The uncompressed size can exceed the table's
`data_section_size + dir_section_size`, because it includes the Segment
Gap and any unused bytes at the end of the last segment.

A writer MUST refuse to write a volume whose size exceeds
`(end_seg - start_seg + 1) x 29696 x R`, where `R` is 1 for an
uncompressed volume and 4 otherwise: such a size comes from a
misread volume table, not from the tape (Section 8.1).

### 4.2. Uncompressed Volumes

An uncompressed volume's bytes are its segments' data laid end to end
in segment order. The offset of each segment is the running sum given
in Section 4.1, not `(n - start_seg) x 29696`: segments shortened by
the bad-sector map are shorter, and `BAD` segments take no room. A
`MISSING` or `UNCORRECTABLE` segment occupies its expected size, all of
it a hole; partially recovered data from an `UNCORRECTABLE` segment is
not used.

Because whole segments are laid out, a Directory-Last volume keeps its
Segment Gap ([QIC-113] section 7): the directory section starts at the
first segment boundary at or after `data_section_size`.

### 4.3. Compressed Volumes

Each recovered segment of a compressed volume holds one extent: an
uncompressed offset, then QIC-122 frames. The writer decodes the extent
and places its uncompressed bytes at that offset in the volume bytes.
The offset field is 8 bytes (quadword) per [QIC-113] Rev G; some
software writes 4 bytes. The width comes from the volume profile and is
not recorded in the header.

Extent bytes that would fall at or beyond the volume size are dropped.
A segment whose extent fails to decode is treated as lost
(Section 5).

In this layout the Segment Gap takes no room. A Directory-Last
compressed volume's directory section has been observed to start at
exactly `data_section_size`. Software that stores the directory section
uncompressed at the start of the volume (Directory-First) places the
data section at `dir_section_size`.

### 4.4. Locating the Directory Section

`directory_offset` is an integer only when the writer knows the
directory's start exactly. The reference writer sets it only for an
uncompressed (`vtbl.compressed` is `false`) Directory-Last volume with
a non-null `data_section_size`, to the first segment boundary at or
after `data_section_size` (Section 4.2). In every other case it is
`null`.

When `directory_offset` is `null`, a reader locates the directory
section itself. Informatively, the reference reader does this:

- Directory-First (`vtbl.flags` bit 5 clear): the directory section
  starts at offset 0.
- Directory-Last: try `data_section_size` and `data_section_size`
  rounded up to a multiple of 29696, the likelier first (rounded for
  uncompressed volumes, exact for compressed ones), and take the first
  at which a plausible Directory Entry parses. If `data_section_size`
  is unusable, walk the data section's Data Entries ([QIC-113]
  section 7.1.3) and try the end of the last one.

Whether the File Set uses the Basic-DOS directory format
([QIC-113] section 7) or an Extended format ([QIC-113] section 8)
follows from the VTBL record: Extended when `flags` bit 0 is set and
VTBL bytes 58-59 hold 113 (little-endian) with a revision in bytes
60-61 of at least 1. `vtbl.os_type` is not a reliable marker.

## 5. Holes and Lost Segments

`holes` lists the byte ranges of the volume bytes that no recovered
segment supplied. Each element is a two-element array
`[start, end]` denoting the half-open range `[start, end)`.

Writers MUST emit `holes` such that:

- `0 <= start < end <= volume size` for every range;
- the ranges are sorted by `start` and neither overlap nor touch
  (each range is maximal: `end` of one is less than `start` of the
  next);
- every byte of the volume bytes is either supplied by a recovered
  segment or inside exactly one range;
- every byte inside a range is zero in the volume bytes.

A hole means "unknown", not "zero". Its zero bytes are filler. A hole
can come from a lost segment, from bytes past the last extent when the
table overstates the volume's size, or from an extent dropped at the
end of the volume. In a compressed volume, the range a lost segment
would have supplied is known only from its neighbours' offsets, so a
hole is the uncovered gap between recovered extents.

`lost_segments` lists, in ascending order, the segment numbers in
`[start_seg, end_seg]` whose data is not in the volume bytes: segments
that are `MISSING` or `UNCORRECTABLE` in the source image, and
segments whose extent failed QIC-122 decoding. `BAD` segments are not
listed: the backup never used them. `lost_segments` is a diagnostic
record; `holes` is the authority on which bytes are missing. A volume
can have holes and no lost segments.

## 6. Processing Rules

### 6.1. Writer Requirements

1. A writer MUST write the preamble with magic `TWVL`, version `1`, and
   the exact byte length of the header.
2. The header MUST be a JSON object [RFC8259] encoded as UTF-8 and MUST
   contain every member of Sections 3.1 and 3.2.
3. The header's `format` MUST be `"TWVL"` and its `version` MUST equal
   the preamble Version.
4. The volume bytes MUST be laid out as Section 4 specifies, and MUST
   be exactly the volume size long.
5. Every hole MUST be zero-filled and MUST be listed in `holes`
   (Section 5). A writer MUST NOT list a recovered byte as a hole and
   MUST NOT close up or shift data around a hole.
6. A writer MUST NOT set `directory_offset` unless it knows the
   directory's start exactly; otherwise it MUST write `null`.
7. A writer MUST NOT fill a hole with guessed, interpolated or
   duplicated data.
8. A writer SHOULD refuse, with a diagnostic, a volume whose `end_seg`
   lies past the end of the source image, or whose size fails the bound
   in Section 4.1, rather than write a misleading file.
9. A writer SHOULD write one file per VTBL entry.

### 6.2. Reader Requirements

1. A reader MUST check the magic and MUST reject a file whose magic is
   not `TWVL`.
2. A reader MUST reject a file whose preamble Version it does not
   implement. A version-1 reader MUST reject every other version.
3. A reader MUST reject a file whose Header Length exceeds the bytes
   remaining after the preamble, or whose header is not a valid UTF-8
   JSON object.
4. A reader MUST ignore header members it does not recognize, at the
   top level and inside `vtbl` and `drive`.
5. A reader MUST treat bytes inside a hole as missing. When it returns
   any range of the volume bytes, it MUST be able to report how many of
   those bytes fall in holes, and MUST NOT present zeros from a hole as
   recovered data. A file whose content overlaps a hole MUST be
   reported as damaged (for example, in a damage report), even if the
   reader still writes it out zero-filled.
6. A reader MUST treat bytes past the end of the file (a volume shorter
   than its `holes` or its section sizes imply) as missing, exactly as
   if they were in a hole.
7. A reader MUST NOT decompress the volume bytes (Section 4).
8. When `directory_offset` is an integer, a reader SHOULD start the
   directory section there; if no Directory Entry parses at that
   offset, it MAY locate the directory as Section 4.4 describes. When
   it is `null`, the reader locates the directory itself.
9. A reader SHOULD take `vtbl.flags`, `data_section_size` and
   `dir_section_size` from the header, and MAY re-decode `vtbl.raw`
   when it needs a field the header leaves `null`.
10. A reader MUST NOT rely on `source_image` or `drive` for anything
    but display and provenance.
11. On a truncated or damaged file whose preamble and header are
    intact, a reader SHOULD recover what it can, treating absent bytes
    as missing (rule 6), and SHOULD say so.

## 7. Versioning and Extensibility

The preamble Version is the format version. It changes only when a
version-1 reader would misread a file: a changed preamble, a changed
meaning of an existing member, or a changed layout of the volume bytes.
The header's `version` member repeats it.

New information MAY be added to the header as new members without a
version change. Writers SHOULD NOT add members whose absence would make
a version-1 reader misread the volume bytes. Readers ignore unknown
members (Section 6.2, rule 4).

Members of `vtbl` follow the decoded VTBL fields; a future volume
profile that decodes more of the record MAY add members to `vtbl`.

## 8. Security and Privacy Considerations

### 8.1. Untrusted Input

TWVL files and the tapes they come from are untrusted.

- **Header length.** Header Length is a u32 and can claim up to
  4 GiB. Readers SHOULD bound it (for example, to the file size and to
  a fixed maximum such as 16 MiB) before allocating or parsing.
- **JSON.** Readers SHOULD use a JSON parser with limits on nesting
  depth, string length and number magnitude, and SHOULD validate member
  types before use. Integers in the header can exceed 2^53; readers
  MUST NOT silently lose precision on offsets and sizes.
- **Sizes from the volume table.** `data_section_size`,
  `dir_section_size` and the volume size derive from a VTBL record
  whose bytes 57-127 were interpreted through a guessed volume profile.
  A wrong profile reads neighbouring bytes (labels, unknown fields) as
  sizes, giving values of gigabytes or more. Writers MUST bound the
  volume size (Section 4.1) before allocating it. Readers MUST NOT
  allocate from these values without checking them against the file
  size, and MUST bounds-check `directory_offset`, every hole range, and
  every offset and length read out of the File Set itself (Directory
  Entry sizes, Data Entry sizes, path lengths) against the volume
  bytes.
- **Holes.** Readers MUST NOT assume `holes` is well-formed: ranges may
  be reversed, overlapping, or outside the volume bytes, and SHOULD be
  clamped or rejected.
- **Paths.** File names and paths in the File Set come from the tape.
  Readers that write files out (to a tar or a directory) MUST sanitize
  them against absolute paths, `..` components and device names.

### 8.2. Privacy

A TWVL file is the backup itself: it contains the files of whoever's
computer was backed up, from documents and mail to address books,
financial records and credentials, plus that person's directory
structure, volume labels and dates. The tapes Tapewyrm is developed
against come from second-hand and recycled stock; their owners never
consented to their data being read.

- Implementations and users SHOULD treat every TWVL file, and anything
  derived from it, as personal data belonging to the tape's original
  owner.
- Users SHOULD NOT publish, share or upload TWVL files, file listings,
  or excerpts of volume bytes from tapes they do not own, including in
  bug reports and test fixtures. Test data SHOULD be synthetic
  (Appendix A).
- Tools SHOULD NOT send volume contents or listings to remote
  services.
- `source_image` records a local path, which can reveal a user name or
  directory layout of the machine that ran the extractor. Writers
  SHOULD record a relative path or a bare file name; users sharing a
  file SHOULD check it.
- `tape_name`, `vtbl.description`, `vtbl.source_label` and `vtbl.raw`
  can identify the original owner (for example, a name or company as
  the tape or volume label).
- Users SHOULD store TWVL files with access restricted to themselves
  and delete them when recovery is done, following any applicable data
  protection law.

## 9. IANA Considerations

This document has no IANA actions.

Informatively, Tapewyrm uses the file name extension `.twvl`. Software
that needs a media type MAY use the unregistered
`application/vnd.tapewyrm.twvl` (or, in older `x-` style,
`application/x-tapewyrm-twvl`).

## 10. References

### 10.1. Normative References

- **[RFC2119]** Bradner, S., "Key words for use in RFCs to Indicate
  Requirement Levels", BCP 14, RFC 2119, March 1997.
- **[RFC8174]** Leiba, B., "Ambiguity of Uppercase vs Lowercase in
  RFC 2119 Key Words", BCP 14, RFC 8174, May 2017.
- **[RFC8259]** Bray, T., Ed., "The JavaScript Object Notation (JSON)
  Data Interchange Format", STD 90, RFC 8259, December 2017.
- **[QIC-113]** Quarter-Inch Cartridge Drive Standards, Inc., "QIC-113
  Revision G: Common Recording Format for Information Interchange on
  Floppy Tape Cartridges (File Set Format)",
  [docs/qic-standards/qic113g.pdf](../qic-standards/qic113g.pdf).
- **[QIC-122]** Quarter-Inch Cartridge Drive Standards, Inc., "QIC-122
  Revision B: Data Compression Format",
  [docs/qic-standards/qic122b.pdf](../qic-standards/qic122b.pdf).
- **[QIC-80]** Quarter-Inch Cartridge Drive Standards, Inc., "QIC-80-MC
  Revision N: Serial Recording Format for 1/4-inch Floppy Tape
  Cartridges" (format parameter record, section 7.1; volume table,
  section 8),
  [docs/qic-standards/qic80n.pdf](../qic-standards/qic80n.pdf).
- **[TWS-2]** Tapewyrm contributors, "TWTI: The Tapewyrm Tape Image
  Format", Tapewyrm Specification TWS-2, [twti.md](twti.md).

### 10.2. Informative References

- **[TWS-1]** Tapewyrm contributors, "TWRF: The Tapewyrm Raw Flux
  Format", Tapewyrm Specification TWS-1, [twrf.md](twrf.md).
- **[TWS-3]** This document, [twvl.md](twvl.md).
- **[QIC-123]** Quarter-Inch Cartridge Drive Standards, Inc., "QIC-123:
  Compression Algorithm Registration" (compression codes).
- **[REF-IMPL]** Tapewyrm, `tapewyrm_archive.twvl` (format) and
  `qiclib.extract` (writer), `qiclib.qic113`, `qiclib.qic113ext` and
  `qicsilver.tar` (readers), https://github.com/indrora/tapewyrm.

## Appendix A. Examples

This example is synthetic. It was produced by running the reference
extractor (`qiclib.extract.extract`, volume profile `qic80-rev-n`) on
a made-up six-segment TWTI image: segments 0-1 header, segment 2 volume
table, segments 3-5 one compressed Directory-Last volume. Segment 4 is
`MISSING`. Segments 3 and 5 each hold one raw (stored) QIC-122 frame of
16 bytes, at uncompressed offsets 0 and 32. The VTBL entry claims a
32-byte data section and a 16-byte directory section. The volume bytes
are placeholder text, not a parseable File Set.

The file is 1182 bytes: 10 bytes of preamble, a 1124-byte header, and
48 volume bytes.

Preamble:

```text
00000000: 5457 564c 0100 6404 0000                 TWVL..d...
          \_______/ \__/ \_______/
           magic     |    header length = 0x00000464 = 1124
                     version = 1
```

Header (offsets 0x0a-0x46d), shown as written:

```json
{
 "format": "TWVL",
 "version": 1,
 "volume_index": 0,
 "tape_name": "EXAMPLE TAPE",
 "vtbl": {
  "signature": "VTBL",
  "start_seg": 3,
  "end_seg": 5,
  "description": "EXAMPLE BACKUP",
  "flags": 32,
  "os_type": 1,
  "compressed": true,
  "dir_section_size": 16,
  "raw": "5654424c030005004558414d504c45204241434b55500000000000000000000000000000000000000000000000000000000000004089633420000000000000000000000000000000000000000000000000000000000000000000000010000000200000000000000000004558414d504c452d534f555243452020000081010000",
  "date": 878938432,
  "multi_cartridge_seq": 0,
  "data_section_size": 32,
  "compression_code": 1,
  "source_label": "EXAMPLE-SOURCE",
  "date_decoded": [
   1996,
   3,
   14,
   12,
   0,
   0
  ]
 },
 "data_section_size": 32,
 "dir_section_size": 16,
 "directory_offset": null,
 "holes": [
  [
   16,
   32
  ]
 ],
 "lost_segments": [
  4
 ],
 "source_image": "example.twti",
 "drive": {
  "device_serial": null,
  "drive_status": null,
  "drive_config": null,
  "drive_rom": null,
  "drive_vendor_id": null,
  "tape_status": null,
  "rate_kbps": 500,
  "firmware_commit": null
 }
}
```

Notes on the header:

- `flags` = 32 = 0x20: bit 5, Directory-Last.
- `raw` byte 124 is `0x81`: compressed (bit 7), QIC-123 code 1.
  Byte 125 is `0x01`: DOS.
- `date` 878938432 = 0x34638940: year 26 + 1970 = 1996, remainder
  decoding to 14 March, 12:00:00.
- `directory_offset` is `null`: the volume is compressed, so the
  writer does not know the directory's start exactly (Section 4.4).
  A reader tries offset 32 (`data_section_size`) first.
- `holes` is `[[16, 32]]`: the bytes the missing segment 4 would have
  supplied. Segment 4 is in `lost_segments`.

Volume bytes (offsets 0x46e-0x49d, volume offsets 0-47):

```text
file      volume
offset    offset  bytes                                            ASCII
0000046e  0000    45 58 41 4d 50 4c 45 2e 54 58 54 20 30 31 32 33  EXAMPLE.TXT 0123
0000047e  0010    00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00  ................
0000048e  0020    44 49 52 45 43 54 4f 52 59 2d 42 59 54 45 53 21  DIRECTORY-BYTES!
                  \_____________________________________________/
  0x00-0x0f  data section, from segment 3's extent (offset 0)
  0x10-0x1f  HOLE [16, 32): zero filler, NOT data (segment 4 lost)
  0x20-0x2f  directory section, from segment 5's extent (offset 32)
```

A reader asked for volume bytes `[8, 24)` returns
`"TXT 0123"` followed by eight zero bytes, and reports 8 of the 16
bytes missing.

## Appendix B. Change Log

| Format version | Date       | Changes |
|---------------:|------------|---------|
| 1              | 2026-10-03 | First specified version. Preamble `TWVL`, u16 version, u32 header length; JSON header with `vtbl`, section sizes, `directory_offset`, `holes`, `lost_segments`, `source_image`, `drive`; uncompressed volume bytes in on-tape order. |

## Author's Address

Tapewyrm contributors
https://github.com/indrora/tapewyrm
