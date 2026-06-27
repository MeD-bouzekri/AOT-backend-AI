"""
graph.py - the REAL LangGraph orchestration.

A proper StateGraph wires the agents into a stateful flow with conditional routing:

    START -> planner -> departments -> governance -> (FROZEN | approver) -> reporter -> END
                                          │
                                          └─ conditional edge: if a Veto is raised, route to
                                             the FROZEN terminal (no path to execution).

What is REAL here:
    - the LangGraph StateGraph (nodes, edges, conditional routing, shared state)
    - the Planner and workers reasoning with a live LLM (Gemini) unless MODE=demo
    - the governance enforcement (deterministic) and the un-bypassable freeze branch
What is MOCKED:
    - only the leaf tools (credit_check, grant_access, ...) - the external-system calls.
      Everything orchestrating them is real.

Run:
    python graph.py "Approve a $95k Slack subscription for our customer data platform"
"""

from __future__ import annotations

import re
from typing import TypedDict, Optional, Annotated
from operator import add

from langgraph.graph import StateGraph, START, END

from schemas import (
    DispatchPlan, DepartmentResult, Veto, StepEvent, CompanyContext,
)
from context_loader import load_company_context
from agents.planner import plan_request
from agents.manager import run_department, workers_for_department
from agents import governance as gov
from report import build_report
import memory


# ───────────────────────── graph state ─────────────────────────

class OrchState(TypedDict, total=False):
    run_id: str
    raw_text: str
    extracted: dict
    plan: DispatchPlan
    precedents: list
    departments: dict[str, DepartmentResult]
    veto: Optional[Veto]
    approvals: list[str]
    report: str
    report_obj: dict
    status: str
    events: Annotated[list[StepEvent], add]   # nodes append; reducer concatenates


_CTX: CompanyContext = load_company_context()


def _ev(state, level, agent, department, **kw) -> StepEvent:
    return StepEvent(run_id=state.get("run_id", "run"), department=department,
                     level=level, agent=agent, **kw)


# ───────────────────────── nodes ─────────────────────────

def node_planner(state: OrchState) -> OrchState:
    raw = state["raw_text"]
    # Institutional Memory: recall similar past cases to inform the plan.
    precedents = memory.recall(raw, k=3)
    events = []
    if precedents:
        cites = "; ".join(
            f"{p.get('summary','?')} -> {p.get('outcome','?')}"
            + (f" ({p['lesson']})" if p.get("lesson") else "")
            for p in precedents if p.get("summary"))
        if cites:
            events.append(_ev(state, "planner", "institutional_memory", "system",
                              phase="Recalled precedents", status="done",
                              output=cites, reasoning="Similar past cases retrieved."))

    plan = plan_request(raw, _CTX, precedents=precedents)
    extracted = _normalize_extracted(plan, raw)
    events.append(_ev(state, "planner", "planner", "system",
                      phase="Plan ready", status="done",
                      output=plan.summary, reasoning=plan.reasoning))
    return {"plan": plan, "extracted": extracted, "precedents": precedents, "events": events}


def node_departments(state: OrchState) -> OrchState:
    plan = state["plan"]
    extracted = state["extracted"]
    departments: dict[str, DepartmentResult] = {}
    events: list[StepEvent] = []

    for dm in plan.departments:
        dept = dm.department
        wnames = workers_for_department(dept, plan.required_workers, _CTX)
        if not wnames:
            continue

        def mgr_emit(d):
            events.append(_ev(state, d.get("level", "worker"), d["agent"], d["department"],
                              assigned_by=d.get("assigned_by"), phase=d.get("phase", ""),
                              status=d.get("status", "done"), output=d.get("output"),
                              reasoning=d.get("reasoning"), tools_used=d.get("tools_used", [])))

        dr = run_department(dept, wnames, extracted, _CTX, emit=mgr_emit)
        departments[dept] = dr

    return {"departments": departments, "events": events}


def node_governance(state: OrchState) -> OrchState:
    extracted = state["extracted"]
    departments = state.get("departments", {})
    events = [_ev(state, "governance", "compliance_overseer", "system",
                  phase="Checking hard policy rules…", status="running")]

    veto = (gov.check_duplicate_employee(extracted)
            or gov.check_compliance(extracted, departments, _CTX)
            or gov.check_risk(extracted, _CTX))

    if veto:
        # a duplicate identity is a hard rejection (denied); policy freezes await an authority
        is_denied = veto.rule_id == "HR-DUP-01"
        events.append(_ev(state, "governance", veto.raised_by.lower() + "_overseer",
                          veto.owning_department,
                          phase=f"{'DENIED' if is_denied else veto.scope.upper()} - {veto.rule_id}",
                          status="blocked",
                          output=veto.message, reasoning=veto.explanation,
                          policy_citation=veto.rule_id))
        return {"veto": veto, "status": "denied" if is_denied else "frozen", "events": events}

    events.append(_ev(state, "governance", "compliance_overseer", "system",
                      phase="No violations", status="done"))
    approvals = gov.needs_human_approval(extracted, state["plan"], _CTX)
    return {"veto": None, "approvals": approvals, "events": events}


