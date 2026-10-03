"""tapewyrm.console: the CLI's logging set-up reaches every library package."""

import logging

from rich.console import Console

from tapewyrm.console import setup_logging


def test_setup_logging_covers_every_library_package():
    # Library code logs under its own top-level package name; one left out of
    # the CLI's set goes silent below WARNING (this happened when qiclib split
    # out, and extraction lost every log line).
    setup_logging(Console(stderr=True), verbose=1, quiet=0)
    for name in ("tapewyrm", "tapewyrm_archive", "qiclib"):
        logger = logging.getLogger(name)
        assert logger.handlers and logger.level == logging.DEBUG, name


def test_verbose_shows_debug_from_context_loading():
    # Config and profile loading happen in the group callback; their debug
    # lines are what you need when a config or profile isn't picked up, so
    # logging must be set up before AppContext.load runs. A subcommand's
    # --help still runs the group callback first (click parses the
    # subcommand's args after it), and touches no hardware.
    from click.testing import CliRunner

    from tapewyrm.cli import cli

    # Wide COLUMNS so rich does not wrap the log line mid-phrase.
    res = CliRunner(env={"COLUMNS": "200"}).invoke(
        cli, ["-v", "--profile", "default", "info", "--help"]
    )
    assert res.exit_code == 0, res.output
    assert "using built-in DriveProfile.default()" in res.stderr
