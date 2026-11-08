# QIC-80-MC recording format

QIC-80-MC defines how data is laid out on a QIC-80 tape: serpentine tracks,
each a run of 32-sector segments, each segment written as if it were one
floppy-disk track. Because every sector carries a floppy-style ID, Tapewyrm
can decode a segment with an ordinary MFM floppy decoder and place every
sector by its own address, whatever order it was captured in.

Source: QIC-80-MC Rev N, 20 Mar 96 ([qic80n.pdf](../qic-standards/qic80n.pdf)).
QIC-40-MC Rev M ([qic40m.pdf](../qic-standards/qic40m.pdf)) uses the same
segment and sector layout; its differences are noted below.

## Recording parameters

| Parameter | QIC-80 | QIC-40 | Source |
|---|---|---|---|
| Method | MFM, most significant bit first | MFM | Rev N §3.1–3.2 |
| Tape speed | 34 ips at 500 kbit/s | 25 ips at 250 kbit/s, or 50 ips at 500 kbit/s | Rev N §3.4, QIC-40 Rev M §3.4 |
| Density | 14,700 bpi | 10,000 bpi | Rev N §3.5, QIC-40 Rev M §3.5 |
| Bit cell | 68 µin | 100 µin | Rev N §3.6 |
| Media | 550 Oe | 550 Oe | Rev N §3.3 |
| Tracks | 28 (0.250 in), 36 (0.315 in) | 20 | Rev N cover |

Every standard adds that "other speeds and compatible transfer rates are
possible". Drives use that freedom, so Tapewyrm never assumes a rate: each
capture records the rate the drive reported (Report Drive Configuration),
and the per-standard rate is only a fallback.

## Tracks

Tracks are serpentine: even tracks are recorded forward from BOT, odd tracks
in reverse, each referenced to a reference burst near BOT (Rev N §4, §5.1).
Every track holds the same number of segments. A Logical Forward capture of
an odd track is already in logical order, so no flux needs reversing.

## Segments

A segment is 32 sectors of 1,024 bytes: 29 data sectors and 3 ECC sectors
(Rev N §5.3). Segments are separated by 0.68 in of erased tape. On tape, each
segment is laid out like one IBM MFM floppy track (Rev N §5.3.2):

| Bytes | Value | Field |
|---|---|---|
| 80 | `4E` | Gap 4A |
| 12 | `00` | Sync |
| 3 | `C2` | Index address mark (missing clock between bits 3 and 4) |
| 1 | `FC` | Index address mark |
| 50 | `4E` | Gap 1 |
| | | *Repeated 32 times:* |
| 12 | `00` | Sync |
| 3 | `A1` | ID address mark (missing clock between bits 4 and 5) |
| 1 | `FE` | ID address mark |
| 1 | FTK | Floppy track |
| 1 | FSD | Floppy side |
| 1 | FSC | Floppy sector |
| 1 | `03` | Size code: 1,024 bytes |
| 2 | CRC | Over the preceding 8 bytes |
| 22 | `4E` | Gap 2 |
| 12 | `00` | Sync |
| 3 | `A1` | Data address mark |
| 1 | `FB` or `F8` | Data address mark; `F8` = deleted data |
| 1,024 | data | Sector data |
| 2 | CRC | Over the preceding 1,028 bytes |
| 218 + 15 | `4E` | Gap 3, with the drop-out guard |
| | | *End of repeat* |
| 274 | `4E` | Gap 4B, until the next index |

Both CRCs are CCITT, `x^16 + x^12 + x^5 + 1`, with the register preset to all
ones. A deleted-data mark (`F8`) marks a sector the format excluded; the
sectors before and between the two header segments are written this way
(Rev N §7).

The drive asserts the hardware Index line at the start of each segment
during Logical Forward. A segment boundary is therefore marked twice: by the
in-stream `C2 C2 C2 FC` mark and by the Index edge, which the firmware
records as a `SEGMENT` marker.

## Sector IDs

