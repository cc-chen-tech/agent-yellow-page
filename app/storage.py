"""SQLite storage layer — agents + nonces."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

from .crypto import NONCE_TTL_SECONDS
from .models import AgentCreate, AgentPatch, MessageSend, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
  id            TEXT PRIMARY KEY,
  name          TEXT NOT NULL UNIQUE,
  display_name  TEXT,
  description   TEXT,
  endpoint      TEXT,
  public_key    TEXT NOT NULL UNIQUE,
  tags_json     TEXT NOT NULL DEFAULT '[]',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  version       INTEGER NOT NULL DEFAULT 1,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agents_name ON agents(name);
CREATE INDEX IF NOT EXISTS idx_agents_public_key ON agents(public_key);

CREATE TABLE IF NOT EXISTS nonces (
  nonce       TEXT PRIMARY KEY,
  expires_at  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_nonces_expires ON nonces(expires_at);

CREATE TABLE IF NOT EXISTS messages (
  id            TEXT PRIMARY KEY,
  thread_id     TEXT NOT NULL,
  in_reply_to   TEXT,
  sender_id     TEXT NOT NULL,
  recipient_id  TEXT NOT NULL,
  subject       TEXT,
  body          TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  read_at       TEXT,
  FOREIGN KEY (sender_id) REFERENCES agents(id) ON DELETE CASCADE,
  FOREIGN KEY (recipient_id) REFERENCES agents(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_msg_recipient ON messages(recipient_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_msg_sender ON messages(sender_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_msg_thread ON messages(thread_id, created_at);
"""


def _loads(s: str, default):
    try:
        v = json.loads(s)
    except (TypeError, ValueError):
        return default
    return v


