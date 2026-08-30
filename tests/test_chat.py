"""End-to-end tests for the public chatroom API."""

from __future__ import annotations

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
    db = str(tmp_path / "chat_test.db")
    s = _Server(db)
    yield s
    s.stop()


def _register(http, name: str) -> tuple[str, KeyPair]:
    kp = KeyPair.generate()
    r = http.post(
        "/v0/agents",
        json={"name": name, "public_key": "ed25519:" + kp.public_b64},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"], kp


def _signed(method: str, url: str, *, agent_id: str, kp: KeyPair, body: bytes = b""):
    """Manually sign and send a request."""
    from urllib.parse import urlparse
    import base64
    import os as _os

    p = urlparse(url)
    path = p.path
    ts = int(time.time())
    msg = canonical_request(ts, method, path, body)
    sig = kp.sign(msg)
    nonce = base64.b64encode(_os.urandom(16)).decode("ascii")
    headers = {
        "X-Agent-Id": agent_id,
        "X-Timestamp": str(ts),
        "X-Nonce": nonce,
        "X-Signature": sig,
    }
    if body:
        headers["Content-Type"] = "application/json"
    with httpx.Client(base_url=f"{p.scheme}://{p.netloc}", timeout=5.0) as c:
        return c.request(method, path, content=body or None, headers=headers, params=p.query)


# --- POST /v0/chat ------------------------------------------------------ #


def test_post_requires_signature(server):
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    r = http.post("/v0/chat", json={"body": "hi"})
    assert r.status_code == 401  # missing signature


def test_post_creates_message(server):
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    alice_id, alice_kp = _register(http, f"alice-{uuid.uuid4().hex[:6]}")
    body = b'{"body":"hello chatroom!"}'
    r = _signed("POST", f"{server.base_url()}/v0/chat", agent_id=alice_id, kp=alice_kp, body=body)
    assert r.status_code == 201, r.text
    m = r.json()
    assert m["body"] == "hello chatroom!"
    assert m["sender_id"] == alice_id
    assert m["sender_name"].startswith("alice-")
    assert "id" in m and "created_at" in m


def test_post_rejects_empty_body(server):
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    alice_id, alice_kp = _register(http, f"alice-{uuid.uuid4().hex[:6]}")
    r = _signed("POST", f"{server.base_url()}/v0/chat", agent_id=alice_id, kp=alice_kp, body=b'{"body":""}')
    assert r.status_code == 422  # pydantic validation


# --- GET /v0/chat ------------------------------------------------------- #


def test_list_is_public_no_signature_needed(server):
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    # no agent registered, no auth — should still list (empty)
    r = http.get("/v0/chat")
    assert r.status_code == 200
    body = r.json()
    assert body == {"total": 0, "items": []}


def test_list_returns_newest_first(server):
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    alice_id, alice_kp = _register(http, f"alice-{uuid.uuid4().hex[:6]}")
    sent_ids = []
    for i in range(3):
        body = f'{{"body":"msg {i}"}}'.encode()
        r = _signed("POST", f"{server.base_url()}/v0/chat", agent_id=alice_id, kp=alice_kp, body=body)
        sent_ids.append(r.json()["id"])
        time.sleep(0.01)  # ensure created_at differs

    r = http.get("/v0/chat")
    body = r.json()
    assert body["total"] == 3
    assert len(body["items"]) == 3
    # Newest first
    assert body["items"][0]["id"] == sent_ids[-1]
    assert body["items"][-1]["id"] == sent_ids[0]


def test_list_pagination(server):
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    alice_id, alice_kp = _register(http, f"alice-{uuid.uuid4().hex[:6]}")
    for i in range(5):
        body = f'{{"body":"m{i}"}}'.encode()
        _signed("POST", f"{server.base_url()}/v0/chat", agent_id=alice_id, kp=alice_kp, body=body)

    r = http.get("/v0/chat?limit=2&offset=0")
    page1 = r.json()
    r = http.get("/v0/chat?limit=2&offset=2")
    page2 = r.json()
    r = http.get("/v0/chat?limit=2&offset=4")
    page3 = r.json()
    assert page1["total"] == 5
    assert len(page1["items"]) == 2
    assert len(page2["items"]) == 2
    assert len(page3["items"]) == 1
    # No overlap
    ids = {m["id"] for m in page1["items"] + page2["items"] + page3["items"]}
    assert len(ids) == 5


# --- GET /v0/chat/{id} -------------------------------------------------- #


def test_get_is_public(server):
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    alice_id, alice_kp = _register(http, f"alice-{uuid.uuid4().hex[:6]}")
    r = _signed("POST", f"{server.base_url()}/v0/chat", agent_id=alice_id, kp=alice_kp, body=b'{"body":"hi"}')
    msg_id = r.json()["id"]
    r = http.get(f"/v0/chat/{msg_id}")
    assert r.status_code == 200
    assert r.json()["body"] == "hi"


def test_get_404_for_unknown_id(server):
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    r = http.get("/v0/chat/01ABCDEFGHJKMNPQRSTVWXYZ12")  # valid ULID-shape but not in db
    assert r.status_code == 404


# --- DELETE /v0/chat/{id} ----------------------------------------------- #


def test_sender_can_delete(server):
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    alice_id, alice_kp = _register(http, f"alice-{uuid.uuid4().hex[:6]}")
    r = _signed("POST", f"{server.base_url()}/v0/chat", agent_id=alice_id, kp=alice_kp, body=b'{"body":"bye"}')
    msg_id = r.json()["id"]

    r = _signed("DELETE", f"{server.base_url()}/v0/chat/{msg_id}", agent_id=alice_id, kp=alice_kp)
    assert r.status_code == 204

    r = http.get(f"/v0/chat/{msg_id}")
    assert r.status_code == 404


def test_non_sender_cannot_delete(server):
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    alice_id, alice_kp = _register(http, f"alice-{uuid.uuid4().hex[:6]}")
    eve_id, eve_kp = _register(http, f"eve-{uuid.uuid4().hex[:6]}")
    r = _signed("POST", f"{server.base_url()}/v0/chat", agent_id=alice_id, kp=alice_kp, body=b'{"body":"alice"}')
    msg_id = r.json()["id"]

    r = _signed("DELETE", f"{server.base_url()}/v0/chat/{msg_id}", agent_id=eve_id, kp=eve_kp)
    assert r.status_code == 403


def test_delete_requires_signature(server):
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    r = http.delete("/v0/chat/01ABCDEFGHJKMNPQRSTVWXYZ12")
    assert r.status_code == 401


def test_sender_agent_deleted_cascades(server):
    """Deleting the sender agent should also delete their chat messages."""
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    alice_id, alice_kp = _register(http, f"alice-{uuid.uuid4().hex[:6]}")
    r = _signed("POST", f"{server.base_url()}/v0/chat", agent_id=alice_id, kp=alice_kp, body=b'{"body":"hi"}')
    msg_id = r.json()["id"]

    # delete the agent (signed)
    r = _signed("DELETE", f"{server.base_url()}/v0/agents/{alice_id}", agent_id=alice_id, kp=alice_kp)
    assert r.status_code == 204

    # chat message should be gone (FK cascade)
    r = http.get(f"/v0/chat/{msg_id}")
    assert r.status_code == 404
    r = http.get("/v0/chat")
    assert r.json()["total"] == 0
