"""HTTP endpoint that resolves in-flight ``cli_config`` commands.

Connected CLI clients POST the outcome of every ``cli_config`` SSE event
they apply here. The endpoint resolves the in-process future the calling
``cli_statusbar_*`` tool is awaiting (see
:mod:`nymeria.core.cli_config_coordinator`); the first ack wins, later
acks report ``delivered=False``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ...core.accounts import AuthenticatedUser
from ...core.cli_config_coordinator import get_cli_config_coordinator
from ..schemas.cli_config import CLIConfigAck, CLIConfigResult

logger = logging.getLogger(__name__)


def create_cli_config_router(
    verify_api_key: Callable[..., Any],
) -> APIRouter:
    router = APIRouter(tags=["CLI Config"])

    @router.post(
        "/cli-config/{command_id}/result",
        response_model=CLIConfigAck,
    )
    async def post_cli_config_result(
        command_id: str,
        body: CLIConfigResult,
        user: AuthenticatedUser = Depends(verify_api_key),
    ) -> CLIConfigAck:
        coord = get_cli_config_coordinator()
        command = coord.get(command_id)
        if command is None:
            # Already resolved by an earlier CLI's ack, a timeout, or a
            # sweep. Expected with several CLIs connected; report it so the
            # client can log it, never error.
            return CLIConfigAck(received=True, delivered=False)

        if command.user_id != user.id and user.role != "admin":
            # Mirrors browser_commands.py — never leak existence to another
            # user.
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
        return CLIConfigAck(received=True, delivered=delivered)

    return router
