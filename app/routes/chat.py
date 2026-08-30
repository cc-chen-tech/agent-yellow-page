"""/v0/chat routes — public chatroom.

Writes are signed; reads are public.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, status
from ulid import ULID

from ..models import ChatMessage, ChatMessageCreate, ChatMessageList
from ..signer import SignedAgentDep
from ..storage import MessageStore, Storage

router = APIRouter(prefix="/v0/chat", tags=["chat"])


def _not_found(what: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": "not_found", "message": what},
    )


def _forbidden(what: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"error": "forbidden", "message": what},
    )


def _row_to_chat(row: dict) -> ChatMessage:
    return ChatMessage(
        id=row["id"],
        sender_id=row["sender_id"],
        sender_name=row.get("sender_name", "?"),
        body=row["body"],
        created_at=row["created_at"],
    )


# --- POST /v0/chat ------------------------------------------------------- #


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ChatMessage)
async def post_chat(
    request: Request,
    payload: ChatMessageCreate,
    sender: SignedAgentDep,
) -> ChatMessage:
    storage: Storage = request.app.state.storage
    msg_id = str(ULID())
    row = storage.chat.insert(msg_id, sender_id=sender["id"], body=payload.body)
    return _row_to_chat(row)


# --- GET /v0/chat -------------------------------------------------------- #


@router.get("", response_model=ChatMessageList)
async def list_chat(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ChatMessageList:
    storage: Storage = request.app.state.storage
    total, rows = storage.chat.list(limit=limit, offset=offset)
    return ChatMessageList(
        total=total,
        items=[_row_to_chat(r) for r in rows],
    )


# --- GET /v0/chat/{id} --------------------------------------------------- #


@router.get("/{message_id}", response_model=ChatMessage)
async def get_chat(
    request: Request,
    message_id: str,
) -> ChatMessage:
    storage: Storage = request.app.state.storage
    row = storage.chat.get_by_id(message_id)
    if row is None:
        raise _not_found(f"chat message not found: {message_id}")
    return _row_to_chat(row)


# --- DELETE /v0/chat/{id} ------------------------------------------------ #


@router.delete("/{message_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_chat(
    request: Request,
    message_id: str,
    agent: SignedAgentDep,
) -> None:
    storage: Storage = request.app.state.storage
    row = storage.chat.get_by_id(message_id)
    if row is None:
        raise _not_found(f"chat message not found: {message_id}")
    if row["sender_id"] != agent["id"]:
        raise _forbidden("only the sender can delete a chat message")
    storage.chat.delete(message_id)
