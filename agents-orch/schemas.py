"""
schemas.py — the typed contracts for the whole system.

Every object that crosses an agent, the gateway, or the frontend boundary is defined here.
Agents never pass raw dicts; everything is a Pydantic model. This gives us: validated LLM
output, a free frontend contract, and a free audit log.

Grouped:
    1. Requests        — what a user submits (HireRecord / PurchaseRequest)
    2. Company context — what the Planner reads to orchestrate for a company
    3. Planning        — the Planner's output (DispatchPlan)
    4. Execution       — per-worker / per-department results
    5. Governance      — Veto (block/halt) + Approval
    6. Observability   — StepEvent (live) + AgentLog (persisted)
    7. LLM config      — per-agent provider/model
"""

from __future__ import annotations

from datetime import datetime, date
from typing import Literal, Optional, Any

from pydantic import BaseModel, Field


# ───────────────────────── 1. Requests ─────────────────────────

Domain = Literal["hr_onboarding", "procurement", "invoice_ap"]
Seniority = Literal["intern", "junior", "mid", "senior", "staff", "exec"]
EmploymentType = Literal["full_time", "contractor", "intern"]


class HireRecord(BaseModel):
    """Parsed onboarding request. The Planner's `extracted` for the HR domain."""
    name: str
    role: str
    department: str
    seniority: Seniority
    location: str
    employment_type: EmploymentType
    remote: bool = False
    handles_sensitive_data: bool = False        # PII / financial → Compliance + Security
    access_scope: list[str] = Field(default_factory=list)  # e.g. ["email", "production"]
    start_date: Optional[date] = None
    manager_email: Optional[str] = None


class InvoiceRequest(BaseModel):
    """Parsed supplier invoice (Algeria AP domain). The Planner's `extracted`.

    Mandatory legal identifiers per Décret exécutif 05-468: NIF + NIS + RC + AI.
    """
    supplier: str
    supplier_nif: Optional[str] = None          # Numéro d'Identification Fiscale (15 digits)
    supplier_nis: Optional[str] = None          # Numéro d'Identification Statistique (ONS)
    supplier_rc: Optional[str] = None           # Registre de Commerce
    supplier_ai: Optional[str] = None           # Article d'Imposition
    customer_nif: Optional[str] = None
    invoice_number: Optional[str] = None
    invoice_date: Optional[str] = None
    description: str = ""
    amount_ht: float = 0.0                       # total before VAT (Hors Taxe)
    vat_rate: int = 19                           # 19 standard, 9 reduced
    vat_amount: float = 0.0
    amount_ttc: float = 0.0                      # total with VAT (Toutes Taxes Comprises)
    currency: str = "DZD"
    payment_method: str = "bank_transfer"        # bank_transfer | cheque | cash
    has_fiscal_stamp: bool = False               # only relevant when payment_method == cash
    has_purchase_order: bool = False
    has_goods_receipt: bool = False
    is_foreign: bool = False                     # foreign-currency / import
    po_number: Optional[str] = None
    # convenience: the amount the thresholds key off (TTC)
    @property
    def amount(self) -> float:
        return self.amount_ttc or self.amount_ht


class PurchaseRequest(BaseModel):
    """Parsed procurement request. The Planner's `extracted` for the procurement domain."""
    vendor: str
    item: str                                   # "Slack + Zoom annual subscription"
    amount: float
    currency: str = "USD"
    recurring: bool = False
    is_data_processor: bool = False             # vendor processes personal data
    has_dpa: bool = False                       # Data Processing Agreement on file
    contract_attached: bool = False
    requested_by_email: Optional[str] = None


# ───────────────────────── 2. Company context ─────────────────────────
# What the generic Planner reads to become company-specific (see ONBOARDING_SETUP.md).

class CompanyProfile(BaseModel):
    name: str
    industry: Optional[str] = None
    size: Optional[str] = None
    locations: list[str] = Field(default_factory=list)
    timezone: str = "UTC"


class DepartmentSpec(BaseModel):
    key: str                                    # "hr" | "it" | "finance"
    name: str
    responsibility: str
    enabled: bool = True


