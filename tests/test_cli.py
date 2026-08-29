"""CLI tests — run the Click commands against a real FastAPI server in-process.

Spins up uvicorn on a free port, points the CLI at it, then shuts down.
"""

from __future__ import annotations

import base64
import json
import os
import re
import socket
import threading
import time
import uuid

import httpx
import pytest
import uvicorn
from click.testing import CliRunner

from app.cli import cli
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
def env(tmp_path, monkeypatch):
    db = str(tmp_path / "cli_test.db")
    cfg = str(tmp_path / "config.json")
    server = _Server(db)
    monkeypatch.setenv("YELLOWPAGE_SERVER", server.base_url())
    monkeypatch.setenv("AGENT_YP_CONFIG", cfg)
    yield server, cfg
    server.stop()


def _register_other(server, name_prefix: str = "x"):
    """Register a second agent via the API; return (id, name, keypair)."""
    name = f"{name_prefix}-{uuid.uuid4().hex[:8]}"
    kp = KeyPair.generate()
    r = httpx.post(
        f"{server.base_url()}/v0/agents",
        json={"name": name, "public_key": "ed25519:" + kp.public_b64},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"], name, kp


def _write_config(cfg_path: str, server, agent_id: str, name: str, kp: KeyPair) -> None:
    """Write a config file directly (bypassing `init`)."""
    cfg = {
        "server": server.base_url(),
        "agent_id": agent_id,
        "name": name,
        "private_key_raw_b64": base64.b64encode(kp.private_raw).decode("ascii"),
    }
    os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
    with open(cfg_path, "w") as f:
        json.dump(cfg, f)


# ============================================================================
# init / whoami / get / list
# ============================================================================


def test_init_registers_and_saves_config(env):
    server, cfg_path = env
    runner = CliRunner()
    name = f"cli-{uuid.uuid4().hex[:8]}"
    result = runner.invoke(
        cli,
        [
            "init", "--name", name, "--display-name", "CLI Test",
            "--tag", "test", "--tag", "cli",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "registered" in result.output
    assert os.path.exists(cfg_path)

    data = json.loads(open(cfg_path).read())
    assert data["name"] == name
    assert data["server"] == server.base_url()
    assert len(data["private_key_raw_b64"]) > 0

    r = httpx.get(f"{server.base_url()}/v0/agents/{name}")
    assert r.status_code == 200
    body = r.json()
    assert body["display_name"] == "CLI Test"
    assert body["tags"] == ["test", "cli"]


def test_whoami_after_init(env):
    server, _cfg = env
    runner = CliRunner()
    name = f"wm-{uuid.uuid4().hex[:8]}"
    r1 = runner.invoke(cli, ["init", "--name", name, "--description", "hello"])
    assert r1.exit_code == 0, r1.output
    r2 = runner.invoke(cli, ["whoami"])
    assert r2.exit_code == 0, r2.output
    body = json.loads(r2.output)
    assert body["name"] == name
    assert body["description"] == "hello"


def test_whoami_without_init_fails(env):
    _server, _cfg = env
    runner = CliRunner()
    result = runner.invoke(cli, ["whoami"])
    assert result.exit_code != 0
    assert "not initialized" in result.output


def test_get_other_agent(env):
    server, _cfg = env
    name = f"other-{uuid.uuid4().hex[:8]}"
    _register_other(server, "o")
    # actually reuse above? need a known name; redo
    kp = KeyPair.generate()
    httpx.post(
        f"{server.base_url()}/v0/agents",
        json={"name": name, "public_key": "ed25519:" + kp.public_b64},
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["get", name])
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["name"] == name


def test_list_with_filters(env):
    server, _ = env
    for i in range(3):
        kp = KeyPair.generate()
        httpx.post(
            f"{server.base_url()}/v0/agents",
            json={
                "name": f"l-cli-{i}-{uuid.uuid4().hex[:6]}",
                "public_key": "ed25519:" + kp.public_b64,
                "tags": ["cli-test-tag"],
                "display_name": f"Bot {i}",
            },
        )
    runner = CliRunner()
    result = runner.invoke(cli, ["list", "--tag", "cli-test-tag", "--limit", "20"])
    assert result.exit_code == 0, result.output
    assert "total:" in result.output
    assert "cli-test-tag" in result.output

    result2 = runner.invoke(cli, ["list", "--q", "Bot", "--json"])
    assert result2.exit_code == 0, result2.output
    parsed = json.loads(result2.output)
    assert parsed["total"] >= 3


def test_list_json_output(env):
    _server, _ = env
    kp = KeyPair.generate()
    httpx.post(
        f"{_server.base_url()}/v0/agents",
        json={"name": f"j-{uuid.uuid4().hex[:8]}", "public_key": "ed25519:" + kp.public_b64},
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["list", "--json"])
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert "items" in parsed
    assert "total" in parsed


def test_update_patches_fields(env):
    server, _cfg = env
    runner = CliRunner()
    name = f"up-{uuid.uuid4().hex[:8]}"
    r1 = runner.invoke(cli, ["init", "--name", name, "--description", "v1", "--tag", "a"])
    assert r1.exit_code == 0, r1.output
    result = runner.invoke(
        cli,
        ["update", "--description", "v2", "--add-tag", "b", "--remove-tag", "a"],
    )
    assert result.exit_code == 0, result.output
    assert "version=2" in result.output

    r = httpx.get(f"{server.base_url()}/v0/agents/{name}")
    body = r.json()
    assert body["version"] == 2
    assert body["description"] == "v2"
    assert body["tags"] == ["b"]


def test_update_with_metadata_json(env):
    server, _cfg = env
    runner = CliRunner()
    name = f"meta-{uuid.uuid4().hex[:8]}"
    runner.invoke(cli, ["init", "--name", name])
    meta = '{"model": "claude", "v": 1}'
    result = runner.invoke(cli, ["update", "--metadata", meta])
    assert result.exit_code == 0, result.output
    r = httpx.get(f"{server.base_url()}/v0/agents/{name}")
    assert r.json()["metadata"] == {"model": "claude", "v": 1}


def test_delete_removes_agent_and_config(env):
    server, cfg_path = env
    runner = CliRunner()
    name = f"del-{uuid.uuid4().hex[:8]}"
    runner.invoke(cli, ["init", "--name", name])
    assert os.path.exists(cfg_path)
    result = runner.invoke(cli, ["delete", "-y"])
    assert result.exit_code == 0, result.output
    assert "deleted" in result.output
    assert not os.path.exists(cfg_path)
    r = httpx.get(f"{server.base_url()}/v0/agents/{name}")
    assert r.status_code == 404


def test_sign_returns_signature(env):
    server, _cfg = env
    runner = CliRunner()
    name = f"sg-{uuid.uuid4().hex[:8]}"
    r1 = runner.invoke(cli, ["init", "--name", name])
    assert r1.exit_code == 0, r1.output
    result = runner.invoke(cli, ["sign"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["target"] == name
    assert "challenge" in payload
    assert "signature" in payload
    assert payload["public_key"].startswith("ed25519:")


# ============================================================================
# name check / pre-flight
# ============================================================================


def test_check_name_available(env):
    runner = CliRunner()
    result = runner.invoke(cli, ["check-name", f"free-{uuid.uuid4().hex[:8]}"])
    assert result.exit_code == 0
    assert json.loads(result.output)["available"] is True


def test_check_name_taken(env):
    server, _ = env
    kp = KeyPair.generate()
    name = f"taken-{uuid.uuid4().hex[:8]}"
    httpx.post(
        f"{server.base_url()}/v0/agents",
        json={"name": name, "public_key": "ed25519:" + kp.public_b64},
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["check-name", name])
    assert result.exit_code == 1
    body = json.loads(result.output)
    assert body["available"] is False
    assert "taken" in body["reason"]


def test_init_rejects_taken_name_without_force(env):
    server, cfg_path = env
    kp = KeyPair.generate()
    name = f"dup-{uuid.uuid4().hex[:8]}"
    httpx.post(
        f"{server.base_url()}/v0/agents",
        json={"name": name, "public_key": "ed25519:" + kp.public_b64},
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["init", "--name", name])
    assert result.exit_code != 0
    assert "already taken" in result.output
    assert not os.path.exists(cfg_path)
    r = httpx.get(f"{server.base_url()}/v0/agents/{name}")
    assert r.status_code == 200
    assert r.json()["version"] == 1  # not bumped


def test_init_force_attempts_despite_taken_name(env):
    server, cfg_path = env
    kp = KeyPair.generate()
    name = f"force-{uuid.uuid4().hex[:8]}"
    httpx.post(
        f"{server.base_url()}/v0/agents",
        json={"name": name, "public_key": "ed25519:" + kp.public_b64},
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["init", "--name", name, "--force"])
    assert result.exit_code != 0
    assert "rejected" in result.output or "409" in result.output
    assert not os.path.exists(cfg_path)


def test_duplicate_name_rejected(env):
    _server, _cfg = env
    runner = CliRunner()
    name = f"dup-{uuid.uuid4().hex[:8]}"
    r1 = runner.invoke(cli, ["init", "--name", name])
    assert r1.exit_code == 0
    r2 = runner.invoke(cli, ["reset", "-y"])
    assert r2.exit_code == 0, r2.output
    r3 = runner.invoke(cli, ["init", "--name", name])
    assert r3.exit_code != 0
    assert "already taken" in r3.output or "rejected" in r3.output


def test_init_preserves_user_metadata(env):
    server, _ = env
    runner = CliRunner()
    name = f"m-{uuid.uuid4().hex[:8]}"
    meta = '{"team": "infra", "lang": "python"}'
    r = runner.invoke(cli, ["init", "--name", name, "--metadata", meta])
    assert r.exit_code == 0, r.output
    r2 = httpx.get(f"{server.base_url()}/v0/agents/{name}")
    assert r2.status_code == 200
    assert r2.json()["metadata"] == {"team": "infra", "lang": "python"}


def test_init_blocks_overwrite(env):
    _server, _cfg = env
    runner = CliRunner()
    runner.invoke(cli, ["init", "--name", f"a-{uuid.uuid4().hex[:8]}"])
    r2 = runner.invoke(cli, ["init", "--name", f"b-{uuid.uuid4().hex[:8]}"])
    assert r2.exit_code != 0
    assert "config already exists" in r2.output


# ============================================================================
# mailbox
# ============================================================================


def test_cli_send_inbox_read_reply_thread(env):
    server, alice_cfg = env
    runner = CliRunner()
    alice_name = f"a-{uuid.uuid4().hex[:8]}"
    r = runner.invoke(cli, ["init", "--name", alice_name, "--display-name", "Alice"])
    assert r.exit_code == 0, r.output

    # Bob: pre-registered via API
    bob_id, bob_name, bob_kp = _register_other(server, "b")
    bob_cfg = os.path.join(os.path.dirname(alice_cfg), "bob.json")
    _write_config(bob_cfg, server, bob_id, bob_name, bob_kp)

    # Alice sends
    r1 = runner.invoke(
        cli, ["send", bob_name, "--subject", "hi bob", "--body", "wanna collab?"]
    )
    assert r1.exit_code == 0, r1.output
    assert "sent" in r1.output

    # Bob: inbox
    bob_runner = CliRunner()
    r2 = bob_runner.invoke(cli, ["--config", bob_cfg, "inbox"])
    assert r2.exit_code == 0, r2.output
    assert "total: 1" in r2.output or "1" in r2.output
    assert alice_name in r2.output  # sender

    # Extract message id
    m = re.search(r"(\w{26})\s+from=" + re.escape(alice_name), r2.output)
    assert m, r2.output
    msg_id = m.group(1)

    # Bob reads
    r3 = bob_runner.invoke(cli, ["--config", bob_cfg, "read", msg_id])
    assert r3.exit_code == 0, r3.output
    assert "wanna collab?" in r3.output
    assert "hi bob" in r3.output

    # Bob replies
    r4 = bob_runner.invoke(cli, ["--config", bob_cfg, "reply", msg_id, "--body", "yes please"])
    assert r4.exit_code == 0, r4.output
    assert "replied" in r4.output

    # Alice: inbox sees the reply
    r5 = runner.invoke(cli, ["inbox"])
    assert r5.exit_code == 0
    assert "1" in r5.output

    # Alice thread
    r6 = runner.invoke(cli, ["thread", msg_id])
    assert r6.exit_code == 0, r6.output
    assert "wanna collab?" in r6.output
    assert "yes please" in r6.output


def test_cli_mark_read_and_outbox(env):
    server, alice_cfg = env
    runner = CliRunner()
    alice_name = f"a-{uuid.uuid4().hex[:8]}"
    runner.invoke(cli, ["init", "--name", alice_name])

    bob_id, bob_name, bob_kp = _register_other(server, "b")
    bob_cfg = os.path.join(os.path.dirname(alice_cfg), "bob.json")
    _write_config(bob_cfg, server, bob_id, bob_name, bob_kp)

    # Alice sends
    runner.invoke(cli, ["send", bob_name, "--body", "x"])

    # Bob: list inbox, mark read, list again
    bob_runner = CliRunner()
    r1 = bob_runner.invoke(cli, ["--config", bob_cfg, "inbox"])
    assert "1" in r1.output
    msg_id = re.search(r"(\w{26})", r1.output).group(1)
    r2 = bob_runner.invoke(cli, ["--config", bob_cfg, "mark-read", msg_id])
    assert r2.exit_code == 0, r2.output
    assert "marked read" in r2.output
    r3 = bob_runner.invoke(cli, ["--config", bob_cfg, "inbox", "--unread"])
    # Unread filter, should show 0
    assert "0" in r3.output

    # Bob: outbox is empty (he never sent anything)
    r4 = bob_runner.invoke(cli, ["--config", bob_cfg, "outbox"])
    assert r4.exit_code == 0
    assert "0" in r4.output

    # Alice: outbox shows the one she sent
    r5 = runner.invoke(cli, ["outbox"])
    assert r5.exit_code == 0
    assert "1" in r5.output


def test_cli_send_to_nonexistent_recipient(env):
    server, _ = env
    runner = CliRunner()
    runner.invoke(cli, ["init", "--name", f"a-{uuid.uuid4().hex[:8]}"])
    r = runner.invoke(cli, ["send", "nobody-here", "--body", "x"])
    assert r.exit_code != 0
    assert "not found" in r.output


def test_cli_send_requires_init(env):
    _server, _cfg = env
    runner = CliRunner()
    r = runner.invoke(cli, ["send", "whoever", "--body", "x"])
    assert r.exit_code != 0
    assert "not initialized" in r.output
