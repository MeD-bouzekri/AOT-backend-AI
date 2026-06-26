"""
orchestrator.py — run a request end to end.

Flow:
    1. Planner    → DispatchPlan (domain, departments, workers, hitl)
    2. Departments→ each manager runs its assigned workers (the demo runs them in turn;
                    independent departments are logically parallel)
    3. Governance → compliance + risk overseers (deterministic). May raise a Veto (freeze).
    4. Freeze?    → run halts; alert routes to the owning department; only the named
                    authority can clear (clear_veto). No path to execution while active.
    5. Approver   → threshold-based human approvals (HITL), if any.
    6. Reporter   → final report + readiness/decision + audit.

Every step emits a StepEvent (live dashboard) and is collected for the audit log. This file
is framework-light on purpose; a LangGraph StateGraph can wrap these same functions later
without changing them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable, Optional

from schemas import (
    StepEvent, DispatchPlan, DepartmentResult, Veto, CompanyContext,
)
from context_loader import load_company_context
from agents.planner import plan_request
from agents.manager import run_department, workers_for_department
from agents import governance as gov


EmitFn = Callable[[StepEvent], None]


class RunResult:
    """The full outcome of a run (what the gateway returns / persists)."""
    def __init__(self):
        self.plan: Optional[DispatchPlan] = None
        self.departments: dict[str, DepartmentResult] = {}
        self.veto: Optional[Veto] = None
        self.approvals: list[str] = []
        self.report: str = ""
        self.status: str = "running"
        self.events: list[StepEvent] = []


def run_request(
    raw_text: str,
    run_id: str = "run-local",
    ctx: Optional[CompanyContext] = None,
    emit: Optional[EmitFn] = None,
) -> RunResult:
    ctx = ctx or load_company_context()
    result = RunResult()

    def _emit(level, agent, department, **kw):
        ev = StepEvent(run_id=run_id, department=department, level=level, agent=agent, **kw)
        result.events.append(ev)
        if emit:
            emit(ev)

    # 1. Planner ──────────────────────────────────────────────
    _emit("planner", "planner", "system", phase="Analyzing request…", status="running")
    plan = plan_request(raw_text, ctx)
    result.plan = plan
    _emit("planner", "planner", "system", phase="Plan ready", status="done",
          output=plan.summary, reasoning=plan.reasoning)

    # the structured fields the workers + overseers read
    extracted = _extract_context(plan, raw_text)

    # 2. Departments ──────────────────────────────────────────
    dept_keys = [d.department for d in plan.departments]
    for dept in dept_keys:
        wnames = workers_for_department(dept, plan.required_workers, ctx)
        if not wnames:
            continue
        _emit("manager", f"{dept}_manager", dept,
              phase=f"Assigning {len(wnames)} task(s)…", status="running")

        def mgr_emit(d):  # bridge manager's dict callback → StepEvent
            _emit(d.get("level", "worker"), d["agent"], d["department"],
                  assigned_by=d.get("assigned_by"), phase=d.get("phase", ""),
                  status=d.get("status", "done"), output=d.get("output"),
                  reasoning=d.get("reasoning"), tools_used=d.get("tools_used", []))

        dept_result = run_department(dept, wnames, extracted, ctx, emit=mgr_emit)
        result.departments[dept] = dept_result
        _emit("manager", f"{dept}_manager", dept, phase="Department done",
              status=dept_result.status, reasoning=dept_result.manager_reasoning)

    # 3. Governance gate ──────────────────────────────────────
    _emit("governance", "compliance_overseer", "system",
          phase="Checking hard policy rules…", status="running")
    veto = gov.check_compliance(extracted, result.departments, ctx) \
        or gov.check_risk(extracted, ctx)

    if veto:
        result.veto = veto
        result.status = "frozen"
        _emit("governance", veto.raised_by.lower() + "_overseer", veto.owning_department,
              phase=f"{veto.scope.upper()} — {veto.rule_id}", status="blocked",
              output=veto.message, reasoning=veto.explanation,
              policy_citation=veto.rule_id)
        # 4. Freeze: alert owning department, await authority. Stop here.
        _emit("governance", "system", veto.owning_department,
              phase=f"Frozen — awaiting {veto.required_authority}", status="awaiting_human",
              output=f"Alert sent to {veto.owning_department} admin. "
                     f"Only {veto.required_authority} can clear.")
        return result

    _emit("governance", "compliance_overseer", "system",
          phase="No violations", status="done")

    # 5. Approver (threshold HITL) ────────────────────────────
    approvals = gov.needs_human_approval(extracted, plan, ctx)
    result.approvals = approvals
    if approvals:
        _emit("governance", "approver", "system", phase="Human approval required",
              status="awaiting_human", output="; ".join(approvals))
        result.status = "awaiting_human"
        # for the demo we continue to a report; in HITL mode the run would pause here.

    # 6. Reporter ─────────────────────────────────────────────
    report = _build_report(plan, result, approvals)
    result.report = report
    if result.status == "running":
        result.status = "done"
    _emit("governance", "reporter", "system", phase="Final report", status="done",
          output=report)

    return result


def clear_veto(result: RunResult, authority: str, decision: str,
               conditions: str = "", ctx: Optional[CompanyContext] = None) -> RunResult:
    """A named authority clears a freeze. Only the matching authority may clear."""
    if not result.veto:
        return result
    if authority != result.veto.required_authority:
        raise PermissionError(
            f"{authority} cannot clear a veto requiring {result.veto.required_authority}"
        )
    result.veto.cleared_by = authority
    result.veto.decision = decision  # type: ignore[assignment]
    result.veto.conditions = conditions or None
    if decision == "deny":
        result.status = "denied"
    else:
        result.status = "done"
        result.report = (
            f"Released by {authority} ({decision}"
            + (f": {conditions}" if conditions else "") + "). "
            + _build_report(result.plan, result, result.approvals)
        )
    return result


# ───────────────────────── helpers ─────────────────────────

def _extract_context(plan: DispatchPlan, raw_text: str) -> dict:
    """Turn the plan + raw text into the flat field dict workers/overseers read.

    In live mode the Planner can populate richer extracted fields; here we derive the few
    the demo logic needs from the raw text + plan.
    """
    import re
    t = raw_text.lower()
    amount = 0.0
    m = re.search(r"\$\s?([\d,]+(?:\.\d+)?)\s?(k|m)?", t)
    if m:
        amount = float(m.group(1).replace(",", "")) * (
            1_000 if m.group(2) == "k" else 1_000_000 if m.group(2) == "m" else 1)
    return {
        "raw": raw_text,
        "domain": plan.domain,
        "amount": amount,
        "employment_type": ("contractor" if "contractor" in t
                            else "intern" if "intern" in t else "full_time"),
        "access_scope": ["production"] if ("production" in t or "prod" in t) else [],
        "seniority": next((s for s in ("intern", "junior", "senior", "staff", "exec")
                          if s in t), "mid"),
        "remote": "remote" in t,
        "is_data_processor": any(w in t for w in ("customer data", "personal data", "pii")),
        "has_dpa": "dpa" in t,
        "department": ("finance" if "financ" in t else "engineering" if "engineer" in t
                      else "sales" if "sales" in t else "general"),
        "vendor": next((v for v in ("slack", "zoom", "notion", "figma")
                       if v in t), "Vendor").title(),
        "role": "New Hire",
    }


def _build_report(plan, result: RunResult, approvals: list[str]) -> str:
    lines = [f"Request: {plan.summary}", f"Domain: {plan.domain}",
             f"Departments: {', '.join(d.department for d in plan.departments)}"]
    for dept, dr in result.departments.items():
        lines.append(f"  [{dept}] {len(dr.workers)} task(s): "
                     + "; ".join(f"{w.worker}→{w.status}" for w in dr.workers))
    if approvals:
        lines.append("Pending approvals: " + "; ".join(approvals))
    lines.append("Outcome: " + result.status)
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    samples = [
        "Approve a $95k annual Slack + Zoom subscription for our customer data platform.",
        "Onboard Priya, a mid software engineer contractor, remote, needs production access.",
        "Approve a $9k Notion license.",
    ]
    reqs = sys.argv[1:] or samples
    for i, s in enumerate(reqs):
        print("\n" + "#" * 74)
        print("REQUEST:", s)
        res = run_request(s, run_id=f"run-{i}")
        for ev in res.events:
            tag = f"[{ev.department}/{ev.level}] {ev.agent}: {ev.phase} ({ev.status})"
            print(" ", tag)
            if ev.output:
                print("       →", ev.output[:100])
        print("STATUS:", res.status)
        if res.veto:
            print(f"FROZEN: {res.veto.rule_id} owner={res.veto.owning_department} "
                  f"authority={res.veto.required_authority}")
