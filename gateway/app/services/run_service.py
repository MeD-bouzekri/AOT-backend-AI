"""
Run service — execute a request through the engine, stream progress, persist, remember.

Flow:
  1. create a Request row
  2. run the LangGraph engine in a worker thread (it is sync) while the event loop stays free
  3. as the engine produces StepEvents, publish them to the bus AND persist as AgentLogRow
  4. on finish: persist Run (+ Veto/Approval), record to Institutional Memory
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

from app.core import engine
from app.core.events import bus

# engine DB layer (re-exported via sys.path bridge)
import db as _db  # type: ignore  # noqa: E402


def new_run_id() -> str:
    return f"run-{uuid.uuid4().hex[:10]}"


def _event_to_dict(ev) -> dict:
    # StepEvent is a pydantic model; normalize to a plain JSON-able dict
    d = ev.model_dump() if hasattr(ev, "model_dump") else dict(ev)
    if isinstance(d.get("ts"), datetime):
        d["ts"] = d["ts"].isoformat()
    return d


def _persist_log(ev: dict) -> None:
    try:
        with _db.get_session() as s:
            s.add(_db.AgentLogRow(
                run_id=ev.get("run_id", ""),
                department=ev.get("department", "system"),
                level=ev.get("level", "worker"),
                agent=ev.get("agent", ""),
                assigned_by=ev.get("assigned_by"),
                action=ev.get("phase", ""),
                phase=ev.get("phase", ""),
                status=ev.get("status", ""),
                tools_used=ev.get("tools_used", []),
                output=ev.get("output"),
                reasoning=ev.get("reasoning"),
                policy_citation=ev.get("policy_citation"),
            ))
            s.commit()
    except Exception:  # noqa: BLE001 — logging must never break a run
        pass


async def start_run(raw_text: str, submitted_by: str) -> dict:
    """Create the request + run the engine in the background. Returns {run_id} immediately."""
    run_id = new_run_id()

    # persist the incoming request
    try:
        with _db.get_session() as s:
            s.add(_db.Request(id=run_id, submitted_by=submitted_by,
                              raw_text=raw_text, status="running"))
            s.commit()
    except Exception:  # noqa: BLE001
        pass

    asyncio.create_task(_execute(run_id, raw_text))
    return {"run_id": run_id, "status": "running"}


async def _execute(run_id: str, raw_text: str) -> None:
    loop = asyncio.get_running_loop()

    # The engine.run is synchronous and returns the final state with all events. We run it in
    # a thread so the loop stays responsive, then replay events with persistence + publish.
    final = await loop.run_in_executor(None, engine.run_graph, raw_text, run_id)

    # Replay engine events with a small pace so the live timeline animates
    # step-by-step instead of dumping all at once. The report step (long ASCII
    # blob) and bookkeeping steps publish without an extra wait.
    events = final.get("events", [])
    for ev in events:
        d = _event_to_dict(ev)
        _persist_log(d)
        await bus.publish_event(d)
        phase = d.get("phase", "")
        is_report = phase in ("Final report", "Report (frozen)")
        if not is_report:
            await asyncio.sleep(0.35)

    # finalize Run row
    plan = final.get("plan")
    veto = final.get("veto")
    status = final.get("status", "done")
    try:
        with _db.get_session() as s:
            s.add(_db.Run(id=run_id, request_id=run_id,
                          plan=(plan.model_dump() if plan else {}),
                          status=status,
                          ended_at=datetime.now(timezone.utc)))
            if veto:
                s.add(_db.VetoRow(run_id=run_id, raised_by=veto.raised_by,
                                  rule_id=veto.rule_id, scope=veto.scope,
                                  message=veto.message, explanation=veto.explanation,
                                  required_authority=veto.required_authority,
                                  status="active"))
            for a in final.get("approvals", []) or []:
                s.add(_db.ApprovalRow(run_id=run_id, approver_role=a.split(":")[0],
                                      item=a, status="pending"))
            s.commit()
    except Exception:  # noqa: BLE001
        pass

    # publish terminal event for the UI
    await bus.publish(f"run:{run_id}", {
        "run_id": run_id, "department": "system", "level": "governance",
        "agent": "system", "phase": "complete",
        "status": "awaiting_human" if (veto or final.get("approvals")) else "done",
        "type": "hitl" if veto else "done",
        "report": final.get("report", ""),
        "veto": (veto.model_dump() if veto else None),
    })


def get_run(run_id: str) -> dict | None:
    with _db.get_session() as s:
        run = s.get(_db.Run, run_id)
        req = s.get(_db.Request, run_id)
    if not run and not req:
        return None
    return {
        "run_id": run_id,
        "request": getattr(req, "raw_text", None),
        "status": getattr(run, "status", getattr(req, "status", "running")),
        "plan": getattr(run, "plan", {}),
    }
