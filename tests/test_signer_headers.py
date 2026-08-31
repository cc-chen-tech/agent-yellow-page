"""Coverage for app/signer.py header validation edge cases."""

from __future__ import annotations

import base64
import os
import socket
import threading
import time
import uuid

import httpx
import pytest
import uvicorn

from app.crypto import KeyPair, canonical_request
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
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        for _ in range(200):
            if self._server.started:
                break
            time.sleep(0.025)

    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)


@pytest.fixture
def server(tmp_path):
    s = _Server(str(tmp_path / "signer_test.db"))
    yield s
    s.stop()


def _register(http, name: str) -> tuple[str, KeyPair]:
    kp = KeyPair.generate()
    r = http.post(
        "/v0/agents",
        json={"name": name, "public_key": "ed25519:" + kp.public_b64},
    )
    assert r.status_code == 201
    return r.json()["id"], kp


def test_signed_endpoint_missing_all_headers(server):
    """Request without any signed-write headers → 401 'missing ...'."""
    # Use /v0/messages (no agent_id in path) so the signer dep runs.
    r = httpx.post(f"{server.base_url()}/v0/messages", json={"body": "x"})
    assert r.status_code == 401
    assert "missing" in r.json()["detail"]["message"].lower()


def test_signed_endpoint_non_integer_timestamp(server):
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    agent_id, kp = _register(http, f"a-{uuid.uuid4().hex[:6]}")
    nonce = base64.b64encode(os.urandom(16)).decode()
    path = "/v0/messages"
    msg = canonical_request(0, "POST", path, b"")
    sig = kp.sign(msg)
    r = httpx.post(
        f"{server.base_url()}{path}",
        headers={
            "X-Agent-Id": agent_id,
            "X-Timestamp": "not-a-number",
            "X-Nonce": nonce,
            "X-Signature": sig,
        },
        json={"body": "hi"},
    )
    assert r.status_code == 401
    body = r.json()["detail"]["message"].lower()
    assert "timestamp" in body or "int" in body


def test_signed_endpoint_skewed_timestamp_returns_410(server):
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    agent_id, kp = _register(http, f"a-{uuid.uuid4().hex[:6]}")
    nonce = base64.b64encode(os.urandom(16)).decode()
    path = "/v0/messages"
    skewed_ts = int(time.time()) - 10_000  # far outside ±300s window
    msg = canonical_request(skewed_ts, "POST", path, b"")
    sig = kp.sign(msg)
    r = httpx.post(
        f"{server.base_url()}{path}",
        headers={
            "X-Agent-Id": agent_id,
            "X-Timestamp": str(skewed_ts),
            "X-Nonce": nonce,
            "X-Signature": sig,
        },
        json={"body": "hi"},
    )
    assert r.status_code == 410


def test_signed_endpoint_unknown_agent_returns_401(server):
    nonce = base64.b64encode(os.urandom(16)).decode()
    fake_id = "01M00000000000000000000000"
    fake_kp = KeyPair.generate()
    path = "/v0/messages"
    msg = canonical_request(int(time.time()), "POST", path, b"")
    sig = fake_kp.sign(msg)
    r = httpx.post(
        f"{server.base_url()}{path}",
        headers={
            "X-Agent-Id": fake_id,
            "X-Timestamp": str(int(time.time())),
            "X-Nonce": nonce,
            "X-Signature": sig,
        },
        json={"body": "hi"},
    )
    assert r.status_code == 401
    assert "unknown agent" in r.json()["detail"]["message"].lower()
