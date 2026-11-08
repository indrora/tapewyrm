# Tapewyrm

Tapewyrm recovers QIC-40, QIC-80, QIC-3010 and QIC-3020 floppy-tape
minicartridges with a Greaseweazle v4.1. It reads raw flux from the tape and
decodes it offline into ordinary tar files that keep the backup's own file
names, dates and attributes. It also writes a report listing anything that
could not be recovered.

These were the "floppy tape" backup drives of the 1990s: the Colorado (later
HP Colorado), Conner, Iomega Ditto and similar QIC-117 drives that plug into a
PC floppy controller. Tapewyrm replaces that controller with a Greaseweazle
running Tapewyrm firmware.

!!! warning "Bench software"
    Tapewyrm works on real tapes, but it is pre-release. Commands, file formats
    and profiles can change between versions. Keep your raw captures: you can
    decode them again with newer software, but an old tape may not survive
    another read.

## How it works

```text
 tape in drive
      |  tw dump              hardware: one Logical Forward pass per track
      v
 track-NN.twrf                TWRF: raw flux + drive identity
      |  tw convert           offline: flux -> MFM -> sectors -> Reed-Solomon; merges dumps
      v
 NAME.twti / NAME.twtz        TWTI: logical tape image (TWTZ = zstd-compressed)
      |  qicsilver identify   cartridge, header, volume table (read only)
      |  qicsilver extract    QIC-122 decompression, QIC-113 offsets
      v
 vol-NN.twvl                  TWVL: one backup volume, with its holes
      |  qicsilver tar
      v
 NAME.tar + damage report
```

Only `tw dump`, `tw info`, `tw drive`, `tw flash` and `tw dfu` need the
hardware. Every later step runs offline, on any machine.

## Where to start

| If you want to | Read |
|---|---|
| Set up the tools and read your first tape | [Guide](guide/index.md) |
| Look up a `tw` command or option | [tw reference](tw/index.md) |
| Get files out of a tape image | [qicsilver reference](qicsilver/index.md) |
| Look at what is on the tape, track by track | [cinnabar](cinnabar/index.md) |
| Check whether your drive is supported | [Drives](hardware/drives/index.md) |
| Understand the on-tape formats | [Formats](formats/index.md) |
| Work on the code | [Development](dev/index.md) |
| Read or write Tapewyrm's own file formats | [Specifications](spec/README.md) |

## The tools

| Tool | Package | Does |
|---|---|---|
| `tw` | `tapewyrm-cli` | Talks to the Greaseweazle and the drive. Dumps tracks to TWRF, converts captures to TWTI images, flashes firmware. |
| `qicsilver` | `qicsilver` | Reads TWTI/TWTZ images: identifies the cartridge and backups, extracts volumes, writes tar files. No hardware. |
| `cinnabar` | `cinnabar` | A viewer for captures and images: flux, records, fields and bytes, lane by lane. |

The libraries underneath are `qiclib` (the QIC formats) and
`tapewyrm-archive` (Tapewyrm's own file formats).

## License

Tapewyrm is public domain under The Unlicense. The firmware is derived from the
Greaseweazle firmware by Keir Fraser, also under The Unlicense. The QIC
standards in `docs/qic-standards/` belong to their publishers and are not
covered by The Unlicense.
