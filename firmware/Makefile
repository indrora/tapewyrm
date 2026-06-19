# ===========================================================================
# Tapewyrm firmware — SKELETON Makefile
# ---------------------------------------------------------------------------
# THIS IS NOT THE REAL BUILD. The real firmware is built by MERGING these QIC
# sources into Greaseweazle's (GW) own Make tree and building the `at32f4`
# target there (DESIGN.md §12.1, §12.5). That tree provides the linker script,
# startup code, AT32F403 HAL, USB/CDC stack, flux timer/DMA, and bootloader —
# none of which are vendored in this repo yet. So this Makefile DELIBERATELY
# does not try to produce a .hex / .bin; doing so would be a lie.
#
# WHAT THE REAL BUILD LOOKS LIKE (TODO(bench) §12.1, §13.6 item 2):
#   - Drop firmware/qic/*.{c,h} and firmware/protocol.h into the vendored GW
#     tree, ABOVE the application entry point (the boot/update layer stays
#     pristine — §12.5).
#   - Add the qic/*.c objects to GW's object list for the at32f4 target.
#   - Hook txn_dispatch() into GW's USB command handler and txn_capture_service()
#     into GW's main loop; bind the gw_* stubs in qic/gw_stubs.h to the real GW
#     primitives (delete the stubs or guard with -DTW_USE_REAL_GW).
#   - Build with GW's pinned `gcc-arm-none-eabi` (§12.1: the toolchain version
#     affects instruction timing, and this firmware is timing-sensitive):
#         make -C <gw-tree> out/at32f4/prod/greaseweazle/target.hex     # release
#         make -C <gw-tree> out/at32f4/debug/greaseweazle/target.hex    # bring-up
#     `debug` enables 3 Mbaud serial logging — use it while debugging the
#     report-bit loop and the arbiter lease machine (§12.1).
#
# WHAT THIS SKELETON MAKEFILE ACTUALLY DOES:
#   `make syntax-check`  — run a C compiler with -fsyntax-only over qic/*.c so
#                          the skeleton is verified to be syntactically valid in
#                          isolation (the gw_stubs.h externs make it self-
#                          contained). Prefers arm-none-eabi-gcc; falls back to a
#                          host compiler (gcc/cc/clang) if no ARM toolchain.
#   `make cross-check`   — REAL cross-compile of qic/*.c to Cortex-M4 (armv7e-m)
#                          objects under out/ with -Werror. Requires
#                          arm-none-eabi-gcc (UCRT64 MSYS ships 13.4.0). This is
#                          the strongest check short of the full GW-tree link:
#                          it exercises actual ARM Thumb codegen, not just parsing.
#   `make help`          — print this summary.
# ===========================================================================

# --- toolchain selection: ARM first, then host fallback --------------------
ARM_CC := $(shell command -v arm-none-eabi-gcc 2>/dev/null)
ifeq ($(ARM_CC),)
  HOST_CC := $(shell command -v gcc 2>/dev/null)
  ifeq ($(HOST_CC),)
    HOST_CC := $(shell command -v cc 2>/dev/null)
  endif
  ifeq ($(HOST_CC),)
    HOST_CC := $(shell command -v clang 2>/dev/null)
  endif
  CHECK_CC := $(HOST_CC)
  CC_KIND  := host ($(CHECK_CC))
else
  CHECK_CC := $(ARM_CC)
  CC_KIND  := arm-none-eabi ($(CHECK_CC))
endif

# Include paths: protocol.h lives in firmware/, the QIC sources + gw_stubs.h in
# firmware/qic/. From qic/*.c, protocol.h is reached as "../protocol.h".
INCLUDES := -Ifirmware -Ifirmware/qic

# Syntax-check flags. -fsyntax-only does not link, so the gw_* externs being
# unresolved is fine — we are only checking the C is well-formed. Freestanding
# so we don't depend on a hosted libc for the (eventual) target.
SYNTAX_FLAGS := -fsyntax-only -std=c11 -Wall -Wextra -ffreestanding \
                $(INCLUDES)

# Real cross-compile flags. The AT32F403 core is a Cortex-M4 (armv7e-m). We
# compile (not link), so unresolved gw_* externs are fine; -Werror keeps the
# skeleton honest. Soft-float keeps us off the hard-float multilib for the check
# (the real GW build picks the FPU/float-abi; that lives in the GW tree, §12.1).
CROSS_FLAGS := -mcpu=cortex-m4 -mthumb -mfloat-abi=soft
COMPILE_FLAGS := -c -std=c11 -Wall -Wextra -Werror -ffreestanding -Os $(INCLUDES)
OUT := firmware/out

QIC_SRCS := $(wildcard firmware/qic/*.c)
QIC_OBJS := $(patsubst firmware/qic/%.c,$(OUT)/%.o,$(QIC_SRCS))

.PHONY: syntax-check cross-check clean help dist

syntax-check:
ifeq ($(CHECK_CC),)
	@echo "No C compiler found (looked for arm-none-eabi-gcc, gcc, cc, clang)."
	@echo "Install gcc-arm-none-eabi (preferred) or a host C compiler, then re-run."
	@echo "Skipping syntax-check (CI's firmware job will syntax-check)."
	@exit 0
else
	@echo "Syntax-checking qic/*.c with $(CC_KIND)"
	@set -e; for f in $(QIC_SRCS); do \
	  echo "  CHK $$f"; \
	  $(CHECK_CC) $(SYNTAX_FLAGS) "$$f"; \
	done
	@echo "OK: all QIC sources are syntactically valid."
endif

cross-check:
ifeq ($(ARM_CC),)
	@echo "cross-check needs arm-none-eabi-gcc (e.g. UCRT64 MSYS). Not found."
	@echo "Run 'make syntax-check' for a host-compiler parse check instead."
	@exit 1
else
	@echo "Cross-compiling qic/*.c -> Cortex-M4 objects with $(ARM_CC)"
	@mkdir -p $(OUT)
	@set -e; for f in $(QIC_SRCS); do \
	  o="$(OUT)/$$(basename $${f%.c}).o"; \
	  echo "  CC  $$f -> $$o"; \
	  $(ARM_CC) $(CROSS_FLAGS) $(COMPILE_FLAGS) "$$f" -o "$$o"; \
	done
	@echo "OK: all QIC sources cross-compile for Cortex-M4 (armv7e-m)."
endif

clean:
	@rm -rf $(OUT)

dist:
	@echo "This skeleton does NOT produce a .hex/.bin (see the header of this"
	@echo "Makefile). Merge firmware/qic into the vendored GW tree and build the"
	@echo "at32f4 target there. TODO(bench) §12.1."
	@exit 1

help:
	@echo "Tapewyrm firmware skeleton Makefile (NOT the real build):"
	@echo "  make syntax-check   # -fsyntax-only over qic/*.c (ARM or host CC)"
	@echo "  make cross-check    # REAL Cortex-M4 compile to out/*.o (needs ARM CC)"
	@echo "  make clean          # remove out/"
	@echo "  make dist           # intentionally errors: real build lives in GW tree"
	@echo "  make help           # this message"
	@echo ""
	@echo "Compiler that would be used: $(if $(CHECK_CC),$(CC_KIND),NONE FOUND)"