def node_frozen(state: OrchState) -> OrchState:
    veto = state["veto"]
    # a hard rejection (duplicate identity) is terminal - no authority can clear it
    denied = veto.rule_id == "HR-DUP-01"
    final_status = "denied" if denied else "frozen"
    report = build_report(
        run_id=state.get("run_id", "run"), company=_CTX.company.name,
        raw_text=state["raw_text"], plan=state["plan"],
        departments=state.get("departments", {}), veto=veto,
        approvals=[], status=final_status,
    )
    if denied:
        alert = _ev(state, "governance", "system", veto.owning_department,
                    phase="Rejected - duplicate identity", status="blocked",
                    output=veto.message)
    else:
        from tools.builtin import employee_directory
        holders = employee_directory(authority=veto.required_authority).get("holders", [])
        who = (f"{holders[0]['name']} ({holders[0]['role']}, {holders[0]['email']})"
               if holders else veto.required_authority)
        alert = _ev(state, "governance", "system", veto.owning_department,
                    phase=f"Frozen - awaiting {veto.required_authority}", status="awaiting_human",
                    output=f"Alert routed to the {veto.owning_department.upper()} department admin. "
                           f"Only {who} can clear this (rule {veto.rule_id}).")
    return {"status": final_status, "report": report["text"], "report_obj": report, "events": [
        alert,
        _ev(state, "governance", "reporter", "system", phase="Report (frozen)",
            status="done", output=report["text"]),
    ]}


def node_reporter(state: OrchState) -> OrchState:
    approvals = state.get("approvals", [])
    status = "awaiting_human" if approvals else "done"
    # a clean, fully-approved onboarding actually creates the employee record
    if status == "done":
        _create_employee_record(state)
    report = build_report(
        run_id=state.get("run_id", "run"), company=_CTX.company.name,
        raw_text=state["raw_text"], plan=state["plan"],
        departments=state.get("departments", {}), veto=None,
        approvals=approvals, status=status,
    )
    return {"report": report["text"], "report_obj": report, "status": status, "events": [
        _ev(state, "governance", "reporter", "system", phase="Final report",
            status="done", output=report["text"])
    ]}


def _create_employee_record(state: OrchState) -> None:
    """Persist a new employee when an HR onboarding completes cleanly. This is what
    the Employees dashboard page reads - proof the system actually onboarded the hire."""
    e = state.get("extracted", {})
    if e.get("domain") != "hr_onboarding" or not e.get("name"):
        return
    try:
        import db as _db  # type: ignore
        from sqlmodel import select, func
        with _db.get_session() as s:
            # next employee code: E### after the current max
            count = s.exec(select(func.count()).select_from(_db.EmployeeRow)).one()
            code = f"E{count + 1:03d}"
            s.add(_db.EmployeeRow(
                employee_code=code,
                name=e.get("name"),
                email=e.get("email"),
                role=e.get("role"),
                department=e.get("department"),
                seniority=e.get("seniority"),
                employment_type=e.get("employment_type"),
                location=e.get("location"),
                national_id=e.get("national_id"),
                tax_id_nif=e.get("tax_id_nif"),
                salary=e.get("salary"),
                contract_type=e.get("contract_type"),
                start_date=e.get("start_date"),
                status="active",
                source_run_id=state.get("run_id"),
            ))
            s.commit()
    except Exception:  # noqa: BLE001 - record creation must never break the run
        pass


# ───────────────────────── conditional routing ─────────────────────────

def route_after_governance(state: OrchState) -> str:
    """The un-bypassable branch: a veto routes ONLY to the frozen terminal."""
    return "frozen" if state.get("veto") else "reporter"


# ───────────────────────── build the graph ─────────────────────────

def build_graph():
    g = StateGraph(OrchState)
    g.add_node("planner", node_planner)
    g.add_node("departments", node_departments)
    g.add_node("governance", node_governance)
    g.add_node("frozen", node_frozen)
    g.add_node("reporter", node_reporter)

    g.add_edge(START, "planner")
    g.add_edge("planner", "departments")
    g.add_edge("departments", "governance")
    g.add_conditional_edges("governance", route_after_governance,
                            {"frozen": "frozen", "reporter": "reporter"})
    g.add_edge("frozen", END)
    g.add_edge("reporter", END)
    return g.compile()


GRAPH = build_graph()


