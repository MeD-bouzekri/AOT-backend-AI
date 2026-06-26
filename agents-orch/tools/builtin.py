"""
builtin.py — the 18 built-in tools (mock-backed for the demo).

Two kinds of tools the system uses:
    DATA tools   — read company "truth" (catalogs, directory, risk lookups). Return dicts.
    ACTION tools — perform a side effect (create account, grant access...). Return
                   {status, ref_id, detail}.

All are MOCK: they return realistic data without touching real SaaS. To go live later,
swap a function body for a real API call — the agents and graph never change.

User-built tools (created by an admin in the dashboard) are NOT here; they are config with a
`mock_response` template, run by tools/runner.py. No code execution for those — safe.
"""

from __future__ import annotations

import json
import random
import uuid
from datetime import date, timedelta
from pathlib import Path


def _ref(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:6].upper()}"


# ───────────────────────── company config (admin-provided data) ─────────────────────────
# All company-specific policy lives in data/company_config.json — filled by the admin in the
# setup wizard. Tools READ it; nothing is hardcoded. A different company ships a different
# file and the agents behave differently with zero code change.

_CONFIG_CACHE: dict | None = None


def _config() -> dict:
    global _CONFIG_CACHE
    if _CONFIG_CACHE is None:
        path = Path(__file__).parent.parent / "data" / "company_config.json"
        _CONFIG_CACHE = json.loads(path.read_text(encoding="utf-8"))
    return _CONFIG_CACHE


def reload_config() -> None:
    """Call after the admin edits the config so tools pick up new values."""
    global _CONFIG_CACHE
    _CONFIG_CACHE = None


# ───────────────────────── DATA tools (read company truth) ─────────────────────────

def doc_catalog(employment_type: str = "full_time", location: str = "", remote: bool = False,
                handles_sensitive_data: bool = False, **_) -> dict:
    """Required documents + deadlines, read from the company's config."""
    cfg = _config()["documents"]
    docs = list(cfg.get("base", []))
    docs += cfg.get(employment_type, [])
    if remote or (location and "remote" in location.lower()):
        docs += cfg.get("remote_extra", [])
    if handles_sensitive_data:
        docs += cfg.get("sensitive_extra", [])
    return {"required_documents": [d["doc"] for d in docs], "documents": docs,
            "count": len(docs)}


def training_catalog(handles_sensitive_data: bool = False, **_) -> dict:
    """Mandatory trainings + deadlines, read from the company's config."""
    cfg = _config()["trainings"]
    trainings = list(cfg.get("base", []))
    if handles_sensitive_data:
        trainings += cfg.get("sensitive_extra", [])
    return {"trainings": trainings, "count": len(trainings)}


def benefits_catalog(employment_type: str = "full_time", **_) -> dict:
    """Benefits package, read from the company's config."""
    cfg = _config()["benefits"]
    pkg = cfg.get(employment_type, [])
    return {"employment_type": employment_type, "benefits": pkg, "eligible": bool(pkg),
            "enrollment_deadline_days": cfg.get("enrollment_window_days") if pkg else None}


def app_catalog(department: str = "", **_) -> dict:
    """Accounts to create, read from the company's config."""
    cfg = _config()["accounts"]
    apps = list(cfg.get("base", [])) + cfg.get("by_department", {}).get(department.lower(), [])
    return {"department": department, "accounts": apps, "count": len(apps),
            "email_sla": cfg.get("email_sla"), "role_tools_sla": cfg.get("role_tools_sla")}


def equipment_catalog(seniority: str = "mid", remote: bool = False,
                      department: str = "", role: str = "", **_) -> dict:
    """Equipment tier + delivery, read from the company's config."""
    cfg = _config()
    tiers = cfg["equipment_tiers"]
    addons = cfg["equipment_addons"]
    eng_kw = cfg.get("engineer_keywords", [])
    is_engineer = any(k in (department + " " + role).lower() for k in eng_kw)
    laptop = tiers.get(seniority, tiers.get("mid", "Laptop"))
    items = [laptop] + (addons["engineer"] if is_engineer else addons["default"])
    allowance = cfg.get("remote_home_office_allowance_usd", 0) if remote else 0
    return {"seniority": seniority, "is_engineer": is_engineer, "equipment": items,
            "delivery": "Ship to home address" if remote else "Desk assignment at office",
            "home_office_allowance_usd": allowance,
            "sla": cfg.get("equipment_sla"), "remote": remote}


