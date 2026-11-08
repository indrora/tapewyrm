# Wire up a drive

This page connects a QIC-117 floppy-tape drive to the Greaseweazle and gets
the drive listening. A floppy-tape drive uses the same 34-pin cable as a
floppy drive, but it is commanded by pulses on the STEP line and answers on
TRK0, so selecting it works differently from a floppy drive. The protocol is
summarised in [QIC-117 command interface](../formats/qic-117.md).

## Prerequisites

- A Greaseweazle v4.1 running the Tapewyrm firmware
  ([Flash the Greaseweazle](flash-greaseweazle.md)).
- A 34-pin floppy cable.
- A PC power supply, or a bench supply with 5 V and 12 V outputs and a 4-pin
  floppy-drive power connector.
- The drive's page under [Drives](../hardware/drives/index.md), if there is
  one.

!!! warning "Do not power the drive from the Greaseweazle"
    The Greaseweazle's power header supplies 5 V only, and a tape drive needs
    12 V for its motors. Power the drive from a separate supply. Leave the
    Greaseweazle on USB power.

## Connect the drive

1. Connect the drive to the Greaseweazle's 34-pin header with the floppy
   cable, pin 1 to pin 1. Pin 1 is usually marked by a red stripe on the
   cable and a small `1` or a square pad on each connector.

    Put one drive on the cable. Phantom-select drives on a shared cable
    can interfere with each other; see [Drive selection](#drive-selection).

2. Connect the drive to the external supply's 4-pin power connector.

3. Connect the Greaseweazle to the host over USB.

4. Switch the supply on.

5. Check the board with `tw info`.

    ```sh
    tw info
    ```

    You should see the `firmware` line with a protocol version. If not, see
    [Flash the Greaseweazle](flash-greaseweazle.md#verify).

6. Wake the drive and check it is listening.

    ```sh
    tw drive select
    ```

    ```text
    status : 0x25 [ready cartridge_present referenced]
    select : yes -- cue INDEX every 2.9 ms
    ```

    (Example output.) `select : yes` means the drive is selected and ready:
    a selected, ready drive pulses its INDEX line every few milliseconds. If
    `tw` stops with `no drive answered auto-detection`, see
    [The drive does not answer](troubleshooting.md#the-drive-does-not-answer).

## Drive selection

A floppy controller selects a drive by asserting one of the drive-select lines
on the cable. Many floppy-tape drives do not use those lines. `tw` wakes the
drive with a **drive profile**, which says how the drive expects to be
selected. The default profile, `auto`, tries the four wake-ups the Linux
`ftape` driver tries, in `ftape`'s order, and keeps the first one the drive
answers. Profiles are listed in [Drive profiles](../tw/profiles.md).

| How the drive is selected | Examples | Profile | Cable notes |
|---|---|---|---|
| **Phantom Select**: a QIC-117 command (46) followed by a unit-address argument | Colorado Jumbo 350, Colorado 1400, TEAC FT-3010 | `colorado`, `colorado.1400`, `teac` | The drive ignores the drive-select lines, so the cable twist and any select jumper do not matter. |
| **Soft Select**: a QIC-117 command (23) followed by a 20-pulse train | Conner, Archive, Summit (per `ftape`) | `mountain` | Untested. |
| **Motor-on**: the drive-select and motor-enable lines of unit 0 | Irwin/Insight 80, early Iomega (per `ftape`) | `insight` | Untested. `tw` drives cable pins 14 and 10 (unit 0 on the IBM PC bus). A drive strapped as unit 1 (pins 12 and 16) needs a copy of the profile with `arg = 1`. |
| **Already listening**: the drive answers without a wake | a phantom drive left selected by an earlier session | `default` | |

Apart from the Motor-on wake, `tw` releases every drive-select line before it
talks to the drive, and again at the end of every command.

### Phantom drives stay selected

A drive selected with Phantom Select **stays selected** after `tw` exits. It
stays selected until you run `tw drive deselect`, the drive is reset, or you
switch its power off. Two consequences:

- The second `tw` command after a cold start usually reports
  `drive answered default`. The drive was still listening from the first
  command, so `auto`'s first try, which sends nothing, got an answer. This is
  expected; `default` has the same timings as `colorado`.
- Two phantom drives on one cable can both be selected at once. Run
  `tw drive deselect` before you switch to the other drive, or keep one drive
  per cable.

The Colorado drives and the TEAC answer at unit address 0. To try another
address, run `tw drive select --unit N`. See [tw drive](../tw/drive.md#tw-drive-select).

## Choose a profile

`auto` works for the tested drives. Name a profile when you know the drive,
when `auto` picks the wrong one, or when the drive needs one `auto` never
tries (`teac`, `conner`, `iomega`, or your own file). Put the profile before
the subcommand:

```sh
tw --profile colorado drive status
```

To set it once, put it in the config file
(`~/.config/tapewyrm/config.toml`):

```toml
profile = "colorado"
```

See [Configuration and logging](../tw/config.md).

!!! warning "TEAC FT-3010"
    Use `--profile teac`. `auto` would talk to it with a 2.2 ms STEP period,
    which this drive does not tolerate; `teac` uses 1.5 ms. An interrupted report command can
    leave it unresponsive until you switch its power off and on. Details are
    on the [TEAC FT-3010](../hardware/drives/teac-ft3010.md) page.

## Next steps

- [Read a tape](read-a-tape.md).
