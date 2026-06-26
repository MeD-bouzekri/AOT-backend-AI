# Keycloak — authentication & RBAC for OrchestrAI

Real OIDC auth with role-based access control. The gateway verifies Keycloak-issued JWTs
(RS256, against the realm JWKS), extracts realm roles + a `department` claim, and enforces
access on every REST + WebSocket route.

Two modes (one switch, identical `Principal` downstream):
- **DEV_AUTH=1** (default) — role/department via `X-Role` / `X-Department` headers. Lets the
  frontend be built before Keycloak is wired. NOT for production.
- **DEV_AUTH=0** — full Keycloak JWT verification.

---

## 1. Start Keycloak (auto-imports the realm)

```bash
docker compose -f gateway/keycloak/docker-compose.yml up
```

- Admin console: http://localhost:8080  (admin / admin)
- Realm `orchestrai` is imported on first start from `realm-orchestrai.json`:
  roles, the `orchestrai-dashboard` OIDC client, a `department` token-claim mapper, and the
  demo users below.

## 2. Run the gateway against Keycloak

```bash
cd gateway
export DEV_AUTH=0
export KEYCLOAK_JWKS_URL=http://localhost:8080/realms/orchestrai/protocol/openid-connect/certs
export KEYCLOAK_ISSUER=http://localhost:8080/realms/orchestrai
python run.py
```

The gateway now rejects any request without a valid token (401) and enforces roles (403).

---

## 3. Roles (realm roles)

| Role | Access |
|---|---|
| `company_admin` | everything — all departments, config, registry, settings |
| `hr_admin` / `it_admin` / `finance_admin` | only their department's live stream + logs |
| `ciso` | clear **security** freezes (e.g. SEC-04 contractor→prod) |
| `cfo` | clear **finance/spend** freezes (e.g. FIN-12) + approve high spend |
| `dpo` | clear **data-privacy** freezes (e.g. PROC-07, DZ-INV-01 missing DPA/NIF) |
| `requester` | submit requests, view own runs |

`ciso`/`cfo`/`dpo` are the **authorities** that can lift a governance freeze — the gateway
checks the caller actually holds the required authority before clearing (see
`POST /api/runs/{id}/clear-veto`).

## 4. Demo users (username / password → roles)

| User | Password | Roles | Dept |
|---|---|---|---|
| `admin1` | `admin1` | company_admin | — |
| `hr1` | `hr1` | hr_admin | hr |
| `it1` | `it1` | it_admin | it |
| `finance1` | `finance1` | finance_admin | finance |
| `ciso` | `ciso` | ciso, it_admin | it |
| `cfo` | `cfo` | cfo, finance_admin | finance |
| `dpo` | `dpo` | dpo | — |

(These mirror real Numidia employees, e.g. CISO = Anas Alasmer.)

---

## 5. Get a token (test without the frontend)

```bash
curl -s -X POST \
  http://localhost:8080/realms/orchestrai/protocol/openid-connect/token \
  -d grant_type=password -d client_id=orchestrai-dashboard \
  -d username=ciso -d password=ciso | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])"
```

Then call the gateway:
```bash
TOKEN=...   # from above
curl -s http://localhost:8000/api/sentinel -H "Authorization: Bearer $TOKEN"
```

## 6. How verification works (for reviewers)

`gateway/app/core/auth.py`:
- `_get_jwks_client()` — cached `PyJWKClient` fetches the realm's RS256 public keys.
- `_keycloak_principal()` — `jwt.decode(...)` verifies **signature + expiry + issuer**; reads
  `realm_access.roles` and the `department` claim into a `Principal`.
- `require_role(*roles)` — FastAPI dependency; 403 unless the principal holds a role
  (company_admin bypasses).
- WebSockets authenticate via `?token=` (Keycloak) or `?role=&department=` (DEV).

Front-end: log in via the `orchestrai-dashboard` OIDC client, then send the access token as
`Authorization: Bearer <token>` on every request and `?token=<token>` on WebSocket connects.

---

## 7. Frontend OIDC config (Next.js)
```
NEXT_PUBLIC_KEYCLOAK_URL=http://localhost:8080
NEXT_PUBLIC_KEYCLOAK_REALM=orchestrai
NEXT_PUBLIC_KEYCLOAK_CLIENT_ID=orchestrai-dashboard
```
Use a maintained OIDC library (e.g. `keycloak-js` or NextAuth Keycloak provider).
