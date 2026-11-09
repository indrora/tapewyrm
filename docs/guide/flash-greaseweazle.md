# Flash the Greaseweazle

This page puts Tapewyrm firmware on a Greaseweazle v4.1, and takes it off
again. Stock Greaseweazle firmware has no QIC-117 commands, so `tw` won't drive
a tape with it. The board is described in
[Greaseweazle v4.1](../hardware/greaseweazle.md).

!!! warning "Tapewyrm firmware does not support `gw`"
    Tapewyrm is a hard fork of the Greaseweazle firmware. Use `tw` with a board
    that runs it; `gw` is only for putting stock firmware back.

## Get the firmware

Use a release package if you have one. Otherwise build it from the repository
root (PlatformIO Core required):

```sh
just fw-dist
```

This writes `dist/tapewyrm-VERSION/firmware/`:

| File | Contents |
|---|---|
| `tapewyrm-VERSION.upd` | update file for `tw update` |
| `tapewyrm-at32f4-VERSION.hex` | bootloader and application merged, for Artery ISP |
| `tapewyrm-at32f4-VERSION.bin` | application only, for DFU |

## Which command

| The board has | Run |
|---|---|
| stock Greaseweazle firmware | `tw update tapewyrm-VERSION.upd` |
| Tapewyrm firmware (update or go back to stock) | `tw update FILE.upd` with a Tapewyrm or stock Greaseweazle `.upd`, or `gw update --force` |
| nothing (empty or broken chip) | `tw update --dfu IMAGE` or `just dfu`, with the DFU header strapped |

`tw update` reboots the board into its bootloader, writes and CRC-checks the
image, and reboots into it. If it fails partway, the board stays in its
bootloader: run it again. See [tw update](../tw/update.md#tw-update).

For DFU, unplug the board, strap the DFU header, plug it in, flash, then unplug
and remove the strap. `dfu-util` must be on your `PATH`. If it doesn't see the
board, flash the `.hex` with Artery ISP instead; the ROM bootloader can't be
damaged, so you can always retry.

During development, `just fw` followed by `just update` rebuilds and flashes the
application `.bin` from your tree. Commit first, so `tw info` can name the
exact source.

## Verify

```sh
tw info
```

A protocol version and capability list on the `firmware` line mean Tapewyrm is
running; `stock Greaseweazle (no Tapewyrm verbs)` means it isn't. See
[tw info](../tw/info.md).

## Next steps

- [Wire up a drive](wiring.md).