class ToolSpec(BaseModel):
    name: str
    description: str
    inputs: list[str] = Field(default_factory=list)
    builtin: bool = True
    mock_response: Optional[dict] = None        # for user-built tools (simulated)


class AgentSpec(BaseModel):
    """A registry agent. Core or user-built; run by the generic ConfigurableAgent node."""
    name: str
    department: str                             # hr|it|finance|governance|shared
    level: Literal["planner", "manager", "worker", "governance"]
    role: str
    system_prompt: str = ""
    tools: list[str] = Field(default_factory=list)
    input_fields: list[str] = Field(default_factory=list)
    output_schema: dict = Field(default_factory=dict)
    conditional_on: Optional[str] = None        # "request.amount > 50000"
    mode: Literal["react", "single", "rule"] = "single"
    llm: Optional["LLMConfig"] = None           # None → system default
    builtin: bool = True
    version: int = 1
    enabled: bool = True


class RequestType(BaseModel):
    domain: Domain
    label: str
    required_fields: list[str] = Field(default_factory=list)
    trigger_hint: str = ""                      # phrases that indicate this domain


class GovThresholds(BaseModel):
    """Governance numbers the overseers + Approver evaluate against (AEGIS-style data)."""
    auto_approve_spend_limit: float = 10_000
    manager_spend_limit: float = 50_000
    director_spend_limit: float = 100_000
    hard_spend_ceiling: float = 1_000_000
    max_vendor_risk_score: float = 0.70         # above → escalate


class HardRule(BaseModel):
    """A deterministic block/halt rule (see GOVERNANCE.md)."""
    id: str                                     # "SEC-04"
    domain: str                                 # "shared" | "procurement" | "hr_onboarding"
    description: str
    condition: str                              # human/eval-able condition string
    action: Literal["block", "halt"]
    message: str
    required_authority: str                     # "CISO" | "CFO" | "DPO"
    enabled: bool = True


class CompanyContext(BaseModel):
    """The single object that makes the generic Planner company-specific."""
    company: CompanyProfile
    departments: list[DepartmentSpec]
    capabilities: list[AgentSpec]               # the worker/tool menu, per dept
    request_types: list[RequestType]
    thresholds: GovThresholds
    hard_rules: list[HardRule]
    authorities: dict[str, list[str]] = Field(default_factory=dict)  # "CISO" -> [emails]
    sla_defaults: dict[str, int] = Field(default_factory=dict)       # domain -> days


# ───────────────────────── 3. Planning ─────────────────────────

class CapabilityGap(BaseModel):
    """A missing agent/tool the Planner needs (self-extension)."""
    kind: Literal["agent", "tool"]
    name: str
    suggested_role: str
    suggested_prompt: Optional[str] = None
    suggested_tools: list[str] = Field(default_factory=list)
    reason: str


class DeptMandate(BaseModel):
    department: str                             # "hr" | "it" | "finance"
    mandate: str                                # what this dept must accomplish
    depends_on: list[str] = Field(default_factory=list)  # other dept keys


class DispatchPlan(BaseModel):
    """The Planner's decision — the dynamic team for this request."""
    domain: Domain
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str
    extracted: dict = Field(default_factory=dict)   # the structured fields the Planner parsed
                                                    # (HireRecord OR PurchaseRequest shape)
    departments: list[DeptMandate]              # which depts + mandates + dependencies
    required_workers: list[str] = Field(default_factory=list)   # specific workers to run
    hitl_points: list[str] = Field(default_factory=list)        # where a human is required
    deadline_days: int = 7
    reasoning: str = ""                         # shown to jury — proves it DECIDED
    status: Literal["ready", "needs_capability"] = "ready"
    gaps: list[CapabilityGap] = Field(default_factory=list)


# ───────────────────────── 4. Execution ─────────────────────────

TaskStatus = Literal["queued", "running", "done", "blocked", "awaiting_human", "error"]


class Evidence(BaseModel):
    """A grounded fact an agent relied on (tool result or search hit)."""
    source: str                                 # tool name or URL
    detail: str


