# TEAC FT-3010

The TEAC FT-3010 is a QIC-3010-capable floppy-tape drive that also reports
QIC-80 mode. It needs a tighter command cadence than the Colorado drives, and
it fails hard when a report is left unfinished.

## Identification

Values as reported by a bench unit (label on the unit: TEAC):

| Report | Raw value | Decoded |
|---|---|---|
| Vendor ID (32) | `0x01CC` | Make 7 (Wangtek/Rexon/Tecmar), model 12. ftape names this word "Wangtek 3200 / Teac 700". |
| ROM version (9) | `0x8C` | ROM 12, beta bit set |
| Drive configuration (8), no cartridge | `0x90` | QIC-80 mode, 500 kbit/s |
| Drive configuration (8), QIC-3010 cartridge | `0x50` | Extra-length tape, 500 kbit/s |
| Drive status (6), no cartridge, idle | `0x09` | Ready, write protected |
| Tape status (33), preformatted QIC-3010 400 ft | `0x64` | QIC-3010, variable length 900 Oe |

The vendor word is shared: TEAC shipped this generation under the ID ftape
lists for the Wangtek 3200. The ftape entry named "Teac FT3010TR" is a
different word, `0x03CF` (make 15). Trust the label on the unit.

`tw` currently labels configuration `0x50` as "QIC-40 mode", because bit 7
(the QIC-80 mode flag) is clear. On a QIC-3010 drive with a QIC-3010
cartridge inserted, that bit carries no meaning; read the rate bits only.

## Profile

Use `--profile teac`. It inherits `generic` and uses the `colorado` wake
(Phantom Select + Enter Primary Mode), with tighter timings:

| Setting | Value | Why |
|---|---|---|
| Wake | Phantom Select (46) + argument 0, then Enter Primary Mode (30) | What the drive answers. |
| Step pulse width | 200 µs | Inherited from `generic`. |
| Step gap | 1300 µs (1.5 ms period) | Centre of Rev J TSTEP (0.9–2.1 ms). The Colorado profiles' 2.2 ms period splits pulse trains on this drive. |
| Terminate gap | 3300 µs (3.5 ms from the last leading edge) | Past the 2.9 ms command time-out maximum. |
| ACK window | 10 ms | Inherited from `generic`. |
| Vendor IDs | `0x01CC` | Lets `tw` suggest this profile when it sees the word. |

With this profile the bench unit passed 600 of 600 reports in 60 s and two
clean load points.

## Formats and rates

| Format | Rate | Status |
|---|---|---|
| QIC-3010 | 500 kbit/s | References a preformatted 400 ft QIC-3010 tape. |
| QIC-80 | 500 kbit/s | Reports QIC-80 mode. A blank QIC-80 cartridge does not reference, as expected. |

## Quirks

- **The command time-out is about 2.2 ms.** The drive acknowledges a report
  2.2 ms after the last Step pulse, the bottom of Rev J's TTIMEOUT range
  (2.2–2.9 ms). At a 2.2 ms Step period a pulse train can split: the drive
  sees a stray single pulse (Soft Reset) or a stray five (Alternate Command
  Time-out). Use the `teac` profile's 1.5 ms period.
- **Alternate Command Time-out moves the ACK to about 6.25 ms.** If a split
  train sends command 5, the drive's time-out becomes Rev J's alternate 6.2–6.9
  ms, and reports acknowledge too late for a short ACK window. Only a Soft
  Reset or a power cycle clears that state. The `generic` 10 ms ACK window
  tolerates it.
- **Never leave a report half-clocked.** An ACK timeout, `tw drive scope
  --pulses N` with a report command, or a Soft Reset in the middle of a report
  leaves the drive in the report subcontext. Later commands are "illegal in
  report subcontext", and then every output goes dark: no cue Index, no Track
  Zero, no Write Protect, no response to any select. Only a power cycle brings
  it back.
- **Automatic Seek Load Point waits for the first status report.** After a
  cartridge is inserted, the drive holds its automatic load point until the
  first Report Drive Status (Rev J permits this, command 14). It then runs for
  about 30–50 s, during which it is not Ready and often does not acknowledge
  reports at all. Report Error Code (7) is rejected until it is Ready (Rev J
  Table 2a), so `tw drive status` shows it as not supported. `wake()` waits
  this out.
- **New-cartridge latch.** The first load point after insertion can end Ready
  with the new-cartridge and error bits still set, and a Seek Load Point sent
  while the latch is set is rejected. Read the error code, then run the load
  point again.
- **Cue Index free-runs.** Index pulses arrive every 2.5 ms, 0.9 ms wide, in
  every state, including during pulse trains and reports. Index-edge report
  timing and the firmware's wait-ready mean nothing on this drive; the profile
  uses fixed settle times.
- **Unexplained software-reset error.** After 11 idle minutes with nothing
  sent, the bench unit came up with error 27 (Software Reset Occurred) latched
  and ran its automatic load point on the first status. The cause is not
  known. `wake()` absorbs it.

## See also

- [Drives](index.md)
- [Drive profiles](../../tw/profiles.md)
- [QIC-117 command interface](../../formats/qic-117.md)
