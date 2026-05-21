"""RFC 8628 device-code polling loop for OAuth providers.

The flow:

1. ``request_credential(kind="oauth", flow="device_code", ...)`` performs the
   initial ``POST {device_authorization_uri}`` (in ``oauth_start``) to obtain
   the ``device_code``/``user_code``/``verification_uri``/``interval``/``expires_in``.
2. It then spawns :func:`poll_device_token` as an asyncio task and registers
   the task in this module's in-memory ``_POLL_TASKS`` map so cancellation can
   interrupt cleanly without storing non-JSON data on ``PendingPrompt``.
3. The poller posts to ``{token_uri}`` at the provider-suggested ``interval``.
   On any of the RFC 8628 error codes:
       - ``authorization_pending`` → keep polling.
       - ``slow_down`` → increase interval by 5s and keep polling.
       - ``expired_token``/``access_denied`` → resolve as failure.
   On success it hands off to :func:`finalize_oauth_credential`, which writes
   the vault record and resolves the coordinator future.

This module never touches the agent directly; the only public surface is the
poll task, which the start helper schedules.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import httpx

from ..config.oauth_providers import OAuthProviderDescriptor
from .auth_prompt_coordinator import get_auth_prompt_coordinator
from .oauth_callback_handler import fail_oauth_prompt, finalize_oauth_credential

logger = logging.getLogger(__name__)


_DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
_TERMINAL_ERRORS = {
    "access_denied": ("denied", "User declined the authentication request."),
    "expired_token": ("expired", "The device code expired before the user completed sign-in."),
}
_HTTP_TIMEOUT_SECONDS = 30.0
_MIN_INTERVAL_SECONDS = 1
_MAX_INTERVAL_SECONDS = 60

# In-memory registry of running poll tasks, keyed by prompt_id. The task
# itself is not JSON-serialisable so it must not live on PendingPrompt.metadata
# (anyone iterating that dict for SSE/logs would crash). The poller removes
# its own entry on exit; the start tool registers its entry after spawn.
_POLL_TASKS: dict[str, "asyncio.Task[None]"] = {}


def register_poll_task(prompt_id: str, task: "asyncio.Task[None]") -> None:
    """Track a poll task so it can be cancelled when the prompt resolves."""
    _POLL_TASKS[prompt_id] = task


def cancel_poll_task(prompt_id: str) -> None:
    """Cancel and forget the poll task for ``prompt_id`` if one is running."""
    task = _POLL_TASKS.pop(prompt_id, None)
    if task is not None and not task.done():
        task.cancel()


def _forget_poll_task(prompt_id: str) -> None:
    _POLL_TASKS.pop(prompt_id, None)


async def poll_device_token(
    *,
    prompt_id: str,
    descriptor: OAuthProviderDescriptor,
    client_id: str,
    client_secret: str | None,
    device_code: str,
    interval: int,
    expires_in: int,
) -> None:
    """Poll ``descriptor.token_uri`` until success, terminal error, or expiry.

    Resolves the coordinator future as a side effect. Logs but does not raise.
    """
    coordinator = get_auth_prompt_coordinator()
    if coordinator.get(prompt_id) is None:
        logger.debug("device-code poll started for missing prompt=%s", prompt_id)
        return

    deadline = asyncio.get_running_loop().time() + max(1, int(expires_in or 0))
    current_interval = max(_MIN_INTERVAL_SECONDS, min(_MAX_INTERVAL_SECONDS, int(interval or 5)))

    def _resolve_failure(status: str, message: str, last_error: Optional[str] = None) -> None:
        """Re-fetch the prompt at resolve time so a swept/rotated prompt doesn't
        get us silently no-op'd. ``coordinator.resolve`` is the only safe way
        to wake the agent's ``wait_for``."""
        fresh = coordinator.get(prompt_id)
        if fresh is None:
            logger.debug("device-code poll: prompt %s already resolved/swept", prompt_id)
            return
        fail_oauth_prompt(prompt=fresh, status=status, message=message, last_error=last_error)

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            while True:
                # Sleep first — RFC 8628 says "the client MUST wait at least
                # the number of seconds specified by the 'interval' parameter".
                await asyncio.sleep(current_interval)

                if asyncio.get_running_loop().time() > deadline:
                    _resolve_failure(
                        status="expired",
                        message=(
                            f"Sign-in window expired before {descriptor.display_name} "
                            "authorization completed."
                        ),
                    )
                    return

                data = {
                    "grant_type": _DEVICE_GRANT_TYPE,
                    "client_id": client_id,
                    "device_code": device_code,
                }
                if client_secret:
                    data["client_secret"] = client_secret

                try:
                    response = await client.post(descriptor.token_uri, data=data)
                except httpx.HTTPError as exc:
                    logger.warning(
                        "device-code poll transient error for %s: %s",
                        descriptor.provider_id,
                        exc,
                    )
                    continue

                payload = _safe_json(response)
                if response.status_code == 200 and "access_token" in payload:
                    fresh = coordinator.get(prompt_id)
                    if fresh is None:
                        logger.debug(
                            "device-code poll: prompt %s vanished before finalize", prompt_id
                        )
                        return
                    finalize_oauth_credential(
                        prompt=fresh,
                        descriptor=descriptor,
                        token_data=payload,
                        source="oauth_device_flow",
                    )
                    return

                error = payload.get("error") or ""
                if error == "authorization_pending":
                    continue
                if error == "slow_down":
                    current_interval = min(_MAX_INTERVAL_SECONDS, current_interval + 5)
                    continue
                if error in _TERMINAL_ERRORS:
                    status, message = _TERMINAL_ERRORS[error]
                    _resolve_failure(status=status, message=message)
                    return

                # Unknown error or HTTP failure with no recognised payload — bail.
                message = (
                    payload.get("error_description")
                    or payload.get("error")
                    or f"Provider returned HTTP {response.status_code}."
                )
                _resolve_failure(
                    status="error",
                    message=f"Device-code exchange failed: {message}",
                    last_error=str(message),
                )
                return
    except asyncio.CancelledError:
        # Caller cancelled (user closed modal / agent timed out). Don't
        # double-resolve — the canceller is expected to resolve the future.
        logger.debug("device-code poll cancelled for prompt=%s", prompt_id)
        raise
    except Exception as exc:  # noqa: BLE001 - top-level safety net
        logger.exception(
            "device-code poll crashed for prompt=%s provider=%s",
            prompt_id,
            descriptor.provider_id,
        )
        # Resolve the future so the agent doesn't hang forever.
        _resolve_failure(
            status="error",
            message=f"Device-code poller crashed: {exc}",
            last_error=str(exc),
        )
    finally:
        _forget_poll_task(prompt_id)


def _safe_json(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
        if isinstance(body, dict):
            return body
    except Exception:
        logger.debug("device-code poll response was not valid JSON", exc_info=True)
    return {}


__all__ = ["poll_device_token", "register_poll_task", "cancel_poll_task"]
