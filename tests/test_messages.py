"""End-to-end tests for the mailbox API.

Tests cover send / inbox / outbox / read / mark-read / thread / delete, and
the permission rules (only sender or recipient can read; only recipient can
mark-read or delete; only participants can reply into a thread).
"""

from __future__ import annotations

import os
import socket
import threading
import time
import uuid

import httpx
import pytest
import uvicorn
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

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
    db = str(tmp_path / "msg_test.db")
    s = _Server(db)
    yield s
    s.stop()


def _register(http, name: str) -> tuple[str, KeyPair]:
    """Register an agent, return (agent_id, keypair)."""
    kp = KeyPair.generate()
    r = http.post(
        "/v0/agents",
        json={"name": name, "public_key": "ed25519:" + kp.public_b64},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"], kp


def _signed_request(
    method: str,
    url: str,
    *,
    agent_id: str,
    keypair: KeyPair,
    body: bytes = b"",
) -> httpx.Response:
    """Manually sign and send a request."""
    from urllib.parse import urlparse

    p = urlparse(url)
    path = p.path
    ts = int(time.time())
    msg = canonical_request(ts, method, path, body)
    sig = keypair.sign(msg)
    import base64
    import os

    nonce = base64.b64encode(os.urandom(16)).decode("ascii")
    headers = {
        "X-Agent-Id": agent_id,
        "X-Timestamp": str(ts),
        "X-Nonce": nonce,
        "X-Signature": sig,
    }
    if body:
        headers["Content-Type"] = "application/json"
    with httpx.Client(base_url=f"{p.scheme}://{p.netloc}", timeout=5.0) as c:
        return c.request(method, path, content=body if body else None, headers=headers, params=p.query)


# --- send / receive --------------------------------------------------------


def test_send_then_inbox_then_thread(server):
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    alice_id, alice_kp = _register(http, f"alice-{uuid.uuid4().hex[:6]}")
    bob_id, bob_kp = _register(http, f"bob-{uuid.uuid4().hex[:6]}")

    # 1. Alice -> Bob
    body = (
        b'{"recipient_id":"' + bob_id.encode() + b'",'
        b'"subject":"hi bob","body":"wanna collab?"}'
    )
    r = _signed_request(
        "POST", f"{server.base_url()}/v0/messages",
        agent_id=alice_id, keypair=alice_kp, body=body,
    )
    assert r.status_code == 201, r.text
    msg1 = r.json()
    assert msg1["sender_name"].startswith("alice-")
    assert msg1["recipient_name"].startswith("bob-")
    assert msg1["subject"] == "hi bob"
    assert msg1["thread_id"] == msg1["id"]  # root: thread_id == self id
    assert msg1["in_reply_to"] is None
    assert msg1["read_at"] is None

    # 2. Bob fetches inbox
    r = _signed_request(
        "GET", f"{server.base_url()}/v0/messages/inbox",
        agent_id=bob_id, keypair=bob_kp,
    )
    assert r.status_code == 200, r.text
    inbox = r.json()
    assert inbox["total"] == 1
    assert inbox["unread"] == 1
    assert inbox["items"][0]["id"] == msg1["id"]

    # 3. Bob reads the message
    r = _signed_request(
        "GET", f"{server.base_url()}/v0/messages/{msg1['id']}",
        agent_id=bob_id, keypair=bob_kp,
    )
    assert r.status_code == 200
    fetched = r.json()
    assert fetched["body"] == "wanna collab?"

    # 4. Bob replies (in_reply_to)
    body = b'{"recipient_id":"' + alice_id.encode() + b'",' b'"body":"sure!","in_reply_to":"' + msg1["id"].encode() + b'"}'
    r = _signed_request(
        "POST", f"{server.base_url()}/v0/messages",
        agent_id=bob_id, keypair=bob_kp, body=body,
    )
    assert r.status_code == 201, r.text
    msg2 = r.json()
    assert msg2["thread_id"] == msg1["thread_id"]  # same thread
    assert msg2["in_reply_to"] == msg1["id"]

    # 5. Alice sees the reply in her inbox
    r = _signed_request(
        "GET", f"{server.base_url()}/v0/messages/inbox",
        agent_id=alice_id, keypair=alice_kp,
    )
    assert r.status_code == 200
    alice_inbox = r.json()
    assert alice_inbox["total"] == 1
    assert alice_inbox["items"][0]["id"] == msg2["id"]

    # 6. Thread view: both messages
    r = _signed_request(
        "GET", f"{server.base_url()}/v0/threads/{msg1['thread_id']}",
        agent_id=alice_id, keypair=alice_kp,
    )
    assert r.status_code == 200
    thread = r.json()
    assert len(thread) == 2
    assert thread[0]["id"] == msg1["id"]
    assert thread[1]["id"] == msg2["id"]


def test_mark_read(server):
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    alice_id, alice_kp = _register(http, f"alice-{uuid.uuid4().hex[:6]}")
    bob_id, bob_kp = _register(http, f"bob-{uuid.uuid4().hex[:6]}")

    body = b'{"recipient_id":"' + bob_id.encode() + b'","body":"hi"}'
    r = _signed_request(
        "POST", f"{server.base_url()}/v0/messages",
        agent_id=alice_id, keypair=alice_kp, body=body,
    )
    msg_id = r.json()["id"]

    # mark read
    patch_body = b'{"action":"mark_read"}'
    r = _signed_request(
        "PATCH", f"{server.base_url()}/v0/messages/{msg_id}",
        agent_id=bob_id, keypair=bob_kp, body=patch_body,
    )
    assert r.status_code == 200, r.text
    assert r.json()["read_at"] is not None

    # inbox unread should be 0
    r = _signed_request(
        "GET", f"{server.base_url()}/v0/messages/inbox",
        agent_id=bob_id, keypair=bob_kp,
    )
    assert r.json()["unread"] == 0


def test_outbox(server):
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    alice_id, alice_kp = _register(http, f"alice-{uuid.uuid4().hex[:6]}")
    bob_id, _ = _register(http, f"bob-{uuid.uuid4().hex[:6]}")
    carol_id, _ = _register(http, f"carol-{uuid.uuid4().hex[:6]}")

    # Alice sends two messages
    for rid in (bob_id, carol_id):
        body = b'{"recipient_id":"' + rid.encode() + b'","body":"x"}'
        _signed_request(
            "POST", f"{server.base_url()}/v0/messages",
            agent_id=alice_id, keypair=alice_kp, body=body,
        )

    r = _signed_request(
        "GET", f"{server.base_url()}/v0/messages/outbox",
        agent_id=alice_id, keypair=alice_kp,
    )
    assert r.status_code == 200
    ob = r.json()
    assert ob["total"] == 2


def test_send_to_nonexistent_returns_404(server):
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    alice_id, alice_kp = _register(http, f"alice-{uuid.uuid4().hex[:6]}")
    body = b'{"recipient_id":"nope-xxx","body":"hi"}'
    r = _signed_request(
        "POST", f"{server.base_url()}/v0/messages",
        agent_id=alice_id, keypair=alice_kp, body=body,
    )
    assert r.status_code == 404


def test_send_to_self_rejected(server):
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    alice_id, alice_kp = _register(http, f"alice-{uuid.uuid4().hex[:6]}")
    body = b'{"recipient_id":"' + alice_id.encode() + b'","body":"talking to myself"}'
    r = _signed_request(
        "POST", f"{server.base_url()}/v0/messages",
        agent_id=alice_id, keypair=alice_kp, body=body,
    )
    assert r.status_code == 400


def test_stranger_cannot_read_message(server):
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    alice_id, alice_kp = _register(http, f"alice-{uuid.uuid4().hex[:6]}")
    bob_id, bob_kp = _register(http, f"bob-{uuid.uuid4().hex[:6]}")
    eve_id, eve_kp = _register(http, f"eve-{uuid.uuid4().hex[:6]}")

    body = b'{"recipient_id":"' + bob_id.encode() + b'","body":"private"}'
    r = _signed_request(
        "POST", f"{server.base_url()}/v0/messages",
        agent_id=alice_id, keypair=alice_kp, body=body,
    )
    msg_id = r.json()["id"]

    # Eve tries to read it
    r = _signed_request(
        "GET", f"{server.base_url()}/v0/messages/{msg_id}",
        agent_id=eve_id, keypair=eve_kp,
    )
    assert r.status_code == 403


def test_sender_cannot_mark_read(server):
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    alice_id, alice_kp = _register(http, f"alice-{uuid.uuid4().hex[:6]}")
    bob_id, bob_kp = _register(http, f"bob-{uuid.uuid4().hex[:6]}")

    body = b'{"recipient_id":"' + bob_id.encode() + b'","body":"hi"}'
    r = _signed_request(
        "POST", f"{server.base_url()}/v0/messages",
        agent_id=alice_id, keypair=alice_kp, body=body,
    )
    msg_id = r.json()["id"]

    # Alice (sender) tries to mark her own sent message as read — only recipient can
    r = _signed_request(
        "PATCH", f"{server.base_url()}/v0/messages/{msg_id}",
        agent_id=alice_id, keypair=alice_kp, body=b'{"action":"mark_read"}',
    )
    assert r.status_code == 403


def test_recipient_can_delete(server):
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    alice_id, alice_kp = _register(http, f"alice-{uuid.uuid4().hex[:6]}")
    bob_id, bob_kp = _register(http, f"bob-{uuid.uuid4().hex[:6]}")

    body = b'{"recipient_id":"' + bob_id.encode() + b'","body":"hi"}'
    r = _signed_request(
        "POST", f"{server.base_url()}/v0/messages",
        agent_id=alice_id, keypair=alice_kp, body=body,
    )
    msg_id = r.json()["id"]

    r = _signed_request(
        "DELETE", f"{server.base_url()}/v0/messages/{msg_id}",
        agent_id=bob_id, keypair=bob_kp,
    )
    assert r.status_code == 204

    r = _signed_request(
        "GET", f"{server.base_url()}/v0/messages/{msg_id}",
        agent_id=bob_id, keypair=bob_kp,
    )
    assert r.status_code == 404


def test_reply_requires_participant(server):
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    alice_id, alice_kp = _register(http, f"alice-{uuid.uuid4().hex[:6]}")
    bob_id, bob_kp = _register(http, f"bob-{uuid.uuid4().hex[:6]}")
    eve_id, eve_kp = _register(http, f"eve-{uuid.uuid4().hex[:6]}")

    # Alice -> Bob
    body = b'{"recipient_id":"' + bob_id.encode() + b'","body":"hi"}'
    r = _signed_request(
        "POST", f"{server.base_url()}/v0/messages",
        agent_id=alice_id, keypair=alice_kp, body=body,
    )
    msg_id = r.json()["id"]

    # Eve (stranger) tries to reply into the thread
    body = b'{"recipient_id":"' + alice_id.encode() + b'","body":"eavesdrop","in_reply_to":"' + msg_id.encode() + b'"}'
    r = _signed_request(
        "POST", f"{server.base_url()}/v0/messages",
        agent_id=eve_id, keypair=eve_kp, body=body,
    )
    assert r.status_code == 403
