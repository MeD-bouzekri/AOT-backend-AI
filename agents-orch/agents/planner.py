"""
planner.py - the Planner (the brain), first real agent.

Reads a natural-language request + the CompanyContext, and REASONS its way to a DispatchPlan:
which departments are needed, each one's mandate, dependencies, human checkpoints, deadline.

The intelligence lives in the LLM (Gemini 2.5 Flash). It is fed the company's real menu of
departments and capabilities and decides the team by judgment - e.g. a finance hire pulls in
the Finance department for payroll/comp, not just HR + IT.

Paths:
    LIVE  (default) -> Gemini reasons over the CompanyContext, returns a structured plan.
    DEMO            -> a deterministic fallback for stage safety if no key / offline.

Run standalone:
    python -m agents.planner
"""

from __future__ import annotations

import json
import re

from schemas import CompanyContext, DispatchPlan, DeptMandate
from llm import is_demo, build_llm, planner_llm
from context_loader import load_company_context


# ───────────────────────── the brain (LLM) ─────────────────────────

PLANNER_SYSTEM = """You are the Planner - the chief orchestrator of an enterprise
multi-agent system. You read one request in plain language and design the MINIMAL, CORRECT
team to handle it. You are precise, not exhaustive. A great plan includes exactly what the
request needs and nothing more.

CORE PRINCIPLES
- Right-size the team. Do NOT add a department or worker "just in case". Each one you
  include must have a concrete reason tied to THIS request. Fewer, well-justified agents beat
  a long list.
- Reason about second-order needs that a checklist would miss, e.g.:
    * A FINANCE or contractor hire needs Finance (payroll/compensation), not just HR + IT.
    * A purchase touching customer/personal data needs a data/security review.
    * A junior, non-sensitive onboarding may need only HR + IT.
- Managers are implicit. List only the WORKERS you need; the system runs the relevant
  department manager automatically. Do NOT list "*_manager" entries.
- Governance is automatic. The compliance and risk overseers ALWAYS run as a final gate -
  do NOT add "compliance_overseer", "risk_overseer", "approver", or "reporter" to
  required_workers. Instead, express oversight through hitl_points when a human must decide.

HOW TO DECIDE EACH FIELD
1. domain        - pick one of the company's request types.
2. confidence    - your honest certainty (0-1) about the domain + plan.
3. departments   - only those genuinely required; give each a one-line mandate; set
                   depends_on when one truly needs another's output first (otherwise leave
                   it empty so they run in PARALLEL).
4. required_workers - exact worker names from the menu, ONLY the ones needed. No managers,
                   no governance/reporter agents.
5. hitl_points   - where a HUMAN must approve, with the specific authority and the reason,
                   e.g. "CISO: production access for a contractor (SEC-04)",
                   "CFO: spend $95k exceeds director limit". Leave EMPTY when the request is
                   clearly within auto-approve limits and has no sensitive flag.
6. deadline_days - use the company's SLA for the domain unless the request implies urgency.
7. reasoning     - 2-4 tight sentences: why this domain, why these departments/workers, and
                   why these (or no) human approvals. Reference the actual thresholds/rules
                   you relied on. No filler.

If a capability the request needs is not in the menu, set status="needs_capability" and
describe the gap instead of forcing an ill-fitting worker.

Be decisive and concrete. Return ONLY the structured plan."""


def _context_brief(ctx: CompanyContext) -> str:
    depts = "\n".join(f"  - {d.key}: {d.name} - {d.responsibility}"
                      for d in ctx.departments if d.enabled)
    menu = "\n".join(f"  - {c.name} [{c.department}/{c.level}]: {c.role}"
                     for c in ctx.capabilities if c.enabled)
    types = "\n".join(f"  - {r.domain} ({r.label}); triggers: {r.trigger_hint}"
                      for r in ctx.request_types)
    return (
        f"COMPANY: {ctx.company.name} ({ctx.company.industry}, {ctx.company.size})\n\n"
        f"DEPARTMENTS:\n{depts}\n\n"
        f"AVAILABLE WORKERS (the menu - use exact names):\n{menu}\n\n"
        f"REQUEST TYPES:\n{types}\n\n"
        f"SPEND THRESHOLDS: {json.dumps(ctx.thresholds.model_dump())}\n"
        f"SLA DEFAULTS (days): {json.dumps(ctx.sla_defaults)}\n"
    )


