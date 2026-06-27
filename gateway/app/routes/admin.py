"""Admin routes - live department stream, overview stats, sentinel insights,
and company_admin-only user management (Keycloak Admin API)."""

from __future__ import annotations

import re
import secrets
import string

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, field_validator

from app.core.auth import Principal, require_auth, require_role, ws_principal
from app.core.events import bus
from app.core import engine
from app.services import keycloak_admin as kc
import db as _db  # type: ignore

router = APIRouter(prefix="/api", tags=["admin"])

# ── role taxonomy: the ONLY realm roles a company_admin may assign ──
# Mirrors realm-orchestrai.json. Anything outside this set is rejected, so a
# caller cannot inject an unknown or escalated role.
ASSIGNABLE_ROLES = {
    "company_admin",
    "hr_admin",
    "it_admin",
    "finance_admin",
    "ciso",
    "cfo",
    "dpo",
    "requester",
}
# Department-scoped roles require (and imply) a department.
DEPT_FOR_ROLE = {"hr_admin": "hr", "it_admin": "it", "finance_admin": "finance"}
VALID_DEPARTMENTS = {"hr", "it", "finance"}


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_USERNAME_RE = re.compile(r"^[a-zA-Z0-9._-]{3,64}$")


class CreateUserBody(BaseModel):
    username: str
    email: str
    first_name: str
    last_name: str
    roles: list[str]
    department: str | None = None

    @field_validator("username")
    @classmethod
    def _username(cls, v: str) -> str:
        v = v.strip()
        if not _USERNAME_RE.match(v):
            raise ValueError("username must be 3-64 chars: letters, digits, . _ -")
        return v

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        v = v.strip().lower()
        if not _EMAIL_RE.match(v):
            raise ValueError("invalid email")
        return v

    @field_validator("first_name", "last_name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name fields are required")
        return v

    @field_validator("roles")
    @classmethod
    def _roles(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("at least one role is required")
        unknown = [r for r in v if r not in ASSIGNABLE_ROLES]
        if unknown:
            raise ValueError(f"unassignable role(s): {unknown}")
        return v


def _gen_temp_password(n: int = 16) -> str:
    """Cryptographically-random temporary password (user must reset on login)."""
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    return "".join(secrets.choice(alphabet) for _ in range(n))


@router.get("/admin/users")
async def admin_list_users(p: Principal = Depends(require_role("company_admin"))):
    """List realm users + their roles/department. company_admin only."""
    return await kc.list_users()


@router.post("/admin/users", status_code=201)
async def admin_create_user(
    body: CreateUserBody,
    p: Principal = Depends(require_role("company_admin")),
):
    """
    Create a Keycloak user, assign realm roles + department, and return a
    one-time temporary password the admin shares out-of-band. company_admin only.
    """
    # derive/validate department against the role taxonomy
    dept = body.department.strip().lower() if body.department else None
    required = next((DEPT_FOR_ROLE[r] for r in body.roles if r in DEPT_FOR_ROLE), None)
    if required:
        # a dept-scoped role implies its department; reject a conflicting one
        if dept and dept != required:
            raise HTTPException(
                400, f"department '{dept}' conflicts with role (expected '{required}')"
            )
        dept = required
    if dept and dept not in VALID_DEPARTMENTS:
        raise HTTPException(400, f"invalid department '{dept}'")

    if await kc.user_exists(body.username, body.email):
        raise HTTPException(409, "Username or email already exists")

    temp_password = _gen_temp_password()
    user_id = await kc.create_user(
        username=body.username,
        email=body.email,
        first_name=body.first_name,
        last_name=body.last_name,
        temp_password=temp_password,
        realm_roles=body.roles,
        department=dept,
    )
    # The temporary password is returned ONCE so the admin can hand it over.
    # The user is forced to change it on first login (UPDATE_PASSWORD action).
    return {
        "id": user_id,
        "username": body.username,
        "roles": body.roles,
        "department": dept,
        "temporary_password": temp_password,
    }


@router.websocket("/admin/stream")
async def admin_stream(ws: WebSocket):
    """Stream StepEvents filtered by the caller's role: dept admins get their dept,
    company_admin gets everything."""
    await ws.accept()
    p = await ws_principal(ws)
    scope = p.scope()                              # 'ALL' | dept | None
    channel = "dept:ALL" if scope in (None, "ALL") else f"dept:{scope}"
    try:
        async for ev in bus.subscribe(channel):
            await ws.send_json(ev)
    except WebSocketDisconnect:
        pass


@router.get("/sentinel")
async def sentinel(p: Principal = Depends(require_auth)):
    """Proactive Sentinel scan (cashflow, vendor concentration, split invoices, ...)."""
    return engine.sentinel_scan()


@router.get("/employees")
async def list_employees(p: Principal = Depends(require_auth)):
    """The company employee directory (seeded + onboarded). Department admins see
    only their department; company_admin sees all."""
    from sqlmodel import select
    scope = p.scope()
    with _db.get_session() as s:
        rows = s.exec(select(_db.EmployeeRow).order_by(_db.EmployeeRow.created_at)).all()
    if scope and scope != "ALL":
        rows = [r for r in rows if r.department == scope]
    return [{
        "employee_code": r.employee_code,
        "name": r.name,
        "email": r.email,
        "role": r.role,
        "department": r.department,
        "seniority": r.seniority,
        "employment_type": r.employment_type,
        "location": r.location,
        "national_id": r.national_id,
        "salary": r.salary,
        "contract_type": r.contract_type,
        "start_date": r.start_date,
        "status": r.status,
        "onboarded": r.source_run_id not in (None, "seed"),
        "source_run_id": r.source_run_id,
        "created_at": r.created_at.isoformat(),
    } for r in rows]


@router.get("/stats/overview")
async def overview(range: str = "7d", p: Principal = Depends(require_auth)):
    """KPIs + chart series, computed from persisted logs/runs."""
    from sqlmodel import select
    scope = p.scope()
    with _db.get_session() as s:
        runs = s.exec(select(_db.Run)).all()
        logs = s.exec(select(_db.AgentLogRow)).all()
        vetoes = s.exec(select(_db.VetoRow)).all()

    if scope and scope != "ALL":
        logs = [lg for lg in logs if lg.department == scope]

    by_status: dict[str, int] = {}
    for r in runs:
        by_status[r.status] = by_status.get(r.status, 0) + 1
    by_domain: dict[str, int] = {}
    for r in runs:
        d = (r.plan or {}).get("domain", "unknown")
        by_domain[d] = by_domain.get(d, 0) + 1
    by_rule: dict[str, int] = {}
    for v in vetoes:
        by_rule[v.rule_id] = by_rule.get(v.rule_id, 0) + 1

    return {
        "kpis": {
            "total_requests": len(runs),
            "frozen": by_status.get("frozen", 0),
            "approved": by_status.get("done", 0),
            "awaiting_human": by_status.get("awaiting_human", 0),
        },
        "by_status": by_status,
        "by_domain": by_domain,
        "blocks_by_rule": by_rule,
        "memory": engine.memory_stats(),
    }
