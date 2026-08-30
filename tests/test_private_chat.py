"""End-to-end tests for the private chatroom API.

Covers the full flow:
- creator creates a room, generates an invite, sends it via mailbox
- a second agent redeems the invite and joins
- non-members cannot read messages
- members can read/post
- only creator can issue invites
- only creator can disband
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
    db = str(tmp_path / "pc_test.db")
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


# --- create / list / info (public) ---------------------------------------- #


def test_create_room(server):
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    alice_id, alice_kp = _register(http, f"alice-{uuid.uuid4().hex[:6]}")
    body = b'{"name":"secret-stuff","display_name":"Secret","description":"private"}'
    r = _signed("POST", f"{server.base_url()}/v0/private-chatrooms", agent_id=alice_id, kp=alice_kp, body=body)
    assert r.status_code == 201, r.text
    room = r.json()
    assert room["name"] == "secret-stuff"
    assert room["creator_name"].startswith("alice-")
    assert room["member_count"] == 1  # creator is auto-member


def test_list_rooms_public(server):
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    alice_id, alice_kp = _register(http, f"alice-{uuid.uuid4().hex[:6]}")
    body = b'{"name":"room-1"}'
    _signed("POST", f"{server.base_url()}/v0/private-chatrooms", agent_id=alice_id, kp=alice_kp, body=body)

    # No auth, public read
    r = httpx.get(f"{server.base_url()}/v0/private-chatrooms")
    assert r.status_code == 200
    listing = r.json()
    assert listing["total"] == 1
    assert listing["items"][0]["name"] == "room-1"


def test_info_public(server):
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    alice_id, alice_kp = _register(http, f"alice-{uuid.uuid4().hex[:6]}")
    body = b'{"name":"room-1","description":"d"}'
    _signed("POST", f"{server.base_url()}/v0/private-chatrooms", agent_id=alice_id, kp=alice_kp, body=body)

    r = httpx.get(f"{server.base_url()}/v0/private-chatrooms/room-1")
    assert r.status_code == 200
    info = r.json()
    assert info["name"] == "room-1"
    assert info["description"] == "d"


def test_create_duplicate_name_409(server):
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    alice_id, alice_kp = _register(http, f"alice-{uuid.uuid4().hex[:6]}")
    _signed(
        "POST", f"{server.base_url()}/v0/private-chatrooms",
        agent_id=alice_id, kp=alice_kp, body=b'{"name":"room-1"}',
    )
    r = _signed(
        "POST", f"{server.base_url()}/v0/private-chatrooms",
        agent_id=alice_id, kp=alice_kp, body=b'{"name":"room-1"}',
    )
    assert r.status_code == 409


# --- invite / join / leave ------------------------------------------------- #


def test_full_invite_flow(server):
    """Creator makes a room, issues an invite; a non-member redeems; non-members still can't read."""
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    alice_id, alice_kp = _register(http, f"alice-{uuid.uuid4().hex[:6]}")
    bob_id, bob_kp = _register(http, f"bob-{uuid.uuid4().hex[:6]}")
    eve_id, eve_kp = _register(http, f"eve-{uuid.uuid4().hex[:6]}")

    # Alice creates room
    r = _signed(
        "POST", f"{server.base_url()}/v0/private-chatrooms",
        agent_id=alice_id, kp=alice_kp, body=b'{"name":"secret-room","description":"d"}',
    )
    room = r.json()
    room_id = room["id"]

    # Alice issues invite
    r = _signed(
        "POST", f"{server.base_url()}/v0/private-chatrooms/{room_id}/invites",
        agent_id=alice_id, kp=alice_kp, body=b'{"max_uses":1}',
    )
    assert r.status_code == 201
    inv = r.json()
    code = inv["code"]
    assert inv["max_uses"] == 1
    assert inv["used_count"] == 0

    # Eve (non-member) tries to read messages — should 403
    r = _signed(
        "GET", f"{server.base_url()}/v0/private-chatrooms/{room_id}/messages",
        agent_id=eve_id, kp=eve_kp,
    )
    assert r.status_code == 403

    # Bob redeems the invite
    body = f'{{"code":"{code}"}}'.encode()
    r = _signed(
        "POST", f"{server.base_url()}/v0/private-chatrooms/{room_id}/join",
        agent_id=bob_id, kp=bob_kp, body=body,
    )
    assert r.status_code == 201, r.text
    joined = r.json()
    assert joined["member_count"] == 2

    # Invite is now exhausted — try with a different non-member
    carol_id, carol_kp = _register(http, f"carol-{uuid.uuid4().hex[:6]}")
    r = _signed(
        "POST", f"{server.base_url()}/v0/private-chatrooms/{room_id}/join",
        agent_id=carol_id, kp=carol_kp, body=body,
    )
    assert r.status_code == 410  # gone

    # Bob (now member) can post
    r = _signed(
        "POST", f"{server.base_url()}/v0/private-chatrooms/{room_id}/messages",
        agent_id=bob_id, kp=bob_kp, body=b'{"body":"hi from bob"}',
    )
    assert r.status_code == 201, r.text

    # Bob can read
    r = _signed(
        "GET", f"{server.base_url()}/v0/private-chatrooms/{room_id}/messages",
        agent_id=bob_id, kp=bob_kp,
    )
    assert r.status_code == 200
    msgs = r.json()
    assert msgs["total"] == 1
    assert msgs["items"][0]["body"] == "hi from bob"

    # Eve still can't read
    r = _signed(
        "GET", f"{server.base_url()}/v0/private-chatrooms/{room_id}/messages",
        agent_id=eve_id, kp=eve_kp,
    )
    assert r.status_code == 403


