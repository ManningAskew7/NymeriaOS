"""Matrix bot trigger for two-way Nymeria communication.

This module uses the Matrix Client-Server API directly. It intentionally keeps
v1 plaintext-only and thin: Matrix events are translated into Nymeria REST/SSE
calls, and all conversation state remains in the API service.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional
from urllib.parse import quote

import httpx

from .api_client import NymeriaAPIClient
from .bot_helpers import SeenEventCache, UserResolver, http_error_detail, safe_id as _safe_id
from .message_splitter import split_matrix_message as split_message
from .sse_consumer import consume_sse_stream
from ..core.service_health import HEARTBEAT_INTERVAL_SECONDS, write_service_heartbeat

logger = logging.getLogger(__name__)

MATRIX_TEXT_LIMIT = 3500
BINDING_REFRESH_INTERVAL_SECONDS = 60
SYNC_TIMEOUT_MS = 30_000


def make_platform_chat_id(room_id: str, thread_root_event_id: Optional[str] = None) -> str:
    """Return the Matrix chat key used by Nymeria chat-app bindings."""
    if thread_root_event_id:
        return f"{room_id}:{thread_root_event_id}"
    return room_id


def make_thread_id(room_id: str, thread_root_event_id: Optional[str] = None) -> str:
    """Generate a native Nymeria thread ID for a Matrix room/thread."""
    base = f"matrix_{_safe_id(room_id)}"
    if thread_root_event_id:
        return f"{base}_thread_{_safe_id(thread_root_event_id)}"
    return base


def strip_matrix_mention(text: str, bot_user_id: Optional[str]) -> str:
    """Remove a Matrix bot user ID mention from message text."""
    if not bot_user_id or not text:
        return text.strip()
    return text.replace(bot_user_id, "").strip()


def event_mentions_bot(event: Mapping[str, Any], bot_user_id: Optional[str]) -> bool:
    """Return True when a Matrix event explicitly mentions the bot user."""
    if not bot_user_id:
        return False
    content = event.get("content")
    if not isinstance(content, Mapping):
        return False
    body = str(content.get("body") or "")
    if bot_user_id in body:
        return True
    mentions = content.get("m.mentions")
    if isinstance(mentions, Mapping):
        user_ids = mentions.get("user_ids")
        if isinstance(user_ids, list) and bot_user_id in user_ids:
            return True
    return False


def matrix_thread_root(content: Mapping[str, Any]) -> Optional[str]:
    """Extract the Matrix thread root event ID, if present."""
    relates_to = content.get("m.relates_to")
    if not isinstance(relates_to, Mapping):
        return None
    if relates_to.get("rel_type") != "m.thread":
        return None
    event_id = relates_to.get("event_id")
    return str(event_id) if event_id else None


@dataclass(frozen=True)
class MatrixReplyTarget:
    room_id: str
    reply_to_event_id: Optional[str] = None
    thread_root_event_id: Optional[str] = None


class MatrixCommand:
    """Small parser for Matrix text commands handled before agent routing."""

    def __init__(self, name: str, arg: str = "") -> None:
        self.name = name
        self.arg = arg

    @classmethod
    def parse(cls, text: str, bot_user_id: Optional[str] = None) -> Optional["MatrixCommand"]:
        cleaned = strip_matrix_mention(text, bot_user_id).strip()
        lower = cleaned.lower()
        for prefix in ("link_", "bind_"):
            if lower.startswith(prefix):
                return cls(prefix[:-1], cleaned[len(prefix):].strip())
        match = re.match(r"^(link|bind)\s+(.+)$", cleaned, re.I)
        if match:
            return cls(match.group(1).lower(), match.group(2).strip())
        if re.match(r"^(unbind|stop)$", cleaned, re.I):
            return cls(cleaned.lower(), "")
        return None


class MatrixHTTPClient:
    """Minimal async Matrix Client-Server API wrapper."""

    def __init__(
        self,
        homeserver: str,
        *,
        access_token: Optional[str] = None,
        user_id: Optional[str] = None,
        password: Optional[str] = None,
        device_id: Optional[str] = None,
    ) -> None:
        self.homeserver = homeserver.rstrip("/")
        self.access_token = access_token
        self.user_id = user_id
        self.password = password
        self.device_id = device_id
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=40, write=10, pool=10))

    async def close(self) -> None:
        await self._client.aclose()

    async def authenticate(self) -> str:
        if not self.access_token:
            if not self.user_id or not self.password:
                raise RuntimeError(
                    "Matrix requires MATRIX_ACCESS_TOKEN, or MATRIX_USER_ID + MATRIX_PASSWORD."
                )
            body: Dict[str, Any] = {
                "type": "m.login.password",
                "identifier": {"type": "m.id.user", "user": self.user_id},
                "password": self.password,
            }
            if self.device_id:
                body["device_id"] = self.device_id
            data = await self._request("POST", "/login", json=body, authed=False)
            self.access_token = str(data.get("access_token") or "")
            self.user_id = str(data.get("user_id") or self.user_id)
            if not self.access_token:
                raise RuntimeError("Matrix login did not return an access token.")
            return self.user_id or ""

        if not self.user_id:
            data = await self._request("GET", "/account/whoami")
            self.user_id = str(data.get("user_id") or "")
        if not self.user_id:
            raise RuntimeError("Matrix account identity could not be resolved.")
        return self.user_id

    async def sync(self, *, since: Optional[str], timeout_ms: int) -> dict:
        params: Dict[str, Any] = {"timeout": timeout_ms}
        if since:
            params["since"] = since
        return await self._request("GET", "/sync", params=params)

    async def send_message(
        self,
        room_id: str,
        content: Mapping[str, Any],
    ) -> dict:
        txn_id = f"nymeria-{uuid.uuid4().hex}"
        return await self._request(
            "PUT",
            f"/rooms/{quote(room_id, safe='')}/send/m.room.message/{txn_id}",
            json=dict(content),
        )

    async def join_room(self, room_id_or_alias: str) -> dict:
        return await self._request(
            "POST",
            f"/join/{quote(room_id_or_alias, safe='')}",
            json={},
        )

    async def upload_media(self, *, data: bytes, filename: str, content_type: str) -> str:
        """Upload bytes to the Matrix media repo and return the mxc:// URI."""
        if not self.access_token:
            raise RuntimeError("Matrix request requires an access token.")
        response = await self._client.post(
            f"{self.homeserver}/_matrix/media/v3/upload",
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": content_type or "application/octet-stream",
            },
            params={"filename": filename},
            content=data,
        )
        response.raise_for_status()
        payload = response.json()
        mxc = payload.get("content_uri") if isinstance(payload, Mapping) else ""
        if not mxc:
            raise RuntimeError("Matrix upload did not return a content_uri")
        return str(mxc)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Optional[dict] = None,
        params: Optional[dict] = None,
        authed: bool = True,
    ) -> dict:
        headers = {"Accept": "application/json"}
        if json is not None:
            headers["Content-Type"] = "application/json"
        if authed:
            if not self.access_token:
                raise RuntimeError("Matrix request requires an access token.")
            headers["Authorization"] = f"Bearer {self.access_token}"
        response = await self._client.request(
            method,
            f"{self.homeserver}/_matrix/client/v3{path}",
            headers=headers,
            json=json,
            params=params,
        )
        response.raise_for_status()
        if response.status_code == 204 or not response.content:
            return {}
        data = response.json()
        return data if isinstance(data, dict) else {}


