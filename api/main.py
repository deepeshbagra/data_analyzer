"""FastAPI entrypoint.

Health and readiness, plus the Phase 1 feature routers mounted below.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, status
from fastapi.responses import JSONResponse
from sqlalchemy import text

from api.db.session import rw_engine
from api.routers import uploads
from api.settings import get_settings

settings = get_settings()

app = FastAPI(
    title="Document Intelligence API",
    version="0.1.0",
    docs_url="/docs" if settings.environment != "production" else None,
)


app.include_router(uploads.router)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness. Deliberately does not touch the database."""
    return {"status": "ok", "environment": settings.environment}


@app.get("/ready")
def ready() -> JSONResponse:
    """Readiness. Verifies the runtime role can reach Postgres.

    Also asserts the app is *not* connected as a superuser -- if it were, every
    row-level security policy in the system would be silently bypassed, so this
    is a startup-time guard against a misconfigured DATABASE_URL.
    """
    checks: dict[str, Any] = {}
    try:
        with rw_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
            row = conn.execute(
                text("SELECT current_user, usesuper FROM pg_user WHERE usename = current_user")
            ).one()
        checks["database"] = "ok"
        checks["role"] = row.current_user
        if row.usesuper:
            checks["database"] = "misconfigured: app role is a superuser, RLS is bypassed"
            return JSONResponse(checks, status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    except Exception as exc:  # readiness reports failures, it never raises
        checks["database"] = f"unavailable: {type(exc).__name__}"
        return JSONResponse(checks, status_code=status.HTTP_503_SERVICE_UNAVAILABLE)

    return JSONResponse(checks, status_code=status.HTTP_200_OK)
