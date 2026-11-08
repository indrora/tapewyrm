# Colorado (CMS) backup

Colorado Memory Systems' backup software (later sold by HP as Colorado
Backup) writes QIC-113 extended volumes, compressed with QIC-122. Its volume
table entries carry both the QIC-113 signature and the vendor's own mark,
which is how Tapewyrm recognises them. Tapes written by it have been
recovered completely, on QIC-80 and QIC-3010 cartridges.

Profile: `cms-qic113` (`packages/qiclib/qiclib/volume/profiles/cms-qic113.toml`).
Status: bench, two tapes.

## Volume table entry

Bytes 0–56 are the standard [VTBL entry](../volume-table.md#vtbl-entry).
Bytes 57–127 follow QIC-113 Rev G §6 with CMS's mark in the vendor data:

| Offset | Size | Field | Value seen |
|---|---|---|---|
| 56 | 1 | Flags | `25` on a QIC-80 tape, `24` on a QIC-3010 tape |
| 57 | 1 | Multi-cartridge sequence | `01` |
| 58 | 2 | QIC-113 signature | `71 00` (113) |
| 60 | 2 | QIC-113 revision | `06 00` (Rev F) |
| 62 | 4 | Vendor mark | `CMS` followed by `00` |
| 92 | 4 | Directory section size | |
| 96 | 8 | Data section size | |
| 106 | 16 | Source volume label | |
| 124 | 1 | Compression | Bit 7 set, code 1 (QIC-122) |
| 125 | 1 | Format and OS type | `06` on the QIC-80 tape, `07` on the QIC-3010 tape |

Flags `25` is vendor-specific, written without verification, and
directory-last. Flags `24` is the same with the vendor-specific bit clear.

!!! note
    The same software set the vendor-specific bit on one cartridge type and
    not the other. QIC-113 Rev G §6 says the bit marks an extended volume, but
    it does not on these tapes. The profile matches on `113` at 58 and `CMS`
    at 62 only. Matching on the bit once sent the QIC-3010 tape to the Basic
    DOS parser, which "succeeded" with nameless empty files.

## Volume contents

- **QIC-113 extended**, directory-last, Rev F. See
  [QIC-113 file sets](../qic-113.md#extended-directory-entry).
- **Compressed with QIC-122**, one extent per segment, 8-byte offsets and
  2-byte frame sizes as in Rev G. Segment spanning is clear.
- **Root entries name the source drive**, as QIC-113 prescribes.
- **Data-section copies of directory entries carry all-ones sizes.** The
  directory section holds the real sizes, and Tapewyrm uses it as the
  authority. The data entry sizes in the directory sum exactly to the volume
  table's data section size.
- **Files that failed at backup time** carry the "file error during backup"
  traversal bit. Tapewyrm reports them as damaged; that damage is the
  original software's, not the tape's.

## Tapes seen

| Cartridge | Header | Notes |
|---|---|---|
| QIC-80 | Format code 5 (fixed), 28 × 207, maximum floppy track 149 | Formatted by Colorado software. Vendor bit set. One volume, fully recovered. |
| QIC-3010, 400 ft | Format code 4, 40 × 298, factory preformatted; manufacturer field names CMS / HP | Vendor bit clear. One volume, fully recovered. |

## See also

- [Vendor formats](index.md)
- [Volume table](../volume-table.md)
- [QIC-122 compression](../qic-122.md)
