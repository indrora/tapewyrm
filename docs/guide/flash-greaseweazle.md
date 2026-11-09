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
| `tapewyrm-at32f4-VERSION.bin` | the application only | `tw dfu`, `tw update` |
| `tapewyrm-at32f4-VERSION.hex` | the Greaseweazle-compatible bootloader and the application, merged | a flasher that takes Intel HEX (Artery ISP) |
| `tapewyrm-VERSION.upd` | a Greaseweazle update file (`just fw-dist` only) | `tw update` |

Get them one of these ways:

- **Build them.** With PlatformIO Core installed, run `just fw` from the
  repository root. The build leaves the files under `dist/`, and the raw
  application image at `firmware/.pio/build/tapewyrm/firmware.bin`, which is
  the default image for `just dfu` and `just update`. `just fw-dist` also
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
    attached, pass `--device VID:PID`. See [tw dfu](../tw/update.md#tw-dfu).

5. Unplug the board, remove the DFU strap, and plug it in again.

!!! warning "Not yet confirmed on a v4.1"
    Whether the AT32F403 ROM bootloader enumerates under stock `dfu-util` is
    still an open item. The design record notes that the working recovery
    path on the bench was the DFU strap with Artery's own ISP tool. If
    `dfu-util` does not see the board, flash the merged `.hex` file with
    Artery ISP instead. Neither path can damage the ROM bootloader, so you can
    always try again.

## Update the firmware later

Once the board runs Tapewyrm or stock Greaseweazle firmware, update it over
USB through the board's application bootloader, with no strap:

```sh
tw update firmware/.pio/build/tapewyrm/firmware.bin
```

`tw update` reboots the board into its bootloader, writes and CRC-checks the
new firmware, and reboots it into the new firmware. It takes the raw
application `.bin` or the `.upd` file from `just fw-dist`. If it fails partway,
the board is left in its bootloader: run `tw update` again. See
[tw update](../tw/update.md#tw-update) for the details.

!!! note "Confirmed on a v4.1"
    `tw update` with a raw `.bin` first updated a real V4.1 on 2026-11-09,
    from October firmware to the current tree. Rewriting the bootloader
    (`tw update --bootloader`) has not been run on a real board yet.

### Update from your source tree

Do this after pulling changes to `firmware/` or `protocol/`, or whenever
`tw info` prints `tw and firmware were built from different commits`. Run it
from the repository root, with nothing reading the tape: an update reboots the
board, which ends any capture.

1. **Commit first.** The build stamps the firmware with the current commit and
   a dirty flag (`tw info` and `BUILD_INFO` report both). A board running a
   `+dirty` build can't be traced back to the exact source.
2. **Build.**

    ```sh
    just fw
    ```

    This builds the bootloader and the application and leaves the application
    image at `firmware/.pio/build/tapewyrm/firmware.bin`. PlatformIO builds
    incrementally, so a build that takes under a second is normal.
3. **Check the image is the one you meant to build.** The commit is a plain
   string inside the image:

    ```sh
    strings firmware/.pio/build/tapewyrm/firmware.bin | grep -E '^[0-9a-f]{40}$'
    git rev-parse HEAD
    ```

    The two lines must match.
4. **Flash.**

    ```sh
    just update
    ```

    This runs `tw update` on the image from step 2. It prints
    `board now running main firmware v1.6` when it is done. The image is
    reported as `v0.0` because a raw `.bin` carries no version number; that is
    expected.
5. **Verify.**

    ```sh
    tw info
    ```

    The `firmware` line must show the commit from step 3, with no `+dirty`,
    and the `note : ... different commits` line must be gone.
6. **Smoke-test a capture.** With a drive wired up and a known-good tape in it,
   read a few seconds of track 0:

    ```sh
    tw drive load-point
    tw drive track 0
    tw drive flux --motion logical --seconds 5
    ```

    The report ends with `sectors : N found at <rate> kbps (N CRC-clean)`; on
    a healthy drive almost all of them are clean. If the tape is at EOT,
    Logical Forward has nowhere to go and the probe reports `0 transitions`;
    that is why `load-point` comes first.

If step 4 fails with `board did not come back on USB`, the image is usually
already written: run `tw info` before anything else. If `tw info` can't find
the board, run `tw update` again (the bootloader is still there), and if that
fails too, use the DFU header as in
[Flash for the first time](#flash-for-the-first-time).

Use `tw update` to install Tapewyrm firmware. Installing it with `gw update`
is not supported.

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
