"""
OrchestrAI Gateway — FastAPI app.

The single secure surface between the Next.js dashboard and the orchestration engine.
Auth (Keycloak / DEV), CORS, route wiring, and DB init on startup.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.core import engine          # noqa: F401 — adds agents-orch to sys.path on import
from app.routes import runs, admin, logs, llm, registry, settings as settings_routes

import db as _db  # type: ignore


@asynccontextmanager
async def lifespan(app: FastAPI):
    _db.init_db()
    yield


app = FastAPI(title="OrchestrAI Gateway", version="1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# routers
app.include_router(runs.router)
app.include_router(admin.router)
app.include_router(logs.router)
app.include_router(llm.router)
app.include_router(registry.router)
app.include_router(settings_routes.router)


@app.get("/health")
async def health():
    return {"status": "ok", "mode": settings.MODE, "dev_auth": settings.DEV_AUTH}
