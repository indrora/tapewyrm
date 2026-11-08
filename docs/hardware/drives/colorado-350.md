# Colorado Jumbo 350

The HP/Colorado Jumbo 350 is a QIC-80 floppy-tape drive and the drive
Tapewyrm was brought up on. It is the reference drive: every QIC-80 tape
recovered so far was dumped on one.

## Identification

Values as reported by a bench unit (jumperless, internal):

| Report | Raw value | Decoded |
|---|---|---|
| Vendor ID (32) | `0x0047` | Legacy whole-word ID 71, Colorado Memory Systems |
| ROM version (9) | `0x58` | ROM 88 |
| Drive configuration (8) | `0x90` | QIC-80 mode, 500 kbit/s |
| Drive status (6) after power-on | `0x77` | Ready, error, cartridge present, new cartridge, referenced, at BOT |
| Error code (7) after power-on | `0x001A` | Error 26, Power On Reset Occurred |

QIC-117 Rev J lists Colorado's make as "4 & 71". Split into make and model
fields, `0x0047` would read as make 1, model 7, so `tw` treats it as the
legacy whole-word ID 71. The Colorado 1400 reports a split word with make 71
instead ([Colorado 1400](colorado-1400.md)).

## Profile

Use `--profile colorado`.

| Setting | Value | Why |
|---|---|---|
| Wake | Phantom Select (46) + argument 0, then Enter Primary Mode (30) | The drive is a phantom drive: it ignores the drive-select lines until phantom-selected. |
| Step pulse width | 200 µs | Inside Rev J's 1–1100 µs. |
| Step gap | 2000 µs (2.2 ms period) | Over Rev J's 2.1 ms TSTEP maximum; this drive tolerates it. |
| Terminate gap | 3000 µs | Ends a pulse train past the 2.9 ms command time-out. |
| Report settle | 900 µs | Rev J TBIT. |

## Formats and rates

| Format | Rate | Status |
|---|---|---|
| QIC-80 | 500 kbit/s | Tested. |
| QIC-40 | 250 or 500 kbit/s | Untested. |

## Quirks

- **A bare Phantom Select does nothing.** The drive ignores command 46 unless
  it is followed by its N+2 unit argument (unit 0, two pulses). Without it the
  drive stays silent and every report times out. The `colorado` profile sends
  the argument.
- **Cue Index pulses confirm selection.** Once selected and ready, the drive
  sends cue Index pulses about 2.9 ms apart (QIC-117 Rev J §1.3). If you see
  them under `tw drive scope`, the drive is selected; look elsewhere before
  blaming timing.
- **The ACK arrives about 3 ms after the last Step pulse.** This is within
  Rev J's TACK of the command time-out.
- **Do not end the wake sequence with Soft Reset.** A reset deselects a
  phantom drive (Rev J command descriptions, Soft Reset). The profile ends
  with Enter Primary Mode instead, and `tw` reads and clears the latched
  power-on error afterwards.
- **No Report Tape Status or Report Format Segments.** The drive never
  acknowledges commands 33 or 37. Tapewyrm takes the format from the tape's
  header segment instead, so this does not affect recovery.
- **Do not send a report while one is in progress.** If a report is left
  half-clocked, the next report command is not acknowledged. Finish or abandon
  the report with Report Next Bit pulses first.
- **Error 41 after reselect.** After a Phantom Deselect and reselect the drive
  latches error 41 (Drive Wakeup Reset Occurred). It is benign; reading the
  error code clears it.
- **Dirty heads look like bad tape.** On the bench, a patch of one track that
  read badly on one pass read cleanly on a later pass after cleaning. Clean
  the head and retry a bad track before you conclude the tape is damaged.

## See also

- [Drives](index.md)
- [tw drive](../../tw/drive.md)
- [QIC-117 command interface](../../formats/qic-117.md)
