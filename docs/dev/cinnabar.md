# cinnabar

`cinnabar` is the desktop viewer. It opens a TWRF capture, a directory of
captures or a TWTI/TWTZ image and draws one track at a time as a stack of
lanes: flux, records, fields, structure and bytes. It only reads, and it reads
through the same libraries the pipeline uses, so what it labels is what
`qicsilver` would read. For what the window shows, see the
[cinnabar reference](../cinnabar/index.md).

## Package facts

| | |
|---|---|
| Directory | `packages/cinnabar` |
| Distribution | `cinnabar` |
| Import name | `cinnabar` |
| Scripts | `cinnabar` (`cinnabar.cli:cli`) |
| Depends on | `qiclib`, `tapewyrm-archive`, `tapewyrm` (editable path sources); `numpy`, `pyqtgraph`, `PySide6`, `rich`, `rich-click` |
| Depended on by | nothing |

cinnabar is a leaf. It imports `tapewyrm.codec` (the PLL and MFM framer) and
nothing else from `tapewyrm-cli`, and it may not import `qicsilver`. Nothing
imports cinnabar, which is what keeps Qt out of `tw` and `qicsilver`.

## Module map

The package repeats the repository's split in miniature: plain data, readers
and decoders with no Qt, and a Qt layer that knows nothing about QIC.

| Module | Qt | Purpose |
|---|---|---|
| `cinnabar.model` | no | What cinnabar draws: `Tape`, `Track`, segments, lanes of nested `Span`s, `ByteMap`. The contract between every other module. Read this first. |
| `cinnabar.load` | no | Readers that turn a file on disk into a `Tape`. |
| `cinnabar.load.image` | no | A TWTI/TWTZ image as tracks of segments on the byte axis. |
| `cinnabar.load.flux` | no | TWRF captures as tracks on the time axis, cut into segments at INDEX pulses and decoded as you look (`FluxTrack`). |
| `cinnabar.load.volumes` | no | Which segment holds what, and each volume's byte stream, for both loaders (`Layout`). |
| `cinnabar.annotate` | no | Decoders that turn bytes into labelled `Span` trees. |
| `cinnabar.annotate.header` | no | Spans for the header segment: format parameter record and bad-sector map. |
| `cinnabar.annotate.volume` | no | Spans for the volume table and each volume's data area. |
| `cinnabar.annotate.sectors` | no | The QIC-80 records lane: MFM sectors, on tape and in an image. |
| `cinnabar.annotate.qic100` | no | The QIC-100-MC records lane: GCR records and their pieces. |
| `cinnabar.decode` | no | Decoding a flux capture one segment at a time, in worker processes. |
| `cinnabar.decode.jobs` | no | One segment's decode as a worker runs it (`SegmentJob`, `run`, `SegmentResult`), and physical-format detection. Importable by a spawned process. |
| `cinnabar.decode.track` | no | `TrackDecoder`: a track's segments through a shared process pool, nearest the view first. |
| `cinnabar.cli` | lazy | The `cinnabar` command. Imports Qt only when it opens a window or renders a screenshot. |
| `cinnabar.console` | no | Rich console, log handler and progress bars. A copy of `tapewyrm.console`; keep them in step. |
| `cinnabar.ui` | yes | The pyqtgraph window. Draws `cinnabar.model` and nothing else. |
| `cinnabar.ui.app` | yes | `MainWindow` (menus, toolbar, lanes, track strip, status bar, details dock), `run` and `screenshot`. |
| `cinnabar.ui.lanes` | yes | The channel stack for one track: a ruler, then one row per lane. |
| `cinnabar.ui.items` | yes | The custom graphics items, one per lane kind. |
| `cinnabar.ui.overview` | yes | The bottom strip: the whole track, with the viewed window on it. |
| `cinnabar.ui.details` | yes | The details panel: decoded rows and a colour-coded hexdump of the selection. |
| `cinnabar.ui.palette` | yes | Colour themes, and the one place a span style becomes a colour. |
| `cinnabar.ui.progress` | yes | A `Progress` that reports through Qt signals, and `Cancellable`, which turns a progress report into a cancellation point. |

## Key types and extension points

- **`Span` trees** (`model`). A span has children, and each lane shows one
  depth of a tree. The records lane is depth 0 of `Segment.records`; the
  structure lanes are depths 0, 1 and 2 of `Segment.fields`. Spans carry
  their position in axis units already, so a widget never converts.
