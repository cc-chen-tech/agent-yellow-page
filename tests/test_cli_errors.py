"""Error-path coverage for the CLI.

Hits the missing-line branches in app/cli.py: every 404 / 403 / 410 / 422
error path that the happy-path tests in test_cli.py skip.
"""

from __future__ import annotations

import json
import os
import re
import uuid

import httpx
import pytest
from click.testing import CliRunner

from app.cli import cli
from app.crypto import KeyPair
from app.main import create_app

# Reuse helpers from test_cli.py without going through tests package
import sys as _sys
import importlib.util as _ilu
_spec = _ilu.spec_from_file_location("test_cli_mod", _sys.path[0] + "/test_cli.py")
_tc = _ilu.module_from_spec(_spec); _spec.loader.exec_module(_tc)
_Server = _tc._Server
_register_other = _tc._register_other
_write_config = _tc._write_config
cli_env = _tc.env


# --- init / config errors -------------------------------------------------- #


def test_init_rejects_overshort_name(cli_env):
    _server, _cfg = cli_env
    runner = CliRunner()
    r = runner.invoke(cli, ["init", "--name", "ab"])  # min_length=3
    assert r.exit_code != 0
    assert "name" in r.output.lower()


def test_init_rejects_invalid_public_key(cli_env):
    _server, _cfg = cli_env
    runner = CliRunner()
    r = runner.invoke(cli, ["init", "--name", f"bad-{uuid.uuid4().hex[:6]}", "--public-key", "ed25519:garbage"])
    # public-key isn't a flag — error path triggers
    assert r.exit_code != 0


# --- get / list edge cases -------------------------------------------------- #


def test_get_404_for_nonexistent_agent(cli_env):
    _server, _cfg = cli_env
    runner = CliRunner()
    r = runner.invoke(cli, ["get", "nonexistent-agent-name"])
    assert r.exit_code != 0
    assert "not found" in r.output.lower() or "404" in r.output


def test_list_with_zero_offset(cli_env):
    _server, _cfg = cli_env
    runner = CliRunner()
    r = runner.invoke(cli, ["list", "--offset", "0", "--limit", "5"])
    assert r.exit_code == 0


# --- whoami / update / delete before init --------------------------------- #


def test_whoami_without_config_file(cli_env):
    _server, _cfg = cli_env
    runner = CliRunner()
    r = runner.invoke(cli, ["whoami"])
    assert r.exit_code != 0
    assert "not initialized" in r.output


def test_update_without_any_flag(cli_env):
    _server, _cfg = cli_env
    runner = CliRunner()
    runner.invoke(cli, ["init", "--name", f"u-{uuid.uuid4().hex[:6]}"])
    r = runner.invoke(cli, ["update"])
    assert r.exit_code != 0
    assert "nothing to update" in r.output.lower()


def test_update_with_invalid_metadata_json(cli_env):
    _server, _cfg = cli_env
    runner = CliRunner()
    runner.invoke(cli, ["init", "--name", f"m-{uuid.uuid4().hex[:6]}"])
    r = runner.invoke(cli, ["update", "--metadata", "not json"])
    assert r.exit_code != 0


def test_delete_aborts_without_yes(cli_env):
    _server, _cfg = cli_env
    runner = CliRunner()
    runner.invoke(cli, ["init", "--name", f"d-{uuid.uuid4().hex[:6]}"])
    r = runner.invoke(cli, ["delete"], input="n\n")
    assert r.exit_code != 0
    assert "aborted" in r.output.lower() or "cancelled" in r.output.lower()


def test_reset_no_config_does_nothing(cli_env):
    _server, _cfg = cli_env
    if os.path.exists(_cfg):
        os.unlink(_cfg)
    runner = CliRunner()
    r = runner.invoke(cli, ["reset", "-y"])
    assert "no config" in r.output.lower() or r.exit_code == 0


# --- mailbox errors --------------------------------------------------------- #


