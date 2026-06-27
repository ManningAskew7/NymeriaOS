"""WhatsApp Cloud API webhook routes."""

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
from ...triggers.whatsapp_bot import (
    BotAPIError,
    NymeriaWhatsAppBot,
    WhatsAppCloudClient,
    extract_inbound_messages,
)
from ...triggers.webhook_security import (
    reject_stale_messages,
    require_configured_secret,
    verify_meta_signature,
)
from ._bot_inprocess import InProcessBotAPI

logger = logging.getLogger(__name__)

_WHATSAPP_SEEN_CACHE = SeenEventCache()


def create_whatsapp_bot_router(
    get_agent_fn: Callable[[], Any],
    get_settings_fn: Callable[[], Settings],
    require_thread_access_fn: Callable[[AuthenticatedUser, str], None],
    publish_sync_event_fn: Callable[..., Any] = default_publish_sync_event,
) -> APIRouter:
    """Create WhatsApp Cloud API webhook routes."""
    router = APIRouter(tags=["WhatsApp"])

    @router.get("/integrations/whatsapp/webhook", response_class=PlainTextResponse)
    async def verify_whatsapp_webhook(
        hub_mode: Optional[str] = Query(default=None, alias="hub.mode"),
        hub_verify_token: Optional[str] = Query(default=None, alias="hub.verify_token"),
        hub_challenge: Optional[str] = Query(default=None, alias="hub.challenge"),
    ):
        settings = get_settings_fn()
        expected = (settings.whatsapp_webhook_verify_token or "").strip()
        if not expected:
            raise HTTPException(
                status_code=503,
                detail="WHATSAPP_WEBHOOK_VERIFY_TOKEN is not configured",
            )
        if hub_mode == "subscribe" and hub_verify_token == expected:
            return PlainTextResponse(hub_challenge or "")
        raise HTTPException(status_code=403, detail="Webhook verification failed")

    @router.post("/integrations/whatsapp/webhook")
    async def receive_whatsapp_webhook(
        request: Request,
        background_tasks: BackgroundTasks,
    ):
        settings = get_settings_fn()
        raw_body = await request.body()
        app_secret = require_configured_secret(
            settings.whatsapp_app_secret,
            "WHATSAPP_APP_SECRET",
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
        reject_stale_messages(payload, extract_inbound_messages, unit="seconds")
        if not settings.whatsapp_access_token or not settings.whatsapp_phone_number_id:
            raise HTTPException(
                status_code=503,
                detail="WHATSAPP_ACCESS_TOKEN and WHATSAPP_PHONE_NUMBER_ID are required",
            )
        background_tasks.add_task(
            _process_whatsapp_webhook,
            payload,
            settings,
            get_agent_fn,
            require_thread_access_fn,
            publish_sync_event_fn,
        )
        return {"status": "accepted"}

    return router


async def _process_whatsapp_webhook(
    payload: dict[str, Any],
    settings: Settings,
    get_agent_fn: Callable[[], Any],
    require_thread_access_fn: Callable[[AuthenticatedUser, str], None],
    publish_sync_event_fn: Callable[..., Any],
) -> None:
    cloud = WhatsAppCloudClient(
        access_token=settings.whatsapp_access_token or "",
        phone_number_id=settings.whatsapp_phone_number_id or "",
        base_url=settings.whatsapp_base_url,
    )
    bot = NymeriaWhatsAppBot(
        InProcessBotAPI(
            agent=get_agent_fn(),
            require_thread_access_fn=require_thread_access_fn,
            publish_sync_event_fn=publish_sync_event_fn,
            origin_client_id="whatsapp",
            display_name="WhatsApp",
            error_cls=BotAPIError,
        ),
        cloud,
        show_tool_events=settings.whatsapp_show_tool_events,
        seen_cache=_WHATSAPP_SEEN_CACHE,
    )
    try:
        await bot.handle_webhook(payload)
    except Exception:  # noqa: BLE001
        logger.exception("WhatsApp webhook processing failed")
    finally:
        await cloud.close()
