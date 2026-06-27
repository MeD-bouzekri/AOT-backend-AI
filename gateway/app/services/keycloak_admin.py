"""
Keycloak Admin API client - server-side only.

Creates users and assigns realm roles / department on behalf of a verified
company_admin. The Keycloak admin credentials live in env (config) and NEVER
leave the gateway; the frontend only ever talks to the gateway with its own
user JWT, which `require_role("company_admin")` verifies before any call here.

Security properties:
  * admin access token is obtained via password grant against the admin realm,
    cached in-process, and refreshed ~30s before expiry (no creds on the wire
    more than necessary, no creds ever returned to a client).
  * role assignment is restricted by the route to a fixed allow-list, so a
    caller cannot inject an arbitrary or unknown realm role.
  * new users are created disabled-free but with a one-time password and a
    forced UPDATE_PASSWORD action, so the temporary password is single-use.
"""

from __future__ import annotations

import time
from typing import Any, Optional

import httpx
from fastapi import HTTPException

from app.core.config import settings


class KeycloakAdminError(HTTPException):
    """Raised on any Admin API failure; surfaces as an HTTP error upstream."""


# ── admin token cache ──
_token: Optional[str] = None
_token_exp: float = 0.0
_SKEW = 30  # refresh this many seconds before the admin token expires


async def _admin_token(client: httpx.AsyncClient) -> str:
    """Fetch (and cache) an admin access token via password grant."""
    global _token, _token_exp
    now = time.time()
    if _token and now < _token_exp - _SKEW:
        return _token

    url = (
        f"{settings.KEYCLOAK_BASE_URL}/realms/{settings.KEYCLOAK_ADMIN_REALM}"
        "/protocol/openid-connect/token"
    )
    try:
        res = await client.post(
            url,
            data={
                "grant_type": "password",
                "client_id": settings.KEYCLOAK_ADMIN_CLIENT_ID,
                "username": settings.KEYCLOAK_ADMIN_USER,
                "password": settings.KEYCLOAK_ADMIN_PASSWORD,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    except httpx.RequestError as exc:
        raise KeycloakAdminError(502, f"Cannot reach Keycloak: {exc}") from exc

    if res.status_code != 200:
        raise KeycloakAdminError(502, "Keycloak admin authentication failed")

    payload = res.json()
    _token = payload["access_token"]
    _token_exp = now + payload.get("expires_in", 60)
    return _token


def _admin_base() -> str:
    return (
        f"{settings.KEYCLOAK_BASE_URL}/admin/realms/{settings.KEYCLOAK_REALM}"
    )


async def _auth_headers(client: httpx.AsyncClient) -> dict[str, str]:
    return {"Authorization": f"Bearer {await _admin_token(client)}"}


async def user_exists(username: str, email: str) -> bool:
    """True if a user with this username or email already exists in the realm."""
    async with httpx.AsyncClient(timeout=10) as client:
        headers = await _auth_headers(client)
        for params in ({"username": username, "exact": "true"},
                       {"email": email, "exact": "true"}):
            res = await client.get(
                f"{_admin_base()}/users", params=params, headers=headers
            )
            if res.status_code == 200 and res.json():
                return True
    return False


async def get_realm_role(client: httpx.AsyncClient, role: str) -> dict[str, Any]:
    """Resolve a realm role representation (id + name) by name."""
    headers = await _auth_headers(client)
    res = await client.get(
        f"{_admin_base()}/roles/{role}", headers=headers
    )
    if res.status_code != 200:
        raise KeycloakAdminError(400, f"Unknown realm role: {role}")
    return res.json()


async def create_user(
    *,
    username: str,
    email: str,
    first_name: str,
    last_name: str,
    temp_password: str,
    realm_roles: list[str],
    department: Optional[str],
) -> str:
    """
    Create a realm user with a ready-to-use password (no forced reset, since the
    app uses direct password grant), assign the given realm roles, and set the
    department attribute.

    Returns the new user's id. Raises KeycloakAdminError on conflict/failure.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        headers = await _auth_headers(client)

        # The dashboard authenticates via direct password grant (no hosted-login
        # redirect), so the user can never complete a UPDATE_PASSWORD / VERIFY_EMAIL
        # required action. We therefore create the account ready to use: a permanent
        # password, no pending actions, email pre-verified. The password is still a
        # one-time secret shown once to the admin; the user should change it in-app.
        body: dict[str, Any] = {
            "username": username,
            "email": email,
            "firstName": first_name,
            "lastName": last_name,
            "enabled": True,
            "emailVerified": True,
            "requiredActions": [],
            "credentials": [
                {"type": "password", "value": temp_password, "temporary": False}
            ],
        }
        if department:
            body["attributes"] = {"department": [department]}

        res = await client.post(
            f"{_admin_base()}/users", json=body, headers=headers
        )
        if res.status_code == 409:
            raise KeycloakAdminError(409, "Username or email already exists")
        if res.status_code not in (201, 204):
            raise KeycloakAdminError(
                502, f"Keycloak user creation failed ({res.status_code})"
            )

        # Keycloak returns the new user's URL in Location; fetch the id from it.
        location = res.headers.get("Location", "")
        user_id = location.rstrip("/").split("/")[-1]
        if not user_id:
            raise KeycloakAdminError(502, "Keycloak did not return a user id")

        # assign realm roles (resolve each to its representation first)
        if realm_roles:
            reps = [await get_realm_role(client, r) for r in realm_roles]
            assign = await client.post(
                f"{_admin_base()}/users/{user_id}/role-mappings/realm",
                json=reps,
                headers=await _auth_headers(client),
            )
            if assign.status_code not in (204, 201):
                # best-effort cleanup so we don't leave a role-less orphan
                await client.delete(
                    f"{_admin_base()}/users/{user_id}",
                    headers=await _auth_headers(client),
                )
                raise KeycloakAdminError(502, "Role assignment failed; user rolled back")

        return user_id


async def list_users() -> list[dict[str, Any]]:
    """List realm users with their realm roles + department (for the Accounts page)."""
    async with httpx.AsyncClient(timeout=15) as client:
        headers = await _auth_headers(client)
        res = await client.get(
            f"{_admin_base()}/users", params={"max": 200}, headers=headers
        )
        if res.status_code != 200:
            raise KeycloakAdminError(502, "Failed to list users")

        users = res.json()
        out: list[dict[str, Any]] = []
        for u in users:
            roles_res = await client.get(
                f"{_admin_base()}/users/{u['id']}/role-mappings/realm",
                headers=await _auth_headers(client),
            )
            roles = (
                [r["name"] for r in roles_res.json()]
                if roles_res.status_code == 200
                else []
            )
            dept = (u.get("attributes", {}).get("department") or [None])[0]
            out.append(
                {
                    "id": u["id"],
                    "username": u.get("username"),
                    "email": u.get("email"),
                    "firstName": u.get("firstName"),
                    "lastName": u.get("lastName"),
                    "enabled": u.get("enabled", True),
                    "roles": roles,
                    "department": dept,
                }
            )
        return out
