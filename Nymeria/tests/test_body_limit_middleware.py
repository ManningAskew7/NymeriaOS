"""Tests for the request body-size limit ASGI middleware (H-5)."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from nymeria.triggers.body_limit import BodySizeLimitMiddleware


def _client() -> TestClient:
    app = FastAPI()

    @app.post("/integrations/whatsapp/webhook")
    async def webhook(request: Request):
        body = await request.body()
        return {"len": len(body)}

    @app.post("/chat")
    async def chat(request: Request):
        body = await request.body()
        return {"len": len(body)}

    @app.post("/other")
    async def other(request: Request):
        body = await request.body()
        return {"len": len(body)}

    app.add_middleware(
        BodySizeLimitMiddleware,
        default_limit=64 * 1024,
        path_limits=[("/integrations/", 1024), ("/chat", 8192)],
    )
    return TestClient(app)


def test_rejects_oversized_webhook_via_content_length():
    resp = _client().post("/integrations/whatsapp/webhook", content=b"x" * 2048)
    assert resp.status_code == 413


def test_allows_small_webhook_body():
    resp = _client().post("/integrations/whatsapp/webhook", content=b"x" * 500)
    assert resp.status_code == 200
    assert resp.json()["len"] == 500


def test_per_prefix_limit_lets_chat_exceed_webhook_cap():
    # 2 KB is over the 1 KB webhook cap but under the 8 KB /chat cap.
    resp = _client().post("/chat", content=b"x" * 2048)
    assert resp.status_code == 200
    assert resp.json()["len"] == 2048


def test_default_limit_applies_to_unmatched_path():
    resp = _client().post("/other", content=b"x" * (64 * 1024 + 1))
    assert resp.status_code == 413


def test_rejects_oversized_chunked_body_without_content_length():
    # A generator body makes httpx use chunked transfer-encoding (no
    # Content-Length), exercising the streamed byte-counter stage.
    def gen():
        yield b"x" * 4096

    resp = _client().post("/integrations/whatsapp/webhook", content=gen())
    assert resp.status_code == 413
