"""
db.py - the MUST tables (SQLModel; SQLite for the hackathon, Postgres-ready).

The single most important table is `agent_logs` - it powers the logs page, the workflow
graph, and the stats overview from one write path. The same object emitted as a live
StepEvent is persisted here.

Tables: requests, runs, agent_logs, vetoes, approvals.
(employees / agents / policies are loaded from JSON fixtures for the demo; promote to tables
later if needed.)
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlmodel import SQLModel, Field, create_engine, Session, Column, JSON, select

DATABASE_URL = "sqlite:///orchestrai.db"
engine = create_engine(DATABASE_URL, echo=False)


def _uid() -> str:
    return str(uuid.uuid4())


class Request(SQLModel, table=True):
    """What a normal user submits on the request page."""
    __tablename__ = "requests"
    id: str = Field(default_factory=_uid, primary_key=True)
    submitted_by: Optional[str] = Field(default=None, index=True)
    raw_text: str
    domain: Optional[str] = Field(default=None, index=True)   # detected by Planner
    extracted: dict = Field(default_factory=dict, sa_column=Column(JSON))
    status: str = Field(default="received", index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Run(SQLModel, table=True):
    """One execution of a request through the graph."""
    __tablename__ = "runs"
    id: str = Field(default_factory=_uid, primary_key=True)
    request_id: str = Field(index=True)
    plan: dict = Field(default_factory=dict, sa_column=Column(JSON))   # DispatchPlan
    status: str = Field(default="running", index=True)
    readiness: Optional[int] = None
    started_at: datetime = Field(default_factory=datetime.utcnow)
    ended_at: Optional[datetime] = None


class AgentLogRow(SQLModel, table=True):
    """One row per agent step - drives logs page, graph, and stats."""
    __tablename__ = "agent_logs"
    id: str = Field(default_factory=_uid, primary_key=True)
    run_id: str = Field(index=True)
    ts: datetime = Field(default_factory=datetime.utcnow, index=True)
    department: str = Field(index=True)
    level: str
    agent: str = Field(index=True)
    assigned_by: Optional[str] = None
    action: str                                # assigned|called_tool|produced_result|BLOCK|HALT|cleared
    phase: str = ""
    status: str = Field(default="", index=True)
    tools_used: list = Field(default_factory=list, sa_column=Column(JSON))
    output: Optional[str] = None
    reasoning: Optional[str] = None
    policy_citation: Optional[str] = None
    metadata_json: dict = Field(default_factory=dict, sa_column=Column(JSON))


class VetoRow(SQLModel, table=True):
    """A block/halt raised by an overseer; cleared by a named authority."""
    __tablename__ = "vetoes"
    id: str = Field(default_factory=_uid, primary_key=True)
    run_id: str = Field(index=True)
    raised_by: str
    rule_id: str
    scope: str                                 # block | halt
    message: str
    explanation: str = ""
    required_authority: str
    status: str = Field(default="active", index=True)   # active | cleared | denied
    cleared_by: Optional[str] = None
    decision: Optional[str] = None
    conditions: Optional[str] = None
    raised_at: datetime = Field(default_factory=datetime.utcnow)
    resolved_at: Optional[datetime] = None


class ApprovalRow(SQLModel, table=True):
    """HITL approval, threshold-based."""
    __tablename__ = "approvals"
    id: str = Field(default_factory=_uid, primary_key=True)
    run_id: str = Field(index=True)
    approver_role: str
    item: str
    risk: str = "low"
    status: str = Field(default="pending", index=True)
    decided_by: Optional[str] = None
    note: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    decided_at: Optional[datetime] = None


class EmployeeRow(SQLModel, table=True):
    """An employee in the company directory. Seeded from data/employees.json and
    appended to whenever an HR onboarding run completes cleanly."""
    __tablename__ = "employees"
    id: str = Field(default_factory=_uid, primary_key=True)
    employee_code: str = Field(index=True)
    name: str
    email: Optional[str] = Field(default=None, index=True)
    role: Optional[str] = None
    department: Optional[str] = None
    seniority: Optional[str] = None
    employment_type: Optional[str] = None
    location: Optional[str] = None
    national_id: Optional[str] = Field(default=None, index=True)  # NIN / CNI - dedup key
    tax_id_nif: Optional[str] = None
    salary: Optional[str] = None
    contract_type: Optional[str] = None
    start_date: Optional[str] = None
    status: str = Field(default="active", index=True)
    source_run_id: Optional[str] = None  # which onboarding run created this row
    created_at: datetime = Field(default_factory=datetime.utcnow)


def init_db() -> None:
    """Create all tables. Call once at startup, then seed the directory."""
    SQLModel.metadata.create_all(engine)
    seed_employees()


def seed_employees() -> None:
    """Load data/employees.json into the employees table once (idempotent)."""
    import json
    from pathlib import Path
    try:
        with Session(engine) as s:
            existing = s.exec(select(EmployeeRow)).first()
            if existing:
                return  # already seeded
            path = Path(__file__).parent / "data" / "employees.json"
            rows = json.loads(path.read_text(encoding="utf-8"))
            for r in rows:
                s.add(EmployeeRow(
                    employee_code=r.get("employee_code", _uid()[:6]),
                    name=r.get("name", "Unknown"),
                    email=r.get("email"),
                    role=r.get("role"),
                    department=r.get("department"),
                    seniority=r.get("seniority"),
                    employment_type=r.get("employment_type"),
                    location=r.get("location"),
                    national_id=r.get("national_id"),
                    status="active", source_run_id="seed",
                ))
            s.commit()
    except Exception:  # noqa: BLE001 - seeding must never break startup
        pass


def get_session() -> Session:
    return Session(engine)


if __name__ == "__main__":
    init_db()
    print("Database initialized at", DATABASE_URL)
