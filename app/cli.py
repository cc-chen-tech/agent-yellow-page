"""CLI for the agent yellow-page.

Designed for AI agents themselves — `init` to register, `whoami` / `get` /
`list` to discover, `update` / `delete` to manage your own card, `sign` to
prove liveness against a server-issued challenge.

Local state is kept in a single JSON file (default: ~/.config/agent-yp/config.json,
0600 permissions) containing the agent id, server URL, and the raw 32-byte
private key seed (base64). The private key NEVER leaves the machine.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path
from typing import Any

import click
import httpx

from .client import YellowPageClient
from .crypto import KeyPair, canonical_request

DEFAULT_CONFIG = Path.home() / ".config" / "agent-yp" / "config.json"
DEFAULT_SERVER = "http://127.0.0.1:8000"


# --- config helpers ---------------------------------------------------------


def _config_path() -> Path:
    return Path(os.environ.get("AGENT_YP_CONFIG", str(DEFAULT_CONFIG)))


def _server() -> str:
    return os.environ.get("YELLOWPAGE_SERVER", DEFAULT_SERVER)


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _save(path: Path, cfg: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # Windows / non-POSIX FS — best effort


def _kp_from_cfg(cfg: dict) -> KeyPair:
    if "private_key_raw_b64" not in cfg:
        raise click.ClickException("no private key in config — run `agent-yp init` first")
    return KeyPair.from_private_raw(base64.b64decode(cfg["private_key_raw_b64"]))


def _client(ctx: click.Context) -> YellowPageClient:
    cfg = ctx.obj["config"]
    c = YellowPageClient(ctx.obj["server"])
    if "agent_id" in cfg and "private_key_raw_b64" in cfg:
        c.agent_id = cfg["agent_id"]
        c.keypair = _kp_from_cfg(cfg)
    return c


def _require_init(ctx: click.Context) -> dict:
    cfg = ctx.obj["config"]
    if "agent_id" not in cfg:
        raise click.ClickException("not initialized — run `agent-yp init --name ...` first")
    return cfg


# --- CLI root ---------------------------------------------------------------


@click.group()
@click.option(
    "--server",
    envvar="YELLOWPAGE_SERVER",
    default=None,
    help="API base URL (default: $YELLOWPAGE_SERVER or http://127.0.0.1:8000)",
)
@click.option(
    "--config",
    "config_path",
    envvar="AGENT_YP_CONFIG",
    default=None,
    type=click.Path(path_type=Path),
    help="Config file path (default: $AGENT_YP_CONFIG or ~/.config/agent-yp/config.json)",
)
@click.pass_context
def cli(ctx: click.Context, server: str | None, config_path: Path | None) -> None:
    """AI Agent Yellow Page — register, discover, update your agent card."""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path or _config_path()
    ctx.obj["server"] = server or _server()
    ctx.obj["config"] = _load(ctx.obj["config_path"])


# --- init -------------------------------------------------------------------


@cli.command()
@click.option("--name", required=True, help="unique slug [a-z0-9-]{3,64}")
@click.option("--display-name", default=None, help="human display name")
@click.option("--description", default=None)
@click.option("--endpoint", default=None, help="agent service URL (https://...)")
@click.option("--tag", "tags", multiple=True, help="repeat for multiple")
@click.option("--metadata", default=None, help="JSON string")
@click.option(
    "--force",
    is_flag=True,
    help="skip the pre-flight name check (use only if you want the server to 409)",
)
@click.pass_context
def init(
    ctx: click.Context,
    name: str,
    display_name: str | None,
    description: str | None,
    endpoint: str | None,
    tags: tuple[str, ...],
    metadata: str | None,
    force: bool,
) -> None:
    """Generate a keypair, register on the server, save config."""
    cfg_path: Path = ctx.obj["config_path"]
    if cfg_path.exists():
        raise click.ClickException(
            f"config already exists at {cfg_path} — run `agent-yp reset` first to overwrite"
        )

    # Pre-flight: check that the name is free. Saves a wasted register
    # roundtrip (and a wasted keypair generation) when the name is taken.
    if not force:
        with _client(ctx) as c:
            try:
                c.get(name)
            except httpx.HTTPStatusError as e:
                if e.response.status_code != 404:
                    raise
                # 404 → name is free, fall through
            else:
                raise click.ClickException(
                    f"name '{name}' is already taken — pick a different one, "
                    f"or use `agent-yp check-name` to verify first "
                    f"(or pass --force to attempt and let the server 409)"
                )

    kp = KeyPair.generate()
    public_key = "ed25519:" + kp.public_b64
    metadata_dict: dict[str, Any] | None = json.loads(metadata) if metadata else None

    click.echo(f"→ generating keypair  pub={public_key[:24]}…")
    click.echo(f"→ registering '{name}' at {ctx.obj['server']}")

    with _client(ctx) as c:
        try:
            card = c.register(
                name=name,
                public_key=public_key,
                display_name=display_name,
                description=description,
                endpoint=endpoint,
                tags=list(tags) or None,
                metadata=metadata_dict,
            )
        except httpx.HTTPStatusError as e:
            # NOTE: HTTPStatusError is a subclass of HTTPError — must come first
            try:
                err = e.response.json().get("detail", e.response.text)
            except Exception:
                err = e.response.text
            raise click.ClickException(f"server rejected: {err}") from e
        except httpx.HTTPError as e:
            raise click.ClickException(f"network error: {e}") from e

    cfg = {
        "server": ctx.obj["server"],
        "agent_id": card["id"],
        "name": card["name"],
        "private_key_raw_b64": base64.b64encode(kp.private_raw).decode("ascii"),
    }
    _save(cfg_path, cfg)
    click.echo(f"✓ registered  id={card['id']}  name={card['name']}  version={card['version']}")
    click.echo(f"  config saved to {cfg_path}")


# --- name check ------------------------------------------------------------


@cli.command("check-name")
@click.argument("name")
@click.pass_context
def check_name(ctx: click.Context, name: str) -> None:
    """Check whether a name is available.

    Exit 0 if available, exit 1 if taken. Outputs a single JSON line so
    it's easy to consume in shell scripts:

        $ agent-yp check-name weather-bot
        {"name": "weather-bot", "available": false, "reason": "name already taken"}
    """
    with _client(ctx) as c:
        try:
            c.get(name)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                click.echo(json.dumps({"name": name, "available": True}))
                return
            raise
    click.echo(
        json.dumps({"name": name, "available": False, "reason": "name already taken"})
    )
    raise SystemExit(1)


# --- whoami / get / list ---------------------------------------------------


@cli.command()
@click.pass_context
def whoami(ctx: click.Context) -> None:
    """Show your own agent record (refreshed from server)."""
    cfg = _require_init(ctx)
    with _client(ctx) as c:
        try:
            card = c.get(cfg["name"])
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise click.ClickException(
                    "agent not found on server — was it deleted? try `agent-yp init`"
                ) from e
            raise
    click.echo(json.dumps(card, indent=2, ensure_ascii=False))


@cli.command()
@click.argument("target")
@click.pass_context
def get(ctx: click.Context, target: str) -> None:
    """Look up an agent by id or name (public, no signature)."""
    with _client(ctx) as c:
        try:
            card = c.get(target)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise click.ClickException(f"agent not found: {target}") from e
            raise
    click.echo(json.dumps(card, indent=2, ensure_ascii=False))


@cli.command(name="list")
@click.option("--q", default=None, help="search text (substring, case-insensitive)")
@click.option("--tag", "tags", multiple=True, help="AND-filter, repeat for multiple")
@click.option("--limit", default=50, type=click.IntRange(1, 200), show_default=True)
@click.option("--offset", default=0, type=click.IntRange(min=0), show_default=True)
@click.option("--json", "as_json", is_flag=True, help="raw JSON output")
@click.pass_context
def list_cmd(
    ctx: click.Context,
    q: str | None,
    tags: tuple[str, ...],
    limit: int,
    offset: int,
    as_json: bool,
) -> None:
    """List / search agents (public)."""
    with _client(ctx) as c:
        result = c.list(
            q=q, tags=list(tags) or None, limit=limit, offset=offset
        )
    if as_json:
        click.echo(json.dumps(result, indent=2, ensure_ascii=False))
        return
    click.echo(f"total: {result['total']}  showing: {len(result['items'])} (offset={offset})")
    for item in result["items"]:
        title = item.get("display_name") or item["name"]
        click.echo(f"  {item['id']}  {item['name']:30s}  {title}")
        if item.get("description"):
            click.echo(f"      {item['description'][:100]}")
        if item.get("tags"):
            click.echo(f"      tags: {','.join(item['tags'])}")
        if item.get("endpoint"):
            click.echo(f"      endpoint: {item['endpoint']}")


# --- update -----------------------------------------------------------------


@cli.command()
@click.option("--description", default=None)
@click.option("--display-name", default=None)
@click.option("--endpoint", default=None)
@click.option("--add-tag", "add_tags", multiple=True)
@click.option("--remove-tag", "remove_tags", multiple=True)
@click.option("--metadata", default=None, help="JSON string (full replace)")
@click.option("--if-match", "if_match", default=None, help="expected version for optimistic lock")
@click.pass_context
def update(
    ctx: click.Context,
    description: str | None,
    display_name: str | None,
    endpoint: str | None,
    add_tags: tuple[str, ...],
    remove_tags: tuple[str, ...],
    metadata: str | None,
    if_match: str | None,
) -> None:
    """Update your agent's card (signed)."""
    cfg = _require_init(ctx)
    fields: dict[str, Any] = {}
    if description is not None:
        fields["description"] = description
    if display_name is not None:
        fields["display_name"] = display_name
    if endpoint is not None:
        fields["endpoint"] = endpoint
    if metadata is not None:
        fields["metadata"] = json.loads(metadata)

    if add_tags or remove_tags:
        with _client(ctx) as c:
            current = c.get(cfg["name"])
        tags = list(current.get("tags") or [])
        for t in add_tags:
            if t not in tags:
                tags.append(t)
        for t in remove_tags:
            if t in tags:
                tags.remove(t)
        fields["tags"] = tags

    if not fields:
        raise click.ClickException("nothing to update (no flags provided)")

    with _client(ctx) as c:
        try:
            card = c.patch(cfg["agent_id"], fields, if_match=if_match)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 409:
                raise click.ClickException(
                    "version conflict — pass --if-match with the current version, "
                    "or omit it for last-write-wins"
                ) from e
            raise
    click.echo(f"✓ updated  version={card['version']}  id={card['id']}")