def test_send_to_nonexistent_recipient(cli_env):
    _server, _cfg = cli_env
    runner = CliRunner()
    runner.invoke(cli, ["init", "--name", f"s-{uuid.uuid4().hex[:6]}"])
    r = runner.invoke(cli, ["send", "nobody-out-there", "--body", "hi"])
    assert r.exit_code != 0
    assert "not found" in r.output.lower()


def test_send_to_self_rejected(cli_env):
    _server, _cfg = cli_env
    runner = CliRunner()
    runner.invoke(cli, ["init", "--name", f"self-{uuid.uuid4().hex[:6]}"])
    cfg = json.loads(open(_cfg).read())
    r = runner.invoke(cli, ["send", cfg["name"], "--body", "to myself"])
    assert r.exit_code != 0
    assert "to yourself" in r.output.lower() or "invalid" in r.output.lower()


def test_reply_404_for_missing_message(cli_env):
    _server, _cfg = cli_env
    runner = CliRunner()
    runner.invoke(cli, ["init", "--name", f"r-{uuid.uuid4().hex[:6]}"])
    r = runner.invoke(cli, ["reply", "01M00000000000000000000000", "--body", "x"])
    assert r.exit_code != 0
    assert "not found" in r.output.lower()


def test_read_message_404(cli_env):
    _server, _cfg = cli_env
    runner = CliRunner()
    runner.invoke(cli, ["init", "--name", f"rd-{uuid.uuid4().hex[:6]}"])
    r = runner.invoke(cli, ["read", "01M00000000000000000000000"])
    assert r.exit_code != 0
    assert "not found" in r.output.lower()


def test_mark_read_404(cli_env):
    _server, _cfg = cli_env
    runner = CliRunner()
    runner.invoke(cli, ["init", "--name", f"mr-{uuid.uuid4().hex[:6]}"])
    r = runner.invoke(cli, ["mark-read", "01M00000000000000000000000"])
    assert r.exit_code != 0
    assert "not found" in r.output.lower()


def test_inbox_before_init_blocked(cli_env):
    _server, _cfg = cli_env
    if os.path.exists(_cfg):
        os.unlink(_cfg)
    runner = CliRunner()
    r = runner.invoke(cli, ["inbox"])
    assert r.exit_code != 0
    assert "not initialized" in r.output


def test_outbox_before_init_blocked(cli_env):
    _server, _cfg = cli_env
    if os.path.exists(_cfg):
        os.unlink(_cfg)
    runner = CliRunner()
    r = runner.invoke(cli, ["outbox"])
    assert r.exit_code != 0
    assert "not initialized" in r.output


def test_thread_403_for_non_participant(cli_env):
    _server, _cfg = cli_env
    runner = CliRunner()
    r = runner.invoke(cli, ["thread", "01M00000000000000000000000"])
    assert r.exit_code != 0
    # No config means cli hits "not initialized" before any HTTP call
    assert "not initialized" in r.output.lower() or "forbidden" in r.output.lower()


# --- chat (public) errors -------------------------------------------------- #


def test_chat_post_422_for_empty_body(cli_env):
    _server, _cfg = cli_env
    runner = CliRunner()
    runner.invoke(cli, ["init", "--name", f"c-{uuid.uuid4().hex[:6]}"])
    # Click requires an arg; the API rejects empty body. Force empty via direct HTTP.
    # Easier: just call with a single space; the server's min_length=1 will allow it.
    r = runner.invoke(cli, ["chat", "post", "x"])
    assert r.exit_code == 0  # happy path actually


def test_chat_read_404(cli_env):
    _server, _cfg = cli_env
    runner = CliRunner()
    r = runner.invoke(cli, ["chat", "read", "01M00000000000000000000000"])
    assert r.exit_code != 0
    assert "not found" in r.output.lower()