- **Two axes.** A flux track is measured in seconds from the start of the
  pass. An image track is measured in bytes of the data area, 29,696 per
  segment slot. `ByteMap` maps "byte N of this segment" to the axis while the
  segment is built.
- **A new decoder adds spans, never widgets.** To show a new structure, add a
  function in `cinnabar.annotate` that returns `Span`s and call it from the
  loader. `cinnabar.ui` should not need to change.
- **`TrackDecoder`** (`decode.track`). `submit_all` queues every segment
  ordered by distance from a focus segment; `focus` re-orders the queue;
  `pause` and `resume` cancel and requeue the segments not yet started;
  `wait` blocks until everything has landed (for `--dump` and tests); `close`
  cancels for good and frees the shared memory. One process pool is shared by
  every decoder in the process.
- **Volume reads.** `cinnabar.cli.volume_read_options` is a copy of
  qicsilver's decorator of the same name. Keep the two identical, flag for
  flag. See [qicsilver](qicsilver.md#key-types-and-extension-points).

## Test

```sh
cd packages/cinnabar
uv sync --extra dev
QT_QPA_PLATFORM=offscreen uv run pytest
```

Or `just check cinnabar cinnabar` from the repository root. `tests/test_ui.py`
sets `QT_QPA_PLATFORM=offscreen` itself; CI sets it for the whole job.

- `tests/conftest.py` builds the fixtures from invented data only: a
  three-segment QIC-80 image (header, volume table, one Basic-DOS volume), a
  second image with an Extended-OS volume, the same segments as MFM flux
  written as real TWRF files, and a QIC-100 capture.
- `cinnabar PATH --dump` prints a track's lanes as text and never imports
  Qt. It exercises everything except the window and is what most tests run.
- `cinnabar PATH --screenshot OUT.png` renders the window offscreen to a PNG
  and exits (`--window START:END` and `--click POS` set the view and the
  selection). Use it to check a UI change by eye.

## Gotchas

!!! warning "Scripts that load a flux track need a `__main__` guard"
    `TrackDecoder` starts its workers with the `spawn` start method, which
    re-imports `__main__` in every worker. A script that builds a
    `MainWindow` or loads a flux track at module level runs again in each
    worker, and each of those spawns more workers. The script hangs and leaves
    orphaned processes. Put the work under `if __name__ == "__main__":`.
    Prefer `cinnabar PATH --screenshot` (a guarded entry point) for renders,
    and write a custom script only for what it cannot do.

- **Close the decoder before a hard exit.** Call `track.decoder.close()`
  before `os._exit`, or the workers orphan to PID 1. Find leftovers with
  `pgrep -f multiprocessing.spawn`.
- **Listeners run on the pool's completion thread.** A Qt listener must hop
  to the main thread itself; `cinnabar.ui.app` does it through a signal.
- **Processes, not threads.** The PLL is pure Python under the GIL. Threads
  would not decode in parallel.
- **Never block the UI on a whole track.** A flux track takes about half a
  minute through the PLL. The loader returns at once with placeholder
  segments and fills them as workers finish, nearest the view first. Keep new
  decode work on that path.
- **Cancellation is a `BaseException`.** Once a `Cancellable` is cancelled, its
  next `task`, `advance` or `update` raises `Cancelled`. Long loops that report
  progress are cancellable for free and must not swallow `BaseException`. Code
  that takes a resource partway through a load (shared memory, a decoder)
  releases it and re-raises (`FluxTrack.__init__`).
- **Use `weakref.finalize`, not `atexit`.** `atexit.register(obj.method)`
  keeps the object alive until exit; it once held every decoder ever opened,
  and its early run during a window close leaked a shared-memory block.
- **Leave no shown widget behind in a test.** Close it, `deleteLater()` it and
  flush the deferred deletes. The session-scoped `qt_teardown` fixture sweeps
  hidden leftovers; without it PySide6 segfaults at interpreter exit about one
  run in four, with every test green.
- **The QIC-100-MC path is synthetic-only.** `qiclib.qic100` and
  `cinnabar.annotate.qic100` have only met generated data, never a real
  QIC-100 tape.
- **`just cinnabar` runs from `packages/cinnabar`**, so relative paths resolve
  from there.

## See also

- [cinnabar reference](../cinnabar/index.md)
- [Lanes](../cinnabar/lanes.md)
- [qiclib](qiclib.md)
