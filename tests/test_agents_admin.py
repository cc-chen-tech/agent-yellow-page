"""Coverage tests for app/routes/agents.py admin and edge paths.

Hits the missing-line branches:
- public_key conflict on register
- PUT /agents/{id} full update + version conflict
- PATCH /agents/{id} version conflict
- If-Match parsing failure
- Signature mismatch (signed by different agent)
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
    s = _Server(str(tmp_path / "admin_test.db"))
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


def _signed(method, url, *, agent_id, kp, body=b"", if_match=None):
    """Manually sign and send a request."""
    from urllib.parse import urlparse
    p = urlparse(url)
    path = p.path
    ts = int(time.time())
    msg = canonical_request(ts, method, path, body)
    sig = kp.sign(msg)
    nonce = base64.b64encode(os.urandom(16)).decode("ascii")
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


# --- public_key conflict (line 84) ----------------------------------------- #


def test_register_rejects_duplicate_public_key(server):
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    kp = KeyPair.generate()
    pk = "ed25519:" + kp.public_b64
    # First registration succeeds
    r1 = http.post(
        "/v0/agents",
        json={"name": f"a-{uuid.uuid4().hex[:6]}", "public_key": pk},
    )
    assert r1.status_code == 201
    # Second registration with the SAME public_key but different name → 409
    r2 = http.post(
        "/v0/agents",
        json={"name": f"b-{uuid.uuid4().hex[:6]}", "public_key": pk},
    )
    assert r2.status_code == 409
    body = r2.json()
    # detail should mention public_key
    assert "public_key" in body.get("detail", {}).get("message", "").lower()


# --- PUT full + PATCH version conflicts ------------------------------------ #


def test_put_full_409_with_wrong_if_match(server):
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    agent_id, kp = _register(http, f"a-{uuid.uuid4().hex[:6]}")
    # bump version first
    r = _signed("PATCH", f"{server.base_url()}/v0/agents/{agent_id}",
                agent_id=agent_id, kp=kp, body=b'{"description":"v2"}')
    assert r.status_code == 200
    # Now PUT with stale if_match=1
    put_body = b'{"name":"full-a","public_key":"ed25519:' + kp.public_b64.encode() + b'","display_name":"X"}'
    r2 = _signed("PUT", f"{server.base_url()}/v0/agents/{agent_id}",
                 agent_id=agent_id, kp=kp, body=put_body, if_match='"1"')
    assert r2.status_code == 409
    body = r2.json()
    assert "version" in body.get("detail", {}).get("message", "").lower()


def test_patch_409_with_stale_if_match(server):
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    agent_id, kp = _register(http, f"a-{uuid.uuid4().hex[:6]}")
    # Update once to bump to v2
    r = _signed("PATCH", f"{server.base_url()}/v0/agents/{agent_id}",
                agent_id=agent_id, kp=kp, body=b'{"description":"v2"}')
    assert r.status_code == 200
    # PATCH with if_match=1 (stale) → 409
    r2 = _signed("PATCH", f"{server.base_url()}/v0/agents/{agent_id}",
                 agent_id=agent_id, kp=kp, body=b'{"description":"v3"}',
                 if_match='"1"')
    assert r2.status_code == 409


def test_get_404_for_unknown_agent(server):
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    r = http.get("/v0/agents/no-such-agent-xyz123")
    assert r.status_code == 404


def test_put_400_for_invalid_if_match(server):
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    agent_id, kp = _register(http, f"a-{uuid.uuid4().hex[:6]}")
    put_body = b'{"name":"full-a","public_key":"ed25519:' + kp.public_b64.encode() + b'"}'
    r = _signed("PUT", f"{server.base_url()}/v0/agents/{agent_id}",
                agent_id=agent_id, kp=kp, body=put_body, if_match="not-a-number")
    assert r.status_code == 400
    body = r.json()
    assert "If-Match" in body.get("detail", {}).get("message", "")


def test_put_400_for_signature_id_mismatch(server):
    """Alice signs a PUT targeting Bob's id → 400."""
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    alice_id, alice_kp = _register(http, f"alice-{uuid.uuid4().hex[:6]}")
    bob_id, bob_kp = _register(http, f"bob-{uuid.uuid4().hex[:6]}")
    put_body = b'{"name":"bob","public_key":"ed25519:' + bob_kp.public_b64.encode() + b'"}'
    r = _signed("PUT", f"{server.base_url()}/v0/agents/{bob_id}",
                agent_id=alice_id, kp=alice_kp, body=put_body)
    assert r.status_code == 400
    body = r.json()
    assert "signature" in body.get("detail", {}).get("message", "").lower() or "path" in body.get("detail", {}).get("message", "").lower()


