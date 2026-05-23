"""HTTP endpoint that resolves in-flight ``chrome_*`` browser commands.

The Chrome extension POSTs the outcome of every ``browser_command`` SSE
event it executes here. The endpoint resolves the in-process future the
calling tool is awaiting (see
:mod:`nymeria.core.browser_command_coordinator`).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ...core.accounts import AuthenticatedUser
from ...core.browser_command_coordinator import get_browser_command_coordinator
from ...core.event_bus import publish_autonomous_event
from ..schemas.browser_commands import BrowserCommandAck, BrowserCommandResult

logger = logging.getLogger(__name__)


def create_browser_commands_router(
    verify_api_key: Callable[..., Any],
) -> APIRouter:
    router = APIRouter(tags=["Browser Commands"])

    @router.post(
        "/browser-commands/{command_id}/result",
        response_model=BrowserCommandAck,
    )
    async def post_browser_command_result(
        command_id: str,
        body: BrowserCommandResult,
        user: AuthenticatedUser = Depends(verify_api_key),
    ) -> BrowserCommandAck:
        coord = get_browser_command_coordinator()
        command = coord.get(command_id)
        if command is None:
            # Already resolved by timeout, sweep, or a previous POST.
            # Fire the observability event anyway so other clients see the
            # outcome, then return delivered=False so the extension can log it.
            logger.info(
                "browser_command_result for unknown command_id=%s (user=%s) - "
                "likely already resolved",
                command_id,
                user.id,
            )
            return BrowserCommandAck(received=True, delivered=False)

        if command.user_id != user.id and user.role != "admin":
            # Mirrors credential_prompts.py:98-104 — never leak existence to
            # another user.
            raise HTTPException(status_code=404, detail="Command not found")

        payload: dict[str, Any] = {
            "ok": body.ok,
            "status": body.status,
        }
        if body.data is not None:
            payload["data"] = body.data
        if body.error is not None:
            payload["error"] = body.error

        delivered = coord.resolve(command_id, payload)

        try:
            publish_autonomous_event(
                event_type="browser_command_result",
                thread_id=command.thread_id,
                user_id=command.user_id,
                task_id="",
                data={
                    "command_id": command_id,
                    "command_type": command.command_type,
                    "ok": body.ok,
                    "status": body.status,
                    "_origin_client_id": f"nymeria-browser:{user.id}",
                },
            )
        except Exception:
            # Observability event is best-effort; never break the resolve path.
            logger.exception(
                "failed to publish browser_command_result event for %s",
                command_id,
            )

        return BrowserCommandAck(received=True, delivered=delivered)

    return router
