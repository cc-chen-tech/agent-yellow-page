"""Coverage tests for app/main.py — mostly the `main()` console entrypoint.

We don't actually start uvicorn; we patch it out and assert the env-driven
host / port / db / reload values get passed through correctly.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

import app.main as main_mod


@pytest.fixture
def patch_uvicorn(monkeypatch):
    """Replace uvicorn.run so calling main() returns immediately."""
    fake = mock.MagicMock()
    monkeypatch.setitem(sys.modules, "uvicorn", fake)
    return fake


def test_main_uses_default_env(patch_uvicorn, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # Clear any host/port override from the test env
    for k in ("YELLOWPAGE_HOST", "YELLOWPAGE_PORT", "YELLOWPAGE_DB", "YELLOWPAGE_RELOAD"):
        monkeypatch.delenv(k, raising=False)
    main_mod.main()
    args, kwargs = patch_uvicorn.run.call_args
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 8000
    assert kwargs["reload"] is False
    # factory should be False (we don't need a fresh app each reload)
    assert kwargs["factory"] is False
    # The default db path's parent is created
    assert Path("./data").exists()


def test_main_honors_env_overrides(patch_uvicorn, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("YELLOWPAGE_HOST", "0.0.0.0")
    monkeypatch.setenv("YELLOWPAGE_PORT", "9999")
    monkeypatch.setenv("YELLOWPAGE_DB", str(tmp_path / "alt" / "yellow.db"))
    monkeypatch.setenv("YELLOWPAGE_RELOAD", "1")

    main_mod.main()
    args, kwargs = patch_uvicorn.run.call_args
    assert kwargs["host"] == "0.0.0.0"
    assert kwargs["port"] == 9999
    assert kwargs["reload"] is True
    assert (tmp_path / "alt").exists()  # mkdir -p ran
    assert "app.main:app" in args  # the module:app string


def test_module_app_is_singleton():
    """Importing app.main creates exactly one app object."""
    # Reimport to confirm idempotence
    importlib.reload(main_mod)
    assert main_mod.app is not None
    assert main_mod.app.title == "AI Agent Yellow Page"


def test_app_factory_with_custom_db(tmp_path):
    """create_app(db_path=...) builds an isolated app per call."""
    from app.main import create_app
    db = str(tmp_path / "x.db")
    app = create_app(db_path=db)
    assert app.state.db_path == db
    # A different db path → different app instance
    db2 = str(tmp_path / "y.db")
    app2 = create_app(db_path=db2)
    assert app is not app2
    assert app2.state.db_path == db2
