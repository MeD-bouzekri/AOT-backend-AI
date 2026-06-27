"""
Authentication + role/department scoping.

Two modes (same Principal out, so switching is contained):
  DEV_AUTH=1  -> role/department from headers X-Role / X-Department (dev the dashboard early)
  Keycloak    -> verify JWT vs realm JWKS, extract realm roles + department claim
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from fastapi import Depends, Header, HTTPException, WebSocket

from app.core.config import settings

DEPT_ROLES = {"hr_admin": "hr", "it_admin": "it", "finance_admin": "finance"}
AUTHORITY_ROLES = {"ciso": "CISO", "cfo": "CFO", "dpo": "DPO"}


@dataclass
class Principal:
    sub: str = "dev-user"
    email: str = "dev@numidia.ia"
    roles: list[str] = field(default_factory=list)
    department: Optional[str] = None

    @property
    def is_company_admin(self) -> bool:
        return "company_admin" in self.roles

    @property
    def authorities(self) -> list[str]:
        """Veto-clearing authorities held (CISO/CFO/DPO)."""
        a = {AUTHORITY_ROLES[r] for r in self.roles if r in AUTHORITY_ROLES}
        if self.is_company_admin:
            a |= {"CISO", "CFO", "DPO"}
        return sorted(a)

    def scope(self) -> Optional[str]:
        """Data scope: 'ALL' for company_admin, else the department, else None."""
        if self.is_company_admin:
            return "ALL"
        if self.department:
            return self.department
        return next((DEPT_ROLES[r] for r in self.roles if r in DEPT_ROLES), None)


# ───────────────────────── builders ─────────────────────────

def _dev_principal(role: str | None, dept: str | None) -> Principal:
    roles = [r.strip() for r in (role or "company_admin").split(",") if r.strip()]
    department = dept or next((DEPT_ROLES[r] for r in roles if r in DEPT_ROLES), None)
    return Principal(roles=roles, department=department)


_jwks_client = None


def _get_jwks_client():
    """Cache the JWKS client (it caches signing keys internally too)."""
    global _jwks_client
    if _jwks_client is None:
        from jwt import PyJWKClient
        if not settings.KEYCLOAK_JWKS_URL:
            raise HTTPException(500, "KEYCLOAK_JWKS_URL not configured")
        _jwks_client = PyJWKClient(settings.KEYCLOAK_JWKS_URL)
    return _jwks_client


def _keycloak_principal(authorization: str | None) -> Principal:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing bearer token")
    token = authorization.split(" ", 1)[1]
    try:
        import jwt
        key = _get_jwks_client().get_signing_key_from_jwt(token).key
        # Keycloak public-client access tokens carry aud="account"; we verify signature,
        # expiry and issuer, and accept the realm audience. Roles come from realm_access.
        claims = jwt.decode(
            token, key, algorithms=["RS256"],
            issuer=settings.KEYCLOAK_ISSUER or None,
            options={"verify_aud": False, "require": ["exp", "iat"]},
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(401, f"Invalid token: {exc}")
    roles = claims.get("realm_access", {}).get("roles", [])
    return Principal(sub=claims.get("sub", ""),
                     email=claims.get("email", claims.get("preferred_username", "")),
                     roles=roles, department=claims.get("department"))


# ───────────────────────── FastAPI deps ─────────────────────────

def require_auth(authorization: str | None = Header(default=None),
                 x_role: str | None = Header(default=None),
                 x_department: str | None = Header(default=None)) -> Principal:
    if settings.DEV_AUTH:
        return _dev_principal(x_role, x_department)
    return _keycloak_principal(authorization)


def require_role(*allowed: str):
    def dep(p: Principal = Depends(require_auth)) -> Principal:
        if p.is_company_admin or any(r in p.roles for r in allowed):
            return p
        raise HTTPException(403, f"Requires one of: {allowed}")
    return dep


async def ws_principal(ws: WebSocket) -> Principal:
    if settings.DEV_AUTH:
        return _dev_principal(ws.query_params.get("role"),
                              ws.query_params.get("department"))
    return _keycloak_principal(f"Bearer {ws.query_params.get('token', '')}")
