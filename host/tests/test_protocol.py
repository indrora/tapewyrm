"""The generated protocol module is in sync with protocol.toml and self-consistent."""

import subprocess
import sys
import tomllib
from pathlib import Path

from tapewyrm.link import protocol

ROOT = Path(__file__).resolve().parents[2]


def test_generator_check_passes():
    # The committed protocol.h / protocol.py must match protocol.toml.
    result = subprocess.run(
        [sys.executable, str(ROOT / "protocol" / "generate.py"), "--check"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_codes_match_spec():
    spec = tomllib.loads((ROOT / "protocol" / "protocol.toml").read_text())
    for entry in spec["transaction"]:
        assert int(protocol.Txn[entry["name"]]) == entry["code"]
    for entry in spec["marker"]:
        assert int(protocol.Marker[entry["name"]]) == entry["code"]
    assert protocol.PROTO_VERSION == spec["version"]