class NymeriaMatrixBot:
    """Matrix sync-loop bot — thin client for Nymeria."""

    def __init__(
        self,
        api: NymeriaAPIClient,
        *,
        homeserver: str,
        access_token: Optional[str] = None,
        user_id: Optional[str] = None,
        password: Optional[str] = None,
        device_id: Optional[str] = None,
        respond_mode: str = "mention",
        free_response_rooms: Optional[list[str]] = None,
        auto_join: bool = False,
        matrix_client: Optional[MatrixHTTPClient] = None,
    ) -> None:
        self.api = api
        self.matrix = matrix_client or MatrixHTTPClient(
            homeserver,
            access_token=access_token,
            user_id=user_id,
            password=password,
            device_id=device_id,
        )
        self.respond_mode = respond_mode if respond_mode in {"mention", "all"} else "mention"
        self.free_response_rooms = set(free_response_rooms or [])
        self.auto_join = auto_join
        self.user_id = user_id
        self._since: Optional[str] = None
        self._running = False
        self._seen = SeenEventCache()
        self._dm_like_rooms: set[str] = set()
        self._encrypted_notice_rooms: set[str] = set()
        self._bindings: Dict[str, str] = {}
        self._user_resolver = UserResolver(self.api, "matrix", logger=logger)
        self._health_task: Optional[asyncio.Task] = None
        self._bindings_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        self.user_id = await self.matrix.authenticate()
        await self._refresh_bindings()
        await self._baseline_sync()
        self._running = True
        self._bindings_task = asyncio.create_task(self._bindings_refresh_loop())
        self._health_task = asyncio.create_task(self._health_heartbeat_loop())
        logger.info("Matrix bot authenticated as %s", self.user_id)
        await self._sync_loop()

    async def close(self) -> None:
        self._running = False
        for task in (self._bindings_task, self._health_task):
            if task:
                task.cancel()
        await self.matrix.close()
        await self.api.close()

    def run(self) -> None:
        asyncio.run(self.start())

    async def resolve_user_id(self, matrix_user_id: str) -> Optional[str]:
        return await self._user_resolver.resolve(matrix_user_id)

    async def _baseline_sync(self) -> None:
        data = await self.matrix.sync(since=None, timeout_ms=0)
        self._since = str(data.get("next_batch") or "")
        self._update_room_state(data)
        logger.info("Matrix baseline sync complete")

    async def _sync_loop(self) -> None:
        while self._running:
            try:
                data = await self.matrix.sync(since=self._since, timeout_ms=SYNC_TIMEOUT_MS)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Matrix sync failed")
                await asyncio.sleep(5)
                continue
            self._since = str(data.get("next_batch") or self._since or "")
            self._update_room_state(data)
            await self._handle_sync_response(data)

    def _update_room_state(self, data: Mapping[str, Any]) -> None:
        rooms = data.get("rooms")
        if not isinstance(rooms, Mapping):
            return
        joined = rooms.get("join")
        if not isinstance(joined, Mapping):
            return
        for room_id, room_data in joined.items():
            if not isinstance(room_data, Mapping):
                continue
            summary = room_data.get("summary")
            if not isinstance(summary, Mapping):
                continue
            joined_count = summary.get("m.joined_member_count")
            invited_count = summary.get("m.invited_member_count")
            invited = invited_count if isinstance(invited_count, int) else 0
            if isinstance(joined_count, int) and joined_count + invited <= 2:
                self._dm_like_rooms.add(str(room_id))

    async def _handle_sync_response(self, data: Mapping[str, Any]) -> None:
        rooms = data.get("rooms")
        if not isinstance(rooms, Mapping):
            return
        await self._handle_invites(rooms.get("invite"))
        joined = rooms.get("join")
        if not isinstance(joined, Mapping):
            return
        for room_id, room_data in joined.items():
            if not isinstance(room_data, Mapping):
                continue
            timeline = room_data.get("timeline")
            if not isinstance(timeline, Mapping):
                continue
            events = timeline.get("events")
            if not isinstance(events, list):
                continue
            for event in events:
                if isinstance(event, Mapping):
                    await self.handle_matrix_event(str(room_id), event)

    async def _handle_invites(self, invited: Any) -> None:
        if not self.auto_join or not isinstance(invited, Mapping):
            return
        for room_id in invited:
            try:
                await self.matrix.join_room(str(room_id))
                logger.info("Matrix auto-joined invited room %s", room_id)
            except Exception:
                logger.exception("Matrix auto-join failed for %s", room_id)

    async def handle_matrix_event(self, room_id: str, event: Mapping[str, Any]) -> None:
        event_id = str(event.get("event_id") or "")
        if not event_id or self._mark_seen(event_id):
            return
        sender = str(event.get("sender") or "")
        if not sender or (self.user_id and sender == self.user_id):
            return

        event_type = str(event.get("type") or "")
        if event_type == "m.room.encrypted":
            if room_id not in self._encrypted_notice_rooms:
                self._encrypted_notice_rooms.add(room_id)
                await self._send_text(
                    MatrixReplyTarget(room_id=room_id),
                    "I received an encrypted Matrix event, but this Nymeria Matrix bot currently supports plaintext rooms only.",
                )
            return
        if event_type != "m.room.message":
            return
        content = event.get("content")
        if not isinstance(content, Mapping):
            return
        msgtype = str(content.get("msgtype") or "")
        if msgtype not in {"m.text", "m.notice"}:
            return
        body = str(content.get("body") or "").strip()
        if not body:
            return

        mentioned = event_mentions_bot(event, self.user_id)
        room_is_free = room_id in self.free_response_rooms or room_id in self._dm_like_rooms
        should_respond = self.respond_mode == "all" or room_is_free or mentioned
        if not should_respond:
            return

        clean_body = strip_matrix_mention(body, self.user_id)
        thread_root = matrix_thread_root(content)
        target = MatrixReplyTarget(
            room_id=room_id,
            reply_to_event_id=event_id,
            thread_root_event_id=thread_root,
        )

        command = MatrixCommand.parse(clean_body, self.user_id)
        if command is not None:
            await self._handle_command(command, room_id=room_id, sender=sender, target=target)
            return

        user_id = await self.resolve_user_id(sender)
        if user_id is None:
            await self._reject_unlinked(target, sender)
            return

        thread_id = self._resolve_thread_id(room_id, thread_root)
        prompt = clean_body if room_is_free else f"[Matrix {sender} in {room_id}]\n{clean_body}"
        await self._stream_to_matrix(
            message=prompt,
            thread_id=thread_id,
            user_id=user_id,
            target=target,
        )

    def _resolve_thread_id(self, room_id: str, thread_root_event_id: Optional[str]) -> str:
        candidates: list[str] = []
        if thread_root_event_id:
            candidates.append(make_platform_chat_id(room_id, thread_root_event_id))
        candidates.append(make_platform_chat_id(room_id))
        for key in candidates:
            bound = self._bindings.get(key)
            if bound:
                return bound
        return make_thread_id(room_id, thread_root_event_id)

    def _mark_seen(self, event_id: str) -> bool:
        """Dedupe by event id via the shared TTL cache."""
        return self._seen.mark_seen(event_id)

    async def _handle_command(
        self,
        command: MatrixCommand,
        *,
        room_id: str,
        sender: str,
        target: MatrixReplyTarget,
    ) -> None:
        if command.name == "link":
            await self._cmd_link(command.arg, sender, target)
        elif command.name == "bind":
            await self._cmd_bind(command.arg, room_id, sender, target)
        elif command.name == "unbind":
            await self._cmd_unbind(room_id, sender, target)
        elif command.name == "stop":
            await self._cmd_stop(room_id, sender, target)

    async def _cmd_link(self, code: str, sender: str, target: MatrixReplyTarget) -> None:
        if not code:
            await self._send_text(target, "Usage: `link <code>`")
            return
        try:
            result = await self.api.claim_platform_link_code(
                code=code,
                provider="matrix",
                platform_user_id=sender,
            )
        except httpx.HTTPStatusError as exc:
            await self._send_text(target, f"Couldn't link: {http_error_detail(exc)}")
            return
        self._user_resolver.invalidate(sender)
        await self._send_text(target, f"Linked this Matrix account to Nymeria user `{result.get('user_id')}`.")

    async def _cmd_bind(
        self,
        code: str,
        room_id: str,
        sender: str,
        target: MatrixReplyTarget,
    ) -> None:
        if not code:
            await self._send_text(target, "Usage: `bind <code>`")
            return
        chat_id = make_platform_chat_id(room_id, target.thread_root_event_id)
        try:
            result = await self.api.claim_thread_bind_code(
                code=code,
                provider="matrix",
                platform_chat_id=chat_id,
                expected_provider_user_id=sender,
            )
        except httpx.HTTPStatusError as exc:
            await self._send_text(target, f"Couldn't bind: {http_error_detail(exc)}")
            return
        thread_id = str(result.get("thread_id") or "")
        if thread_id:
            self._bindings[chat_id] = thread_id
        await self._send_text(target, f"Bound this Matrix room to `{thread_id}`.")

    async def _cmd_unbind(self, room_id: str, sender: str, target: MatrixReplyTarget) -> None:
        user_id = await self.resolve_user_id(sender)
        if user_id is None:
            await self._reject_unlinked(target, sender)
            return
        chat_id = make_platform_chat_id(room_id, target.thread_root_event_id)
        try:
            result = await self.api.unbind_chatapp_by_chat(
                provider="matrix",
                platform_chat_id=chat_id,
                user_id=user_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Matrix unbind failed")
            await self._send_text(target, f"Couldn't unbind: {exc}")
            return
        if not result.get("unbound"):
            await self._send_text(target, "This Matrix room is not bound.")
            return
        self._bindings.pop(chat_id, None)
        await self._send_text(target, "Unbound. Future messages will use the default Matrix thread.")

    async def _cmd_stop(self, room_id: str, sender: str, target: MatrixReplyTarget) -> None:
        user_id = await self.resolve_user_id(sender)
        if user_id is None:
            await self._reject_unlinked(target, sender)
            return
        thread_id = self._resolve_thread_id(room_id, target.thread_root_event_id)
        try:
            await self.api.stop(thread_id, user_id=user_id)
        except Exception as exc:  # noqa: BLE001
            await self._send_text(target, f"Couldn't stop the current run: {exc}")
            return
        await self._send_text(target, "Stopped the current run for this Matrix room.")

    async def _reject_unlinked(self, target: MatrixReplyTarget, sender: str) -> None:
        await self._send_text(
            target,
            "This Matrix account is not linked to a Nymeria user yet.\n"
            f"Use `link <code>` here, or ask an admin to run "
            f"`python3 run.py users link-platform <email> matrix {sender}`.",
        )

    async def _stream_to_matrix(
        self,
        *,
        message: str,
        thread_id: str,
        user_id: str,
        target: MatrixReplyTarget,
    ) -> None:
        handler = _MatrixStreamHandler(self, target)
        try:
            await consume_sse_stream(
                self.api.chat_stream(message, thread_id, user_id),
                handler,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Matrix streaming failed; falling back to sync")
            try:
                data = await self.api.chat(message, thread_id, user_id)
                response = str(data.get("response") or "")
                tool_count = data.get("tool_call_count") or 0
                if tool_count:
                    response += f"\n\nTool calls: {tool_count}"
                await self._send_text(target, response or "(no response)")
            except Exception as fallback_exc:  # noqa: BLE001
                logger.exception("Matrix sync fallback failed")
                await self._send_text(
                    target,
                    f"Sorry, I encountered an error: {fallback_exc or exc}",
                )

    async def _send_text(self, target: MatrixReplyTarget, content: str) -> None:
        for chunk in split_message(content, MATRIX_TEXT_LIMIT):
            body: Dict[str, Any] = {
                "msgtype": "m.text",
                "body": chunk,
            }
            if target.reply_to_event_id:
                body["m.relates_to"] = {
                    "m.in_reply_to": {"event_id": target.reply_to_event_id}
                }
            await self.matrix.send_message(target.room_id, body)

    async def _send_workspace_attachment(self, target: MatrixReplyTarget, path: str) -> None:
        """Download a generated workspace file, upload it, and send an m.image event."""
        result = await self.api.download_workspace_file(path)
        if result is None:
            await self._send_text(target, f"Workspace artifact: {path}")
            return
        raw_bytes, filename, content_type = result
        try:
            mxc = await self.matrix.upload_media(
                data=raw_bytes, filename=filename, content_type=content_type
            )
            msgtype = "m.image" if (content_type or "").startswith("image/") else "m.file"
            content: Dict[str, Any] = {
                "msgtype": msgtype,
                "body": filename,
                "url": mxc,
                "info": {"mimetype": content_type, "size": len(raw_bytes)},
            }
            if target.reply_to_event_id:
                content["m.relates_to"] = {
                    "m.in_reply_to": {"event_id": target.reply_to_event_id}
                }
            await self.matrix.send_message(target.room_id, content)
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to upload Matrix workspace attachment %s: %s", path, e)
            await self._send_text(target, f"Workspace artifact: {path}")

    async def _refresh_bindings(self) -> None:
        try:
            entries = await self.api.list_chatapp_bindings(provider="matrix")
        except Exception:  # noqa: BLE001
            logger.exception("Failed to refresh Matrix chat-app bindings; keeping current cache")
            return
        bindings: Dict[str, str] = {}
        for entry in entries:
            chat_id = str(entry.get("platform_chat_id") or "")
            thread_id = str(entry.get("thread_id") or "")
            if chat_id and thread_id:
                bindings[chat_id] = thread_id
        self._bindings = bindings
        logger.debug("Matrix chat-app bindings refreshed: %d entries", len(bindings))

    async def _bindings_refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(BINDING_REFRESH_INTERVAL_SECONDS)
            await self._refresh_bindings()

    async def _health_heartbeat_loop(self) -> None:
        while True:
            try:
                api_ok = await self.api.health()
                write_service_heartbeat(
                    "matrix-bot",
                    status="ok" if api_ok and self._running else "unhealthy",
                    details={"api": api_ok, "matrix_user_id": self.user_id},
                )
            except Exception:  # noqa: BLE001
                logger.warning("Matrix health heartbeat failed", exc_info=True)
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)


class _MatrixStreamHandler:
    """Render Nymeria SSE events into Matrix messages."""

    def __init__(self, bot: NymeriaMatrixBot, target: MatrixReplyTarget) -> None:
        self._bot = bot
        self._target = target
        self._buffer = ""
        self._done = False

    async def flush_text(self, final: bool = False) -> None:
        if not self._buffer.strip():
            self._buffer = ""
            return
        text = self._buffer
        self._buffer = ""
        await self._bot._send_text(self._target, text)

    async def on_thinking(self) -> None:
        return None

    async def on_response_chunk(self, content: str) -> None:
        self._buffer += content
        if len(self._buffer) >= MATRIX_TEXT_LIMIT:
            await self.flush_text()

    async def on_compacting(self, message: str) -> None:
        await self._bot._send_text(self._target, message)

    async def on_compacted(
        self,
        summary: str,
        messages_removed: int,
        title: str,
    ) -> None:
        detail = title
        if messages_removed:
            detail += f" ({messages_removed} messages summarized)"
        if summary:
            detail += f"\n\n{summary[:900]}"
        await self._bot._send_text(self._target, detail)

    async def on_tool_call(
        self,
        name: str,
        args: Dict[str, Any],
        call_id: str,
        count: int,
    ) -> None:
        return None

    async def on_tool_result(
        self,
        call_id: str,
        result: str,
        attachments: List[str],
    ) -> None:
        for path in attachments:
            await self._bot._send_workspace_attachment(self._target, path)

    async def on_tool_reload(self, tools: List[str], ttl: str) -> None:
        names = ", ".join(tools) if tools else "tools"
        await self._bot._send_text(self._target, f"Tool binding: {names} ({ttl})")

    async def on_workspace_artifact(self, path: str) -> None:
        await self._bot._send_text(self._target, f"Workspace artifact: {path}")

    async def on_error(self, content: str) -> None:
        await self._bot._send_text(self._target, f"Sorry, I encountered an error: {content}")

    async def on_iteration_limit(self, content: str) -> None:
        await self._bot._send_text(self._target, content)

    async def on_done(self, tool_call_count: int) -> None:
        if tool_call_count and self._buffer:
            self._buffer += f"\n\nTool calls: {tool_call_count}"
        await self.flush_text(final=True)
        self._done = True

    async def on_stream_end(self, tool_call_count: int) -> None:
        if self._done:
            return
        if tool_call_count and self._buffer:
            self._buffer += f"\n\nTool calls: {tool_call_count}"
        await self.flush_text(final=True)