def test_put_full_replaces_agent(server):
    """PUT with matching version replaces all fields."""
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    agent_id, kp = _register(http, f"a-{uuid.uuid4().hex[:6]}")
    put_body = (
        b'{"name":"full-rep","public_key":"ed25519:' + kp.public_b64.encode() +
        b'","display_name":"NewName","description":"replaced","tags":["a","b"]}'
    )
    r = _signed("PUT", f"{server.base_url()}/v0/agents/{agent_id}",
                agent_id=agent_id, kp=kp, body=put_body, if_match='"1"')
    assert r.status_code == 200, r.text
    updated = r.json()
    assert updated["display_name"] == "NewName"
    assert updated["description"] == "replaced"
    assert updated["version"] == 2
    assert updated["tags"] == ["a", "b"]


def test_register_duplicate_field_returns_409(server):
    """If both name and public_key collide, server returns 409 (name wins)."""
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    name = f"dup-{uuid.uuid4().hex[:6]}"
    kp = KeyPair.generate()
    pk = "ed25519:" + kp.public_b64
    r1 = http.post("/v0/agents", json={"name": name, "public_key": pk})
    assert r1.status_code == 201
    # same name AND same key → name conflict wins (ConflictError with field=name)
    r2 = http.post("/v0/agents", json={"name": name, "public_key": pk})
    assert r2.status_code == 409
    assert "name" in r2.json()["detail"]["message"].lower()


# --- find_by_id_or_name edge cases ---------------------------------------- #


def test_get_accepts_ulid_or_name(server):
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    name = f"abc-{uuid.uuid4().hex[:6]}"
    agent_id, _ = _register(http, name)
    # By id
    r1 = http.get(f"/v0/agents/{agent_id}")
    assert r1.status_code == 200
    assert r1.json()["id"] == agent_id
    # By name
    r2 = http.get(f"/v0/agents/{name}")
    assert r2.status_code == 200
    assert r2.json()["id"] == agent_id


def test_challenge_for_unknown_agent_returns_404(server):
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    r = http.get("/v0/agents/no-such-agent/challenge")
    assert r.status_code == 404


# --- DELETE errors --------------------------------------------------------- #


def test_delete_400_for_signature_id_mismatch(server):
    """Alice signs a DELETE targeting Bob's id → 400."""
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    alice_id, alice_kp = _register(http, f"alice-{uuid.uuid4().hex[:6]}")
    bob_id, bob_kp = _register(http, f"bob-{uuid.uuid4().hex[:6]}")
    r = _signed("DELETE", f"{server.base_url()}/v0/agents/{bob_id}",
                agent_id=alice_id, kp=alice_kp)
    assert r.status_code == 400
    body = r.json()
    assert "signature" in body.get("detail", {}).get("message", "").lower() or "path" in body.get("detail", {}).get("message", "").lower()


# --- bulk endpoint (disabled by default) ----------------------------------- #


def test_bulk_import_disabled_returns_403(server):
    """Without YELLOWPAGE_ALLOW_BULK=1, /_bulk always 403s."""
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    kp = KeyPair.generate()
    r = http.post(
        "/v0/agents/_bulk",
        json=[{
            "name": f"bulk-{uuid.uuid4().hex[:6]}",
            "public_key": "ed25519:" + kp.public_b64,
        }],
    )
    assert r.status_code == 403
    assert r.json()["detail"]["error"] == "forbidden"


# --- agent_id not found edge cases for put/patch -------------------------- #


def test_put_404_for_unknown_agent_with_valid_signature(server):
    """Sign a PUT for a never-existed agent — signer check fails (401), not 404.

    The auth dependency runs before the route handler, so the path is short.
    """
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    fake_id = "01M00000000000000000000000"
    fake_kp = KeyPair.generate()
    r = _signed("PUT", f"{server.base_url()}/v0/agents/{fake_id}",
                agent_id=fake_id, kp=fake_kp, body=b'{"name":"x","public_key":"ed25519:dummy"}')
    # NotFound path in storage.update_full is unreachable because the auth
    # dependency 401s first. We accept either 401 or 404 here — the spec
    # is that one of them surfaces.
    assert r.status_code in (401, 404)


def test_delete_204_for_self(server):
    http = httpx.Client(base_url=server.base_url(), timeout=5.0)
    agent_id, kp = _register(http, f"d-self-{uuid.uuid4().hex[:6]}")
    r = _signed("DELETE", f"{server.base_url()}/v0/agents/{agent_id}",
                agent_id=agent_id, kp=kp)
    assert r.status_code == 204
    # Subsequent GET → 404
    r2 = http.get(f"/v0/agents/{agent_id}")
    assert r2.status_code == 404
