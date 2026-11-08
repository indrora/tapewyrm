# Codebase overview

This section is for working on Tapewyrm itself: the five Python packages, the
Greaseweazle firmware graft and the device protocol that joins them. To use the
tools, start with the [guide](../guide/index.md) instead.

Before you change any code, read `STYLE.md` at the repository root. It is not
part of this site. It sets the line-ending rule (CRLF), the citation rule for
every non-obvious number, the logging and progress rules, the test layout and
the voice of these pages. Coding assistants are told to read it too.

## Repository layout

| Path | Contents |
|---|---|
| `packages/tapewyrm-cli/` | `tw`: the hardware side. USB link, QIC-117 drive control, `tw dump`, the flux codec and `tw convert`. See [tapewyrm-cli](tapewyrm-cli.md). |
| `packages/tapewyrm-archive/` | The on-disk formats (TWRF, TWTI/TWTZ, TWVL) and the progress protocol every library reports through. See [tapewyrm-archive](tapewyrm-archive.md). |
| `packages/qiclib/` | The QIC formats with no hardware: segments, Reed-Solomon, the header, the volume table, QIC-113, QIC-122, cartridge and volume profiles. See [qiclib](qiclib.md). |
| `packages/qicsilver/` | `qicsilver`: tape image to files (`identify`, `extract`, `inspect`, `tar`). See [qicsilver](qicsilver.md). |
| `packages/cinnabar/` | `cinnabar`: the Qt viewer for captures and images. See [cinnabar](cinnabar.md). |
| `firmware/` | Greaseweazle v1.6 firmware, vendored complete, with the QIC graft in `src/qic/qic.c`. See [Firmware](firmware.md). |
| `protocol/` | `protocol.toml`, the single source of the device protocol codes, and `generate.py`, which writes both ends. See [Device protocol](protocol.md). |
| `tools/` | `package.py` (release bundle), `ihex.py` (pure-Python HEX merge), `clean.py` (removes build output). See [Building and testing](building.md). |
| `docs/` | This site (MkDocs). `docs/spec/` holds the normative TWS file-format specifications; `docs/qic-standards/` holds the standards PDFs. |
| `agents/` | The design record (`DESIGN.md`) and notes for coding assistants. Not published. Parts of `DESIGN.md` are out of date; where it and the code disagree, the code is right. |
| `.github/workflows/` | CI: `packages.yml`, `contract.yml`, `firmware.yml`, `package.yml`, and `docs.yml`, which builds this site and deploys it to GitHub Pages. |
| `justfile` | Task recipes. See [Building and testing](building.md#just-recipes). |
| `mkdocs.yml` | Site configuration and `nav`. Every page must appear in `nav`. |
| `captures/` | Your own tape captures and images. Ignored by git, and so are `*.twrf`, `*.twti` and `*.twtz` anywhere in the tree. |

## Package dependencies

Arrows read "depends on". Nothing points the other way.

```text
tapewyrm-cli  ──► tapewyrm-archive ◄── qiclib ◄── qicsilver
      └──────────────────────────────► qiclib   (tw convert only)
cinnabar ──► tapewyrm-cli (the flux codec only), qiclib, tapewyrm-archive
```

- `tapewyrm-archive` is the base. It is stdlib-only, apart from
  `backports.zstd` below Python 3.14.
- `qiclib` depends only on `tapewyrm-archive` and is stdlib-only otherwise.
- `tapewyrm-cli` uses `qiclib` for its data types and, in `tw convert`, for
  image building. It never imports `qicsilver` or Qt.
- `qicsilver` never depends on the hardware package.
- `cinnabar` is a leaf. It imports `tapewyrm.codec` for the PLL and MFM framer
  and nothing else from `tapewyrm-cli`. Nothing depends on cinnabar.

The two libraries (`tapewyrm-archive`, `qiclib`) never import `rich` or
`click` and never print. The three CLIs (`tw`, `qicsilver`, `cinnabar`) own
all terminal output, each through its own `console.py`. The three
`console.py` files are kept identical by hand.

Siblings are wired together with editable path sources in each
`pyproject.toml`:

```toml
[tool.uv.sources]
tapewyrm-archive = { path = "../tapewyrm-archive", editable = true }
```

An edit to a library is visible to every package that depends on it with no
reinstall.

## Where to start

| You want to | Read first |
|---|---|
| Fix a decode or recovery bug | [qiclib](qiclib.md): `qiclib.place`, `qiclib.segment`, `qiclib.rs`, `qiclib.build`. The physical layer is `tapewyrm.codec` in [tapewyrm-cli](tapewyrm-cli.md). |
| Read a tape no profile fits | [Writing a volume profile](volume-profiles.md). |
| Support a new drive | Drive profiles in [tapewyrm-cli](tapewyrm-cli.md#drive-profiles) and [Drive profiles](../tw/profiles.md). |
| Change a file format | [tapewyrm-archive](tapewyrm-archive.md) and the specification in `docs/spec/`. |
| Add a device verb | [Device protocol](protocol.md), then [Firmware](firmware.md). |
| Add a lane to the viewer | [cinnabar](cinnabar.md): `cinnabar.model`, then `cinnabar.annotate`. |
| Build, test or release | [Building and testing](building.md). |

## Ground rules

- **No backward compatibility yet.** Tapewyrm is pre-release. You can change
  file formats, CLI flags and layouts freely. Update the specification and
  these pages in the same change. Do not add shims, aliases or migration code.
- **No malicious-input threat model.** Inputs are your own captures and images.
  There are no decompression caps. A malformed or truncated file must still be
  rejected with a clear error, never a traceback.
- **Required arguments are positional**, not required flags.
- **Never discard a read.** Every pass a tape gives is kept; choosing between
  passes happens later, in `tw convert`.
- **No tape contents in the repository.** Recovered files and directory
  listings from real tapes never go into code, tests or docs. Header and
  volume-table metadata are fine (the bench fixtures in `qiclib.testing` hold
  only those).
- **Commits** follow `STYLE.md` section 5 and need the maintainer's approval.

## See also

- [Building and testing](building.md)
- [Specifications](../spec/README.md)
