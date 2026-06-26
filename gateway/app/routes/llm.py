"""LLM routes — Ollama validation + model listing (sensitive data → local only)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import Principal, require_role
from app.core import engine

router = APIRouter(prefix="/api/llm", tags=["llm"])


class OllamaValidateBody(BaseModel):
    base_url: str
    model: str


@router.post("/validate-ollama")
async def validate_ollama(body: OllamaValidateBody,
                          p: Principal = Depends(require_role("company_admin"))):
    result = await engine.validate_ollama(body.base_url, body.model)
    return result.model_dump() if hasattr(result, "model_dump") else result


@router.get("/ollama/models")
async def ollama_models(base_url: str,
                        p: Principal = Depends(require_role("company_admin"))):
    return {"models": await engine.list_ollama_models(base_url)}
