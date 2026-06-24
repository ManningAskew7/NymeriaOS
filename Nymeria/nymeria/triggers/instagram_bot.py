"""Meta Instagram webhook client for two-way Nymeria communication."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import AsyncIterable, Mapping
from dataclasses import dataclass
from typing import Any, Optional, Protocol

import httpx

from .bot_helpers import safe_id as _safe_id
from .message_splitter import split_instagram_message as split_message
from .sse_consumer import consume_sse_stream

logger = logging.getLogger(__name__)

INSTAGRAM_TEXT_LIMIT = 1000
SEEN_MESSAGE_TTL_SECONDS = 10 * 60
SEEN_MESSAGE_MAX = 5000


class BotAPIError(Exception):
    """User-facing error raised by the Nymeria API adapter."""

    def __init__(self, detail: str, *, status_code: int = 400) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


class InstagramNymeriaAPI(Protocol):
    """Small API surface the Instagram client needs from Nymeria."""

    async def resolve_platform_user(self, platform: str, platform_user_id: str) -> Optional[str]:
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


def normalize_ig_user(value: str) -> str:
    """Normalize Instagram-scoped IDs for stable storage."""
    return _safe_id(str(value or "unknown"))


def make_platform_user_id(sender_id: str) -> str:
    """Return the Instagram identity key stored in Nymeria platform links."""
    return normalize_ig_user(sender_id)


def make_platform_chat_id(sender_id: str, ig_id: Optional[str] = None) -> str:
    """Return the Instagram chat key used by Nymeria chat-app bindings."""
    ig_user = normalize_ig_user(sender_id)
    ig_account = _safe_id(ig_id or "")
    if ig_account != "unknown":
        return f"instagram:{ig_account}:{ig_user}"
    return f"instagram:{ig_user}"


def make_thread_id(sender_id: str, ig_id: Optional[str] = None) -> str:
    """Generate a native Nymeria thread ID for an Instagram direct chat."""
    ig_user = normalize_ig_user(sender_id)
    ig_account = _safe_id(ig_id or "")
    if ig_account != "unknown":
        return f"instagram_{ig_account}_{ig_user}"
    return f"instagram_{ig_user}"


@dataclass(frozen=True)
class InstagramInboundMessage:
    message_id: str
    sender_id: str
    ig_id: str
    text: str
    message_type: str
    timestamp: Optional[int] = None


@dataclass(frozen=True)
class InstagramReplyTarget:
    ig_user: str
    ig_id: str


class _SeenMessageCache:
    """TTL cache for webhook message dedupe by Instagram event/message ID."""

    def __init__(
        self,
        *,
        ttl_seconds: int = SEEN_MESSAGE_TTL_SECONDS,
        max_items: int = SEEN_MESSAGE_MAX,
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
            stale = [
                key
                for key, expiry in self._items.items()
                if expiry <= now
            ] + list(self._items)[:stale_count]
        for key in stale:
            self._items.pop(key, None)


class InstagramCommand:
    """Small parser for Instagram text commands handled before agent routing."""

    def __init__(self, name: str, arg: str = "") -> None:
        self.name = name
        self.arg = arg

    @classmethod
    def parse(cls, text: str) -> Optional["InstagramCommand"]:
        cleaned = text.strip()
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


def _mapping_text(value: Any, key: str) -> str:
    if not isinstance(value, Mapping):
        return ""
    raw = value.get(key)
    return str(raw or "").strip()


def _extract_message_event(
    event: Mapping[str, Any],
    *,
    entry_ig_id: str,
) -> Optional[InstagramInboundMessage]:
    sender = event.get("sender")
    recipient = event.get("recipient")
    message = event.get("message")
    if not isinstance(sender, Mapping) or not isinstance(recipient, Mapping):
        return None
    raw_sender_id = str(sender.get("id") or "").strip()
    if not raw_sender_id:
        return None
    sender_id = normalize_ig_user(raw_sender_id)
    ig_id = _safe_id(str(recipient.get("id") or entry_ig_id or ""))
    if not isinstance(message, Mapping):
        return None
    if bool(message.get("is_echo")):
        return None

    text = str(message.get("text") or "").strip()
    if not text:
        quick_reply = message.get("quick_reply")
        text = _mapping_text(quick_reply, "payload")
    message_id = str(message.get("mid") or "").strip()
    if not text or not message_id:
        return None
    timestamp = event.get("timestamp")
    return InstagramInboundMessage(
        message_id=message_id,
        sender_id=sender_id,
        ig_id=ig_id,
        text=text,
        message_type="message",
        timestamp=timestamp if isinstance(timestamp, int) else None,
    )


def _extract_postback_event(
    event: Mapping[str, Any],
    *,
    entry_ig_id: str,
) -> Optional[InstagramInboundMessage]:
    sender = event.get("sender")
    recipient = event.get("recipient")
    postback = event.get("postback")
    if (
        not isinstance(sender, Mapping)
        or not isinstance(recipient, Mapping)
        or not isinstance(postback, Mapping)
    ):
        return None
    raw_sender_id = str(sender.get("id") or "").strip()
    if not raw_sender_id:
        return None
    sender_id = normalize_ig_user(raw_sender_id)
    ig_id = _safe_id(str(recipient.get("id") or entry_ig_id or ""))
    text = _mapping_text(postback, "title") or _mapping_text(postback, "payload")
    if not text:
        return None
    timestamp = event.get("timestamp")
    message_id = str(postback.get("mid") or "").strip()
    if not message_id:
        message_id = f"postback:{ig_id}:{sender_id}:{timestamp or 0}:{_safe_id(text)[:80]}"
    return InstagramInboundMessage(
        message_id=message_id,
        sender_id=sender_id,
        ig_id=ig_id,
        text=text,
        message_type="postback",
        timestamp=timestamp if isinstance(timestamp, int) else None,
    )


def extract_inbound_messages(payload: Mapping[str, Any]) -> list[InstagramInboundMessage]:
    """Extract text-like inbound messages from an Instagram Platform webhook."""
    entries = payload.get("entry")
    if not isinstance(entries, list):
        return []
    out: list[InstagramInboundMessage] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        entry_ig_id = _safe_id(str(entry.get("id") or ""))
        events = entry.get("messaging")
        if not isinstance(events, list):
            continue
        for event in events:
            if not isinstance(event, Mapping):
                continue
            inbound = _extract_message_event(event, entry_ig_id=entry_ig_id)
            if inbound is None:
                inbound = _extract_postback_event(event, entry_ig_id=entry_ig_id)
            if inbound is not None:
                out.append(inbound)
    return out


class InstagramGraphClient:
    """Minimal async Instagram Send API client."""

    def __init__(
        self,
        *,
        access_token: str,
        ig_user_id: Optional[str] = None,
        base_url: str = "https://graph.instagram.com/v23.0",
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.access_token = access_token
        self.ig_user_id = _safe_id(ig_user_id or "")
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=30, write=10, pool=10)
        )
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def send_text(
        self,
        ig_user: str,
        body: str,
        *,
        ig_id: Optional[str] = None,
    ) -> dict[str, Any]:
        ig_account = _safe_id(ig_id or self.ig_user_id)
        if ig_account == "unknown":
            raise BotAPIError("INSTAGRAM_IG_USER_ID is required to send Instagram replies")
        payload = {
            "recipient": {"id": normalize_ig_user(ig_user)},
            "message": {"text": body},
        }
        response = await self._client.post(
            f"{self.base_url}/{ig_account}/messages",
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        response.raise_for_status()
        if not response.content:
            return {}
        data = response.json()
        return data if isinstance(data, dict) else {}


class NymeriaInstagramBot:
    """Instagram Platform webhook processor."""

    def __init__(
        self,
        api: InstagramNymeriaAPI,
        instagram: InstagramGraphClient,
        *,
        show_tool_events: bool = False,
        seen_cache: Optional[_SeenMessageCache] = None,
    ) -> None:
        self.api = api
        self.instagram = instagram
        self.show_tool_events = show_tool_events
        self._seen = seen_cache or _SeenMessageCache()
        self._bindings: dict[str, str] = {}
        self._binding_users: dict[str, str] = {}

    async def refresh_bindings(self) -> None:
        try:
            entries = await self.api.list_chatapp_bindings(provider="instagram")
        except Exception:  # noqa: BLE001
            logger.exception("Failed to refresh Instagram chat-app bindings")
            return
        bindings: dict[str, str] = {}
        users: dict[str, str] = {}
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

    async def handle_webhook(self, payload: Mapping[str, Any]) -> dict[str, int]:
        messages = extract_inbound_messages(payload)
        if not messages:
            return {"processed": 0, "skipped": 0}
        await self.refresh_bindings()
        processed = 0
        skipped = 0
        for message in messages:
            if self._seen.mark_seen(message.message_id):
                skipped += 1
                continue
            await self.handle_message(message)
            processed += 1
        return {"processed": processed, "skipped": skipped}

    async def handle_message(self, message: InstagramInboundMessage) -> None:
        target = InstagramReplyTarget(ig_user=message.sender_id, ig_id=message.ig_id)
        command = InstagramCommand.parse(message.text)
        if command is not None:
            await self._handle_command(command, message=message, target=target)
            return

        platform_user_id = make_platform_user_id(message.sender_id)
        user_id = await self.api.resolve_platform_user("instagram", platform_user_id)
        if user_id is None:
            await self._reject_unlinked(target, platform_user_id)
            return

        thread_id = self._resolve_thread_id(message.sender_id, message.ig_id)
        await self._stream_to_instagram(
            message=message.text,
            thread_id=thread_id,
            user_id=user_id,
            target=target,
        )

    def _resolve_thread_id(self, sender_id: str, ig_id: Optional[str]) -> str:
        chat_id = make_platform_chat_id(sender_id, ig_id)
        return self._bindings.get(chat_id) or make_thread_id(sender_id, ig_id)

    async def _handle_command(
        self,
        command: InstagramCommand,
        *,
        message: InstagramInboundMessage,
        target: InstagramReplyTarget,
    ) -> None:
        if command.name == "link":
            await self._cmd_link(command.arg, message.sender_id, target)
        elif command.name == "bind":
            await self._cmd_bind(command.arg, message.sender_id, message.ig_id, target)
        elif command.name == "unbind":
            await self._cmd_unbind(message.sender_id, message.ig_id, target)
        elif command.name == "stop":
            await self._cmd_stop(message.sender_id, message.ig_id, target)

    async def _cmd_link(
        self,
        code: str,
        sender_id: str,
        target: InstagramReplyTarget,
    ) -> None:
        if not code:
            await self._send_text(target, "Usage: `link <code>`")
            return
        platform_user_id = make_platform_user_id(sender_id)
        try:
            result = await self.api.claim_platform_link_code(
                code=code,
                provider="instagram",
                platform_user_id=platform_user_id,
            )
        except BotAPIError as exc:
            await self._send_text(target, f"Couldn't link: {exc.detail}")
            return
        await self._send_text(
            target,
            f"Linked this Instagram account to Nymeria user `{result.get('user_id')}`.",
        )

    async def _cmd_bind(
        self,
        code: str,
        sender_id: str,
        ig_id: str,
        target: InstagramReplyTarget,
    ) -> None:
        if not code:
            await self._send_text(target, "Usage: `bind <code>`")
            return
        chat_id = make_platform_chat_id(sender_id, ig_id)
        platform_user_id = make_platform_user_id(sender_id)
        try:
            result = await self.api.claim_thread_bind_code(
                code=code,
                provider="instagram",
                platform_chat_id=chat_id,
                expected_provider_user_id=platform_user_id,
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
        await self._send_text(target, f"Bound this Instagram chat to `{thread_id}`.")

    async def _cmd_unbind(
        self,
        sender_id: str,
        ig_id: str,
        target: InstagramReplyTarget,
    ) -> None:
        platform_user_id = make_platform_user_id(sender_id)
        user_id = await self.api.resolve_platform_user("instagram", platform_user_id)
        if user_id is None:
            await self._reject_unlinked(target, platform_user_id)
            return
        chat_id = make_platform_chat_id(sender_id, ig_id)
        try:
            result = await self.api.unbind_chatapp_by_chat(
                provider="instagram",
                platform_chat_id=chat_id,
                user_id=user_id,
            )
        except BotAPIError as exc:
            await self._send_text(target, f"Couldn't unbind: {exc.detail}")
            return
        if not result.get("unbound"):
            await self._send_text(target, "This Instagram chat is not bound.")
            return
        self._bindings.pop(chat_id, None)
        self._binding_users.pop(chat_id, None)
        await self._send_text(
            target,
            "Unbound. Future messages will use the default Instagram thread.",
        )

    async def _cmd_stop(
        self,
        sender_id: str,
        ig_id: str,
        target: InstagramReplyTarget,
    ) -> None:
        platform_user_id = make_platform_user_id(sender_id)
        user_id = await self.api.resolve_platform_user("instagram", platform_user_id)
        if user_id is None:
            await self._reject_unlinked(target, platform_user_id)
            return
        thread_id = self._resolve_thread_id(sender_id, ig_id)
        try:
            await self.api.stop(thread_id, user_id=user_id)
        except BotAPIError as exc:
            await self._send_text(target, f"Couldn't stop the current run: {exc.detail}")
            return
        await self._send_text(target, "Stopped the current run for this Instagram chat.")

    async def _reject_unlinked(
        self,
        target: InstagramReplyTarget,
        platform_user_id: str,
    ) -> None:
        await self._send_text(
            target,
            "This Instagram account is not linked to a Nymeria user yet.\n"
            "Use `link <code>` here, or ask an admin to run "
            f"`python3 run.py users link-platform <email> instagram {platform_user_id}`.",
        )

    async def _stream_to_instagram(
        self,
        *,
        message: str,
        thread_id: str,
        user_id: str,
        target: InstagramReplyTarget,
    ) -> None:
        handler = _InstagramStreamHandler(self, target)
        try:
            await consume_sse_stream(self.api.chat_stream(message, thread_id, user_id), handler)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Instagram streaming failed; falling back to sync")
            try:
                data = await self.api.chat(message, thread_id, user_id)
                response = str(data.get("response") or "")
                tool_count = data.get("tool_call_count") or 0
                if tool_count:
                    response += f"\n\nTool calls: {tool_count}"
                await self._send_text(target, response or "(no response)")
            except Exception as fallback_exc:  # noqa: BLE001
                logger.exception("Instagram sync fallback failed")
                await self._send_text(
                    target,
                    f"Sorry, I encountered an error: {fallback_exc or exc}",
                )

    async def _send_text(self, target: InstagramReplyTarget, content: str) -> None:
        for chunk in split_message(content, INSTAGRAM_TEXT_LIMIT):
            await self.instagram.send_text(target.ig_user, chunk, ig_id=target.ig_id)


class _InstagramStreamHandler:
    """Render Nymeria SSE events into Instagram Send API text messages."""

    def __init__(self, bot: NymeriaInstagramBot, target: InstagramReplyTarget) -> None:
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
        if len(self._buffer) >= INSTAGRAM_TEXT_LIMIT:
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