def test_only_creator_can_invite(server):
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    alice_id, alice_kp = _register(http, f"alice-{uuid.uuid4().hex[:6]}")
    bob_id, bob_kp = _register(http, f"bob-{uuid.uuid4().hex[:6]}")

    r = _signed(
        "POST", f"{server.base_url()}/v0/private-chatrooms",
        agent_id=alice_id, kp=alice_kp, body=b'{"name":"room-1"}',
    )
    room_id = r.json()["id"]

    # Alice invites Bob (works — creator)
    r = _signed(
        "POST", f"{server.base_url()}/v0/private-chatrooms/{room_id}/invites",
        agent_id=alice_id, kp=alice_kp, body=b'{"max_uses":1}',
    )
    inv_code = r.json()["code"]

    # Bob joins
    r = _signed(
        "POST", f"{server.base_url()}/v0/private-chatrooms/{room_id}/join",
        agent_id=bob_id, kp=bob_kp, body=f'{{"code":"{inv_code}"}}'.encode(),
    )
    assert r.status_code == 201

    # Bob (now member but not creator) tries to invite
    r = _signed(
        "POST", f"{server.base_url()}/v0/private-chatrooms/{room_id}/invites",
        agent_id=bob_id, kp=bob_kp, body=b'{"max_uses":1}',
    )
    assert r.status_code == 403


def test_invite_for_different_room_rejected(server):
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    alice_id, alice_kp = _register(http, f"alice-{uuid.uuid4().hex[:6]}")
    bob_id, bob_kp = _register(http, f"bob-{uuid.uuid4().hex[:6]}")

    # Two rooms
    r1 = _signed("POST", f"{server.base_url()}/v0/private-chatrooms", agent_id=alice_id, kp=alice_kp, body=b'{"name":"room-1"}').json()
    r2 = _signed("POST", f"{server.base_url()}/v0/private-chatrooms", agent_id=alice_id, kp=alice_kp, body=b'{"name":"room-2"}').json()

    # Invite for r1
    inv = _signed("POST", f"{server.base_url()}/v0/private-chatrooms/{r1['id']}/invites", agent_id=alice_id, kp=alice_kp, body=b'{"max_uses":1}').json()
    code = inv["code"]
    code = code

    # Bob tries to use r1's code to join r2
    r = _signed(
        "POST", f"{server.base_url()}/v0/private-chatrooms/{r2['id']}/join",
        agent_id=bob_id, kp=bob_kp, body=f'{{"code":"{code}"}}'.encode(),
    )
    assert r.status_code == 400


