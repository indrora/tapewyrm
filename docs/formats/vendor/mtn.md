# MTN backup

Some QIC-80 tapes carry the letters `MTN` in their volume table entries. The
software that wrote them is not identified; `MTN` may be Mountain Network
Solutions' backup software, but that is not confirmed, so the profile is
named after the bytes. These tapes hold QIC-113 Basic DOS volumes compressed
with QIC-122, with a volume table layout and extent width that differ from
the standards. Every offset on this page was read off real tapes, not taken
from a specification.

Profile: `mtn` (`packages/qiclib/qiclib/volume/profiles/mtn.toml`).
Status: bench, two tapes, both recovered.

## Volume table entry

Bytes 0–56 are the standard [VTBL entry](../volume-table.md#vtbl-entry). The
vendor-specific bit is **clear**, yet bytes 57–127 do not follow QIC-80-MC
Rev N:

| Offset | Size | Field | Rev N has here | Notes |
|---|---|---|---|---|
| 57 | 1 | Multi-cartridge sequence | Same | `01` |
| 58 | 3 | Mark | Vendor extension data | `MTN` (`4D 54 4E`) |
| 92 | 4 | Directory section size | Same | |
| 96 | 4 | Data section size | Quadword at 96–103 | A doubleword: the label starts at 102 |
| 100 | 2 | Unknown | | |
| 102 | 16 | Source volume label | Label at 106 | Four bytes earlier than Rev N |
| 118 | 2 | Unknown | | |
| 120 | 1 | Compression | Compression at 124 | `81`: compressed, QIC-123 code 1 (QIC-122) |

Read with Rev N's offsets, the same record claims a data section of billions
of gigabytes. Read with these, one bench volume holds about 164 MB of data in
about 98 MB of tape, a 1.7:1 ratio typical of QIC-122. That plausibility is
what Tapewyrm's profile scoring checks.

## Compression extents

The volumes use QIC-122 frames as in QIC-113 Rev G §9, with one difference:

| Field | QIC-113 Rev G | MTN |
|---|---|---|
| Uncompressed volume byte offset | 8 bytes | **4 bytes** |
| Frame size | 2 bytes | 2 bytes |

With an 8-byte offset, the first frame size lands inside the offset field
and nearly every segment fails to decode. With 4 bytes, each segment's
offset equals the sum of the output of the segments before it. The profile
sets `[extent] offset_bytes = 4`.

The directory is stored as raw frames at the start of the volume, so the
data section starts at an uncompressed offset equal to the volume table's
directory section size.

## Volume contents

The volumes are QIC-113 Basic DOS, directory first (directory-last bit
clear). They follow the standard in places where Tapewyrm's first reader did
not, and differ from it in two:

| Item | QIC-113 Rev G | MTN tapes |
|---|---|---|
| Fixed portion size byte | 10 (includes the extra-info byte) | **9**: no extra-info byte |
| End of directory | Last-in-table attribute bit | **Zero padding**; the bit is not set |
| Directory order | Example is depth-first over levels | Depth-first over levels |
| Data entry size | Includes the data header | Includes the data header |
| Path entry | Directory the item is in | Directory the item is in |
| Empty directories | Header-only data entry, non-zero size | Same; no directory level |

The `qic113-basic` parser reads the size byte to place the name portion, so
9-byte and 10-byte entries both parse without a setting.

## Tapes seen

| Cartridge | Header | Notes |
|---|---|---|
| QIC-80 307.5 ft (DC2120), factory preformatted | Format code 2 (fixed), 28 × 150 | One volume. Header bytes 78 and 144–145 non-zero, which Rev N calls unused. |
| QIC-80 | Not recorded here | One volume. Some files lost where their data headers sat in unreadable segments. |

## See also

- [Vendor formats](index.md)
- [QIC-113 file sets](../qic-113.md)
- [QIC-122 compression](../qic-122.md)
- [Header and bad-sector map](../header.md#bad-sector-map-fixed-formats)