_SCHEMA_HINT = """Return a JSON object with EXACTLY these fields:
{
  "domain": "hr_onboarding" | "procurement",
  "confidence": 0.0-1.0,
  "summary": "one short sentence",
  "extracted": { ...the parsed facts of the request... },
  "departments": [ { "department": "hr|it|finance", "mandate": "what they do", "depends_on": [] } ],
  "required_workers": ["exact worker names from the menu"],
  "hitl_points": ["AUTHORITY: reason, e.g. 'CFO: $95k exceeds director limit'"],
  "deadline_days": 7,
  "reasoning": "2-4 tight sentences referencing the thresholds/rules you used",
  "status": "ready",
  "gaps": []
}

EXTRACTED - fill the structured facts you parsed from the request:
- If domain is hr_onboarding:
    { "name": "...", "role": "...", "department": "hr|it|finance|sales|engineering",
      "seniority": "intern|junior|mid|senior|staff|exec",
      "employment_type": "full_time|contractor|intern", "remote": true|false,
      "location": "...", "handles_sensitive_data": true|false,
      "access_scope": ["production" if mentioned, else empty],
      // personal / payroll data - copy EXACTLY as written if present, else null:
      "email": "...|null", "phone": "...|null", "date_of_birth": "...|null",
      "nationality": "...|null", "national_id": "...|null", "tax_id_nif": "...|null",
      "bank_rib": "...|null", "social_security_cnas": "...|null",
      "marital_status": "...|null", "salary": "...|null", "contract_type": "...|null",
      "start_date": "...|null" }
  Do NOT invent these; only fill what the request actually states, verbatim.
- If domain is procurement:
    { "vendor": "...", "item": "...", "amount": <number, no $ or commas>,
      "recurring": true|false, "is_data_processor": true|false, "has_dpa": true|false,
      "contract_attached": true|false }
- If domain is invoice_ap (a supplier invoice / facture to pay):
    { "supplier": "...", "supplier_nif": "...|null", "customer_nif": "...|null",
      "invoice_number": "...|null", "amount_ht": <number>, "vat_amount": <number>,
      "amount_ttc": <number>, "currency": "DZD", "has_fiscal_stamp": true|false,
      "has_purchase_order": true|false, "has_goods_receipt": true|false,
      "is_foreign": true|false, "po_number": "...|null" }
  amount_ttc is the total to pay. If only one amount is given, put it in amount_ttc.

WORKER SELECTION RULES (critical - wrong workers give wrong results):
- hr_onboarding workers (ONLY these): docs_worker, benefits_worker, scheduler_worker,
  training_worker, accounts_worker, equipment_worker, access_worker, payroll_worker.
- procurement workers (ONLY these): budget_worker, vendor_risk_worker.
- invoice_ap workers (ONLY these): invoice_validator_worker (validate fields/NIF/VAT/stamp),
  matching_worker (three-way match), fraud_sentinel_worker (duplicate/split/threshold
  anomalies), budget_worker (budget check), payment_worker (schedule payment).
  Run validation + fraud + matching BEFORE payment.
- NEVER mix workers across domains.
- required_workers: worker-level names only. No "*_manager", no compliance_overseer,
  risk_overseer, approver, or reporter.
- departments: only those genuinely needed (finance for procurement and invoice_ap).
- hitl_points: empty if within auto-approve limits and no risk/missing-field flag."""


