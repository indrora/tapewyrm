# Tapewyrm task runner (DESIGN.md §12.6). `just --list` to see recipes.
# Python helper scripts carry PEP 723 inline metadata and are run with `uv run`,
# so their dependencies (e.g. crcmod for .upd) are fetched automatically.

set windows-shell := ["cmd.exe", "/c"]

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
flash image="firmware/.pio/build/tapewyrm/firmware.bin":
    cd packages/tapewyrm-cli && uv run tw flash ../../{{image}}

# recovery flash via the hardware DFU header + AT32 ROM bootloader (tw -> dfu-util)
dfu bin="firmware/.pio/build/tapewyrm/firmware.bin":
    cd packages/tapewyrm-cli && uv run tw dfu ../../{{bin}}

# one Python package: sync, lint, typecheck, test (no hardware). STYLE.md §2.
check pkg module:
    cd packages/{{pkg}} && uv sync --extra dev
    cd packages/{{pkg}} && uv run ruff check .
    cd packages/{{pkg}} && uv run ruff format --check .
    cd packages/{{pkg}} && uv run mypy {{module}}
    cd packages/{{pkg}} && uv run pytest

# every Python package, base first (the same set CI's packages.yml runs)
host: (check "tapewyrm-archive" "tapewyrm_archive") (check "qiclib" "qiclib") (check "tapewyrm-cli" "tapewyrm") (check "qicsilver" "qicsilver")

# just the tests of every package (fast loop)
test:
    cd packages/tapewyrm-archive && uv run pytest -q
    cd packages/qiclib && uv run pytest -q
    cd packages/tapewyrm-cli && uv run pytest -q
    cd packages/qicsilver && uv run pytest -q

# lint + typecheck every package without tests
lint:
    cd packages/tapewyrm-archive && uv run ruff check . && uv run mypy tapewyrm_archive
    cd packages/qiclib && uv run ruff check . && uv run mypy qiclib
    cd packages/tapewyrm-cli && uv run ruff check . && uv run mypy tapewyrm
    cd packages/qicsilver && uv run ruff check . && uv run mypy qicsilver

# remove build, package, and cache artifacts (keeps the uv venv)
clean:
    uv run tools/clean.py

# everything CI runs (protocol drift check last)
ci: gen host
    git diff --exit-code

# run qicsilver (TWTI image -> identify / extract / tar), e.g. `just qicsilver identify captures/jc.twti`
qicsilver *args:
    uv run --project packages/qicsilver qicsilver {{args}}
