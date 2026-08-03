"""Thin HTTP client for the Nymeria REST API.

Used by bot thin clients (Discord, Telegram, Twitch)
worker, slash commands, and ``run.py`` helpers to interact with the
Nymeria backend without running their own NymeriaAgent instance.
All state lives in the API container — this is just a typed wrapper
around ``httpx.AsyncClient``.
"""

import asyncio
import logging
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional
from urllib.parse import quote

import httpx

from .sse_consumer import parse_sse_data_line

logger = logging.getLogger(__name__)

# Generous timeout for LLM calls that can take 30s+
_CHAT_TIMEOUT = httpx.Timeout(connect=10, read=300, write=10, pool=10)
_SSE_TIMEOUT = httpx.Timeout(connect=10, read=None, write=10, pool=10)
_DEFAULT_TIMEOUT = httpx.Timeout(connect=10, read=30, write=10, pool=10)
# A relayed workflow run blocks until the engine finishes; its wall clock is
# clamped to 3600s per definition, so read must outlast that.
_WORKFLOW_TIMEOUT = httpx.Timeout(connect=10, read=3700, write=10, pool=10)


def _path_param(value: Any) -> str:
    """URL-encode a path segment without preserving slashes."""

    return quote(str(value), safe="")


def _clean_params(**values: Any) -> Optional[Dict[str, Any]]:
    """Drop None query parameters while preserving falsey values."""

    params = {key: value for key, value in values.items() if value is not None}
    return params or None


