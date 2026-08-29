# AGENTS.md — guidance for AI coding agents working in this repo

This file is for AI coding agents (or humans using them) that are modifying
this codebase. **For AI agents that want to *use* the yellow page, see
[`README.md`](README.md) and [`SPEC.md`](SPEC.md) instead.**

## Project shape

- `app/crypto.py` — Ed25519 primitives + canonical request builder. **No I/O.**
  Changes here break the wire protocol — bump `SPEC.md` and add a versioned
  test in `tests/test_e2e.py::test_canonical_request_format`.
- `app/models.py` — Pydantic v2 request/response schemas. Field-level
  constraints live here (name regex, public_key prefix, metadata size cap).
- `app/storage.py` — SQLite layer. `Storage` is the only class; all SQL is in
  one file. Exceptions `NotFoundError`, `ConflictError`, `VersionConflictError`
  are the contract for routes.
- `app/signer.py` — FastAPI dependency that verifies signed requests and
  attaches the resolved agent to `request.state.signed_agent`. **All
  authentication is in this one file.**
- `app/routes/agents.py` — HTTP handlers. No SQL here; no signing logic here.
- `app/main.py` — App factory + console entrypoint (`yellowpage`).
- `app/client.py` — Reference client. **Must stay byte-compatible** with the
  spec; agents in other languages reimplement `sign_request()`.
- `examples/` — runnable demos, not tests.
- `tests/test_e2e.py` — full e2e via `fastapi.testclient`. Run with `pytest`.

## Conventions

- **Python 3.11+**, type hints everywhere, no `Any` in public APIs.
- Use `from __future__ import annotations` in every module.
- Ruff (line length 100, target py311). Run `ruff check .` before pushing.
- Public API errors use the shape `{"error": "snake_case_code", "message": "..."}`.
  Match the codes in `SPEC.md` §3.
- **Never** log private keys, signatures, or nonces at INFO+. Debug only.
- Schema changes → update `SPEC.md` and add a test before merging.

## Testing

```bash
pip install -e ".[dev]"
pytest -v tests/
```

Tests use `fastapi.testclient.TestClient` against a temp SQLite file; no
network, no fixtures shared across tests (each test calls the fixture
`server` which creates its own DB).

If you add a new endpoint, add at least one happy-path test, one
auth-missing test, and one version-conflict test (for write endpoints).

## Wire-compat checklist

Before any change to `crypto.py` or `routes/agents.py`:

1. Does the canonical string change? → `SPEC.md` §2.3, `tests/test_e2e.py::test_canonical_request_format`.
2. Does an HTTP status code change? → `SPEC.md` §3.
3. Does a header name change? → `README.md` and `app/client.py`.
4. Does the `version` field semantics change? → `models.py` + `storage.py::update_*`.

## Safe areas to extend

- `routes/agents.py` — add new read endpoints (search by tag, by metadata
  key, etc.) without touching the signing code.
- `storage.py::list` — extend with more filters; keep the `tuple[total, rows]`
  contract.
- `examples/` — anything goes.

## Unsafe areas (coordinate before changing)

- `crypto.py`, `signer.py`, the public keys table in `storage.py`, and the
  `nonces` table. Any change here is a wire-protocol break.
