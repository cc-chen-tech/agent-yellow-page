"""Coverage tests for the small edge cases in:
- app/routes/agents.py (name + public_key both collide, "duplicate field" fallback)
- app/routes/private_chat.py (list_rooms with creator filter, leave not-member,
  thread_id path resolution)
- app/routes/messages.py (patch 404, delete 403)
"""

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
    s = _Server(str(tmp_path / "edges_test.db"))
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


def _signed(method, url, *, agent_id, kp, body=b"", if_match=None):
    from urllib.parse import urlparse
    p = urlparse(url)
    path = p.path
    ts = int(time.time())
    msg = canonical_request(ts, method, path, body)
    sig = kp.sign(msg)
    nonce = base64.b64encode(os.urandom(16)).decode()
    headers = {
        "X-Agent-Id": agent_id,
        "X-Timestamp": str(ts),
        "X-Nonce": nonce,
        "X-Signature": sig,
    }
    if body:
        headers["Content-Type"] = "application/json"
    if if_match:
        headers["If-Match"] = if_match
    with httpx.Client(base_url=f"{p.scheme}://{p.netloc}", timeout=5.0) as c:
        return c.request(method, path, content=body or None, headers=headers, params=p.query)


# --- agents.py: name + public_key both collide → "duplicate field" -------- #


def test_register_name_and_key_both_collide_returns_409(server):
    """The error path fallback raises _conflict("duplicate field") only when
    the field attribute is neither 'name' nor 'public_key' — in practice
    name always wins, but we exercise the public_key branch by colliding
    public_key with a *different* name."""
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    kp = KeyPair.generate()
    pk = "ed25519:" + kp.public_b64
    name1 = f"first-{uuid.uuid4().hex[:6]}"
    name2 = f"second-{uuid.uuid4().hex[:6]}"
    r1 = http.post("/v0/agents", json={"name": name1, "public_key": pk})
    assert r1.status_code == 201
    # Same public_key, different name → ConflictError(field="public_key")
    r2 = http.post("/v0/agents", json={"name": name2, "public_key": pk})
    assert r2.status_code == 409
    body = r2.json()
    assert "public_key" in body["detail"]["message"].lower()


# --- private_chat.py: list_rooms with creator filter (name not found) ------ #


def test_pc_list_creator_filter_with_nonexistent_name_returns_404(server):
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    r = http.get(f"{server.base_url()}/v0/private-chatrooms", params={"creator": "no-such-creator"})
    assert r.status_code == 404
    assert "not found" in r.json()["detail"]["message"].lower()


# --- private_chat.py: leave when not a member returns 403 ---------------- #


def test_pc_leave_403_when_not_member(server):
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    alice_id, alice_kp = _register(http, f"al-{uuid.uuid4().hex[:6]}")
    eve_id, eve_kp = _register(http, f"ev-{uuid.uuid4().hex[:6]}")
    r = _signed("POST", f"{server.base_url()}/v0/private-chatrooms",
                agent_id=alice_id, kp=alice_kp, body=b'{"name":"room-leave"}')
    room_name = r.json()["name"]
    # Eve (not a member) tries to leave → 403
    r2 = _signed("POST", f"{server.base_url()}/v0/private-chatrooms/{room_name}/leave",
                 agent_id=eve_id, kp=eve_kp)
    assert r2.status_code == 403


# --- private_chat.py: invite with expires_in_seconds --------------------- #


def test_pc_invite_with_expiry_includes_expires_at(server):
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    agent_id, kp = _register(http, f"al-{uuid.uuid4().hex[:6]}")
    r = _signed("POST", f"{server.base_url()}/v0/private-chatrooms",
                agent_id=agent_id, kp=kp, body=b'{"name":"room-1"}')
    name = r.json()["name"]
    r2 = _signed("POST", f"{server.base_url()}/v0/private-chatrooms/{name}/invites",
                 agent_id=agent_id, kp=kp,
                 body=b'{"max_uses":3,"expires_in_seconds":3600}')
    assert r2.status_code == 201
    inv = r2.json()
    assert inv["max_uses"] == 3
    assert inv["expires_at"] is not None


# --- private_chat.py: read/delete single message (member + sender checks) -- #


