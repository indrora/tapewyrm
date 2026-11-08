# Cartridges

This page describes the minicartridges Tapewyrm reads: their recording
formats, lengths and widths, how the drive reports them, and how Tapewyrm
names a cartridge from its header. It also covers cleaning cartridges.

## Cartridge catalogue

Tapewyrm names a cartridge from the tracks and segments per track recorded in
its header segment. The catalogue is `packages/qiclib/qiclib/profiles/cartridge/`,
one TOML file per entry; `qicsilver identify` prints the match. Capacities are
the standards' cover figures, before compression.

| Standard | Width | Tracks | Length | Capacity | Coercivity | Segments per track | Source |
|---|---|---|---|---|---|---|---|
| QIC-40 | 0.250 in | 20 | 205 ft (DC2000) | 40 MB | 550 Oe | 68 (fixed) | QIC-40-MC Rev M cover, §5.3 |
| QIC-40 | 0.250 in | 20 | 307.5 ft | Not stated | 550 Oe | 102 (fixed) | QIC-40-MC Rev M §5.3 |
| QIC-40 | 0.250 in | 20 | 1,100 ft | Not stated | 550 Oe | 365 (fixed) | QIC-40-MC Rev M §5.3 |
| QIC-80 | 0.250 in | 28 | 205 ft (DC2080) | 80 MB | 550 Oe | Variable | QIC-80-MC Rev N cover |
| QIC-80 | 0.250 in | 28 | 307.5 ft (DC2120) | 120 MB | 550 Oe | Variable | QIC-80-MC Rev N cover |
| QIC-80 | 0.250 in | 28 | 425 ft | 172 MB | 550 Oe | Variable, 207 minimum | QIC-80-MC Rev N cover, §5.4.1 |
| QIC-80 | 0.250 in | 28 | 1,000 ft | 400 MB | 550 Oe | Variable | QIC-80-MC Rev N cover |
| QIC-80 wide | 0.315 in | 36 | 400 ft | 200 MB | 550 Oe | Variable | QIC-80-MC Rev N cover |
| QIC-80 wide | 0.315 in | 36 | 750 ft | 400 MB | 550 Oe | Variable | QIC-80-MC Rev N cover |
| QIC-80 wide | 0.315 in | 36 | 1,000 ft | 500 MB | 550 Oe | Variable | QIC-80-MC Rev N cover |
| QIC-3010 | 0.250 in | 40 | 400 ft | 340 MB | 900 Oe | Variable | QIC-3010-MC Rev H cover |
| QIC-3010 | 0.250 in | 40 | 1,000 ft | 800 MB | 900 Oe | Variable | QIC-3010-MC Rev H cover |
| QIC-3010 wide | 0.315 in | 50 | 400 ft | 425 MB | 900 Oe | Variable | QIC-3010-MC Rev H cover |
| QIC-3010 wide | 0.315 in | 50 | 750 ft | 800 MB | 900 Oe | Variable | QIC-3010-MC Rev H cover |
| QIC-3010 wide | 0.315 in | 50 | 1,000 ft | 1,100 MB | 900 Oe | Variable | QIC-3010-MC Rev H cover |
| QIC-3020 | 0.250 in | 40 | 400 ft | 680 MB | 900 Oe | Variable | QIC-3020-MC Rev H cover |
| QIC-3020 | 0.250 in | 40 | 1,000 ft (Verbatim MC3020EX "QIC-Extra") | 1,700 MB | 900 Oe | Variable; 1,475 on the bench cartridge | Bench, against QIC-3020-MC Rev H §5.4.1 |
| QIC-3020 wide | 0.315 in | 50 | 400 ft | 833 MB | 900 Oe | Variable | QIC-3020-MC Rev H cover |
| QIC-3020 wide | 0.315 in | 50 | 750 ft | 1,600 MB | 900 Oe | Variable | QIC-3020-MC Rev H cover |

