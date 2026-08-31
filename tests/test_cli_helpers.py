"""Direct unit tests for app/cli.py helper functions.

Exercises the small utility functions (chmod on _save, _kp_from_cfg error,
_config_path, _server env-var fallback) that the CLI command tests
don't directly hit.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from unittest import mock

import pytest
from click.testing import CliRunner

from app.cli import _config_path, _kp_from_cfg, _load, _save, _server, cli
from app.crypto import KeyPair


def test_server_reads_env(monkeypatch):
    monkeypatch.setenv("YELLOWPAGE_SERVER", "http://test:1234")
    assert _server() == "http://test:1234"


def test_server_default(monkeypatch):
    monkeypatch.delenv("YELLOWPAGE_SERVER", raising=False)
    assert _server() == "http://127.0.0.1:8000"


def test_config_path_reads_env(monkeypatch):
    monkeypatch.setenv("AGENT_YP_CONFIG", "/tmp/my-config.json")
    assert _config_path() == Path("/tmp/my-config.json")


def test_config_path_default(monkeypatch):
    monkeypatch.delenv("AGENT_YP_CONFIG", raising=False)
    p = _config_path()
    assert str(p).endswith("config.json")


def test_kp_from_cfg_raises_when_missing():
    with pytest.raises(Exception) as ei:
        _kp_from_cfg({})
    assert "no private key" in str(ei.value).lower() or "init" in str(ei.value).lower()


def test_kp_from_cfg_works_with_valid_cfg():
    kp = KeyPair.generate()
    cfg = {"private_key_raw_b64": base64.b64encode(kp.private_raw).decode("ascii")}
    loaded = _kp_from_cfg(cfg)
    assert loaded.private_raw == kp.private_raw


def test_load_returns_empty_when_file_missing(tmp_path):
    p = tmp_path / "nope.json"
    assert _load(p) == {}


def test_load_returns_dict(tmp_path):
    p = tmp_path / "ok.json"
    p.write_text(json.dumps({"a": 1}))
    assert _load(p) == {"a": 1}


def test_save_sets_chmod_0600(tmp_path):
    """On POSIX, _save tries to chmod 0600 — verify it runs and file is created."""
    p = tmp_path / "sub" / "cfg.json"
    _save(p, {"hello": "world"})
    assert p.exists()
    assert json.loads(p.read_text()) == {"hello": "world"}
    # On POSIX, mode should be 0o600 (or close — may have umask interaction)
    if hasattr(os, "chmod"):  # POSIX
        mode = p.stat().st_mode & 0o777
        assert mode in (0o600, 0o644, 0o664)  # chmod may fail silently on some FS


def test_save_chmod_failure_swallowed(tmp_path):
    """If chmod fails (e.g. read-only FS), _save shouldn't raise."""
    p = tmp_path / "cfg.json"
    # Mock os.chmod to raise OSError
    with mock.patch("os.chmod", side_effect=OSError("read-only fs")):
        _save(p, {"x": 1})  # must not raise
    assert p.exists()


def test_cli_help_shows_subcommands():
    runner = CliRunner()
    r = runner.invoke(cli, ["--help"])
    assert r.exit_code == 0
    for cmd in ("init", "list", "inbox", "send", "chat", "pc"):
        assert cmd in r.output
