# Colorado 1400

The HP/Colorado 1400 is a QIC-3010-class floppy-tape drive. Tapewyrm can
select it and read its reports, but the bench unit has not referenced any
tape, so no tape has been dumped on it.

## Identification

Values as reported by a bench unit:

| Report | Raw value | Decoded |
|---|---|---|
| Vendor ID (32) | `0x11C3` | Make 71 (Colorado Memory Systems), model 3 |
| ROM version (9) | `0x79` | ROM 121 |
| Drive configuration (8) | `0x98` | QIC-80 mode bit set, 1 Mbit/s |
| Tape status (33) | Supported | Format UNKNOWN with every tape tried |

## Profile

Use `--profile colorado.1400`. It is a copy of `colorado` with the same wake
sequence and timings:

| Setting | Value |
|---|---|
| Wake | Phantom Select (46) + argument 0, then Enter Primary Mode (30) |
| Step pulse width | 200 µs |
| Step gap | 2000 µs (2.2 ms period) |
| Terminate gap | 3000 µs |
| Report settle | 900 µs |

`--profile auto` does not try `colorado.1400`: its wake is identical to
`colorado`, which `auto` already tries.

## Formats and rates

| Format | Rate | Status |
|---|---|---|
| QIC-3010 | 1 Mbit/s reported; 500 kbit/s is the standard rate | Drive accepts the format and rate selects. No tape referenced. |
| QIC-80 | 500 kbit/s | Refused: Select Rate or Format returns error 31. |

## Quirks

- **It does not reference tapes.** Seek Load Point runs for about 29 s and
  ends Ready at BOT with no error, but the Referenced bit stays clear and
  Report Tape Status says format UNKNOWN. This happened with a QIC-80 tape and
  with a sealed factory QIC-3010 tape. The cause is not known; suspect the
  drive. Dump QIC-80 tapes on a [Colorado Jumbo 350](colorado-350.md).
- **It refuses QIC-80 selects.** Select Rate or Format (27) accepts
  `format qic3010` and `rate 1000`, and rejects `format qic80` and `rate 500`
  with error 31 (Rate or Format Selection Error).
- **Cue Index pulses are about 2.0 ms apart**, faster than the Jumbo 350's
  2.9 ms. Both are inside Rev J's TINX range.
- **Read Data is gated during physical motion.** Physical Forward (12)
  produces no Read Data transitions at all, on this drive and on the Jumbo
  350. A physical-motion flux probe cannot tell you whether the read channel
  works; only a Logical Forward capture on a referenced tape can.
- **The cleaning cartridge on the bench is not detected.** With it loaded the
  drive reports status `0x01` (Ready, no cartridge present) and Physical
  Forward fails with error 2 (Cartridge Not Present or Removed). A data tape in
  the same drive is detected, so the switch works and the cartridge shell does
  not reach it. `tw drive clean` cannot run with that cartridge, and no
  software option can force it: the drive refuses to move. See
  [Cartridges](../cartridges.md#cleaning-cartridges).

## See also

- [Drives](index.md)
- [tw drive](../../tw/drive.md)
- [QIC-3010 and QIC-3020](../../formats/qic-3010-3020.md)