The standards' PDFs are in the [QIC standards library](../formats/standards.md).
The physical cartridge standards (QIC-159, QIC-160, QIC-161, QIC-165 to
QIC-168 and others) are listed there too.

## How Tapewyrm names a cartridge

Variable-length formats write as many segments per track as the tape holds.
Each standard's §5.4.1 gives the minimum segment count for a length, and
Tapewyrm inverts it to turn the header's count back into a length:

| Standard | Minimum segments per track | Example |
|---|---|---|
| QIC-80 | `int((length_in × 0.97 − 1.36 + 0.68) / 23.88)` | 207 for 425 ft |
| QIC-3010 | `int((length_in × 0.97 − 1.36 + 0.452) / 15.936)` | 219 for 300 ft |
| QIC-3020 | `int((length_in × 0.97 − 1.36 + 0.226) / 8.131)` | 429 for 300 ft |

QIC-40 fixes the count per length, so it needs no formula.

QIC-3010 and QIC-3020 use the same track counts, so geometry alone can match
both. `qicsilver identify` then uses the drive's Report Tape Status, which
the capture records, or format code 6 (QIC-3020 only). With neither, it lists
both candidates rather than pick one.

## Fixed-format QIC-80 tapes

QIC-80 tapes formatted before the variable-length format (QIC-80-MC Rev K
and earlier) carry format codes 2, 3 or 5 with fixed segment counts. Rev N
does not describe them. Counts seen on the bench:

| Format code | Cartridge | Segments per track | Notes |
|---|---|---|---|
| 2 | 307.5 ft DC2120-class | 150 | Two cartridges from different makers; one factory preformatted. |
| 5 | QIC-80, Colorado-formatted (length not confirmed) | 207 | Floppy tracks per side = 150. |

See [Header and bad-sector map](../formats/header.md) for how these headers
differ.

## What the drive reports

Report Tape Status (QIC-117 command 33) describes the inserted cartridge.
Not every drive supports it (the Colorado Jumbo 350 does not). Layout from
QIC-117 Rev J Table 2c:

| Bits | Field | Values |
|---|---|---|
| 0–3 | Tape format | 0 unknown, 1 QIC-40, 2 QIC-80, 3 QIC-3020, 4 QIC-3010, 5–15 reserved |
| 4–6 | Tape type | 0 unknown, 1 205 ft or 425+ ft 550 Oe, 2 307.5 ft 550 Oe (XL), 3 variable length 550 Oe, 4 1,100 ft 550 Oe, 5 reserved, 6 variable length 900 Oe, 7 reserved |
| 7 | Wide tape | 1 = 0.315 in (8 mm) |

A drive reports format 0 (unknown) until the tape is referenced. A blank or
unformatted cartridge stays unknown. Tapewyrm records the raw report in every
capture and trusts the tape's header segment over it.

Report Drive Configuration (command 8) bit 6, extra length, is set when the
drive detects a non-standard-length tape.

## Cleaning cartridges

QIC-117 Rev J (Report Drive Status, bit 2) says a drive recognises a cleaning
cartridge when Cartridge Present is clear and the Write Protect line is
inactive. QIC-147 (Cleaning Cartridge Recognition) and QIC-176 (Cleaning
Cartridge Recognition with Light Prism) describe how the cartridge signals
this.

`tw drive clean` runs alternating Physical Forward and Physical Reverse
passes. It has not been verified on hardware.

!!! note
    On the bench, a QIC cleaning cartridge did not trip a Colorado 1400's
    cartridge-present switch. The drive reported no cartridge and refused
    motion with error 2 (Cartridge Not Present or Removed), while a data tape
    in the same drive was detected. If your drive does not see a cleaning
    cartridge, use a different cleaning cartridge or clean the head by hand.
    No `tw` option can make the drive move.

## See also

- [QIC-80-MC recording format](../formats/qic-80-mc.md)
- [QIC-3010 and QIC-3020](../formats/qic-3010-3020.md)
- [Cartridge and volume profiles](../qicsilver/profiles.md)
- [Drives](drives/index.md)
