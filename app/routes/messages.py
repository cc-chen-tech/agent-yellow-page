"""/v0/messages routes (mailbox)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, status
from ulid import ULID

from ..models import Message, MessageList, MessagePatch, MessageSend
from ..signer import SignedAgentDep
from ..storage import MessageStore, Storage

router = APIRouter(prefix="/v0/messages", tags=["messages"])


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


def _forbidden(what: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"error": "forbidden", "message": what},
    )


def _store(request: Request) -> MessageStore:
    s: Storage = request.app.state.storage
    return s.messages  # attached by main.py


def _row_to_message(row: dict) -> Message:
    return Message(
        id=row["id"],
        thread_id=row["thread_id"],
        in_reply_to=row.get("in_reply_to"),
        sender_id=row["sender_id"],
        sender_name=row.get("sender_name", "?"),
        recipient_id=row["recipient_id"],
        recipient_name=row.get("recipient_name", "?"),
        subject=row.get("subject"),
        body=row["body"],
        created_at=row["created_at"],
        read_at=row.get("read_at"),
    )


# --- POST /v0/messages ---------------------------------------------------- #


@router.post("", status_code=status.HTTP_201_CREATED, response_model=Message)
async def send_message(
    request: Request,
    payload: MessageSend,
    sender: SignedAgentDep,
) -> Message:
    storage: Storage = request.app.state.storage
    msg_store = storage.messages

    # Resolve recipient
    recipient = storage.find_by_id_or_name(payload.recipient_id)
    if recipient is None:
        raise _not_found(f"recipient not found: {payload.recipient_id}")
    if recipient["id"] == sender["id"]:
        raise _bad_request("cannot send a message to yourself")

    # Resolve thread_id
    thread_id: str
    if payload.in_reply_to:
        parent = msg_store.get_by_id(payload.in_reply_to)
        if parent is None:
            raise _not_found(f"in_reply_to message not found: {payload.in_reply_to}")
        if sender["id"] not in (parent["sender_id"], parent["recipient_id"]):
            raise _forbidden(
                "you are not a participant of the message you are replying to"
            )
        thread_id = parent["thread_id"]
    else:
        thread_id = str(ULID())  # will be overwritten with self id below

    message_id = str(ULID())
    if not payload.in_reply_to:
        # Self-id as thread root
        thread_id = message_id

    row = msg_store.insert(
        message_id=message_id,
        thread_id=thread_id,
        sender_id=sender["id"],
        recipient_id=recipient["id"],
        payload=payload,
    )
    return _row_to_message(row)


# --- GET /v0/messages/inbox ---------------------------------------------- #


@router.get("/inbox", response_model=MessageList)
async def inbox(
    request: Request,
    agent: SignedAgentDep,
    unread: Annotated[bool, Query(description="only unread")] = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> MessageList:
    storage: Storage = request.app.state.storage
    total, unread_count, rows = storage.messages.inbox(
        agent["id"], unread_only=unread, limit=limit, offset=offset
    )
    return MessageList(
        total=total,
        unread=unread_count,
        items=[_row_to_message(r) for r in rows],
    )


# --- GET /v0/messages/outbox --------------------------------------------- #


@router.get("/outbox", response_model=MessageList)
async def outbox(
    request: Request,
    agent: SignedAgentDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> MessageList:
    storage: Storage = request.app.state.storage
    total, rows = storage.messages.outbox(agent["id"], limit=limit, offset=offset)
    return MessageList(
        total=total,
        unread=0,
        items=[_row_to_message(r) for r in rows],
    )


# --- GET /v0/messages/{id} ----------------------------------------------- #


@router.get("/{message_id}", response_model=Message)
async def get_message(
    request: Request,
    message_id: str,
    agent: SignedAgentDep,
) -> Message:
    storage: Storage = request.app.state.storage
    msg = storage.messages.get_by_id(message_id)
    if msg is None:
        raise _not_found(f"message not found: {message_id}")
    if not storage.messages.is_participant(msg, agent["id"]):
        raise _forbidden("you are not a participant of this message")
    return _row_to_message(msg)


# --- PATCH /v0/messages/{id} --------------------------------------------- #


@router.patch("/{message_id}", response_model=Message)
async def patch_message(
    request: Request,
    message_id: str,
    payload: MessagePatch,
    agent: SignedAgentDep,
) -> Message:
    storage: Storage = request.app.state.storage
    msg = storage.messages.get_by_id(message_id)
    if msg is None:
        raise _not_found(f"message not found: {message_id}")
    if msg["recipient_id"] != agent["id"]:
        raise _forbidden("only the recipient can update a message")

    if payload.action == "mark_read":
        storage.messages.mark_read(message_id)

    msg = storage.messages.get_by_id(message_id)
    assert msg is not None
    return _row_to_message(msg)


# --- DELETE /v0/messages/{id} -------------------------------------------- #


@router.delete("/{message_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_message(
    request: Request,
    message_id: str,
    agent: SignedAgentDep,
) -> None:
    storage: Storage = request.app.state.storage
    msg = storage.messages.get_by_id(message_id)
    if msg is None:
        raise _not_found(f"message not found: {message_id}")
    if msg["recipient_id"] != agent["id"]:
        raise _forbidden("only the recipient can delete a message")
    storage.messages.delete(message_id)


# --- GET /v0/threads/{thread_id} ----------------------------------------- #


threads_router = APIRouter(prefix="/v0/threads", tags=["threads"])


@threads_router.get("/{thread_id}", response_model=list[Message])
async def get_thread(
    request: Request,
    thread_id: str,
    agent: SignedAgentDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> list[Message]:
    storage: Storage = request.app.state.storage
    rows = storage.messages.thread(thread_id, limit=limit)
    if not rows:
        # Could be empty thread (deleted) or non-existent — distinguish by checking
        # whether any participant exists. For now, return [] for any unknown thread_id
        # to avoid leaking existence info.
        return []
    if not storage.messages.is_participant(rows[0], agent["id"]):
        raise _forbidden("you are not a participant of this thread")
    return [_row_to_message(r) for r in rows]
