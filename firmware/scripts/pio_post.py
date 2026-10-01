# PlatformIO post-script: replace the ststm32 platform's bare-metal defaults
# with the exact flag set the old Rules.mk used, and generate the linker script.
#
# ststm32's frameworks/_bare.py (run when no framework is set) appends things
# that are wrong for this firmware:
#   * LIBS = c, gcc, m, stdc++  -- we are -nostdlib and ship our own string.c
#     / util.c; dragging in newlib risks duplicate or silently-substituted
#     memcpy/memset and bloats the 48K image.
#   * -fdata-sections, --relax, F_CPU -- harmless-ish, but not what the
#     firmware was validated with. Parity with the Make build matters more.
#   * PlatformIO also adds -I<src> -I<inc> and a PLATFORMIO=xxxxx define.
# So we *Replace* rather than Append. This is safe in a post: script because
# SCons expands $CCFLAGS & co. lazily, when each command runs; the object and
# program nodes were created by BuildProgram() but have not been built yet.
# (Per-file additions live in pio_pre.py via the $TW_FILE_CFLAGS token.)
#
# Two environments matter: `env` links the program, while project sources are
# compiled with `projenv`, a Clone PlatformIO makes inside BuildProgram(). A
# clone does not see later changes to `env`, so the compile flags must be
# replaced on BOTH (only `env` needs the link flags).

from os.path import join

Import("env", "projenv")  # noqa: F821

is_bootloader = env.GetProjectOption("custom_bootloader", "no") == "yes"
project_dir = env.subst("$PROJECT_DIR")
inc_dir = join(project_dir, "inc")

# Rules.mk FLAGS, verbatim, in the same order. Shared by C, .S and the link.
flags = [
    "-g", "-Os", "-nostdlib", "-std=gnu99", "-iquote", inc_dir,
    "-Wall", "-Werror", "-Wno-format", "-Wdeclaration-after-statement",
    "-Wstrict-prototypes", "-Wredundant-decls", "-Wnested-externs",
    "-fno-common", "-fno-exceptions", "-fno-strict-aliasing",
    "-mlittle-endian", "-mthumb", "-mfloat-abi=soft",
    "-Wno-unused-value", "-ffunction-sections",
    "-mcpu=cortex-m4",
]  # fmt: skip

defines = [("AT32F4", 4), ("MCU", 4), "NDEBUG"]
if is_bootloader:
    defines.append(("BOOTLOADER", 1))

compile_flags = dict(
    CPPPATH=[],
    CPPDEFINES=defines,
    LIBS=[],
    LIBPATH=[],
    CCFLAGS=list(flags),
    # C only: forced decls.h include + the per-file hook from pio_pre.py.
    CFLAGS=["-include", "decls.h", "$TW_FILE_CFLAGS"],
    TW_FILE_CFLAGS=[],
    CXXFLAGS=[],
    # .S files go through `$CC -x assembler-with-cpp`; Rules.mk AFLAGS.
    ASPPFLAGS=["-x", "assembler-with-cpp", *flags, "-D__ASSEMBLY__"],
    ASFLAGS=list(flags),
)
for build_env in (env, projenv):  # noqa: F821
    build_env.Replace(**compile_flags)

# Linker script: src/target.ld.S is C-preprocessed with AFLAGS + the MCU /
# BOOTLOADER defines to pick FLASH_BASE/FLASH_LEN (Rules.mk `%.ld` rule).
ldscript = env.Command(
    join("$BUILD_DIR", "target.ld"),
    join("$PROJECT_SRC_DIR", "target.ld.S"),
    env.VerboseAction(
        "$CC -P -E $ASPPFLAGS $_CPPDEFFLAGS $SOURCE -o $TARGET",
        "Generating linker script $TARGET",
    ),
)

env.Replace(
    LDSCRIPT_PATH="",
    LINKFLAGS=[*flags, "-Wl,--gc-sections", "-T", ldscript[0].get_abspath()],
)
env.Depends(env["PIOMAINPROG"], ldscript)

# Also emit firmware.hex by default. ststm32 registers an ElfToHex builder but
# only builds .bin unless you ask for the `buildhex` target; tools/package.py
# needs both HEXes to merge bootloader + app into one image.
env.Default(env.ElfToHex(join("$BUILD_DIR", "${PROGNAME}"), env["PIOMAINPROG"]))
