# Drive profiles

A drive profile holds a drive family's QIC-117 timings and the wake sequence
`tw` sends before every drive session. A profile is a TOML file; a new drive
is a new file, not a code change.

## Choose a profile

Pick a profile with the global `--profile` option, or set it once in the
config file. `tw` resolves it in this order:

1. `--profile NAME` or `--profile PATH` on the command line.
2. `profile = "NAME"` in the config file ([Configuration and logging](config.md)).
3. `auto`.

A bare name loads `tapewyrm/profiles/drive/NAME.toml` from the
`tapewyrm-cli` package. A value with a path separator or a `.toml` suffix, or
one that names an existing file, is loaded as a path. `default` and `auto` are
built in and have no file.

The profile is loaded when `tw` starts, so a profile that does not load fails
every command with `could not load profile 'NAME': ...`.

## Shipped profiles

| Profile | Drive | Wake sequence | State |
|---|---|---|---|
| `auto` | Whatever answers | Probes `default`, `colorado`, `mountain`, `insight` in turn; see [auto](#auto). | Default. Only the Colorado wake-up is tested on hardware. |
| `default` | A drive that is already listening | None. | Built in. Timings as below. |
| `generic` | Base for other profiles | None. | Rev J nominal timings (2.0 ms STEP period). Meant to be inherited. |
| `colorado` | Colorado Jumbo 350 and family | Phantom Select (46) unit 0, then Enter Primary Mode (30). | Tested. |
| `colorado.1400` | Colorado 1400 (QIC-3010) | As `colorado`. | Tested. Refuses the QIC-80 rate and format; see [Colorado 1400](../hardware/drives/colorado-1400.md). |
| `teac` | TEAC FT-3010 (vendor ID `0x01cc`) | As `colorado`, inherited from `generic`, with a 1.5 ms STEP period. | Tested. See [TEAC FT-3010](../hardware/drives/teac-ft3010.md). |
| `mountain` | Conner, Archive, Summit and others in `ftape`'s vendor table | Soft Select (23) with its 20-pulse train. | Untested. |
| `insight` | Irwin/Insight 80, early Iomega | Wait 100 ms, then assert unit 0's drive-select and motor-enable lines. | Untested. |
| `conner` | Conner, Conner-Archive | Soft Reset, then Enter Primary Mode. | Placeholder. `ftape` uses the Mountain wake-up for these drives. |
| `iomega` | Iomega Ditto | Soft Reset, then Enter Primary Mode. | Placeholder. |

Drive identification and the profile to use for each tested drive are on the
[Drives](../hardware/drives/index.md) pages.

## auto

`auto` is the default when no profile is named anywhere. It does what the
Linux floppy-tape driver `ftape` does for a drive it does not know yet
(`ftape_activate_drive()` in `ftape-ctl.c`, Linux 2.6.19): it tries `ftape`'s
four wake-up methods in `ftape`'s order and keeps the first one the drive
answers.

| Try | Profile | `ftape` method | What `tw` sends |
|---|---|---|---|
| 1 | `default` | None | Nothing; just Report Drive Status. |
| 2 | `colorado` | Colorado | Phantom Select (46) with unit 0, then Enter Primary Mode (30). |
| 3 | `mountain` | Mountain | Soft Select (23) and its 20-pulse train. |
| 4 | `insight` | Motor-on | Wait 100 ms, then assert unit 0's drive-select and motor-enable lines (cable pins 14 and 10). |

"Answers" is `ftape`'s test: Report Drive Status succeeds within 4 tries and
is not `0xff`. These are the wake-ups `ftape` sent, unasked, to any drive on
the cable; that is the precedent for sending them.

Like `ftape`, `tw` undoes nothing between tries except the motor: after a
Motor-on try that gets no answer, it switches the motor off and releases the
select. It also switches that motor off at the end of every session. If no
drive answers, it stops with `no drive answered auto-detection`. A try the
board cannot do is skipped with a log line saying why.

Each try and its result is logged at INFO:

```text
INFO     auto: trying default: no wake steps
INFO     auto: default: no answer (command 6: no ACK bit -- ...)
INFO     auto: trying colorado: phantom select 0, enter primary mode
INFO     auto: drive answered colorado; using profile 'colorado'
```

(Example output.)

`auto` never tries these:

- `colorado.1400` and `teac`: they wake the same way as `colorado`. A
  Colorado 1400 is picked up as `colorado`, which has the same wake-up and
  timings. A TEAC FT-3010 needs the `teac` timings; name it.
- `conner` and `iomega`: `ftape` never wakes a drive with Soft Reset, and a
  Soft Reset deselects a phantom drive.

A phantom drive that an earlier session left selected answers try 1, so the
second command after a cold start usually reports `using profile 'default'`.
That is expected.

!!! note "Not yet checked on hardware"
    Only the Colorado wake-up has met real drives. The None, Mountain and
    Motor-on tries, their order, the 4-try status test and the Motor-on undo
    have only run against simulated drives. That the Motor-on lines match what
    a PC floppy controller does under `ftape` is a reading of the source, not
    a measurement. If `auto` misbehaves, name the profile and report it.

## Profile file format

```toml
# my-drive.toml
name = "my-drive"
inherits = "generic"
wakeup = "colorado"
vendor_ids = [0x01cc]
bit_order = "lsb"
report_strategy = "fixed_settle"
quirks = []

[timing]
pulse_us = 200
inter_pulse_us = 1800
terminate_gap_us = 3000
tack_us = 10000
report_settle_us = 900
motion_timeout_s = 20

[[wake]]
cmd = "phantom select"
arg = 0
delay_ms = 50

[[wake]]
cmd = "enter primary mode"
delay_ms = 50
```

Every key is optional.

### Top-level keys

| Key | Default | Description |
|---|---|---|
| `name` | the file name without `.toml` | The profile's name, used in log lines. Never inherited. |
| `inherits` | none | Another profile, by name or path, to start from. See [Inheritance](#inheritance). |
| `wakeup` | `none` | One of `ftape`'s wake-up methods: `none`, `colorado`, `mountain` or `insight`. Expands to that method's wake steps. Ignored when the profile has `[[wake]]` steps. |
| `vendor_ids` | `[]` | The Report Vendor ID (32) words this profile was written for. `tw drive status` names the profile when the drive reports one of them. Only the shipped profiles are searched. |
| `bit_order` | `lsb` | The bit order of report bits clocked off TRK0: `lsb` or `msb`. QIC-117 reports are LSB first. |
| `report_strategy` | `fixed_settle` | How the firmware samples each report bit. `fixed_settle` samples a fixed time after the second Report Next Bit pulse (`report_settle_us`). `index_edge` waits for an INDEX cue per bit instead; untested on hardware. |
| `quirks` | `[]` | A list of strings. Reserved; no code reads it yet. |

### [timing]

| Key | Default | Description |
|---|---|---|
| `pulse_us` | `200` | STEP pulse width, in microseconds. QIC-117 Rev J Table 1 allows 1 to 1100. |
| `inter_pulse_us` | `2000` | Gap between STEP pulses, measured from the trailing edge of one to the leading edge of the next. The period the drive sees is `pulse_us + inter_pulse_us`; Rev J allows 0.9 to 2.1 ms. |
| `terminate_gap_us` | `3000` | Gap that ends a command train. It must exceed the drive's command time-out (2.2 to 2.9 ms), or the next train merges into this one. |
| `tack_us` | `10000` | How long to wait for a report's ACK bit after the terminate gap. |
| `report_settle_us` | `900` | How long after the second Report Next Bit pulse to sample the report bit (`fixed_settle`). Rev J: within 900 µs. |
| `motion_timeout_s` | `20` | Host-side ceiling on waiting for Ready after a motion command that has no time-out of its own. Not sent to the firmware. |

The defaults are the ones the built-in `default` profile uses. `generic` sets
`inter_pulse_us = 1800`, for the 2.0 ms nominal period. The older profiles
(`colorado`, `mountain` and the rest) keep 2000, a 2.2 ms period, which the
Colorado drives tolerate and the TEAC FT-3010 does not.

### [[wake]]

Each `[[wake]]` table is one step of the wake sequence, run in order.

| Key | Default | Description |
|---|---|---|
| `cmd` | required | A QIC-117 command name in lower case (`phantom select`, `soft select`, `soft reset`, `enter primary mode`), or a line step (below). |
| `arg` | none | The command's argument, if it takes one. Phantom Select's is the unit address. |
| `delay_ms` | `0` | How long to wait after the step. |

Two line steps send no QIC-117 command:

| Step | Does |
|---|---|
| `delay` | Waits `delay_ms`. |
| `motor on` | Asserts the drive-select and motor-enable lines of IBM PC bus unit `arg` (unit 0: cable pins 14 and 10; unit 1: pins 12 and 16). |

`[[wake]]` steps replace whatever `wakeup` would have generated. Do not end a
phantom drive's sequence with Soft Reset: a reset deselects a phantom drive.

After the steps, `tw` reads the status, which also reads and clears any
latched error (error 26, Power On Reset Occurred, is normal after power-on).

`tw drive select --unit N` rewrites the `arg` of a profile's Phantom Select
step for that one command.

### Inheritance

`inherits` names a parent profile, resolved the same way as `--profile`. The
child wins key by key:

- `[timing]` merges one value at a time, so a child can change one number.
- Every other key, including `wakeup`, `wake`, `vendor_ids` and `quirks`, is
  replaced whole when the child sets it. Lists are never concatenated.
- `name` is never inherited.

Inheritance chains are followed to the end. A cycle is an error.

`teac.toml` is the shortest example: it inherits `generic`, sets
`wakeup = "colorado"` and `vendor_ids`, and overrides two timing values.

## Write a profile for a new drive

1. Copy `generic.toml` or the shipped profile closest to your drive.
2. Set `name`, and `vendor_ids` from `tw drive status` once the drive answers.
3. Load it by path: `tw --profile ./my-drive.toml drive status`.
4. Check the cue INDEX and the reports with `tw drive select` and
   `tw drive scope`.

## See also

- [Wire up a drive](../guide/wiring.md#drive-selection)
- [tw drive](drive.md)
- [Configuration and logging](config.md)
- [QIC-117 command interface](../formats/qic-117.md)
