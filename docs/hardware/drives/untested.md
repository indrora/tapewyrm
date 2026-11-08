# Conner and Iomega

This page covers the drive families Tapewyrm has a profile for but has never
run on hardware: Conner (and Archive) drives and the Iomega Ditto. Treat
their profiles as starting points. If you have one of these drives, start
with `--profile auto` and report what answers.

## Status

| Profile | Drive family | Wake sequence | Timings | Status |
|---|---|---|---|---|
| `conner` | Conner, Conner-Archive QIC-80 drives | Soft Reset (1), wait 1 s, then Enter Primary Mode (30) | Colorado values (2.2 ms Step period) | Untested placeholder |
| `iomega` | Iomega Ditto, QIC-80 and QIC-3010 generations | Soft Reset (1), wait 1 s, then Enter Primary Mode (30) | Colorado values (2.2 ms Step period) | Untested placeholder |
| `mountain` | ftape's Mountain wake: Conner, Archive, Summit/Mountain, COREtape, ComByte, PERTEC | Soft Select (23) + its literal 20-pulse train | Colorado values | Never run on hardware; tried by `auto` |
| `insight` | ftape's Motor-on wake: Insight 80 / Irwin 80SX, early Iomega | Wait 100 ms, then drive select + motor on for unit 0 | Colorado values | Never run on hardware; tried by `auto` |

!!! warning
    The `conner` and `iomega` wake sequences start with Soft Reset. ftape
    never wakes a drive that way, and a Soft Reset deselects a phantom drive
    (QIC-117 Rev J, Soft Reset). Prefer `--profile auto`, which tries ftape's
    methods and leaves `conner` and `iomega` out.

## What to expect

- **Capability levels differ.** Some basic drives do not implement Calibrate
  Tape Length (36) or Report Format Segments (37). Tapewyrm reads the tape's
  geometry from its header segment, so this matters only before the header is
  found, when `tw` falls back to 207 segments per track for QIC-80.
- **Ditto generations vary.** Iomega shipped several Ditto drives with
  different firmware. Record what yours reports with `tw drive status`.
- **Timings are the Colorado values**, a 2.2 ms Step period. That is over Rev
  J's 2.1 ms maximum. If reports fail or the drive behaves as if it was reset,
  copy the profile, inherit `generic` (2.0 ms) and try again; see
  [TEAC FT-3010](teac-ft3010.md) for what a too-slow cadence looks like.

## Bring up an untested drive

1. Run `tw --profile auto drive status`. Note which profile answered and the
   vendor ID, ROM version and configuration.
2. Run `tw drive scope` to confirm cue Index pulses and the ACK delay.
3. Copy the closest profile, set `inherits = "generic"`, and adjust the wake
   and timings. See [Drive profiles](../../tw/profiles.md).
4. Load a tape you can afford to lose and run `tw drive load-point`.

## Irwin drives

An Irwin A120/2040 on the bench answered no QIC-117 select method at all.
Irwin drives use Irwin's own command protocol and the QIC-100-MC GCR
recording, not QIC-80. Tapewyrm does not support them.

## See also

- [Drives](index.md)
- [Drive profiles](../../tw/profiles.md)
- [tw drive](../../tw/drive.md)
