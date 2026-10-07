# lineage: REQ-GF-001 / implement.foundation (see runs/ for the decision trail)
"""Pure-ASGI middleware: request ids + access log, security headers, body size limit."""

import json
import re
import time
import uuid

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from service.errors import PayloadTooLargeError, error_body
from service.observability import get_logger, request_id_var

access_log = get_logger("access")
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class RequestContextMiddleware:
    """Assigns ``X-Request-ID`` (accepting a well-formed inbound one) and logs each request.

    The access log deliberately records method, route, status and latency only: no client IP,
    user agent or query string (compliance policy CMP-001).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        inbound = dict(scope["headers"]).get(b"x-request-id", b"").decode("latin-1")
        request_id = inbound if _VALID_REQUEST_ID.match(inbound) else uuid.uuid4().hex
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        status = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message).append("X-Request-ID", request_id)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            route = scope.get("route")
            access_log.info(
                "request",
                extra={
                    "method": scope["method"],
                    "route": getattr(route, "path", "unmatched"),
                    "status": status,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )
            request_id_var.reset(token)


_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "interest-cohort=()",
}
_API_CSP = "default-src 'none'; frame-ancestors 'none'"
_DOCS_PATHS = ("/docs", "/redoc")


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp, *, hsts: bool = False) -> None:
        self.app = app
        self.hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        is_docs = str(scope.get("path", "")).startswith(_DOCS_PATHS)

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for key, value in _SECURITY_HEADERS.items():
                    headers.setdefault(key, value)
                if not is_docs:
                    headers.setdefault("Content-Security-Policy", _API_CSP)
                if self.hsts:
                    headers.setdefault("Strict-Transport-Security", "max-age=63072000")
            await send(message)

        await self.app(scope, receive, send_wrapper)


class BodySizeLimitMiddleware:
    """Rejects bodies over ``max_bytes`` with 413, by Content-Length and while streaming."""

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
            await self._reject(send)
            return
        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    # Raised inside the route's body read, so the app's handler renders a 413.
                    raise PayloadTooLargeError(f"request body exceeds {self.max_bytes} bytes")
            return message

        await self.app(scope, limited_receive, send)

    async def _reject(self, send: Send) -> None:
        body = json.dumps(
            error_body("payload_too_large", f"request body exceeds {self.max_bytes} bytes")
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
