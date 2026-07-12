"""Pluggable agent-turn executor abstraction.

In slim, the API process owns a ``NymeriaAgent`` and runs every turn locally
through ``agent.astream(...)``. In Docker, the worker container schedules
TODOs and trigger events but no longer runs the agent itself — it relays the
turn to the API container via HTTP, so the API stays the single agent
runtime (and the in-memory ``ThreadLockManager`` / ``PendingPromptQueue``
remain authoritative per-thread).

This module exposes a uniform ``TurnExecutor`` protocol so the ticker and
trigger manager don't care which mode they're running in. ``LocalAgentExecutor``
wraps a ``NymeriaAgent``; ``APIClientExecutor`` wraps a ``NymeriaAPIClient``
and translates kwargs into the ``/chat`` request body.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, AsyncIterator, Protocol, runtime_checkable

if TYPE_CHECKING:
    from .agent import NymeriaAgent
    from ..triggers.api_client import NymeriaAPIClient

logger = logging.getLogger(__name__)


@runtime_checkable
class TurnExecutor(Protocol):
    """Minimal surface every turn executor must implement.

    ``is_remote`` lets callers tell at a glance whether they are routing
    through HTTP (and therefore can't assume in-memory agent state is
    available locally). ``astream`` must accept the same kwargs as
    :meth:`NymeriaAgent.astream` and yield the same chunk dicts.
    ``run_workflow`` is the headless workflow-firing seam: local executors
    run the engine in-process (the API is the only workflow runtime, exactly
    like agent turns), remote ones relay to ``POST /workflows/{id}/execute``.
    """

    is_remote: bool

    def astream(self, **astream_kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        ...

    async def run_workflow(
        self,
        workflow_id: str,
        params: dict[str, Any],
        *,
        user_id: str,
        thread_id: str,
    ) -> dict[str, Any]:
        ...

    async def aclose(self) -> None:
        ...


class LocalAgentExecutor:
    """Adapter that runs turns directly on an in-process ``NymeriaAgent``."""

    is_remote = False

    def __init__(self, agent: "NymeriaAgent") -> None:
        self._agent = agent

    @property
    def agent(self) -> "NymeriaAgent":
        """The wrapped in-process agent (turn-buffer tee reads context stats
        and the effective model off it for the synthesized ``done`` event)."""
        return self._agent

    async def astream(self, **astream_kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        async for chunk in self._agent.astream(**astream_kwargs):
            yield chunk

    async def run_workflow(
        self,
        workflow_id: str,
        params: dict[str, Any],
        *,
        user_id: str,
        thread_id: str,
    ) -> dict[str, Any]:
        """Run one published workflow in-process; returns the envelope dict.

        Refusals (missing definition, unapproved revision, depth) raise
        ``RuntimeError`` so callers' failure handling (trigger health, TODO
        retries) sees them; a completed run returns its envelope whatever the
        status, and the caller decides what an error envelope means.
        """
        from .custom_tools import get_custom_tool_loader
        from .workflows.tool_runtime import run_workflow_by_id

        refusal, run = await run_workflow_by_id(
            get_custom_tool_loader(),
            workflow_id,
            dict(params or {}),
            user_id=user_id,
            thread_id=thread_id,
        )
        if refusal is not None:
            raise RuntimeError(f"workflow {workflow_id!r} refused: {refusal}")
        assert run is not None  # exactly one of (refusal, run) is None
        return run.envelope.to_dict()

    async def aclose(self) -> None:
        # The agent's lifetime is owned by whoever constructed it; nothing
        # for this executor to close.
        return None


# Kwargs ``NymeriaAgent.astream`` accepts but ``NymeriaAPIClient.chat_stream``
# does not. We don't pass these to the remote API — the worker has no use for
# legacy image attachments and they're already covered by ``attachments``.
_REMOTE_DROPPED_KWARGS = {"images"}


class APIClientExecutor:
    """Adapter that runs turns by POSTing to ``/chat`` on the API container.

    ``publish_autonomous_events=False`` tells the API not to mirror its own
    autonomous SSE bookends/chunks for this call — the caller (worker ticker
    or trigger manager) is the sole publisher and uses stable task IDs
    (``todo.id`` for scheduled TODOs, ``f"trigger-{trigger.id}"`` for
    triggers). The default True preserves existing watchdog and
    webhook-fire behaviour where the API owns publishing.
    """

    is_remote = True

    def __init__(
        self,
        client: "NymeriaAPIClient",
        *,
        publish_autonomous_events: bool = False,
    ) -> None:
        self._client = client
        self._publish_autonomous_events = publish_autonomous_events

    async def astream(self, **astream_kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        translated = self._translate_kwargs(astream_kwargs)
        async for chunk in self._client.chat_stream(**translated):
            yield chunk

    async def run_workflow(
        self,
        workflow_id: str,
        params: dict[str, Any],
        *,
        user_id: str,
        thread_id: str,
    ) -> dict[str, Any]:
        """Relay a headless workflow run to the API container.

        Same contract as the local executor: the envelope dict for any
        completed run, ``RuntimeError`` for refusals (HTTP 403/404) so the
        caller's failure machinery engages.
        """
        import httpx

        try:
            result = await self._client.run_workflow(
                workflow_id,
                dict(params or {}),
                user_id=user_id,
                thread_id=thread_id or None,
            )
        except httpx.HTTPStatusError as exc:
            detail = ""
            try:
                detail = str(exc.response.json().get("detail") or "")
            except Exception:  # noqa: BLE001 - non-JSON error body
                detail = exc.response.text[:200]
            raise RuntimeError(
                f"workflow {workflow_id!r} refused: {detail or exc}"
            ) from exc
        envelope = result.get("envelope") if isinstance(result, dict) else None
        if not isinstance(envelope, dict):
            raise RuntimeError(
                f"workflow {workflow_id!r} relay returned no envelope"
            )
        return envelope

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _translate_kwargs(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """Convert ``agent.astream`` kwargs into ``chat_stream`` kwargs.

        ``_is_self_invoke`` and ``_trigger_override`` lose their leading
        underscores (the chat_stream API exposes them as ``is_self_invoke`` /
        ``trigger_override``). Unknown agent-only kwargs are dropped with a
        debug log so future callers don't silently lose data.
        """
        translated: dict[str, Any] = {}

        for key in (
            "message",
            "thread_id",
            "user_id",
            "attachments",
            "force_unsupported_attachments",
            "source",
            "source_id",
            "source_label",
            "trigger_id",
            "trigger_name",
        ):
            if key in kwargs:
                translated[key] = kwargs[key]

        if "_is_self_invoke" in kwargs:
            translated["is_self_invoke"] = bool(kwargs["_is_self_invoke"])
        if "_trigger_override" in kwargs:
            translated["trigger_override"] = kwargs["_trigger_override"]

        translated["publish_autonomous_events"] = self._publish_autonomous_events

        known = set(translated) | {"_is_self_invoke", "_trigger_override"}
        extra = [k for k in kwargs if k not in known and k not in _REMOTE_DROPPED_KWARGS]
        if extra:
            logger.debug(
                "APIClientExecutor dropping kwargs not supported by chat_stream: %s",
                extra,
            )
        return translated


def wrap_for_stream(
    executor_or_agent: Any,
) -> "TurnExecutor":
    """Return a ``TurnExecutor`` for ``executor_or_agent``.

    Convenience for call sites that historically passed a ``NymeriaAgent``
    directly to ``stream_and_collect`` and now want to keep doing so.
    Anything that already exposes ``astream`` is returned unchanged so we
    don't accidentally double-wrap.
    """
    if hasattr(executor_or_agent, "is_remote"):
        return executor_or_agent
    return LocalAgentExecutor(executor_or_agent)
