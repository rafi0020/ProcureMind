"""Application factory."""

import logging
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import TimeoutError as PoolTimeout

from app.api import (
    ap_approvals,
    ap_settings,
    approvals,
    audit,
    auth,
    documents,
    evaluation,
    events,
    exceptions,
    health,
    inbox,
    investigations,
    invoices,
    orders,
    settings,
    usage,
    users,
    vendors,
)
from app.config import get_settings
from app.llm.factory import SavedKeyUnreadable
from app.obs import metrics
from app.obs.logging import configure_logging, request_id_var
from app.security.headers import install_security_headers

log = logging.getLogger("procuremind.http")
PUBLIC_DEMO_SECRET = "local-demo-secret-change-me"  # noqa: S105 - the compose default, public by design


def create_app() -> FastAPI:
    config = get_settings()
    configure_logging(config.log_level)
    if config.cookie_secure and config.secret_key in ("", PUBLIC_DEMO_SECRET):
        # COOKIE_SECURE=true says this is a real deployment; the demo secret is printed in the repository.
        raise RuntimeError("Set SECRET_KEY to a private random value before serving over HTTPS.")
    app = FastAPI(title="ProcureMind", version="1.0.0")

    @app.exception_handler(SavedKeyUnreadable)
    async def key_unreadable(_request: Request, exc: SavedKeyUnreadable) -> JSONResponse:
        # The stored key was written under another SECRET_KEY: a configuration problem an administrator can fix.
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(PoolTimeout)
    async def busy(_request: Request, _exc: PoolTimeout) -> JSONResponse:
        # Every database connection of this process stayed in use for the whole wait: the service is overloaded,
        # not broken. Say so, and when to try again, instead of a 500.
        return JSONResponse(
            {"detail": "The service is busy. Try again in a few seconds."},
            status_code=503,
            headers={"Retry-After": "5"},
        )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "X-CSRF-Token", "X-Request-ID", "Last-Event-ID"],
        expose_headers=["X-Request-ID", "Retry-After"],
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = request.headers.get("X-Request-ID", "")[:64] or uuid.uuid4().hex
        token = request_id_var.set(rid)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers["X-Request-ID"] = rid
        if request.url.path not in ("/healthz", "/readyz", "/metrics"):
            log.info(
                "request",
                extra={
                    "request_id": rid,
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "ms": round((time.perf_counter() - started) * 1000, 1),
                },
            )
        return response

    install_security_headers(app)
    metrics.install_request_metrics(app)
    for module in (
        health,
        auth,
        users,
        audit,
        settings,
        investigations,
        events,
        approvals,
        usage,
        vendors,
        documents,
        inbox,
        invoices,
        orders,
        ap_settings,
        exceptions,
        ap_approvals,
        evaluation,
        metrics,
    ):
        app.include_router(module.router)
    app.include_router(inbox.public)
    return app
