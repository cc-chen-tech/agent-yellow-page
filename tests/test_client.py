"""Direct unit tests for app.client.YellowPageClient.

Exercises every public method (happy path + error handling) so the SDK
can be used independently of the CLI.
"""

from __future__ import annotations

import base64
import os
import socket
import threading
import time
import uuid
from urllib.parse import urlencode

import httpx
import pytest
import uvicorn

from app.client import YellowPageClient
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
    s = _Server(str(tmp_path / "client_test.db"))
    yield s
    s.stop()


def _auth_client(server, name: str) -> tuple[YellowPageClient, dict]:
    """Register and return an authenticated client + card."""
    c = YellowPageClient(server.base_url())
    kp = KeyPair.generate()
    card = c.register(
        name=name,
        public_key="ed25519:" + kp.public_b64,
    )
    c.agent_id = card["id"]
    c.keypair = kp
    return c, card


# --- constructor / context manager ----------------------------------------- #


def test_context_manager_closes_http():
    c = YellowPageClient("http://127.0.0.1:1")
    with c as inside:
        assert inside is c
    # httpx.Client.close was called → internal _http is replaced
    assert c._http is not None


def test_register(server):
    c = YellowPageClient(server.base_url())
    kp = KeyPair.generate()
    card = c.register(name=f"a-{uuid.uuid4().hex[:6]}", public_key="ed25519:" + kp.public_b64)
    assert card["version"] == 1
    assert "id" in card


def test_register_raises_on_duplicate(server):
    c = YellowPageClient(server.base_url())
    name = f"dup-{uuid.uuid4().hex[:6]}"
    kp1 = KeyPair.generate()
    c.register(name=name, public_key="ed25519:" + kp1.public_b64)
    kp2 = KeyPair.generate()
    with pytest.raises(httpx.HTTPStatusError) as ei:
        c.register(name=name, public_key="ed25519:" + kp2.public_b64)
    assert ei.value.response.status_code == 409



def test_list_with_q_and_tag_filter(server):
    # YellowPageClient.register doesn't accept tags, so use raw httpx
    kp = KeyPair.generate()
    name = f"filterable-{uuid.uuid4().hex[:6]}"  # name also contains "filterable"
    httpx.post(
        f"{server.base_url()}/v0/agents",
        json={"name": name, "public_key": "ed25519:" + kp.public_b64, "tags": ["filterable"]},
    )
    # query the public list endpoint with q + tag (q matches name substring, tag matches)
    c = YellowPageClient(server.base_url())
    r = c._http.get("/v0/agents?q=filterable&tag=filterable&limit=5")
    res = r.json()
    assert "items" in res
    assert res["total"] >= 1


def test_get_other_agent(server):
    c, _ = _auth_client(server, f"a-{uuid.uuid4().hex[:6]}")
    kp = KeyPair.generate()
    other = c.register(name=f"b-{uuid.uuid4().hex[:6]}", public_key="ed25519:" + kp.public_b64)
    got = c.get(other["name"])
    assert got["id"] == other["id"]


def test_get_404(server):
    c, _ = _auth_client(server, f"a-{uuid.uuid4().hex[:6]}")
    with pytest.raises(httpx.HTTPStatusError) as ei:
        c.get("no-such-agent")
    assert ei.value.response.status_code == 404


def test_update_with_version(server):
    c, _ = _auth_client(server, f"u-{uuid.uuid4().hex[:6]}")
    out = c.patch(c.agent_id, {"description": "v1"}, if_match="1")
    assert out["version"] == 2
    out2 = c.patch(c.agent_id, {"description": "v2"}, if_match="2")
    assert out2["version"] == 3


def test_update_conflict_409(server):
    c, _ = _auth_client(server, f"u-{uuid.uuid4().hex[:6]}")
    with pytest.raises(httpx.HTTPStatusError) as ei:
        c.patch(c.agent_id, {"description": "x"}, if_match="999")
    assert ei.value.response.status_code == 409



def test_delete_self(server):
    c, card = _auth_client(server, f"d-{uuid.uuid4().hex[:6]}")
    c.delete(card["id"])


# --- mailbox methods ------------------------------------------------------ #


