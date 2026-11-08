# qicsilver

`qicsilver` reads QIC tape images and gets the files back. It works offline on
the TWTI and TWTZ images that `tw convert` writes, and never touches hardware.

## Where it sits

`tw dump` reads the tape into raw flux captures (TWRF). `tw convert` decodes
and merges those captures into one tape image (TWTI, or TWTZ, the same image
compressed with zstd). `qicsilver` does everything after that:

```text
tw dump --> TWRF --> tw convert --> TWTI/TWTZ --> qicsilver extract --> TWVL --> qicsilver tar
                                         |                                |
                                         +--> qicsilver identify          +--> qicsilver inspect
                                         +--> qicsilver inspect / tar (straight from the image)
```

A TWVL file holds one backup volume, decompressed and laid out by its QIC-113
offsets, with the byte ranges that were lost recorded as holes. See
[TWS-3 TWVL](../spec/twvl.md).

`tw convert` is the slow step. Every `qicsilver` command takes seconds to a few
minutes.

## Synopsis

```text
qicsilver [--progress] [-v] [-q] COMMAND [ARGS]...
```

From the repository root, run it through `uv` or `just`:

```sh
uv run --project packages/qicsilver qicsilver identify captures/NAME.twtz
just qicsilver identify captures/NAME.twtz
```

## Global options

Global options go before the command name.

| Option | Default | Description |
|---|---|---|
| `--progress` | off | Show progress bars on stderr. |
| `-v`, `--verbose` | off | More log output. `-v` turns on debug logging. |
| `-q`, `--quiet` | off | Less log output. `-q` shows warnings and errors only; `-qq` shows errors only. |
| `--version` | | Print the version and exit. |
| `--help` | | Print help and exit. Every command also takes `--help`. |

`qicsilver` has no `--port`, `--profile` or `--config`: it never opens a
device. Results, including `--json` documents, go to stdout. Logs and progress
bars go to stderr.

## Commands

| Command | What it does |
|---|---|
| [`identify`](identify.md) | Reports the cartridge, header, dates, bad sectors and volume table of a tape image. |
| [`extract`](extract.md) | Writes one TWVL file per backup volume in a tape image. |
| [`inspect`](inspect.md) | Lists the files in a volume, like `tar tv`, without extracting anything. |
| [`tar`](tar.md) | Writes a volume's files to a pax tar archive, plus a damage report. |

The volume commands (`identify`, `extract`, and `inspect` and `tar` when given
an image) read the volume table through a **volume profile**. See
[Cartridge and volume profiles](profiles.md). When no profile fits a tape, you
can set the profile's layers by hand; see [Manual read overrides](overrides.md).

## Typical session

```sh
qicsilver identify captures/NAME.twtz
qicsilver inspect captures/NAME.twtz --volume 0
qicsilver --progress extract captures/NAME.twtz captures/NAME-vols
qicsilver --progress tar captures/NAME-vols/vol-00.twvl captures/NAME.tar
```

`identify` shows what is on the tape and numbers its volumes. `inspect` lists
one volume straight from the image. `extract` writes the volumes to TWVL files,
and `tar` turns one of them into an archive. `inspect` and `tar` also accept the
image directly, so `extract` is optional.

## Exit status

| Status | Meaning |
|---|---|
| `0` | The command succeeded. |
| `1` | The command failed: the file is the wrong kind, a volume does not exist, or the volume table or a profile could not be read. The message is on stderr. |
| `2` | Usage error: a bad option value or a combination of options that cannot work. Nothing is read or written. |

## See also

- [Read a tape](../guide/read-a-tape.md): the whole procedure, drive to tar.
- [tw convert](../tw/convert.md): makes the tape images `qicsilver` reads.
- [cinnabar](../cinnabar/index.md): view a capture or image lane by lane.
- [qicsilver for developers](../dev/qicsilver.md): modules and tests.
