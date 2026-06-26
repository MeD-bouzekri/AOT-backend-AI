"""Logs routes — the audit trail (powers the logs page)."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core.auth import Principal, require_auth
import db as _db  # type: ignore

router = APIRouter(prefix="/api", tags=["logs"])


@router.get("/logs")
async def list_logs(run: str | None = None, dept: str | None = None,
                    agent: str | None = None, status: str | None = None,
                    q: str | None = None, limit: int = 200, offset: int = 0,
                    p: Principal = Depends(require_auth)):
    from sqlmodel import select
    scope = p.scope()
    with _db.get_session() as s:
        stmt = select(_db.AgentLogRow).order_by(_db.AgentLogRow.ts.desc())
        rows = s.exec(stmt).all()

    def keep(r) -> bool:
        if scope and scope != "ALL" and r.department != scope:
            return False
        if run and r.run_id != run:
            return False
        if dept and r.department != dept:
            return False
        if agent and r.agent != agent:
            return False
        if status and r.status != status:
            return False
        if q and q.lower() not in ((r.reasoning or "") + (r.output or "")).lower():
            return False
        return True

    filtered = [r for r in rows if keep(r)]
    page = filtered[offset:offset + limit]
    return {
        "total": len(filtered),
        "items": [{"id": r.id, "ts": r.ts.isoformat(), "run_id": r.run_id,
                   "department": r.department, "agent": r.agent, "action": r.action,
                   "status": r.status, "tools_used": r.tools_used,
                   "output": r.output, "reasoning": r.reasoning,
                   "policy_citation": r.policy_citation} for r in page],
    }


@router.get("/logs/{log_id}")
async def get_log(log_id: str, p: Principal = Depends(require_auth)):
    with _db.get_session() as s:
        r = s.get(_db.AgentLogRow, log_id)
    if not r:
        return {"error": "not found"}
    return r.model_dump()