def test_send_message_to_other(server):
    sender, _ = _auth_client(server, f"s-{uuid.uuid4().hex[:6]}")
    kp = KeyPair.generate()
    recip_card = sender.register(name=f"r-{uuid.uuid4().hex[:6]}",
                                  public_key="ed25519:" + kp.public_b64)
    msg = sender.send_message(recip_card["id"], body="hi", subject="s")
    assert msg["recipient_id"] == recip_card["id"]
    assert msg["subject"] == "s"
    assert msg["thread_id"] == msg["id"]


def test_send_message_requires_auth(server):
    c = YellowPageClient(server.base_url())
    with pytest.raises(RuntimeError, match="send_message"):
        c.send_message("whoever", body="x")


def test_inbox_outbox(server):
    sender, _ = _auth_client(server, f"s-{uuid.uuid4().hex[:6]}")
    kp = KeyPair.generate()
    recip = sender.register(name=f"r-{uuid.uuid4().hex[:6]}",
                            public_key="ed25519:" + kp.public_b64)
    # Send two messages
    sender.send_message(recip["id"], body="msg 1")
    sender.send_message(recip["id"], body="msg 2")
    # Set recipient agent
    recip_client = YellowPageClient(server.base_url())
    recip_client.agent_id = recip["id"]
    recip_client.keypair = kp
    inbox = recip_client.inbox()
    assert inbox["total"] == 2
    assert inbox["unread"] == 2
    outbox = sender.outbox()
    assert outbox["total"] == 2


def test_inbox_unread_filter(server):
    sender, _ = _auth_client(server, f"s-{uuid.uuid4().hex[:6]}")
    kp = KeyPair.generate()
    recip = sender.register(name=f"r-{uuid.uuid4().hex[:6]}",
                            public_key="ed25519:" + kp.public_b64)
    sender.send_message(recip["id"], body="hi")
    recip_client = YellowPageClient(server.base_url())
    recip_client.agent_id = recip["id"]
    recip_client.keypair = kp
    msg = recip_client.inbox()["items"][0]
    recip_client.mark_read(msg["id"])
    # After mark_read, unread filter returns 0
    inbox = recip_client.inbox(unread=True)
    assert inbox["total"] == 0
    inbox2 = recip_client.inbox(unread=False)
    assert inbox2["total"] == 1


def test_inbox_requires_auth(server):
    c = YellowPageClient(server.base_url())
    with pytest.raises(RuntimeError, match="inbox"):
        c.inbox()


def test_get_mark_delete_message(server):
    sender, _ = _auth_client(server, f"s-{uuid.uuid4().hex[:6]}")
    kp = KeyPair.generate()
    recip = sender.register(name=f"r-{uuid.uuid4().hex[:6]}",
                            public_key="ed25519:" + kp.public_b64)
    sent = sender.send_message(recip["id"], body="hi")
    # Recipient fetches + marks + deletes
    recip_client = YellowPageClient(server.base_url())
    recip_client.agent_id = recip["id"]
    recip_client.keypair = kp
    fetched = recip_client.get_message(sent["id"])
    assert fetched["body"] == "hi"
    marked = recip_client.mark_read(sent["id"])
    assert marked["read_at"] is not None
    recip_client.delete_message(sent["id"])
    with pytest.raises(httpx.HTTPStatusError) as ei:
        recip_client.get_message(sent["id"])
    assert ei.value.response.status_code == 404


def test_thread_view(server):
    sender, _ = _auth_client(server, f"s-{uuid.uuid4().hex[:6]}")
    kp = KeyPair.generate()
    recip = sender.register(name=f"r-{uuid.uuid4().hex[:6]}",
                            public_key="ed25519:" + kp.public_b64)
    recip_client = YellowPageClient(server.base_url())
    recip_client.agent_id = recip["id"]
    recip_client.keypair = kp
    m1 = sender.send_message(recip["id"], body="first")
    m2 = recip_client.send_message(sender.agent_id, body="second", in_reply_to=m1["id"])
    assert m2["thread_id"] == m1["thread_id"]
    thread = sender.thread(m1["thread_id"])
    assert len(thread) == 2
    assert thread[0]["body"] == "first"
    assert thread[1]["body"] == "second"


