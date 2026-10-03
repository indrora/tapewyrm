"""Tapewyrm CLI: run context, config loading and the ``tw`` group (DESIGN.md §6A.7).

``AppContext`` carries the resolved ``--port`` / ``--profile`` / ``--config``
settings, the shared stderr console and the ``--progress`` switch on
``ctx.obj``; ``cli`` is the root ``@click.group()`` every command module
registers on (see the layout in ``tapewyrm/cli/__init__.py``).

Config precedence: CLI flags -> config file -> profile defaults, resolved once in
``AppContext.load`` and carried on ``ctx.obj``. The config file is ``--config``
if given, else the per-user file from ``default_config_path()`` if it exists.
The drive profile defaults to ``auto`` (try ftape's wake-up methods in ftape's
order, ``qic117.profile.AUTO_ORDER``) when neither names one.
"""

from __future__ import annotations

import logging
import os
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import rich_click as click
from rich.console import Console
from tapewyrm_archive.progress import Progress

from tapewyrm.console import make_console, progress_display, setup_logging
from tapewyrm.qic117.profile import AUTO, load_profile
from tapewyrm.types import DriveProfile

log = logging.getLogger(__name__)


def default_config_path() -> Path:
    """The per-user config file read when ``--config`` is not given.

    Stdlib only, no platformdirs: one small file does not justify a dependency,
    and CLI users expect the XDG location on every Unix, macOS included (git,
    gh and uv all use ~/.config there too).

    * Windows: ``%APPDATA%\\tapewyrm\\config.toml``
    * elsewhere: ``$XDG_CONFIG_HOME/tapewyrm/config.toml``, defaulting to
      ``~/.config/tapewyrm/config.toml``. The XDG spec says a relative
      ``XDG_CONFIG_HOME`` is invalid and must be ignored, so it is.
    """
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME", "")
        if xdg and not Path(xdg).is_absolute():
            log.debug("XDG_CONFIG_HOME=%r is relative; ignoring it per the XDG spec", xdg)
            xdg = ""
        base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "tapewyrm" / "config.toml"


@dataclass
class AppContext:
    """Resolved run context shared across subcommands (DESIGN.md §6A.7)."""

    port: str | None
    profile_name: str
    # None means ``--profile auto``: the profile is picked per drive session by
    # probing (``qic117.drive.auto_wake``), because it depends on the hardware.
    profile: DriveProfile | None
    passes: int = 1
    out_dir: Path | None = None
    settings: dict[str, Any] = field(default_factory=dict)
    # stderr console shared by logging and the progress bars (tapewyrm.console)
    console: Console = field(default_factory=make_console)
    show_progress: bool = False

    @contextmanager
    def progress(self) -> Iterator[Progress]:
        """Live progress bars for one command if ``--progress``, else a no-op."""
        with progress_display(self.console, self.show_progress) as prog:
            yield prog

    @classmethod
    def load(
        cls,
        port: str | None,
        profile: str | None,
        config: str | None,
        console: Console | None = None,
    ) -> AppContext:
        """Resolve config precedence CLI -> file -> profile defaults.

        A value set on the CLI wins; otherwise the config file supplies it;
        otherwise the profile / built-in defaults apply. The config file is
        ``config`` when given (and then only that file), else the per-user
        ``default_config_path()`` when it exists. With no profile named
        anywhere the profile is ``auto`` and ``self.profile`` is None.

        ``console`` is the stderr console logging was already routed to; the
        group builds it and calls ``setup_logging`` *before* this, so the
        config/profile debug lines below show under ``-v``. ``None`` makes a
        fresh one (tests, library callers).
        """
        file_settings: dict[str, Any] = {}
        if config is not None:
            cfg_path = Path(config)
        else:
            cfg_path = default_config_path()
            log.debug("no --config; looking for the per-user config at %s", cfg_path)
        if cfg_path.exists():
            log.debug("loading config %s", cfg_path)
            try:
                with cfg_path.open("rb") as f:
                    file_settings = tomllib.load(f)
            except (OSError, tomllib.TOMLDecodeError) as exc:
                # The per-user file is read on every run, so a typo in it must
                # say which file to fix rather than end in a traceback.
                log.debug("config %s failed to load: %r", cfg_path, exc)
                raise click.ClickException(f"could not read config {cfg_path}: {exc}") from exc
        else:
            log.debug("config %s does not exist; using CLI/profile defaults", cfg_path)

        # Precedence for each resolvable setting.
        resolved_port = port if port is not None else file_settings.get("port")
        resolved_profile_name = str(
            profile if profile is not None else file_settings.get("profile", AUTO)
        )
        prof: DriveProfile | None = None
        if resolved_profile_name == AUTO:
            log.debug("drive profile is %r: picked by probing at session time", AUTO)
        else:
            log.debug("loading drive profile %r", resolved_profile_name)
        try:
            if resolved_profile_name != AUTO:
                prof = load_profile(resolved_profile_name)
        except Exception as exc:  # ProfileError or IO — surface as a CLI error
            log.debug("profile %r failed to load: %r", resolved_profile_name, exc)
            raise click.ClickException(
                f"could not load profile {resolved_profile_name!r}: {exc}"
            ) from exc

        passes = int(file_settings.get("passes", 1))
        out_dir = file_settings.get("out_dir")
        return cls(
            port=resolved_port,
            profile_name=resolved_profile_name,
            profile=prof,
            passes=passes,
            out_dir=Path(out_dir) if out_dir else None,
            settings=file_settings,
            console=console if console is not None else make_console(),
        )


# ---------------------------------------------------------------------------
# Group + shared options
# ---------------------------------------------------------------------------


@click.group()
@click.version_option(package_name="tapewyrm", prog_name="tw")
@click.option("--port", default=None, help="GW serial port (autodetect if unset)")
@click.option(
    "--profile",
    default=None,
    help="drive profile name or path; default 'auto' (tries ftape's wake-ups, see docs)",
)
@click.option(
    "--config",
    type=click.Path(),
    default=None,
    help="config TOML file (default: ~/.config/tapewyrm/config.toml if present)",
)
@click.option("--progress", "show_progress", is_flag=True, help="show progress bars (stderr)")
@click.option("-v", "--verbose", count=True, help="more log output (-v for debug)")
@click.option("-q", "--quiet", count=True, help="less log output (-q warnings, -qq errors)")
@click.pass_context
def cli(
    ctx: click.Context,
    port: str | None,
    profile: str | None,
    config: str | None,
    show_progress: bool,
    verbose: int,
    quiet: int,
) -> None:
    """tw — Tapewyrm: QIC-80 floppy-tape recovery over Greaseweazle v4.1.

    The single Tapewyrm tool: capture, decode, recover, and flash firmware.
    Does not require the ``gw`` executable.
    """
    # Logging first: AppContext.load reads the config file and drive profile,
    # and its debug lines are exactly what -v is for when one isn't picked up.
    console = make_console()
    setup_logging(console, verbose, quiet)
    app = AppContext.load(port, profile, config, console)
    app.show_progress = show_progress
    log.debug(
        "context: port=%r profile=%r passes=%d out_dir=%s progress=%s",
        app.port,
        app.profile_name,
        app.passes,
        app.out_dir,
        show_progress,
    )
    ctx.obj = app
