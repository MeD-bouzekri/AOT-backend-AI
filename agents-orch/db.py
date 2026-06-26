"""
db.py — the MUST tables (SQLModel; SQLite for the hackathon, Postgres-ready).

The single most important table is `agent_logs` — it powers the logs page, the workflow
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

from sqlmodel import SQLModel, Field, create_engine, Session, Column, JSON

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
    """One row per agent step — drives logs page, graph, and stats."""
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


def init_db() -> None:
    """Create all tables. Call once at startup."""
    SQLModel.metadata.create_all(engine)


def get_session() -> Session:
    return Session(engine)


if __name__ == "__main__":
    init_db()
    print("Database initialized at", DATABASE_URL)
