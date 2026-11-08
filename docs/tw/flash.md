# tw flash and tw dfu

Write a firmware image to the Greaseweazle. `tw dfu` uses the AT32 ROM
bootloader and is the path that works today. `tw flash` is the routine update
path through the board's application bootloader, and is not implemented yet.

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

## tw flash

Update the firmware through the Greaseweazle-compatible application
bootloader, over USB, with no strap.

### Synopsis

```text
tw [GLOBAL OPTIONS] flash [--dfu] IMAGE
```

### Description

The board keeps Greaseweazle's own application bootloader, and Tapewyrm keeps
its update protocol unchanged. `tw flash` is meant to speak that protocol
itself, so no `gw` install is needed.

!!! note "Not implemented yet"
    The bootloader client is not written. `tw flash IMAGE` checks that `IMAGE`
    exists, then stops with an error that tells you to use `tw dfu`. Use
    [tw dfu](#tw-dfu) for every flash until this changes.

With `--dfu`, `tw flash` takes the DFU path instead, exactly as `tw dfu IMAGE`
with its defaults. It does not take `tw dfu`'s options.

### Options

| Option | Default | Description |
|---|---|---|
| `IMAGE` | | The firmware image. Must exist. |
| `--dfu` | off | Flash through DFU instead of the application bootloader. |

The global `--port` selects the board.

### Examples

Flash through DFU with the defaults:

```sh
tw flash --dfu firmware/.pio/build/tapewyrm/firmware.bin
```

### Exit status

`1` without `--dfu`, until the bootloader client is written. With `--dfu`, as
for `tw dfu`.

## See also

- [Flash the Greaseweazle](../guide/flash-greaseweazle.md)
- [tw info](info.md), to check what is running after a flash
- [Greaseweazle v4.1](../hardware/greaseweazle.md)
- [Firmware](../dev/firmware.md)
