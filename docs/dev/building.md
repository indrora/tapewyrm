# Building and testing

This page covers the Python toolchain, every `just` recipe, CI, the release
bundle in `dist/` and how to run the tools from a checkout.

## Prerequisites

| Tool | Needed for | Notes |
|---|---|---|
| [uv](https://docs.astral.sh/uv/) | Everything | Manages Python (3.11 or later), each package's virtual environment and the PEP 723 helper scripts. |
| [just](https://github.com/casey/just) | The recipes below | Optional. Every recipe is a short shell command you can run by hand. |
| PlatformIO Core (`pio`) | Firmware builds only | Fetches its own pinned ARM GCC. See [Firmware](firmware.md#build-the-firmware). |
| `dfu-util` | `tw dfu` only | First flash and recovery. |

No hardware is needed to build or test anything.

## Python projects

Each package under `packages/` is its own uv project with its own
`pyproject.toml`, `uv.lock` and `.venv`. There is no workspace at the
repository root. Siblings are installed editable through
`[tool.uv.sources]` path entries, so a change in `tapewyrm-archive` is seen
at once by `qiclib`, `tw`, `qicsilver` and `cinnabar`.

| Package directory | Distribution | Import name | Scripts |
|---|---|---|---|
| `packages/tapewyrm-archive` | `tapewyrm-archive` | `tapewyrm_archive` | none |
| `packages/qiclib` | `qiclib` | `qiclib` | none |
| `packages/tapewyrm-cli` | `tapewyrm` | `tapewyrm` | `tw`, `tapewyrm` (same command) |
| `packages/qicsilver` | `qicsilver` | `qicsilver` | `qicsilver` |
| `packages/cinnabar` | `cinnabar` | `cinnabar` | `cinnabar` |

All five build with hatchling. Each package's tooling configuration is the
same:

- **ruff** for lint and formatting, line length 100, target `py311`, rules
  `E F I UP B W`, with `E501` ignored for spec tables.
- **mypy** over the package's module, untyped definitions allowed, with
  `warn_unused_ignores` and `warn_redundant_casts`.
- **pytest** with `testpaths = ["tests"]` and `-ra`. Coverage comes from
  `pytest-cov`.

The development tools are the `dev` extra. To work on one package by hand:

```sh
cd packages/qiclib
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy qiclib
```

!!! note
    `packages/tapewyrm-cli/tapewyrm/link/protocol.py` is excluded from ruff
    (`extend-exclude`). It is generated; its formatting belongs to
    `protocol/generate.py`. See [Device protocol](protocol.md).

## Just recipes

Run `just --list` for the one-line summaries. All recipes run from the
repository root.

### Checks and tests

| Recipe | Runs | Use it to |
|---|---|---|
| `just check PKG MODULE` | In `packages/PKG`: `uv sync --extra dev`, `ruff check .`, `ruff format --check .`, `mypy MODULE`, `pytest`. | Check one package the way CI does, for example `just check qiclib qiclib` or `just check tapewyrm-cli tapewyrm`. |
| `just host` | `check` for every package, base first: `tapewyrm-archive`, `qiclib`, `tapewyrm-cli`, `qicsilver`, `cinnabar`. | Run before you call a change done (`STYLE.md` section 2.1). It is the same set CI's `packages.yml` runs. |
| `just test` | `pytest -q` in every package. No sync, no lint. | The fast loop, once each package has been synced. |
| `just lint` | `ruff check` and `mypy` in every package. No format check, no tests. | Lint without waiting for tests. |
| `just ci` | `gen`, then `host`, then `git diff --exit-code`. | Everything CI checks, with the protocol drift check last. Fails if regeneration changed any tracked file, so commit or stash unrelated edits first. |

### Protocol

| Recipe | Runs | Use it to |
|---|---|---|
| `just gen` | `uv run protocol/generate.py` | Regenerate `firmware/inc/protocol.h` and `tapewyrm/link/protocol.py` after you edit `protocol/protocol.toml`. |
| `just gen-check` | `uv run protocol/generate.py --check` | Exit 1, naming the stale files, if either generated file is out of date. |

### Firmware and packaging

| Recipe | Runs | Use it to |
|---|---|---|
| `just fw [mcus="at32f4"]` | `uv run tools/package.py --skip-host --mcus MCUS` | Build the bootloader and application with PlatformIO and merge them into one HEX. Output goes to `dist/`. |
| `just fw-dist` | `uv run tools/package.py --dist --skip-host` | Build every MCU with a PlatformIO env (only `at32f4` today) plus a combined `.upd` update file. |
| `just package` | `uv run tools/package.py` | Build the host wheels and sdists and the `at32f4` firmware into one zip. |
| `just clean` | `uv run tools/clean.py` | Remove `firmware/out/`, `dist/`, `packages/tapewyrm-cli/dist/` and `htmlcov/`, and the ruff, mypy and pytest caches at the repository root and in `packages/tapewyrm-cli/`. It keeps every `.venv`. |

`just clean` does not remove `firmware/.pio/` or the caches in the other four
packages. Delete those by hand if you need to.

### Flashing

| Recipe | Runs | Use it to |
|---|---|---|
| `just update [image=...]` | `tw update IMAGE` from `packages/tapewyrm-cli` | Update the board through the Greaseweazle-compatible application bootloader. The default image is `firmware/.pio/build/tapewyrm/firmware.bin`. |
| `just dfu [bin=...]` | `tw dfu BIN` from `packages/tapewyrm-cli` | First flash or recovery through the AT32 ROM bootloader with `dfu-util`. Same default image. |

See [tw update and tw dfu](../tw/update.md) for what each path does to the
board.

### Running the tools

| Recipe | Runs |
|---|---|
| `just tapewyrm ARGS...` | `uv run --project packages/tapewyrm-cli tw ARGS...` |
| `just qicsilver ARGS...` | `uv run --project packages/qicsilver qicsilver ARGS...` |
| `just cinnabar ARGS...` | `uv run cinnabar ARGS...` from `packages/cinnabar` |

`just cinnabar` changes into `packages/cinnabar` first, so a relative path
you pass it is resolved from there, not from the repository root. Pass an
absolute path or prefix `../../`.

### Documentation

| Recipe | Runs | Use it to |
|---|---|---|
| `just docs` | `mkdocs serve` with `mkdocs==1.6.1`, `mkdocs-shadcn==0.12.2` and `pygments`, through `uvx` | Preview the site on `http://127.0.0.1:8000` with live reload. |
| `just docs-build` | `mkdocs build --strict` with the same pins | Build the site into `site/`. Fails on a broken link, a missing anchor or a page that is not in `nav`. |

`uvx` fetches the pinned MkDocs into a throwaway environment. Nothing is added
to any package. Every new page must be added to `nav` in `mkdocs.yml`.

## Run from the tree

You do not need to install anything to run the tools. Each of these uses the
package's own environment, synced on first use:

```sh
uv run --project packages/tapewyrm-cli tw --help
uv run --project packages/qicsilver qicsilver identify captures/NAME.twtz
uv run --project packages/cinnabar cinnabar captures/NAME.twtz
```

Inside a package directory, `uv run tw ...` (or `qicsilver`, `cinnabar`)
does the same. `python -m tapewyrm.cli` is the same entry point as `tw`.

An editable install reports its source through git: `tw info` asks git
for the commit of the checkout. A built wheel instead carries a stamp,
`tapewyrm/_build_stamp.py`, which `packages/tapewyrm-cli/hatch_build.py`
writes into the wheel only. The stamp step is skipped for editable installs,
so the source tree never holds a generated file.

## Package a release

`tools/package.py` is a PEP 723 script. `uv run` fetches its one dependency,
`crcmod`, for the `.upd` CRCs. It needs `uv`, PlatformIO Core and Python, and
nothing else: no `make`, `srecord` or `zip`.

| Option | Effect |
|---|---|
| (none) | Host wheels and sdists, plus the `at32f4` firmware. |
| `--dist` | Every MCU in `DIST_MCUS` (`at32f4`) plus a combined `.upd`. |
| `--mcus MCU...` | Build these MCU targets instead. |
| `--skip-host` | No wheels. |
| `--skip-firmware` | No firmware. |

The script stages a directory and zips it:

```text
dist/
  tapewyrm-<version>/
    firmware/
      tapewyrm-at32f4-<fw>.hex     bootloader + application, merged
      tapewyrm-at32f4-<fw>.bin     application only
      tapewyrm-<fw>.upd            with --dist only
    host/
      *.whl, *.tar.gz              tapewyrm-archive, qiclib, tapewyrm, qicsilver
    README.md
    UNLICENSE
  tapewyrm-<version>.zip
```

- `<version>` is the `version` in `packages/tapewyrm-cli/pyproject.toml`.
- `<fw>` is `custom_fw_major.custom_fw_minor` from `firmware/platformio.ini`
  (currently `1.6`).
- The host wheels are built with `uv build` into `packages/tapewyrm-cli/dist/`
  and copied. `tapewyrm-archive` and `qiclib` are not on PyPI, so all four
  ship together and `pip install host/*.whl` resolves locally.
- **cinnabar is not packaged.** Run it from the tree.

## Continuous integration

GitHub Actions, in `.github/workflows/`:

| Workflow | Triggers | Does |
|---|---|---|
| `packages.yml` | Push or PR touching `packages/**` | A matrix over all five packages, each in its own directory: `uv sync --extra dev`, `ruff check .`, `ruff format --check .`, `mypy MODULE`, `pytest --cov=MODULE --cov-report=term-missing`. Sets `QT_QPA_PLATFORM=offscreen` for cinnabar. `fail-fast` is off. |
| `contract.yml` | Push or PR touching `protocol/**` or either generated file | `python protocol/generate.py --check` on Python 3.12. Fails on protocol drift. |
| `firmware.yml` | Push or PR touching `firmware/**` or `tools/**` | Installs PlatformIO with pipx, caches `~/.platformio` keyed on `platformio.ini`, runs `uv run tools/package.py --skip-host --mcus at32f4` (the same as `just fw`) and uploads `dist/**/firmware/*`. |
| `package.yml` | A `v*` tag, or a manual run | `uv run tools/package.py --dist` and uploads `dist/tapewyrm-*.zip`. |

Because the packages depend on each other through path sources, a change to
any one of them re-runs the whole matrix.

## Line endings

Every tracked text file is CRLF, except `uv.lock`, which `.gitattributes`
pins to LF because uv rewrites it. `protocol/generate.py` writes CRLF itself.
If your editor or a tool writes LF, convert before you commit:

```sh
perl -pi -e 's/(?<!\r)\n/\r\n/' path/to/file
```

Check `git diff --stat` for an implausibly large diff before committing; it
usually means a file lost its CRLF.

## See also

- [Firmware](firmware.md)
- [Device protocol](protocol.md)
- [Install the tools](../guide/install.md)
