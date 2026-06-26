"""
context_loader.py — assemble the CompanyContext the Planner reads.

For the demo, the company's setup lives in JSON fixtures (data/). In production this comes
from the DB tables filled by the setup wizard. Either way the Planner only ever sees a
CompanyContext, so swapping the source later changes nothing downstream.
"""

from __future__ import annotations

import json
from pathlib import Path

from schemas import (
    CompanyContext, CompanyProfile, DepartmentSpec, AgentSpec, RequestType,
    GovThresholds, HardRule,
)

DATA = Path(__file__).parent / "data"


def _load(name: str) -> dict | list:
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def load_company_context() -> CompanyContext:
    """Build the CompanyContext from the seeded fixtures."""
    policies = _load("policies.json")
    depts = _load("departments.json")

    return CompanyContext(
        company=CompanyProfile(**policies["company"]),
        departments=[DepartmentSpec(**d) for d in depts["departments"]],
        capabilities=[AgentSpec(**c) for c in depts["capabilities"]],
        request_types=[RequestType(**r) for r in depts["request_types"]],
        thresholds=GovThresholds(**policies["thresholds"]),
        hard_rules=[HardRule(**h) for h in policies["hard_rules"]],
        authorities=policies.get("authorities", {}),
        sla_defaults=policies.get("sla_defaults", {}),
    )


def load_employees() -> list[dict]:
    return _load("employees.json")  # type: ignore[return-value]
