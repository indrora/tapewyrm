"""tapewyrm.console: the CLI's logging set-up reaches every library package."""

import logging

from rich.console import Console

from tapewyrm.console import setup_logging


def test_setup_logging_covers_every_library_package():
    # Library code logs under its own top-level package name; one left out of
    # the CLI's set goes silent below WARNING (this happened when qiclib split
    # out, and `tw extract` lost every log line).
    setup_logging(Console(stderr=True), verbose=1, quiet=0)
    for name in ("tapewyrm", "tapewyrm_archive", "qiclib"):
        logger = logging.getLogger(name)
        assert logger.handlers and logger.level == logging.DEBUG, name
