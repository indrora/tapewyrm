# Tapewyrm task runner (DESIGN.md §12.6). `just --list` to see recipes.
# Python helper scripts carry PEP 723 inline metadata and are run with `uv run`,
# so their dependencies (e.g. crcmod for .upd) are fetched automatically.

set windows-shell := ["cmd.exe", "/c"]

_default:
    @just --list

# regenerate protocol.h + protocol.py from the single source of truth
gen:
    uv run protocol/generate.py

# verify the generated protocol artifacts are in sync (CI uses this)
gen-check:
    uv run protocol/generate.py --check

# build the WHOLE project as one package (host wheel + at32f4 firmware) -> dist/
package:
    uv run tools/package.py

# build just the firmware images via PlatformIO -> dist/ (pure-Python HEX merge)
fw mcus="at32f4":
    uv run tools/package.py --skip-host --mcus {{mcus}}

# full firmware release: every PIO-wired MCU (at32f4) + a combined .upd -> dist/ (no host wheel)
fw-dist:
    uv run tools/package.py --dist --skip-host

# convenience flash via the GW-compatible application bootloader (tw owns this, not gw)
update image="firmware/.pio/build/tapewyrm/firmware.bin":
    cd packages/tapewyrm-cli && uv run tw update ../../{{image}}

# recovery flash via the hardware DFU header + AT32 ROM bootloader (tw -> dfu-util)
dfu bin="firmware/.pio/build/tapewyrm/firmware.bin":
    cd packages/tapewyrm-cli && uv run tw dfu ../../{{bin}}

# one Python package: sync, lint, typecheck, test (no hardware). STYLE.md §2.
check pkg module:
    uv --project packages/{{pkg}} sync --extra dev
    uv --project packages/{{pkg}} run ruff check .
    uv --project packages/{{pkg}} run ruff format --check .
    uv --project packages/{{pkg}} run mypy {{module}}
    uv --project packages/{{pkg}} run pytest

# every Python package, base first (the same set CI's packages.yml runs)
host: (check "tapewyrm-archive" "tapewyrm_archive") (check "qiclib" "qiclib") (check "tapewyrm-cli" "tapewyrm") (check "qicsilver" "qicsilver") (check "cinnabar" "cinnabar")

# just the tests of every package (fast loop)
test:
    uv --project packages/tapewyrm-archive run pytest -q
    uv --project packages/qiclib run pytest -q
    uv --project packages/tapewyrm-cli run pytest -q
    uv --project packages/qicsilver run pytest -q
    uv --project packages/cinnabar run pytest -q

# lint + typecheck every package without tests
lint:
    uv --project packages/tapewyrm-archive run ruff check . && uv --project packages/tapewyrm-archive run mypy tapewyrm_archive
    uv --project packages/qiclib run ruff check . && uv --project packages/qiclib run mypy qiclib
    uv --project packages/tapewyrm-cli run ruff check . && uv --project packages/tapewyrm-cli run mypy tapewyrm
    uv --project packages/qicsilver run ruff check . && uv --project packages/qicsilver run mypy qicsilver
    uv --project packages/cinnabar run ruff check . && uv --project packages/cinnabar run mypy cinnabar

# look at a capture or image in the viewer (cinnabar PATH [--track N] [--theme dark])
cinnabar *args:
    uv --project packages/cinnabar run cinnabar {{args}}

# remove build, package, and cache artifacts (keeps the uv venv)
clean:
    uv run tools/clean.py

# everything CI runs (protocol drift check last)
ci: gen host
    git diff --exit-code

# run qicsilver (TWTI image -> identify / extract / inspect / tar), e.g. `just qicsilver identify captures/jc.twti`
qicsilver *args:
    uv run --project packages/qicsilver qicsilver {{args}}
tapewyrm *args:
    uv run --project packages/tapewyrm-cli tw {{args}}

# docs site (MkDocs + mkdocs-shadcn, pinned; STYLE.md section 4). Live preview on :8000
docs:
    uvx --from mkdocs==1.6.1 --with mkdocs-shadcn==0.12.2 --with pygments mkdocs serve

# build the docs site into site/, failing on broken links or pages missing from nav
docs-build:
    uvx --from mkdocs==1.6.1 --with mkdocs-shadcn==0.12.2 --with pygments mkdocs build --strict