def _plan_live(raw_text: str, ctx: CompanyContext, precedents: list | None = None) -> DispatchPlan:
    # Use a higher token budget + explicit JSON instruction; Gemini Flash truncates
    # otherwise. We parse the JSON ourselves into the Pydantic model for full control.
    cfg = planner_llm().model_copy(update={"max_tokens": 4096})
    llm = build_llm(cfg)
    precedent_block = ""
    if precedents:
        lines = []
        for p in precedents:
            if not p.get("summary"):
                continue
            line = f"- {p['summary']} -> outcome: {p.get('outcome','?')}"
            if p.get("rule_id"):
                line += f" (rule {p['rule_id']})"
            if p.get("lesson"):
                line += f" - lesson: {p['lesson']}"
            lines.append(line)
        if lines:
            precedent_block = (
                "\n=== INSTITUTIONAL MEMORY (similar past cases) ===\n"
                + "\n".join(lines)
                + "\nConsider these precedents; if a past case was blocked or flagged for a "
                  "reason that applies here, proactively account for it in your plan and "
                  "reasoning.\n"
            )
    prompt = (
        f"{PLANNER_SYSTEM}\n\n"
        f"=== COMPANY CONTEXT ===\n{_context_brief(ctx)}\n"
        f"{precedent_block}"
        f"=== REQUEST ===\n{raw_text}\n\n"
        f"{_SCHEMA_HINT}\n\n"
        f"Respond with ONLY the JSON object, no markdown fences, no commentary."
    )
    resp = llm.invoke(prompt)
    text = resp.content if hasattr(resp, "content") else str(resp)
    data = _extract_json(text)
    plan = DispatchPlan.model_validate(data)
    return _normalize(plan, ctx)


# governance/manager agents are run automatically by the graph, never selected by the Planner
_AUTO_AGENTS = {"compliance_overseer", "risk_overseer", "approver", "reporter"}

# which workers are valid for each domain (a purchase must not run onboarding workers)
_DOMAIN_WORKERS = {
    "procurement": {"budget_worker", "vendor_risk_worker"},
    "hr_onboarding": {"docs_worker", "benefits_worker", "scheduler_worker",
                      "training_worker", "accounts_worker", "equipment_worker",
                      "access_worker", "payroll_worker"},
    "invoice_ap": {"invoice_validator_worker", "matching_worker",
                   "fraud_sentinel_worker", "budget_worker", "payment_worker"},
}
# departments that make sense per domain
_DOMAIN_DEPTS = {
    "procurement": {"finance"},
    "hr_onboarding": {"hr", "it", "finance"},
    "invoice_ap": {"finance"},
}


def _normalize(plan: DispatchPlan, ctx: CompanyContext) -> DispatchPlan:
    """Keep only real, domain-appropriate worker names; drop managers/governance/strays."""
    valid_workers = {c.name for c in ctx.capabilities if c.level == "worker"}
    allowed = _DOMAIN_WORKERS.get(plan.domain, valid_workers)
    plan.required_workers = [
        w for w in plan.required_workers
        if w in valid_workers and w in allowed and w not in _AUTO_AGENTS
    ]
    # if the LLM gave nothing usable, fall back to a sensible default set for the domain
    if not plan.required_workers:
        plan.required_workers = sorted(allowed & valid_workers)

    allowed_depts = _DOMAIN_DEPTS.get(plan.domain, set())
    plan.departments = [d for d in plan.departments
                        if d.department != "governance"
                        and (not allowed_depts or d.department in allowed_depts)]
    # ensure at least the departments implied by the chosen workers are present
    worker_depts = {c.department for c in ctx.capabilities
                    if c.name in plan.required_workers}
    have = {d.department for d in plan.departments}
    for dept in worker_depts - have:
        from schemas import DeptMandate
        plan.departments.append(DeptMandate(department=dept, mandate=f"Handle {dept} tasks."))
    return plan