# --- delete / reset ---------------------------------------------------------


@cli.command()
@click.option("-y", "--yes", is_flag=True, help="skip confirmation")
@click.pass_context
def delete(ctx: click.Context, yes: bool) -> None:
    """Delete your agent (signed). Removes local config too."""
    cfg = _require_init(ctx)
    if not yes:
        click.confirm(
            f"delete agent '{cfg['name']}' ({cfg['agent_id']})?", abort=True
        )
    with _client(ctx) as c:
        try:
            c.delete(cfg["agent_id"])
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                click.echo("(already gone on server)")
            else:
                raise
    cfg_path: Path = ctx.obj["config_path"]
    if cfg_path.exists():
        cfg_path.unlink()
    click.echo("✓ deleted and local config removed")


@cli.command()
@click.option("-y", "--yes", is_flag=True, help="skip confirmation")
def reset(yes: bool) -> None:
    """Remove local config (logout without contacting the server)."""
    path = _config_path()
    if not path.exists():
        click.echo("(no config to remove)")
        return
    if not yes:
        click.confirm(f"delete {path}?", abort=True)
    path.unlink()
    click.echo(f"✓ removed {path}")


# --- sign / prove liveness --------------------------------------------------


@cli.command()
@click.argument("target", required=False, default=None)
@click.pass_context
def sign(ctx: click.Context, target: str | None) -> None:
    """Sign a server-issued challenge (proves you still hold the private key).

    Without TARGET: signs the challenge issued for your own agent.
    With TARGET: signs a challenge for another agent (uses your key to prove
    liveness to a third party).
    """
    cfg = ctx.obj["config"]
    kp = _kp_from_cfg(cfg)
    target = target or cfg.get("name")
    if not target:
        raise click.ClickException("no agent in config and no TARGET given")
    with _client(ctx) as c:
        chal = c.challenge(target)
    # canonical_request uses (timestamp=0, method, path, body) — but the
    # server's challenge is raw bytes. Sign those bytes directly.
    msg = base64.b64decode(chal["challenge"])
    signature_b64 = kp.sign(msg)
    click.echo(json.dumps({
        "target": target,
        "challenge": chal["challenge"],
        "expires_at": chal["expires_at"],
        "public_key": "ed25519:" + kp.public_b64,
        "signature": signature_b64,
    }, indent=2))


# --- entrypoint -------------------------------------------------------------


def main() -> None:
    cli(obj={})


if __name__ == "__main__":
    main()
