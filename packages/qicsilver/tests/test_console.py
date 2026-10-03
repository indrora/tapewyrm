"""qicsilver.console: the CLI's logging set-up reaches every library package."""

import logging

from rich.console import Console

from qicsilver.console import setup_logging


def test_setup_logging_covers_every_library_package():
    # A package left out of the set goes silent below WARNING (see tapewyrm-cli's
    # twin of this test, which caught exactly that when qiclib split out).
    setup_logging(Console(stderr=True), verbose=1, quiet=0)
    for name in ("qicsilver", "qiclib", "tapewyrm_archive"):
        logger = logging.getLogger(name)
        assert logger.handlers and logger.level == logging.DEBUG, name
