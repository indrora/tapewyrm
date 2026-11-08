# Volume table

The volume table lists the backup volumes (file sets) on a tape: where each
one starts and ends, its description and date, and how its contents are laid
out. Every writer agrees on the first 57 bytes of an entry. The rest belongs
to the software that wrote the tape, which is why Tapewyrm reads it through
volume profiles.

Sources: QIC-80-MC Rev N §8 ([qic80n.pdf](../qic-standards/qic80n.pdf));
QIC-113 Rev G §6 ([qic113g.pdf](../qic-standards/qic113g.pdf)).

## Location

The volume table is the **first segment of the logical area**, the segment
the header names as the first data segment (Rev N §8). It is a sequence of
128-byte entries. The table ends at the first entry without a known
signature; if the first entry has none, the tape is empty.

| Signature | Entry | Source |
|---|---|---|
| `VTBL` | One volume (file set) | Rev N §8 |
| `XTBL` | Extends the preceding `VTBL`: Unicode volume name and password | Rev N §8.1 |
| `UTID` | Unicode tape name; at most one per table | Rev N §8.2 |
| `EXVT` | Continues the table in another segment; the last entry of its segment | Rev N §8.3 |

## VTBL entry

Words, doublewords and quadwords are little-endian. QIC-80-MC Rev N §8:

| Offset | Size | Field | Description |
|---|---|---|---|
| 0 | 4 | Signature | `VTBL` |
| 4 | 2 | Start segment | First segment of the volume on this cartridge |
| 6 | 2 | End segment | Last segment of the volume on this cartridge |
| 8 | 44 | Description | ASCII; first byte zero if none |
| 52 | 4 | Date | Packed date (see [Header](header.md#format-parameter-record)) |
| 56 | 1 | Flags | See below |
| 57 | 1 | Multi-cartridge sequence | 1 for the first cartridge |
| 58 | 26 | Vendor extension data | Free for the writer; should carry a signature |
| 84 | 8 | Password | ASCII; first byte zero if none |
| 92 | 4 | Directory section size | Bytes reserved for the directory |
| 96 | 8 | Data section size | Bytes in the data section |
| 104 | 2 | OS version | Major, minor |
| 106 | 16 | Source volume label | ASCII |
| 122 | 1 | Logical device | Device the file set came from |
| 123 | 1 | Reserved | |
| 124 | 1 | Compression | Bits 0–5: QIC-123 algorithm code (`01` = QIC-122). Bit 7: compressed. |
| 125 | 1 | Format and OS type | 1 = DOS Basic format; anything else = extended format |
| 126 | 2 | Reserved | |

**Flags** (byte 56):

| Bit | Meaning |
|---|---|
| 0 | Vendor-specific: only bytes 0–56 are defined by the standard |
| 1 | Volume continues on another cartridge |
| 2 | Written without verification |
| 3 | Re-direction inhibited |
| 4 | Compressed data spans segments |
| 5 | Directory last: the directory follows the data. Always set for extended format. |
| 6–7 | Reserved |

**Format and OS type** (byte 125). Rev N lists 0–6; QIC-113 Rev G §6 adds 7:

| Value | Rev N | QIC-113 Rev G |
|---|---|---|
| 0 | Unknown | Extended format, OS unknown |
| 1 | DOS | DOS Basic format |
| 2 | Unix | Unix |
| 3 | OS/2 | OS/2 |
| 4 | Novell NetWare | Novell NetWare |
| 5 | Windows NT | Windows NT |
| 6 | DOS extended format (long file names) | DOS extended format |
| 7 | — | Windows 95 |

## QIC-113 extended volumes on QIC-40/80

QIC-113 Rev G §6 marks an extended-format volume on a QIC-40 or QIC-80 tape
by setting the vendor-specific bit and writing two words into the vendor
extension data:

| Offset | Size | Field |
|---|---|---|
| 58 | 2 | `113` (`71 00`) |
| 60 | 2 | QIC-113 revision: F = 6, G = 7 |
| 62 | 22 | Free for the writer |

Bytes 84–127 then follow the Rev N layout above. Old software sees the
vendor-specific bit and leaves the volume alone.

## Vendor variants

Real tapes do not all follow the standard past byte 56. Each layout Tapewyrm
knows is a volume profile in `packages/qiclib/qiclib/volume/profiles/`:

| Profile | Fingerprint | Fields past byte 56 | Contents | Status |
|---|---|---|---|---|
| `qic80-rev-n` | Vendor bit clear (no positive mark) | Rev N offsets | QIC-113 Basic DOS | Spec |
| `qic113-rev-g` | `113` at byte 58 | Rev N offsets | QIC-113 extended | Spec |
| `cms-qic113` | `113` at 58 and `CMS` at 62 | Rev N offsets | QIC-113 extended | Bench, two tapes |
| `mtn` | `MTN` at 58 | Directory size 92, data size 96 (doubleword), label 102, compression 120 | QIC-113 Basic DOS | Bench, two tapes |
| `vendor-unknown` | Vendor bit set (no positive mark) | None trusted | Basic DOS guessed | Spec |
| `qicstream-v2` | `FSET` at volume byte 4 | Rev N offsets, unverified | QICStream control-code stream | Untested |
| `novastor` | `<<NoVaStOr>>` at volume byte 116 | None | No parser | Untested |

The vendor pages describe the bench layouts:
[Colorado (CMS) backup](vendor/cms.md) and [MTN backup](vendor/mtn.md).

## Tapewyrm notes

**Modules.** `qiclib.volume.table` walks the table (`vtbl_records`) and
decodes bytes 0–56 (`parse_vtbl_base`); its fixed Rev N reading is the
reference the `qic80-rev-n` and `qic113-rev-g` profiles are tested against.
`qiclib.volume.profile` reads the rest through profiles.

**Segment numbers are words.** Bytes 4–7 are two words. An early reader took
them as doublewords and got nonsense ranges.

**The vendor bit is not a fingerprint.** The same CMS software set the
vendor-specific bit on a QIC-80 tape and left it clear on a QIC-3010 tape,
with bytes 57–65 otherwise identical. Profiles match on what the software
wrote (the `113` and `CMS` marks), never on that flag. For the same reason
Tapewyrm accepts any QIC-113 revision at byte 60, and does not consult byte
125 to decide between Basic and extended format.

**Selection.** A profile whose fingerprint fails is eliminated. The survivors
decode every entry and are scored on plausibility: segment range inside the
data area, a sane date, a printable label, a known compression code, and a
size that fits the segments. Then the winning profile's file-set parser
probes the extracted volume, and hands over to the next survivor if it fails.
Details are in [Cartridge and volume profiles](../qicsilver/profiles.md).

**Overrides.** You can set any field offset, extent width or parser by hand
before a profile exists. See [Manual read overrides](../qicsilver/overrides.md).

**Not yet handled.** `EXVT` overflow is recognised but not followed, so a
table longer than one segment is read only as far as its first segment.
`XTBL` and `UTID` entries are skipped.

## See also

- [QIC-113 file sets](qic-113.md)
- [Vendor formats](vendor/index.md)
- [Writing a volume profile](../dev/volume-profiles.md)
