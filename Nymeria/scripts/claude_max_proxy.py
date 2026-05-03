#!/usr/bin/env python3
"""
Claude Max Proxy — Lightweight reverse proxy for Anthropic API via Claude Max OAuth.

Reads OAuth credentials from ~/.claude/.credentials.json (written by Claude Code),
swaps the x-api-key header for a Bearer token with the oauth-2025-04-20 beta header,
and forwards requests unchanged to api.anthropic.com. Zero format translation —
tool definitions, tool calls, and streaming SSE pass through verbatim.

Usage:
    python scripts/claude_max_proxy.py                  # Default (127.0.0.1:8318)
    python scripts/claude_max_proxy.py --port 8318      # Custom port
"""

import json
import logging
import time
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

logger = logging.getLogger(__name__)

CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
ANTHROPIC_API = "https://api.anthropic.com"
TOKEN_REFRESH_URL = "https://console.anthropic.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"

# Refresh 60 seconds before actual expiry to avoid edge-case failures
EXPIRY_BUFFER_MS = 60_000

app = FastAPI(title="Claude Max Proxy", docs_url=None, redoc_url=None)

# Module-level state
_credentials: dict = {}
_http_client: httpx.AsyncClient | None = None


def _load_credentials() -> dict:
    """Load OAuth credentials from Claude Code's credential store."""
    if not CREDENTIALS_PATH.exists():
        raise FileNotFoundError(
            f"Credentials not found at {CREDENTIALS_PATH}. "
            "Ensure Claude Code is installed and authenticated."
        )
    data = json.loads(CREDENTIALS_PATH.read_text())
    oauth = data.get("claudeAiOauth")
    if not oauth:
        raise ValueError("No claudeAiOauth key in credentials file.")
    return oauth


def _is_expired(creds: dict) -> bool:
    """Check if the access token is expired (with buffer)."""
    expires_at = creds.get("expiresAt", 0)
    now_ms = int(time.time() * 1000)
    return now_ms >= (expires_at - EXPIRY_BUFFER_MS)


async def _refresh_token(creds: dict) -> dict:
    """Refresh the access token using the refresh token."""
    global _credentials

    refresh_token = creds.get("refreshToken")
    if not refresh_token:
        raise ValueError("No refreshToken in credentials — re-authenticate Claude Code.")

    logger.info("Access token expired, refreshing...")

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            TOKEN_REFRESH_URL,
            json={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": CLIENT_ID,
            },
        )
        resp.raise_for_status()
        token_data = resp.json()

    # Update in-memory credentials
    creds["accessToken"] = token_data["access_token"]
    creds["refreshToken"] = token_data.get("refresh_token", refresh_token)
    creds["expiresAt"] = int(time.time() * 1000) + token_data.get("expires_in", 28800) * 1000

    # Persist back to disk so other tools pick up the new token
    full_data = json.loads(CREDENTIALS_PATH.read_text())
    full_data["claudeAiOauth"] = creds
    CREDENTIALS_PATH.write_text(json.dumps(full_data, indent=2))

    _credentials = creds
    logger.info("Token refreshed successfully (expires in %ds)", token_data.get("expires_in", 0))
    return creds


async def _get_access_token() -> str:
    """Get a valid access token, refreshing if needed."""
    global _credentials

    if not _credentials:
        _credentials = _load_credentials()

    if _is_expired(_credentials):
        _credentials = await _refresh_token(_credentials)

    return _credentials["accessToken"]


def _build_headers(access_token: str, original_headers: dict) -> dict:
    """Build upstream headers: forward all, swap auth, merge beta flags."""
    # Headers to drop (hop-by-hop, auth we're replacing, proxy artifacts)
    skip = {"host", "x-api-key", "content-length", "transfer-encoding", "connection"}

    headers = {
        k: v for k, v in original_headers.items()
        if k.lower() not in skip
    }

    # Swap auth: remove any existing auth, add OAuth Bearer
    headers.pop("authorization", None)
    headers["Authorization"] = f"Bearer {access_token}"

    # Merge anthropic-beta: keep SDK's betas (e.g. interleaved-thinking)
    # and append the OAuth beta flag
    existing_beta = headers.get("anthropic-beta", "")
    oauth_beta = "oauth-2025-04-20"
    if existing_beta:
        if oauth_beta not in existing_beta:
            headers["anthropic-beta"] = f"{existing_beta},{oauth_beta}"
    else:
        headers["anthropic-beta"] = oauth_beta

    return headers


