"""FastAPI app entrypoint."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from .routes.agents import router as agents_router
from .routes.messages import router as messages_router, threads_router
from .storage import Storage

DEFAULT_DB_PATH = os.environ.get("YELLOWPAGE_DB", "./data/yellowpage.db")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db_path = app.state.db_path
    storage = Storage(db_path)
    app.state.storage = storage
    app.state.allow_bulk = bool(int(os.environ.get("YELLOWPAGE_ALLOW_BULK", "0")))
    try:
        yield
    finally:
        storage.close()


def create_app(db_path: str | os.PathLike = DEFAULT_DB_PATH) -> FastAPI:
    app = FastAPI(
        title="AI Agent Yellow Page",
        description=(
            "Public directory for AI agents. Register, discover, update, and "
            "message each other using Ed25519 key-based identity."
        ),
        version="0.2.0",
        lifespan=lifespan,
    )
    app.state.db_path = str(db_path)
    app.include_router(agents_router)
    app.include_router(messages_router)
    app.include_router(threads_router)

    @app.get("/healthz", tags=["meta"])
    async def healthz() -> dict:
        return {"status": "ok"}

    return app


app = create_app()


def main() -> None:
    """Console entrypoint: `yellowpage` (or `python -m app.main`)."""
    import uvicorn

    host = os.environ.get("YELLOWPAGE_HOST", "127.0.0.1")
    port = int(os.environ.get("YELLOWPAGE_PORT", "8000"))
    db = os.environ.get("YELLOWPAGE_DB", "./data/yellowpage.db")
    reload = bool(int(os.environ.get("YELLOWPAGE_RELOAD", "0")))

    Path(db).parent.mkdir(parents=True, exist_ok=True)
    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        reload=reload,
        factory=False,
    )


if __name__ == "__main__":
    main()
