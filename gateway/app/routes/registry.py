"""Registry routes — list the agent/tool catalog; create user-built agents/tools."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import Principal, require_auth, require_role
from app.core import engine

router = APIRouter(prefix="/api/registry", tags=["registry"])

_DATA = Path(__file__).resolve().parents[3] / "agents-orch" / "data"
_AGENTS_FILE = _DATA / "registry_agents.json"
_TOOLS_FILE = _DATA / "registry_tools.json"


def _load(path: Path) -> list:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def _save(path: Path, items: list) -> None:
    path.write_text(json.dumps(items, indent=2), encoding="utf-8")


class AgentSpecBody(BaseModel):
    name: str
    department: str
    role: str
    system_prompt: str = ""
    tools: list[str] = []
    mode: str = "single"                 # single | react | rule
    llm: dict | None = None              # {provider:'ollama', base_url, model}


class ToolSpecBody(BaseModel):
    name: str
    description: str
    inputs: list[str] = []
    mock_response: dict | None = None


@router.get("/agents")
async def list_agents(p: Principal = Depends(require_auth)):
    ctx = engine.load_company_context()
    builtin = [c.model_dump() for c in ctx.capabilities]
    return {"builtin": builtin, "user_built": _load(_AGENTS_FILE)}


@router.post("/agents")
async def create_agent(body: AgentSpecBody,
                       p: Principal = Depends(require_role("company_admin"))):
    items = _load(_AGENTS_FILE)
    items = [a for a in items if a.get("name") != body.name]
    spec = body.model_dump()
    spec.update({"level": "worker", "builtin": False, "enabled": True})
    items.append(spec)
    _save(_AGENTS_FILE, items)
    return {"created": body.name}


@router.get("/tools")
async def list_tools(p: Principal = Depends(require_auth)):
    return {"builtin": engine.list_tools(), "user_built": _load(_TOOLS_FILE)}


@router.post("/tools")
async def create_tool(body: ToolSpecBody,
                      p: Principal = Depends(require_role("company_admin"))):
    items = _load(_TOOLS_FILE)
    items = [t for t in items if t.get("name") != body.name]
    spec = body.model_dump()
    spec["builtin"] = False
    items.append(spec)
    _save(_TOOLS_FILE, items)
    return {"created": body.name}
