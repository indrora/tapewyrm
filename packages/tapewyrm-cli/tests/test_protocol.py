"""The generated protocol module is in sync with protocol.toml and self-consistent."""

import subprocess
import sys
import tomllib
from pathlib import Path

from tapewyrm.link import protocol

ROOT = Path(__file__).resolve().parents[3]  # tests -> tapewyrm-cli -> packages -> repo


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


def test_twrf_wire_markers_match_the_firmware_protocol():
    """TWRF files store marker opcodes; tapewyrm-archive keeps its own copy.

    If the firmware protocol renumbers a marker, captures written before and
    after would disagree. This keeps that from happening silently.
    """
    from tapewyrm_archive.twrf import WireMarker

    from tapewyrm.link.protocol import Marker

    assert {m.name: m.value for m in WireMarker} == {m.name: m.value for m in Marker}
