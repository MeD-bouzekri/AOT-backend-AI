"""Gateway settings - environment-driven configuration."""

from __future__ import annotations

import os


class Settings:
    # auth
    DEV_AUTH: bool = os.getenv("DEV_AUTH", "1") == "1"
    KEYCLOAK_JWKS_URL: str = os.getenv(
        "KEYCLOAK_JWKS_URL",
        "http://localhost:8080/realms/orchestrai/protocol/openid-connect/certs")
    KEYCLOAK_ISSUER: str = os.getenv(
        "KEYCLOAK_ISSUER", "http://localhost:8080/realms/orchestrai")
    KEYCLOAK_AUDIENCE: str = os.getenv("KEYCLOAK_AUDIENCE", "account")

    # Keycloak Admin API - server-side only; NEVER exposed to the frontend.
    # Used to create users / assign roles on behalf of a company_admin.
    KEYCLOAK_BASE_URL: str = os.getenv("KEYCLOAK_BASE_URL", "http://localhost:8080")
    KEYCLOAK_REALM: str = os.getenv("KEYCLOAK_REALM", "orchestrai")
    KEYCLOAK_ADMIN_REALM: str = os.getenv("KEYCLOAK_ADMIN_REALM", "master")
    KEYCLOAK_ADMIN_CLIENT_ID: str = os.getenv("KEYCLOAK_ADMIN_CLIENT_ID", "admin-cli")
    KEYCLOAK_ADMIN_USER: str = os.getenv("KEYCLOAK_ADMIN_USER", "admin")
    KEYCLOAK_ADMIN_PASSWORD: str = os.getenv("KEYCLOAK_ADMIN_PASSWORD", "admin")

    # CORS - the Next.js dashboard origin(s)
    CORS_ORIGINS: list[str] = [
        o.strip() for o in os.getenv(
            "CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000"
        ).split(",") if o.strip()
    ]
    # With allow_credentials=True a literal "*" is rejected by browsers, so we
    # match tunnels (ngrok, etc.) by regex and echo the real origin back.
    # Default allows localhost + any *.ngrok / *.ngrok-free.app host.
    CORS_ORIGIN_REGEX: str = os.getenv(
        "CORS_ORIGIN_REGEX",
        r"https?://(localhost|127\.0\.0\.1)(:\d+)?|https://.*\.(ngrok\.io|ngrok-free\.app|ngrok\.app)",
    )

    # engine
    MODE: str = os.getenv("MODE", "live")            # live | demo

    API_PREFIX: str = "/api"


settings = Settings()
