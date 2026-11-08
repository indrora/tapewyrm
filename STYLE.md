# Tapewyrm style guide

> **Assistants (Claude, Copilot, anything else):** read this file before you
> change code here, and follow it. If you find a convention in the code that
> this guide does not describe, add it here rather than inventing your own.

This guide describes how the code in this repository is *already* written. It is
derived from the existing host (Python) and firmware (C) sources, not from an
aspirational ideal. When in doubt, read the neighbouring code and match it.

---

## 1. Repository-wide rules

### 1.1 Line endings: CRLF

Every tracked text file uses **CRLF** line endings (Python, C, Markdown, TOML,
the justfile). Preserve them on edit. The one exception is `uv.lock`, which
uv rewrites with LF on every lock; `.gitattributes` pins it to LF, so leave it
LF. `protocol/generate.py` writes its outputs with CRLF itself.

If a tool writes LF, convert before committing, e.g.:

```sh
perl -pi -e 's/(?<!\r)\n/\r\n/' path/to/file
```

### 1.2 The spec is the source of truth, and we cite it

This project reimplements old, poorly-documented formats (QIC-40/80/3010/3020,
QIC-113, QIC-117, QIC-122) and talks to real, quirky hardware. Every non-obvious
number, offset, bit or timing in the code **cites where it came from**:

- a section of `agents/DESIGN.md` (`DESIGN.md §7.3`), or
- a QIC standard in `docs/qic-standards/` (`QIC-80-MC Rev N §7.1`,
  `QIC-117 Rev J error 19`), or
- a bench observation (`the bench Colorado tape (code 5) ...`).

Field comments carry their byte offsets (`# 24-25`, `# 56 bit 0`).

### 1.3 Comments explain *why*, at length when it matters

Comments are prose, in full sentences. Short code that encodes a long story
gets the long story, including:

