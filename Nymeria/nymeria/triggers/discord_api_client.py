"""Thin HTTP client for the Nymeria REST API.

Used by the Discord bot (and potentially other frontends) to interact
with the Nymeria backend without running their own NymeriaAgent instance.
All state lives in the API container — this is just a wrapper.
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
    """Async HTTP client for the Nymeria REST API."""

    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._headers = {"Authorization": f"Bearer {api_key}"}

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    async def _get(self, path: str, params: Optional[dict] = None) -> dict:
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
            resp = await client.get(
                self._url(path), headers=self._headers, params=params
            )
            resp.raise_for_status()
            return resp.json()

    async def _post(self, path: str, json: Optional[dict] = None, params: Optional[dict] = None) -> dict:
        async with httpx.AsyncClient(timeout=_CHAT_TIMEOUT) as client:
            resp = await client.post(
                self._url(path), headers=self._headers, json=json, params=params
            )
            resp.raise_for_status()
            return resp.json()

    async def _put(self, path: str, json: Optional[dict] = None, params: Optional[dict] = None) -> dict:
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
            resp = await client.put(
                self._url(path), headers=self._headers, json=json, params=params
            )
            resp.raise_for_status()
            return resp.json()

    async def _patch(self, path: str, json: Optional[dict] = None) -> dict:
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
            resp = await client.patch(
                self._url(path), headers=self._headers, json=json
            )
            resp.raise_for_status()
            return resp.json()

    async def _delete(self, path: str, params: Optional[dict] = None) -> dict:
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
            resp = await client.delete(
                self._url(path), headers=self._headers, params=params
            )
            resp.raise_for_status()
            return resp.json()

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
        return await self._post("/chat/sync", json=body)

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
        async with httpx.AsyncClient(timeout=_CHAT_TIMEOUT) as client:
            async with client.stream(
                "POST",
                self._url("/chat"),
                headers=self._headers,
                json=body,
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

    async def stop(self, thread_id: str) -> dict:
        """Abort the current operation on a thread."""
        return await self._post(f"/threads/{thread_id}/stop")

    async def compact(self, thread_id: str, user_id: str) -> dict:
        """Compact conversation context."""
        return await self._post(
            f"/threads/{thread_id}/compact", params={"user_id": user_id}
        )

    async def delete_thread(self, thread_id: str) -> dict:
        """Delete all history for a thread."""
        return await self._delete(f"/threads/{thread_id}")

    async def clear_thread(self, thread_id: str, user_id: str = "default") -> dict:
        """Clear conversation history only (preserve notepad + config)."""
        return await self._post(
            f"/threads/{thread_id}/clear", params={"user_id": user_id}
        )

    async def get_history(self, thread_id: str, include_internal: bool = False) -> dict:
        """Get conversation history for a thread."""
        return await self._get(
            f"/threads/{thread_id}/history",
            params={"include_internal": str(include_internal).lower()},
        )

    async def get_context_stats(self, thread_id: str) -> dict:
        """Get token usage and context stats for a thread."""
        return await self._get(f"/threads/{thread_id}/context")

    async def get_thread_config(self, thread_id: str) -> Optional[dict]:
        """Get per-thread configuration (tools, instructions, etc.)."""
        try:
            return await self._get(f"/threads/{thread_id}/config")
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
        return await self._get("/tools/defaults", params={"user_id": user_id})

    async def list_all_tools(self, user_id: str = "default") -> List[dict]:
        """List all tools (built-in + optional + MCP) with enabled state."""
        data = await self._get(f"/users/{user_id}/tools")
        return data.get("tools", [])

    async def get_tool_categories(self) -> dict:
        """Get tool categories summary."""
        return await self._get("/tools/categories")

    # ── Memories ──────────────────────────────────────────────────────────

    async def list_memories(self, user_id: str) -> List[dict]:
        """List all memories for a user."""
        data = await self._get(f"/users/{user_id}/memories")
        return data.get("memories", [])

    async def save_memory(self, user_id: str, key: str, value: str) -> dict:
        """Save or update a memory."""
        return await self._post(
            f"/users/{user_id}/memories", json={"key": key, "value": value}
        )

    async def forget_memory(self, user_id: str, key: str) -> dict:
        """Remove a memory by key."""
        return await self._delete(f"/users/{user_id}/memories/{key}")

    async def search_memories(self, user_id: str, query: str) -> List[dict]:
        """Search memories by keyword."""
        data = await self._get(
            f"/users/{user_id}/memories/search", params={"q": query}
        )
        return data.get("results", [])

    # ── TODOs ─────────────────────────────────────────────────────────────

    async def list_todos(self, user_id: str) -> List[dict]:
        """List all TODOs for a user."""
        data = await self._get("/todos", params={"user_id": user_id})
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
        return await self._post("/todos", json=body, params={"user_id": user_id})

    async def update_todo(self, user_id: str, todo_id: str, **kwargs) -> dict:
        """Update a TODO (partial update)."""
        return await self._patch(
            f"/todos/{todo_id}",
            json=kwargs,
        )

    async def complete_todo(self, user_id: str, todo_id: str) -> dict:
        """Mark a TODO as done."""
        return await self._post(
            f"/todos/{todo_id}/complete",
            params={"user_id": user_id},
        )

    async def delete_todo(self, user_id: str, todo_id: str) -> dict:
        """Delete a TODO permanently."""
        return await self._delete(
            f"/todos/{todo_id}",
            params={"user_id": user_id},
        )

    # ── Settings Management ─────────────────────────────────────────────

    async def update_settings(self, **kwargs) -> dict:
        """Update global server settings (PATCH /settings)."""
        return await self._patch("/settings", json=kwargs)

    async def update_thread_config(self, thread_id: str, **kwargs) -> dict:
        """Update per-thread configuration (PATCH /threads/{id}/config)."""
        return await self._patch(f"/threads/{thread_id}/config", json=kwargs)

    async def get_env_vars(self) -> dict:
        """Get all settable env vars with masked values."""
        return await self._get("/settings/env")

    async def get_env_var(self, key: str) -> dict:
        """Get a single env var's unmasked value."""
        return await self._get(f"/settings/env/{key}")

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
            async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
                resp = await client.get(
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

    async def health(self) -> bool:
        """Check if the API is healthy."""
        try:
            data = await self._get("/health")
            return data.get("status") == "ok"
        except Exception:
            return False
