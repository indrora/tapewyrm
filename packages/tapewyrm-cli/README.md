# tapewyrm-cli (`tw`)

The hardware half of Tapewyrm: **`tw`** talks to a Greaseweazle v4.1 running
Tapewyrm firmware, drives a QIC-117 floppy-tape drive, dumps tracks to TWRF
flux captures, and converts those captures into a TWTI/TWTZ tape image. It also
flashes the firmware. It never needs the `gw` executable. Once you have a tape
image, [qicsilver](../qicsilver/README.md) takes over.

The Python distribution is named `tapewyrm` and installs two names for the same
command: `tw` and the long form `tapewyrm`.

## Commands

Global flags: `--port` (Greaseweazle serial port; found automatically if
unset), `--profile` (drive profile name or path; default `auto`), `--config` (a
TOML file that can set `port`, `profile`, ...; default
`~/.config/tapewyrm/config.toml` if it exists), `--progress` (progress bars on stderr),
`-v` (debug log), `-q` / `-qq` (warnings / errors only). Results go to stdout,
logs and bars to stderr.

| Command | Needs hardware | Does |
|---|---|---|
| `tw info` | Greaseweazle | Which `tw` and firmware builds are in use, and which board. Recognises stock Greaseweazle firmware too. |
| `tw drive select` / `deselect` | drive | Wake and select the drive and check it is listening (cue INDEX pulses); release a phantom-selected drive. |
| `tw drive status` / `report NAME` | drive | Every QIC-117 report the drive answers (status, error, configuration, ROM, vendor, tape status), or one of them. |
| `tw drive load-point` / `fwd` / `rev` / `stop` / `track N` / `micro up\|down` | drive | Move the tape or the head by hand. |
| `tw drive rate KBPS` / `format FMT` | drive | Select the data rate (500, 1000, 2000) or tape format (command 27). Writes nothing to tape. |
| `tw drive flux` / `scope` | drive | Diagnostics: record whatever the head sees to a TWRF file; edge-log the TRK0/INDEX/WRPROT/pin 34 lines. |
| `tw dump DIR [TRACKS] [--check]` | drive | One Logical Forward pass per track (TRACKS such as `0-27` or `0,2,5-7`; default: every track of the format the drive reports), streamed to `DIR/track-NN.twrf`, with a `dump.jsonl` log of each pass. Stops early if the tape looks unhealthy. |
| `tw convert SOURCE... IMAGE` | no | TWRF captures (dump directories or single files) -> TWTI (`.twti`, sparse) or TWTZ (`.twtz`, zstd). Several dumps of one tape are merged. |
| `tw flash IMAGE [--dfu]` | Greaseweazle | Update the firmware through the Greaseweazle-compatible bootloader. |
| `tw dfu IMAGE` | Greaseweazle (DFU strap) | First flash or recovery through the AT32 ROM bootloader, using `dfu-util`. |

`tw drive` commands never write to tape: the drive layer refuses write
commands. See the [quick start](../../docs/quickstart.md) for a full walk
through, and [firmware/README.md](../../firmware/README.md) for flashing.

## Drive profiles

A drive profile (`tapewyrm/profiles/drive/<name>.toml`) holds a drive family's
QIC-117 timings and the wake sequence `tw` sends before every session. Pick one
with `--profile NAME` or `--profile path/to/file.toml`, or set it once as
`profile = "NAME"` in the config file. The config file is `--config FILE` if
given, else the per-user `~/.config/tapewyrm/config.toml`
(`$XDG_CONFIG_HOME/tapewyrm/config.toml`; `%APPDATA%\tapewyrm\config.toml` on
Windows) if it exists. Precedence: `--profile`, then the config file, then
`auto`.

