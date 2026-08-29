"""Signed-request dependency for FastAPI.

Verifies X-Agent-Id / X-Timestamp / X-Nonce / X-Signature headers, attaches
the resolved agent record to request.state so handlers can use it.
"""

from __future__ import annotations

import time
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status

from .crypto import verify_request
from .storage import NotFoundError, Storage


def _unauthorized(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"error": "unauthorized", "message": message},
    )


def _gone(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_410_GONE,
        detail={"error": "gone", "message": message},
    )


async def require_signed_request(
    request: Request,
    x_agent_id: Annotated[str | None, Header(alias="X-Agent-Id")] = None,
    x_timestamp: Annotated[str | None, Header(alias="X-Timestamp")] = None,
    x_nonce: Annotated[str | None, Header(alias="X-Nonce")] = None,
    x_signature: Annotated[str | None, Header(alias="X-Signature")] = None,
) -> dict:
    """Resolve & verify a signed write request. Returns the agent row."""
    if not (x_agent_id and x_timestamp and x_nonce and x_signature):
        raise _unauthorized("missing X-Agent-Id / X-Timestamp / X-Nonce / X-Signature")

    try:
        ts = int(x_timestamp)
    except ValueError:
        raise _unauthorized("X-Timestamp must be unix seconds (int)") from None

    # Body has to be read once; FastAPI caches it so Pydantic can re-use
    body = await request.body()

    storage: Storage = request.app.state.storage
    agent = storage.get_by_id(x_agent_id)
    if agent is None:
        raise _unauthorized(f"unknown agent id: {x_agent_id}")

    # Verify signature
    try:
        verify_request(
            public_key_string=agent["public_key"],
            signature_b64=x_signature,
            timestamp=ts,
            method=request.method,
            path=request.url.path,
            body=body,
            now=int(time.time()),
        )
    except ValueError as e:
        msg = str(e)
        if "timestamp" in msg:
            raise _gone(msg) from None
        raise _unauthorized(msg) from None

    # Anti-replay: nonce must be fresh
    if not storage.consume_nonce(x_nonce):
        raise _gone(f"nonce already used: {x_nonce}")

    # Stash for handlers
    request.state.signed_agent = agent
    return agent


SignedAgentDep = Annotated[dict, Depends(require_signed_request)]
