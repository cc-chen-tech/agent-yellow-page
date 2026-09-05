"""Coverage tests for app/main.py meta endpoints (landing, sitemap, feed)."""

from __future__ import annotations

import os
import socket
import threading
import time
import uuid

import httpx
import pytest
import uvicorn

from app.crypto import KeyPair
from app.main import create_app


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Server:
    def __init__(self, db_path: str):
        self.port = _free_port()
        self.app = create_app(db_path=db_path)
        config = uvicorn.Config(
            self.app, host="127.0.0.1", port=self.port, log_level="warning"
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._service_run, daemon=True)
        self._thread.start()
        for _ in range(200):
            if self._server.started:
                break
            time.sleep(0.025)

    def _service_run(self):
        self._server.run()

    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)


@pytest.fixture
def server(tmp_path):
    s = _Server(str(tmp_path / "meta_test.db"))
    yield s
    s.stop()


def test_landing_returns_html(server):
    r = httpx.get(f"{server.base_url()}/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    body = r.text
    # Smoke-check key content
    assert "AI Agent Yellow Page" in body
    assert "agent-yp init" in body
    assert "/v0/agents" in body
    assert "https://github.com/cc-chen-tech/agent-yellow-page" in body


def test_sitemap_returns_xml(server):
    r = httpx.get(f"{server.base_url()}/sitemap.xml")
    assert r.status_code == 200
    assert "xml" in r.headers["content-type"]
    body = r.text
    assert "<?xml" in body
    assert "<urlset" in body
    # core paths listed
    assert "/v0/agents" in body
    assert "/v0/chat" in body
    assert "/docs" in body
    # uses the public base (default fallback in absence of env var)
    assert "47.94.164.38" in body or "127.0.0.1" in body or "localhost" in body


def test_sitemap_uses_yellowpage_public_url_env(monkeypatch, tmp_path):
    """Sitemap should use YELLOWPAGE_PUBLIC_URL when set."""
    monkeypatch.setenv("YELLOWPAGE_PUBLIC_URL", "https://my.example.com")
    s = _Server(str(tmp_path / "sitemap_env.db"))
    try:
        r = httpx.get(f"{s.base_url()}/sitemap.xml")
        assert "https://my.example.com/v0/agents" in r.text
    finally:
        s.stop()


def test_feed_returns_rss_with_recent_agents(server):
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    # Register a few agents
    for i in range(3):
        kp = KeyPair.generate()
        r = http.post(
            "/v0/agents",
            json={
                "name": f"feed-{i}-{uuid.uuid4().hex[:6]}",
                "public_key": "ed25519:" + kp.public_b64,
                "description": f"feed agent {i}",
            },
        )
        assert r.status_code == 201
    # Fetch feed
    r = httpx.get(f"{server.base_url()}/feed.xml")
    assert r.status_code == 200
    assert "application/rss+xml" in r.headers["content-type"]
    body = r.text
    assert "<rss" in body
    assert "<channel>" in body
    # At least one of the test agents appears
    assert "feed-" in body
    # Items are present
    assert body.count("<item>") >= 3


def test_feed_with_empty_db(server):
    r = httpx.get(f"{server.base_url()}/feed.xml")
    assert r.status_code == 200
    body = r.text
    assert "<rss" in body
    # Should have an empty channel (no items)
    assert "<item>" not in body
