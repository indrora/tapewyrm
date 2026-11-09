# tw update and tw dfu

Write a firmware image to the Greaseweazle. `tw update` is the routine path:
over USB, through the board's Greaseweazle-compatible application bootloader,
with no strap. `tw dfu` uses the AT32 ROM bootloader, for the first flash and
for recovery.

For the procedure, including where to get an image, see
[Flash the Greaseweazle](../guide/flash-greaseweazle.md).

## tw dfu

First flash or recovery through the AT32 ROM bootloader.

### Synopsis

```text
tw [GLOBAL OPTIONS] dfu [--dfu-util PATH] [--device VID:PID] [--alt N] IMAGE
```

### Description

Strap the board's DFU header and plug it in, and the AT32F403 starts its
built-in ROM bootloader instead of the firmware. That bootloader does not
depend on anything in flash, so this path also recovers a board whose
firmware or bootloader is broken.

`tw dfu` does not speak DFU itself. It runs the third-party
[`dfu-util`](https://dfu-util.sourceforge.net/) with this command line:

```text
dfu-util -a ALT [-d VID:PID] -s :leave -D IMAGE
```

`-s :leave` asks the bootloader to leave DFU mode and run the new firmware
when it is done. `IMAGE` is passed through unchanged; use the application
`.bin` (`firmware/.pio/build/tapewyrm/firmware.bin`, or
`tapewyrm-at32f4-VERSION.bin` from a release).

!!! warning "Not yet confirmed on a v4.1"
    Whether the AT32F403 ROM bootloader enumerates under stock `dfu-util`, and
    the ROM bootloader's USB ID (`2e3c:df11` in the source), are still open
    items. The design record notes that the recovery path that worked on the
    bench was the DFU strap with Artery's ISP tool. The ROM bootloader cannot be overwritten, so a failed attempt can
    always be retried.

### Options

| Option | Default | Description |
|---|---|---|
| `IMAGE` | | The firmware image. Must exist. |
| `--dfu-util PATH` | `dfu-util` | The `dfu-util` program to run, if it is not on your `PATH`. |
| `--device VID:PID` | none (`dfu-util` finds the device) | The USB ID of the DFU device, passed as `-d`. Use it when more than one DFU device is attached. |
| `--alt N` | `0` | The DFU alternate interface, passed as `-a`. |

### Examples

Flash the image `just fw` built:

```sh
tw dfu firmware/.pio/build/tapewyrm/firmware.bin
```

Use a `dfu-util` outside `PATH` and name the device:

```sh
tw dfu --dfu-util /opt/dfu-util/bin/dfu-util --device 2e3c:df11 dist/tapewyrm-0.1.0/firmware/tapewyrm-at32f4-1.6.bin
```

`just dfu [BIN]` runs `tw dfu` with `firmware/.pio/build/tapewyrm/firmware.bin`
as the default.

### Exit status

`0` and `flashed IMAGE via DFU` when `dfu-util` succeeded. `1` if the image is
missing, `dfu-util` is not found, or `dfu-util` exits with an error.

## tw update

Update the firmware through the Greaseweazle-compatible application
bootloader, over USB, with no strap.

### Synopsis

```text
tw [GLOBAL OPTIONS] update [--bootloader] [--dfu] IMAGE
```

### Description

The board keeps Greaseweazle's own application bootloader, and Tapewyrm keeps
its update protocol unchanged. `tw update` speaks that protocol itself, so no
`gw` install is needed. An update goes like this:

1. Ask the running firmware what it is (`GET_INFO`).
2. If the main firmware is running, tell it to reboot into the bootloader,
   then wait for the board to come back on USB. `tw` finds it again by the
   MCU unique ID in its USB serial number, so a renamed serial port does not
   matter. The serial's two-letter prefix is ignored: Tapewyrm application
   images say `TW`, while the bootloader (which `tw update` never rewrites)
   and older images say `GW`, so one update crosses both.
3. Send the image. The bootloader erases the old main firmware, writes the new
   one and checks its CRC. If the CRC fails it erases the half-written image
   and stays in the bootloader; run `tw update` again.
4. Tell the bootloader to start the new firmware, reconnect, and print the
   version it reports.

`IMAGE` is either a `.upd` update file (from `just fw-dist`; `tw` picks the
entry that matches the board's MCU) or the raw application `.bin`
(`firmware/.pio/build/tapewyrm/firmware.bin`). A raw `.bin` has no version
number, so the update is reported as v0.0. `tw` checks the image's vector
table before it touches the board and refuses an image linked for the wrong
slot, such as the bootloader's `.bin`. Intel HEX is refused; that format is
for Artery ISP.

If the board's update strap is fitted, the bootloader runs on every reset.
`tw update` still writes the firmware, then tells you to remove the strap and
power-cycle the board.

`--bootloader` rewrites the bootloader itself instead, from the running main
firmware. It needs a `.upd` file. The main firmware checks the whole image's
CRC in RAM before it erases anything, but a power cut during the write leaves
the board with no bootloader; [tw dfu](#tw-dfu) recovers it.

With `--dfu`, `tw update` takes the DFU path instead, exactly as `tw dfu IMAGE`
with its defaults. It does not take `tw dfu`'s options.

!!! note "Confirmed on a v4.1, main firmware only"
    A main-firmware update from a raw `.bin` was first run on a real V4.1 on
    2026-11-09. `--bootloader` and `.upd` files are tested only against the
    firmware's update code and the `mk_update.py` format so far. If an update
    fails partway, the bootloader is still there: run `tw update` again, or
    fall back to [tw dfu](#tw-dfu). Which command fits which board is in
    [Flash the Greaseweazle](../guide/flash-greaseweazle.md#which-command).

### Options

| Option | Default | Description |
|---|---|---|
| `IMAGE` | | A `.upd` file or the raw application `.bin`. Must exist. |
| `--bootloader` | off | Rewrite the bootloader instead of the main firmware. Needs a `.upd`. |
| `--dfu` | off | Flash through DFU instead of the application bootloader. |

The global `--port` selects the board, and `--progress` shows a progress bar
while the image is written.

### Examples

Update to the image `just fw` built:

```sh
tw update firmware/.pio/build/tapewyrm/firmware.bin
```

Update from a release:

```sh
tw --progress update dist/tapewyrm-0.1.0/firmware/tapewyrm-1.6.upd
```

`just update [IMAGE]` runs `tw update` with
`firmware/.pio/build/tapewyrm/firmware.bin` as the default.

### Exit status

`0` after a verified write, with `wrote ...` and the version the board now
runs. `1` if the image is unusable, the board refuses the update, the CRC
check fails, or the board does not come back on USB.

## See also

- [Flash the Greaseweazle](../guide/flash-greaseweazle.md)
- [tw info](info.md), to check what is running after a flash
- [Greaseweazle v4.1](../hardware/greaseweazle.md)
- [Firmware](../dev/firmware.md)
