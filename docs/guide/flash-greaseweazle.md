# Flash the Greaseweazle

This page puts the Tapewyrm firmware on a Greaseweazle v4.1. Stock
Greaseweazle firmware has no QIC-117 commands, so `tw` refuses to drive a tape
with it; `tw info` still describes it.

The Greaseweazle keeps its own application bootloader. Tapewyrm replaces only
the application above it, so the same board can go back to stock Greaseweazle
firmware later; see
[Go back to stock Greaseweazle firmware](#go-back-to-stock-greaseweazle-firmware).
The board is described in [Greaseweazle v4.1](../hardware/greaseweazle.md).

!!! warning "Tapewyrm firmware does not support `gw`"
    Tapewyrm is a hard fork of the Greaseweazle firmware and diverges from it
    on purpose. Use `tw` with a board that runs Tapewyrm firmware. Greaseweazle's
    own `gw` command is not supported against it, even where it happens to
    work. To use the board as a Greaseweazle again, flash stock firmware back.

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

Installing Tapewyrm firmware with `gw update` is not supported.

## Go back to stock Greaseweazle firmware

The bootloader is Greaseweazle's own code, so going back to stock goes through
Greaseweazle's own update path. Do it with a strap fitted, so that `gw` talks
only to the bootloader and never to the Tapewyrm firmware. Use the Update
Jumper first, and the DFU header if that fails.

### With the Update Jumper and `gw update`

You need Greaseweazle's host tools (`gw`) installed.

1. Unplug the Greaseweazle from USB.

2. Fit a jumper across the `RXI` and `TXO` pins. This is Greaseweazle's
   Update Jumper: with it fitted, the board starts in its bootloader instead
   of the firmware.

3. Plug the board into USB and run `gw update`.

    ```sh
    gw update
    ```

    `gw update` downloads the latest stock firmware release and writes it. To
    install a release you have already downloaded, pass
    `--file greaseweazle-VERSION.upd`.

4. Unplug the board, remove the jumper, and plug it in again. `gw info`
   should now report stock firmware.

This replaces the application only. The bootloader stays the one Tapewyrm
installed: Greaseweazle's bootloader code, built with Tapewyrm's USB name and
serial number. For a board that is stock down to the bootloader, use the DFU
header instead.

!!! warning "Not yet run on a v4.1"
    This route follows Greaseweazle's own update procedure, and the Tapewyrm
    bootloader's update code is unchanged from Greaseweazle's, but nobody has
    yet run it on a real board. If it fails, use the DFU header.

### With the DFU header (fallback)

The DFU header uses the AT32's ROM bootloader, like the
[first flash](#flash-for-the-first-time). It does not depend on anything on
the board, and it replaces the bootloader and the firmware together.

1. Download `greaseweazle-firmware-VERSION.zip` from the
   [Greaseweazle firmware releases](https://github.com/keirf/greaseweazle-firmware/releases).
   It contains `hex/greaseweazle-at32f4-VERSION.hex`, the stock bootloader and
   firmware merged.

2. Unplug the board, strap the DFU header, and plug it in again.

3. Flash `greaseweazle-at32f4-VERSION.hex` with Artery ISP. `dfu-util` does
   not take Intel HEX: to use it instead, convert the file to a raw image
   first and write it at `0x08000000`.

4. Unplug the board, remove the strap, and plug it in again.

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