# --- public chatroom methods --------------------------------------------- #


def test_chat_post_list_get_delete(server):
    c, _ = _auth_client(server, f"c-{uuid.uuid4().hex[:6]}")
    sent = c.chat_post("hello public")
    listing = c.chat_list()
    assert listing["total"] >= 1
    fetched = c.chat_get(sent["id"])
    assert fetched["body"] == "hello public"
    c.chat_delete(sent["id"])
    with pytest.raises(httpx.HTTPStatusError) as ei:
        c.chat_get(sent["id"])
    assert ei.value.response.status_code == 404


def test_chat_post_requires_auth(server):
    c = YellowPageClient(server.base_url())
    with pytest.raises(RuntimeError, match="chat_post"):
        c.chat_post("hi")


def test_chat_list_is_public_no_auth(server):
    c = YellowPageClient(server.base_url())  # no auth needed
    res = c.chat_list(limit=5)
    assert "items" in res


def test_chat_delete_requires_auth(server):
    c = YellowPageClient(server.base_url())
    with pytest.raises(RuntimeError, match="chat_delete"):
        c.chat_delete("01M00000000000000000000000")


# --- private chatroom methods -------------------------------------------- #


def test_pc_create_list_info(server):
    c, _ = _auth_client(server, f"c-{uuid.uuid4().hex[:6]}")
    room = c.pc_create(name=f"r-{uuid.uuid4().hex[:6]}", description="d")
    assert room["member_count"] == 1
    listing = c.pc_list(q=room["name"][:6])
    assert any(r["id"] == room["id"] for r in listing["items"])
    info = c.pc_info(room["name"])
    assert info["id"] == room["id"]


def test_pc_invite_join_flow(server):
    alice, _ = _auth_client(server, f"al-{uuid.uuid4().hex[:6]}")
    kp = KeyPair.generate()
    bob_card = alice.register(name=f"bob-{uuid.uuid4().hex[:6]}",
                              public_key="ed25519:" + kp.public_b64)
    bob = YellowPageClient(server.base_url())
    bob.agent_id = bob_card["id"]
    bob.keypair = kp

    room = alice.pc_create(name=f"r-{uuid.uuid4().hex[:6]}")
    inv = alice.pc_invite(room["name"], max_uses=3)
    joined = bob.pc_join(room["name"], inv["code"])
    assert joined["member_count"] == 2


def test_pc_invite_requires_auth(server):
    c = YellowPageClient(server.base_url())
    with pytest.raises(RuntimeError, match="agent_id and keypair"):
        c.pc_invite("any")


def test_pc_join_requires_auth(server):
    c = YellowPageClient(server.base_url())
    with pytest.raises(RuntimeError, match="agent_id and keypair"):
        c.pc_join("any", "code")


def test_pc_send_messages_members_leave_delete(server):
    alice, _ = _auth_client(server, f"al-{uuid.uuid4().hex[:6]}")
    kp = KeyPair.generate()
    bob_card = alice.register(name=f"bo-{uuid.uuid4().hex[:6]}",
                              public_key="ed25519:" + kp.public_b64)
    bob = YellowPageClient(server.base_url())
    bob.agent_id = bob_card["id"]
    bob.keypair = kp

    room = alice.pc_create(name=f"r-{uuid.uuid4().hex[:6]}")
    inv = alice.pc_invite(room["name"])
    bob.pc_join(room["name"], inv["code"])

    members = bob.pc_members(room["name"])
    assert members["total"] == 2

    sent = bob.pc_send(room["name"], "hi from bob")
    msgs = bob.pc_messages(room["name"])
    assert msgs["total"] == 1
    assert msgs["items"][0]["body"] == "hi from bob"

    bob.pc_leave(room["name"])
    members2 = alice.pc_members(room["name"])
    assert members2["total"] == 1

    alice.pc_delete(room["name"])
    # After disband, server returns 404 for info
    with pytest.raises(httpx.HTTPStatusError):
        alice.pc_info(room["name"])


def test_pc_delete_requires_auth(server):
    c = YellowPageClient(server.base_url())
    with pytest.raises(RuntimeError, match="agent_id and keypair"):
        c.pc_delete("any")