- **what the spec says vs. what real hardware does** (e.g. "Rev N says 255; the
  bench tape says 150");
- **past bugs the code guards against** (e.g. "this used to leak the 0-based
  values, so the bench tape read as 11/22 instead of 23 December"), so nobody
  "simplifies" the fix away;
- **ordering constraints** ("Must run BEFORE Reed-Solomon correction: ...").

Do not narrate what the code obviously does.

### 1.4 Markers

- `TODO:` for known missing work, with enough context to act on it.
- `TODO(bench):` for anything that must be confirmed against a real drive,
  tape or scope (DESIGN.md §13.6).

### 1.5 Prefer the standard library and existing helpers

Reuse what the codebase already has (`qiclib.header`, `qiclib.volume.table`, `qiclib.place`,
`qiclib.rs.verify`, `qiclib.vote`, `qiclib.segment`, `tapewyrm_archive.twrf`, `tape.dump.drive_identity`, ...) before
writing a new helper. When two call sites do the same multi-step dance, factor
it into one function both call.

---

## 2. Python packages (`packages/*`)

Every Python package in `packages/` follows this section: `tapewyrm-cli`
(`tw`), `tapewyrm-archive`, `qiclib`, `qicsilver` and `cinnabar`. Rules written
about `tw` apply to every CLI (`tw`, `qicsilver`, `cinnabar`): the same
rich-click front end, the same global `--progress` / `-v` / `-q` flags, the
same stdout/stderr split, and the same logging rules in §2.5. Rules written
about "library code" apply to every non-CLI module in every package.

Package graph (arrows = "depends on"); nothing may point the other way:

    tapewyrm-cli  ──► tapewyrm-archive ◄── qiclib ◄── qicsilver
          └──────────────────────────────► qiclib   (tw convert only)
    cinnabar ──► tapewyrm-cli (the flux codec only), qiclib, tapewyrm-archive

- **cinnabar** is the viewer, a leaf: a desktop app (PySide6 through
  pyqtgraph, numpy) that only *reads*, through the libraries above. Nothing
  depends on it, so `tw` and `qicsilver` never import Qt. Its own split is
  the same one in miniature: `cinnabar.model` (plain data, no Qt),
  `cinnabar.load` and `cinnabar.annotate` (readers and decoders, no Qt),
  `cinnabar.decode` (worker-process decoding, no Qt), `cinnabar.ui` (Qt,
  knows nothing about QIC). `cinnabar --dump` exercises everything but
  the window; the window is tested offscreen (`QT_QPA_PLATFORM=offscreen`).

- **tapewyrm-archive** and **qiclib** are libraries and are **stdlib-only**:
  no rich, click, rich-click or any other presentation or CLI library, and no
  hardware code. They log through `logging` and report through
  `tapewyrm_archive.progress`, and nothing else. The one third-party
  exception is `backports.zstd` in tapewyrm-archive, and only below Python
  3.14: it is the official backport of the stdlib `compression.zstd` (PEP
  784) that TWTZ images use. Import zstd only via `tapewyrm_archive._zstd`.
- The CLIs (`tapewyrm-cli`, `qicsilver`, `cinnabar`) own all terminal
  presentation. Each has its own `console.py` (rich console, `RichHandler`,
  progress bars); the three copies are kept identical by hand -- change one,
  change the others.
- Sibling dependencies use editable path sources:
  `[tool.uv.sources] tapewyrm-archive = { path = "../tapewyrm-archive", editable = true }`.
- On-disk constants shared with the firmware (TWRF marker opcodes) are copied
  into the archive package, and a test in tapewyrm-cli asserts the copies match.
- User-facing sizes are decimal SI in every CLI (1 kB = 1000 B, MB = 10^6 B,
  GB = 10^9 B), matching cartridge covers; never divide by 1024 for output.

### 2.1 Tooling

- Python **3.11+** (`target-version = "py311"`), managed with **uv**.
- **ruff** for lint *and* formatting (Black-compatible), line length **100**,
  rules `E F I UP B W` (E501 ignored for spec tables/comments).
- **mypy** over each package's module (untyped defs allowed, but new code is
  typed).
- **pytest**, tests in each package's `tests/`, no hardware required.
- `just host` runs all of the above. Run it before calling work done.

`tapewyrm/link/protocol.py` is generated by `protocol/generate.py` -- never
edit it by hand; edit `protocol/protocol.toml` and run `just gen`.

### 2.2 Module shape

- Every module opens with a docstring saying what it is, where it sits in the
  pipeline, and which DESIGN.md sections it implements.
- `from __future__ import annotations` comes right after the docstring.
- Imports are ruff/isort ordered. Heavy or hardware imports inside CLI commands
  are done **lazily inside the command function** so `tw --help` stays fast.
- `tw` commands live one module per command group under `tapewyrm/cli/`
  (`drive.py`, `dump.py`, `convert.py`, `info.py`, `inspect.py`,
  `firmware.py`); the root
  `cli` group and `AppContext` are in `app.py`, shared drive plumbing in
  `session.py`. A new command group is a new module, imported by
  `tapewyrm/cli/__init__.py` so it registers.
- Larger modules are split into sections with banner comments:

  ```python
  # ---------------------------------------------------------------------------
  # Format parameter record + BSM (header segment)
  # ---------------------------------------------------------------------------
  ```

### 2.3 Naming

- Descriptive names over one- or two-letter names, except for tight,
  conventional loops and well-known domain abbreviations.
- Domain abbreviations from the QIC specs are used as-is and are fine:
  `seg`, `lsn`, `bsm`, `vtbl`, `tpt`/`tps` (tape track / segment),
  `fsd`/`ftk`/`fsc` (floppy side / track / sector), `spt`.
- Module aliases avoid shadowing: `from qiclib import segment as seg_mod`,
  `volume as volume_mod`.
- Private helpers are `_leading_underscore`.

### 2.4 Data

- Plain data is a `@dataclass` (use `frozen=True` when it is a value).
  Optional spec fields that may be undefined are `int | None`, not sentinel 0.
- Keep the raw bytes alongside parsed fields when a later decoder may need
  them (`VtblEntry.raw`).
- Parsers come in pairs: `parse_x(seg: Segment)` wrapping
  `parse_x_data(data: bytes)`, so tests and images can feed bytes directly.

### 2.5 Errors and output

- Library code raises `ValueError` (or a module-specific exception) with a
  message that says *what* failed and *what to do*; it does not print.
- Library modules log through the standard library: one
  `log = logging.getLogger(__name__)` per module, `log.info(...)` for the
  narrative a user follows ("track 3: capturing -> ..."), `log.debug(...)` for
  bench detail, `log.warning(...)` for things that need a human. No `print`,
  no `log=` callback parameters.
- **Log before you act.** Each significant action (opening a device or file,
  issuing a drive or link command, starting a decode, writing output, a retry)
  gets a line saying what is *about* to happen, so a hang or crash shows what
  was in flight. A result line after it is welcome too.
- **Every guard logs at debug.** An `if` that raises, returns early,
  `continue`s, `break`s or falls back to a default gets a `log.debug` naming
  what was checked, the values seen, and what happens next.
- Debug calls use lazy `%`-style args (`log.debug("seg %d: %s", n, why)`),
  never f-strings, so disabled debug stays cheap.
- Hot paths (per flux transition, bit, byte, USB packet) never log per
  iteration on the success path. Frequent guards in them bump a counter and
  log one summary after the loop.
- Long loops take `progress: Progress = NULL_PROGRESS` (`tapewyrm_archive.progress`)
  and wrap themselves in `with progress.task(desc, total=..., unit=...) as bar:`,
  calling `bar.advance()` / `bar.update()`. Advance at the *top* of a loop body
  that uses `continue`. `total=None` when the length is unknown (a capture).
- A loop that runs in a worker process reports through a
  `tapewyrm_archive.progress.QueueProgress` (its tasks become messages on a
  `multiprocessing` queue) and the parent replays them onto its real
  `Progress` with a `ProgressRelay`; the worker's log records cross the same
  queue through a stdlib `QueueHandler`. `tapewyrm.image.convert.decode_captures`
  is the pattern. A worker never sees `rich` either.
- Progress reports are also where long work can be cancelled. Wrap a
  `Progress` in `cinnabar.ui.progress.Cancellable` (a `threading.Event`);
  once it is cancelled, the next `task`/`advance`/`update` raises `Cancelled`,
  a `BaseException` like `asyncio.CancelledError`. So a long loop that reports
  progress is cancellable for free, and must not swallow `BaseException`. Code
  that takes hold of a resource partway through (shared memory, a decoder)
  releases it and re-raises when the rest of the load fails or is cancelled
  (`FluxTrack.__init__`). Something that only needs freeing at garbage
  collection or exit uses `weakref.finalize`, never `atexit.register(obj.method)`,
  which keeps the object alive until exit.
- Library code never imports `rich` or `click`. Presentation lives in each
  CLI's `console.py` (rich log handler, progress bars) and its commands
  (`tapewyrm/cli/`, `qicsilver/cli.py`).
- The CLI (`rich_click`, imported as `click`) converts `ValueError` into
  `click.ClickException` and owns all user-facing output.
- Output channels: command *results* (status lines, summaries, `--json`) go to
  stdout via `click.echo`; logs and `--progress` bars go to stderr through the
  one shared console on `AppContext.console`. Never print to stderr any other
  way while bars may be live -- it tears the display.
- Global flags on `tw`: `--progress`, `-v`/`-q` (log level around INFO).
  Subcommands must not reuse `-v`/`-q`.

### 2.6 Tests

- One `tests/test_<module>.py` per module.
- Synthetic tape structures come from `qiclib.testing.builders`; extend the
  builders rather than hand-rolling bytes in a test.
- Each test has a docstring or a name that says what behaviour it pins down.
- A Qt test (cinnabar) runs offscreen and never leaves a *shown* widget
  behind: close it, `deleteLater()` it and flush the deferred deletes. Hidden
  leftovers are swept by the session-scoped `qt_teardown` fixture in
  `packages/cinnabar/tests/conftest.py`; without that sweep PySide6 segfaults
  at interpreter exit about one run in four, with every test green.

### 2.7 Profiles, parsers and verdicts

Anything that varies by *who made the tape* is data, not code: drive quirks
(`tapewyrm/profiles/drive`), cartridges (`qiclib/profiles/cartridge`) and the
backup program's layout (`qiclib/volume/profiles`). A volume profile names
three layers -- the VTBL field offsets, the extent widths and the file-set
parser -- and all three live in the `qiclib.volume` package: `table` (the
VTBL records), `profile` (the TOML loader, gate and scoring), `qic122` (the
extents), `fileset` (the parsers, with the `qic113` / `qic113ext` decoders
behind two of them) and `verdict` (the shared vocabulary). The header
segment, which describes the tape and no volume, is `qiclib.header`. Nothing
is re-exported from `qiclib.volume` itself; import the submodule you mean.
The rules for writing a profile are:

- **A `[match]` item is something the writer wrote**, never a flag it may or
  may not have set: `113` at byte 58, `"CMS"` at 62, `"MTN"` at 58. The
  items are a gate, not a hint; a profile that fails one is out for that
  tape. (The CMS profile once matched on the vendor-specific bit, which the
  same software sets on QIC-80 and not on QIC-3010; see
  `volume/profiles/cms-qic113.toml`.)
- **Say how exercised it is**: `status = "spec" | "bench" | "untested"`,
  and `tapes = [...]` for a bench profile. An untested profile warns; so does
  one confirmed on a single tape.
- **A parser returns a verdict, not just entries.** It subclasses
  `qiclib.volume.fileset.FileSetParser`, implements `read` and adds the checks only
  it can make (the data-entry signatures are where the directory says);
  the standard checks (`qiclib.volume.fileset.common`) come free. A parse that
  raises is a fatal probe, never a crash.
- **Warnings are a closed enum** (`qiclib.volume.verdict.VolumeWarning`). A new
  caveat is a new member with a one-line meaning, not a free string; it
  travels in `identify --json`, the TWVL header (TWS-3 sections 3.4, 3.5),
  `inspect` and the tar's pax global header, and every consumer compares
  against the member.
- **Nothing downstream re-decides.** `qicsilver inspect` and `tar` read the
  parser the TWVL header names; they probe every parser only when the header
  names none.

---

## 3. Firmware (C, `firmware/`)

The firmware is a graft onto Greaseweazle. Code inside GW-derived files follows
**GW's existing style**; new Tapewyrm code follows **BSD Kernel Normal Form**
(`style(9)`), which GW's style is already close to:

- 4-space indents in GW-derived files (match the file you are in).
- `/* ... */` block comments; large banner comments for sections.
- File header comment: path, one-line purpose, licence line, then a long
  "WHAT THIS IS" explanation for anything non-trivial.
- Shared constants come from the generated `inc/protocol.h`; never hard-code a
  value that exists in `protocol/protocol.toml`.

---

## 4. Documentation (`docs/`, `agents/`)

### 4.1 Where things go

| Path | Audience | Published |
|---|---|---|
| `README.md` | Someone who just cloned the repo: clone, install, flash, `tw drive status`, then links. Keep it short. | No |
| `docs/guide/` | Task walkthroughs: install, flash, wire, read a tape, troubleshoot. | Yes |
| `docs/tw/`, `docs/qicsilver/`, `docs/cinnabar/` | Reference for each tool: one page per command or feature. | Yes |
| `docs/hardware/` | The Greaseweazle, the drives we have tested and their quirks, cartridges. | Yes |
| `docs/formats/` | The QIC standards, summarised as far as Tapewyrm uses them, plus the vendor structures (volume tables, directory layouts). | Yes |
| `docs/dev/` | Working on the code: one page per package, the firmware, the protocol. | Yes |
| `docs/spec/` | The normative TWS file-format specifications (TWRF, TWTI, TWVL). | Yes |
| `agents/` | The design record (`DESIGN.md`) and notes for coding assistants. | No |
| `packages/*/README.md`, `firmware/README.md` | Stubs: one paragraph, how to run the tests, links into `docs/dev/`. | No |

One fact lives in one place. Link to it; don't copy it.

The site is MkDocs with the `mkdocs-shadcn` theme. `mkdocs.yml` at the repo
root holds the `nav`, and every page must appear in it. `just docs` serves the
site locally and `just docs-build` builds it with `--strict`, which fails on
broken links.

### 4.2 Voice

Write like a vendor reference manual (MSDN, the AWS docs): plain, exact,
second person, present tense.

- Lead with what the page is for, in one or two sentences. No preamble.
- Talk to the reader: "Run `tw dump`", not "The user should run `tw dump`".
- One idea per paragraph. Short sentences. Active voice.
- Say what happens, then why it matters, then what to do about it.
- No hype, no jokes, no "simply", "just" or "easy".
- Put uncertainty where it belongs: "Untested." "Observed on one drive."
  Don't guess, and don't hedge things you know.
- Sentence-case headings. Use procedure headings that start with a verb
  ("Dump a tape") and reference headings that name the thing ("Drive profiles").

### 4.3 Page shapes

**Command reference** (`docs/tw/dump.md` and similar):

1. One-line summary.
2. `## Synopsis`: a fenced `text` block with the usage line.
3. `## Description`: what it does and when to use it.
4. `## Options`: a table (`Option | Default | Description`).
5. `## Examples`: fenced `sh` blocks, each with one sentence saying what it does.
6. `## Exit status` / `## Output`, when it matters.
7. `## See also`.

**Procedure** (`docs/guide/*`): prerequisites, then numbered steps. Each step
has one action, the command, and what you should see. End with "Next steps".

**Format** (`docs/formats/*`): purpose, then layout tables
(`Offset | Size | Field | Description`, offsets in decimal with hex in
parentheses where the standard uses hex), then Tapewyrm notes: which module
implements it, and what we do differently from the standard or have only seen
on the bench. Cite the standard and its section (`QIC-80-MC Rev N §7.1`) and
link the PDF in `docs/qic-standards/`.

**Hardware** (`docs/hardware/drives/*`): identification (vendor/model IDs as
reported), the drive profile to use, supported formats and rates, then a
`## Quirks` list. Each quirk says what you observe, what causes it if we know,
and what to do.

### 4.4 Markdown

- Code, commands, flags, file names and field names go in backticks.
- Fenced blocks always name a language (`sh`, `text`, `toml`, `python`, `c`).
- Use admonitions (`!!! note`, `!!! warning`, `!!! danger`) for things the
  reader must not miss. A warning is something that can lose data or damage
  hardware. Don't stack more than two on one screen.
- Links are relative (`../tw/dump.md`), never absolute site URLs.
- Never put recovered file contents or directory listings from real tapes in
  the docs. Header and volume-table metadata are fine.
- New Markdown files use CRLF line endings like everything else (§1.1).

---

## 5. Commits

Format:

```
<type>: <action summary>

<longer action summary>
```

`<type>` is one word (`fix`, `feature`, `docs`, `firmware(usb)`, `ui`, ...).
Never commit without the maintainer's explicit approval.
