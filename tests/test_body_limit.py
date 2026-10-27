"""SEC-0026 item 2: request-body size cap (BodySizeLimitMiddleware in app/main.py).

Requests whose declared Content-Length exceeds settings.max_request_body_bytes get a 413
JSON response before routing, so FastAPI never parses an oversized multipart body.
Known limitation (not tested here, by design): chunked requests with no Content-Length
are not bounded by this cap.
"""
import asyncio

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import BodySizeLimitMiddleware

CAP = 100


def _mini_app(max_bytes: int = CAP) -> FastAPI:
    mini = FastAPI()
    mini.add_middleware(BodySizeLimitMiddleware, max_bytes=max_bytes)

    @mini.post("/echo")
    async def echo(request: Request):
        return {"received": len(await request.body())}

    return mini


def test_default_cap_matches_largest_legitimate_upload():
    # strava.GPX_MAX_BYTES is 20 MB; the default request cap must not be lower.
    from app.routers.strava import GPX_MAX_BYTES

    assert settings.max_request_body_bytes >= GPX_MAX_BYTES


def test_over_cap_returns_413_json_and_route_does_not_run():
    with TestClient(_mini_app()) as c:
        res = c.post("/echo", content=b"x" * (CAP + 1))
    assert res.status_code == 413
    assert res.json() == {"detail": "Request body too large"}


def test_at_cap_and_normal_requests_pass():
    with TestClient(_mini_app()) as c:
        assert c.post("/echo", content=b"x" * CAP).json() == {"received": CAP}
        assert c.post("/echo", content=b"hello").json() == {"received": 5}
        assert c.post("/echo").json() == {"received": 0}


def test_real_app_rejects_oversized_unauthenticated_multipart_before_auth(client):
    # Pre-auth DoS path from SEC-0026: oversized multipart to an admin-only upload route.
    # Must be 413 (cap), not 401/403 (auth) and not a 5xx.
    too_big = settings.max_request_body_bytes + 1
    res = client.post(
        "/strava/trails/gpx-preview",
        content=b"x" * 16,
        headers={
            "Content-Type": "multipart/form-data; boundary=b",
            "Content-Length": str(too_big),
        },
    )
    assert res.status_code == 413


def test_real_app_normal_requests_still_reach_routes(client):
    assert client.get("/health").status_code == 200
    # A small unauthenticated POST gets past the cap and is rejected by auth, not by size.
    res = client.post("/strava/trails/gpx-preview", files={"file": ("a.gpx", b"<gpx/>")})
    assert res.status_code in (401, 403)


def test_content_length_header_name_is_case_insensitive():
    # ASGI servers lowercase header names, but the middleware must not depend on it.
    seen = {}

    async def inner(scope, receive, send):
        seen["called"] = True

    mw = BodySizeLimitMiddleware(inner, max_bytes=CAP)
    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/echo",
        "headers": [(b"Content-Length", str(CAP + 1).encode())],
    }
    asyncio.run(mw(scope, receive, send))

    assert "called" not in seen
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 413


def test_malformed_content_length_and_non_http_scopes_pass_through():
    calls = []

    async def inner(scope, receive, send):
        calls.append(scope["type"])

    mw = BodySizeLimitMiddleware(inner, max_bytes=CAP)

    async def noop(*_a, **_k):
        return None

    asyncio.run(mw({"type": "http", "headers": [(b"content-length", b"abc")]}, noop, noop))
    asyncio.run(mw({"type": "lifespan", "headers": []}, noop, noop))
    asyncio.run(mw({"type": "websocket", "headers": [(b"content-length", b"999999")]}, noop, noop))
    assert calls == ["http", "lifespan", "websocket"]
