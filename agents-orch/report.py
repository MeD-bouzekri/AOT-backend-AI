"""
report.py - build the professional final report for a run.

Produces a structured, complete report that covers every outcome:
    - header (request, domain, company, timestamp, final status)
    - the Planner's plan (departments, mandates, chosen team, reasoning)
    - execution detail per department -> per worker (what ran, tool, result, reasoning)
    - governance outcome: clean / FROZEN (block/halt) / pending human approval
    - for a freeze: the rule, the owning department alerted, the authority who must clear,
      and the resolution if cleared
    - approvals required + their authority
    - a day-1 readiness score (onboarding) or a decision (procurement)

Returned as both a structured dict (for the dashboard/DB) and a formatted text block.
"""

from __future__ import annotations

from datetime import datetime, timezone

from schemas import DispatchPlan, DepartmentResult, Veto
from tools.builtin import employee_directory


def build_report(
    *,
    run_id: str,
    company: str,
    raw_text: str,
    plan: DispatchPlan,
    departments: dict[str, DepartmentResult],
    veto: Veto | None,
    approvals: list[str],
    status: str,
) -> dict:
    """Assemble the full report object."""
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # execution detail
    exec_blocks = []
    total_actions = 0
    for dept, dr in departments.items():
        workers = []
        for w in dr.workers:
            total_actions += 1
            workers.append({
                "worker": w.worker,
                "tool_used": w.tool_used,
                "status": w.status,
                "output": w.output,
                "reasoning": w.reasoning,
                "flags": w.flags,
            })
        exec_blocks.append({
            "department": dept,
            "manager": f"{dept}_manager",
            "manager_reasoning": dr.manager_reasoning,
            "status": dr.status,
            "workers": workers,
        })

    # governance section
    governance = {"outcome": "clean", "detail": "No policy violations detected."}
    if veto:
        holders = employee_directory(authority=veto.required_authority).get("holders", [])
        clearer = (f"{holders[0]['name']} ({holders[0]['role']}, {holders[0]['email']})"
                   if holders else veto.required_authority)
        governance = {
            "outcome": veto.scope.upper(),                 # BLOCK | HALT
            "rule_id": veto.rule_id,
            "raised_by": veto.raised_by,
            "message": veto.message,
            "explanation": veto.explanation,
            "owning_department": veto.owning_department,
            "alerted_admin": f"{veto.owning_department.upper()} department admin",
            "required_authority": veto.required_authority,
            "must_be_cleared_by": clearer,
            "cleared_by": veto.cleared_by,
            "decision": veto.decision,
            "conditions": veto.conditions,
        }
    elif approvals:
        governance = {"outcome": "AWAITING_APPROVAL",
                      "detail": "Within policy, but human approval is required.",
                      "approvals": approvals}

    # readiness / decision
    if plan.domain == "hr_onboarding":
        outcome_metric = {"type": "readiness_score", "value": _readiness(status, departments)}
    else:
        outcome_metric = {"type": "decision", "value": _decision(status, veto, approvals)}

    report = {
        "run_id": run_id,
        "company": company,
        "generated_at": ts,
        "request": raw_text,
        "domain": plan.domain,
        "summary": plan.summary,
        "final_status": status,
        "plan": {
            "departments": [{"department": d.department, "mandate": d.mandate,
                             "depends_on": d.depends_on} for d in plan.departments],
            "team": plan.required_workers,
            "hitl_points": plan.hitl_points,
            "deadline_days": plan.deadline_days,
            "reasoning": plan.reasoning,
            "confidence": plan.confidence,
        },
        "execution": exec_blocks,
        "total_actions": total_actions,
        "governance": governance,
        "approvals": approvals,
        "outcome_metric": outcome_metric,
        "executor": "OrchestrAI Executor (automated)",
    }
    report["text"] = render_text(report)
    return report


# ───────────────────────── metrics ─────────────────────────

def _readiness(status: str, departments: dict[str, DepartmentResult]) -> int:
    if status in ("frozen", "denied"):
        return 0
    if status == "awaiting_human":
        return 75
    if not departments:
        return 0
    done = sum(1 for dr in departments.values()
               for w in dr.workers if w.status == "done")
    total = sum(len(dr.workers) for dr in departments.values()) or 1
    return round(100 * done / total)


