"""Shared pytest set-up for tapewyrm-cli.

``tw`` reads a per-user config file when ``--config`` is not given
(``cli.default_config_path``). Point it at an empty temporary directory for
every test, so a developer's own ~/.config/tapewyrm/config.toml can never
change what the tests see.
"""

import pytest


@pytest.fixture(autouse=True)
def _isolated_user_config(tmp_path, monkeypatch):
    config_home = tmp_path / "user-config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("APPDATA", str(config_home))
    return config_home