class NymeriaAPIClient:
    """Async HTTP client for the Nymeria REST API.

    When the client is constructed with an admin-role service token, each
    per-user call can attach ``X-Nymeria-Act-As: <user_id>`` so the server
    routes the request as that user. Callers pass ``act_as=<user_id>`` on
    the underlying ``_get``/``_post``/... helpers; the header is the
    authoritative per-user identity honored on every per-user endpoint, so
    shared infrastructure (bots, the worker, slash commands) can act for any
    user without holding that user's raw token.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        token_refresher: Optional[Callable[[], Optional[str]]] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._token_refresher = token_refresher
        self._clients: Dict[asyncio.AbstractEventLoop, httpx.AsyncClient] = {}

    async def close(self) -> None:
        """Close any loop-local HTTP connection pools."""

        clients = list(self._clients.values())
        self._clients.clear()
        for client in clients:
            try:
                await client.aclose()
            except RuntimeError as exc:
                if "Event loop is closed" not in str(exc):
                    raise

    async def aclose(self) -> None:
        """Alias for callers that use httpx-style async close naming."""
        await self.close()

    async def __aenter__(self) -> "NymeriaAPIClient":
        self._client_for_loop()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _client_for_loop(self) -> httpx.AsyncClient:
        """Return the HTTP client bound to the current event loop.

        Prunes entries keyed to loops that have since closed so the map cannot
        grow without bound. A long-lived client reused across many one-shot
        ``asyncio.run(...)`` loops (the CLI REPL, ``run.py`` helpers) would
        otherwise accumulate one ``AsyncClient`` per dead loop, each holding
        open sockets that can never be ``aclose``d from here (their loop is
        gone). The current loop's entry is never pruned; the stale references
        are simply dropped for garbage collection.
        """

        loop = asyncio.get_running_loop()
        for dead in [
            other
            for other in list(self._clients)
            if other is not loop and other.is_closed()
        ]:
            self._clients.pop(dead, None)
        client = self._clients.get(loop)
        if client is None:
            client = httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT)
            self._clients[loop] = client
        return client

    def _headers_for(self, act_as: Optional[str]) -> Dict[str, str]:
        if not act_as:
            return self._headers
        return {**self._headers, "X-Nymeria-Act-As": act_as}

    def _maybe_refresh_token(self, failed_token: str) -> bool:
        """Re-resolve the service token via the injected refresher.

        ``failed_token`` is the token the 401'd attempt actually sent. Returns
        True when the refresher yields a non-empty token that DIFFERS from it,
        meaning a retry has fresh credentials: either the on-disk token rotated
        just now, or a concurrent call on this shared client already refreshed
        ``self.api_key`` (comparing against the failed token, not the current
        one, is what lets that second caller retry too). Returns False when
        there is no refresher, the refresher errors, or the resolved token is
        empty or identical to the failed one; retrying with the same token
        would only hammer the API's auth-failure rate limiter, so the caller
        surfaces the original 401 instead.
        """
        if self._token_refresher is None:
            return False
        try:
            refreshed = self._token_refresher()
        except Exception as exc:
            logger.warning("Service token refresh failed: %s", exc)
            return False
        refreshed = (refreshed or "").strip()
        if not refreshed or refreshed == failed_token:
            return False
        if refreshed != self.api_key:
            logger.info("Service token rotated; retrying with refreshed credentials")
            self.api_key = refreshed
            self._headers = {"Authorization": f"Bearer {refreshed}"}
        return True

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[dict] = None,
        params: Optional[dict] = None,
        act_as: Optional[str] = None,
        timeout: httpx.Timeout = _DEFAULT_TIMEOUT,
        files: Optional[dict] = None,
        raw: bool = False,
    ) -> Any:
        """``files`` sends a multipart body; ``raw`` returns the httpx.Response
        instead of parsed JSON (for binary payloads like TTS audio)."""
        retried = False
        while True:
            attempt_token = self.api_key
            resp = await self._client_for_loop().request(
                method,
                self._url(path),
                headers=self._headers_for(act_as),
                json=json_body,
                params=params,
                files=files,
                timeout=timeout,
            )
            try:
                resp.raise_for_status()
                break
            except httpx.HTTPStatusError as exc:
                # A 401 can mean the api re-minted the shared service token
                # while this long-running process still held the old one.
                if (
                    not retried
                    and exc.response.status_code == 401
                    and self._maybe_refresh_token(attempt_token)
                ):
                    retried = True
                    continue
                raise
        if raw:
            return resp
        if getattr(resp, "status_code", None) == 204:
            return {}
        return resp.json()

    async def _get(self, path: str, params: Optional[dict] = None, act_as: Optional[str] = None) -> Any:
        return await self._request("GET", path, params=params, act_as=act_as)

    async def _post(self, path: str, json: Optional[dict] = None, params: Optional[dict] = None, act_as: Optional[str] = None) -> Any:
        return await self._request(
            "POST",
            path,
            json_body=json,
            params=params,
            act_as=act_as,
            timeout=_CHAT_TIMEOUT,
        )

    async def _put(self, path: str, json: Optional[dict] = None, params: Optional[dict] = None, act_as: Optional[str] = None) -> Any:
        return await self._request(
            "PUT", path, json_body=json, params=params, act_as=act_as
        )

    async def _patch(self, path: str, json: Optional[dict] = None, act_as: Optional[str] = None) -> Any:
        return await self._request("PATCH", path, json_body=json, act_as=act_as)

    async def _delete(self, path: str, params: Optional[dict] = None, act_as: Optional[str] = None) -> Any:
        return await self._request("DELETE", path, params=params, act_as=act_as)

    # ── Chat ──────────────────────────────────────────────────────────────

    async def chat(
        self,
        message: str,
        thread_id: str,
        user_id: str,
        attachments: Optional[List[Dict[str, Any]]] = None,
        force_unsupported_attachments: bool = False,
        trigger_override: Optional[str] = None,
        platform_origin: Optional[Dict[str, Any]] = None,
    ) -> dict:
        """Send a message and get a response (non-streaming).

        ``attachments`` is the list of file dicts accepted by the API
        (``file_type``, ``data_url``, ``mime_type``, ``file_name``); see
        ``triggers.attachment_helpers.build_attachment``.

        ``force_unsupported_attachments`` skips the model-capability
        compatibility check that would otherwise raise an error event.
        Set this from chat-only frontends (Discord/Telegram) where the
        user can't dismiss the desktop's "model may not support" modal.

        ``platform_origin`` carries the originating platform message
        (``{platform, channel_id, message_id, kind}``) so the backend can
        target the ``react`` tool; same shape as :meth:`chat_stream`.

        Returns dict with 'response' (str), 'tool_call_count' (int), and
        'suppress_reply' (bool; when True the caller should not post the
        response text, the user sees only the emoji reaction).
        """
        body: Dict[str, Any] = {
            "message": message,
            "thread_id": thread_id,
            "user_id": user_id,
        }
        if attachments:
            body["attachments"] = attachments
        if force_unsupported_attachments:
            body["force_unsupported_attachments"] = True
        if trigger_override:
            body["trigger_override"] = trigger_override
        if platform_origin:
            body["platform_origin"] = platform_origin
        return await self._post("/chat/sync", json=body, act_as=user_id)

    # ── Workflows ─────────────────────────────────────────────────────────

    async def run_workflow(
        self,
        workflow_id: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        user_id: str,
        thread_id: Optional[str] = None,
    ) -> dict:
        """Execute a published workflow tool as ``user_id`` (headless relay).

        Blocks until the run finishes (bounded by the workflow's wall-clock
        cap). Returns ``{workflow_id, run_id, envelope}``; a refused run
        (missing definition, unapproved revision) raises the HTTP error.
        """
        body: Dict[str, Any] = {"params": params or {}}
        if thread_id:
            body["thread_id"] = thread_id
        return await self._request(
            "POST",
            f"/workflows/{_path_param(workflow_id)}/execute",
            json_body=body,
            act_as=user_id,
            timeout=_WORKFLOW_TIMEOUT,
        )

    # ── Voice ─────────────────────────────────────────────────────────────

    async def transcribe_audio(
        self,
        audio_bytes: bytes,
        filename: str,
        content_type: str,
        user_id: Optional[str] = None,
    ) -> str:
        """Transcribe audio via ``POST /voice/stt``. Returns the transcript.

        Raises ``httpx.HTTPStatusError`` on failure; 503 means the backend has
        no STT provider configured.
        """
        data = await self._request(
            "POST",
            "/voice/stt",
            files={"audio": (filename, audio_bytes, content_type)},
            act_as=user_id,
            timeout=_CHAT_TIMEOUT,
        )
        return (data or {}).get("text", "")

    async def synthesize_speech(
        self,
        text: str,
        voice_note: bool = False,
        user_id: Optional[str] = None,
    ) -> "tuple[bytes, str]":
        """Synthesize speech via ``POST /voice/tts``.

        Returns ``(audio_bytes, content_type)``. ``voice_note=True`` asks for
        a chat-platform voice-message container (Ogg/Opus or MP3); check the
        returned content type rather than assuming. 503 means no TTS provider.
        """
        resp = await self._request(
            "POST",
            "/voice/tts",
            json_body={"text": text, "voice_note": voice_note},
            act_as=user_id,
            timeout=_CHAT_TIMEOUT,
            raw=True,
        )
        return resp.content, resp.headers.get("content-type", "audio/mpeg")

    async def chat_stream(
        self,
        message: str,
        thread_id: str,
        user_id: str,
        is_self_invoke: bool = False,
        trigger_override: Optional[str] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
        force_unsupported_attachments: bool = False,
        source: Optional[str] = None,
        source_id: Optional[str] = None,
        source_label: Optional[str] = None,
        publish_autonomous_events: Optional[bool] = None,
        trigger_id: Optional[str] = None,
        trigger_name: Optional[str] = None,
        platform_origin: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Stream chat events via SSE (POST /chat).

        Yields dicts with 'type' key: thinking, response, tool_call,
        tool_result, error, done, etc.

        For autonomous/trusted callers, set
        is_self_invoke=True so the API treats the nudge as an internal
        message and routes it through the autonomous prompt path.
        trigger_override supplies the label (e.g. 'watchdog').

        ``attachments`` carries multimodal file payloads; same shape as
        :meth:`chat`.

        ``platform_origin`` carries the originating platform message
        (``{platform, channel_id, message_id, kind}``), set by chat-platform
        bots on every dispatched turn so the ``react`` tool can post an emoji
        reaction back to it; ``kind == "reaction"`` marks a reaction-triggered
        turn (the backend appends the react-tool guidance block while the
        tool is unbound).

        ``publish_autonomous_events`` lets the Docker worker tell the API
        to suppress its own autonomous SSE mirroring (task_started,
        chunk fan-out, task_completed, notifications) for this call —
        the worker is the sole publisher with stable task IDs. Pass
        False from worker-owned ticker/trigger relays; leave None to
        accept the API default (True).

        Autonomous self-invoke calls switch to a no-read-timeout pool so
        a queued prompt waiting on a busy thread can drain whenever the
        running turn halts at its next sub-turn boundary, even if that
        takes longer than the regular five-minute window.
        """
        body: Dict[str, Any] = {
            "message": message,
            "thread_id": thread_id,
            "user_id": user_id,
        }
        if is_self_invoke:
            body["is_self_invoke"] = True
        if trigger_override:
            body["trigger_override"] = trigger_override
        if attachments:
            body["attachments"] = attachments
        if force_unsupported_attachments:
            body["force_unsupported_attachments"] = True
        if source:
            body["source"] = source
        if source_id:
            body["source_id"] = source_id
        if source_label:
            body["source_label"] = source_label
        if trigger_id:
            body["trigger_id"] = trigger_id
        if trigger_name:
            body["trigger_name"] = trigger_name
        if platform_origin:
            body["platform_origin"] = platform_origin
        if publish_autonomous_events is not None:
            body["publish_autonomous_events"] = publish_autonomous_events
        stream_timeout = _SSE_TIMEOUT if is_self_invoke else _CHAT_TIMEOUT
        yielded = False
        retried = False
        while True:
            attempt_token = self.api_key
            try:
                async with self._client_for_loop().stream(
                    "POST",
                    self._url("/chat"),
                    headers=self._headers_for(user_id),
                    json=body,
                    timeout=stream_timeout,
                ) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        chunk = parse_sse_data_line(line)
                        if chunk is None:
                            continue
                        yielded = True
                        yield chunk
                return
            except httpx.HTTPStatusError as exc:
                # The 401 from a re-minted service token fires at stream open,
                # before any chunk is yielded; never retry after output started.
                if (
                    not retried
                    and not yielded
                    and exc.response.status_code == 401
                    and self._maybe_refresh_token(attempt_token)
                ):
                    retried = True
                    continue
                raise

    async def reattach_turn_stream(
        self,
        thread_id: str,
        user_id: Optional[str] = None,
        *,
        turn_id: Optional[str] = None,
        from_seq: int = 0,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Re-attach to a thread's buffered interactive turn via SSE.

        GET /threads/{id}/turn/stream: replays the holder turn's buffered
        events with ``seq`` greater than ``from_seq`` (byte-identical to the
        original POST /chat stream), then tails live events until the turn
        ends. Raises ``httpx.HTTPStatusError`` with status 404
        (``turn_not_found``) or 410 (``turn_replay_gap``) when the turn is
        not attachable; callers fall back to history reconciliation.
        """
        params: Dict[str, Any] = {"from_seq": from_seq}
        if turn_id:
            params["turn_id"] = turn_id
        async with self._client_for_loop().stream(
            "GET",
            self._url(f"/threads/{_path_param(thread_id)}/turn/stream"),
            headers={
                **self._headers_for(user_id),
                "Accept": "text/event-stream",
            },
            params=params,
            timeout=_SSE_TIMEOUT,
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                chunk = parse_sse_data_line(line)
                if chunk is None:
                    continue
                yield chunk

    async def get_thread_status(
        self, thread_id: str, user_id: Optional[str] = None
    ) -> dict:
        """Lightweight thread status (revision, processing, attachable turn)."""
        return await self._get(
            f"/threads/{_path_param(thread_id)}/status", act_as=user_id
        )

    async def autonomous_stream(
        self,
        user_id: str = "default",
        *,
        client_id: Optional[str] = None,
        act_as: Optional[str] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Stream autonomous task events via SSE (GET /autonomous/stream)."""

        params = _clean_params(user_id=user_id, client_id=client_id)
        headers = {
            **self._headers_for(act_as),
            "Accept": "text/event-stream",
        }
        async with self._client_for_loop().stream(
            "GET",
            self._url("/autonomous/stream"),
            headers=headers,
            params=params,
            timeout=_SSE_TIMEOUT,
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                chunk = parse_sse_data_line(line)
                if chunk is None:
                    continue
                yield chunk

    # ── Thread Management ─────────────────────────────────────────────────

    async def stop(self, thread_id: str, user_id: Optional[str] = None) -> dict:
        """Abort the current operation on a thread."""
        return await self._post(f"/threads/{thread_id}/stop", act_as=user_id)

    async def compact(
        self, thread_id: str, user_id: str, priority: Optional[str] = None
    ) -> dict:
        """Compact conversation context.

        Optional ``priority`` is a free-text focus instruction that steers what the
        summary emphasizes (it never drops other required content).
        """
        params = {"user_id": user_id}
        if priority:
            params["priority"] = priority
        return await self._post(
            f"/threads/{thread_id}/compact", params=params, act_as=user_id,
        )

    async def prune(self, thread_id: str, user_id: str, mode: str = "full") -> dict:
        """Deterministically compress tool returns in a thread (no LLM)."""
        return await self._post(
            f"/threads/{thread_id}/prune",
            params={"user_id": user_id, "mode": mode},
            act_as=user_id,
        )

    async def delete_thread(self, thread_id: str, user_id: Optional[str] = None) -> dict:
        """Delete all history for a thread."""
        return await self._delete(f"/threads/{thread_id}", act_as=user_id)

    async def list_threads(self, user_id: str) -> List[dict]:
        """List threads visible to a user."""
        data = await self._get("/threads", act_as=user_id)
        return data.get("threads", [])

    async def claim_thread(
        self,
        thread_id: str,
        user_id: str,
        *,
        title: Optional[str] = None,
        platform: Optional[str] = None,
    ) -> dict:
        """Eagerly claim a new user-owned thread, optionally with metadata."""
        body = _clean_params(title=title, platform=platform) or None
        return await self._post(
            f"/threads/{thread_id}/claim",
            json=body,
            act_as=user_id,
        )

    async def update_thread_metadata(
        self,
        thread_id: str,
        user_id: str,
        *,
        title: Optional[str] = None,
        pinned: Optional[bool] = None,
    ) -> dict:
        """Update a thread's title and/or pinned state."""
        body: Dict[str, Any] = {}
        if title is not None:
            body["title"] = title
        if pinned is not None:
            body["pinned"] = pinned
        return await self._patch(
            f"/threads/{thread_id}/metadata", json=body, act_as=user_id
        )

    async def clear_thread(self, thread_id: str, user_id: str = "default") -> dict:
        """Clear conversation history only (preserve notepad + config)."""
        return await self._post(
            f"/threads/{thread_id}/clear", params={"user_id": user_id}, act_as=user_id,
        )

    async def rewind_thread(self, thread_id: str, steps: int = 1, user_id: Optional[str] = None) -> dict:
        """Remove the last N user+assistant exchanges from thread state.

        Uses RemoveMessage + update_state on the backend (same mechanism as
        context trimming).
        """
        return await self._post(
            f"/threads/{thread_id}/rewind",
            json={"steps": steps},
            act_as=user_id,
        )

    async def branch_thread(
        self,
        thread_id: str,
        *,
        title: Optional[str] = None,
        from_message_index: Optional[int] = None,
        user_id: Optional[str] = None,
    ) -> dict:
        """Create a new thread from a source thread's checkpoints and config."""
        body: Dict[str, Any] = {}
        if title is not None:
            body["title"] = title
        if from_message_index is not None:
            body["from_message_index"] = from_message_index
        return await self._post(
            f"/threads/{_path_param(thread_id)}/branch",
            json=body,
            act_as=user_id,
        )

    async def get_history(
        self,
        thread_id: str,
        include_internal: bool = False,
        user_id: Optional[str] = None,
    ) -> dict:
        """Get conversation history for a thread.

        ``user_id`` becomes ``X-Nymeria-Act-As`` so the backend's per-thread
        access check runs against the human user, not the bot service token —
        otherwise bot-service first-touch would claim the thread and lock the
        real owner out.
        """
        return await self._get(
            f"/threads/{thread_id}/history",
            params={"include_internal": str(include_internal).lower()},
            act_as=user_id,
        )

    async def get_context_stats(self, thread_id: str, user_id: Optional[str] = None) -> dict:
        """Get token usage and context stats for a thread."""
        return await self._get(f"/threads/{_path_param(thread_id)}/context", act_as=user_id)

    async def get_thread_overview(
        self, thread_id: str, user_id: Optional[str] = None
    ) -> Optional[dict]:
        """Get resolved thread status/config summary for headers and dashboards."""
        try:
            return await self._get(
                f"/threads/{_path_param(thread_id)}/overview",
                act_as=user_id,
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    async def get_thread_config(
        self, thread_id: str, user_id: Optional[str] = None
    ) -> Optional[dict]:
        """Get per-thread configuration (tools, instructions, etc.)."""
        try:
            return await self._get(f"/threads/{_path_param(thread_id)}/config", act_as=user_id)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    async def delete_thread_config(
        self, thread_id: str, user_id: Optional[str] = None
    ) -> dict:
        """Reset a thread to global defaults by deleting its saved config."""
        return await self._delete(
            f"/threads/{_path_param(thread_id)}/config",
            act_as=user_id,
        )

    async def get_agent_templates(self, user_id: Optional[str] = None) -> dict:
        """List callable-thread templates."""
        return await self._get("/agents/templates", act_as=user_id)

    async def list_agent_threads(self, user_id: Optional[str] = None) -> List[dict]:
        """List callable threads visible to the authenticated user."""
        data = await self._get("/agents/threads", act_as=user_id)
        return data.get("threads", [])

    async def create_agent_thread(
        self,
        request: Dict[str, Any],
        user_id: Optional[str] = None,
    ) -> dict:
        """Create a callable agent thread."""
        return await self._post("/agents/threads", json=request, act_as=user_id)

    async def list_thread_teams(self, user_id: Optional[str] = None) -> List[dict]:
        """List callable visibility teams for a user's threads."""
        data = await self._get("/thread-teams", act_as=user_id)
        if isinstance(data, dict):
            return data.get("teams", [])
        return data

    async def create_thread_team(
        self,
        name: str,
        thread_ids: List[str],
        user_id: Optional[str] = None,
    ) -> dict:
        """Create a callable thread team."""
        return await self._post(
            "/thread-teams",
            json={"name": name, "thread_ids": thread_ids},
            act_as=user_id,
        )

    async def update_thread_team(
        self,
        team_id: str,
        *,
        name: Optional[str] = None,
        thread_ids: Optional[List[str]] = None,
        user_id: Optional[str] = None,
    ) -> dict:
        """Update a callable thread team's name and/or membership."""
        body = _clean_params(name=name, thread_ids=thread_ids) or {}
        return await self._patch(
            f"/thread-teams/{_path_param(team_id)}",
            json=body,
            act_as=user_id,
        )

    async def delete_thread_team(
        self, team_id: str, user_id: Optional[str] = None
    ) -> dict:
        """Delete a callable thread team."""
        return await self._delete(f"/thread-teams/{_path_param(team_id)}", act_as=user_id)

    async def export_thread(
        self, thread_id: str, user_id: Optional[str] = None
    ) -> dict:
        """Export a share document for a thread."""
        return await self._get(f"/threads/{_path_param(thread_id)}/export", act_as=user_id)

    async def import_thread(
        self, document: Dict[str, Any], user_id: Optional[str] = None
    ) -> dict:
        """Import a thread share document."""
        return await self._post("/threads/import", json=document, act_as=user_id)

    # ── Accounts ──────────────────────────────────────────────────────────

    async def update_me(
        self,
        display_name: str,
        user_id: Optional[str] = None,
    ) -> dict:
        """Update the authenticated user's display name."""
        return await self._patch(
            "/me",
            json={"display_name": display_name},
            act_as=user_id,
        )

    async def list_my_tokens(self, user_id: Optional[str] = None) -> List[dict]:
        """List API tokens owned by the authenticated user."""
        data = await self._get("/me/tokens", act_as=user_id)
        return data if isinstance(data, list) else data.get("tokens", [])

    async def issue_my_token(
        self,
        label: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> dict:
        """Issue a new API token for the authenticated user."""
        return await self._post("/me/tokens", json={"label": label}, act_as=user_id)

    async def revoke_my_token(
        self,
        token_hash_prefix: str,
        user_id: Optional[str] = None,
    ) -> dict:
        """Revoke one of the authenticated user's API tokens."""
        return await self._delete(
            f"/me/tokens/{_path_param(token_hash_prefix)}",
            act_as=user_id,
        )

    async def list_admin_users(self) -> List[dict]:
        """List all user accounts. Admin-only."""
        data = await self._get("/admin/users")
        return data if isinstance(data, list) else data.get("users", [])

    async def create_admin_user(self, request: Dict[str, Any]) -> dict:
        """Create a user account and issue an initial token. Admin-only."""
        return await self._post("/admin/users", json=request)

    async def get_admin_user(self, user_id: str) -> dict:
        """Get one user account with admin details."""
        return await self._get(f"/admin/users/{_path_param(user_id)}")

    async def update_admin_user(self, user_id: str, patch: Dict[str, Any]) -> dict:
        """Update an account's profile, role, or disabled state. Admin-only."""
        return await self._patch(f"/admin/users/{_path_param(user_id)}", json=patch)

    async def delete_admin_user(self, user_id: str) -> dict:
        """Delete a user account. Admin-only."""
        return await self._delete(f"/admin/users/{_path_param(user_id)}")

    async def list_user_tokens(self, user_id: str) -> List[dict]:
        """List a user's tokens. Admin-only."""
        data = await self._get(f"/admin/users/{_path_param(user_id)}/tokens")
        return data if isinstance(data, list) else data.get("tokens", [])

    async def issue_user_token(
        self, user_id: str, label: Optional[str] = None
    ) -> dict:
        """Issue a token for another user. Admin-only."""
        return await self._post(
            f"/admin/users/{_path_param(user_id)}/tokens",
            json={"label": label},
        )

    async def rotate_user_tokens(
        self, user_id: str, label: Optional[str] = None
    ) -> dict:
        """Revoke all active tokens for a user and issue a replacement."""
        return await self._post(
            f"/admin/users/{_path_param(user_id)}/tokens/rotate",
            json={"label": label},
        )

    async def revoke_user_token(self, user_id: str, token_hash_prefix: str) -> dict:
        """Revoke a user's token by hash prefix. Admin-only."""
        return await self._delete(
            f"/admin/users/{_path_param(user_id)}/tokens/{_path_param(token_hash_prefix)}"
        )

    async def list_user_platforms(self, user_id: str) -> List[dict]:
        """List linked platform identities for a user. Admin-only."""
        data = await self._get(f"/admin/users/{_path_param(user_id)}/platforms")
        return data if isinstance(data, list) else data.get("platforms", [])

    async def link_user_platform(
        self,
        user_id: str,
        provider: str,
        provider_user_id: str,
    ) -> dict:
        """Link a platform identity to a user. Admin-only."""
        return await self._post(
            f"/admin/users/{_path_param(user_id)}/platforms",
            json={"provider": provider, "provider_user_id": provider_user_id},
        )

    async def unlink_user_platform(
        self,
        user_id: str,
        provider: str,
        provider_user_id: str,
    ) -> dict:
        """Unlink a platform identity from a user. Admin-only."""
        return await self._delete(
            "/admin/users/"
            f"{_path_param(user_id)}/platforms/"
            f"{_path_param(provider)}/{_path_param(provider_user_id)}"
        )

    async def list_my_platforms(self, user_id: Optional[str] = None) -> List[dict]:
        """List the authenticated user's linked platform identities."""
        data = await self._get("/me/platforms", act_as=user_id)
        return data if isinstance(data, list) else data.get("platforms", [])

    async def request_self_platform_link_code(
        self,
        provider: str,
        user_id: Optional[str] = None,
    ) -> dict:
        """Issue a self-service platform-link code."""
        return await self._post(
            "/me/platform-link-codes",
            json={"provider": provider},
            act_as=user_id,
        )

    async def issue_chatapp_bind_code(
        self,
        thread_id: str,
        provider: str,
        user_id: Optional[str] = None,
    ) -> dict:
        """Issue a chat-app bind code for a thread."""
        return await self._post(
            f"/threads/{_path_param(thread_id)}/chatapp/bind-code",
            json={"provider": provider},
            act_as=user_id,
        )

    async def list_thread_bindings(
        self,
        thread_id: str,
        user_id: Optional[str] = None,
    ) -> List[dict]:
        """List chat-app bindings for a thread."""
        data = await self._get(
            f"/threads/{_path_param(thread_id)}/chatapp/bindings",
            act_as=user_id,
        )
        return data if isinstance(data, list) else data.get("bindings", [])

    async def unbind_thread_chatapp(
        self,
        thread_id: str,
        binding_id: int,
        user_id: Optional[str] = None,
    ) -> dict:
        """Remove a chat-app binding from a thread."""
        return await self._delete(
            f"/threads/{_path_param(thread_id)}/chatapp/bindings/{binding_id}",
            act_as=user_id,
        )

    async def list_my_telegram_bots(
        self, user_id: Optional[str] = None
    ) -> List[dict]:
        """List Telegram bots registered by the authenticated user."""
        data = await self._get("/me/telegram-bots", act_as=user_id)
        return data if isinstance(data, list) else data.get("bots", [])

    async def get_my_telegram_bot(
        self, bot_id: int, user_id: Optional[str] = None
    ) -> dict:
        """Get one registered Telegram bot."""
        return await self._get(f"/me/telegram-bots/{bot_id}", act_as=user_id)

    async def register_my_telegram_bot(
        self, bot_token: str, user_id: Optional[str] = None
    ) -> dict:
        """Register a user-owned Telegram bot."""
        return await self._post(
            "/me/telegram-bots",
            json={"bot_token": bot_token},
            act_as=user_id,
        )

    async def remove_my_telegram_bot(
        self, bot_id: int, user_id: Optional[str] = None
    ) -> dict:
        """Remove a user-owned Telegram bot."""
        return await self._delete(f"/me/telegram-bots/{bot_id}", act_as=user_id)

    # ── Commands ─────────────────────────────────────────────────────────

    async def execute_command(
        self,
        command: str,
        *,
        thread_id: Optional[str] = None,
        source: str = "user",
        actor: Optional[str] = None,
        surface: Optional[str] = None,
        user_id: Optional[str] = None,
        supports_forms: bool = False,
    ) -> dict:
        """Execute a backend slash command and return markdown output.

        ``supports_forms`` declares that THIS caller renders declarative form
        payloads (``data.form``); only form-rendering clients (the Rich CLI)
        pass True. Without it the backend strips form payloads and never
        answers a bare command with a generated picker.
        """
        payload: dict[str, Any] = {
            "command": command,
            "thread_id": thread_id,
            "source": source,
        }
        if actor is not None:
            payload["actor"] = actor
        if surface is not None:
            payload["surface"] = surface
        if supports_forms:
            payload["supports_forms"] = True
        return await self._post(
            "/commands/execute",
            json=payload,
            act_as=user_id,
        )

    async def list_command_options(
        self,
        ref: str,
        *,
        thread_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> List[dict]:
        """Resolve a ``choices_ref`` option set live (``GET
        /commands/options/{ref}``), scoped to the acting user. Raises on
        HTTP errors like every other method here; autocomplete callers
        degrade to no suggestions themselves."""
        data = await self._get(
            f"/commands/options/{ref}",
            params={"thread_id": thread_id} if thread_id else None,
            act_as=user_id,
        )
        return data if isinstance(data, list) else []

    async def list_commands(
        self,
        *,
        source: Optional[str] = None,
        actor: Optional[str] = None,
        surface: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> List[dict]:
        """List registered slash commands for a caller source."""
        params = {}
        if source is not None:
            params["source"] = source
        if actor is not None:
            params["actor"] = actor
        if surface is not None:
            params["surface"] = surface
        data = await self._get(
            "/commands",
            params=params or None,
            act_as=user_id,
        )
        return data if isinstance(data, list) else data.get("commands", [])

    # ── Hook approvals ────────────────────────────────────────────────────

    async def get_hook_approvals(self, user_id: Optional[str] = None) -> List[dict]:
        """List pending require_approval hook holds visible to the caller."""
        data = await self._get("/hooks/approvals", act_as=user_id)
        return data if isinstance(data, list) else data.get("approvals", [])

    async def resolve_hook_approval(
        self,
        record_id: str,
        approved: bool,
        *,
        note: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> dict:
        """Approve or deny a pending hook-approval hold.

        404 = unknown or not visible to the caller; 409 = no longer pending
        (already resolved, timed out, or the waiting turn died).
        """
        payload: Dict[str, Any] = {"approved": approved}
        if note:
            payload["note"] = note
        return await self._post(
            f"/hooks/approvals/{_path_param(record_id)}/resolve",
            json=payload,
            act_as=user_id,
        )

    # ── LLM fallback consent ──────────────────────────────────────────────

    async def get_fallback_approvals(self, user_id: Optional[str] = None) -> List[dict]:
        """List parked model-swap consent prompts visible to the caller."""
        data = await self._get("/llm/fallback-approvals", act_as=user_id)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            approvals = data.get("approvals", [])
            return approvals if isinstance(approvals, list) else []
        return []

    async def resolve_fallback_approval(
        self,
        record_id: str,
        approved: bool,
        *,
        hold_seconds: Optional[int] = None,
        hold_permanent: bool = False,
        note: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> dict:
        """Approve or decline a parked model swap (fallback consent).

        On approve, ``hold_seconds`` picks the hold duration and
        ``hold_permanent`` pins it until manually reverted (wins over
        ``hold_seconds``); both omitted = the record's default hold.
        404 = unknown or not visible to the caller; 409 = no longer pending
        (already resolved, timed out, or the parked turn died).
        """
        payload: Dict[str, Any] = {"approved": approved}
        if hold_seconds is not None:
            payload["hold_seconds"] = int(hold_seconds)
        if hold_permanent:
            payload["hold_permanent"] = True
        if note:
            payload["note"] = note
        return await self._post(
            f"/llm/fallback-approvals/{_path_param(record_id)}/resolve",
            json=payload,
            act_as=user_id,
        )

    # ── CLI config commands ───────────────────────────────────────────────

    async def post_cli_config_result(
        self,
        command_id: str,
        result: Dict[str, Any],
        user_id: Optional[str] = None,
    ) -> dict:
        """Ack a ``cli_config`` autonomous event (first ack wins server-side).

        ``result`` carries ``ok``, ``status`` ("success" | "error"), and
        optional ``data``/``error`` fields.
        """
        return await self._post(
            f"/cli-config/{_path_param(command_id)}/result",
            json=result,
            act_as=user_id,
        )

    # ── Settings ──────────────────────────────────────────────────────────

    async def get_settings(self, user_id: Optional[str] = None) -> dict:
        """Get server settings."""
        return await self._get("/settings", act_as=user_id)

    async def test_llm_provider_config(
        self,
        request: Dict[str, Any],
        user_id: Optional[str] = None,
    ) -> dict:
        """Test an LLM provider configuration without saving it."""
        return await self._post("/settings/llm/test", json=request, act_as=user_id)

    async def get_llm_runtime_diagnostics(
        self, user_id: Optional[str] = None
    ) -> dict:
        """Get runtime LLM/provider diagnostics."""
        return await self._get("/settings/llm/runtime", act_as=user_id)

    # ── Tools ─────────────────────────────────────────────────────────────

    async def get_tools(self) -> List[dict]:
        """List all visible tool definitions."""
        data = await self._get("/tools")
        return data.get("tools", [])

    async def get_optional_tools(self, user_id: str = "default") -> List[dict]:
        """List tools available for per-thread enabling."""
        data = await self._get("/tools/optional", act_as=user_id)
        return data.get("tools", [])

    async def get_default_tools(self, user_id: str = "default") -> dict:
        """Get default tool set for a user.

        Returns dict with 'default_tools' (list of names) and
        'available_tools' (list of tool info dicts).
        """
        return await self._get("/tools/defaults", params={"user_id": user_id}, act_as=user_id)

    async def set_default_tools(
        self, tool_names: List[str], user_id: str = "default"
    ) -> dict:
        """Replace the user's default tool set for new threads."""
        return await self._put(
            "/tools/defaults",
            json={"tool_names": tool_names},
            params={"user_id": user_id},
            act_as=user_id,
        )

    async def reset_default_tools(self, user_id: str = "default") -> dict:
        """Reset the user's default tool set to core defaults."""
        return await self._delete(
            "/tools/defaults",
            params={"user_id": user_id},
            act_as=user_id,
        )

    async def list_all_tools(self, user_id: str = "default") -> List[dict]:
        """List all tools (built-in + optional + MCP) with enabled state."""
        data = await self._get(f"/users/{user_id}/tools", act_as=user_id)
        return data.get("tools", [])

    async def search_tools(
        self,
        query: str,
        *,
        user_id: str = "default",
        category: Optional[str] = None,
        thread_id: Optional[str] = None,
        top_k: int = 15,
        include_status: bool = True,
    ) -> dict:
        """Search visible tools with backend ranking."""
        return await self._get(
            f"/users/{_path_param(user_id)}/tools/search",
            params=_clean_params(
                query=query,
                category=category,
                thread_id=thread_id,
                top_k=top_k,
                include_status=str(include_status).lower(),
            ),
            act_as=user_id,
        )

    async def get_tool_preferences(self, user_id: str = "default") -> dict:
        """Get user tool preferences."""
        return await self._get(
            f"/users/{_path_param(user_id)}/tools/preferences",
            act_as=user_id,
        )

    async def set_tool_config(
        self,
        user_id: str,
        tool_name: str,
        config: Dict[str, Any],
    ) -> dict:
        """Set a user-scoped tool configuration."""
        return await self._put(
            f"/users/{_path_param(user_id)}/tools/{_path_param(tool_name)}/config",
            json={"config": config},
            act_as=user_id,
        )

    async def reset_tool_preferences(self, user_id: str = "default") -> dict:
        """Reset all user tool preferences."""
        return await self._post(
            f"/users/{_path_param(user_id)}/tools/reset",
            act_as=user_id,
        )

    async def get_unified_tools(self, user_id: str = "default") -> dict:
        """List built-in, MCP, and custom tools in the unified shape."""
        return await self._get(
            f"/users/{_path_param(user_id)}/tools/unified",
            act_as=user_id,
        )

    async def set_unified_tool_enabled(
        self,
        tool_id: str,
        enabled: bool,
        user_id: str = "default",
    ) -> dict:
        """Enable or disable a unified tool for a user."""
        return await self._put(
            f"/users/{_path_param(user_id)}/tools/unified/"
            f"{_path_param(tool_id)}/enable",
            json={"enabled": enabled},
            act_as=user_id,
        )

    async def set_unified_tool_description(
        self,
        tool_id: str,
        description: Optional[str],
        user_id: str = "default",
    ) -> dict:
        """Set or clear a user custom description for a unified tool."""
        return await self._put(
            f"/users/{_path_param(user_id)}/tools/unified/"
            f"{_path_param(tool_id)}/description",
            json={"description": description},
            act_as=user_id,
        )

    async def set_unified_tool_config(
        self,
        tool_id: str,
        config: Dict[str, Any],
        user_id: str = "default",
    ) -> dict:
        """Set or clear user config for a unified tool."""
        return await self._put(
            f"/users/{_path_param(user_id)}/tools/unified/"
            f"{_path_param(tool_id)}/config",
            json={"config": config},
            act_as=user_id,
        )

    async def create_unified_tool(self, request: Dict[str, Any]) -> dict:
        """Create a custom tool through the unified API. Admin-only."""
        return await self._post("/tools/unified", json=request)

    async def update_unified_tool(
        self, tool_id: str, request: Dict[str, Any]
    ) -> dict:
        """Update a custom tool through the unified API. Admin-only."""
        return await self._put(f"/tools/unified/{_path_param(tool_id)}", json=request)

    async def delete_unified_tool(self, tool_id: str) -> dict:
        """Delete a custom tool through the unified API. Admin-only."""
        return await self._delete(f"/tools/unified/{_path_param(tool_id)}")

    async def list_custom_tools(self) -> dict:
        """List custom tools. Admin-only."""
        return await self._get("/tools/custom")

    async def create_custom_tool(self, request: Dict[str, Any]) -> dict:
        """Create a custom tool. Admin-only."""
        return await self._post("/tools/custom", json=request)

    async def get_custom_tool(self, tool_id: str) -> dict:
        """Get one custom tool. Admin-only."""
        return await self._get(f"/tools/custom/{_path_param(tool_id)}")

    async def update_custom_tool(
        self, tool_id: str, request: Dict[str, Any]
    ) -> dict:
        """Update a custom tool. Admin-only."""
        return await self._put(f"/tools/custom/{_path_param(tool_id)}", json=request)

    async def delete_custom_tool(self, tool_id: str) -> dict:
        """Delete a custom tool. Admin-only."""
        return await self._delete(f"/tools/custom/{_path_param(tool_id)}")

    async def test_custom_tool(
        self, tool_id: str, params: Dict[str, Any]
    ) -> dict:
        """Test a custom tool with sample parameters. Admin-only."""
        return await self._post(
            f"/tools/custom/{_path_param(tool_id)}/test",
            json={"params": params},
        )

    async def export_custom_tools(self) -> dict:
        """Export all custom tools. Admin-only."""
        return await self._get("/tools/custom/export")

    async def import_custom_tools(self, tools: List[Dict[str, Any]]) -> dict:
        """Import custom tool definitions. Admin-only."""
        return await self._post("/tools/custom/import", json={"tools": tools})

    async def get_tool_categories(self) -> dict:
        """Get tool categories summary."""
        return await self._get("/tools/categories")

    # ── Skills ────────────────────────────────────────────────────────────

    async def list_skills(
        self,
        user_id: str = "default",
        scope: Optional[str] = None,
    ) -> List[dict]:
        """List installed skills visible to a user."""
        data = await self._get(
            "/skills",
            params=_clean_params(user_id=user_id, scope=scope),
            act_as=user_id,
        )
        return data.get("skills", [])

    async def get_skill(self, name: str, user_id: str = "default") -> dict:
        """Get full metadata and body for an installed skill."""
        return await self._get(
            f"/skills/{_path_param(name)}",
            params={"user_id": user_id},
            act_as=user_id,
        )

    async def install_skill(
        self,
        request: Dict[str, Any],
        user_id: str = "default",
    ) -> dict:
        """Install a skill from a marketplace."""
        data = await self._post(
            "/skills/install",
            json=request,
            params={"user_id": user_id},
            act_as=user_id,
        )
        return data.get("skill", data)

    async def uninstall_skill(
        self,
        name: str,
        scope: str = "user",
        user_id: str = "default",
    ) -> dict:
        """Uninstall a user or global skill."""
        return await self._delete(
            f"/skills/{_path_param(name)}",
            params={"scope": scope, "user_id": user_id},
            act_as=user_id,
        )

    async def search_skills_marketplace(
        self,
        source: str = "anthropic",
        query: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> List[dict]:
        """Search a configured skills marketplace."""
        data = await self._get(
            "/skills/marketplace/search",
            params=_clean_params(source=source, q=query),
            act_as=user_id,
        )
        return data.get("results", [])

    async def get_thread_active_skills(
        self,
        thread_id: str,
        user_id: str = "default",
    ) -> dict:
        """Get the resolved active skill set for a thread."""
        return await self._get(
            f"/threads/{_path_param(thread_id)}/skills",
            params={"user_id": user_id},
            act_as=user_id,
        )

    async def get_thread_callable_tools(
        self,
        thread_id: str,
        user_id: str = "default",
    ) -> dict:
        """Get callable threads available as tools to a thread."""
        return await self._get(
            f"/threads/{_path_param(thread_id)}/callable-tools",
            params={"user_id": user_id},
            act_as=user_id,
        )

    async def get_global_skills(self, user_id: str = "default") -> List[str]:
        """Get skills enabled by default for a user."""
        data = await self._get(
            "/settings/global-skills",
            params={"user_id": user_id},
            act_as=user_id,
        )
        return data.get("enabled_global_skills", [])

    async def set_global_skills(
        self,
        skill_names: List[str],
        user_id: str = "default",
    ) -> List[str]:
        """Replace skills enabled by default for a user."""
        data = await self._put(
            "/settings/global-skills",
            json={"skill_names": skill_names},
            params={"user_id": user_id},
            act_as=user_id,
        )
        return data.get("enabled_global_skills", [])

    # ── MCP Servers ───────────────────────────────────────────────────────

    async def list_mcp_servers(self) -> dict:
        """List MCP server definitions. Admin-only."""
        return await self._get("/mcp-servers")

    async def get_mcp_server(self, server_id: str) -> dict:
        """Get one MCP server definition. Admin-only."""
        return await self._get(f"/mcp-servers/{_path_param(server_id)}")

    async def create_mcp_server(
        self,
        request: Dict[str, Any],
        *,
        thread_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> dict:
        """Create an MCP server and optionally enable it for a thread."""
        return await self._post(
            "/mcp-servers",
            json=request,
            params=_clean_params(thread_id=thread_id),
            act_as=user_id,
        )

    async def update_mcp_server(
        self, server_id: str, request: Dict[str, Any]
    ) -> dict:
        """Update an MCP server definition. Admin-only."""
        return await self._put(
            f"/mcp-servers/{_path_param(server_id)}",
            json=request,
        )

    async def delete_mcp_server(self, server_id: str) -> dict:
        """Delete an MCP server definition. Admin-only."""
        return await self._delete(f"/mcp-servers/{_path_param(server_id)}")

    async def discover_mcp_server_tools(self, server_id: str) -> dict:
        """Force MCP tool discovery for a server. Admin-only."""
        return await self._post(f"/mcp-servers/{_path_param(server_id)}/discover")

    async def test_mcp_server(self, server_id: str) -> dict:
        """Test MCP server connectivity. Admin-only."""
        return await self._post(f"/mcp-servers/{_path_param(server_id)}/test")

    async def preview_mcp_server_install(self, request: Dict[str, Any]) -> dict:
        """Preview an MCP install source without executing it."""
        return await self._post("/mcp-servers/install/preview", json=request)

    async def install_mcp_server(self, request: Dict[str, Any]) -> dict:
        """Install an MCP server from a source string or preview token."""
        return await self._post("/mcp-servers/install", json=request)

    async def retry_mcp_server_install(
        self,
        server_id: str,
        request: Optional[Dict[str, Any]] = None,
    ) -> dict:
        """Retry setup/discovery for a draft or failed MCP server."""
        return await self._post(
            f"/mcp-servers/{_path_param(server_id)}/retry",
            json=request or {},
        )

    # ── Triggers ──────────────────────────────────────────────────────────

    async def list_triggers(
        self,
        user_id: str = "default",
        *,
        enabled_only: bool = False,
        thread_id: Optional[str] = None,
    ) -> List[dict]:
        """List event triggers for a user."""
        data = await self._get(
            "/triggers",
            params=_clean_params(
                user_id=user_id,
                enabled_only=enabled_only,
                thread_id=thread_id,
            ),
            act_as=user_id,
        )
        return data if isinstance(data, list) else data.get("triggers", [])

    async def create_trigger(
        self,
        request: Dict[str, Any],
        user_id: str = "default",
    ) -> dict:
        """Create an event trigger."""
        return await self._post(
            "/triggers",
            json=request,
            params={"user_id": user_id},
            act_as=user_id,
        )

    async def get_trigger(self, trigger_id: str, user_id: str = "default") -> dict:
        """Get one event trigger."""
        return await self._get(
            f"/triggers/{_path_param(trigger_id)}",
            params={"user_id": user_id},
            act_as=user_id,
        )

    async def update_trigger(
        self,
        trigger_id: str,
        request: Dict[str, Any],
        user_id: str = "default",
    ) -> dict:
        """Update an event trigger."""
        return await self._patch(
            f"/triggers/{_path_param(trigger_id)}",
            json=request,
            act_as=user_id,
        )

    async def delete_trigger(
        self, trigger_id: str, user_id: str = "default"
    ) -> dict:
        """Delete an event trigger."""
        return await self._delete(
            f"/triggers/{_path_param(trigger_id)}",
            params={"user_id": user_id},
            act_as=user_id,
        )

    async def get_trigger_sources(self) -> dict:
        """List available trigger source types."""
        data = await self._get("/triggers/sources/list")
        return data.get("sources", data)

    async def reload_trigger_sources(self) -> dict:
        """Reload trigger source plugins."""
        return await self._post("/triggers/sources/reload")

    async def test_trigger(
        self, trigger_id: str, user_id: str = "default"
    ) -> dict:
        """Dry-run a trigger against its sample event."""
        return await self._post(
            f"/triggers/{_path_param(trigger_id)}/test",
            params={"user_id": user_id},
            act_as=user_id,
        )

    async def get_trigger_executions(
        self,
        trigger_id: str,
        user_id: str = "default",
        limit: int = 50,
    ) -> List[dict]:
        """Get execution history for one trigger."""
        data = await self._get(
            f"/triggers/{_path_param(trigger_id)}/executions",
            params={"user_id": user_id, "limit": limit},
            act_as=user_id,
        )
        return data if isinstance(data, list) else data.get("executions", [])

    async def get_recent_trigger_executions(
        self,
        user_id: str = "default",
        limit: int = 50,
    ) -> List[dict]:
        """Get recent trigger executions for a user."""
        data = await self._get(
            "/triggers/executions/recent",
            params={"user_id": user_id, "limit": limit},
            act_as=user_id,
        )
        return data if isinstance(data, list) else data.get("executions", [])

    async def fire_trigger(
        self,
        trigger_id: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        secret: Optional[str] = None,
        user_id: str = "default",
    ) -> dict:
        """Fire a webhook trigger as an authenticated API caller."""
        return await self._post(
            f"/triggers/fire/{_path_param(trigger_id)}",
            json=payload or {},
            params=_clean_params(secret=secret, user_id=user_id),
            act_as=user_id,
        )

    # ── Activity and Notifications ────────────────────────────────────────

    async def get_activity(
        self,
        user_id: str = "default",
        *,
        limit: int = 50,
        activity_type: Optional[str] = None,
        thread_id: Optional[str] = None,
    ) -> dict:
        """Get recent activity entries."""
        return await self._get(
            "/activity",
            params=_clean_params(
                user_id=user_id,
                limit=limit,
                activity_type=activity_type,
                thread_id=thread_id,
            ),
            act_as=user_id,
        )

    async def get_notifications(self, user_id: str = "default") -> dict:
        """Get notifications and unread count for a user."""
        return await self._get("/notifications", act_as=user_id)

    async def mark_notification_read(
        self,
        notification_id: str,
        user_id: str = "default",
    ) -> dict:
        """Mark one notification as read."""
        return await self._post(
            f"/notifications/{_path_param(notification_id)}/read",
            act_as=user_id,
        )

    async def mark_all_notifications_read(
        self, user_id: str = "default"
    ) -> dict:
        """Mark all notifications as read for a user."""
        return await self._post("/notifications/read-all", act_as=user_id)

    # ── Memories ──────────────────────────────────────────────────────────

    async def list_memories(self, user_id: str) -> List[dict]:
        """List all memories for a user."""
        data = await self._get(f"/users/{user_id}/memories", act_as=user_id)
        return data.get("memories", [])

    async def save_memory(self, user_id: str, key: str, value: str) -> dict:
        """Save or update a memory."""
        return await self._post(
            f"/users/{user_id}/memories", json={"key": key, "value": value}, act_as=user_id,
        )

    async def forget_memory(self, user_id: str, key: str) -> dict:
        """Remove a memory by key."""
        return await self._delete(f"/users/{user_id}/memories/{key}", act_as=user_id)

    async def search_memories(self, user_id: str, query: str) -> List[dict]:
        """Search memories by keyword."""
        data = await self._get(
            f"/users/{user_id}/memories/search", params={"q": query}, act_as=user_id,
        )
        return data.get("results", [])

    # ── TODOs ─────────────────────────────────────────────────────────────

    async def list_todos(
        self,
        user_id: str,
        *,
        filter_status: Optional[str] = None,
        thread_id: Optional[str] = None,
    ) -> List[dict]:
        """List all TODOs for a user."""
        data = await self._get(
            "/todos",
            params=_clean_params(
                user_id=user_id,
                filter_status=filter_status,
                thread_id=thread_id,
            ),
            act_as=user_id,
        )
        return data.get("items", [])

    async def list_users_with_todos(self) -> List[str]:
        """List all user IDs that have TODO lists."""
        data = await self._get("/todos/users")
        # Endpoint returns a JSON array directly
        if isinstance(data, list):
            return data
        return []

    async def send_external_notification(
        self,
        user_id: str,
        message: str,
        *,
        thread_id: str = "",
    ) -> List[str]:
        """Deliver an off-frontend alert via the user's default notification
        profile (``POST /notifications/external``). The API holds the master
        secrets key, so thin services holding only the service token can deliver
        without vault access. Returns the destination names that accepted
        delivery.
        """
        data = await self._post(
            "/notifications/external",
            json={"message": message, "thread_id": thread_id},
            act_as=user_id,
        )
        if isinstance(data, dict) and isinstance(data.get("delivered_to"), list):
            return [str(name) for name in data["delivered_to"]]
        return []

    async def add_todo(
        self,
        user_id: str,
        task: str,
        scheduled_for: str = "1d",
        notes: Optional[str] = None,
        recurrence: Optional[str] = None,
        thread_id: Optional[str] = None,
    ) -> dict:
        """Create a new TODO."""
        body: dict = {"task": task, "scheduled_for": scheduled_for}
        if notes:
            body["notes"] = notes
        if recurrence:
            body["recurrence"] = recurrence
        if thread_id:
            body["thread_id"] = thread_id
        return await self._post("/todos", json=body, params={"user_id": user_id}, act_as=user_id)

    async def update_todo(self, user_id: str, todo_id: str, **kwargs) -> dict:
        """Update a TODO (partial update)."""
        return await self._patch(
            f"/todos/{todo_id}",
            json=kwargs,
            act_as=user_id,
        )

    async def complete_todo(self, user_id: str, todo_id: str) -> dict:
        """Mark a TODO as done."""
        return await self._post(
            f"/todos/{todo_id}/complete",
            params={"user_id": user_id},
            act_as=user_id,
        )

    async def delete_todo(self, user_id: str, todo_id: str) -> dict:
        """Delete a TODO permanently."""
        return await self._delete(
            f"/todos/{todo_id}",
            params={"user_id": user_id},
            act_as=user_id,
        )

    # ── Settings Management ─────────────────────────────────────────────

    async def update_settings(self, *, user_id: Optional[str] = None, **kwargs) -> dict:
        """Update global server settings (PATCH /settings).

        ``user_id`` becomes ``X-Nymeria-Act-As`` so the API gate enforces the
        real caller's role rather than the bot service token's admin role.
        Slash commands and other agent-callable mutators must always pass it.
        """
        return await self._patch("/settings", json=kwargs, act_as=user_id)

    async def update_thread_config(
        self, thread_id: str, *, user_id: Optional[str] = None, **kwargs,
    ) -> dict:
        """Update per-thread configuration (PATCH /threads/{id}/config)."""
        return await self._patch(f"/threads/{thread_id}/config", json=kwargs, act_as=user_id)

    async def get_env_vars(self, *, user_id: Optional[str] = None) -> dict:
        """Get all settable env vars with masked values."""
        return await self._get("/settings/env", act_as=user_id)

    async def get_env_var(self, key: str, *, user_id: Optional[str] = None) -> dict:
        """Get a single env var's value (`/env <key>` is an explicit reveal)."""
        return await self._get(
            f"/settings/env/{key}", params={"reveal": "true"}, act_as=user_id
        )

    async def list_models(self, user_id: Optional[str] = None) -> List[dict]:
        """List known models from the model capabilities cache."""
        return await self._get("/models", act_as=user_id)

    async def list_available_models(
        self,
        provider: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> List[dict]:
        """Fetch available models from the LLM provider."""
        params = {"provider": provider} if provider else None
        return await self._get("/models/available", params=params, act_as=user_id)

    # ── System ────────────────────────────────────────────────────────────

    async def restart_api(self) -> dict:
        """Trigger API server restart."""
        return await self._post("/restart")

    # ── Workspace ────────────────────────────────────────────────────────

    async def download_workspace_file(
        self,
        file_path: str,
        user_id: Optional[str] = None,
    ) -> Optional[tuple]:
        """Download a file from the workspace.

        Returns ``(raw_bytes, filename, content_type)`` or ``None``.

        Returns ``None`` for an over-limit file, an HTTP-status error (e.g. 404
        missing / 403 denied), or a transient network error, so callers can
        uniformly skip the attachment. The two failure classes are logged
        distinctly (HTTP-status at ``error`` with the status code, network at
        ``warning``) so a masked auth/config problem is no longer
        indistinguishable from a simply-missing file. Only ``httpx`` errors are
        caught; an unexpected programming error propagates rather than being
        swallowed as a silent ``None``.
        """
        _MAX_SIZE = 50 * 1024 * 1024  # Telegram bot limit
        try:
            resp = await self._client_for_loop().get(
                self._url("/workspace/download"),
                headers=self._headers_for(user_id),
                params={"path": file_path},
                timeout=_DEFAULT_TIMEOUT,
            )
            resp.raise_for_status()
            if len(resp.content) > _MAX_SIZE:
                logger.warning("Workspace file too large to attach: %s (%d bytes)", file_path, len(resp.content))
                return None
            content_type = resp.headers.get("content-type", "application/octet-stream")
            cd = resp.headers.get("content-disposition", "")
            filename = file_path.rsplit("/", 1)[-1]
            if "filename=" in cd:
                filename = cd.split("filename=")[-1].strip('" ')
            return (resp.content, filename, content_type)
        except httpx.HTTPStatusError as e:
            logger.error(
                "Workspace download failed for %s: HTTP %s",
                file_path,
                e.response.status_code,
            )
            return None
        except httpx.RequestError as e:
            logger.warning("Workspace download network error for %s: %s", file_path, e)
            return None

    async def download_workspace_artifact(
        self,
        path: str,
        user_id: Optional[str] = None,
    ) -> Optional[tuple]:
        """Alias for artifact-oriented callers."""
        return await self.download_workspace_file(path, user_id=user_id)

    # ── Health ────────────────────────────────────────────────────────────

    async def get_me(self, act_as: Optional[str] = None) -> dict:
        """Return the authenticated user (or act-as target). Used by bots to
        look up a user's role for admin-gated commands."""
        return await self._get("/me", act_as=act_as)

    async def resolve_platform_user(self, platform: str, platform_user_id: str) -> Optional[str]:
        """
        Resolve a platform-native user id (Discord/Telegram/Twitch) to the
        Nymeria account it's linked to. Returns the Nymeria user_id or None
        if no mapping exists. Admin-only on the server side — bots carry the
        service token (admin role), so this works for them.
        """
        try:
            data = await self._get(
                "/platform/resolve",
                params={"provider": platform, "provider_user_id": str(platform_user_id)},
            )
            return data.get("user_id")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    async def health(self) -> bool:
        """Check if the API is healthy."""
        try:
            data = await self._get("/health")
            return data.get("status") == "ok"
        except Exception:
            return False

    # ── Chat-app bindings (admin-only) ────────────────────────────────────

    async def list_chatapp_bindings(self, provider: str) -> List[dict]:
        """List every chat-app binding for the given provider. Admin-only.

        Bots call this on startup and periodically to populate their local
        ``chat_id <-> thread_id`` cache so message routing doesn't need a
        per-message HTTP lookup.
        """
        return await self._get(
            "/admin/chatapp/bindings", params={"provider": provider}
        )

    async def lookup_chatapp_binding(
        self,
        *,
        provider: str,
        platform_chat_id: Optional[str] = None,
        thread_id: Optional[str] = None,
    ) -> Optional[dict]:
        """Look up a single binding by either chat_id or thread_id.

        Returns the binding dict, or None if no binding exists.
        """
        params: Dict[str, str] = {"provider": provider}
        if platform_chat_id is not None:
            params["platform_chat_id"] = str(platform_chat_id)
        if thread_id is not None:
            params["thread_id"] = thread_id
        try:
            return await self._get("/admin/chatapp/bindings/lookup", params=params)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    async def claim_thread_bind_code(
        self,
        *,
        code: str,
        provider: str,
        platform_chat_id: str,
        expected_provider_user_id: str,
    ) -> dict:
        """Atomically consume a /bind <code> and create the thread binding.

        Returns ``{binding_id, thread_id, user_id}`` on success; raises an
        ``httpx.HTTPStatusError`` on 400 (invalid/expired code), 403
        (code issued by a different account), or 409 (chat already bound).
        """
        return await self._post(
            "/admin/chatapp/bindings/claim",
            json={
                "code": code,
                "provider": provider,
                "platform_chat_id": str(platform_chat_id),
                "expected_provider_user_id": str(expected_provider_user_id),
            },
        )

    async def unbind_chatapp_by_chat(
        self,
        *,
        provider: str,
        platform_chat_id: str,
        user_id: Optional[str] = None,
        user_telegram_bot_id: Optional[int] = None,
    ) -> dict:
        """Remove the binding for (provider, chat_id). Returns ``{unbound: bool, thread_id?}``."""
        return await self._delete(
            "/admin/chatapp/bindings/by-chat",
            params=_clean_params(
                provider=provider,
                platform_chat_id=str(platform_chat_id),
                user_id=user_id,
                user_telegram_bot_id=user_telegram_bot_id,
            ),
        )

    async def switch_chatapp_binding(
        self,
        *,
        provider: str,
        platform_chat_id: str,
        thread_id: str,
        user_id: str,
        user_telegram_bot_id: Optional[int] = None,
    ) -> dict:
        """Move a chat-app chat binding to an existing user-owned thread."""
        body: Dict[str, Any] = {
            "provider": provider,
            "platform_chat_id": str(platform_chat_id),
            "thread_id": thread_id,
            "user_id": user_id,
            "user_telegram_bot_id": user_telegram_bot_id,
        }
        return await self._post("/admin/chatapp/bindings/switch", json=body)

    async def claim_platform_link_code(
        self, *, code: str, provider: str, platform_user_id: str
    ) -> dict:
        """Atomically consume a /start link_<code> and create the
        ``platform_identities`` row. Returns ``{user_id, provider, provider_user_id, created_at}``.
        """
        return await self._post(
            "/admin/platform/link-codes/claim",
            json={
                "code": code,
                "provider": provider,
                "platform_user_id": str(platform_user_id),
            },
        )

    async def claim_thread_bind_code_via_bot(
        self,
        *,
        code: str,
        provider: str,
        platform_chat_id: str,
        via_user_telegram_bot_id: int,
    ) -> dict:
        """User-owned-bot variant of ``claim_thread_bind_code``. The bot's
        registration record is the credential here, so we don't pass a
        Telegram user id — the API verifies that the bind code's issuer
        matches the bot's owner.
        """
        return await self._post(
            "/admin/chatapp/bindings/claim-via-bot",
            json={
                "code": code,
                "provider": provider,
                "platform_chat_id": str(platform_chat_id),
                "via_user_telegram_bot_id": via_user_telegram_bot_id,
            },
        )

    # ── User-owned Telegram bots (admin-only, for supervisor) ─────────────

    async def list_admin_telegram_bots(self) -> List[dict]:
        """List every enabled user-owned bot **with decrypted tokens**.

        The supervisor process inside ``nymeria-telegram-bot`` calls this
        every ~15s to refresh the set of polling loops. Each entry has
        ``{id, owner_user_id, bot_username, bot_token, enabled, created_at,
        last_seen_at}``. Returns ``[]`` cleanly when ``NYMERIA_SECRETS_KEY``
        isn't configured (no bots could exist in that case anyway).
        """
        return await self._get("/admin/telegram-bots")

    async def report_telegram_bot_seen(self, bot_id: int) -> None:
        """Heartbeat ping to bump ``last_seen_at`` after a successful
        refresh of this bot's polling loop.
        """
        try:
            await self._post(f"/admin/telegram-bots/{bot_id}/seen")
        except httpx.HTTPStatusError:
            # Heartbeat failure is non-fatal — don't crash the supervisor
            # if the API is briefly unhappy.
            return
