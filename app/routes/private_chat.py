"""/v0/private-chatrooms routes — existence public, content member-only."""

from __future__ import annotations

import base64
import os
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, status
from ulid import ULID

from ..models import (
    PrivateChatMessage,
    PrivateChatMessageCreate,
    PrivateChatMessageList,
    PrivateChatroom,
    PrivateChatroomCreate,
    PrivateChatroomList,
    PrivateInvite,
    PrivateInviteCreate,
    PrivateJoinRequest,
    PrivateMember,
    PrivateMemberList,
)
from ..signer import SignedAgentDep
from ..storage import (
    ConflictError,
    NotFoundError,
    PrivateChatStore,
    Storage,
)

router = APIRouter(prefix="/v0/private-chatrooms", tags=["private-chatrooms"])


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


def _conflict(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"error": "conflict", "message": message},
    )


def _gone(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_410_GONE,
        detail={"error": "gone", "message": message},
    )


def _store(request: Request) -> PrivateChatStore:
    s: Storage = request.app.state.storage
    return s.private_chat


def _resolve_room(id_or_name: str, store: PrivateChatStore) -> dict:
    room = store.find_room(id_or_name)
    if room is None:
        raise _not_found(f"private chatroom not found: {id_or_name}")
    return room


def _row_to_room(row: dict) -> PrivateChatroom:
    return PrivateChatroom(
        id=row["id"],
        name=row["name"],
        display_name=row.get("display_name"),
        description=row.get("description"),
        creator_id=row["creator_id"],
        creator_name=row.get("creator_name", "?"),
        member_count=row.get("member_count", 0),
        created_at=row["created_at"],
    )