**`auto`** (the default) does what the Linux floppy-tape driver ftape does
for an unknown drive (`ftape_activate_drive()` in
`drivers/char/ftape/lowlevel/ftape-ctl.c`, Linux 2.6.19): it tries ftape's
wake-up methods in ftape's order -- `default` (ftape "None"), `colorado`
("Colorado": Phantom Select 46 + unit 0, plus Enter Primary Mode),
`mountain` ("Mountain": Soft Select 23 + 20 pulses), `insight` ("Motor-on":
unit 0's select and motor-enable lines) -- and keeps the first the drive
answers. The list is `AUTO_ORDER` in `tapewyrm/qic117/profile.py`; the full
citation is above `auto_wake` in `tapewyrm/qic117/drive.py`. "Answers" is
ftape's test: Report Drive Status succeeds within 4 tries and is not 0xff.
As in ftape, nothing is undone between tries except the Motor-on motor, which
goes off (with the select) after a failed try and at the end of every
session. Each try and its result is logged at INFO; a try the board cannot
do (Motor-on without the motor lines) is skipped with an INFO line saying
why. **Not verified on hardware:** only the Colorado wake has met real
drives; None, Mountain, Motor-on, the order and the undo have only run
against simulated drives. `conner` and `iomega` are never tried
automatically (their Soft Reset wake is not ftape's).

| Profile | Drive | State |
|---|---|---|
| `auto` | Whatever answers ftape's wake-ups | Default. Probe described above; not verified on hardware. |
| `default` | Any drive already listening | Rev J timings, no wake sequence (ftape "None"). |
| `colorado` | Colorado Jumbo 350 (and family) | Tested. Phantom drive: Phantom Select (46) with unit 0, then Enter Primary Mode. |
| `colorado.1400` | Colorado 1400 (QIC-3010) | Tested. Same addressing as the 350. |
| `mountain` | Conner, Archive, Summit, ... (ftape's vendor table) | Untested. ftape's Mountain wake: Soft Select (23) + 20 pulses. |
| `insight` | Irwin/Insight 80, early Iomega 250 | Untested. ftape's Motor-on wake: wait 100 ms, IBM PC bus unit 0 select + motor. |
| `conner` | Conner / Conner-Archive | Placeholder (Soft Reset, then Enter Primary Mode). ftape uses `mountain` for these. |
| `iomega` | Iomega Ditto | Placeholder (Soft Reset, then Enter Primary Mode). ftape tries None, Colorado or Motor-on for these. |

A wake step is a QIC-117 command name, or a line step: `delay` (just its
`delay_ms`) or `motor on` (IBM PC bus select + motor-enable for unit `arg`).

A new drive is a new TOML file, not a code change.

## Modules

| Module | What |
|---|---|
| `tapewyrm.cli` | The `tw` command group (rich-click). |
| `tapewyrm.console` | Terminal presentation: log handler and progress bars. Kept identical to `qicsilver.console` by hand. |
| `tapewyrm.link` | USB link to the board: `transport` (serial framing), `device` (`DeviceLink`, capability gate), `update` (firmware flashing), `protocol` (generated by `protocol/generate.py`; do not edit). No QIC semantics. |
| `tapewyrm.qic117` | QIC-117: command table and N+2 argument encoding, `Qic117Drive` dispatch, status and error decoding, the drive-profile loader. |
| `tapewyrm.tape` | `dump` (`tw dump`), `fluxprobe` (`tw drive flux`), `transport` (logical motion and capture). |
| `tapewyrm.codec` | Offline physical decode: Greaseweazle flux stream parsing (`gwstream`, `flux`), the Greaseweazle software PLL (`gwpll`, vendored), MFM framing and CRC (`mfm`), `pipeline`. |
| `tapewyrm.image` | `tw convert`: decoded sectors handed to `qiclib.build` to make the TWTI/TWTZ image. |
| `tapewyrm.buildinfo` | Which source this `tw` was built from (`tw info`). |
| `tapewyrm.profiles.drive` | The drive profiles above. |

Depends on [tapewyrm-archive](../tapewyrm-archive/README.md) (file formats)
and [qiclib](../qiclib/README.md) (used only by `tw convert`). See
[STYLE.md](../../STYLE.md) §2 for the package rules.

## Develop

```sh
cd packages/tapewyrm-cli
uv sync --extra dev
uv run pytest          # no hardware needed
uv run tw --help
```

Or, from the repository root, `just check tapewyrm-cli tapewyrm` (lint, format
check, mypy and tests) and `just tapewyrm ARGS`.