def access_matrix(role: str = "", department: str = "", access_scope: list | None = None,
                  employment_type: str = "full_time", **_) -> dict:
    """Access scope + approval requirement, read from the company's config."""
    cfg = _config()["access"]
    access_scope = access_scope or []
    baseline = list(cfg.get("baseline", []))
    elevated_scopes = set(cfg.get("elevated_scopes", []))
    elevated = [s for s in access_scope if s in elevated_scopes]
    sensitive = bool(elevated)
    return {"role": role, "baseline_access": baseline, "requested_access": access_scope,
            "elevated_requested": elevated, "sensitive": sensitive,
            "employment_type": employment_type,
            "approval_required": (cfg.get("elevated_approval") if sensitive else "standard"),
            "least_privilege_note": cfg.get("principle", "least privilege")}


def budget_store(department: str = "finance", **_) -> dict:
    """Department budget, read from the company's config."""
    budgets = _config()["department_budgets"]
    total = budgets.get(department.lower(), budgets.get("default", 250_000))
    spent = int(total * random.uniform(0.2, 0.5))
    return {"department": department, "annual_budget": total, "spent": spent,
            "remaining": total - spent}


# ───────────────────────── invoice / AP tools (Algeria finance) ─────────────────────────

_ALGERIA_CACHE: dict | None = None


def _algeria() -> dict:
    global _ALGERIA_CACHE
    if _ALGERIA_CACHE is None:
        path = Path(__file__).parent.parent / "data" / "algeria-finance.json"
        _ALGERIA_CACHE = json.loads(path.read_text(encoding="utf-8"))
    return _ALGERIA_CACHE


def invoice_validator(supplier_nif: str = "", supplier_nis: str = "", supplier_rc: str = "",
                      supplier_ai: str = "", customer_nif: str = "", invoice_number: str = "",
                      payment_method: str = "bank_transfer", has_fiscal_stamp: bool = False,
                      vat_rate: int = 19, amount_ht: float = 0.0, vat_amount: float = 0.0,
                      amount_ttc: float = 0.0, **_) -> dict:
    """Validate an Algerian invoice per Décret exécutif 05-468 + Loi de Finances 2022.

    Mandatory identifiers: NIF + NIS + RC + AI (supplier) and customer NIF.
    Fiscal stamp (timbre fiscal) is required ONLY for cash payments (1%, 5–2500 DZD).
    """
    rules = _algeria()
    tax = rules["tax_rules"]
    missing = []
    # the four legal identifiers (LF 2022: missing supplier NIF loses VAT deduction)
    if not supplier_nif:
        missing.append("supplier_nif")
    if not supplier_nis:
        missing.append("supplier_nis")
    if not supplier_rc:
        missing.append("supplier_rc")
    if not supplier_ai:
        missing.append("supplier_ai")
    if not customer_nif:
        missing.append("customer_nif")
    if not invoice_number:
        missing.append("invoice_number")

    # fiscal stamp ONLY when paying cash
    stamp_note = "not applicable (non-cash payment)"
    if payment_method == "cash":
        if not has_fiscal_stamp:
            missing.append("fiscal_stamp")
        expected_stamp = min(max(amount_ttc * tax["fiscal_stamp_rate_percent"] / 100,
                                 tax["fiscal_stamp_min_dzd"]), tax["fiscal_stamp_max_dzd"])
        stamp_note = f"cash payment → 1% stamp ≈ {expected_stamp:,.0f} DZD (bounded 5–2500)"

    # VAT verification (rate 19 or 9)
    rate = vat_rate if vat_rate in (tax["vat_rate_standard"], tax["vat_rate_reduced"]) else tax["vat_rate_standard"]
    vat_ok = True
    vat_note = ""
    if amount_ht:
        expected = round(amount_ht * rate / 100, 2)
        vat_ok = abs(expected - vat_amount) < max(1.0, expected * 0.02)
        vat_note = f"expected VAT @ {rate}% = {expected:,.2f}, invoice has {vat_amount:,.2f}"
        if amount_ttc and abs((amount_ht + vat_amount) - amount_ttc) > 1.0:
            vat_ok = False
            vat_note += "; TTC != HT + VAT"

    valid = not missing and vat_ok
    return {"valid": valid, "missing_fields": missing, "vat_ok": vat_ok, "vat_note": vat_note,
            "fiscal_stamp_note": stamp_note, "payment_method": payment_method,
            "legal_basis": rules.get("legal_basis", ""),
            "mandatory_fields": rules["invoice_requirements"]["mandatory_fields"]}


