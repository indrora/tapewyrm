"""`tw convert` options: -j reaches the library, and a bad value stops before any decode."""

from __future__ import annotations

from click.testing import CliRunner

import tapewyrm.cli as cli_mod
from tapewyrm.image import convert as convert_mod


def _recording(calls: list) -> object:
    def fake_convert(sources, out, *, progress, jobs):
        calls.append((list(sources), out, jobs))

    return fake_convert


def _invoke(tmp_path, monkeypatch, *flags: str):
    calls: list = []
    monkeypatch.setattr(convert_mod, "convert", _recording(calls))
    src = tmp_path / "dump"
    src.mkdir(exist_ok=True)
    res = CliRunner().invoke(cli_mod.cli, ["convert", *flags, str(src), str(tmp_path / "t.twtz")])
    return res, calls


def test_jobs_reaches_convert(tmp_path, monkeypatch):
    res, calls = _invoke(tmp_path, monkeypatch, "-j", "4")
    assert res.exit_code == 0, res.output
    assert calls == [([tmp_path / "dump"], tmp_path / "t.twtz", 4)]


def test_jobs_zero_means_automatic(tmp_path, monkeypatch):
    res, calls = _invoke(tmp_path, monkeypatch, "--jobs", "0")
    assert res.exit_code == 0, res.output
    assert calls[0][2] == 0


def test_jobs_defaults_to_one(tmp_path, monkeypatch):
    res, calls = _invoke(tmp_path, monkeypatch)
    assert res.exit_code == 0, res.output
    assert calls[0][2] == 1


def test_negative_jobs_are_refused_before_any_decode(tmp_path, monkeypatch):
    res, calls = _invoke(tmp_path, monkeypatch, "-j", "-1")
    assert res.exit_code == 2
    assert "-j" in res.output or "--jobs" in res.output
    assert calls == []
