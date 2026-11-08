# Flash the Greaseweazle

This page puts the Tapewyrm firmware on a Greaseweazle v4.1. Stock
Greaseweazle firmware has no QIC-117 commands, so `tw` refuses to drive a tape
with it; `tw info` still describes it.

The Greaseweazle keeps its own application bootloader. Tapewyrm replaces only
the application above it, so the same board can go back to stock Greaseweazle
firmware later. The board is described in
[Greaseweazle v4.1](../hardware/greaseweazle.md).

## Prerequisites

- The tools installed ([Install the tools](install.md)).
- A Greaseweazle v4.1 and a USB cable.
- [`dfu-util`](https://dfu-util.sourceforge.net/) on your `PATH`, for the
  first flash.
- Nothing connected to the 34-pin header while you flash.

## Get a firmware image

You need one of these files:

| File | Contents | Use with |
|---|---|---|
| `tapewyrm-at32f4-VERSION.bin` | the application only | `tw dfu`, `tw flash` |
| `tapewyrm-at32f4-VERSION.hex` | the Greaseweazle-compatible bootloader and the application, merged | a flasher that takes Intel HEX (Artery ISP) |
| `tapewyrm-VERSION.upd` | a Greaseweazle update file (`just fw-dist` only) | the bootloader's update protocol |

Get them one of these ways:

- **Build them.** With PlatformIO Core installed, run `just fw` from the
  repository root. The build leaves the files under `dist/`, and the raw
  application image at `firmware/.pio/build/tapewyrm/firmware.bin`, which is
  the default image for `just dfu` and `just flash`. `just fw-dist` also
  writes the `.upd` file. The toolchain is pinned in `firmware/platformio.ini`
  and PlatformIO downloads it itself. Build details are in
  [Firmware](../dev/firmware.md).
- **Use a release package.** `just package` (or a release zip) produces
  `dist/tapewyrm-VERSION/firmware/` with the `.bin` and `.hex` files beside
  the host wheels.

## Flash for the first time

The first flash uses the AT32 microcontroller's built-in ROM bootloader. It
does not depend on any firmware on the board, so it also recovers a board
whose application or bootloader is broken.

1. Unplug the Greaseweazle from USB.

2. Strap the board's DFU header. The location of the header is shown on the
   [Greaseweazle v4.1](../hardware/greaseweazle.md) page.

3. Plug the board into USB. With the strap fitted, it starts in the ROM
   bootloader instead of the firmware.

4. Run `tw dfu` with the application image.

    ```sh
    tw dfu firmware/.pio/build/tapewyrm/firmware.bin
    ```

    `tw dfu` runs `dfu-util -a 0 -s :leave -D IMAGE`. You should see
    `dfu-util`'s progress, then `flashed IMAGE via DFU`. If `dfu-util` is not
    on your `PATH`, pass `--dfu-util PATH`. If more than one DFU device is
    attached, pass `--device VID:PID`. See [tw dfu](../tw/flash.md#tw-dfu).

5. Unplug the board, remove the DFU strap, and plug it in again.

!!! warning "Not yet confirmed on a v4.1"
    Whether the AT32F403 ROM bootloader enumerates under stock `dfu-util` is
    still an open item. The design record notes that the working recovery
    path on the bench was the DFU strap with Artery's own ISP tool. If
    `dfu-util` does not see the board, flash the merged `.hex` file with
    Artery ISP instead. Neither path can damage the ROM bootloader, so you can
    always try again.

## Update the firmware later

Once the board runs Tapewyrm or stock Greaseweazle firmware, routine updates
are meant to go through the board's application bootloader over USB, with no
strap:

```sh
tw flash firmware/.pio/build/tapewyrm/firmware.bin
```

!!! note "`tw flash` is not implemented yet"
    The application-bootloader client in `tw` is not written yet. `tw flash`
    checks that the image exists, then stops with an error that tells you to
    use `tw dfu`. Until it is, update the firmware the same way as the first
    flash. `tw flash --dfu IMAGE` takes the DFU path directly, with
    `dfu-util`'s defaults.

The bootloader is Greaseweazle's own and speaks its update protocol, so the
stock Greaseweazle `gw update` command is expected to accept the `.upd` file
from `just fw-dist`. Tapewyrm does not need or test `gw`.

## Verify

Run `tw info`. It talks to the Greaseweazle only, never the drive.

```sh
tw info
```

```text
tw        : 0.1.0, commit 5ed5c63a1b2c [git checkout]
device    : tapewyrm-GW V4.1
port      : /dev/cu.usbmodem1101
mcu       : 288 MHz, 224 kB SRAM
firmware  : 1.6, commit 5ed5c63a1b2c, protocol v1 [capture, markers, verbs]
```

(Example output. Your port, clock and commit differ.)

Check the `firmware` line:

- A protocol version and a capability list mean the Tapewyrm firmware is
  running.
- `stock Greaseweazle (no Tapewyrm verbs)` means the flash did not take.
  Flash again.
- A `note : tw and firmware were built from different commits` line means
  the host tools and the firmware come from different source trees. That is
  allowed, but rebuild one of them if a command misbehaves.

See [tw info](../tw/info.md) for each field.

## Next steps

- [Wire up a drive](wiring.md).