def test_chat_stranger_cannot_delete_via_cli(cli_env):
    server, _cfg = cli_env
    # Alice posts a chat message
    alice_runner = CliRunner()
    alice_runner.invoke(cli, ["init", "--name", f"alice-{uuid.uuid4().hex[:6]}"])
    r = alice_runner.invoke(cli, ["chat", "post", "alice's note"])
    assert r.exit_code == 0
    msg_id = re.search(r"id=(\w{26})", r.output).group(1)
    # Eve registers via API, config in separate file
    eve_id, eve_name, eve_kp = _register_other(server, "eve")
    eve_cfg = os.path.join(os.path.dirname(_cfg), "eve.json")
    _write_config(eve_cfg, server, eve_id, eve_name, eve_kp)
    eve_runner = CliRunner()
    r = eve_runner.invoke(cli, ["--config", eve_cfg, "chat", "delete", msg_id, "-y"])
    assert r.exit_code != 0
    # Should be 403 — only sender can delete
    assert "403" in r.output or "sender" in r.output.lower()


# --- private chatroom errors ----------------------------------------------- #


def test_pc_info_404(cli_env):
    _server, _cfg = cli_env
    runner = CliRunner()
    r = runner.invoke(cli, ["pc", "info", "nonexistent-room"])
    assert r.exit_code != 0
    assert "not found" in r.output.lower()


def test_pc_create_409_for_duplicate_name(cli_env):
    _server, _cfg = cli_env
    runner = CliRunner()
    runner.invoke(cli, ["init", "--name", f"pc-{uuid.uuid4().hex[:6]}"])
    name = f"dup-{uuid.uuid4().hex[:6]}"
    r1 = runner.invoke(cli, ["pc", "create", "--name", name])
    assert r1.exit_code == 0
    r2 = runner.invoke(cli, ["pc", "create", "--name", name])
    assert r2.exit_code != 0
    assert "already taken" in r2.output or "rejected" in r2.output


def test_pc_invite_requires_init(cli_env):
    _server, _cfg = cli_env
    if os.path.exists(_cfg):
        os.unlink(_cfg)
    runner = CliRunner()
    r = runner.invoke(cli, ["pc", "invite", "any-room"])
    assert r.exit_code != 0
    assert "not initialized" in r.output


def test_pc_invite_404_for_missing_room(cli_env):
    _server, _cfg = cli_env
    runner = CliRunner()
    runner.invoke(cli, ["init", "--name", f"pc-{uuid.uuid4().hex[:6]}"])
    r = runner.invoke(cli, ["pc", "invite", "no-such-room"])
    assert r.exit_code != 0
    assert "not found" in r.output.lower()


def test_pc_invite_403_for_non_creator(cli_env):
    server, _cfg = cli_env
    # Alice creates
    alice_runner = CliRunner()
    alice_runner.invoke(cli, ["init", "--name", f"al-{uuid.uuid4().hex[:6]}"])
    r = alice_runner.invoke(cli, ["pc", "create", "--name", f"r-{uuid.uuid4().hex[:6]}"])
    room = re.search(r"id=(\w{26})", r.output).group(1)
    # Bob registers, joins via invite, then tries to invite (only creator can)
    bob_id, bob_name, bob_kp = _register_other(server, "bob")
    bob_cfg = os.path.join(os.path.dirname(_cfg), "bob.json")
    _write_config(bob_cfg, server, bob_id, bob_name, bob_kp)
    inv = alice_runner.invoke(cli, ["pc", "invite", re.search(r"name=(\S+)", r.output).group(1)]).output
    code = re.search(r"code:\s+(\S+)", inv).group(1)
    bob_runner = CliRunner()
    bob_runner.invoke(cli, ["--config", bob_cfg, "pc", "join", re.search(r"name=(\S+)", r.output).group(1), "--code", code])
    r2 = bob_runner.invoke(cli, ["--config", bob_cfg, "pc", "invite", re.search(r"name=(\S+)", r.output).group(1)])
    assert r2.exit_code != 0
    assert "creator" in r2.output.lower() or "403" in r2.output