The ID's cylinder, head and record fields are reused as a tape coordinate
(Rev N §5.3.1):

| Name | Field | Range |
|---|---|---|
| FTK | Floppy track (ID byte 0) | 0–254 |
| FSD | Floppy side (ID byte 1) | 0 to a length-dependent maximum |
| FSC | Floppy sector (ID byte 2) | 1–128 |

One floppy track is 128 sectors, or 4 segments. One floppy side is 255
floppy tracks, or 1,020 segments. The first sector on tape track 0 is
`(FSD, FTK, FSC) = (0, 0, 1)`.

Rev N gives the coordinate algebra, where `spt` is segments per tape track:

```text
LSN = 32640 × FSD + 128 × FTK + (FSC − 1)        logical sector number
SEG = 1020 × FSD + 4 × FTK + int((FSC − 1) / 32) logical segment
TPT = SEG / spt                                  tape track
TPS = SEG mod spt                                segment within the track
FSD = SEG / 1020    FTK = (SEG mod 1020) / 4    FSC = (SEG mod 4) × 32 + 1
```

For a 425 ft, 0.250 in tape: 207 segments per track, 5,796 segments and
185,472 sectors in all (Rev N §5.3).

## Geometry by cartridge

Segments per track is fixed for QIC-40 and variable for QIC-80. For the
variable format, Rev N §5.4.1 gives the minimum for a tape length:

```text
minimum segments = int((length_in × 0.97 − 1.36 + 0.68) / 23.88)
```

| Cartridge | Tracks | Segments per track | Source |
|---|---|---|---|
| QIC-40, 205 ft (DC2000) | 20 | 68 | QIC-40 Rev M §5.3 |
| QIC-40, 307.5 ft | 20 | 102 | QIC-40 Rev M §5.3 |
| QIC-40, 1,100 ft | 20 | 365 | QIC-40 Rev M §5.3 |
| QIC-80, 425 ft | 28 | 207 minimum | Rev N §5.4.1 |
| QIC-80, 307.5 ft, fixed format code 2 | 28 | 150 | Bench |
| QIC-80, fixed format code 5 | 28 | 207 | Bench |

The header segment records the real count, and Tapewyrm always takes
geometry from there. The full cartridge list is on
[Cartridges](../hardware/cartridges.md).

## Tapewyrm notes

**Modules.** `tapewyrm.codec.gwstream` reads the captured Greaseweazle
stream, `tapewyrm.codec.gwpll` turns flux into bit cells at the capture's own
rate, and `tapewyrm.codec.mfm` finds address marks, IDs and data blocks.
`qiclib.merge` unions sectors across passes, keeping a CRC-good copy.
`qiclib.place` and `qiclib.geometry` apply the coordinate algebra.

**Floppy tracks per side is not always 255.** Rev N's constants (1,020
segments per side, 32,640 sectors per side) hold only for format code 4,
whose header records a maximum floppy track of 254. The general rule is
*floppy tracks per side = header maximum FTK + 1*. A bench tape with fixed
format code 5 records maximum FTK 149, so a side is 150 floppy tracks or 600
segments; with Rev N's 1,020 every sector beyond side 0 landed in the wrong
segment. `qiclib.geometry` takes `ftk_per_side` as a parameter (default 255).

**The header comes first.** Because placement depends on the header's
geometry, `qiclib.header.locate_header` places sectors with a default
geometry, finds the header at the start of track 0 (where any geometry
places correctly), then re-places every sector under the header's geometry.

**Out-of-range IDs are dropped.** A sector whose FSC is outside 1–128, whose
FTK is past the side, or whose FSD is negative is discarded at placement
rather than placed in a nonsense segment.

**Capture order does not matter.** Every sector locates itself, so partial,
retried and out-of-order captures reassemble. The segment markers corroborate
the IDs but are not needed for placement.

## See also

- [Error correction](ecc.md)
- [Header and bad-sector map](header.md)
- [QIC-3010 and QIC-3020](qic-3010-3020.md)
- [TWS-1 TWRF](../spec/twrf.md)
