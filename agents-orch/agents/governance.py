"""
governance.py — the system gate (overseers), AEGIS-style.

Two deterministic overseers run automatically AFTER the departments and BEFORE execution:
    compliance_overseer → checks hard rules → may raise a BLOCK (per-request veto)
    risk_overseer       → checks spend ceilings → may raise a HALT (whole run)

The decision is DETERMINISTIC (rules + numbers), never an LLM — reliable and defensible.
An LLM may later *explain* a veto, but never decide it. A veto names the owning department
(so its admin is alerted) and the authority that can clear it (CISO/CFO/DPO).

This is the un-bypassable part: the graph routes a vetoed run only to human review; there is
no path to execution while a veto is active and uncleared.
"""

from __future__ import annotations

from schemas import (
    CompanyContext, DispatchPlan, DepartmentResult, Veto, HardRule,
    HireRecord, PurchaseRequest,
)


# which worker/step a rule is "about" → which department owns the freeze
_RULE_OWNER = {
    "SEC-04": ("it", "access_worker"),
    "PROC-07": ("finance", "vendor_risk_worker"),
    "FIN-12": ("finance", "budget_worker"),
    "DZ-INV-01": ("finance", "invoice_validator_worker"),
    "DZ-INV-02": ("finance", "fraud_sentinel_worker"),
    "DZ-INV-03": ("finance", "budget_worker"),
}


def check_compliance(
    extracted: dict,
    departments: dict[str, DepartmentResult],
    ctx: CompanyContext,
) -> Veto | None:
    """Run the compliance overseer: evaluate enabled hard rules deterministically."""
    domain = extracted.get("domain")
    for rule in ctx.hard_rules:
        if not rule.enabled or rule.action != "block":
            continue
        # only apply rules that are shared or match this request's domain
        if rule.domain not in ("shared", domain):
            continue
        if _violates(rule, extracted, departments):
            owner_dept, worker = _RULE_OWNER.get(rule.id, (_primary_dept(extracted), None))
            return Veto(
                raised_by="Compliance", rule_id=rule.id, scope="block",
                message=rule.message,
                explanation=f"{rule.description} (rule {rule.id}).",
                owning_department=owner_dept, blocked_worker=worker,
                required_authority=rule.required_authority,
            )
    return None


def check_risk(
    extracted: dict,
    ctx: CompanyContext,
) -> Veto | None:
    """Run the risk overseer: spend-ceiling halts."""
    amount = float(extracted.get("amount", 0) or 0)
    if amount > ctx.thresholds.hard_spend_ceiling:
        return Veto(
            raised_by="Risk", rule_id="FIN-12", scope="halt",
            message=f"Spend ${amount:,.0f} exceeds the hard ceiling "
                    f"${ctx.thresholds.hard_spend_ceiling:,.0f}.",
            explanation="Fund-wide halt for executive review.",
            owning_department="finance", blocked_worker="budget_worker",
            required_authority="CFO",
        )
    return None


# ───────────────────────── rule evaluation (deterministic) ─────────────────────────

def _violates(rule: HardRule, extracted: dict, departments: dict[str, DepartmentResult]) -> bool:
    """Evaluate a hard rule against the request. Specific, auditable checks per rule id."""
    if rule.id == "SEC-04":
        # contractor receiving production access
        is_contractor = extracted.get("employment_type") == "contractor"
        scope = extracted.get("access_scope") or []
        wants_prod = any(s in ("production", "prod") for s in scope)
        # also honor a worker-set sensitive flag from access_worker
        sensitive = _has_flag(departments, "it", "sensitive")
        return is_contractor and (wants_prod or sensitive)

    if rule.id == "PROC-07":
        return bool(extracted.get("is_data_processor")) and not extracted.get("has_dpa")

    if rule.id == "FIN-12":
        return float(extracted.get("amount", 0) or 0) > 1_000_000

    # ── Algeria invoice rules ──────────────────────────────
    if rule.id == "DZ-INV-01":
        # mandatory legal identifiers per Décret 05-468 (NIF+NIS+RC+AI + customer NIF)
        missing = (not extracted.get("supplier_nif")
                   or not extracted.get("supplier_nis")
                   or not extracted.get("supplier_rc")
                   or not extracted.get("supplier_ai")
                   or not extracted.get("customer_nif"))
        # fiscal stamp is required ONLY for cash payments
        if extracted.get("payment_method") == "cash" and not extracted.get("has_fiscal_stamp"):
            missing = True
        # honor the validator's own flag if it set one
        if _has_flag(departments, "finance", "invalid_invoice"):
            missing = True
        return bool(missing)

    if rule.id == "DZ-INV-02":
        # fraud/anomaly risk detected by the fraud sentinel
        return _has_flag(departments, "finance", "fraud_risk")

    if rule.id == "DZ-INV-03":
        return float(extracted.get("amount_ttc", 0) or 0) > 10_000_000

    return False


def _has_flag(departments: dict[str, DepartmentResult], dept: str, flag: str) -> bool:
    dr = departments.get(dept)
    if not dr:
        return False
    return any(w.flags.get(flag) for w in dr.workers)


def _primary_dept(extracted: dict) -> str:
    return "finance" if "amount" in extracted else "hr"


# ───────────────────────── approver (threshold-based HITL) ─────────────────────────

def needs_human_approval(extracted: dict, plan: DispatchPlan, ctx: CompanyContext) -> list[str]:
    """Deterministic approval requirements (separate from a hard block)."""
    needs = []

    if extracted.get("domain") == "invoice_ap":
        # Algerian AP approval thresholds (DZD): manager 500k, finance lead 2M, CFO 5M.
        amt = float(extracted.get("amount_ttc", 0) or 0)
        if amt > 5_000_000:
            needs.append(f"CFO: invoice {amt:,.0f} DZD exceeds the 5,000,000 DZD CFO threshold")
        elif amt > 2_000_000:
            needs.append(f"Finance Director: invoice {amt:,.0f} DZD exceeds the 2,000,000 DZD finance-lead limit")
        elif amt > 500_000:
            needs.append(f"Manager: invoice {amt:,.0f} DZD exceeds the 500,000 DZD manager limit")
        if extracted.get("is_foreign"):
            needs.append("Compliance Officer: foreign-currency invoice requires FX/compliance review")
        return needs

    amount = float(extracted.get("amount", 0) or 0)
    th = ctx.thresholds
    if amount > th.director_spend_limit:
        needs.append(f"CFO: spend ${amount:,.0f} exceeds director limit ${th.director_spend_limit:,.0f}")
    elif amount > th.manager_spend_limit:
        needs.append(f"Finance Director: spend ${amount:,.0f} exceeds manager limit ${th.manager_spend_limit:,.0f}")
    return needs