def test_leave(server):
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    alice_id, alice_kp = _register(http, f"alice-{uuid.uuid4().hex[:6]}")
    bob_id, bob_kp = _register(http, f"bob-{uuid.uuid4().hex[:6]}")
    r = _signed("POST", f"{server.base_url()}/v0/private-chatrooms", agent_id=alice_id, kp=alice_kp, body=b'{"name":"room-1"}').json()
    room_id = r["id"]
    inv = _signed("POST", f"{server.base_url()}/v0/private-chatrooms/{room_id}/invites", agent_id=alice_id, kp=alice_kp, body=b'{"max_uses":1}').json()
    code = inv["code"]
    _signed("POST", f"{server.base_url()}/v0/private-chatrooms/{room_id}/join", agent_id=bob_id, kp=bob_kp, body=f'{{"code":"{code}"}}'.encode())

    # Bob leaves
    r = _signed("POST", f"{server.base_url()}/v0/private-chatrooms/{room_id}/leave", agent_id=bob_id, kp=bob_kp)
    assert r.status_code == 204

    # Now Bob can't read
    r = _signed("GET", f"{server.base_url()}/v0/private-chatrooms/{room_id}/messages", agent_id=bob_id, kp=bob_kp)
    assert r.status_code == 403


def test_only_creator_can_disband(server):
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    alice_id, alice_kp = _register(http, f"alice-{uuid.uuid4().hex[:6]}")
    bob_id, bob_kp = _register(http, f"bob-{uuid.uuid4().hex[:6]}")
    r = _signed("POST", f"{server.base_url()}/v0/private-chatrooms", agent_id=alice_id, kp=alice_kp, body=b'{"name":"room-1"}').json()
    room_id = r["id"]
    inv = _signed("POST", f"{server.base_url()}/v0/private-chatrooms/{room_id}/invites", agent_id=alice_id, kp=alice_kp, body=b'{"max_uses":1}').json()
    code = inv["code"]
    _signed("POST", f"{server.base_url()}/v0/private-chatrooms/{room_id}/join", agent_id=bob_id, kp=bob_kp, body=f'{{"code":"{code}"}}'.encode())

    # Bob tries to disband — 403
    r = _signed("DELETE", f"{server.base_url()}/v0/private-chatrooms/{room_id}", agent_id=bob_id, kp=bob_kp)
    assert r.status_code == 403

    # Alice disbands — 204
    r = _signed("DELETE", f"{server.base_url()}/v0/private-chatrooms/{room_id}", agent_id=alice_id, kp=alice_kp)
    assert r.status_code == 204

    # Gone
    r = httpx.get(f"{server.base_url()}/v0/private-chatrooms/{room_id}")
    assert r.status_code == 404


def test_member_list_member_only(server):
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    alice_id, alice_kp = _register(http, f"alice-{uuid.uuid4().hex[:6]}")
    eve_id, eve_kp = _register(http, f"eve-{uuid.uuid4().hex[:6]}")
    r = _signed("POST", f"{server.base_url()}/v0/private-chatrooms", agent_id=alice_id, kp=alice_kp, body=b'{"name":"room-1"}').json()
    room_id = r["id"]

    # Alice is a member, Eve is not
    r = _signed("GET", f"{server.base_url()}/v0/private-chatrooms/{room_id}/members", agent_id=alice_id, kp=alice_kp)
    assert r.status_code == 200
    assert r.json()["total"] == 1

    r = _signed("GET", f"{server.base_url()}/v0/private-chatrooms/{room_id}/members", agent_id=eve_id, kp=eve_kp)
    assert r.status_code == 403


def test_sender_can_delete_message(server):
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    alice_id, alice_kp = _register(http, f"alice-{uuid.uuid4().hex[:6]}")
    bob_id, bob_kp = _register(http, f"bob-{uuid.uuid4().hex[:6]}")
    r = _signed("POST", f"{server.base_url()}/v0/private-chatrooms", agent_id=alice_id, kp=alice_kp, body=b'{"name":"room-1"}').json()
    room_id = r["id"]
    inv = _signed("POST", f"{server.base_url()}/v0/private-chatrooms/{room_id}/invites", agent_id=alice_id, kp=alice_kp, body=b'{"max_uses":1}').json()
    code = inv["code"]
    _signed("POST", f"{server.base_url()}/v0/private-chatrooms/{room_id}/join", agent_id=bob_id, kp=bob_kp, body=f'{{"code":"{code}"}}'.encode())

    msg = _signed("POST", f"{server.base_url()}/v0/private-chatrooms/{room_id}/messages", agent_id=bob_id, kp=bob_kp, body=b'{"body":"oops"}').json()
    msg_id = msg["id"]

    # Alice (member but not sender) tries to delete — 403
    r = _signed("DELETE", f"{server.base_url()}/v0/private-chatrooms/{room_id}/messages/{msg_id}", agent_id=alice_id, kp=alice_kp)
    assert r.status_code == 403

    # Bob (sender) deletes — 204
    r = _signed("DELETE", f"{server.base_url()}/v0/private-chatrooms/{room_id}/messages/{msg_id}", agent_id=bob_id, kp=bob_kp)
    assert r.status_code == 204

    # Gone
    r = _signed("GET", f"{server.base_url()}/v0/private-chatrooms/{room_id}/messages/{msg_id}", agent_id=bob_id, kp=bob_kp)
    assert r.status_code == 404


