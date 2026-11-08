# tw

`tw` is the hardware half of Tapewyrm. It talks to a Greaseweazle v4.1
running the Tapewyrm firmware, drives a QIC-117 floppy-tape drive, dumps tape
tracks to TWRF flux captures, and converts the captures into a TWTI or TWTZ
tape image. It also flashes the firmware. It does not need Greaseweazle's `gw`
command. Reading backups and files out of an image is the job of
[qicsilver](../qicsilver/index.md).

The package installs the same command under two names, `tw` and `tapewyrm`.
To run it from a clone, see [Install the tools](../guide/install.md).

## Synopsis

```text
tw [GLOBAL OPTIONS] COMMAND [ARGS]...
```

## Global options

Global options go **before** the subcommand: `tw --profile colorado drive
status`, not `tw drive status --profile colorado`.

| Option | Default | Description |
|---|---|---|
| `--version` | | Print the version and exit. |
| `--port TEXT` | found automatically | The Greaseweazle's serial port. Without it, `tw` looks for the one attached board (USB `1209:4d69`) and stops if there are none or several. The config file can set it. |
| `--profile TEXT` | `auto` | The drive profile: a name (`colorado`, `teac`, ...) or a path to a `.toml` file. `auto` tries the wake-ups the Linux `ftape` driver tries. See [Drive profiles](profiles.md). The config file can set it. |
| `--config PATH` | the per-user file, if it exists | The config file to read instead of the per-user one. See [Configuration and logging](config.md). |
| `--progress` | off | Show progress bars on stderr. |
| `-v`, `--verbose` | | More log output. `-v` turns on debug logging. |
| `-q`, `--quiet` | | Less log output. `-q` shows warnings and errors only, `-qq` errors only. |
| `--help` | | Show help and exit. Works on every subcommand. |

`--profile` and `--port` matter only to the commands that open the board or
the drive. `--profile` is read even by commands that do not use it, so a bad
profile name fails every command.

## Commands

| Command | Needs | Does |
|---|---|---|
| [`tw info`](info.md) | Greaseweazle | Shows which `tw` and firmware builds are in use, and which board. |
| [`tw drive`](drive.md) | drive | Runs one QIC-117 operation by hand: select, status, reports, tape and head motion, diagnostics. Never writes to tape. |
| [`tw dump`](dump.md) | drive | Captures whole tracks to TWRF files, one Logical Forward pass each. |
| [`tw convert`](convert.md) | nothing | Decodes TWRF captures into a TWTI or TWTZ tape image. |
| [`tw inspect`](inspect.md) | nothing | Shows the header of a TWRF, TWTI/TWTZ or TWVL file. |
| [`tw flash`](flash.md) | Greaseweazle | Updates the firmware through the application bootloader. Not implemented yet. |
| [`tw dfu`](flash.md#tw-dfu) | Greaseweazle, DFU strap | First flash or recovery through the AT32 ROM bootloader, using `dfu-util`. |

## Output

Results go to stdout. Logs and progress bars go to stderr, so a command's
output can be piped or redirected cleanly:

```sh
tw inspect --json captures/mytape.twtz > captures/mytape-header.json
```

## Exit status

| Status | Meaning |
|---|---|
| `0` | The command succeeded. |
| `1` | The command failed. `tw` prints `Error:` and the reason on stderr. |
| `2` | The command line was wrong: an unknown option, a missing argument, or a value out of range. |

## See also

- [Drive profiles](profiles.md)
- [Configuration and logging](config.md)
- [Read a tape](../guide/read-a-tape.md)
