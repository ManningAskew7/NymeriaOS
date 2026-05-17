"""LINE Messaging API webhook client for Nymeria."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import time
from collections.abc import AsyncIterable, Mapping
from dataclasses import dataclass
from typing import Any, Optional, Protocol

import httpx

from .message_splitter import split_line_message as split_message
from .sse_consumer import consume_sse_stream

logger = logging.getLogger(__name__)

LINE_TEXT_LIMIT = 5000
LINE_API_BASE_URL = "https://api.line.me/v2/bot"
SEEN_EVENT_TTL_SECONDS = 10 * 60
SEEN_EVENT_MAX = 5000


class BotAPIError(Exception):
    """User-facing error raised by the Nymeria API adapter."""

    def __init__(self, detail: str, *, status_code: int = 400) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


class LineNymeriaAPI(Protocol):
    """Small API surface the LINE client needs from Nymeria."""

    async def resolve_platform_user(self, provider: str, provider_user_id: str) -> Optional[str]:
        ...

    async def list_chatapp_bindings(self, provider: str) -> list[dict[str, Any]]:
        ...

    async def claim_platform_link_code(
        self,
        *,
        code: str,
        provider: str,
        platform_user_id: str,
    ) -> dict[str, Any]:
        ...

    async def claim_thread_bind_code(
        self,
        *,
        code: str,
        provider: str,
        platform_chat_id: str,
        expected_provider_user_id: str,
    ) -> dict[str, Any]:
        ...

    async def unbind_chatapp_by_chat(
        self,
        *,
        provider: str,
        platform_chat_id: str,
        user_id: Optional[str] = None,
    ) -> dict[str, Any]:
        ...

    async def stop(self, thread_id: str, user_id: Optional[str] = None) -> dict[str, Any]:
        ...

    def chat_stream(
        self,
        message: str,
        thread_id: str,
        user_id: str,
    ) -> AsyncIterable[dict[str, Any]]:
        ...

    async def chat(self, message: str, thread_id: str, user_id: str) -> dict[str, Any]:
        ...


class LineClientProtocol(Protocol):
    """Outbound LINE reply surface."""

    async def send_message(self, target: "LineReplyTarget", text: str) -> None:
        ...

    async def close(self) -> None:
        ...


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value)).strip("-") or "unknown"


def make_platform_user_id(user_id: str) -> str:
    """Return the LINE identity key stored in Nymeria platform links."""
    return str(user_id or "").strip() or "line-unknown"


def line_target_from_source(source_type: str, source: Mapping[str, Any]) -> str:
    """Return the LINE push-message target ID for a webhook source."""
    normalized = str(source_type or source.get("type") or "").lower()
    if normalized == "group":
        return str(source.get("groupId") or "").strip()
    if normalized == "room":
        return str(source.get("roomId") or "").strip()
    return str(source.get("userId") or "").strip()


def make_platform_chat_id(source_type: str, target_id: str) -> str:
    """Return the LINE conversation key used by chat-app bindings."""
    normalized = str(source_type or "user").lower()
    return f"line:{normalized}:{target_id}"


def make_thread_id(source_type: str, target_id: str, *, user_id: str = "") -> str:
    """Generate a native Nymeria thread ID for a LINE conversation."""
    normalized = str(source_type or "user").lower()
    if normalized == "group":
        return f"line_group_{_safe_id(target_id)}"
    if normalized == "room":
        return f"line_room_{_safe_id(target_id)}"
    return f"line_dm_{_safe_id(user_id or target_id)}"


def strip_line_mentions(
    text: str,
    *,
    mention: Optional[Mapping[str, Any]] = None,
    bot_name: Optional[str] = None,
    bot_user_id: Optional[str] = None,
) -> str:
    """Remove LINE bot mentions from visible text."""
    cleaned = str(text or "")
    mentionees = mention.get("mentionees") if isinstance(mention, Mapping) else None
    ranges: list[tuple[int, int]] = []
    if isinstance(mentionees, list):
        for item in mentionees:
            if not isinstance(item, Mapping):
                continue
            is_self = bool(item.get("isSelf"))
            is_bot_user = bot_user_id and str(item.get("userId") or "") == bot_user_id
            if not is_self and not is_bot_user:
                continue
            try:
                index = int(item.get("index"))
                length = int(item.get("length"))
            except (TypeError, ValueError):
                continue
            if index >= 0 and length > 0:
                ranges.append((index, length))
    for index, length in sorted(ranges, reverse=True):
        cleaned = f"{cleaned[:index]} {cleaned[index + length:]}"
    if bot_name:
        cleaned = re.sub(rf"@?{re.escape(bot_name)}", " ", cleaned, flags=re.I)
    return re.sub(r"\s+", " ", cleaned).strip()


def event_mentions_bot(
    event: "LineEvent",
    *,
    bot_name: Optional[str] = None,
    bot_user_id: Optional[str] = None,
) -> bool:
    """Return True when a LINE message explicitly invokes the bot."""
    mentionees = event.mention.get("mentionees") if isinstance(event.mention, Mapping) else None
    if isinstance(mentionees, list):
        for item in mentionees:
            if not isinstance(item, Mapping):
                continue
            if item.get("isSelf") is True:
                return True
            if bot_user_id and str(item.get("userId") or "") == bot_user_id:
                return True
    if bot_name and re.search(rf"(^|\s)@?{re.escape(bot_name)}\b", event.raw_text, re.I):
        return True
    return False


def validate_line_signature(
    raw_body: bytes,
    signature: Optional[str],
    channel_secret: str,
) -> bool:
    """Validate a LINE `x-line-signature` header against the raw request body."""
    if not signature:
        return False
    expected = base64.b64encode(
        hmac.new(
            channel_secret.encode("utf-8"),
            raw_body,
            hashlib.sha256,
        ).digest()
    ).decode("ascii")
    return hmac.compare_digest(expected, signature.strip())


@dataclass(frozen=True)
class LineEvent:
    event_id: str
    event_type: str
    source_type: str
    user_id: str
    target_id: str
    text: str
    raw_text: str
    message_id: str
    reply_token: Optional[str] = None
    timestamp: Optional[int] = None
    mode: str = ""
    destination: str = ""
    mention: Mapping[str, Any] | None = None

    @property
    def is_direct(self) -> bool:
        return self.source_type.lower() == "user"

    @property
    def platform_user_id(self) -> str:
        return make_platform_user_id(self.user_id)


@dataclass(frozen=True)
class LineReplyTarget:
    to: str
    source_type: str
    platform_chat_id: Optional[str] = None


def event_from_payload(
    payload: Mapping[str, Any],
    *,
    destination: str = "",
    bot_name: Optional[str] = None,
    bot_user_id: Optional[str] = None,
) -> Optional[LineEvent]:
    """Normalize a LINE webhook event into a text message event."""
    event_type = str(payload.get("type") or "").strip()
    if event_type != "message":
        return None

    message = payload.get("message")
    source = payload.get("source")
    if not isinstance(message, Mapping) or not isinstance(source, Mapping):
        return None
    if str(message.get("type") or "") != "text":
        return None

    source_type = str(source.get("type") or "").lower()
    target_id = line_target_from_source(source_type, source)
    user_id = str(source.get("userId") or "").strip()
    message_id = str(message.get("id") or "").strip()
    raw_text = str(message.get("text") or "")
    if not source_type or not target_id or not user_id or not message_id or not raw_text:
        return None

    mention = message.get("mention")
    mention_map = mention if isinstance(mention, Mapping) else None
    text = strip_line_mentions(
        raw_text,
        mention=mention_map,
        bot_name=bot_name,
        bot_user_id=bot_user_id or destination,
    )
    if not text:
        return None

    event_id = str(payload.get("webhookEventId") or message_id)
    return LineEvent(
        event_id=event_id,
        event_type=event_type,
        source_type=source_type,
        user_id=user_id,
        target_id=target_id,
        text=text,
        raw_text=raw_text,
        message_id=message_id,
        reply_token=str(payload.get("replyToken") or "") or None,
        timestamp=payload.get("timestamp") if isinstance(payload.get("timestamp"), int) else None,
        mode=str(payload.get("mode") or ""),
        destination=destination,
        mention=mention_map,
    )


class _SeenEventCache:
    """TTL cache for LINE webhook event dedupe."""

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
            stale = [key for key, expiry in self._items.items() if expiry <= now]
            stale += list(self._items)[:stale_count]
        for key in stale:
            self._items.pop(key, None)


class LineRESTClient:
    """Minimal LINE Messaging API client for push-message replies."""

    def __init__(
        self,
        *,
        channel_access_token: str,
        api_base_url: str = LINE_API_BASE_URL,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.channel_access_token = channel_access_token
        self.api_base_url = api_base_url.rstrip("/")
        self._client = http_client or httpx.AsyncClient(timeout=30)
        self._owns_client = http_client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def send_message(self, target: LineReplyTarget, text: str) -> None:
        response = await self._client.post(
            f"{self.api_base_url}/message/push",
            headers={
                "Authorization": f"Bearer {self.channel_access_token}",
                "Content-Type": "application/json",
            },
            json={
                "to": target.to,
                "messages": [{"type": "text", "text": text}],
            },
        )
        response.raise_for_status()


class LineCommand:
    """Small parser for LINE text commands handled before agent routing."""

    def __init__(self, name: str, arg: str = "") -> None:
        self.name = name
        self.arg = arg

    @classmethod
    def parse(
        cls,
        text: str,
        *,
        mention: Optional[Mapping[str, Any]] = None,
        bot_name: Optional[str] = None,
        bot_user_id: Optional[str] = None,
    ) -> Optional["LineCommand"]:
        cleaned = strip_line_mentions(
            text,
            mention=mention,
            bot_name=bot_name,
            bot_user_id=bot_user_id,
        )
        if cleaned.startswith("/"):
            cleaned = cleaned[1:].strip()
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


class NymeriaLineBot:
    """LINE webhook bot - thin client for Nymeria."""

    def __init__(
        self,
        *,
        api: LineNymeriaAPI,
        line_client: LineClientProtocol,
        bot_name: str = "Nymeria",
        bot_user_id: Optional[str] = None,
        respond_mode: str = "mention",
        show_tool_events: bool = False,
        seen_cache: Optional[_SeenEventCache] = None,
    ) -> None:
        self.api = api
        self.line = line_client
        self.bot_name = bot_name
        self.bot_user_id = bot_user_id
        self.respond_mode = respond_mode
        self.show_tool_events = show_tool_events
        self._seen = seen_cache or _SeenEventCache()
        self._bindings: dict[str, str] = {}
        self._binding_users: dict[str, str] = {}
        self._active_chats: set[str] = set()

    async def close(self) -> None:
        await self.line.close()

    async def handle_payload(self, payload: Mapping[str, Any]) -> None:
        destination = str(payload.get("destination") or self.bot_user_id or "")
        events = payload.get("events")
        if not isinstance(events, list):
            return
        for item in events:
            if not isinstance(item, Mapping):
                continue
            event = event_from_payload(
                item,
                destination=destination,
                bot_name=self.bot_name,
                bot_user_id=self.bot_user_id or destination,
            )
            if event is not None:
                await self.handle_event(event)

    async def handle_event(self, event: LineEvent) -> None:
        if self._seen.mark_seen(event.event_id):
            return
        if not event.text:
            return

        await self.refresh_bindings()
        bot_user_id = self.bot_user_id or event.destination
        mentioned = event_mentions_bot(
            event,
            bot_name=self.bot_name,
            bot_user_id=bot_user_id,
        )
        target = LineReplyTarget(
            to=event.target_id,
            source_type=event.source_type,
            platform_chat_id=self._chat_id_for_event(event),
        )
        clean_text = strip_line_mentions(
            event.raw_text,
            mention=event.mention,
            bot_name=self.bot_name,
            bot_user_id=bot_user_id,
        ) or event.text
        command = LineCommand.parse(
            event.raw_text or event.text,
            mention=event.mention,
            bot_name=self.bot_name,
            bot_user_id=bot_user_id,
        )

        accepted = event.is_direct or mentioned or self.respond_mode == "all"
        if not accepted and target.platform_chat_id in self._active_chats:
            accepted = True
        if not accepted:
            return

        if command is not None:
            await self._handle_command(command, event=event, target=target)
            return

        user_id = await self.api.resolve_platform_user("line", event.platform_user_id)
        if user_id is None:
            await self._reject_unlinked(target, event.platform_user_id)
            return

        thread_id = self._resolve_thread_id(event)
        if event.is_direct:
            prompt = clean_text
        else:
            prompt = f"[LINE {event.user_id} in {event.target_id}]\n{clean_text}"
        self._active_chats.add(target.platform_chat_id or self._chat_id_for_event(event))
        await self._stream_to_line(
            message=prompt,
            thread_id=thread_id,
            user_id=user_id,
            target=target,
        )

    async def refresh_bindings(self) -> None:
        try:
            entries = await self.api.list_chatapp_bindings(provider="line")
        except Exception:  # noqa: BLE001
            logger.exception("Failed to refresh LINE bindings; keeping current cache")
            return
        bindings: dict[str, str] = {}
        binding_users: dict[str, str] = {}
        for entry in entries:
            chat_id = str(entry.get("platform_chat_id") or "")
            thread_id = str(entry.get("thread_id") or "")
            user_id = str(entry.get("user_id") or "")
            if chat_id and thread_id:
                bindings[chat_id] = thread_id
                if user_id:
                    binding_users[chat_id] = user_id
        self._bindings = bindings
        self._binding_users = binding_users

    def _resolve_thread_id(self, event: LineEvent) -> str:
        chat_id = self._chat_id_for_event(event)
        if chat_id in self._bindings:
            return self._bindings[chat_id]
        return make_thread_id(event.source_type, event.target_id, user_id=event.user_id)

    def _chat_id_for_event(self, event: LineEvent) -> str:
        return make_platform_chat_id(event.source_type, event.target_id)

    async def _handle_command(
        self,
        command: LineCommand,
        *,
        event: LineEvent,
        target: LineReplyTarget,
    ) -> None:
        if command.name == "link":
            await self._cmd_link(command.arg, event, target)
        elif command.name == "bind":
            await self._cmd_bind(command.arg, event, target)
        elif command.name == "unbind":
            await self._cmd_unbind(event, target)
        elif command.name == "stop":
            await self._cmd_stop(event, target)

    async def _cmd_link(self, code: str, event: LineEvent, target: LineReplyTarget) -> None:
        if not code:
            await self._send_text(target, "Usage: `link <code>`")
            return
        try:
            result = await self.api.claim_platform_link_code(
                code=code,
                provider="line",
                platform_user_id=event.platform_user_id,
            )
        except BotAPIError as exc:
            await self._send_text(target, f"Couldn't link: {exc.detail}")
            return
        await self._send_text(
            target,
            f"Linked this LINE account to Nymeria user `{result.get('user_id')}`.",
        )

    async def _cmd_bind(self, code: str, event: LineEvent, target: LineReplyTarget) -> None:
        if not code:
            await self._send_text(target, "Usage: `bind <code>`")
            return
        chat_id = self._chat_id_for_event(event)
        try:
            result = await self.api.claim_thread_bind_code(
                code=code,
                provider="line",
                platform_chat_id=chat_id,
                expected_provider_user_id=event.platform_user_id,
            )
        except BotAPIError as exc:
            await self._send_text(target, f"Couldn't bind: {exc.detail}")
            return
        thread_id = str(result.get("thread_id") or "")
        user_id = str(result.get("user_id") or "")
        if thread_id:
            self._bindings[chat_id] = thread_id
        if user_id:
            self._binding_users[chat_id] = user_id
        await self._send_text(target, f"Bound this LINE chat to `{thread_id}`.")

    async def _cmd_unbind(self, event: LineEvent, target: LineReplyTarget) -> None:
        user_id = await self.api.resolve_platform_user("line", event.platform_user_id)
        if user_id is None:
            await self._reject_unlinked(target, event.platform_user_id)
            return
        chat_id = self._chat_id_for_event(event)
        try:
            result = await self.api.unbind_chatapp_by_chat(
                provider="line",
                platform_chat_id=chat_id,
                user_id=user_id,
            )
        except BotAPIError as exc:
            await self._send_text(target, f"Couldn't unbind: {exc.detail}")
            return
        if not result.get("unbound"):
            await self._send_text(target, "This LINE chat is not bound.")
            return
        self._bindings.pop(chat_id, None)
        self._binding_users.pop(chat_id, None)
        await self._send_text(
            target,
            "Unbound. Future messages will use the default LINE thread.",
        )

    async def _cmd_stop(self, event: LineEvent, target: LineReplyTarget) -> None:
        user_id = await self.api.resolve_platform_user("line", event.platform_user_id)
        if user_id is None:
            await self._reject_unlinked(target, event.platform_user_id)
            return
        thread_id = self._resolve_thread_id(event)
        try:
            await self.api.stop(thread_id, user_id=user_id)
        except BotAPIError as exc:
            await self._send_text(target, f"Couldn't stop the current run: {exc.detail}")
            return
        await self._send_text(target, "Stopped the current run for this LINE chat.")

    async def _reject_unlinked(self, target: LineReplyTarget, platform_user_id: str) -> None:
        await self._send_text(
            target,
            "This LINE account is not linked to a Nymeria user yet.\n"
            "Use `link <code>` here, or ask an admin to run "
            f"`python3 run.py users link-platform <email> line {platform_user_id}`.",
        )

    async def _stream_to_line(
        self,
        *,
        message: str,
        thread_id: str,
        user_id: str,
        target: LineReplyTarget,
    ) -> None:
        handler = _LineStreamHandler(self, target)
        try:
            await consume_sse_stream(self.api.chat_stream(message, thread_id, user_id), handler)
        except Exception as exc:  # noqa: BLE001
            logger.exception("LINE streaming failed; falling back to sync")
            try:
                data = await self.api.chat(message, thread_id, user_id)
                response = str(data.get("response") or "")
                tool_count = data.get("tool_call_count") or 0
                if tool_count:
                    response += f"\n\nTool calls: {tool_count}"
                await self._send_text(target, response or "(no response)")
            except Exception as fallback_exc:  # noqa: BLE001
                logger.exception("LINE sync fallback failed")
                await self._send_text(
                    target,
                    f"Sorry, I encountered an error: {fallback_exc or exc}",
                )

    async def _send_text(self, target: LineReplyTarget, content: str) -> None:
        for chunk in split_message(content, LINE_TEXT_LIMIT):
            await self.line.send_message(target, chunk)


class _LineStreamHandler:
    """Render Nymeria SSE events into LINE messages."""

    def __init__(self, bot: NymeriaLineBot, target: LineReplyTarget) -> None:
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
        if len(self._buffer) >= LINE_TEXT_LIMIT:
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
        args: dict[str, Any],
        call_id: str,
        count: int,
    ) -> None:
        if self._bot.show_tool_events:
            await self._bot._send_text(self._target, f"Tool #{count}: {name}")

    async def on_tool_result(
        self,
        call_id: str,
        result: str,
        attachments: list[str],
    ) -> None:
        if self._bot.show_tool_events and result:
            await self._bot._send_text(self._target, f"Tool result: {result[:900]}")
        for path in attachments:
            await self._bot._send_text(self._target, f"Workspace artifact: {path}")

    async def on_tool_reload(self, tools: list[str], ttl: str) -> None:
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


def credential_source_present(channel_access_token: Optional[str]) -> bool:
    return bool(channel_access_token and channel_access_token.strip())
