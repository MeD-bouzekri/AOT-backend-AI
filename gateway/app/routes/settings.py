"""Settings routes — governance thresholds, hard rules, default Ollama LLM, mode."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import Principal, require_role
from app.core import engine

router = APIRouter(prefix="/api", tags=["settings"])

_DATA = Path(__file__).resolve().parents[3] / "agents-orch" / "data"
_POLICIES = _DATA / "policies.json"
_COMPANY = _DATA / "company_config.json"


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


class ThresholdsBody(BaseModel):
    thresholds: dict


class DefaultLLMBody(BaseModel):
    base_url: str
    model: str


@router.get("/settings")
async def get_settings(p: Principal = Depends(require_role("company_admin"))):
    pol = _read(_POLICIES)
    cfg = _read(_COMPANY)
    return {
        "thresholds": pol.get("thresholds", {}),
        "hard_rules": pol.get("hard_rules", []),
        "authorities": pol.get("authorities", {}),
        "company": pol.get("company", {}),
        "default_llm": cfg.get("default_llm"),
    }


@router.put("/settings/thresholds")
async def update_thresholds(body: ThresholdsBody,
                            p: Principal = Depends(require_role("company_admin"))):
    pol = _read(_POLICIES)
    pol["thresholds"] = body.thresholds
    _write(_POLICIES, pol)
    engine.reload_config()
    return {"updated": True, "thresholds": body.thresholds}


@router.get("/policies")
async def get_policies(p: Principal = Depends(require_role("company_admin"))):
    return _read(_POLICIES).get("hard_rules", [])


@router.put("/policies")
async def update_policies(rules: list[dict],
                          p: Principal = Depends(require_role("company_admin"))):
    pol = _read(_POLICIES)
    pol["hard_rules"] = rules
    _write(_POLICIES, pol)
    engine.reload_config()
    return {"updated": True, "count": len(rules)}


@router.put("/settings/default-llm")
async def set_default_llm(body: DefaultLLMBody,
                          p: Principal = Depends(require_role("company_admin"))):
    """Validate + persist the default Ollama server for the whole system."""
    result = await engine.validate_ollama(body.base_url, body.model)
    ok = getattr(result, "ok", False) if hasattr(result, "ok") else result.get("ok")
    if not ok:
        return {"updated": False, "validation": result.model_dump()
                if hasattr(result, "model_dump") else result}
    cfg = _read(_COMPANY)
    cfg["default_llm"] = {"provider": "ollama", "base_url": body.base_url,
                          "model": body.model}
    _write(_COMPANY, cfg)
    engine.reload_config()
    return {"updated": True, "default_llm": cfg["default_llm"]}
