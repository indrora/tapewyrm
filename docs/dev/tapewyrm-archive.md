# tapewyrm-archive

`tapewyrm-archive` holds Tapewyrm's on-disk formats (TWRF, TWTI/TWTZ and TWVL)
and the small pieces every other package shares: the QIC-117 report decoders,
the QIC-80 date decoder and the progress protocol. It is the base of the
package graph, so it stays small and stdlib-only.

## Package facts

| | |
|---|---|
| Directory | `packages/tapewyrm-archive` |
| Distribution | `tapewyrm-archive` |
| Import name | `tapewyrm_archive` |
| Scripts | none |
| Depends on | `backports.zstd`, on Python below 3.14 only |
| Depended on by | `tapewyrm-cli`, `qiclib`, `qicsilver`, `cinnabar` |

The package is stdlib-only: no `rich`, no `click`, no hardware code. The one
exception is `backports.zstd`, the official backport of the stdlib
`compression.zstd` module (PEP 784) that TWTZ images use. On Python 3.14 and
later it is not installed and the stdlib module is used. Import zstd only
through `tapewyrm_archive._zstd`.

## Module map

| Module | Purpose |
|---|---|
| `tapewyrm_archive.twrf` | TWRF raw flux captures: `RawFluxCapture`, the preamble writer and header reader, the flux-body tokenizer `parse_body`, the encoders and `WireMarker`. Spec: [TWS-1](../spec/twrf.md). |
| `tapewyrm_archive.twti` | TWTI logical tape images and TWTZ, the same stream zstd-compressed: `TapeImage`, `SegmentEntry`, `SegmentState`, `sniff`. Spec: [TWS-2](../spec/twti.md). |
| `tapewyrm_archive.twvl` | TWVL extracted backup volumes: `Volume`, `SparseVolume`, `find_holes`. Spec: [TWS-3](../spec/twvl.md). |
| `tapewyrm_archive.types` | The types the formats are made of: `CaptureHeader`, `Marker`, `MarkerKind`, `Direction`, `TapeFormat`. |
| `tapewyrm_archive.inspect` | Header inspection for every Tapewyrm file, told apart by magic (`tw inspect`). |
| `tapewyrm_archive.qic117` | What the QIC-117 report bytes stored in TWRF and TWTI headers mean: drive status, configuration, tape status, vendor ID. |
| `tapewyrm_archive.qic80date` | The QIC-80 packed "short date": the one decoder every package uses. |
| `tapewyrm_archive.progress` | The progress protocol (`Progress`, `ProgressTask`, `NULL_PROGRESS`), plus `QueueProgress` and `ProgressRelay` for worker processes. |
| `tapewyrm_archive.provenance` | How a header records the path of the file it was made from. |
| `tapewyrm_archive.errors` | `MalformedFileError` (a `ValueError`) and its subclass `TruncatedFileError`. |
| `tapewyrm_archive._zstd` | The Zstandard module: stdlib `compression.zstd` or its backport. |

| Format | Written by | Read by |
|---|---|---|
| TWRF | `tw dump`, `tw drive flux` | `tw convert`, `cinnabar` |
| TWTI / TWTZ | `tw convert` | `qiclib`, `qicsilver`, `cinnabar` |
| TWVL | `qicsilver extract` | `qicsilver inspect`, `qicsilver tar` |

## Key types and extension points

- **`parse_body`** (`twrf`) walks a capture's flux body token by token and
  returns a `ParsedStream`: intervals, INDEX positions, markers and the
  stream's end accounting. The body is Greaseweazle's flux encoding with
  Tapewyrm markers on the `0xFF` opcode escape, and it has no byte stuffing,
  so `0xFF` can appear as data. Never scan a body for `0xFF`; always tokenize
  it.
- **`WireMarker`** (`twrf`) is a copy of the marker codes in
  `protocol/protocol.toml`. The archive does not import the generated
  `tapewyrm.link.protocol`, because that would make the base depend on `tw`.
  A test in tapewyrm-cli asserts the two are equal. When you add a marker,
  update both (see [Device protocol](protocol.md)).
- **`Progress`** (`progress`) is the protocol every long library loop takes as
  `progress: Progress = NULL_PROGRESS`. A loop wraps itself in
  `with progress.task(desc, total=..., unit=...) as bar:` and calls
  `bar.advance()` or `bar.update()`. In a worker process, pass a
  `QueueProgress`; the parent replays its messages onto its real `Progress`
  with a `ProgressRelay`.
- **Errors.** A reader that meets a cut-short or malformed file raises
  `TruncatedFileError` or `MalformedFileError` with a message that says what
  failed. The CLIs turn these into a clean error and exit. Truncated and
  malformed files must never produce a traceback.
- **A format change** is a change to the specification in `docs/spec/` and to
  the module, in the same commit. There is no compatibility to keep: old files
  may stop reading, and the fix is to re-convert or re-dump.

## Test

```sh
cd packages/tapewyrm-archive
uv sync --extra dev
uv run pytest
```

Or `just check tapewyrm-archive tapewyrm_archive` from the repository root.

The tests cover round-trips of every format (`test_image_formats.py`), TWRF
framing (`test_twrf_framing.py`), malformed and truncated input
(`test_malformed.py`), inspection, provenance, the report decoders, the date
decoder and the progress protocol.

## Gotchas

- **Nothing here may log through anything but `logging`**, or report through
  anything but `tapewyrm_archive.progress`. No `print`.
- **SI sizes.** Every user-facing size is decimal (1 kB = 1000 B). A library
  that formats a size for a CLI must not divide by 1024.
- **The firmware writes TWRF bodies.** The marker payload layouts in
  `twrf.parse_body` must match the serializers in `firmware/src/qic/qic.c`
  byte for byte (little-endian). A change to either is a change to both and
  to [TWS-1](../spec/twrf.md).
- **TWTZ has no size cap.** Inputs are your own files; there is no
  decompression-bomb defence, by design.

## See also

- [Specifications](../spec/README.md)
- [tw inspect](../tw/inspect.md)
