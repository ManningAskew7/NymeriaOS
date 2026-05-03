"""Thin HTTP client for the Nymeria REST API.

Used by bot thin clients (Discord, Telegram, Twitch), the watchdog
worker, slash commands, and ``run.py`` helpers to interact with the
Nymeria backend without running their own NymeriaAgent instance.
All state lives in the API container — this is just a typed wrapper
around ``httpx.AsyncClient``.
"""

import json as _json
import logging
from typing import Any, AsyncGenerator, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

# Generous timeout for LLM calls that can take 30s+
_CHAT_TIMEOUT = httpx.Timeout(connect=10, read=300, write=10, pool=10)
_DEFAULT_TIMEOUT = httpx.Timeout(connect=10, read=30, write=10, pool=10)


class NymeriaAPIClient:
    """Async HTTP client for the Nymeria REST API.

    When the client is constructed with an admin-role service token, each
    per-user call can attach ``X-Nymeria-Act-As: <user_id>`` so the server
    routes the request as that user. Callers pass ``act_as=<user_id>`` on
    the underlying ``_get``/``_post``/... helpers. Today the header is only
    consumed by ``GET /me`` and ``GET /platform/resolve``; Step 3b's route
    cutover will make it the authoritative identity for every endpoint.
    """

    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._client = httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT)

    async def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        await self._client.aclose()

    async def aclose(self) -> None:
        """Alias for callers that use httpx-style async close naming."""
        await self.close()

    async def __aenter__(self) -> "NymeriaAPIClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _headers_for(self, act_as: Optional[str]) -> Dict[str, str]:
        if not act_as:
            return self._headers
        return {**self._headers, "X-Nymeria-Act-As": act_as}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[dict] = None,
        params: Optional[dict] = None,
        act_as: Optional[str] = None,
        timeout: httpx.Timeout = _DEFAULT_TIMEOUT,
    ) -> dict:
        resp = await self._client.request(
            method,
            self._url(path),
            headers=self._headers_for(act_as),
            json=json_body,
            params=params,
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()

    async def _get(self, path: str, params: Optional[dict] = None, act_as: Optional[str] = None) -> dict:
        return await self._request("GET", path, params=params, act_as=act_as)

    async def _post(self, path: str, json: Optional[dict] = None, params: Optional[dict] = None, act_as: Optional[str] = None) -> dict:
        return await self._request(
            "POST",
            path,
            json_body=json,
            params=params,
            act_as=act_as,
            timeout=_CHAT_TIMEOUT,
        )

    async def _put(self, path: str, json: Optional[dict] = None, params: Optional[dict] = None, act_as: Optional[str] = None) -> dict:
        return await self._request(
            "PUT", path, json_body=json, params=params, act_as=act_as
        )

    async def _patch(self, path: str, json: Optional[dict] = None, act_as: Optional[str] = None) -> dict:
        return await self._request("PATCH", path, json_body=json, act_as=act_as)

    async def _delete(self, path: str, params: Optional[dict] = None, act_as: Optional[str] = None) -> dict:
        return await self._request("DELETE", path, params=params, act_as=act_as)

    # ── Chat ──────────────────────────────────────────────────────────────

    async def chat(
        self,
        message: str,
        thread_id: str,
        user_id: str,
        attachments: Optional[List[Dict[str, Any]]] = None,
        force_unsupported_attachments: bool = False,
    ) -> dict:
        """Send a message and get a response (non-streaming).

        ``attachments`` is the list of file dicts accepted by the API
        (``file_type``, ``data_url``, ``mime_type``, ``file_name``); see
        ``triggers.attachment_helpers.build_attachment``.

        ``force_unsupported_attachments`` skips the model-capability
        compatibility check that would otherwise raise an error event.
        Set this from chat-only frontends (Discord/Telegram) where the
        user can't dismiss the desktop's "model may not support" modal.

        Returns dict with 'response' (str) and 'tool_call_count' (int).
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
        return await self._post("/chat/sync", json=body, act_as=user_id)

    async def chat_stream(
        self,
        message: str,
        thread_id: str,
        user_id: str,
        is_self_invoke: bool = False,
        trigger_override: Optional[str] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
        force_unsupported_attachments: bool = False,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Stream chat events via SSE (POST /chat).

        Yields dicts with 'type' key: thinking, response, tool_call,
        tool_result, error, done, etc.

        For autonomous/trusted callers (watchdog worker, etc.), set
        is_self_invoke=True so the API treats the nudge as an internal
        message and routes it through the autonomous prompt path.
        trigger_override supplies the label (e.g. 'watchdog').

        ``attachments`` carries multimodal file payloads; same shape as
        :meth:`chat`.
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
        async with self._client.stream(
            "POST",
            self._url("/chat"),
            headers=self._headers_for(user_id),
            json=body,
            timeout=_CHAT_TIMEOUT,
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data: "):
                    continue
                raw = line[6:]
                if raw.startswith(":"):
                    continue
                try:
                    yield _json.loads(raw)
                except _json.JSONDecodeError:
                    continue

    # ── Thread Management ─────────────────────────────────────────────────

    async def stop(self, thread_id: str, user_id: Optional[str] = None) -> dict:
        """Abort the current operation on a thread."""
        return await self._post(f"/threads/{thread_id}/stop", act_as=user_id)

    async def compact(self, thread_id: str, user_id: str) -> dict:
        """Compact conversation context."""
        return await self._post(
            f"/threads/{thread_id}/compact", params={"user_id": user_id}, act_as=user_id,
        )

    async def delete_thread(self, thread_id: str, user_id: Optional[str] = None) -> dict:
        """Delete all history for a thread."""
        return await self._delete(f"/threads/{thread_id}", act_as=user_id)

    async def list_threads(self, user_id: str) -> List[dict]:
        """List threads visible to a user."""
        data = await self._get("/threads", act_as=user_id)
        return data.get("threads", [])

    async def claim_thread(self, thread_id: str, user_id: str) -> dict:
        """Eagerly claim a new user-owned thread."""
        return await self._post(f"/threads/{thread_id}/claim", act_as=user_id)

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
        return await self._get(f"/threads/{thread_id}/context", act_as=user_id)

    async def get_thread_config(
        self, thread_id: str, user_id: Optional[str] = None
    ) -> Optional[dict]:
        """Get per-thread configuration (tools, instructions, etc.)."""
        try:
            return await self._get(f"/threads/{thread_id}/config", act_as=user_id)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    # ── Settings ──────────────────────────────────────────────────────────

    async def get_settings(self) -> dict:
        """Get server settings."""
        return await self._get("/settings")

    # ── Tools ─────────────────────────────────────────────────────────────

    async def get_default_tools(self, user_id: str = "default") -> dict:
        """Get default tool set for a user.

        Returns dict with 'default_tools' (list of names) and
        'available_tools' (list of tool info dicts).
        """
        return await self._get("/tools/defaults", params={"user_id": user_id}, act_as=user_id)

    async def list_all_tools(self, user_id: str = "default") -> List[dict]:
        """List all tools (built-in + optional + MCP) with enabled state."""
        data = await self._get(f"/users/{user_id}/tools", act_as=user_id)
        return data.get("tools", [])

    async def get_tool_categories(self) -> dict:
        """Get tool categories summary."""
        return await self._get("/tools/categories")

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

    async def list_todos(self, user_id: str) -> List[dict]:
        """List all TODOs for a user."""
        data = await self._get("/todos", params={"user_id": user_id}, act_as=user_id)
        return data.get("items", [])

    async def list_users_with_todos(self) -> List[str]:
        """List all user IDs that have TODO lists (for the watchdog worker)."""
        data = await self._get("/todos/users")
        # Endpoint returns a JSON array directly
        if isinstance(data, list):
            return data
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
        """Get a single env var's unmasked value."""
        return await self._get(f"/settings/env/{key}", act_as=user_id)

    async def list_models(self) -> List[dict]:
        """List known models from the model capabilities cache."""
        return await self._get("/models")

    async def list_available_models(self, provider: Optional[str] = None) -> List[dict]:
        """Fetch available models from the LLM provider."""
        params = {"provider": provider} if provider else None
        return await self._get("/models/available", params=params)

    # ── System ────────────────────────────────────────────────────────────

    async def restart_api(self) -> dict:
        """Trigger API server restart."""
        return await self._post("/restart")

    # ── Workspace ────────────────────────────────────────────────────────

    async def download_workspace_file(
        self, file_path: str
    ) -> Optional[tuple]:
        """Download a file from the workspace.

        Returns ``(raw_bytes, filename, content_type)`` or ``None``.
        """
        _MAX_SIZE = 50 * 1024 * 1024  # Telegram bot limit
        try:
            resp = await self._client.get(
                self._url("/workspace/download"),
                headers=self._headers,
                params={"path": file_path},
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
        except Exception as e:
            logger.warning("Failed to download workspace file %s: %s", file_path, e)
            return None

    # ── Health ────────────────────────────────────────────────────────────

    async def get_me(self, act_as: Optional[str] = None) -> dict:
        """Return the authenticated user (or act-as target). Used by bots to
        look up a user's role for admin-gated commands."""
        return await self._get("/me", act_as=act_as)

    async def resolve_platform_user(self, provider: str, provider_user_id: str) -> Optional[str]:
        """
        Resolve a platform-native user id (Discord/Telegram/Twitch) to the
        Nymeria account it's linked to. Returns the Nymeria user_id or None
        if no mapping exists. Admin-only on the server side — bots carry the
        service token (admin role), so this works for them.
        """
        try:
            data = await self._get(
                "/platform/resolve",
                params={"provider": provider, "provider_user_id": str(provider_user_id)},
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
        self, *, provider: str, platform_chat_id: str
    ) -> dict:
        """Remove the binding for (provider, chat_id). Returns ``{unbound: bool, thread_id?}``."""
        return await self._delete(
            "/admin/chatapp/bindings/by-chat",
            params={"provider": provider, "platform_chat_id": str(platform_chat_id)},
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