@app.post("/v1/messages")
async def proxy_messages(request: Request):
    """Forward /v1/messages to Anthropic API with OAuth auth."""
    global _http_client

    try:
        access_token = await _get_access_token()
    except Exception as e:
        logger.error("Failed to get access token: %s", e)
        return JSONResponse({"error": str(e)}, status_code=502)

    body = await request.body()
    headers = _build_headers(access_token, dict(request.headers))
    logger.info("Proxy headers: %s", {k: v[:20] + "..." if len(v) > 20 else v for k, v in headers.items()})

    if _http_client is None:
        _http_client = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0))

    # Check if streaming
    try:
        body_json = json.loads(body)
        is_streaming = body_json.get("stream", False)
    except (json.JSONDecodeError, UnicodeDecodeError):
        is_streaming = False

    upstream_url = f"{ANTHROPIC_API}/v1/messages"

    try:
        return await _forward_request(headers, body, is_streaming, upstream_url)
    except Exception as e:
        logger.error("Proxy error: %s", e, exc_info=True)
        return JSONResponse({"error": f"Proxy error: {str(e)}"}, status_code=502)


async def _forward_request(headers, body, is_streaming, upstream_url):
    """Forward the request to Anthropic."""
    if is_streaming:
        # Stream SSE chunks back verbatim
        req = _http_client.build_request("POST", upstream_url, headers=headers, content=body)
        upstream_resp = await _http_client.send(req, stream=True)

        async def stream_generator():
            try:
                async for chunk in upstream_resp.aiter_bytes():
                    yield chunk
            finally:
                await upstream_resp.aclose()

        return StreamingResponse(
            stream_generator(),
            status_code=upstream_resp.status_code,
            media_type=upstream_resp.headers.get("content-type", "text/event-stream"),
        )
    else:
        # Non-streaming: forward response
        resp = await _http_client.post(upstream_url, headers=headers, content=body)
        try:
            return JSONResponse(content=resp.json(), status_code=resp.status_code)
        except (json.JSONDecodeError, ValueError):
            # Upstream returned non-JSON (e.g. HTML error page) — pass through raw
            from fastapi.responses import Response
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                media_type=resp.headers.get("content-type", "text/plain"),
            )


@app.get("/v1/models")
async def list_models():
    """Return available Claude models (for frontend/tooling compatibility)."""
    models = [
        {"id": "claude-opus-4-6", "name": "Claude Opus 4.6"},
        {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6"},
        {"id": "claude-haiku-4-5-20251001", "name": "Claude Haiku 4.5"},
        {"id": "claude-opus-4-20250514", "name": "Claude Opus 4"},
        {"id": "claude-sonnet-4-20250514", "name": "Claude Sonnet 4"},
    ]
    return {"data": models, "object": "list"}


@app.on_event("startup")
async def startup():
    """Validate credentials on startup."""
    global _credentials
    try:
        _credentials = _load_credentials()
        expired = _is_expired(_credentials)
        sub_type = _credentials.get("subscriptionType", "unknown")
        logger.info(
            "Loaded credentials (subscription=%s, expired=%s)", sub_type, expired
        )
        if expired:
            logger.info("Token expired — will refresh on first request.")
    except Exception as e:
        logger.error("Failed to load credentials: %s", e)
        raise


@app.on_event("shutdown")
async def shutdown():
    """Clean up HTTP client."""
    global _http_client
    if _http_client:
        await _http_client.aclose()
        _http_client = None


def run(host: str = "127.0.0.1", port: int = 8318) -> None:
    """Run the proxy server."""
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"  WARNING: Binding to {host} exposes your Claude Max subscription")
        print(f"  to the network. The proxy has no authentication. Use 127.0.0.1")
        print(f"  unless you know what you're doing.")
    print(f"Starting Claude Max Proxy on {host}:{port}")
    print(f"  Credentials: {CREDENTIALS_PATH}")
    print(f"  Upstream: {ANTHROPIC_API}")
    print(f"  Endpoint: http://{host}:{port}/v1/messages")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Claude Max Proxy")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", "-p", type=int, default=8318, help="Bind port (default: 8318)")
    args = parser.parse_args()
    run(host=args.host, port=args.port)