def nif_check(supplier_nif: str = "", **_) -> dict:
    """Validate a supplier Tax ID (NIF). Algerian NIF is a 15-digit number."""
    nif = (supplier_nif or "").replace(" ", "")
    valid = nif.isdigit() and len(nif) == 15
    return {"supplier_nif": supplier_nif, "valid_format": valid,
            "note": "NIF must be 15 digits" if not valid else "NIF format OK",
            "registered": valid}   # mock registry check


def three_way_match(has_purchase_order: bool = False, has_goods_receipt: bool = False,
                    po_number: str = "", amount_ttc: float = 0.0, **_) -> dict:
    """Invoice + Purchase Order + Goods Receipt must match (Algeria compliance)."""
    matched = bool(has_purchase_order and has_goods_receipt)
    issues = []
    if not has_purchase_order:
        issues.append("missing purchase order")
    if not has_goods_receipt:
        issues.append("missing goods receipt")
    return {"matched": matched, "po_number": po_number, "issues": issues,
            "note": "3-way match complete" if matched else "3-way match incomplete"}


def fraud_check(supplier: str = "", supplier_nif: str = "", amount_ttc: float = 0.0,
                invoice_number: str = "", **_) -> dict:
    """Anomaly/fraud heuristics from the Algeria fraud rules + recent-invoice history."""
    rules = _algeria()["fraud_and_anomaly_rules"]
    history = _recent_invoices()
    flags = []

    # duplicate (same supplier + same amount, or same invoice number)
    dupes = [h for h in history
             if h.get("invoice_number") == invoice_number
             or (h.get("supplier") == supplier and abs(h.get("amount_ttc", 0) - amount_ttc) < 1)]
    if rules.get("flag_duplicate_invoices") and dupes:
        flags.append("duplicate_invoice")

    # split invoices (same supplier, many invoices this month)
    same_vendor = [h for h in history if h.get("supplier") == supplier]
    if (rules.get("flag_split_invoices")
            and len(same_vendor) + 1 >= rules.get("same_vendor_multiple_invoices_monthly_threshold", 3)):
        flags.append("possible_split_invoices")

    # amount just below an approval threshold
    if rules.get("amount_just_below_threshold_flag"):
        thresholds = _algeria()["approval_thresholds"].values()
        for t in thresholds:
            if 0 < (t - amount_ttc) <= t * 0.03:   # within 3% just under a threshold
                flags.append("amount_just_below_threshold")
                break

    risk = "high" if flags else "low"
    return {"supplier": supplier, "flags": flags, "risk": risk,
            "duplicates_found": len(dupes), "vendor_invoices_this_month": len(same_vendor)}


def _recent_invoices() -> list[dict]:
    path = Path(__file__).parent.parent / "data" / "recent_invoices.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return []
    return []


def record_payment(supplier: str = "", amount_ttc: float = 0.0, currency: str = "DZD",
                   method: str = "bank_transfer", **_) -> dict:
    """Record an approved supplier payment (mock side effect)."""
    return {"status": "scheduled", "ref_id": _ref("PAY"),
            "detail": f"Scheduled {method} of {amount_ttc:,.2f} {currency} to {supplier}."}


