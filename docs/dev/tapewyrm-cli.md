# tapewyrm-cli

`tapewyrm-cli` is the hardware half of Tapewyrm. It installs `tw`, which talks
to a Greaseweazle v4.1 running Tapewyrm firmware, drives a QIC-117 tape drive,
dumps tracks to TWRF flux captures, converts captures to a TWTI/TWTZ image and
flashes the firmware. It never needs or calls the `gw` executable. For the
commands themselves, see the [tw reference](../tw/index.md).

## Package facts

| | |
|---|---|
| Directory | `packages/tapewyrm-cli` |
| Distribution | `tapewyrm` |
| Import name | `tapewyrm` |
| Scripts | `tw` and `tapewyrm` (the same `tapewyrm.cli:cli`) |
| Depends on | `tapewyrm-archive`, `qiclib` (editable path sources); `click`, `pyserial`, `rich`, `rich-click` |
| Depended on by | `cinnabar`, for `tapewyrm.codec` only |

`qiclib` is a dependency for one command. `tw convert` hands decoded sectors to
`qiclib.build`; the rest of the package uses only qiclib's data types. The
codec is pure-stdlib Python. Greaseweazle's PLL is vendored as
`tapewyrm/codec/gwpll.py`, not depended on, so the decode stack imports and
tests with no hardware and no compiled wheels.

## Module map

| Module | Purpose |
|---|---|
| `tapewyrm.cli` | The `tw` command group. Importing it registers every command module. |
| `tapewyrm.cli.__main__` | `python -m tapewyrm.cli`, the same entry point as `tw`. |
| `tapewyrm.cli.app` | The root `cli` group, `AppContext`, config-file loading and the global options. |
| `tapewyrm.cli.session` | Shared drive plumbing: open the link, load the profile, wake and select the drive. |
| `tapewyrm.cli.drive` | `tw drive ...`: hand-driven drive control for bench work. |
| `tapewyrm.cli.dump` | `tw dump`. |
| `tapewyrm.cli.convert` | `tw convert`. |
| `tapewyrm.cli.info` | `tw info`: host and firmware builds, and the board. |
| `tapewyrm.cli.inspect` | `tw inspect`: the header of any Tapewyrm file. |
| `tapewyrm.cli.firmware` | `tw update` and `tw dfu`. |
| `tapewyrm.console` | Rich console, log handler and progress bars. Kept identical to `qicsilver.console` and `cinnabar.console` by hand. |
| `tapewyrm.link.transport` | `Transport` protocol, `SerialTransport` (pyserial) and `FakeTransport` (scriptable, for tests); Greaseweazle command-packet framing. |
| `tapewyrm.link.device` | `DeviceLink`: typed calls for each device transaction, the capability gate and the `LinkError` tree. No QIC semantics. |
| `tapewyrm.link.protocol` | Opcodes, marker codes and the protocol version. Generated; do not edit. |
| `tapewyrm.link.update` | Firmware flashing: the Greaseweazle-compatible bootloader client and the `dfu-util` path. |
| `tapewyrm.qic117.commands` | The QIC-117 command table (`Cmd`, `Kind`) and the N+2 argument encoder. |
| `tapewyrm.qic117.drive` | `Qic117Drive`, the semantic dispatch layer over a `DeviceLink`, and `auto_wake`. |
| `tapewyrm.qic117.profile` | The drive-profile TOML loader, inheritance and `AUTO_ORDER`. |
| `tapewyrm.qic117.status` | Error classification, plus re-exports of the report decoders in `tapewyrm_archive.qic117`. |
| `tapewyrm.tape.dump` | `tw dump`: one Logical Forward pass per track to TWRF, pass checks, retries, `--keep-all`, resume. |
| `tapewyrm.tape.fluxprobe` | `tw drive flux`: run the tape and record whatever the head sees. |
| `tapewyrm.codec.gwstream` | Parse a capture stream: Greaseweazle flux encoding plus Tapewyrm markers (wraps `tapewyrm_archive.twrf.parse_body`). |
| `tapewyrm.codec.gwpll` | Flux to MFM bitcells: Greaseweazle's software PLL, vendored. |
| `tapewyrm.codec.mfm` | IBM MFM segment framing and CCITT CRC. |
| `tapewyrm.codec.flux` | Synthetic-fixture flux loader for `codec.pipeline`. |
| `tapewyrm.codec.pipeline` | Synthetic-fixture decode pipeline. Used by tests only. |
| `tapewyrm.image.convert` | `tw convert`: decode each capture, in worker processes when `-j` is above 1, then `qiclib.build.build_image`. |
| `tapewyrm.buildinfo` | Which source this `tw` was built from: git for a checkout, the wheel's build stamp otherwise. |
| `tapewyrm.types` | Dataclasses and enums that cross module boundaries, including `DriveProfile`. |
| `tapewyrm/profiles/drive/*.toml` | The packaged drive profiles. Data, not a module. |

## Layers

```text
tapewyrm.cli       commands, options, output            (rich-click)
tapewyrm.tape      dump, flux probe                     (orchestration)
tapewyrm.qic117    QIC-117 commands, wake, reports      (drive semantics)
tapewyrm.link      DeviceLink, transport, flashing      (no QIC semantics)
       │
   USB CDC-ACM ── Greaseweazle v4.1, Tapewyrm firmware

tapewyrm.codec     flux -> bitcells -> MFM sectors      (offline, no hardware)
tapewyrm.image     sectors -> qiclib.build -> TWTI      (offline)
```