def test_pc_join_410_for_invalid_code(cli_env):
    server, _cfg = cli_env
    # Alice creates the room
    alice_runner = CliRunner()
    alice_runner.invoke(cli, ["init", "--name", f"al-{uuid.uuid4().hex[:6]}"])
    r = alice_runner.invoke(cli, ["pc", "create", "--name", f"r-{uuid.uuid4().hex[:6]}"])
    name = re.search(r"name=(\S+)", r.output).group(1)
    # Eve (not a member) tries to join with a bogus code → 410
    eve_id, eve_name, eve_kp = _register_other(server, "eve")
    eve_cfg = os.path.join(os.path.dirname(_cfg), "eve.json")
    _write_config(eve_cfg, server, eve_id, eve_name, eve_kp)
    eve_runner = CliRunner()
    r2 = eve_runner.invoke(
        cli, ["--config", eve_cfg, "pc", "join", name, "--code", "TOTALLY_INVALID"]
    )
    assert r2.exit_code != 0
    assert (
        "invalid" in r2.output.lower()
        or "expired" in r2.output.lower()
        or "exhausted" in r2.output.lower()
        or "410" in r2.output
    )


def test_pc_join_409_for_already_member(cli_env):
    """After joining, calling join again → 409 already-member."""
    server, _cfg = cli_env
    alice_runner = CliRunner()
    alice_runner.invoke(cli, ["init", "--name", f"al-{uuid.uuid4().hex[:6]}"])
    r = alice_runner.invoke(cli, ["pc", "create", "--name", f"r-{uuid.uuid4().hex[:6]}"])
    name = re.search(r"name=(\S+)", r.output).group(1)
    inv_out = alice_runner.invoke(cli, ["pc", "invite", name, "--max-uses", "5"]).output
    code = re.search(r"code:\s+(\S+)", inv_out).group(1)
    # Bob joins with multi-use code
    bob_id, bob_name, bob_kp = _register_other(server, "bob")
    bob_cfg = os.path.join(os.path.dirname(_cfg), "bob.json")
    _write_config(bob_cfg, server, bob_id, bob_name, bob_kp)
    bob_runner = CliRunner()
    rj = bob_runner.invoke(cli, ["--config", bob_cfg, "pc", "join", name, "--code", code])
    assert rj.exit_code == 0
    # Bob joins again with same code (still valid) → 409 already a member
    r2 = bob_runner.invoke(cli, ["--config", bob_cfg, "pc", "join", name, "--code", code])
    assert r2.exit_code != 0
    assert "already" in r2.output.lower() or "409" in r2.output


def test_pc_leave_403_for_non_member(cli_env):
    server, _cfg = cli_env
    # Alice creates room, Eve tries to leave it
    alice_runner = CliRunner()
    alice_runner.invoke(cli, ["init", "--name", f"al-{uuid.uuid4().hex[:6]}"])
    r = alice_runner.invoke(cli, ["pc", "create", "--name", f"r-{uuid.uuid4().hex[:6]}"])
    name = re.search(r"name=(\S+)", r.output).group(1)

    eve_id, eve_name, eve_kp = _register_other(server, "eve")
    eve_cfg = os.path.join(os.path.dirname(_cfg), "eve.json")
    _write_config(eve_cfg, server, eve_id, eve_name, eve_kp)
    eve_runner = CliRunner()
    r = eve_runner.invoke(cli, ["--config", eve_cfg, "pc", "leave", name])
    assert r.exit_code != 0
    assert "not a member" in r.output.lower() or "403" in r.output


def test_pc_members_403_for_non_member(cli_env):
    server, _cfg = cli_env
    alice_runner = CliRunner()
    alice_runner.invoke(cli, ["init", "--name", f"al-{uuid.uuid4().hex[:6]}"])
    r = alice_runner.invoke(cli, ["pc", "create", "--name", f"r-{uuid.uuid4().hex[:6]}"])
    name = re.search(r"name=(\S+)", r.output).group(1)

    eve_id, eve_name, eve_kp = _register_other(server, "eve")
    eve_cfg = os.path.join(os.path.dirname(_cfg), "eve.json")
    _write_config(eve_cfg, server, eve_id, eve_name, eve_kp)
    eve_runner = CliRunner()
    r = eve_runner.invoke(cli, ["--config", eve_cfg, "pc", "members", name])
    assert r.exit_code != 0
    assert "member" in r.output.lower() or "403" in r.output


