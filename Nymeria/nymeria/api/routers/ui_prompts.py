"""HTTP endpoint that resolves in-flight ``ui_prompt`` interactive prompts.

The desktop app POSTs the user's submission (or dismissal) of every
``ui_prompt`` SSE event here. The endpoint resolves the in-process future
the calling tool is awaiting (see :mod:`nymeria.core.ui_prompt_coordinator`)
and publishes a ``ui_prompt_result`` event so every other open client
retracts its copy of the modal.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ...core.accounts import AuthenticatedUser
from ...core.event_bus import publish_autonomous_event
from ...core.ui_prompt_coordinator import get_ui_prompt_coordinator
from ..schemas.ui_prompts import UiPromptAck, UiPromptResult

logger = logging.getLogger(__name__)


def create_ui_prompts_router(
    verify_api_key: Callable[..., Any],
) -> APIRouter:
    router = APIRouter(tags=["UI Prompts"])

    @router.post(
        "/ui-prompts/{prompt_id}/result",
        response_model=UiPromptAck,
    )
    async def post_ui_prompt_result(
        prompt_id: str,
        body: UiPromptResult,
        user: AuthenticatedUser = Depends(verify_api_key),
    ) -> UiPromptAck:
        coord = get_ui_prompt_coordinator()
        prompt = coord.get(prompt_id)
        if prompt is None:
            # Already resolved by timeout, abort, sweep, or a previous POST.
            logger.info(
                "ui_prompt_result for unknown prompt_id=%s (user=%s) - "
                "likely already resolved",
                prompt_id,
                user.id,
            )
            return UiPromptAck(received=True, delivered=False)

        if prompt.user_id != user.id and user.role != "admin":
            # Never leak existence to another user (mirrors browser_commands).
            raise HTTPException(status_code=404, detail="Prompt not found")

        payload: dict[str, Any] = {
            "ok": body.status == "submitted",
            "status": body.status,
        }
        if body.status == "submitted":
            payload["values"] = body.values or {}

        delivered = coord.resolve(prompt_id, payload)

        try:
            # Deliberately no values on the bus: they may hold sensitive user
            # input, and the agent already receives them via the tool result.
            publish_autonomous_event(
                event_type="ui_prompt_result",
                thread_id=prompt.thread_id,
                user_id=prompt.user_id,
                task_id="",
                data={
                    "prompt_id": prompt_id,
                    "status": body.status,
                },
            )
        except Exception:
            # Observability event is best-effort; never break the resolve path.
            logger.exception(
                "failed to publish ui_prompt_result event for %s",
                prompt_id,
            )

        return UiPromptAck(received=True, delivered=delivered)

    return router