class Storage:
    """Thin wrapper around a single sqlite3 connection.

    Uses WAL + per-call transactions. Single-writer friendly; the public
    service is a single process so a connection lock is enough.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._migrate()
        # Sub-stores
        self.messages = MessageStore(self)

    def _migrate(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _tx(self):
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    # --- nonces ------------------------------------------------------------ #

    def consume_nonce(self, nonce: str, now: int | None = None) -> bool:
        """Record a nonce. Returns True on first use, False if already seen.

        Also garbage-collects expired nonces.
        """
        if now is None:
            now = int(time.time())
        with self._tx() as conn:
            conn.execute(
                "DELETE FROM nonces WHERE expires_at < ?", (now,)
            )
            cur = conn.execute(
                "INSERT OR IGNORE INTO nonces(nonce, expires_at) VALUES (?, ?)",
                (nonce, now + NONCE_TTL_SECONDS),
            )
            return cur.rowcount == 1

    # --- agents ------------------------------------------------------------ #

    def get_by_id(self, agent_id: str) -> dict | None:
        with self._lock:
            cur = self._conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    def get_by_name(self, name: str) -> dict | None:
        with self._lock:
            cur = self._conn.execute("SELECT * FROM agents WHERE name = ?", (name,))
            row = cur.fetchone()
            return dict(row) if row else None

    def get_by_public_key(self, public_key: str) -> dict | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM agents WHERE public_key = ?", (public_key,)
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def find_by_id_or_name(self, id_or_name: str) -> dict | None:
        if not id_or_name:
            return None
        # 26-char ULID-ish vs name pattern
        if len(id_or_name) == 26 and id_or_name.isalnum() and id_or_name.isupper():
            return self.get_by_id(id_or_name)
        return self.get_by_name(id_or_name)

    def insert(self, agent_id: str, payload: AgentCreate) -> dict:
        now = utcnow()
        row = {
            "id": agent_id,
            "name": payload.name,
            "display_name": payload.display_name,
            "description": payload.description,
            "endpoint": payload.endpoint,
            "public_key": payload.public_key,
            "tags_json": json.dumps(payload.tags, ensure_ascii=False),
            "metadata_json": json.dumps(payload.metadata, ensure_ascii=False),
            "version": 1,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }
        with self._tx() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO agents (
                        id, name, display_name, description, endpoint, public_key,
                        tags_json, metadata_json, version, created_at, updated_at
                    ) VALUES (
                        :id, :name, :display_name, :description, :endpoint, :public_key,
                        :tags_json, :metadata_json, :version, :created_at, :updated_at
                    )
                    """,
                    row,
                )
            except sqlite3.IntegrityError as e:
                # UNIQUE violated
                msg = str(e).lower()
                if "name" in msg:
                    raise ConflictError("name", payload.name) from e
                if "public_key" in msg:
                    raise ConflictError("public_key", "<hidden>") from e
                raise ConflictError("field", "<hidden>") from e
        return row

    def update_full(
        self, agent_id: str, expected_version: int, payload: AgentCreate
    ) -> dict:
        """PUT-style full replace. Bumps version. Returns new row."""
        now = utcnow().isoformat()
        new_tags = json.dumps(payload.tags, ensure_ascii=False)
        new_meta = json.dumps(payload.metadata, ensure_ascii=False)
        with self._tx() as conn:
            cur = conn.execute(
                """
                UPDATE agents
                SET name = ?, display_name = ?, description = ?, endpoint = ?,
                    tags_json = ?, metadata_json = ?, version = version + 1,
                    updated_at = ?
                WHERE id = ? AND version = ?
                """,
                (
                    payload.name,
                    payload.display_name,
                    payload.description,
                    payload.endpoint,
                    new_tags,
                    new_meta,
                    now,
                    agent_id,
                    expected_version,
                ),
            )
            if cur.rowcount == 0:
                # Distinguish not-found from version-mismatch
                if self.get_by_id(agent_id) is None:
                    raise NotFoundError(agent_id)
                raise VersionConflictError(agent_id, expected_version)
            # Note: name/public_key remain unchanged in full-replace path —
            # if you want rename, do PATCH + delete + insert; for now we keep id-keyed.
        row = self.get_by_id(agent_id)
        assert row is not None
        return row

    def update_partial(
        self, agent_id: str, expected_version: int, patch: AgentPatch
    ) -> dict:
        """PATCH-style partial update."""
        sets: list[str] = []
        params: list[Any] = []
        if patch.display_name is not None:
            sets.append("display_name = ?")
            params.append(patch.display_name)
        if patch.description is not None:
            sets.append("description = ?")
            params.append(patch.description)
        if patch.endpoint is not None:
            sets.append("endpoint = ?")
            params.append(patch.endpoint)
        if patch.tags is not None:
            sets.append("tags_json = ?")
            params.append(json.dumps(patch.tags, ensure_ascii=False))
        if patch.metadata is not None:
            sets.append("metadata_json = ?")
            params.append(json.dumps(patch.metadata, ensure_ascii=False))
        if not sets:
            # Nothing to update; return current row
            row = self.get_by_id(agent_id)
            if row is None:
                raise NotFoundError(agent_id)
            return row
        sets.append("version = version + 1")
        sets.append("updated_at = ?")
        params.append(utcnow().isoformat())
        params.append(agent_id)
        params.append(expected_version)
        with self._tx() as conn:
            cur = conn.execute(
                f"UPDATE agents SET {', '.join(sets)} WHERE id = ? AND version = ?",
                params,
            )
            if cur.rowcount == 0:
                if self.get_by_id(agent_id) is None:
                    raise NotFoundError(agent_id)
                raise VersionConflictError(agent_id, expected_version)
        row = self.get_by_id(agent_id)
        assert row is not None
        return row

    def delete(self, agent_id: str) -> bool:
        with self._tx() as conn:
            cur = conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
            return cur.rowcount == 1

    def list(
        self,
        *,
        q: str | None = None,
        tags: Iterable[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[int, list[dict]]:
        where: list[str] = []
        params: list[Any] = []
        if q:
            like = f"%{q.lower()}%"
            where.append("(LOWER(name) LIKE ? OR LOWER(display_name) LIKE ? OR LOWER(description) LIKE ?)")
            params.extend([like, like, like])
        if tags:
            # AND semantics: each tag must appear in tags_json — cheap substring match
            for t in tags:
                where.append("tags_json LIKE ?")
                # JSON array stores tags as quoted strings; this matches e.g. "tag" or ,"tag"
                params.append(f'%"{t}"%')
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        # Count
        with self._lock:
            total = self._conn.execute(
                f"SELECT COUNT(*) AS c FROM agents {where_sql}", params
            ).fetchone()["c"]
            cur = self._conn.execute(
                f"SELECT * FROM agents {where_sql} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                params + [limit, offset],
            )
            rows = [dict(r) for r in cur.fetchall()]
        return total, rows


# --- storage-level exceptions --------------------------------------------- #


class NotFoundError(Exception):
    pass


class ConflictError(Exception):
    def __init__(self, field: str, value: str):
        super().__init__(f"{field} conflict: {value}")
        self.field = field
        self.value = value


class VersionConflictError(Exception):
    def __init__(self, agent_id: str, expected: int):
        super().__init__(f"version conflict on {agent_id}: expected {expected}")
        self.agent_id = agent_id
        self.expected = expected


class ForbiddenError(Exception):
    """Caller is not allowed to act on this resource."""


# --- messages ------------------------------------------------------------- #


class MessageStore:
    """CRUD for the messages table. Operates on dict rows."""

    def __init__(self, storage: "Storage"):
        self._s = storage

    @property
    def _conn(self):
        return self._s._conn

    @property
    def _lock(self):
        return self._s._lock

    @contextmanager
    def _tx(self):
        # delegate to storage's _tx to share the same connection/lock
        with self._s._tx() as conn:
            yield conn

    def _hydrate(self, row: dict) -> dict:
        """Join sender_name / recipient_name from agents table."""
        sender = self._s.get_by_id(row["sender_id"]) or {}
        recipient = self._s.get_by_id(row["recipient_id"]) or {}
        return {
            **row,
            "sender_name": sender.get("name", "?"),
            "recipient_name": recipient.get("name", "?"),
        }

    def insert(
        self,
        message_id: str,
        thread_id: str,
        sender_id: str,
        recipient_id: str,
        payload: MessageSend,
    ) -> dict:
        now = utcnow()
        row = {
            "id": message_id,
            "thread_id": thread_id,
            "in_reply_to": payload.in_reply_to,
            "sender_id": sender_id,
            "recipient_id": recipient_id,
            "subject": payload.subject,
            "body": payload.body,
            "created_at": now.isoformat(),
            "read_at": None,
        }
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO messages (
                    id, thread_id, in_reply_to, sender_id, recipient_id,
                    subject, body, created_at, read_at
                ) VALUES (
                    :id, :thread_id, :in_reply_to, :sender_id, :recipient_id,
                    :subject, :body, :created_at, :read_at
                )
                """,
                row,
            )
        return self._hydrate(row)

    def get_by_id(self, message_id: str) -> dict | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM messages WHERE id = ?", (message_id,)
            )
            row = cur.fetchone()
            if row is None:
                return None
            return self._hydrate(dict(row))

    def inbox(
        self,
        recipient_id: str,
        *,
        unread_only: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[int, int, list[dict]]:
        where = "recipient_id = ?"
        params: list[Any] = [recipient_id]
        if unread_only:
            where += " AND read_at IS NULL"
        with self._lock:
            total = self._conn.execute(
                f"SELECT COUNT(*) AS c FROM messages WHERE {where}", params
            ).fetchone()["c"]
            unread = self._conn.execute(
                "SELECT COUNT(*) AS c FROM messages WHERE recipient_id = ? AND read_at IS NULL",
                [recipient_id],
            ).fetchone()["c"]
            cur = self._conn.execute(
                f"SELECT * FROM messages WHERE {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                params + [limit, offset],
            )
            rows = [self._hydrate(dict(r)) for r in cur.fetchall()]
        return total, unread, rows

    def outbox(
        self,
        sender_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[int, list[dict]]:
        with self._lock:
            total = self._conn.execute(
                "SELECT COUNT(*) AS c FROM messages WHERE sender_id = ?",
                [sender_id],
            ).fetchone()["c"]
            cur = self._conn.execute(
                "SELECT * FROM messages WHERE sender_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                [sender_id, limit, offset],
            )
            rows = [self._hydrate(dict(r)) for r in cur.fetchall()]
        return total, rows

    def thread(
        self,
        thread_id: str,
        *,
        limit: int = 200,
    ) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM messages WHERE thread_id = ? ORDER BY created_at ASC LIMIT ?",
                [thread_id, limit],
            )
            return [self._hydrate(dict(r)) for r in cur.fetchall()]

    def mark_read(self, message_id: str) -> bool:
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE messages SET read_at = ? WHERE id = ? AND read_at IS NULL",
                (utcnow().isoformat(), message_id),
            )
            return cur.rowcount == 1

    def delete(self, message_id: str) -> bool:
        with self._tx() as conn:
            cur = conn.execute(
                "DELETE FROM messages WHERE id = ?", (message_id,)
            )
            return cur.rowcount == 1

    def is_participant(self, message: dict, agent_id: str) -> bool:
        return message["sender_id"] == agent_id or message["recipient_id"] == agent_id
