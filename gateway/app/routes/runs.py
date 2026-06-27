"""Run routes - submit a request, stream it live, fetch state, clear a freeze."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from app.core.auth import Principal, require_auth, ws_principal
from app.core.events import bus
from app.services import run_service
import db as _db  # type: ignore

router = APIRouter(prefix="/api", tags=["runs"])


class RequestBody(BaseModel):
    text: str


class ClearVetoBody(BaseModel):
    authority: str                      # CISO | CFO | DPO
    decision: str                       # release | release_with_conditions | deny
    conditions: str | None = None
    note: str | None = None


@router.post("/request")
async def submit_request(body: RequestBody, p: Principal = Depends(require_auth)):
    if not body.text.strip():
        raise HTTPException(400, "Empty request")
    return await run_service.start_run(body.text, submitted_by=p.email)


@router.get("/runs/{run_id}")
async def get_run(run_id: str, p: Principal = Depends(require_auth)):
    run = run_service.get_run(run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    return run


@router.get("/runs")
async def list_runs(p: Principal = Depends(require_auth)):
    with _db.get_session() as s:
        from sqlmodel import select
        rows = s.exec(select(_db.Run).order_by(_db.Run.started_at.desc())).all()
    return [{"run_id": r.id, "status": r.status,
             "plan": r.plan, "started_at": r.started_at.isoformat()} for r in rows]


@router.get("/runs/{run_id}/graph")
async def run_graph(run_id: str, p: Principal = Depends(require_auth)):
    """Reconstruct a node graph from the run's persisted logs (for /workflows).

    Nodes are the distinct agents that ran. Edges follow the orchestration
    topology: planner -> every department worker/manager -> governance ->
    reporter. We prefer an explicit `assigned_by` link when the engine set it,
    and otherwise infer edges from the agent's level so the graph is always
    connected (the saved logs rarely carry assigned_by).
    """
    from sqlmodel import select
    with _db.get_session() as s:
        logs = s.exec(select(_db.AgentLogRow)
                      .where(_db.AgentLogRow.run_id == run_id)
                      .order_by(_db.AgentLogRow.ts)).all()

    # collapse to one node per agent, keeping the latest (terminal) status
    nodes: dict[str, dict] = {}
    order: list[str] = []
    explicit: list[dict] = []
    for lg in logs:
        if lg.agent not in nodes:
            order.append(lg.agent)
        nodes[lg.agent] = {"id": lg.agent, "label": lg.agent, "level": lg.level,
                           "department": lg.department, "status": lg.status}
        if lg.assigned_by and lg.assigned_by != lg.agent:
            explicit.append({"from": lg.assigned_by, "to": lg.agent})

    # bucket agents by level (preserve first-seen order)
    def by_level(level: str) -> list[str]:
        return [a for a in order if nodes[a]["level"] == level]

    planners = by_level("planner")
    middles = by_level("manager") + by_level("worker")
    govs = by_level("governance")

    # the single planner anchor (institutional_memory + planner are both
    # 'planner' level; use the literal 'planner' agent when present)
    anchor = "planner" if "planner" in nodes else (planners[0] if planners else None)

    edges: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def link(a: str | None, b: str | None) -> None:
        if a and b and a != b and (a, b) not in seen:
            seen.add((a, b))
            edges.append({"from": a, "to": b})

    # chain the planner-level nodes (e.g. institutional_memory -> planner)
    for pa in planners:
        link(pa, anchor)
    # planner -> each worker/manager
    for m in middles:
        link(anchor, m)
    # workers/managers -> governance overseer(s)
    gov_entry = govs[0] if govs else None
    for m in middles:
        link(m, gov_entry)
    if not middles:
        link(anchor, gov_entry)
    # chain governance nodes (overseer -> reporter / frozen -> reporter)
    for i in range(len(govs) - 1):
        link(govs[i], govs[i + 1])

    # explicit assigned_by edges take precedence / augment
    for e in explicit:
        link(e["from"], e["to"])

    return {"run_id": run_id, "nodes": list(nodes.values()), "edges": edges}


@router.websocket("/runs/{run_id}/stream")
async def stream_run(ws: WebSocket, run_id: str):
    await ws.accept()
    try:
        async for ev in bus.subscribe(f"run:{run_id}"):
            await ws.send_json(ev)
    except WebSocketDisconnect:
        pass


@router.get("/vetoes")
async def list_vetoes(status: str = "active", p: Principal = Depends(require_auth)):
    """
    List vetoes for the Veto Review page. By default only active freezes.

    Scoped to the caller: an authority (CISO/CFO/DPO) sees the freezes it can
    actually clear; company_admin sees all. Each row is joined to its run's
    request text so the reviewer has context.
    """
    from sqlmodel import select
    with _db.get_session() as s:
        q = select(_db.VetoRow)
        if status != "all":
            q = q.where(_db.VetoRow.status == status)
        vetoes = s.exec(q.order_by(_db.VetoRow.raised_at.desc())).all()

        out = []
        for v in vetoes:
            # authority scoping: only surface freezes this principal may clear
            if v.status == "active" and not p.is_company_admin \
                    and v.required_authority not in p.authorities:
                continue
            req = s.get(_db.Request, v.run_id)
            run = s.get(_db.Run, v.run_id)
            out.append({
                "id": v.id,
                "run_id": v.run_id,
                "request": getattr(req, "raw_text", None),
                "domain": (getattr(run, "plan", {}) or {}).get("domain"),
                "raised_by": v.raised_by,
                "rule_id": v.rule_id,
                "scope": v.scope,
                "message": v.message,
                "explanation": v.explanation,
                "required_authority": v.required_authority,
                "status": v.status,
                "cleared_by": v.cleared_by,
                "decision": v.decision,
                "conditions": v.conditions,
                "raised_at": v.raised_at.isoformat() if v.raised_at else None,
                "resolved_at": v.resolved_at.isoformat() if v.resolved_at else None,
                "can_clear": p.is_company_admin or v.required_authority in p.authorities,
            })
    return out


@router.post("/runs/{run_id}/clear-veto")
async def clear_veto(run_id: str, body: ClearVetoBody, p: Principal = Depends(require_auth)):
    # the caller must actually hold the authority the veto requires
    with _db.get_session() as s:
        from sqlmodel import select
        veto = s.exec(select(_db.VetoRow)
                      .where(_db.VetoRow.run_id == run_id,
                             _db.VetoRow.status == "active")).first()
        if not veto:
            raise HTTPException(404, "No active veto on this run")
        if body.authority != veto.required_authority or body.authority not in p.authorities:
            raise HTTPException(403,
                                f"This freeze can only be cleared by {veto.required_authority}")
        veto.status = "cleared" if body.decision != "deny" else "denied"
        veto.cleared_by = p.email
        veto.decision = body.decision
        veto.conditions = body.conditions
        veto.resolved_at = datetime.now(timezone.utc)
        run = s.get(_db.Run, run_id)
        if run:
            run.status = "denied" if body.decision == "deny" else "done"
        s.add(veto)
        s.commit()
        # capture the fields we need AFTER the session closes (the row detaches
        # once the `with` block exits, so reading veto.* later would raise).
        rule_id = veto.rule_id

    is_deny = body.decision == "deny"

    # feedback loop -> memory learns the resolution
    try:
        from app.core import engine
        engine.feedback(run_id, outcome=("denied" if is_deny else "done"),
                        lesson=f"{rule_id} cleared by {body.authority}: {body.decision}")
    except Exception:  # noqa: BLE001
        pass

    await bus.publish(f"run:{run_id}", {
        "run_id": run_id, "department": "system",
        "level": "governance", "agent": "system", "phase": "veto cleared",
        "status": "denied" if is_deny else "done", "type": "resolved",
        "output": f"{body.authority} {body.decision} (rule {rule_id})",
    })
    return {"run_id": run_id, "rule_id": rule_id, "decision": body.decision,
            "cleared_by": p.email}