def vendor_directory(vendor: str = "", **_) -> dict:
    known = {
        "slack": {"tier": "enterprise", "years_active": 11, "soc2": True},
        "zoom": {"tier": "enterprise", "years_active": 12, "soc2": True},
        "notion": {"tier": "smb", "years_active": 6, "soc2": True},
        "figma": {"tier": "smb", "years_active": 8, "soc2": True},
    }
    info = known.get(vendor.lower(), {"tier": "unknown", "years_active": 1, "soc2": False})
    return {"vendor": vendor, **info, "on_file": vendor.lower() in known}


def credit_check(vendor: str = "", **_) -> dict:
    score = random.choice([720, 690, 755, 640, 780])
    rating = "A" if score >= 750 else "B+" if score >= 700 else "B" if score >= 650 else "C"
    return {"vendor": vendor, "credit_score": score, "rating": rating,
            "default_risk": round(max(0.02, (800 - score) / 1000), 3)}


def breach_history(vendor: str = "", **_) -> dict:
    breaches = {"slack": 0, "zoom": 1, "notion": 0, "figma": 0}
    n = breaches.get(vendor.lower(), random.choice([0, 0, 1]))
    return {"vendor": vendor, "known_breaches": n,
            "last_incident": "2020 (resolved)" if n else None,
            "risk_flag": n > 0}


_EMPLOYEES_CACHE: list[dict] | None = None


def _employees() -> list[dict]:
    global _EMPLOYEES_CACHE
    if _EMPLOYEES_CACHE is None:
        import json
        from pathlib import Path
        path = Path(__file__).parent.parent / "data" / "employees.json"
        _EMPLOYEES_CACHE = json.loads(path.read_text(encoding="utf-8"))
    return _EMPLOYEES_CACHE


def employee_directory(email: str = "", role: str = "", department: str = "",
                       authority: str = "", **_) -> dict:
    """Real lookup against data/employees.json."""
    emps = _employees()

    if authority:  # find who holds an authority (e.g. who can clear a CISO veto)
        holders = [e for e in emps if authority in e.get("authority", {}).get("can_clear_veto", [])]
        # prefer the dedicated role-holder (e.g. the CISO) over someone with blanket
        # super-authority (e.g. the CEO who can clear everything).
        role_map = {"CISO": "ciso", "CFO": "cfo", "DPO": "data protection"}
        key = role_map.get(authority, authority.lower())
        primary = [e for e in holders if key in e["role"].lower()]
        ordered = primary + [e for e in holders if e not in primary]
        return {"authority": authority,
                "holders": [{"name": e["name"], "email": e["email"], "role": e["role"]}
                            for e in ordered], "found": bool(ordered)}

    if email:
        rec = next((e for e in emps if e["email"].lower() == email.lower()), None)
        return {"found": bool(rec), **(rec or {"email": email})}

    if department:  # approvers in a department
        appr = [e for e in emps if e["department"] == department
                and e.get("authority", {}).get("approve_limit", 0) > 0]
        return {"department": department,
                "approvers": [{"name": e["name"], "role": e["role"],
                               "approve_limit": e["authority"]["approve_limit"]} for e in appr]}

    return {"count": len(emps),
            "roles": sorted({e["role"] for e in emps})}


def policy_store(rule_id: str = "", **_) -> dict:
    return {"rule_id": rule_id, "source": "data/policies.json",
            "note": "Hard rules are evaluated deterministically by the overseers."}


# ───────────────────────── ACTION tools (mock side effects) ─────────────────────────

def send_doc(document: str = "", person: str = "", to: str = "", **_) -> dict:
    who = person or to or "the new hire"
    return {"status": "sent", "ref_id": _ref("DOC"),
            "detail": f"Sent '{document or 'onboarding documents'}' to {who} for e-signature."}


def assign_training(person: str = "", trainings: list | None = None, **_) -> dict:
    trainings = trainings or []
    who = person or "the new hire"
    names = [t["training"] if isinstance(t, dict) else t for t in trainings]
    return {"status": "assigned", "ref_id": _ref("TRN"),
            "detail": f"Assigned {len(names)} mandatory training(s) to {who}: "
                      f"{', '.join(names) or 'standard set'}."}


