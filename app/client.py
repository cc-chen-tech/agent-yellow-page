"""Convenience client for the yellow-page API.

This is the reference implementation of the signing protocol — agents in
other languages should reproduce `sign_request()` in their own stack.
"""

from __future__ import annotations

import base64
import os
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from .crypto import KEY_PREFIX, KeyPair, canonical_request


class YellowPageClient:
    """Minimal sync-ish client (httpx.Client under the hood).

    Holds the agent's private key and signs every write request automatically.
    """

    def __init__(
        self,
        base_url: str,
        agent_id: str | None = None,
        keypair: KeyPair | None = None,
        timeout: float = 10.0,
    ):
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(base_url=self.base_url, timeout=timeout)
        self.agent_id = agent_id
        self.keypair = keypair

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # --- registration / discovery (no signature) ------------------------- #

    def register(
        self,
        name: str,
        public_key: str,
        *,
        display_name: str | None = None,
        description: str | None = None,
        endpoint: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict:
        body = {
            "name": name,
            "public_key": public_key,
        }
        if display_name is not None:
            body["display_name"] = display_name
        if description is not None:
            body["description"] = description
        if endpoint is not None:
            body["endpoint"] = endpoint
        if tags is not None:
            body["tags"] = tags
        if metadata is not None:
            body["metadata"] = metadata
        r = self._http.post("/v0/agents", json=body)
        r.raise_for_status()
        return r.json()

    def get(self, id_or_name: str) -> dict:
        r = self._http.get(f"/v0/agents/{id_or_name}")
        r.raise_for_status()
        return r.json()

    def list(
        self,
        *,
        q: str | None = None,
        tags: list[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        params: list[tuple[str, str]] = []
        if q:
            params.append(("q", q))
        for t in tags or []:
            params.append(("tag", t))
        params.append(("limit", str(limit)))
        params.append(("offset", str(offset)))
        r = self._http.get("/v0/agents?" + urlencode(params))
        r.raise_for_status()
        return r.json()

    def challenge(self, id_or_name: str) -> dict:
        r = self._http.get(f"/v0/agents/{id_or_name}/challenge")
        r.raise_for_status()
        return r.json()

    # --- signed write ops ----------------------------------------------- #

    def _sign_headers(self, method: str, path: str, body: bytes) -> dict[str, str]:
        if not (self.agent_id and self.keypair):
            raise RuntimeError("agent_id and keypair must be set for signed requests")
        ts = int(time.time())
        nonce = base64.b64encode(os.urandom(16)).decode("ascii")
        msg = canonical_request(ts, method, path, body)
        sig = self.keypair.sign(msg)
        return {
            "X-Agent-Id": self.agent_id,
            "X-Timestamp": str(ts),
            "X-Nonce": nonce,
            "X-Signature": sig,
        }

    def patch(self, agent_id: str, fields: dict[str, Any], *, if_match: str | None = None) -> dict:
        path = f"/v0/agents/{agent_id}"
        import json

        body = json.dumps(fields, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = self._sign_headers("PATCH", path, body)
        headers["Content-Type"] = "application/json"
        if if_match:
            headers["If-Match"] = if_match
        r = self._http.patch(path, content=body, headers=headers)
        r.raise_for_status()
        return r.json()

    def put(self, agent_id: str, fields: dict[str, Any], *, if_match: str | None = None) -> dict:
        path = f"/v0/agents/{agent_id}"
        import json

        body = json.dumps(fields, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = self._sign_headers("PUT", path, body)
        headers["Content-Type"] = "application/json"
        if if_match:
            headers["If-Match"] = if_match
        r = self._http.put(path, content=body, headers=headers)
        r.raise_for_status()
        return r.json()

    def delete(self, agent_id: str) -> None:
        path = f"/v0/agents/{agent_id}"
        headers = self._sign_headers("DELETE", path, b"")
        r = self._http.delete(path, headers=headers)
        r.raise_for_status()
