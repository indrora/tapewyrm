# PlatformIO pre-script: per-file compiler flags (the old Rules.mk/Makefile
# target-specific variables).
#
# Why this is a *pre* script: PlatformIO consults build middlewares while it
# collects the project's sources, which happens inside env.BuildProgram() in the
# platform's builder/main.py -- i.e. BEFORE any post: script runs. Middlewares
# registered later are silently ignored.
#
# Why the middlewares only set $TW_FILE_CFLAGS and never touch $CFLAGS
# directly: a middleware's overrides are evaluated *now*, at collection time,
# but scripts/pio_post.py replaces $CFLAGS wholesale *afterwards*. If we wrote
# `CFLAGS=env["CFLAGS"] + [...]` here we would freeze the platform's default
# (wrong) flags into those objects. Instead pio_post.py puts the literal
# token "$TW_FILE_CFLAGS" into $CFLAGS, SCons expands it lazily at command
# time, and the per-file override below supplies its value.

Import("env")  # noqa: F821  (SCons injects Import/env)

fw_major = env.GetProjectOption("custom_fw_major")
fw_minor = env.GetProjectOption("custom_fw_minor")
src_dir = env.subst("$PROJECT_SRC_DIR")


def _with_file_cflags(*flags):
    """Middleware factory: compile the matched node with extra C flags."""

    def _middleware(env, node):
        return env.Object(node, TW_FILE_CFLAGS=list(flags))

    return _middleware


# src/usb/Makefile: `$(OBJS) $(OBJS-y): CFLAGS += -include $(SRCDIR)/defs.h`
env.AddBuildMiddleware(
    _with_file_cflags("-include", f"{src_dir}/usb/defs.h"), "*/usb/*.c"
)

# src/Makefile: GCC would otherwise "optimise" the loops inside our own
# memset/memcpy into calls to memset/memcpy -- infinite recursion.
env.AddBuildMiddleware(
    _with_file_cflags("-fno-tree-loop-distribute-patterns"), "*/util.c"
)

# src/Makefile: build_info.c bakes the firmware version into GetInfo.
env.AddBuildMiddleware(
    _with_file_cflags(f"-DFW_MAJOR={fw_major}", f"-DFW_MINOR={fw_minor}"),
    "*/build_info.c",
)