def _decision(status: str, veto: Veto | None, approvals: list[str]) -> str:
    if veto:
        return f"BLOCKED ({veto.rule_id}) - awaiting {veto.required_authority}"
    if approvals:
        return "PENDING APPROVAL"
    if status == "denied":
        return "DENIED"
    return "APPROVED"


# ───────────────────────── text rendering ─────────────────────────

def render_text(r: dict) -> str:
    L = []
    L.append("=" * 68)
    L.append(f"  {r['company'].upper()} - ORCHESTRAI EXECUTION REPORT")
    L.append("=" * 68)
    L.append(f"Run ID      : {r['run_id']}")
    L.append(f"Generated   : {r['generated_at']}")
    L.append(f"Request     : {r['request']}")
    L.append(f"Domain      : {r['domain']}   (confidence {r['plan']['confidence']})")
    L.append(f"Final status: {r['final_status'].upper()}")
    L.append("")

    L.append("-" * 68)
    L.append("1. PLAN (decided by the Planner)")
    L.append("-" * 68)
    L.append(f"Reasoning   : {r['plan']['reasoning']}")
    L.append(f"Deadline    : {r['plan']['deadline_days']} day(s)")
    L.append("Departments :")
    for d in r["plan"]["departments"]:
        dep = f"  - {d['department']}: {d['mandate']}"
        if d["depends_on"]:
            dep += f"  (depends on: {', '.join(d['depends_on'])})"
        L.append(dep)
    L.append(f"Team        : {', '.join(r['plan']['team']) or '(none)'}")
    if r["plan"]["hitl_points"]:
        L.append("Human gates : " + "; ".join(r["plan"]["hitl_points"]))
    L.append("")

    L.append("-" * 68)
    L.append("2. EXECUTION (per department)")
    L.append("-" * 68)
    for blk in r["execution"]:
        L.append(f"[{blk['department'].upper()}]  manager: {blk['manager']}  "
                 f"status: {blk['status']}")
        for w in blk["workers"]:
            flag = "  ⚑sensitive" if w["flags"].get("sensitive") else ""
            L.append(f"   • {w['worker']} ({w['tool_used'] or 'no tool'}) -> "
                     f"{w['status']}{flag}")
            if w["output"]:
                L.append(f"       {w['output'][:140]}")
        L.append("")

    L.append("-" * 68)
    L.append("3. GOVERNANCE")
    L.append("-" * 68)
    g = r["governance"]
    if g["outcome"] in ("BLOCK", "HALT"):
        L.append(f"OUTCOME     : 🔴 {g['outcome']} - rule {g['rule_id']} "
                 f"(raised by {g['raised_by']})")
        L.append(f"Reason      : {g['message']}")
        L.append(f"Explanation : {g['explanation']}")
        L.append(f"Owning dept : {g['owning_department'].upper()} - {g['alerted_admin']} alerted")
        L.append(f"Clearable by: {g['must_be_cleared_by']} (authority: {g['required_authority']})")
        if g.get("cleared_by"):
            L.append(f"Resolution  : cleared by {g['cleared_by']} - decision: {g['decision']}"
                     + (f" ({g['conditions']})" if g.get("conditions") else ""))
        else:
            L.append("Resolution  : PENDING - workflow frozen, the orchestrator cannot override.")
    elif g["outcome"] == "AWAITING_APPROVAL":
        L.append("OUTCOME     : ⏸ awaiting human approval")
        for a in g.get("approvals", []):
            L.append(f"   - {a}")
    else:
        L.append("OUTCOME     : ✅ clean - no policy violations.")
    L.append("")

    L.append("-" * 68)
    L.append("4. OUTCOME")
    L.append("-" * 68)
    m = r["outcome_metric"]
    if m["type"] == "readiness_score":
        L.append(f"Day-1 readiness : {m['value']}%")
    else:
        L.append(f"Decision        : {m['value']}")
    L.append(f"Total actions   : {r['total_actions']}")
    L.append(f"Executed by     : {r['executor']}")
    L.append("=" * 68)
    return "\n".join(L)
