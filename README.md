# Tapewyrm

**Recover QIC-40, QIC-80, QIC-3010 and QIC-3020 floppy tapes with a
Greaseweazle v4.1.**

Tapewyrm replaces the PC floppy controller a QIC-117 tape drive (Colorado,
Conner, Iomega Ditto, ...) expects with a Greaseweazle running Tapewyrm
firmware. It captures the raw flux of each track, then decodes it offline into
a tar file with the backup's own file names, dates and attributes, plus a
report of anything it could not recover.

Tapewyrm is bench software. It has recovered real tapes, but expect rough
edges.

## You need

- A Greaseweazle v4.1, a QIC-117 floppy-tape drive and a 34-pin floppy cable.
- A 5 V / 12 V supply for the drive. The Greaseweazle does not power it.
- [uv](https://docs.astral.sh/uv/), [just](https://github.com/casey/just), and
  [PlatformIO Core](https://platformio.org/install/cli) and `dfu-util` to build
  and flash the firmware.

## Quick start

Clone the repository:

```sh
git clone https://github.com/indrora/tapewyrm.git
cd tapewyrm
```

Build the firmware:

```sh
just fw
```

Flash it. Unplug the Greaseweazle, strap its DFU header, and plug it back in.
Then run the following, and unplug, unstrap and replug the board when it
finishes:

```sh
just dfu
```

Check that the board answers:

```sh
just tapewyrm info
```

Connect the drive and power it, insert a cartridge, then wake the drive and
read its status:

```sh
just tapewyrm drive select
just tapewyrm drive status
```

If `drive status` shows the drive, the cartridge and no error, you are ready
to read a tape. Continue with
[Read a tape](docs/guide/read-a-tape.md).

> [!NOTE]
> The DFU path is the only flash method that works today; `tw flash` is not
> implemented yet. See [Flash the Greaseweazle](docs/guide/flash-greaseweazle.md)
> if `dfu-util` does not see the board.

## Documentation

The full documentation is in [`docs/`](docs/index.md) and builds as a site with
`just docs` (live preview) or `just docs-build`.

| | |
|---|---|
| [Guide](docs/guide/index.md) | Install, flash, wire up a drive, read a tape, troubleshoot. |
| [tw](docs/tw/index.md), [qicsilver](docs/qicsilver/index.md), [cinnabar](docs/cinnabar/index.md) | Command reference for each tool. |
| [Hardware](docs/hardware/index.md) | The Greaseweazle, tested drives and their quirks, cartridges. |
| [Formats](docs/formats/index.md) | The QIC standards as Tapewyrm uses them, and vendor backup formats. |
| [Development](docs/dev/index.md) | Packages, firmware, protocol, building and testing. |
| [Specifications](docs/spec/README.md) | The TWRF, TWTI/TWTZ and TWVL file formats. |

Contributors and coding assistants: read [STYLE.md](STYLE.md) and
[agents/AGENTS.md](agents/AGENTS.md) first.

## License

Public domain under [The Unlicense](UNLICENSE). The firmware derives from the
[Greaseweazle firmware](https://github.com/keirf/greaseweazle-firmware) by Keir
Fraser, also under The Unlicense. No GPL code is included. The QIC standards in
`docs/qic-standards/` belong to their publishers and are not covered by The
Unlicense.
