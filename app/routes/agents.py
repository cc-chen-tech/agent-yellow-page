"""/v0/agents routes."""

from __future__ import annotations

import base64
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response, status
from pydantic import ValidationError
from ulid import ULID

from ..models import (
    AgentCard,
    AgentCreate,
    AgentList,
    AgentPatch,
    ChallengeResponse,
    utcnow,
)
from ..signer import SignedAgentDep
from ..storage import (
    ConflictError,
    NotFoundError,
    Storage,
    VersionConflictError,
)

router = APIRouter(prefix="/v0/agents", tags=["agents"])


# --- helpers -------------------------------------------------------------- #


def _bad_request(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"error": "invalid_request", "message": message},
    )


def _not_found(what: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": "not_found", "message": what},
    )


def _conflict(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"error": "conflict", "message": message},
    )


def _resolve_path_id(id_or_name: str, storage: Storage) -> dict:
    row = storage.find_by_id_or_name(id_or_name)
    if row is None:
        raise _not_found(f"agent not found: {id_or_name}")
    return row


# --- POST /v0/agents ------------------------------------------------------ #


@router.post("", status_code=status.HTTP_201_CREATED, response_model=AgentCard)
async def register_agent(payload: AgentCreate, request: Request) -> AgentCard:
    storage: Storage = request.app.state.storage

    # Pre-check for clearer errors (race condition with concurrent inserts is still possible)
    if storage.get_by_name(payload.name):
        raise _conflict(f"name already taken: {payload.name}")
    if storage.get_by_public_key(payload.public_key):
        raise _conflict("public_key already bound to another agent")

    agent_id = str(ULID())
    try:
        row = storage.insert(agent_id, payload)
    except ConflictError as e:
        if e.field == "name":
            raise _conflict(f"name already taken: {payload.name}") from e
        if e.field == "public_key":
            raise _conflict("public_key already bound to another agent") from e
        raise _conflict("duplicate field") from e

    return AgentCard.from_row(row)


# --- GET /v0/agents ------------------------------------------------------- #


@router.get("", response_model=AgentList)
async def list_agents(
    request: Request,
    q: Annotated[str | None, Query(max_length=128, description="substring search")] = None,
    tag: Annotated[list[str] | None, Query(description="AND-filter, repeat")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AgentList:
    storage: Storage = request.app.state.storage
    total, rows = storage.list(q=q, tags=tag, limit=limit, offset=offset)
    return AgentList(
        total=total,
        limit=limit,
        offset=offset,
        items=[AgentCard.from_row(r) for r in rows],
    )


# --- GET /v0/agents/{id_or_name} ------------------------------------------ #


@router.get("/{id_or_name}", response_model=AgentCard)
async def get_agent(id_or_name: str, request: Request) -> AgentCard:
    storage: Storage = request.app.state.storage
    row = _resolve_path_id(id_or_name, storage)
    return AgentCard.from_row(row)


# --- PUT /v0/agents/{id} -------------------------------------------------- #


@router.put("/{agent_id}", response_model=AgentCard)
async def replace_agent(
    request: Request,
    agent_id: str,
    payload: AgentCreate,
    agent: SignedAgentDep,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> AgentCard:
    # Signed agent must match the path id
    if agent["id"] != agent_id:
        raise _bad_request("signature does not match path id")

    storage: Storage = request.app.state.storage
    expected_version = agent["version"]
    if if_match:
        m = re.match(r'^"?(\d+)"?$', if_match.strip())
        if not m:
            raise _bad_request("If-Match must be a quoted integer version")
        expected_version = int(m.group(1))

    try:
        row = storage.update_full(agent_id, expected_version, payload)
    except NotFoundError as e:
        raise _not_found(f"agent not found: {agent_id}") from e
    except VersionConflictError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "conflict",
                "message": f"version mismatch: expected={e.expected}, "
                f"current={storage.get_by_id(agent_id)['version']}",
            },
        ) from e
    return AgentCard.from_row(row)


# --- PATCH /v0/agents/{id} ------------------------------------------------ #


@router.patch("/{agent_id}", response_model=AgentCard)
async def patch_agent(
    request: Request,
    agent_id: str,
    payload: AgentPatch,
    agent: SignedAgentDep,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> AgentCard:
    if agent["id"] != agent_id:
        raise _bad_request("signature does not match path id")

    storage: Storage = request.app.state.storage
    expected_version = agent["version"]
    if if_match:
        m = re.match(r'^"?(\d+)"?$', if_match.strip())
        if not m:
            raise _bad_request("If-Match must be a quoted integer version")
        expected_version = int(m.group(1))

    try:
        row = storage.update_partial(agent_id, expected_version, payload)
    except NotFoundError as e:
        raise _not_found(f"agent not found: {agent_id}") from e
    except VersionConflictError as e:
        current = storage.get_by_id(agent_id)
        cur_v = current["version"] if current else "?"
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "conflict",
                "message": f"version mismatch: expected={e.expected}, current={cur_v}",
            },
        ) from e
    return AgentCard.from_row(row)


# --- DELETE /v0/agents/{id} ----------------------------------------------- #


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(
    request: Request,
    agent_id: str,
    agent: SignedAgentDep,
) -> Response:
    if agent["id"] != agent_id:
        raise _bad_request("signature does not match path id")

    storage: Storage = request.app.state.storage
    if not storage.delete(agent_id):
        raise _not_found(f"agent not found: {agent_id}")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- GET /v0/agents/{id_or_name}/challenge ------------------------------- #


@router.get("/{id_or_name}/challenge", response_model=ChallengeResponse)
async def get_challenge(id_or_name: str, request: Request) -> ChallengeResponse:
    storage: Storage = request.app.state.storage
    _resolve_path_id(id_or_name, storage)  # 404 if missing
    raw = os.urandom(16)
    challenge = base64.b64encode(raw).decode("ascii")
    expires = datetime.now(timezone.utc) + timedelta(minutes=5)
    return ChallengeResponse(challenge=challenge, expires_at=expires)


# --- bulk import (admin, MVP only; not signed) --------------------------- #


@router.post("/_bulk", response_model=list[AgentCard], include_in_schema=False)
async def bulk_import(payloads: list[AgentCreate], request: Request) -> list[AgentCard]:
    """Disabled by default; enable only in dev. Not exposed in OpenAPI."""
    if not request.app.state.allow_bulk:
        raise HTTPException(403, detail={"error": "forbidden", "message": "bulk disabled"})
    storage: Storage = request.app.state.storage
    out: list[AgentCard] = []
    for p in payloads:
        if storage.get_by_name(p.name) or storage.get_by_public_key(p.public_key):
            continue
        agent_id = str(ULID())
        row = storage.insert(agent_id, p)
        out.append(AgentCard.from_row(row))
    return out
