"""
sentinel.py - Proactive Sentinel.

Unlike the per-request overseers, the Sentinel scans ACROSS recent activity to surface risks
before they escalate. It answers "what should a human worry about right now?" for the
Algeria finance pains: cashflow exposure, vendor concentration, split-invoice clusters,
duplicate suppliers, and approval bottlenecks.

It runs:
    - on demand (dashboard "insights" panel, or GET /api/sentinel),
    - or on a schedule (cron) to push proactive alerts.

Reads: data/recent_invoices.json (AP activity) + memory (recent run outcomes) +
       data/algeria-finance.json (the proactive thresholds the company set).
"""

from __future__ import annotations

import json
from pathlib import Path
from collections import defaultdict

import memory

_DATA = Path(__file__).parent / "data"


def _load(name: str, default):
    p = _DATA / name
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return default
    return default


def scan() -> dict:
    """Return a list of proactive alerts + summary metrics for the dashboard."""
    invoices = _load("recent_invoices.json", [])
    rules = _load("algeria-finance.json", {})
    proactive = rules.get("proactive_alerts", {})
    fraud_rules = rules.get("fraud_and_anomaly_rules", {})

    alerts: list[dict] = []

    # ── cashflow exposure: sum of pending invoice value ──────────────
    total_pending = sum(float(i.get("amount_ttc", 0) or 0) for i in invoices)
    cashflow_threshold = proactive.get("cashflow_impact_threshold", 5_000_000)
    if total_pending > cashflow_threshold:
        alerts.append({
            "severity": "high", "type": "cashflow",
            "message": f"Pending AP exposure is {total_pending:,.0f} DZD, above the "
                       f"{cashflow_threshold:,.0f} DZD cashflow alert threshold.",
            "recommendation": "Review payment scheduling to protect cash position.",
        })

    # ── vendor concentration ─────────────────────────────────────────
    by_vendor_amt: dict[str, float] = defaultdict(float)
    by_vendor_cnt: dict[str, int] = defaultdict(int)
    for i in invoices:
        v = i.get("supplier", "?")
        by_vendor_amt[v] += float(i.get("amount_ttc", 0) or 0)
        by_vendor_cnt[v] += 1
    if total_pending > 0:
        for v, amt in by_vendor_amt.items():
            share = amt / total_pending
            if share > 0.30:
                alerts.append({
                    "severity": "medium", "type": "vendor_concentration",
                    "message": f"{v} accounts for {share*100:.0f}% of pending AP "
                               f"({amt:,.0f} DZD).",
                    "recommendation": "Diversify suppliers or review the relationship.",
                })

    # ── split-invoice clusters (same vendor, many invoices) ──────────
    split_threshold = fraud_rules.get("same_vendor_multiple_invoices_monthly_threshold", 3)
    for v, cnt in by_vendor_cnt.items():
        if cnt >= split_threshold:
            amts = [i.get("amount_ttc", 0) for i in invoices if i.get("supplier") == v]
            alerts.append({
                "severity": "high", "type": "split_invoices",
                "message": f"{v} submitted {cnt} invoices recently "
                           f"({', '.join(f'{a:,.0f}' for a in amts)} DZD) - possible split "
                           f"to stay under approval thresholds.",
                "recommendation": "Consolidate and route to compliance review.",
            })

    # ── duplicate supplier NIF with different names (shell risk) ─────
    nif_names: dict[str, set] = defaultdict(set)
    for i in invoices:
        if i.get("supplier_nif"):
            nif_names[i["supplier_nif"]].add(i.get("supplier", "?"))
    for nif, names in nif_names.items():
        if len(names) > 1:
            alerts.append({
                "severity": "high", "type": "nif_mismatch",
                "message": f"NIF {nif} appears under multiple supplier names: "
                           f"{', '.join(names)}.",
                "recommendation": "Investigate possible shell/duplicate vendor.",
            })

    # ── frozen-run cluster (governance kept blocking similar things) ──
    mem = memory.stats()
    frozen = mem.get("by_outcome", {}).get("frozen", 0)
    if frozen >= 3:
        alerts.append({
            "severity": "medium", "type": "governance_pattern",
            "message": f"{frozen} workflows were frozen recently - a recurring policy gap "
                       f"may need attention.",
            "recommendation": "Review the most-triggered rules with the policy owners.",
        })

    return {
        "alerts": alerts,
        "metrics": {
            "pending_invoices": len(invoices),
            "pending_value_dzd": total_pending,
            "distinct_vendors": len(by_vendor_amt),
            "memory_total": mem.get("total", 0),
            "frozen_count": frozen,
        },
        "status": "risk" if any(a["severity"] == "high" for a in alerts)
                  else "watch" if alerts else "clear",
    }


if __name__ == "__main__":
    import sys
    result = scan()
    print(f"SENTINEL STATUS: {result['status'].upper()}")
    print(f"Metrics: {json.dumps(result['metrics'])}")
    print(f"\n{len(result['alerts'])} proactive alert(s):")
    for a in result["alerts"]:
        print(f"  [{a['severity'].upper()}] {a['type']}: {a['message']}")
        print(f"      -> {a['recommendation']}")
