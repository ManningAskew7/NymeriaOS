"""Rocket.Chat bot trigger for two-way Nymeria communication.

Thin client architecture: the bot receives Rocket.Chat room messages through
the realtime room stream, resolves Rocket.Chat users to Nymeria accounts, and
calls the Nymeria REST/SSE API for all agent work.
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Dict, List, Mapping, Optional, Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx
import websockets

from .api_client import NymeriaAPIClient
from .bot_helpers import UserResolver, http_error_detail
from .message_splitter import split_rocketchat_message as split_message
from .sse_consumer import consume_sse_stream
from ..core.service_health import HEARTBEAT_INTERVAL_SECONDS, write_service_heartbeat

logger = logging.getLogger(__name__)

ROCKETCHAT_TEXT_LIMIT = 4000
BINDING_REFRESH_INTERVAL_SECONDS = 60
SEEN_EVENT_TTL_SECONDS = 10 * 60
SEEN_EVENT_MAX = 5000
ACTIVE_THREAD_MAX = 5000


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value)).strip("-") or "unknown"


def _normalize_base_url(base_url: str) -> str:
    return base_url.rstrip("/")


def _api_base_url(base_url: str) -> str:
    normalized = _normalize_base_url(base_url)
    return normalized if normalized.endswith("/api/v1") else f"{normalized}/api/v1"


def _websocket_url(base_url: str) -> str:
    normalized = _normalize_base_url(base_url)
    if normalized.endswith("/api/v1"):
        normalized = normalized[: -len("/api/v1")]
    parsed = urlsplit(normalized)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    path = f"{parsed.path.rstrip('/')}/websocket"
    return urlunsplit((scheme, parsed.netloc, path, "", ""))


def make_server_key(base_url: str) -> str:
    """Return the Rocket.Chat server key used to scope users and chats."""
    parsed = urlsplit(_normalize_base_url(base_url))
    return _safe_id(parsed.netloc or parsed.path or "rocketchat")


def make_platform_user_id(server_key: str, user_id: str) -> str:
    """Return the Rocket.Chat identity key stored in Nymeria platform links."""
    return f"{_safe_id(server_key)}:{_safe_id(user_id)}"


def make_platform_chat_id(server_key: str, room_id: str, thread_id: Optional[str] = None) -> str:
    """Return the Rocket.Chat chat key used by Nymeria chat-app bindings."""
    base = f"{_safe_id(server_key)}:{_safe_id(room_id)}"
    if thread_id:
        return f"{base}:{_safe_id(thread_id)}"
    return base


def make_thread_id(
    server_key: str,
    room_id: str,
    *,
    user_id: Optional[str] = None,
    thread_id: Optional[str] = None,
    is_dm: bool = False,
) -> str:
    """Generate a native Nymeria thread ID for a Rocket.Chat conversation."""
    server = _safe_id(server_key)
    if is_dm and user_id:
        base = f"rocketchat_dm_{server}_{_safe_id(user_id)}"
    else:
        base = f"rocketchat_{server}_{_safe_id(room_id)}"
    if thread_id:
        return f"{base}_thread_{_safe_id(thread_id)}"
    return base


def strip_bot_mention(
    text: str,
    *,
    bot_username: Optional[str] = None,
    bot_name: Optional[str] = None,
) -> str:
    """Remove simple Rocket.Chat bot mentions from user text."""
    cleaned = text.strip()
    for value in (bot_username, bot_name):
        if not value:
            continue
        cleaned = re.sub(rf"(^|\s)@{re.escape(value)}\b\s*", " ", cleaned).strip()
    return cleaned


def message_mentions_bot(
    message: "RocketChatMessage",
    *,
    bot_user_id: Optional[str] = None,
    bot_username: Optional[str] = None,
    bot_name: Optional[str] = None,
) -> bool:
    """Return True when a Rocket.Chat message explicitly mentions the bot."""
    mention_values = {value for value in (bot_user_id, bot_username, bot_name) if value}
    for mention in message.mentions_list:
        if not isinstance(mention, Mapping):
            continue
        values = {
            str(mention.get("_id") or ""),
            str(mention.get("username") or ""),
            str(mention.get("name") or ""),
        }
        if mention_values & values:
            return True
    content = message.text or ""
    return any(
        re.search(rf"(^|\s)@{re.escape(value)}\b", content)
        for value in (bot_username, bot_name)
        if value
    )


@dataclass(frozen=True)
class RocketChatRoom:
    id: str
    name: str = ""
    type: str = ""

    @property
    def is_dm(self) -> bool:
        return self.type == "d"


@dataclass(frozen=True)
class RocketChatMessage:
    id: str
    room_id: str
    user_id: str
    username: str
    name: str
    text: str
    thread_id: Optional[str] = None
    mentions: list[Mapping[str, Any]] | None = None

    @property
    def mentions_list(self) -> list[Mapping[str, Any]]:
        return self.mentions or []


@dataclass(frozen=True)
class RocketChatReplyTarget:
    room_id: str
    thread_id: Optional[str] = None


class RocketChatClientProtocol(Protocol):
    async def get_me(self) -> Mapping[str, Any]:
        """Return the authenticated Rocket.Chat user."""

    async def get_subscriptions(self) -> list[RocketChatRoom]:
        """Return room subscriptions for the authenticated user."""

    async def post_message(self, *, room_id: str, text: str, thread_id: Optional[str] = None) -> Mapping[str, Any]:
        """Post a Rocket.Chat message."""

    def websocket_events(self, room_ids: list[str]) -> AsyncGenerator[Mapping[str, Any], None]:
        """Yield realtime Rocket.Chat frames."""
        ...

    async def close(self) -> None:
        """Close client resources."""


def message_from_stream_event(event: Mapping[str, Any]) -> Optional[RocketChatMessage]:
    """Normalize a Rocket.Chat `stream-room-messages` frame into a message."""
    if event.get("msg") != "changed":
        return None
    if event.get("collection") != "stream-room-messages":
        return None
    fields = event.get("fields")
    if not isinstance(fields, Mapping):
        return None
    args = fields.get("args")
    if not isinstance(args, list) or not args:
        return None
    raw = args[0]
    if not isinstance(raw, Mapping):
        return None
    message_id = str(raw.get("_id") or "")
    room_id = str(raw.get("rid") or "")
    user = raw.get("u")
    if not isinstance(user, Mapping):
        return None
    user_id = str(user.get("_id") or "")
    if not message_id or not room_id or not user_id:
        return None
    mentions = raw.get("mentions")
    if not isinstance(mentions, list):
        mentions = []
    return RocketChatMessage(
        id=message_id,
        room_id=room_id,
        user_id=user_id,
        username=str(user.get("username") or ""),
        name=str(user.get("name") or ""),
        text=str(raw.get("msg") or "").strip(),
        thread_id=str(raw.get("tmid") or "") or None,
        mentions=[item for item in mentions if isinstance(item, Mapping)],
    )


class _SeenMessageCache:
    """TTL cache for Rocket.Chat message dedupe."""

    def __init__(
        self,
        *,
        ttl_seconds: int = SEEN_EVENT_TTL_SECONDS,
        max_items: int = SEEN_EVENT_MAX,
        clock=time.monotonic,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._max_items = max_items
        self._clock = clock
        self._items: dict[str, float] = {}

    def mark_seen(self, key: str) -> bool:
        """Return True when *key* was already seen and still fresh."""
        now = self._clock()
        expires_at = self._items.get(key)
        if expires_at and expires_at > now:
            return True
        self._items[key] = now + self._ttl_seconds
        self._prune(now)
        return False

    def _prune(self, now: float) -> None:
        if len(self._items) <= self._max_items:
            stale = [key for key, expiry in self._items.items() if expiry <= now]
        else:
            stale_count = len(self._items) - (self._max_items // 2)
            stale = [
                key
                for key, expiry in self._items.items()
                if expiry <= now
            ] + list(self._items)[:stale_count]
        for key in stale:
            self._items.pop(key, None)


class RocketChatHTTPClient:
    """Minimal async Rocket.Chat REST + realtime wrapper."""

    def __init__(self, base_url: str, auth_token: str, user_id: str) -> None:
        self.base_url = _normalize_base_url(base_url)
        self.api_base_url = _api_base_url(base_url)
        self.auth_token = auth_token
        self.user_id = user_id
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=30, write=10, pool=10))

    async def close(self) -> None:
        await self._client.aclose()

    async def get_me(self) -> Mapping[str, Any]:
        return await self._request("GET", "/me")

    async def get_subscriptions(self) -> list[RocketChatRoom]:
        data = await self._request("GET", "/subscriptions.get")
        rows = data.get("update")
        if not isinstance(rows, list):
            return []
        rooms: list[RocketChatRoom] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            room_id = str(row.get("rid") or "")
            if not room_id:
                continue
            rooms.append(
                RocketChatRoom(
                    id=room_id,
                    name=str(row.get("name") or row.get("fname") or room_id),
                    type=str(row.get("t") or ""),
                )
            )
        return rooms

    async def post_message(
        self,
        *,
        room_id: str,
        text: str,
        thread_id: Optional[str] = None,
    ) -> Mapping[str, Any]:
        body: dict[str, Any] = {"channel": room_id, "text": text}
        if thread_id:
            body["tmid"] = thread_id
        return await self._request("POST", "/chat.postMessage", json=body)

    async def websocket_events(self, room_ids: list[str]) -> AsyncGenerator[Mapping[str, Any], None]:
        async with websockets.connect(_websocket_url(self.base_url), ping_interval=30, ping_timeout=30) as ws:
            await ws.send(_json.dumps({"msg": "connect", "version": "1", "support": ["1"]}))
            await ws.send(
                _json.dumps(
                    {
                        "msg": "method",
                        "method": "login",
                        "id": "nymeria-login",
                        "params": [{"resume": self.auth_token}],
                    }
                )
            )
            for index, room_id in enumerate(room_ids):
                await ws.send(
                    _json.dumps(
                        {
                            "msg": "sub",
                            "id": f"nymeria-room-{index}",
                            "name": "stream-room-messages",
                            "params": [room_id, False],
                        }
                    )
                )
            async for raw in ws:
                try:
                    data = _json.loads(raw)
                except (TypeError, _json.JSONDecodeError):
                    logger.debug("Ignoring non-JSON Rocket.Chat WebSocket frame")
                    continue
                if isinstance(data, Mapping):
                    yield data

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Optional[dict] = None,
    ) -> Mapping[str, Any]:
        headers = {
            "Accept": "application/json",
            "X-Auth-Token": self.auth_token,
            "X-User-Id": self.user_id,
        }
        if json is not None:
            headers["Content-Type"] = "application/json"
        response = await self._client.request(
            method,
            f"{self.api_base_url}{path}",
            headers=headers,
            json=json,
        )
        response.raise_for_status()
        if response.status_code == 204 or not response.content:
            return {}
        data = response.json()
        return data if isinstance(data, Mapping) else {}


class RocketChatCommand:
    """Small parser for Rocket.Chat text commands handled before agent routing."""

    def __init__(self, name: str, arg: str = "") -> None:
        self.name = name
        self.arg = arg

    @classmethod
    def parse(
        cls,
        text: str,
        *,
        bot_username: Optional[str] = None,
        bot_name: Optional[str] = None,
    ) -> Optional["RocketChatCommand"]:
        cleaned = strip_bot_mention(text, bot_username=bot_username, bot_name=bot_name).strip()
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


class NymeriaRocketChatBot:
    """Rocket.Chat realtime bot — thin client for Nymeria."""

    def __init__(
        self,
        api: NymeriaAPIClient,
        *,
        base_url: str,
        auth_token: str,
        user_id: str,
        respond_mode: str = "mention",
        show_tool_events: bool = False,
        rocketchat_client: Optional[RocketChatClientProtocol] = None,
    ) -> None:
        self.api = api
        self.base_url = _normalize_base_url(base_url)
        self.server_key = make_server_key(base_url)
        self.respond_mode = respond_mode if respond_mode in {"mention", "all"} else "mention"
        self.show_tool_events = show_tool_events
        self.rocketchat = rocketchat_client or RocketChatHTTPClient(base_url, auth_token, user_id)
        self.bot_user_id = user_id
        self.bot_username: Optional[str] = None
        self.bot_name: Optional[str] = None
        self._running = False
        self._rooms: dict[str, RocketChatRoom] = {}
        self._seen = _SeenMessageCache()
        self._active_threads: set[str] = set()
        self._bindings: Dict[str, str] = {}
        self._binding_users: Dict[str, str] = {}
        self._user_resolver = UserResolver(self.api, "rocketchat", logger=logger)
        self._health_task: Optional[asyncio.Task] = None
        self._bindings_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        identity = await self.rocketchat.get_me()
        data = identity.get("me") if isinstance(identity.get("me"), Mapping) else identity
        if isinstance(data, Mapping):
            self.bot_user_id = str(data.get("_id") or data.get("userId") or self.bot_user_id)
            self.bot_username = str(data.get("username") or "") or None
            self.bot_name = str(data.get("name") or "") or None
        if not self.bot_user_id:
            raise RuntimeError("Rocket.Chat account identity could not be resolved.")
        await self._refresh_rooms()
        await self._refresh_bindings()
        self._running = True
        self._bindings_task = asyncio.create_task(self._bindings_refresh_loop())
        self._health_task = asyncio.create_task(self._health_heartbeat_loop())
        logger.info(
            "Rocket.Chat bot authenticated as %s on %s",
            self.bot_username or self.bot_user_id,
            self.server_key,
        )
        await self._listen_loop()

    async def close(self) -> None:
        self._running = False
        for task in (self._bindings_task, self._health_task):
            if task:
                task.cancel()
        await self.rocketchat.close()
        await self.api.close()

    def run(self) -> None:
        """Synchronous entry point used by run.py."""
        asyncio.run(self.start())

    async def _refresh_rooms(self) -> None:
        rooms = await self.rocketchat.get_subscriptions()
        self._rooms = {room.id: room for room in rooms}
        logger.info("Rocket.Chat subscribed rooms loaded: %d", len(self._rooms))

    async def _listen_loop(self) -> None:
        reconnect_delay = 2
        while self._running:
            try:
                if not self._rooms:
                    await self._refresh_rooms()
                async for event in self.rocketchat.websocket_events(list(self._rooms)):
                    if not self._running:
                        break
                    await self.handle_websocket_event(event)
                reconnect_delay = 2
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Rocket.Chat realtime loop failed; reconnecting")
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 60)
                try:
                    await self._refresh_rooms()
                except Exception:  # noqa: BLE001
                    logger.warning("Rocket.Chat room refresh failed during reconnect", exc_info=True)

    async def resolve_user_id(self, rocketchat_user_id: str) -> Optional[str]:
        platform_user_id = make_platform_user_id(self.server_key, rocketchat_user_id)
        return await self._user_resolver.resolve(platform_user_id)

    async def handle_websocket_event(self, event: Mapping[str, Any]) -> None:
        """Handle one Rocket.Chat realtime event frame."""
        message = message_from_stream_event(event)
        if message is None:
            return
        if message.user_id == self.bot_user_id:
            return
        if self._seen.mark_seen(message.id):
            return
        if not message.text.strip():
            return

        room = self._rooms.get(message.room_id, RocketChatRoom(id=message.room_id))
        is_dm = room.is_dm
        mentioned = message_mentions_bot(
            message,
            bot_user_id=self.bot_user_id,
            bot_username=self.bot_username,
            bot_name=self.bot_name,
        )
        incoming_thread = message.thread_id
        is_thread_reply = bool(incoming_thread and incoming_thread != message.id)
        reply_thread = incoming_thread if incoming_thread else (message.id if not is_dm else None)
        active_key = (
            make_platform_chat_id(self.server_key, message.room_id, reply_thread)
            if reply_thread
            else ""
        )
        should_respond = (
            is_dm
            or self.respond_mode == "all"
            or mentioned
            or (is_thread_reply and active_key in self._active_threads)
        )
        if not should_respond:
            return

        clean_text = strip_bot_mention(
            message.text,
            bot_username=self.bot_username,
            bot_name=self.bot_name,
        )
        target = RocketChatReplyTarget(room_id=message.room_id, thread_id=reply_thread)
        command = RocketChatCommand.parse(
            clean_text,
            bot_username=self.bot_username,
            bot_name=self.bot_name,
        )
        if command is not None:
            await self._handle_command(command, message=message, target=target, is_dm=is_dm)
            return
        if not clean_text.strip():
            return

        user_id = await self.resolve_user_id(message.user_id)
        if user_id is None:
            await self._reject_unlinked(target, message.user_id)
            return

        thread_id = self._resolve_thread_id(message, is_dm=is_dm, thread_id=reply_thread)
        if active_key:
            self._remember_active_thread(active_key)

        prompt = clean_text
        if not is_dm:
            sender = f"@{message.username}" if message.username else message.user_id
            prompt = f"[Rocket.Chat {sender} in {room.name or message.room_id}]\n{clean_text}"

        await self._stream_to_rocketchat(
            message=prompt,
            thread_id=thread_id,
            user_id=user_id,
            target=target,
        )

    def _resolve_thread_id(
        self,
        message: RocketChatMessage,
        *,
        is_dm: bool,
        thread_id: Optional[str],
    ) -> str:
        candidates: list[str] = []
        if thread_id:
            candidates.append(make_platform_chat_id(self.server_key, message.room_id, thread_id))
        candidates.append(make_platform_chat_id(self.server_key, message.room_id))
        for key in candidates:
            bound = self._bindings.get(key)
            if bound:
                return bound
        return make_thread_id(
            self.server_key,
            message.room_id,
            user_id=message.user_id,
            thread_id=thread_id if not is_dm else None,
            is_dm=is_dm,
        )

    def _remember_active_thread(self, key: str) -> None:
        self._active_threads.add(key)
        if len(self._active_threads) > ACTIVE_THREAD_MAX:
            for old_key in list(self._active_threads)[: ACTIVE_THREAD_MAX // 2]:
                self._active_threads.discard(old_key)

    async def _handle_command(
        self,
        command: RocketChatCommand,
        *,
        message: RocketChatMessage,
        target: RocketChatReplyTarget,
        is_dm: bool,
    ) -> None:
        if command.name == "link":
            await self._cmd_link(command.arg, message.user_id, target)
        elif command.name == "bind":
            await self._cmd_bind(command.arg, message.user_id, message.room_id, target.thread_id, target)
        elif command.name == "unbind":
            await self._cmd_unbind(message.user_id, message.room_id, target.thread_id, target)
        elif command.name == "stop":
            await self._cmd_stop(message, target, is_dm=is_dm)

    async def _cmd_link(self, code: str, user_id: str, target: RocketChatReplyTarget) -> None:
        if not code:
            await self._send_text(target, "Usage: `link <code>`")
            return
        platform_user_id = make_platform_user_id(self.server_key, user_id)
        try:
            result = await self.api.claim_platform_link_code(
                code=code,
                provider="rocketchat",
                platform_user_id=platform_user_id,
            )
        except httpx.HTTPStatusError as exc:
            await self._send_text(target, f"Couldn't link: {http_error_detail(exc)}")
            return
        self._user_resolver.invalidate(platform_user_id)
        await self._send_text(target, f"Linked this Rocket.Chat account to Nymeria user `{result.get('user_id')}`.")

    async def _cmd_bind(
        self,
        code: str,
        user_id: str,
        room_id: str,
        thread_id: Optional[str],
        target: RocketChatReplyTarget,
    ) -> None:
        if not code:
            await self._send_text(target, "Usage: `bind <code>`")
            return
        chat_id = make_platform_chat_id(self.server_key, room_id, thread_id)
        platform_user_id = make_platform_user_id(self.server_key, user_id)
        try:
            result = await self.api.claim_thread_bind_code(
                code=code,
                provider="rocketchat",
                platform_chat_id=chat_id,
                expected_provider_user_id=platform_user_id,
            )
        except httpx.HTTPStatusError as exc:
            detail = http_error_detail(exc)
            if exc.response.status_code == 409:
                await self._send_text(target, f"Already bound: {detail}")
            else:
                await self._send_text(target, f"Couldn't bind: {detail}")
            return
        bound_thread_id = str(result.get("thread_id") or "")
        bound_user_id = str(result.get("user_id") or "")
        if bound_thread_id:
            self._bindings[chat_id] = bound_thread_id
        if bound_user_id:
            self._binding_users[chat_id] = bound_user_id
        await self._send_text(target, f"Bound this Rocket.Chat conversation to `{bound_thread_id}`.")

    async def _cmd_unbind(
        self,
        user_id: str,
        room_id: str,
        thread_id: Optional[str],
        target: RocketChatReplyTarget,
    ) -> None:
        nymeria_user_id = await self.resolve_user_id(user_id)
        if nymeria_user_id is None:
            await self._reject_unlinked(target, user_id)
            return
        chat_id = make_platform_chat_id(self.server_key, room_id, thread_id)
        try:
            result = await self.api.unbind_chatapp_by_chat(
                provider="rocketchat",
                platform_chat_id=chat_id,
                user_id=nymeria_user_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Rocket.Chat unbind failed")
            await self._send_text(target, f"Couldn't unbind: {exc}")
            return
        if not result.get("unbound"):
            await self._send_text(target, "This Rocket.Chat conversation is not bound.")
            return
        self._bindings.pop(chat_id, None)
        self._binding_users.pop(chat_id, None)
        await self._send_text(target, "Unbound. Future messages will use the default Rocket.Chat thread.")

    async def _cmd_stop(self, message: RocketChatMessage, target: RocketChatReplyTarget, *, is_dm: bool) -> None:
        nymeria_user_id = await self.resolve_user_id(message.user_id)
        if nymeria_user_id is None:
            await self._reject_unlinked(target, message.user_id)
            return
        thread_id = self._resolve_thread_id(message, is_dm=is_dm, thread_id=target.thread_id)
        try:
            await self.api.stop(thread_id, user_id=nymeria_user_id)
        except Exception as exc:  # noqa: BLE001
            await self._send_text(target, f"Couldn't stop the current run: {exc}")
            return
        await self._send_text(target, "Stopped the current run for this Rocket.Chat conversation.")

    async def _reject_unlinked(self, target: RocketChatReplyTarget, user_id: str) -> None:
        platform_user_id = make_platform_user_id(self.server_key, user_id)
        await self._send_text(
            target,
            "This Rocket.Chat account is not linked to a Nymeria user yet.\n"
            f"Use `link <code>` here, or ask an admin to run "
            f"`python3 run.py users link-platform <email> rocketchat {platform_user_id}`.",
        )

    async def _stream_to_rocketchat(
        self,
        *,
        message: str,
        thread_id: str,
        user_id: str,
        target: RocketChatReplyTarget,
    ) -> None:
        handler = _RocketChatStreamHandler(self, target)
        try:
            await consume_sse_stream(
                self.api.chat_stream(message, thread_id, user_id),
                handler,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Rocket.Chat streaming failed; falling back to sync")
            try:
                data = await self.api.chat(message, thread_id, user_id)
                response = str(data.get("response") or "")
                tool_count = data.get("tool_call_count") or 0
                if tool_count:
                    response += f"\n\nTool calls: {tool_count}"
                await self._send_text(target, response or "(no response)")
            except Exception as fallback_exc:  # noqa: BLE001
                logger.exception("Rocket.Chat sync fallback failed")
                await self._send_text(
                    target,
                    f"Sorry, I encountered an error: {fallback_exc or exc}",
                )

    async def _send_text(self, target: RocketChatReplyTarget, content: str) -> None:
        for chunk in split_message(content, ROCKETCHAT_TEXT_LIMIT):
            await self.rocketchat.post_message(
                room_id=target.room_id,
                text=chunk,
                thread_id=target.thread_id,
            )

    async def _refresh_bindings(self) -> None:
        try:
            entries = await self.api.list_chatapp_bindings(provider="rocketchat")
        except Exception:  # noqa: BLE001
            logger.exception("Failed to refresh Rocket.Chat chat-app bindings; keeping current cache")
            return
        bindings: Dict[str, str] = {}
        users: Dict[str, str] = {}
        for entry in entries:
            chat_id = str(entry.get("platform_chat_id") or "")
            thread_id = str(entry.get("thread_id") or "")
            user_id = str(entry.get("user_id") or "")
            if chat_id and thread_id:
                bindings[chat_id] = thread_id
                if user_id:
                    users[chat_id] = user_id
        self._bindings = bindings
        self._binding_users = users
        logger.debug("Rocket.Chat chat-app bindings refreshed: %d entries", len(bindings))

    async def _bindings_refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(BINDING_REFRESH_INTERVAL_SECONDS)
            await self._refresh_bindings()

    async def _health_heartbeat_loop(self) -> None:
        while True:
            try:
                api_ok = await self.api.health()
                write_service_heartbeat(
                    "rocketchat-bot",
                    status="ok" if api_ok and self._running else "unhealthy",
                    details={
                        "api": api_ok,
                        "server": self.server_key,
                        "bot_user_id": self.bot_user_id,
                        "rooms": len(self._rooms),
                    },
                )
            except Exception:  # noqa: BLE001
                logger.warning("Rocket.Chat health heartbeat failed", exc_info=True)
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)


class _RocketChatStreamHandler:
    """Render Nymeria SSE events into Rocket.Chat messages."""

    def __init__(self, bot: NymeriaRocketChatBot, target: RocketChatReplyTarget) -> None:
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
        if len(self._buffer) >= ROCKETCHAT_TEXT_LIMIT:
            await self.flush_text()

    async def on_compacting(self, message: str) -> None:
        await self._bot._send_text(self._target, message)

    async def on_compacted(self, summary: str, messages_removed: int, title: str) -> None:
        detail = title
        if messages_removed:
            detail += f" ({messages_removed} messages summarized)"
        if summary:
            detail += f"\n\n{summary[:900]}"
        await self._bot._send_text(self._target, detail)

    async def on_tool_call(self, name: str, args: Dict[str, Any], call_id: str, count: int) -> None:
        if not self._bot.show_tool_events:
            return
        args_text = ""
        if args:
            try:
                args_text = _json.dumps(args, indent=2, ensure_ascii=False)
            except Exception:  # noqa: BLE001
                args_text = str(args)
        if len(args_text) > 800:
            args_text = args_text[:797] + "..."
        body = f"**Tool:** `{name}`"
        if args_text:
            body += f"\n```json\n{args_text}\n```"
        await self._bot._send_text(self._target, body)

    async def on_tool_result(self, call_id: str, result: str, attachments: List[str]) -> None:
        if self._bot.show_tool_events:
            result_text = str(result or "")
            if len(result_text) > 800:
                result_text = result_text[:797] + "..."
            await self._bot._send_text(self._target, f"**Result:**\n```\n{result_text}\n```")
        for path in attachments:
            await self._bot._send_text(self._target, f"Workspace artifact: `{path}`")

    async def on_tool_reload(self, tools: List[str], ttl: str) -> None:
        names = ", ".join(tools) if tools else "tools"
        await self._bot._send_text(self._target, f"Tool binding: `{names}` ({ttl})")

    async def on_workspace_artifact(self, path: str) -> None:
        await self._bot._send_text(self._target, f"Workspace artifact: `{path}`")

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
