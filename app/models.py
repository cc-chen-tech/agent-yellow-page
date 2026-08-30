"""Pydantic models — request/response shapes for the public API."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from .crypto import KEY_PREFIX, verify_public_key

# --------------------------------------------------------------------------- #
# Field-level constraints
# --------------------------------------------------------------------------- #

NAME_PATTERN = r"^[a-z0-9](?:[a-z0-9-]{1,62}[a-z0-9])?$"
TAG_PATTERN = r"^[a-z0-9-]{1,32}$"
ENDPOINT_PATTERN = r"^https?://.+"

NameStr = Annotated[
    str,
    StringConstraints(min_length=3, max_length=64, pattern=NAME_PATTERN),
]
TagStr = Annotated[
    str,
    StringConstraints(min_length=1, max_length=32, pattern=TAG_PATTERN),
]
EndpointStr = Annotated[
    str,
    StringConstraints(min_length=1, max_length=2048, pattern=ENDPOINT_PATTERN),
]
PublicKeyStr = Annotated[
    str,
    StringConstraints(min_length=len(KEY_PREFIX) + 1, max_length=128),
]

MAX_METADATA_BYTES = 4 * 1024


# --------------------------------------------------------------------------- #
# Public schema
# --------------------------------------------------------------------------- #


class AgentBase(BaseModel):
    """Fields every agent has. Subset of AgentCard used for create/update."""

    model_config = ConfigDict(extra="forbid")

    name: NameStr
    display_name: str | None = Field(default=None, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    endpoint: EndpointStr | None = None
    public_key: PublicKeyStr
    tags: list[TagStr] = Field(default_factory=list, max_length=32)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("public_key")
    @classmethod
    def _check_public_key(cls, v: str) -> str:
        if not verify_public_key(v):
            raise ValueError("public_key must be 'ed25519:' + base64(32 raw bytes)")
        return v

    @field_validator("metadata")
    @classmethod
    def _check_metadata_size(cls, v: dict[str, Any]) -> dict[str, Any]:
        import json

        size = len(json.dumps(v, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if size > MAX_METADATA_BYTES:
            raise ValueError(
                f"metadata serialized size {size} exceeds {MAX_METADATA_BYTES} bytes"
            )
        return v


class AgentCreate(AgentBase):
    """Body of POST /agents."""


class AgentPatch(BaseModel):
    """Body of PATCH /agents/{id}. All fields optional; public_key CANNOT change."""

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    endpoint: EndpointStr | None = None
    tags: list[TagStr] | None = Field(default=None, max_length=32)
    metadata: dict[str, Any] | None = None

    @field_validator("metadata")
    @classmethod
    def _check_metadata_size(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        if v is None:
            return v
        import json

        size = len(json.dumps(v, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if size > MAX_METADATA_BYTES:
            raise ValueError(
                f"metadata serialized size {size} exceeds {MAX_METADATA_BYTES} bytes"
            )
        return v


class AgentCard(AgentBase):
    """Public-facing agent record (server-assigned fields included)."""

    id: str
    version: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "AgentCard":
        from .storage import _loads  # local to avoid cycle

        return cls(
            id=row["id"],
            name=row["name"],
            display_name=row.get("display_name"),
            description=row.get("description"),
            endpoint=row.get("endpoint"),
            public_key=row["public_key"],
            tags=_loads(row["tags_json"], list),
            metadata=_loads(row["metadata_json"], dict),
            version=row["version"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


class AgentList(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[AgentCard]


class ChallengeResponse(BaseModel):
    challenge: str
    expires_at: datetime


# --- Mailbox schemas ------------------------------------------------------ #


class MessageSend(BaseModel):
    """Body of POST /v0/messages."""

    model_config = ConfigDict(extra="forbid")

    recipient_id: str = Field(
        ..., min_length=1, max_length=64,
        description="recipient's id (ULID) or name (slug)",
    )
    subject: str | None = Field(default=None, max_length=200)
    body: str = Field(..., min_length=1, max_length=32 * 1024)
    in_reply_to: str | None = Field(default=None, max_length=64)


class Message(BaseModel):
    """A stored message, server-side fields included."""

    id: str
    thread_id: str
    in_reply_to: str | None
    sender_id: str
    sender_name: str
    recipient_id: str
    recipient_name: str
    subject: str | None
    body: str
    created_at: datetime
    read_at: datetime | None


class MessageList(BaseModel):
    total: int
    unread: int
    items: list[Message]


class MessagePatch(BaseModel):
    """Body of PATCH /v0/messages/{id}."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["mark_read"]


# --- Chatroom schemas ----------------------------------------------------- #


class ChatMessageCreate(BaseModel):
    """Body of POST /v0/chat."""

    model_config = ConfigDict(extra="forbid")

    body: str = Field(..., min_length=1, max_length=4 * 1024)


class ChatMessage(BaseModel):
    """A single chatroom message."""

    id: str
    sender_id: str
    sender_name: str
    body: str
    created_at: datetime


class ChatMessageList(BaseModel):
    total: int
    items: list[ChatMessage]


# --- Private chatroom schemas -------------------------------------------- #


class PrivateChatroomCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: NameStr
    display_name: str | None = Field(default=None, max_length=128)
    description: str | None = Field(default=None, max_length=2000)


class PrivateChatroom(BaseModel):
    """Public-facing view of a private chatroom."""

    id: str
    name: str
    display_name: str | None
    description: str | None
    creator_id: str
    creator_name: str
    member_count: int
    created_at: datetime


class PrivateChatroomList(BaseModel):
    total: int
    items: list[PrivateChatroom]


class PrivateInviteCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_uses: int | None = Field(default=None, ge=1, le=10000)
    expires_in_seconds: int | None = Field(default=None, ge=1, le=30 * 86400)


class PrivateInvite(BaseModel):
    code: str
    chatroom_id: str
    created_by: str
    created_at: datetime
    expires_at: datetime | None
    max_uses: int | None
    used_count: int


class PrivateJoinRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(..., min_length=4, max_length=64)


class PrivateMember(BaseModel):
    agent_id: str
    name: str
    joined_at: datetime
    invited_by: str | None


class PrivateMemberList(BaseModel):
    total: int
    items: list[PrivateMember]


class PrivateChatMessageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str = Field(..., min_length=1, max_length=4 * 1024)


class PrivateChatMessage(BaseModel):
    id: str
    chatroom_id: str
    sender_id: str
    sender_name: str
    body: str
    created_at: datetime


class PrivateChatMessageList(BaseModel):
    total: int
    items: list[PrivateChatMessage]


class ErrorBody(BaseModel):
    error: str
    message: str


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Compile patterns once at import time for faster validation.
_NAME_RE = re.compile(NAME_PATTERN)
_TAG_RE = re.compile(TAG_PATTERN)
_ENDPOINT_RE = re.compile(ENDPOINT_PATTERN)


def quick_name_check(s: str) -> bool:
    return bool(_NAME_RE.match(s))


def quick_tag_check(s: str) -> bool:
    return bool(_TAG_RE.match(s))


def quick_endpoint_check(s: str) -> bool:
    return bool(_ENDPOINT_RE.match(s))


# Lightweight re-export so callers can `from app.models import Field, ...`
__all__ = [
    "AgentBase",
    "AgentCreate",
    "AgentPatch",
    "AgentCard",
    "AgentList",
    "ChallengeResponse",
    "ErrorBody",
    "utcnow",
    "Field",
    "BaseModel",
    "model_validator",
    "Literal",
]
