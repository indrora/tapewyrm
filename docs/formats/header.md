# Header segment and bad-sector map

The header segment describes the tape: its format, its geometry, its history,
and which sectors the format excluded. Tapewyrm reads it before anything
else, because placing sectors beyond the start of track 0 and correcting any
segment both depend on it.

Source: QIC-80-MC Rev N §7 ([qic80n.pdf](../qic-standards/qic80n.pdf)).
QIC-3010-MC Rev H and QIC-3020-MC Rev H §7 use the same layout with the
differences listed on [QIC-3010 and QIC-3020](qic-3010-3020.md#header-differences).

## Location

The **first defect-free segment** on the tape is the header segment. The
second defect-free segment is a duplicate. Segments before the first copy and
between the two copies are written with deleted-data marks (Rev N §7).

| Sectors | Use |
|---|---|
| 0 | Format parameter record (bytes 0–255) |
| 0–28 | Bad-sector map (from byte 256 for format code 4) |
| 29–31 | ECC |

Rev N Appendix B shows a typical layout: segment 0 damaged, segments 1 and 2
the header copies, segment 3 the volume table, file sets from segment 4.

## Format parameter record

Sector 0, bytes 0–255. Words and doublewords are little-endian. Rev N §7.1:

| Offset | Size | Field | Description |
|---|---|---|---|
| 0 | 4 | Signature | `55 AA 55 AA` |
| 4 | 1 | Format code | `04` = variable-length format. `02`, `03` and `05` are fixed formats from QIC-80-MC Rev K. |
| 5 | 1 | Revision | Rev M = `0D`, Rev L = `0C`, earlier = `00` |
| 6 | 2 | Header segment | Segment number of this header |
| 8 | 2 | Duplicate header segment | |
| 10 | 2 | First data segment | First segment of the logical area (holds the volume table) |
| 12 | 2 | Last data segment | |
| 14 | 4 | Format date | Date of the most recent format, packed (below) |
| 18 | 4 | Write date | Date of the most recent write or format |
| 22 | 2 | Unused | |
| 24 | 2 | Segments per track | |
| 26 | 1 | Tracks per cartridge | |
| 27 | 1 | Maximum floppy side | Length-dependent |
| 28 | 1 | Maximum floppy track | 254 for format code 4 |
| 29 | 1 | Maximum floppy sector | 128 |
| 30 | 44 | Tape name | ASCII, left-justified, space-filled |
| 74 | 4 | Name date | When the tape name was written |
| 78 | 50 | Unused | |
| 128 | 1 | Re-format error | `FF` if fields were lost to a tape error during re-format |
| 129 | 1 | Unused | |
| 130 | 4 | Segments written | Segments written, formatted or verified over the tape's life |
| 134 | 4 | Unused | |
| 138 | 4 | Initial format date | |
| 142 | 2 | Format count | Times the tape has been formatted |
| 144 | 2 | Unused | |
| 146 | 44 | Manufacturer | Duplicator's identification, on preformatted tapes; zero otherwise |
| 190 | 44 | Lot code | Duplicator's lot code, on preformatted tapes; zero otherwise |
| 234 | 22 | Unused | QIC-3020 format code 6 uses 234–249 |

**Packed dates** (Rev N §7.1, also QIC-113's "Short Date/Time"): bits 31–25
are year − 1970; bits 24–0 hold
`SC + 60 × (MN + 60 × (HR + 24 × (DY + 31 × MO)))`, with month and day
counted from 0.

## Bad-sector map, format code 4

From offset 256 of the header segment, a list of 3-byte entries in ascending
order (Rev N §7.2):

| Bits | Field |
|---|---|
| 0–22 | Logical sector number **plus one** (1-based, so 0 can end the list) |
| 23 | Whole segment: this sector (the first of its segment) and the next 31 are bad |

An entry of `00 00 00` ends the list. The map can hold 314,016 entries and
address 16,777,215 sectors.

## Bad-sector map, fixed formats

Fixed-format QIC-80 tapes (format codes 2, 3 and 5) use a different map.
Rev N refers to Rev K for these and does not describe them, and Rev K is not
in the standards library. Tapewyrm reads them as follows, from the bench:

| Offset | Size | Field |
|---|---|---|
| 2048 + 4n | 4 | Mask for segment n: bit k set = sector k excluded. `FFFFFFFF` = whole segment bad. |

This is the only reading under which the excluded segments of a bench tape
(code 5) decode, with the QIC-113 byte offsets of the neighbouring segments
agreeing exactly on both sides. Every slot of an excluded sector still
carried a valid ID on that tape: the sector is recorded but is not part
of the codeword.

Other fields of fixed-format headers match the Rev N layout byte for byte on
the bench tapes (segments per track, tracks, name, dates, counters). One
factory-preformatted code-2 tape has non-zero bytes at 78 and 144–145, which
Rev N calls unused; their meaning is unknown.

## Tapewyrm notes

**Module.** `qiclib.header`: `parse_header_data` decodes the record and the
map, `locate_header` finds the header and re-places every sector under its
geometry, and `apply_bsm` marks excluded sectors before correction.

**Finding the header.** `locate_header` walks segments in order and takes
the first whose data starts with the signature. If sector 0 of a segment is
unreadable, it tries a Reed-Solomon correction of that segment before moving
on, and if the first copy is lost it finds the duplicate one segment later.

**Geometry from the header.** Tracks, segments per track, and floppy tracks
per side (maximum floppy track + 1) all come from the header. A fixed-format
tape can record a maximum floppy track of 149, not 254; see
[QIC-80-MC](qic-80-mc.md#tapewyrm-notes).

**Where the map starts.** Rev N puts the parameter record in bytes 0–255, so
the code-4 map starts at 256. An earlier version started at 128 and read the
lifetime segment counter and the initial format date as two bad sectors.

**The map describes the tape, not the read.** Excluded slots are computed for
every segment, including segments that were not captured, so a lost
segment's real data length is known when a volume is extracted.

**Format code 6** (QIC-3020 only) is not handled; see
[QIC-3010 and QIC-3020](qic-3010-3020.md#tapewyrm-notes).

## See also

- [Error correction](ecc.md)
- [Volume table](volume-table.md)
- [qicsilver identify](../qicsilver/identify.md)
