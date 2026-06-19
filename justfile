# Tapewyrm task runner (DESIGN.md §12.6). `just --list` to see recipes.

set windows-shell := ["cmd.exe", "/c"]

# regenerate protocol.h + protocol.py from the single source of truth
gen:
    python protocol/generate.py

# verify the generated protocol artifacts are in sync (CI uses this)
gen-check:
    python protocol/generate.py --check

# build the WHOLE project as one package (host wheel + firmware images) -> dist/
package:
    python tools/package.py

# build just the firmware images -> dist/ (portable: pure-Python HEX merge, no srecord/crcmod)
fw mcus="at32f4":
    python tools/package.py --skip-host --mcus {{mcus}}

# full Greaseweazle-style firmware release (all MCUs + .upd files); needs srecord + crcmod (CI/Linux)
fw-dist:
    make -C firmware dist

# convenience flash via the GW-compatible application bootloader (tw owns this, not gw)
flash image="firmware/out/at32f4/prod/tapewyrm/target.bin":
    cd host && uv run tw flash ../{{image}}

# recovery flash via the hardware DFU header + AT32 ROM bootloader (tw -> dfu-util)
dfu bin="firmware/out/at32f4/prod/tapewyrm/target.bin":
    cd host && uv run tw dfu ../{{bin}}

# host: sync, lint, typecheck, test (no hardware)
host:
    cd host && uv sync --extra dev
    cd host && uv run ruff check .
    cd host && uv run ruff format --check .
    cd host && uv run mypy tapewyrm
    cd host && uv run pytest

# just the host tests (fast loop)
test:
    cd host && uv run pytest

# lint + format + typecheck without tests
lint:
    cd host && uv run ruff check .
    cd host && uv run mypy tapewyrm

# everything CI runs (protocol drift check last)
ci: gen host
    git diff --exit-code