class Verdict(BaseModel):
    """The structured output of a 'perfect' worker agent — a real specialist verdict."""
    findings: str = ""                          # what the agent discovered
    analysis: str = ""                          # the agent's reasoning over the findings
    recommendation: str = ""                    # the concrete recommendation / action taken
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    evidence: list[Evidence] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)   # e.g. ["sensitive", "high_risk"]


class WorkerResult(BaseModel):
    worker: str
    department: str
    tool_used: Optional[str] = None
    output: str = ""                            # human-readable summary (recommendation)
    reasoning: str = ""
    status: TaskStatus = "done"
    flags: dict[str, Any] = Field(default_factory=dict)   # e.g. {"sensitive": True}
    verdict: Optional[Verdict] = None           # the full structured specialist verdict
    iterations: int = 1                         # ReAct steps taken
    critiqued: bool = False                     # whether a critic verified this


class DepartmentResult(BaseModel):
    department: str
    manager_reasoning: str = ""
    workers: list[WorkerResult] = Field(default_factory=list)
    status: TaskStatus = "done"


# ───────────────────────── 5. Governance ─────────────────────────

class Veto(BaseModel):
    """A block/halt raised by an overseer. Persistent until a named authority clears it."""
    raised_by: str                              # "Compliance" | "Risk"
    rule_id: str                                # "SEC-04"
    scope: Literal["block", "halt"]
    message: str
    explanation: str = ""                       # LLM-written (explain only, never decide)
    owning_department: str                      # the dept whose work is frozen → alert its admin
    blocked_worker: Optional[str] = None        # the specific step that triggered it
    required_authority: str                     # "CISO" | "CFO" | "DPO"  → who can clear
    cleared_by: Optional[str] = None
    decision: Optional[Literal["release", "release_with_conditions", "deny"]] = None
    conditions: Optional[str] = None


class Approval(BaseModel):
    id: str
    approver_role: str                          # "Hiring Manager" | "CFO" | "CISO"
    item: str
    risk: Literal["low", "medium", "high"] = "low"
    status: Literal["pending", "approved", "rejected"] = "pending"
    note: Optional[str] = None


# ───────────────────────── 6. Observability ─────────────────────────

class StepEvent(BaseModel):
    """Emitted live over WebSocket AND persisted as an AgentLog. One object, both surfaces."""
    run_id: str
    department: str                             # hr|it|finance|governance  (filter/ACL key)
    level: Literal["planner", "manager", "worker", "governance"]
    agent: str
    assigned_by: Optional[str] = None           # who gave the task
    phase: str = ""                             # live label "Checking access scope…"
    status: TaskStatus = "running"
    tools_used: list[str] = Field(default_factory=list)
    output: Optional[str] = None
    reasoning: Optional[str] = None
    policy_citation: Optional[str] = None
    ts: datetime = Field(default_factory=datetime.utcnow)


class AgentLog(BaseModel):
    """Persisted audit row (mirror of StepEvent + a couple of fields)."""
    run_id: str
    ts: datetime
    department: str
    level: str
    agent: str
    assigned_by: Optional[str] = None
    action: str                                 # assigned|called_tool|produced_result|BLOCK|HALT|cleared
    phase: str = ""
    status: str = ""
    tools_used: list[str] = Field(default_factory=list)
    output: Optional[str] = None
    reasoning: Optional[str] = None
    policy_citation: Optional[str] = None
    metadata: dict = Field(default_factory=dict)


# ───────────────────────── 7. LLM config ─────────────────────────

class LLMConfig(BaseModel):
    """Per-agent LLM selection (see PLATFORM_FEATURES.md)."""
    provider: Literal["claude", "gemini", "ollama"] = "claude"
    model: str = "claude-opus-4-8"
    base_url: Optional[str] = None              # required for ollama
    api_key_ref: Optional[str] = None           # secret NAME, never the raw key
    temperature: float = 0.2
    max_tokens: int = 1024


class OllamaValidation(BaseModel):
    ok: bool
    available: list[str] = Field(default_factory=list)
    error: Optional[str] = None


# resolve forward refs (AgentSpec references LLMConfig defined later)
AgentSpec.model_rebuild()