def _row_to_invite(row: dict) -> PrivateInvite:
    return PrivateInvite(
        code=row["code"],
        chatroom_id=row["chatroom_id"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        expires_at=row.get("expires_at"),
        max_uses=row.get("max_uses"),
        used_count=row["used_count"],
    )


def _row_to_message(row: dict) -> PrivateChatMessage:
    return PrivateChatMessage(
        id=row["id"],
        chatroom_id=row["chatroom_id"],
        sender_id=row["sender_id"],
        sender_name=row.get("sender_name", "?"),
        body=row["body"],
        created_at=row["created_at"],
    )


# --- POST /v0/private-chatrooms (create) --------------------------------- #


@router.post("", status_code=status.HTTP_201_CREATED, response_model=PrivateChatroom)
async def create_room(
    request: Request,
    payload: PrivateChatroomCreate,
    creator: SignedAgentDep,
) -> PrivateChatroom:
    store = _store(request)
    room_id = str(ULID())
    try:
        row = store.create_room(
            room_id, payload.name, creator["id"],
            display_name=payload.display_name, description=payload.description,
        )
    except ConflictError as e:
        raise _conflict(f"name already taken: {payload.name}") from e
    return _row_to_room(row)


# --- GET /v0/private-chatrooms (public list) ----------------------------- #


@router.get("", response_model=PrivateChatroomList)
async def list_rooms(
    request: Request,
    q: Annotated[str | None, Query(max_length=128, description="substring search")] = None,
    creator: Annotated[str | None, Query(max_length=64, description="id or name")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PrivateChatroomList:
    store = _store(request)
    storage: Storage = request.app.state.storage
    creator_id = None
    if creator:
        agent = storage.find_by_id_or_name(creator)
        if agent is None:
            raise _not_found(f"creator not found: {creator}")
        creator_id = agent["id"]
    total, rows = store.list_rooms(q=q, creator_id=creator_id, limit=limit, offset=offset)
    return PrivateChatroomList(
        total=total,
        items=[_row_to_room(r) for r in rows],
    )


# --- GET /v0/private-chatrooms/{id_or_name} ------------------------------ #


@router.get("/{id_or_name}", response_model=PrivateChatroom)
async def get_room(
    request: Request,
    id_or_name: str,
) -> PrivateChatroom:
    store = _store(request)
    return _row_to_room(_resolve_room(id_or_name, store))


# --- POST /v0/private-chatrooms/{id}/invites (creator only) -------------- #


@router.post(
    "/{id_or_name}/invites", status_code=status.HTTP_201_CREATED, response_model=PrivateInvite
)
async def create_invite(
    request: Request,
    id_or_name: str,
    payload: PrivateInviteCreate,
    creator: SignedAgentDep,
) -> PrivateInvite:
    store = _store(request)
    room = _resolve_room(id_or_name, store)
    if room["creator_id"] != creator["id"]:
        raise _forbidden("only the creator can issue invites")
    code = base64.b64encode(os.urandom(16)).decode("ascii")
    expires_at = None
    if payload.expires_in_seconds is not None:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=payload.expires_in_seconds)
    row = store.create_invite(
        code=code, room_id=room["id"], created_by=creator["id"],
        max_uses=payload.max_uses, expires_at=expires_at,
    )
    return _row_to_invite(row)


# --- GET /v0/private-chatrooms/{id}/invites (creator only) --------------- #


@router.get("/{id_or_name}/invites", response_model=list[PrivateInvite])
async def list_invites(
    request: Request,
    id_or_name: str,
    creator: SignedAgentDep,
) -> list[PrivateInvite]:
    store = _store(request)
    room = _resolve_room(id_or_name, store)
    if room["creator_id"] != creator["id"]:
        raise _forbidden("only the creator can list invites")
    return [_row_to_invite(r) for r in store.list_invites_for_room(room["id"])]


# --- POST /v0/private-chatrooms/{id}/join -------------------------------- #


@router.post(
    "/{id_or_name}/join", status_code=status.HTTP_201_CREATED, response_model=PrivateChatroom
)
async def join_room(
    request: Request,
    id_or_name: str,
    payload: PrivateJoinRequest,
    joiner: SignedAgentDep,
) -> PrivateChatroom:
    store = _store(request)
    room = _resolve_room(id_or_name, store)
    if store.is_member(room["id"], joiner["id"]):
        raise _conflict("you are already a member of this chatroom")
    consumed = store.consume_invite(payload.code)
    if consumed is None:
        raise _gone("invite is invalid, expired, or exhausted")
    if consumed["chatroom_id"] != room["id"]:
        raise _bad_request("invite is for a different chatroom")
    # Find the inviter (creator) for the joined_by column
    inviter_id = consumed["created_by"]
    store.add_member(room["id"], joiner["id"], invited_by=inviter_id)
    return _row_to_room(store.get_room(room["id"]))


# --- POST /v0/private-chatrooms/{id}/leave ------------------------------- #


@router.post("/{id_or_name}/leave", status_code=status.HTTP_204_NO_CONTENT)
async def leave_room(
    request: Request,
    id_or_name: str,
    member: SignedAgentDep,
) -> None:
    store = _store(request)
    room = _resolve_room(id_or_name, store)
    if not store.is_member(room["id"], member["id"]):
        raise _forbidden("you are not a member of this chatroom")
    if not store.remove_member(room["id"], member["id"]):
        raise _not_found("not a member")


# --- GET /v0/private-chatrooms/{id}/members (member only) --------------- #


@router.get("/{id_or_name}/members", response_model=PrivateMemberList)
async def list_members(
    request: Request,
    id_or_name: str,
    member: SignedAgentDep,
) -> PrivateMemberList:
    store = _store(request)
    room = _resolve_room(id_or_name, store)
    if not store.is_member(room["id"], member["id"]):
        raise _forbidden("only members can see the member list")
    members = store.list_members(room["id"])
    return PrivateMemberList(
        total=len(members),
        items=[
            PrivateMember(
                agent_id=m["agent_id"],
                name=m["name"],
                joined_at=m["joined_at"],
                invited_by=m["invited_by"],
            )
            for m in members
        ],
    )


# --- POST /v0/private-chatrooms/{id}/messages (member only) ------------- #


@router.post(
    "/{id_or_name}/messages",
    status_code=status.HTTP_201_CREATED,
    response_model=PrivateChatMessage,
)
async def send_message(
    request: Request,
    id_or_name: str,
    payload: PrivateChatMessageCreate,
    sender: SignedAgentDep,
) -> PrivateChatMessage:
    store = _store(request)
    room = _resolve_room(id_or_name, store)
    if not store.is_member(room["id"], sender["id"]):
        raise _forbidden("only members can post messages")
    msg_id = str(ULID())
    row = store.insert_message(msg_id, room["id"], sender["id"], payload.body)
    return _row_to_message(row)


# --- GET /v0/private-chatrooms/{id}/messages (member only) --------------- #


@router.get(
    "/{id_or_name}/messages", response_model=PrivateChatMessageList
)
async def list_messages(
    request: Request,
    id_or_name: str,
    member: SignedAgentDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PrivateChatMessageList:
    store = _store(request)
    room = _resolve_room(id_or_name, store)
    if not store.is_member(room["id"], member["id"]):
        raise _forbidden("only members can read messages")
    total, rows = store.list_messages(room["id"], limit=limit, offset=offset)
    return PrivateChatMessageList(
        total=total,
        items=[_row_to_message(r) for r in rows],
    )


# --- GET / DELETE single message (member / sender) ---------------------- #


@router.get(
    "/{id_or_name}/messages/{message_id}", response_model=PrivateChatMessage
)
async def get_message(
    request: Request,
    id_or_name: str,
    message_id: str,
    member: SignedAgentDep,
) -> PrivateChatMessage:
    store = _store(request)
    room = _resolve_room(id_or_name, store)
    if not store.is_member(room["id"], member["id"]):
        raise _forbidden("only members can read messages")
    msg = store.get_message(message_id)
    if msg is None or msg["chatroom_id"] != room["id"]:
        raise _not_found(f"message not found: {message_id}")
    return _row_to_message(msg)


@router.delete(
    "/{id_or_name}/messages/{message_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_message(
    request: Request,
    id_or_name: str,
    message_id: str,
    sender: SignedAgentDep,
) -> None:
    store = _store(request)
    room = _resolve_room(id_or_name, store)
    msg = store.get_message(message_id)
    if msg is None or msg["chatroom_id"] != room["id"]:
        raise _not_found(f"message not found: {message_id}")
    if msg["sender_id"] != sender["id"]:
        raise _forbidden("only the sender can delete a message")
    store.delete_message(message_id)


# --- DELETE /v0/private-chatrooms/{id} (creator only) ------------------- #


@router.delete("/{id_or_name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_room(
    request: Request,
    id_or_name: str,
    creator: SignedAgentDep,
) -> None:
    store = _store(request)
    room = _resolve_room(id_or_name, store)
    if room["creator_id"] != creator["id"]:
        raise _forbidden("only the creator can delete the chatroom")
    store.delete_room(room["id"])