def test_invite_with_max_uses(server):
    """One invite code with max_uses=3 should let 3 different agents join."""
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    alice_id, alice_kp = _register(http, f"alice-{uuid.uuid4().hex[:6]}")
    r = _signed("POST", f"{server.base_url()}/v0/private-chatrooms", agent_id=alice_id, kp=alice_kp, body=b'{"name":"room-1"}').json()
    room_id = r["id"]
    inv = _signed("POST", f"{server.base_url()}/v0/private-chatrooms/{room_id}/invites", agent_id=alice_id, kp=alice_kp, body=b'{"max_uses":3}').json()
    code = inv["code"]
    code = code

    for i in range(3):
        kp_id, kp = _register(http, f"u{i}-{uuid.uuid4().hex[:6]}")
        r = _signed("POST", f"{server.base_url()}/v0/private-chatrooms/{room_id}/join", agent_id=kp_id, kp=kp, body=f'{{"code":"{code}"}}'.encode())
        assert r.status_code == 201, f"join {i}: {r.text}"

    # 4th attempt fails
    kp4_id, kp4 = _register(http, f"u4-{uuid.uuid4().hex[:6]}")
    r = _signed("POST", f"{server.base_url()}/v0/private-chatrooms/{room_id}/join", agent_id=kp4_id, kp=kp4, body=f'{{"code":"{code}"}}'.encode())
    assert r.status_code == 410


def test_invite_list_only_creator(server):
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    alice_id, alice_kp = _register(http, f"alice-{uuid.uuid4().hex[:6]}")
    bob_id, bob_kp = _register(http, f"bob-{uuid.uuid4().hex[:6]}")
    r = _signed("POST", f"{server.base_url()}/v0/private-chatrooms", agent_id=alice_id, kp=alice_kp, body=b'{"name":"room-1"}').json()
    room_id = r["id"]
    inv = _signed("POST", f"{server.base_url()}/v0/private-chatrooms/{room_id}/invites", agent_id=alice_id, kp=alice_kp, body=b'{"max_uses":1}').json()
    code = inv["code"]
    _signed("POST", f"{server.base_url()}/v0/private-chatrooms/{room_id}/join", agent_id=bob_id, kp=bob_kp, body=f'{{"code":"{code}"}}'.encode())

    # Bob (member but not creator) lists invites — 403
    r = _signed("GET", f"{server.base_url()}/v0/private-chatrooms/{room_id}/invites", agent_id=bob_id, kp=bob_kp)
    assert r.status_code == 403

    # Alice lists — works
    r = _signed("GET", f"{server.base_url()}/v0/private-chatrooms/{room_id}/invites", agent_id=alice_id, kp=alice_kp)
    assert r.status_code == 200
    assert len(r.json()) >= 1


def test_search_by_creator(server):
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    alice_id, alice_kp = _register(http, f"alice-{uuid.uuid4().hex[:6]}")
    _signed("POST", f"{server.base_url()}/v0/private-chatrooms", agent_id=alice_id, kp=alice_kp, body=b'{"name":"alpha-room"}')
    _signed("POST", f"{server.base_url()}/v0/private-chatrooms", agent_id=alice_id, kp=alice_kp, body=b'{"name":"beta-room"}')

    r = httpx.get(f"{server.base_url()}/v0/private-chatrooms", params={"creator": alice_id, "limit": 10})
    assert r.status_code == 200
    assert r.json()["total"] == 2

    r = httpx.get(f"{server.base_url()}/v0/private-chatrooms?q=alpha")
    assert r.status_code == 200
    assert r.json()["total"] == 1