def enroll_benefits(package: list | None = None, to: str = "", **_) -> dict:
    package = package or []
    if not package:
        return {"status": "skipped", "ref_id": _ref("BEN"), "detail": "No benefits package (ineligible)."}
    return {"status": "enrolled", "ref_id": _ref("BEN"), "detail": f"Enrolled {to} in {', '.join(package)}."}


def calendar(person: str = "", start_date: str = "", **_) -> dict:
    """Read the onboarding calendar template (day-1 / week-1 events)."""
    who = person or "the new hire"
    base = date.today()
    events = [
        ("Orientation & welcome", 0),
        ("IT setup session", 0),
        ("Manager 1:1", 1),
        ("Team introduction", 1),
        ("Security & compliance training", 2),
    ]
    return {"person": who,
            "schedule": [{"event": e, "date": (base + timedelta(days=d)).isoformat()}
                         for e, d in events]}


def schedule_event(title: str = "", person: str = "", day_offset: int = 0, **_) -> dict:
    when = (date.today() + timedelta(days=day_offset)).isoformat()
    who = person or "the new hire"
    return {"status": "scheduled", "ref_id": _ref("CAL"),
            "detail": f"Scheduled day-1/week-1 onboarding events for {who} starting {when}."}


def create_account(person: str = "", department: str = "", apps: list | None = None, **_) -> dict:
    apps = apps or app_catalog(department=department)["accounts"]
    who = person or "the new hire"
    return {"status": "created", "ref_id": _ref("ACC"),
            "accounts": apps,
            "detail": f"Created {len(apps)} accounts for {who}: {', '.join(apps[:4])}…"}


def order_equipment(person: str = "", seniority: str = "mid", remote: bool = False, **_) -> dict:
    pkg = equipment_catalog(seniority=seniority, remote=remote)
    who = person or "the new hire"
    return {"status": "ordered", "ref_id": _ref("EQ"),
            "detail": f"Ordered {pkg['equipment'][0]} for {who} — {pkg['delivery']}."}


def grant_access(person: str = "", access_scope: list | None = None, **_) -> dict:
    scope = access_scope or ["baseline"]
    who = person or "the new hire"
    return {"status": "granted", "ref_id": _ref("AX"),
            "detail": f"Granted {', '.join(scope)} access to {who}."}


def payroll_system(person: str = "", employment_type: str = "full_time",
                   action: str = "setup", amount: float = 0.0, **_) -> dict:
    who = person or "the new hire"
    kind = "contractor payment profile" if employment_type == "contractor" else "payroll & tax profile"
    return {"status": "done", "ref_id": _ref("PAY"),
            "detail": f"Set up {kind} for {who}" + (f" (${amount:,.0f})" if amount else "") + "."}


# ───────────────────────── registry ─────────────────────────

BUILTIN_TOOLS = {
    # data
    "doc_catalog": doc_catalog,
    "benefits_catalog": benefits_catalog,
    "app_catalog": app_catalog,
    "equipment_catalog": equipment_catalog,
    "access_matrix": access_matrix,
    "budget_store": budget_store,
    "vendor_directory": vendor_directory,
    "credit_check": credit_check,
    "breach_history": breach_history,
    "employee_directory": employee_directory,
    "policy_store": policy_store,
    "calendar": calendar,
    "training_catalog": training_catalog,
    # invoice / AP (Algeria)
    "invoice_validator": invoice_validator,
    "nif_check": nif_check,
    "three_way_match": three_way_match,
    "fraud_check": fraud_check,
    # action
    "send_doc": send_doc,
    "assign_training": assign_training,
    "record_payment": record_payment,
    "enroll_benefits": enroll_benefits,
    "schedule_event": schedule_event,
    "create_account": create_account,
    "order_equipment": order_equipment,
    "grant_access": grant_access,
    "payroll_system": payroll_system,
}