`tapewyrm.link` passes a QIC-117 command as a number of STEP pulses and
nothing else. Everything that knows what a command means lives in
`tapewyrm.qic117`. Keep it that way: it is what lets the firmware stay a
verbatim pulse engine.

## Key types and extension points

- **`DeviceLink`** (`link.device`) is the only door to the board. `open()`
  runs the capability gate: Greaseweazle `GET_INFO`, then the Tapewyrm
  `INFO` verb, which must report a protocol version of at least
  `PROTO_VERSION` and the `verbs` and `capture` capabilities. It raises
  `LinkVersionError` otherwise. Its errors are
  `LinkError`, `LinkTimeout`, `LinkVersionError` and `LinkClosed`.
- **`Transport`** (`link.transport`) is a `Protocol`. `SerialTransport` is the
  real port; `FakeTransport` queues responses and records requests, so
  `DeviceLink` and everything above it runs in tests with no board.
- **`Qic117Drive`** (`qic117.drive`) sends commands, reads reports and
  refuses any command that writes to tape (Enter Format Mode, Write
  Reference Burst) unless `allow_writes` is set. No `tw` command sets it.
- **`AppContext`** (`cli.app`) carries the shared console, the resolved
  config, the port and the profile into every command.
- **A new command group** is a new module under `tapewyrm/cli/`, imported by
  `tapewyrm/cli/__init__.py` so it registers. Import heavy or hardware modules
  inside the command function, so `tw --help` stays fast.
- **A new device verb** is a protocol change. See
  [Device protocol](protocol.md#add-a-verb).

### Drive profiles

A drive profile is a TOML file in `tapewyrm/profiles/drive/`. A new drive is a
new file, never a code change. The loader (`qic117.profile`) resolves a bare
name in that directory and treats anything path-like (a `.toml` suffix, a
separator, an existing path) as a path. `default` and `auto` are not files:
`default` is `DriveProfile.default()` with no wake, and `auto` probes the
profiles in `AUTO_ORDER` (`default`, `colorado`, `mountain`, `insight`).

```toml
name = "teac"
inherits = "generic"         # another profile, by name or path
wakeup = "colorado"          # "none" | "colorado" | "mountain" | "insight"
vendor_ids = [0x01cc]        # Report Vendor ID words this profile is for
bit_order = "lsb"            # "msb" | "lsb"
report_strategy = "fixed_settle"   # "fixed_settle" | "index_edge"
quirks = []

[timing]
pulse_us = 200
inter_pulse_us = 1800
terminate_gap_us = 3000
tack_us = 10000
report_settle_us = 900
motion_timeout_s = 20

[[wake]]                     # explicit steps; they replace what wakeup generates
cmd = "soft reset"
arg = 0
delay_ms = 1000
```

Only `name` is required. Inherit from `generic`, the packaged base with the
QIC-117 Rev J nominal timings and no wake. `inherits` resolves recursively and
a cycle is an error. `[timing]` merges one value at a time; every other key
replaces the parent's value wholesale. `inter_pulse_us` is measured from the
trailing edge of STEP, so the period the drive sees is
`pulse_us + inter_pulse_us`.

The packaged profiles are `generic`, `colorado`, `colorado.1400`, `teac`,
`mountain`, `insight`, `conner` and `iomega`. Which are tested on which drive
is on [Drive profiles](../tw/profiles.md).

## Test

```sh
cd packages/tapewyrm-cli
uv sync --extra dev
uv run pytest
```

Or `just check tapewyrm-cli tapewyrm` from the repository root. No test needs
hardware.

- Drive and link tests script a `FakeTransport`.
- Codec tests use synthetic flux from `tapewyrm.codec.flux` and
  `tapewyrm.codec.pipeline`; tape structures come from
  `qiclib.testing.builders`.
- `tests/test_protocol.py` runs `protocol/generate.py --check`, checks the
  generated enums against `protocol/protocol.toml` and asserts that
  `tapewyrm_archive.twrf.WireMarker` equals the generated `Marker`. The
  archive package keeps its own copy of the marker codes so it need not
  depend on `tw`; this test is what keeps the copies equal.

## Gotchas

- **Do not edit `tapewyrm/link/protocol.py`.** Edit `protocol/protocol.toml`
  and run `just gen`.
- **Payload layouts are mirrored by hand.** The opcodes are generated, but the
  byte layout of each verb's request and response is written out twice, in
  `firmware/src/qic/qic.c` and in `tapewyrm/link/device.py`. A change to one
  needs the same change to the other. See
  [Device protocol](protocol.md#verb-payloads).
- **A verb response is at most 64 bytes**, header included. See
  [Device protocol](protocol.md#the-64-byte-response-limit).
- **The decode is CPU-bound pure Python.** `tw convert` runs one capture per
  worker process. Memory bounds the worker count: a worker holds parsed flux
  as Python ints, about 25 bytes per byte of capture
  (`image.convert._RSS_PER_FLUX_BYTE`), and `worker_count` sizes `-j 0` from
  that and the machine's RAM.
- **Worker processes never see `rich`.** They report through
  `tapewyrm_archive.progress.QueueProgress` and a stdlib `QueueHandler`; the
  parent replays both with a `ProgressRelay`. `image.convert.decode_captures`
  is the pattern to copy.
- **Stdout is for results only.** Logs and progress bars go to stderr through
  `AppContext.console`. Do not print to stderr any other way while bars may
  be live; it tears the display.
- **Subcommands must not define `-v` or `-q`.** They are global.

## See also

- [tw reference](../tw/index.md)
- [Device protocol](protocol.md)
- [TWS-1 TWRF](../spec/twrf.md)
