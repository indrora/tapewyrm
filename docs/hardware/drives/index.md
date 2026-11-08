# Tested drives

This page lists the floppy-interface tape drives Tapewyrm has a profile for,
which of them have been on the bench, and what each one reads. Pick the drive
profile here, then pass it to `tw` with `--profile`. How profiles are written
and resolved is in [Drive profiles](../../tw/profiles.md).

## Drive table

| Drive | Profile | Status | Formats | Data rates seen | Page |
|---|---|---|---|---|---|
| HP/Colorado Jumbo 350 | `colorado` | Tested: reads QIC-80 tapes end to end | QIC-80 (QIC-40 untested) | 500 kbit/s | [Colorado Jumbo 350](colorado-350.md) |
| HP/Colorado 1400 | `colorado.1400` | Tested: commands and reports work; has not referenced a tape | QIC-3010 (drive class); QIC-80 refused on the bench | 1 Mbit/s reported | [Colorado 1400](colorado-1400.md) |
| TEAC FT-3010 | `teac` | Tested: commands, reports and load point work; references a QIC-3010 tape | QIC-80, QIC-3010 | 500 kbit/s | [TEAC FT-3010](teac-ft3010.md) |
| Conner / Archive | `conner` | Untested placeholder | QIC-80 (assumed) | Unknown | [Conner and Iomega](untested.md) |
| Iomega Ditto | `iomega` | Untested placeholder | QIC-80, QIC-3010 (assumed) | Unknown | [Conner and Iomega](untested.md) |
| Irwin A120/2040 | None | Does not answer QIC-117 | Not QIC-80: Irwin's own recording | None | [Conner and Iomega](untested.md#irwin-drives) |

"Formats" is what the drive is built for. "Data rates seen" is what Report
Drive Configuration returned on the bench, which can change with the
cartridge inserted (QIC-117 Rev J Table 2c).

## Wake-up profiles

Some profiles describe a wake-up method rather than a drive. They come from
the Linux `ftape` driver's vendor table, which sent these sequences to every
drive it probed.

| Profile | Wake sequence | Status |
|---|---|---|
| `default` | None: ask for status. Answers if the drive is already listening. | Built in. |
| `generic` | None. QIC-117 Rev J Table 1 nominal timings. The base other profiles inherit. | Spec values. |
| `colorado` | Phantom Select (46) with unit 0, then Enter Primary Mode (30). | Bench-verified on the Jumbo 350, the 1400 and the FT-3010. |
| `mountain` | Soft Select (23) and its 20-pulse train. ftape lists it for Conner, Archive, Summit, COREtape, ComByte and PERTEC drives. | Never run on hardware. |
| `insight` | Wait 100 ms, then assert drive select and motor-on. ftape lists it for Irwin/Insight 80 and early Iomega drives. | Never run on hardware. |
| `auto` | Tries `default`, `colorado`, `mountain`, `insight` in that order and keeps the first that answers. | Built in. |

Use `--profile auto` with an unknown drive, then switch to the named profile
once you know what answered.

## Identify a drive

Run `tw drive status` to read the drive's reports. The fields that identify a
drive are:

| Report | QIC-117 command | What it tells you |
|---|---|---|
| Vendor ID | 32 | Make (bits 6–15) and model (bits 0–5). Some older drives report a whole-word legacy ID. |
| ROM version | 9 | Firmware revision (bits 0–6) and a beta flag (bit 7). |
| Drive configuration | 8 | Current data rate (bits 3–4), extra-length tape (bit 6), QIC-80 mode (bit 7). |
| Tape status | 33 | Recording format and tape type of the inserted cartridge. Not every drive supports it. |

The field layouts are in [QIC-117 command interface](../../formats/qic-117.md).

## See also

- [Drive profiles](../../tw/profiles.md)
- [tw drive](../../tw/drive.md)
- [Cartridges](../cartridges.md)
