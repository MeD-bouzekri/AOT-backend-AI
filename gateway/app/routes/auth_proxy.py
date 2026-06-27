"""
Keycloak passthrough proxy.

Lets the whole app run behind a single tunnel (ngrok 8000): the browser talks
to Keycloak through the gateway at `/auth/...` instead of hitting :8080 directly.
So the frontend sets NEXT_PUBLIC_KEYCLOAK_URL = https://<gateway-url>/auth.

Only forwards what the public client needs (token, logout, certs, openid-config,
auth/login). It is a transparent reverse proxy: method, query, headers and body
pass through; the upstream response is returned as-is.
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Request, Response

from app.core.config import settings

router = APIRouter(prefix="/auth", tags=["auth-proxy"])

# headers we must not copy back (hop-by-hop / set by the server itself)
_DROP = {"content-length", "transfer-encoding", "connection", "keep-alive"}


@router.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD"],
)
async def proxy(path: str, request: Request) -> Response:
    upstream = f"{settings.KEYCLOAK_BASE_URL.rstrip('/')}/{path}"

    # forward the original headers except Host (let httpx set it for upstream)
    fwd_headers = {
        k: v for k, v in request.headers.items() if k.lower() != "host"
    }
    body = await request.body()

    async with httpx.AsyncClient(follow_redirects=False, timeout=30.0) as client:
        upstream_resp = await client.request(
            request.method,
            upstream,
            params=request.query_params,
            headers=fwd_headers,
            content=body,
        )

    resp_headers = {
        k: v for k, v in upstream_resp.headers.items() if k.lower() not in _DROP
    }
    return Response(
        content=upstream_resp.content,
        status_code=upstream_resp.status_code,
        headers=resp_headers,
        media_type=upstream_resp.headers.get("content-type"),
    )