# ───────────────────────── helpers ─────────────────────────

# small models sometimes copy the schema's "...|null" placeholder or echo the
# field label ("NIF 987...") into a value; strip both so workers cite clean data.
_PII_LABELS = re.compile(r"^(nif|nis|rc|ai|cni|cnas|rib|tax id|national id|chifa)\s*[:.]?\s*",
                         re.IGNORECASE)


def _clean_extracted_strings(e: dict) -> dict:
    out = {}
    for k, v in e.items():
        if isinstance(v, str):
            s = v.strip()
            if s.endswith("|null"):
                s = s[:-5].strip()
            if s.lower() in ("null", "none", ""):
                out[k] = None
                continue
            s = _PII_LABELS.sub("", s)
            out[k] = s
        else:
            out[k] = v
    return out


def _normalize_extracted(plan: DispatchPlan, raw_text: str) -> dict:
    """Use the Planner's structured `extracted` as the source of truth; fill safe defaults
    and a regex-derived amount as a backstop. Always carries the domain so workers know
    whether they are handling a hire or a purchase.
    """
    e = dict(plan.extracted or {})
    e = _clean_extracted_strings(e)
    e["domain"] = plan.domain
    e["raw"] = raw_text
    # expose the hire's name as `person` for the action tools (avoids the `name` collision)
    if e.get("name"):
        e["person"] = e["name"]

    # amount backstop (Planner usually fills it; regex covers any miss)
    if not e.get("amount") and plan.domain == "procurement":
        # require at least one digit so a stray "$" or comma can't match empty
        m = re.search(r"\$?\s?(\d[\d,]*(?:\.\d+)?)\s?(k|m)?", raw_text.lower())
        if m:
            num = m.group(1).replace(",", "")
            try:
                e["amount"] = float(num) * (
                    1_000 if m.group(2) == "k" else 1_000_000 if m.group(2) == "m" else 1)
            except ValueError:
                pass  # not a parseable amount - leave it for the default below
    e.setdefault("amount", 0.0)

    if plan.domain == "hr_onboarding":
        e.setdefault("employment_type", "full_time")
        e.setdefault("seniority", "mid")
        e.setdefault("remote", False)
        e.setdefault("access_scope", [])
        e.setdefault("department", "general")
        e.setdefault("role", "New Hire")
        e.setdefault("handles_sensitive_data", False)
    elif plan.domain == "invoice_ap":
        e.setdefault("department", "finance")
        e.setdefault("currency", "DZD")
        e.setdefault("payment_method", "bank_transfer")
        e.setdefault("vat_rate", 19)
        e.setdefault("has_fiscal_stamp", False)
        e.setdefault("has_purchase_order", False)
        e.setdefault("has_goods_receipt", False)
        # the amount thresholds key off TTC
        if not e.get("amount_ttc") and e.get("amount"):
            e["amount_ttc"] = e["amount"]
        e["amount"] = e.get("amount_ttc", 0.0)
    else:  # procurement
        e.setdefault("vendor", "Vendor")
        e.setdefault("is_data_processor", False)
        e.setdefault("has_dpa", False)
        e.setdefault("department", "finance")   # finance owns the budget for a purchase
    return e


def run(raw_text: str, run_id: str = "run-local") -> OrchState:
    """Invoke the compiled graph for one request, then record it to Institutional Memory."""
    final = GRAPH.invoke({"raw_text": raw_text, "run_id": run_id, "events": []})
    _remember_run(run_id, raw_text, final)
    return final


def _remember_run(run_id: str, raw_text: str, final: OrchState) -> None:
    """Persist the finished run so future requests can learn from it (the feedback loop)."""
    try:
        plan = final.get("plan")
        veto = final.get("veto")
        rec = memory.make_record(
            run_id=run_id,
            domain=getattr(plan, "domain", "unknown"),
            summary=getattr(plan, "summary", raw_text[:60]),
            request=raw_text,
            outcome=final.get("status", "done"),
            rule_id=getattr(veto, "rule_id", None),
            department=getattr(veto, "owning_department", None),
            lesson=(f"{veto.message}" if veto else ""),
        )
        memory.remember(rec)
    except Exception:  # noqa: BLE001 - memory must never break a run
        pass


if __name__ == "__main__":
    import sys
    samples = [
        "Approve a $95k annual Slack + Zoom subscription for our customer data platform.",
        "Onboard Priya, a mid software engineer contractor, remote, needs production access.",
        "Approve a $9k Notion license.",
    ]
    reqs = sys.argv[1:] or samples
    for i, s in enumerate(reqs):
        print("\n" + "#" * 76)
        print("REQUEST:", s)
        final = run(s, run_id=f"run-{i}")
        print(final.get("report", "(no report)"))