def _extract_json(text: str) -> dict:
    """Pull the JSON object out of an LLM response, tolerating common LLM glitches."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1:
        text = text[start:end + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Tolerate trailing commas and stray newlines inside strings.
        cleaned = re.sub(r",(\s*[}\]])", r"\1", text)        # trailing commas
        cleaned = re.sub(r"//.*?$", "", cleaned, flags=re.MULTILINE)  # // comments
        return json.loads(cleaned)


# ───────────────────────── deterministic fallback (stage safety only) ─────────────────────────

def _classify_domain(text: str, ctx: CompanyContext) -> tuple[str, float]:
    t = text.lower()
    if re.search(r"\$\s?\d", t) or any(w in t for w in
                                       ("approve", "subscription", "license", "vendor", "buy")):
        return "procurement", 0.8
    return "hr_onboarding", 0.7


def _plan_fallback(raw_text: str, ctx: CompanyContext) -> DispatchPlan:
    """Used only when MODE=demo or the LLM is unreachable. Coarse but safe."""
    domain, conf = _classify_domain(raw_text, ctx)
    t = raw_text.lower()
    if domain == "procurement":
        amount = _amount(t)
        depts = [DeptMandate(department="finance",
                             mandate="Validate budget, assess vendor risk, prepare payment.")]
        workers = ["budget_worker", "vendor_risk_worker"]
        hitl = (["approver:CFO"] if amount > ctx.thresholds.director_spend_limit
                else ["approver:Finance Director"] if amount > ctx.thresholds.manager_spend_limit
                else [])
        return DispatchPlan(domain="procurement", confidence=conf,
                            summary=f"Procurement (${amount:,.0f})", departments=depts,
                            required_workers=workers, hitl_points=hitl,
                            deadline_days=ctx.sla_defaults.get("procurement", 5),
                            reasoning="Fallback plan (LLM unavailable).")
    # onboarding - include Finance when the hire is finance-related
    depts = [DeptMandate(department="hr", mandate="Documents, benefits, scheduling."),
             DeptMandate(department="it", mandate="Accounts, equipment, access.")]
    workers = ["docs_worker", "scheduler_worker", "accounts_worker", "equipment_worker",
               "access_worker"]
    if "financ" in t:
        depts.append(DeptMandate(department="finance",
                                 mandate="Set up payroll and compensation profile."))
        workers.append("payroll_worker")
    if "contractor" not in t and "intern" not in t:
        workers.insert(1, "benefits_worker")
    hitl = ["access_worker"] if any(w in t for w in ("pii", "prod", "sensitive")) else []
    return DispatchPlan(domain="hr_onboarding", confidence=conf, summary="Onboarding",
                        departments=depts, required_workers=workers, hitl_points=hitl,
                        deadline_days=ctx.sla_defaults.get("hr_onboarding", 7),
                        reasoning="Fallback plan (LLM unavailable).")


def _amount(t: str) -> float:
    m = re.search(r"\$\s?([\d,]+(?:\.\d+)?)\s?(k|m)?", t)
    if not m:
        return 0.0
    v = float(m.group(1).replace(",", ""))
    return v * (1_000 if m.group(2) == "k" else 1_000_000 if m.group(2) == "m" else 1)


# ───────────────────────── public entry ─────────────────────────

def plan_request(raw_text: str, ctx: CompanyContext | None = None,
                 precedents: list | None = None) -> DispatchPlan:
    """Produce a DispatchPlan. LLM-first; deterministic fallback on demo/offline/error.

    `precedents` = similar past cases from Institutional Memory, fed to the Planner so it can
    learn from history (e.g. a vendor that previously split invoices).
    """
    ctx = ctx or load_company_context()
    if is_demo():
        return _plan_fallback(raw_text, ctx)
    try:
        return _plan_live(raw_text, ctx, precedents or [])
    except Exception as exc:  # noqa: BLE001 - never crash the demo on an LLM hiccup
        plan = _plan_fallback(raw_text, ctx)
        plan.reasoning += f"  [fell back: {type(exc).__name__}: {exc}]"
        return plan


if __name__ == "__main__":
    samples = [
        "Onboard Maya, an intern sales rep, remote in Berlin, starting Monday.",
        "Hire a senior finance analyst, a girl, based in NYC.",
        "Approve a $95k annual Slack + Zoom subscription for our customer data platform.",
        "Approve a $9k Notion license.",
        "Onboard Priya, a mid software engineer contractor, remote, needs production access.",
    ]
    context = load_company_context()
    for s in samples:
        plan = plan_request(s, context)
        print("\n" + "=" * 72)
        print("REQUEST:", s)
        print(f"DOMAIN:  {plan.domain}  (confidence {plan.confidence})")
        print("DEPTS:  ", [(d.department, d.depends_on) for d in plan.departments])
        print("WORKERS:", plan.required_workers)
        print("HITL:   ", plan.hitl_points)
        print("WHY:    ", plan.reasoning)