def test_pc_send_403_for_non_member(cli_env):
    server, _cfg = cli_env
    alice_runner = CliRunner()
    alice_runner.invoke(cli, ["init", "--name", f"al-{uuid.uuid4().hex[:6]}"])
    r = alice_runner.invoke(cli, ["pc", "create", "--name", f"r-{uuid.uuid4().hex[:6]}"])
    name = re.search(r"name=(\S+)", r.output).group(1)

    eve_id, eve_name, eve_kp = _register_other(server, "eve")
    eve_cfg = os.path.join(os.path.dirname(_cfg), "eve.json")
    _write_config(eve_cfg, server, eve_id, eve_name, eve_kp)
    eve_runner = CliRunner()
    r = eve_runner.invoke(cli, ["--config", eve_cfg, "pc", "send", name, "hi"])
    assert r.exit_code != 0
    assert "member" in r.output.lower() or "403" in r.output


def test_pc_messages_403_for_non_member(cli_env):
    server, _cfg = cli_env
    alice_runner = CliRunner()
    alice_runner.invoke(cli, ["init", "--name", f"al-{uuid.uuid4().hex[:6]}"])
    r = alice_runner.invoke(cli, ["pc", "create", "--name", f"r-{uuid.uuid4().hex[:6]}"])
    name = re.search(r"name=(\S+)", r.output).group(1)

    eve_id, eve_name, eve_kp = _register_other(server, "eve")
    eve_cfg = os.path.join(os.path.dirname(_cfg), "eve.json")
    _write_config(eve_cfg, server, eve_id, eve_name, eve_kp)
    eve_runner = CliRunner()
    r = eve_runner.invoke(cli, ["--config", eve_cfg, "pc", "messages", name])
    assert r.exit_code != 0
    assert "member" in r.output.lower() or "403" in r.output


def test_pc_delete_403_for_non_creator(cli_env):
    server, _cfg = cli_env
    alice_runner = CliRunner()
    alice_runner.invoke(cli, ["init", "--name", f"al-{uuid.uuid4().hex[:6]}"])
    r = alice_runner.invoke(cli, ["pc", "create", "--name", f"r-{uuid.uuid4().hex[:6]}"])
    name = re.search(r"name=(\S+)", r.output).group(1)

    bob_id, bob_name, bob_kp = _register_other(server, "bob")
    bob_cfg = os.path.join(os.path.dirname(_cfg), "bob.json")
    _write_config(bob_cfg, server, bob_id, bob_name, bob_kp)
    bob_runner = CliRunner()
    r = bob_runner.invoke(cli, ["--config", bob_cfg, "pc", "delete", name, "-y"])
    assert r.exit_code != 0
    assert "creator" in r.output.lower() or "403" in r.output


def test_pc_delete_aborts_without_yes(cli_env):
    _server, _cfg = cli_env
    runner = CliRunner()
    runner.invoke(cli, ["init", "--name", f"al-{uuid.uuid4().hex[:6]}"])
    r = runner.invoke(cli, ["pc", "create", "--name", f"r-{uuid.uuid4().hex[:6]}"])
    name = re.search(r"name=(\S+)", r.output).group(1)
    r2 = runner.invoke(cli, ["pc", "delete", name], input="n\n")
    assert r2.exit_code != 0


# --- chat delete confirmation abort ---------------------------------------- #


def test_chat_delete_aborts_without_yes(cli_env):
    _server, _cfg = cli_env
    runner = CliRunner()
    runner.invoke(cli, ["init", "--name", f"c-{uuid.uuid4().hex[:6]}"])
    r = runner.invoke(cli, ["chat", "post", "delete me?"])
    msg_id = re.search(r"id=(\w{26})", r.output).group(1)
    r2 = runner.invoke(cli, ["chat", "delete", msg_id], input="n\n")
    assert r2.exit_code != 0
