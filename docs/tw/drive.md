# tw drive

Runs one QIC-117 operation on the tape drive by hand: select, status, reports,
tape and head motion, and bench diagnostics.

## Synopsis

```text
tw [GLOBAL OPTIONS] drive COMMAND [ARGS]...
```

## Description

Each `tw drive` command is one short session. It opens the board, releases
every drive-select line, wakes the drive with the drive profile (global
`--profile`, default `auto`), runs the operation, and on the way out switches
off any motor line the wake turned on, releases the drive-select lines and
closes the port. Commands that move the tape stop it, or wait for the drive to
become ready, before they return.

A drive woken with Phantom Select stays selected after the command exits,
until `tw drive deselect`, a reset or a power cycle. See
[Wire up a drive](../guide/wiring.md#phantom-drives-stay-selected).

!!! note "Read-only"
    `tw drive` commands never write to tape. The drive layer refuses QIC-117
    commands that write. `rate` and `format` change what the drive selects for
    its next operation; they write nothing.

With `auto`, every command logs the wake-ups it tried at INFO level. Name the
profile with `--profile NAME` to skip the probe. See
[Drive profiles](profiles.md).

## Commands

| Command | Does |
|---|---|
| [`select`](#tw-drive-select) | Wake and select the drive, and check it is listening. |
| [`deselect`](#tw-drive-deselect) | Release a phantom-selected drive. |
| [`status`](#tw-drive-status) | Every report the drive answers. |
| [`report`](#tw-drive-report) | One report command, by name or code. |
| [`load-point`](#tw-drive-load-point) | Seek Load Point: rewind and re-reference the tape. |
| [`fwd`, `rev`, `stop`](#tw-drive-fwd-rev-and-stop) | Physical Forward or Reverse for a time; Stop Tape. |
| [`track`](#tw-drive-track) | Seek the head to a track. |
| [`micro`](#tw-drive-micro) | Micro-step the head up or down. |
| [`rate`](#tw-drive-rate) | Select the data rate. |
| [`format`](#tw-drive-format) | Select the tape format. |
| [`clean`](#tw-drive-clean) | Run a cleaning cartridge back and forth. |
| [`flux`](#tw-drive-flux) | Run the tape and record whatever the head sees. |
| [`scope`](#tw-drive-scope) | Log edges on the TRK0, INDEX, WRPROT and pin 34 lines. |

Every command takes `--help`.

## Status line

Most commands end by printing the drive status byte (Report Drive Status, 6)
and the flags set in it:

```text
status : 0x25 [ready cartridge_present referenced]
```

| Flag | Meaning |
|---|---|
| `ready` | The drive is ready for a command. |
| `error` | An error is latched. `tw` reads and clears it with Report Error Code. |
| `cartridge_present` | A cartridge is in the drive. |
| `write_protect` | The cartridge is write-protected. |
| `new_cartridge` | A cartridge was inserted since the last status. |
| `referenced` | The drive has found the tape's reference holes. `tw dump` needs this. |
| `at_bot` | The tape is at the beginning. |
| `at_eot` | The tape is at the end. |

`-` means no flag is set.

## tw drive select

Wakes and selects the drive, and checks that it is listening.

```text
tw [GLOBAL OPTIONS] drive select [--unit N]
```

Runs the profile's wake sequence, reads the status, and watches the INDEX
line for the cue pulses a selected, ready drive emits every few milliseconds
(QIC-117 Rev J Fig. 6). The drive stays selected after this command exits.

| Option | Default | Description |
|---|---|---|
| `--unit N` | the profile's (0 for every shipped profile) | The phantom unit address, 0 to 63. With a named profile, its Phantom Select step addresses unit N. With `auto`, only the candidates that use Phantom Select are re-addressed; the others send their own wake-up unchanged. |

The second line of output says what the INDEX line showed:

| Output | Meaning |
|---|---|
| `select : yes -- cue INDEX every 2.9 ms` | Selected and ready. |
| `select : answering reports, but no cue INDEX seen` | Ready, but no cue pulses. |
| `select : answering reports; drive busy (no cue INDEX until Ready)` | The drive is busy, for example re-referencing a new cartridge. |

```sh
tw drive select
tw --profile colorado drive select --unit 1
```

## tw drive deselect

Releases a phantom-selected drive with Phantom Deselect (47).

```text
tw [GLOBAL OPTIONS] drive deselect
```

Phantom drives ignore the drive-select lines, so they stay selected until told
otherwise. This command sends no wake sequence, so it does not need a
`--profile`. It prints `deselect: done`, or `deselect: still cueing (N ms)!`
if the INDEX cue pulses continue afterwards.

## tw drive status

Prints every report the drive answers.

```text
tw [GLOBAL OPTIONS] drive status
```

```text
cleared on wake: 26 Power On Reset Occurred (process error)
drive status : 0x25 [ready cartridge_present referenced]
last error   : 0x001a -> 26 Power On Reset Occurred (process error)
drive config : 0x90 -> 500 kbps, QIC-80 bit set
rom version  : 0x5c -> version 92
vendor id    : not supported by this drive (no ACK)
tape status  : not supported by this drive (no ACK)
```

(Example output.)

| Line | Report | Description |
|---|---|---|
| `cleared on wake` | Report Error Code (7) | The error the wake read and cleared, if any. Error 26 after power-on is normal. |
| `drive status` | Report Drive Status (6) | See [Status line](#status-line). |
| `last error` | Report Error Code (7) | The error code and the command it came from. Meaningful only while `error` is set; otherwise the drive repeats the last one it reported. |
| `drive config` | Report Drive Configuration (8) | The data rate the drive will use for Logical Forward, whether the QIC-80 bit is set, and `extra-length tape` when that bit is set. On Rev C and later drives the tape format is in `tape status`, not here. |
| `rom version` | Report ROM Version (9) | The firmware version; `(BETA)` if the top bit is set. |
| `vendor id` | Report Vendor ID (32) | The maker and model, and `-> profile NAME` when a shipped profile lists this ID. |
| `tape status` | Report Tape Status (33) | The tape's format and type, and `wide (8mm)` for wide cartridges. |

Older drives do not implement every report; those lines read `not supported
by this drive (no ACK)`, and `tw` clears the error the unsupported command
latched so the remaining reports still work. A drive that is not ready
answers `no answer (drive not Ready; Rev J Table 2a gates it)` instead.

## tw drive report

Runs one report command and prints its raw value.

```text
tw [GLOBAL OPTIONS] drive report NAME
```

`NAME` is a report command's name or code. The `report` prefix is optional,
and spaces, hyphens and underscores are interchangeable.

| Code | Name |
|---|---|
| 6 | `report drive status` |
| 7 | `report error code` |
| 8 | `report drive configuration` |
| 9 | `report rom version` |
| 32 | `report vendor id` |
| 33 | `report tape status` |
| 37 | `report format segments` |

```sh
tw drive report "rom version"
tw drive report 9
```

```text
report rom version (cmd 9): 0x5c 0b01011100
```

(Example output.)

## tw drive load-point

Sends Seek Load Point (QIC-117 command 14).

```text
tw [GLOBAL OPTIONS] drive load-point
```

The drive rewinds to the load point and re-references the tape. This can take
30 seconds or more. Run it when `tw drive status` does not show `referenced`.
If the wait cleared a latched error, the command prints it as `cleared: ...`.

## tw drive fwd, rev and stop

Moves the tape at full speed in physical (unreferenced) mode.

```text
tw [GLOBAL OPTIONS] drive fwd [--seconds S]
tw [GLOBAL OPTIONS] drive rev [--seconds S]
tw [GLOBAL OPTIONS] drive stop
```

`fwd` sends Physical Forward, waits, then sends Stop Tape; it stops early at
the end of the tape. `rev` does the same with Physical Reverse and stops early
at the beginning. `stop` sends Stop Tape.

| Option | Default | Description |
|---|---|---|
| `--seconds S` | `2.0` | How long to run the tape (`fwd`, `rev`). |

## tw drive track

Sends Seek Head to Track.

```text
tw [GLOBAL OPTIONS] drive track TRACK
```

`TRACK` is 0 to 63.

## tw drive micro

Micro-steps the head up or down.

```text
tw [GLOBAL OPTIONS] drive micro {up|down}
```

Moves the head a fraction of a track to re-centre it on the current track.

## tw drive rate

Selects the data rate (Select Rate or Format, 27), then confirms it from the
configuration report.

```text
tw [GLOBAL OPTIONS] drive rate {500|1000|2000}
```

The argument is in kbps. 250 kbps is not offered: its argument means 4 Mbps on
a QIC-3020 drive. A drive that cannot change rate latches error 31 (Rate or
Format Selection Error), shown in the status line.

## tw drive format

Selects the tape format the drive works in (Select Rate or Format, 27).

```text
tw [GLOBAL OPTIONS] drive format {qic40|qic80|qic3020|qic3010}
```

Writes nothing. Rev J ties the selected format to formatting; Tapewyrm uses it
to probe whether a multi-format drive needs telling before it references an
older tape. Older drives latch error 31.

## tw drive clean

Runs a cleaning cartridge back and forth over the head.

```text
tw [GLOBAL OPTIONS] drive clean [--passes N] [--seconds S]
```

QIC-117 has no clean-heads command. A cleaning cartridge cleans by passing its
tape over the head, so this command alternates Physical Forward and Physical
Reverse. It uses physical motion because a cleaning cartridge has no format
and no reference holes. Each pass stops early at the end of the tape, and Stop
Tape is always sent on the way out, even on Ctrl-C.

| Option | Default | Description |
|---|---|---|
| `--passes N` | `2` | How many passes, 1 to 20. Odd passes run forward, even ones reverse, so the default brings the tape back to about where it started. |
| `--seconds S` | `30.0` | How long each pass runs. |

Only run this with a cleaning cartridge loaded. On a data tape it writes
nothing but wears the tape. The 30-second default is not from a cited source.
Untested on the bench; see [Cartridges](../hardware/cartridges.md).

## tw drive flux

Runs the tape for a time and records whatever comes off the head.

```text
tw [GLOBAL OPTIONS] drive flux [--motion {fwd|rev|logical}] [--seconds S] [-o FILE]
```

A diagnostic for a drive that will not reference a tape, or a tape that
decodes nothing: is there any signal at all? It records a TWRF file and prints
the amount of flux, the number of INDEX pulses, a histogram of transition
spacing, and how many sectors decode.

| Option | Default | Description |
|---|---|---|
| `--motion` | `fwd` | `fwd` and `rev` use Physical Forward and Reverse, which run an unreferenced tape. `logical` uses Logical Forward, the reading mode. |
| `--seconds S` | `5.0` | How long to run the tape. |
| `-o`, `--out FILE` | `flux-probe.twrf` | The TWRF file to record into. |

Use `--motion logical` to count sectors: physical motion runs at high speed,
where sectors never decode, and the bench drives send no flux at all in it.

```sh
tw --profile colorado drive flux --motion logical --seconds 10 -o captures/probe.twrf
```

## tw drive scope

Logs edges on the TRK0, INDEX, WRPROT and pin 34 lines. A bench probe.

```text
tw [GLOBAL OPTIONS] drive scope [--pulses N] [--ms MS]
```

| Option | Default | Description |
|---|---|---|
| `--pulses N` | `0` | Send N STEP pulses (a QIC-117 command) first, 0 to 255. |
| `--ms MS` | `300` | How long to log, 1 to 10000 ms. |

It prints the initial line states, the edge count per line, and the first
edges with their times. A selected, ready drive shows cue INDEX pulses every
few milliseconds.

!!! warning
    `--pulses N` sends a raw command train. On the TEAC FT-3010 an incomplete
    report command leaves the drive dark until a power cycle. See
    [TEAC FT-3010](../hardware/drives/teac-ft3010.md).

## Exit status

`0` on success. `1` if the board or the drive does not answer, the drive
rejects the command, or the profile cannot be loaded. `2` for a bad argument.

## See also

- [Drive profiles](profiles.md)
- [Wire up a drive](../guide/wiring.md)
- [QIC-117 command interface](../formats/qic-117.md)
- [Troubleshooting](../guide/troubleshooting.md)