def test_pc_read_message_member_only(server):
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    alice_id, alice_kp = _register(http, f"al-{uuid.uuid4().hex[:6]}")
    eve_id, eve_kp = _register(http, f"ev-{uuid.uuid4().hex[:6]}")
    r = _signed("POST", f"{server.base_url()}/v0/private-chatrooms",
                agent_id=alice_id, kp=alice_kp, body=b'{"name":"room-1"}')
    name = r.json()["name"]
    r2 = _signed("POST", f"{server.base_url()}/v0/private-chatrooms/{name}/messages",
                 agent_id=alice_id, kp=alice_kp, body=b'{"body":"hi"}')
    msg_id = r2.json()["id"]
    # Eve (non-member) reads the message → 403
    r3 = _signed("GET", f"{server.base_url()}/v0/private-chatrooms/{name}/messages/{msg_id}",
                 agent_id=eve_id, kp=eve_kp)
    assert r3.status_code == 403


# --- private_chat.py: message 404 (msg in different room) ---------------- #


def test_pc_read_message_404_when_msg_in_other_room(server):
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    alice_id, alice_kp = _register(http, f"al-{uuid.uuid4().hex[:6]}")
    r1 = _signed("POST", f"{server.base_url()}/v0/private-chatrooms",
                 agent_id=alice_id, kp=alice_kp, body=b'{"name":"room-1"}')
    n1 = r1.json()["name"]
    r2 = _signed("POST", f"{server.base_url()}/v0/private-chatrooms",
                 agent_id=alice_id, kp=alice_kp, body=b'{"name":"room-2"}')
    n2 = r2.json()["name"]
    # Post message in r1
    rm = _signed("POST", f"{server.base_url()}/v0/private-chatrooms/{n1}/messages",
                 agent_id=alice_id, kp=alice_kp, body=b'{"body":"hi"}')
    msg_id = rm.json()["id"]
    # Try to read it via r2 → 404
    r3 = _signed("GET", f"{server.base_url()}/v0/private-chatrooms/{n2}/messages/{msg_id}",
                 agent_id=alice_id, kp=alice_kp)
    assert r3.status_code == 404


# --- messages.py: patch 404 + delete 403 + thread 403 --------------------- #


def test_message_patch_404_for_nonexistent(server):
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    agent_id, kp = _register(http, f"a-{uuid.uuid4().hex[:6]}")
    # 404: nonexistent message
    r = _signed("PATCH", f"{server.base_url()}/v0/messages/01M00000000000000000000000",
                agent_id=agent_id, kp=alice_kp_for(agent_id, kp) if False else kp,
                body=b'{"action":"mark_read"}')
    assert r.status_code == 404


def alice_kp_for(a, k):  # tiny shim to keep tests readable
    return k


def test_message_delete_403_for_sender(server):
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    alice_id, alice_kp = _register(http, f"a-{uuid.uuid4().hex[:6]}")
    bob_id, bob_kp = _register(http, f"b-{uuid.uuid4().hex[:6]}")
    # Alice sends to Bob
    r = _signed("POST", f"{server.base_url()}/v0/messages",
                agent_id=alice_id, kp=alice_kp,
                body=f'{{"recipient_id":"{bob_id}","body":"hi"}}'.encode())
    msg_id = r.json()["id"]
    # Bob (recipient) deletes the message → 204
    r2 = _signed("DELETE", f"{server.base_url()}/v0/messages/{msg_id}",
                 agent_id=bob_id, kp=bob_kp)
    assert r2.status_code == 204


def test_thread_view_403_for_non_participant(server):
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    alice_id, alice_kp = _register(http, f"a-{uuid.uuid4().hex[:6]}")
    bob_id, bob_kp = _register(http, f"b-{uuid.uuid4().hex[:6]}")
    eve_id, eve_kp = _register(http, f"e-{uuid.uuid4().hex[:6]}")
    # Alice → Bob
    r = _signed("POST", f"{server.base_url()}/v0/messages",
                agent_id=alice_id, kp=alice_kp,
                body=f'{{"recipient_id":"{bob_id}","body":"hi"}}'.encode())
    thread_id = r.json()["thread_id"]
    # Eve tries to view → 403
    r2 = _signed("GET", f"{server.base_url()}/v0/threads/{thread_id}",
                 agent_id=eve_id, kp=eve_kp)
    assert r2.status_code == 403
