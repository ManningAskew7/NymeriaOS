"""Meta Instagram webhook routes."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from ...config import Settings
from ...core.accounts import AuthenticatedUser
from ...core.event_bus import publish_sync_event as default_publish_sync_event
from ...triggers.bot_helpers import SeenEventCache
from ...triggers.instagram_bot import (
    BotAPIError,
    InstagramGraphClient,
    NymeriaInstagramBot,
    extract_inbound_messages,
)
from ...triggers.webhook_security import (
    reject_stale_messages,
    require_configured_secret,
    verify_meta_signature,
)
from ._bot_inprocess import InProcessBotAPI

logger = logging.getLogger(__name__)

_INSTAGRAM_SEEN_CACHE = SeenEventCache()


def create_instagram_bot_router(
    get_agent_fn: Callable[[], Any],
    get_settings_fn: Callable[[], Settings],
    require_thread_access_fn: Callable[[AuthenticatedUser, str], None],
    publish_sync_event_fn: Callable[..., Any] = default_publish_sync_event,
) -> APIRouter:
    """Create Meta Instagram webhook routes."""
    router = APIRouter(tags=["Instagram"])

    @router.get("/integrations/instagram/webhook", response_class=PlainTextResponse)
    async def verify_instagram_webhook(
        hub_mode: Optional[str] = Query(default=None, alias="hub.mode"),
        hub_verify_token: Optional[str] = Query(default=None, alias="hub.verify_token"),
        hub_challenge: Optional[str] = Query(default=None, alias="hub.challenge"),
    ):
        settings = get_settings_fn()
        expected = (settings.instagram_webhook_verify_token or "").strip()
        if not expected:
            raise HTTPException(
                status_code=503,
                detail="INSTAGRAM_WEBHOOK_VERIFY_TOKEN is not configured",
            )
        if hub_mode == "subscribe" and hub_verify_token == expected:
            return PlainTextResponse(hub_challenge or "")
        raise HTTPException(status_code=403, detail="Webhook verification failed")

    @router.post("/integrations/instagram/webhook")
    async def receive_instagram_webhook(
        request: Request,
        background_tasks: BackgroundTasks,
    ):
        settings = get_settings_fn()
        raw_body = await request.body()
        app_secret = require_configured_secret(
            settings.instagram_app_secret,
            "INSTAGRAM_APP_SECRET",
        )
        if not verify_meta_signature(
            raw_body,
            request.headers.get("x-hub-signature-256"),
            app_secret,
        ):
            raise HTTPException(status_code=403, detail="Invalid webhook signature")
        try:
            payload = json.loads(raw_body.decode("utf-8") or "{}")
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Invalid JSON payload")
        reject_stale_messages(payload, extract_inbound_messages, unit="milliseconds")
        if not settings.instagram_access_token:
            raise HTTPException(
                status_code=503,
                detail="INSTAGRAM_ACCESS_TOKEN is required",
            )
        background_tasks.add_task(
            _process_instagram_webhook,
            payload,
            settings,
            get_agent_fn,
            require_thread_access_fn,
            publish_sync_event_fn,
        )
        return {"status": "accepted"}

    return router


async def _process_instagram_webhook(
    payload: dict[str, Any],
    settings: Settings,
    get_agent_fn: Callable[[], Any],
    require_thread_access_fn: Callable[[AuthenticatedUser, str], None],
    publish_sync_event_fn: Callable[..., Any],
) -> None:
    graph = InstagramGraphClient(
        access_token=settings.instagram_access_token or "",
        ig_user_id=settings.instagram_ig_user_id,
        base_url=settings.instagram_graph_api_base_url,
    )
    bot = NymeriaInstagramBot(
        InProcessBotAPI(
            agent=get_agent_fn(),
            require_thread_access_fn=require_thread_access_fn,
            publish_sync_event_fn=publish_sync_event_fn,
            origin_client_id="instagram",
            display_name="Instagram",
            error_cls=BotAPIError,
        ),
        graph,
        show_tool_events=settings.instagram_show_tool_events,
        seen_cache=_INSTAGRAM_SEEN_CACHE,
    )
    try:
        await bot.handle_webhook(payload)
    except Exception:  # noqa: BLE001
        logger.exception("Instagram webhook processing failed")
    finally:
        await graph.close()
