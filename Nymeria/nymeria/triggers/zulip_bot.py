"""Zulip bot trigger for two-way Nymeria communication.

Thin client architecture: the bot receives Zulip message events through the
Events API, resolves Zulip users to Nymeria accounts, and calls the Nymeria
REST/SSE API for all agent work.
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Protocol
from urllib.parse import urlsplit

import httpx

from .api_client import NymeriaAPIClient
from .bot_helpers import UserResolver, http_error_detail
from .message_splitter import split_zulip_message as split_message
from .sse_consumer import consume_sse_stream
from ..core.service_health import HEARTBEAT_INTERVAL_SECONDS, write_service_heartbeat

logger = logging.getLogger(__name__)

ZULIP_TEXT_LIMIT = 4000
BINDING_REFRESH_INTERVAL_SECONDS = 60
SEEN_EVENT_MAX = 5000


class ZulipQueueExpired(RuntimeError):
    """Raised when Zulip garbage-collected the long-poll event queue."""


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value)).strip("-") or "unknown"


def _normalize_base_url(base_url: str) -> str:
    return base_url.rstrip("/")


def _api_base_url(base_url: str) -> str:
    normalized = _normalize_base_url(base_url)
    return normalized if normalized.endswith("/api/v1") else f"{normalized}/api/v1"


def make_realm_key(base_url: str) -> str:
    """Return the Zulip realm key used to scope users and chats."""
    parsed = urlsplit(_normalize_base_url(base_url))
    return _safe_id(parsed.netloc or parsed.path or "zulip")


def make_platform_user_id(realm_key: str, user_id: int | str) -> str:
    """Return the Zulip identity key stored in Nymeria platform links."""
    return f"{_safe_id(realm_key)}:{_safe_id(str(user_id))}"


def make_direct_chat_id(realm_key: str, sender_id: int | str) -> str:
    return f"{_safe_id(realm_key)}:direct:{_safe_id(str(sender_id))}"


def make_stream_chat_id(realm_key: str, stream_id: int | str, topic: Optional[str] = None) -> str:
    base = f"{_safe_id(realm_key)}:stream:{_safe_id(str(stream_id))}"
    if topic:
        return f"{base}:{_safe_id(topic)}"
    return base


def make_thread_id(
    realm_key: str,
    *,
    sender_id: int | str | None = None,
    stream_id: int | str | None = None,
    topic: Optional[str] = None,
    is_direct: bool = False,
) -> str:
    """Generate a native Nymeria thread ID for a Zulip conversation."""
    realm = _safe_id(realm_key)
    if is_direct and sender_id is not None:
        return f"zulip_dm_{realm}_{_safe_id(str(sender_id))}"
    base = f"zulip_{realm}_{_safe_id(str(stream_id or 'stream'))}"
    if topic:
        return f"{base}_topic_{_safe_id(topic)}"
    return base


def strip_bot_mention(text: str, *, bot_full_name: Optional[str] = None, bot_email: Optional[str] = None) -> str:
    """Remove common Zulip bot mentions from user text."""
    cleaned = text.strip()
    if bot_full_name:
        cleaned = re.sub(rf"@?\*\*{re.escape(bot_full_name)}\*\*\s*", "", cleaned).strip()
        cleaned = re.sub(rf"(^|\s)@{re.escape(bot_full_name)}\b\s*", " ", cleaned).strip()
    if bot_email:
        cleaned = cleaned.replace(bot_email, "").strip()
    return cleaned


def message_mentions_bot(
    message: "ZulipMessage",
    *,
    bot_full_name: Optional[str] = None,
    bot_email: Optional[str] = None,
) -> bool:
    """Return True when a Zulip message explicitly mentions the bot."""
    if "mentioned" in message.flags:
        return True
    content = message.content or ""
    if bot_full_name and (
        f"@**{bot_full_name}**" in content
        or re.search(rf"(^|\s)@{re.escape(bot_full_name)}\b", content)
    ):
        return True
    return bool(bot_email and bot_email in content)


@dataclass(frozen=True)
class ZulipMessage:
    id: int
    sender_id: int
    sender_email: str
    sender_full_name: str
    type: str
    content: str
    stream_id: Optional[int] = None
    stream_name: Optional[str] = None
    topic: Optional[str] = None
    flags: tuple[str, ...] = ()

    @property
    def is_direct(self) -> bool:
        return self.type in {"direct", "private"}


@dataclass(frozen=True)
class ZulipReplyTarget:
    message_type: str
    to: str
    topic: Optional[str] = None


class ZulipClientProtocol(Protocol):
    async def get_me(self) -> Mapping[str, Any]:
        """Return the authenticated Zulip bot user."""

    async def register_queue(self) -> Mapping[str, Any]:
        """Register a Zulip event queue."""

    async def get_events(self, *, queue_id: str, last_event_id: int) -> Mapping[str, Any]:
        """Long-poll Zulip events."""

    async def send_message(self, *, message_type: str, to: str, content: str, topic: Optional[str] = None) -> Mapping[str, Any]:
        """Send a Zulip message."""

    async def upload_file(self, *, filename: str, data: bytes, content_type: str) -> str:
        """Upload a file and return its server uri."""

    async def close(self) -> None:
        """Close client resources."""


def message_from_event(event: Mapping[str, Any]) -> Optional[ZulipMessage]:
    """Normalize a Zulip `message` event into a message object."""
    if event.get("type") != "message":
        return None
    raw = event.get("message")
    if not isinstance(raw, Mapping):
        return None
    raw_id = raw.get("id")
    raw_sender_id = raw.get("sender_id")
    if raw_id is None or raw_sender_id is None:
        return None
    try:
        message_id = int(raw_id)
        sender_id = int(raw_sender_id)
    except (TypeError, ValueError):
        return None
    flags = raw.get("flags")
    if not isinstance(flags, list):
        flags = []
    message_type = str(raw.get("type") or "")
    topic = str(raw.get("topic") or raw.get("subject") or "") or None
    stream_id: Optional[int]
    try:
        stream_id = int(raw["stream_id"]) if raw.get("stream_id") is not None else None
    except (TypeError, ValueError):
        stream_id = None
    stream_name: Optional[str] = None
    display_recipient = raw.get("display_recipient")
    if isinstance(display_recipient, str):
        stream_name = display_recipient
    return ZulipMessage(
        id=message_id,
        sender_id=sender_id,
        sender_email=str(raw.get("sender_email") or ""),
        sender_full_name=str(raw.get("sender_full_name") or ""),
        type=message_type,
        content=str(raw.get("content") or "").strip(),
        stream_id=stream_id,
        stream_name=stream_name,
        topic=topic,
        flags=tuple(str(flag) for flag in flags),
    )


class ZulipHTTPClient:
    """Minimal async Zulip REST/Event API wrapper."""

    def __init__(self, base_url: str, email: str, api_key: str) -> None:
        self.base_url = _normalize_base_url(base_url)
        self.api_base_url = _api_base_url(base_url)
        self.email = email
        self.api_key = api_key
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=120, write=10, pool=10))

    async def close(self) -> None:
        await self._client.aclose()

    async def get_me(self) -> Mapping[str, Any]:
        return await self._request("GET", "/users/me")

    async def register_queue(self) -> Mapping[str, Any]:
        return await self._request(
            "POST",
            "/register",
            data={
                "event_types": _json.dumps(["message"]),
                "apply_markdown": "false",
                "client_gravatar": "false",
            },
        )

    async def get_events(self, *, queue_id: str, last_event_id: int) -> Mapping[str, Any]:
        return await self._request(
            "GET",
            "/events",
            params={"queue_id": queue_id, "last_event_id": last_event_id},
        )

    async def send_message(
        self,
        *,
        message_type: str,
        to: str,
        content: str,
        topic: Optional[str] = None,
    ) -> Mapping[str, Any]:
        data: dict[str, Any] = {"type": message_type, "to": to, "content": content}
        if topic:
            data["topic"] = topic
        return await self._request("POST", "/messages", data=data)

    async def upload_file(self, *, filename: str, data: bytes, content_type: str) -> str:
        """Upload a file to Zulip and return its server uri (e.g. /user_uploads/...)."""
        response = await self._client.post(
            f"{self.api_base_url}/user_uploads",
            auth=(self.email, self.api_key),
            files={"file": (filename, data, content_type or "application/octet-stream")},
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        payload = response.json()
        uri = payload.get("uri") if isinstance(payload, Mapping) else ""
        if not uri:
            raise RuntimeError("Zulip upload did not return a uri")
        return str(uri)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        data: Optional[dict] = None,
        params: Optional[dict] = None,
    ) -> Mapping[str, Any]:
        response = await self._client.request(
            method,
            f"{self.api_base_url}{path}",
            auth=(self.email, self.api_key),
            data=data,
            params=params,
            headers={"Accept": "application/json"},
        )
        payload: Mapping[str, Any] = {}
        if response.content:
            try:
                parsed = response.json()
                if isinstance(parsed, Mapping):
                    payload = parsed
            except ValueError:
                payload = {}
        if payload.get("code") == "BAD_EVENT_QUEUE_ID":
            raise ZulipQueueExpired(str(payload.get("msg") or "Zulip event queue expired"))
        response.raise_for_status()
        if payload.get("result") == "error":
            raise RuntimeError(str(payload.get("msg") or "Zulip API error"))
        return payload


class ZulipCommand:
    """Small parser for Zulip text commands handled before agent routing."""

    def __init__(self, name: str, arg: str = "") -> None:
        self.name = name
        self.arg = arg

    @classmethod
    def parse(
        cls,
        text: str,
        *,
        bot_full_name: Optional[str] = None,
        bot_email: Optional[str] = None,
    ) -> Optional["ZulipCommand"]:
        cleaned = strip_bot_mention(text, bot_full_name=bot_full_name, bot_email=bot_email).strip()
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


class NymeriaZulipBot:
    """Zulip event-queue bot — thin client for Nymeria."""

    def __init__(
        self,
        api: NymeriaAPIClient,
        *,
        base_url: str,
        email: str,
        api_key: str,
        respond_mode: str = "mention",
        show_tool_events: bool = False,
        zulip_client: Optional[ZulipClientProtocol] = None,
    ) -> None:
        self.api = api
        self.base_url = _normalize_base_url(base_url)
        self.realm_key = make_realm_key(base_url)
        self.respond_mode = respond_mode if respond_mode in {"mention", "all"} else "mention"
        self.show_tool_events = show_tool_events
        self.zulip = zulip_client or ZulipHTTPClient(base_url, email, api_key)
        self.bot_user_id: Optional[int] = None
        self.bot_email = email
        self.bot_full_name: Optional[str] = None
        self._running = False
        self._queue_id: Optional[str] = None
        self._last_event_id = -1
        self._seen_event_ids: set[int] = set()
        self._bindings: Dict[str, str] = {}
        self._binding_users: Dict[str, str] = {}
        self._user_resolver = UserResolver(self.api, "zulip", logger=logger)
        self._health_task: Optional[asyncio.Task] = None
        self._bindings_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        identity = await self.zulip.get_me()
        raw_user_id = identity.get("user_id")
        if raw_user_id is None:
            raise RuntimeError("Zulip account identity could not be resolved.") from None
        try:
            self.bot_user_id = int(raw_user_id)
        except (TypeError, ValueError):
            raise RuntimeError("Zulip account identity could not be resolved.") from None
        self.bot_email = str(identity.get("email") or self.bot_email)
        self.bot_full_name = str(identity.get("full_name") or "") or None
        await self._refresh_bindings()
        await self._register_queue()
        self._running = True
        self._bindings_task = asyncio.create_task(self._bindings_refresh_loop())
        self._health_task = asyncio.create_task(self._health_heartbeat_loop())
        logger.info(
            "Zulip bot authenticated as %s on %s",
            self.bot_email,
            self.realm_key,
        )
        await self._poll_loop()

    async def close(self) -> None:
        self._running = False
        for task in (self._bindings_task, self._health_task):
            if task:
                task.cancel()
        await self.zulip.close()
        await self.api.close()

    def run(self) -> None:
        """Synchronous entry point used by run.py."""
        asyncio.run(self.start())

    async def _register_queue(self) -> None:
        data = await self.zulip.register_queue()
        self._queue_id = str(data.get("queue_id") or "")
        self._last_event_id = int(data.get("last_event_id") or -1)
        if not self._queue_id:
            raise RuntimeError("Zulip register_queue did not return queue_id.")

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                if not self._queue_id:
                    await self._register_queue()
                data = await self.zulip.get_events(
                    queue_id=self._queue_id or "",
                    last_event_id=self._last_event_id,
                )
            except ZulipQueueExpired:
                logger.info("Zulip event queue expired; registering a new queue")
                await self._register_queue()
                continue
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Zulip event poll failed")
                await asyncio.sleep(5)
                continue
            events = data.get("events")
            if not isinstance(events, list):
                continue
            for event in events:
                if not isinstance(event, Mapping):
                    continue
                event_id = event.get("id")
                if isinstance(event_id, int):
                    self._last_event_id = max(self._last_event_id, event_id)
                await self.handle_zulip_event(event)

    async def resolve_user_id(self, zulip_user_id: int | str) -> Optional[str]:
        platform_user_id = make_platform_user_id(self.realm_key, zulip_user_id)
        return await self._user_resolver.resolve(platform_user_id)

    async def handle_zulip_event(self, event: Mapping[str, Any]) -> None:
        """Handle one Zulip event frame."""
        event_id = event.get("id")
        if isinstance(event_id, int) and self._mark_seen(event_id):
            return
        message = message_from_event(event)
        if message is None:
            return
        if self.bot_user_id is not None and message.sender_id == self.bot_user_id:
            return
        if self.bot_email and message.sender_email == self.bot_email:
            return
        if not message.content.strip():
            return

        mentioned = message_mentions_bot(
            message,
            bot_full_name=self.bot_full_name,
            bot_email=self.bot_email,
        )
        should_respond = message.is_direct or self.respond_mode == "all" or mentioned
        if not should_respond:
            return

        clean_text = strip_bot_mention(
            message.content,
            bot_full_name=self.bot_full_name,
            bot_email=self.bot_email,
        )
        target = self._reply_target(message)
        command = ZulipCommand.parse(
            clean_text,
            bot_full_name=self.bot_full_name,
            bot_email=self.bot_email,
        )
        if command is not None:
            await self._handle_command(command, message=message, target=target)
            return
        if not clean_text.strip():
            return

        user_id = await self.resolve_user_id(message.sender_id)
        if user_id is None:
            await self._reject_unlinked(target, message.sender_id)
            return

        thread_id = self._resolve_thread_id(message)
        prompt = clean_text
        if not message.is_direct:
            sender = message.sender_full_name or str(message.sender_id)
            prompt = f"[Zulip {sender} in {message.stream_name or message.stream_id}#{message.topic or ''}]\n{clean_text}"

        await self._stream_to_zulip(
            message=prompt,
            thread_id=thread_id,
            user_id=user_id,
            target=target,
        )

    def _reply_target(self, message: ZulipMessage) -> ZulipReplyTarget:
        if message.is_direct:
            return ZulipReplyTarget(
                message_type="direct",
                to=_json.dumps([message.sender_id]),
            )
        return ZulipReplyTarget(
            message_type="stream",
            to=message.stream_name or str(message.stream_id or ""),
            topic=message.topic or "Nymeria",
        )

    def _resolve_thread_id(self, message: ZulipMessage) -> str:
        candidates: list[str] = []
        if message.is_direct:
            candidates.append(make_direct_chat_id(self.realm_key, message.sender_id))
        else:
            if message.stream_id is not None and message.topic:
                candidates.append(make_stream_chat_id(self.realm_key, message.stream_id, message.topic))
            if message.stream_id is not None:
                candidates.append(make_stream_chat_id(self.realm_key, message.stream_id))
        for key in candidates:
            bound = self._bindings.get(key)
            if bound:
                return bound
        return make_thread_id(
            self.realm_key,
            sender_id=message.sender_id,
            stream_id=message.stream_id,
            topic=message.topic,
            is_direct=message.is_direct,
        )

    def _mark_seen(self, event_id: int) -> bool:
        if event_id in self._seen_event_ids:
            return True
        self._seen_event_ids.add(event_id)
        if len(self._seen_event_ids) > SEEN_EVENT_MAX:
            for old_id in list(self._seen_event_ids)[: SEEN_EVENT_MAX // 2]:
                self._seen_event_ids.discard(old_id)
        return False

    async def _handle_command(
        self,
        command: ZulipCommand,
        *,
        message: ZulipMessage,
        target: ZulipReplyTarget,
    ) -> None:
        if command.name == "link":
            await self._cmd_link(command.arg, message.sender_id, target)
        elif command.name == "bind":
            await self._cmd_bind(command.arg, message, target)
        elif command.name == "unbind":
            await self._cmd_unbind(message, target)
        elif command.name == "stop":
            await self._cmd_stop(message, target)

    async def _cmd_link(
        self,
        code: str,
        sender_id: int,
        target: ZulipReplyTarget,
    ) -> None:
        if not code:
            await self._send_text(target, "Usage: `link <code>`")
            return
        platform_user_id = make_platform_user_id(self.realm_key, sender_id)
        try:
            result = await self.api.claim_platform_link_code(
                code=code,
                provider="zulip",
                platform_user_id=platform_user_id,
            )
        except httpx.HTTPStatusError as exc:
            await self._send_text(target, f"Couldn't link: {http_error_detail(exc)}")
            return
        self._user_resolver.invalidate(platform_user_id)
        await self._send_text(target, f"Linked this Zulip account to Nymeria user `{result.get('user_id')}`.")

    async def _cmd_bind(self, code: str, message: ZulipMessage, target: ZulipReplyTarget) -> None:
        if not code:
            await self._send_text(target, "Usage: `bind <code>`")
            return
        chat_id = (
            make_direct_chat_id(self.realm_key, message.sender_id)
            if message.is_direct
            else make_stream_chat_id(self.realm_key, message.stream_id or "", message.topic)
        )
        platform_user_id = make_platform_user_id(self.realm_key, message.sender_id)
        try:
            result = await self.api.claim_thread_bind_code(
                code=code,
                provider="zulip",
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
        thread_id = str(result.get("thread_id") or "")
        user_id = str(result.get("user_id") or "")
        if thread_id:
            self._bindings[chat_id] = thread_id
        if user_id:
            self._binding_users[chat_id] = user_id
        await self._send_text(target, f"Bound this Zulip conversation to `{thread_id}`.")

    async def _cmd_unbind(self, message: ZulipMessage, target: ZulipReplyTarget) -> None:
        user_id = await self.resolve_user_id(message.sender_id)
        if user_id is None:
            await self._reject_unlinked(target, message.sender_id)
            return
        chat_id = (
            make_direct_chat_id(self.realm_key, message.sender_id)
            if message.is_direct
            else make_stream_chat_id(self.realm_key, message.stream_id or "", message.topic)
        )
        try:
            result = await self.api.unbind_chatapp_by_chat(
                provider="zulip",
                platform_chat_id=chat_id,
                user_id=user_id,
            )
        except httpx.HTTPStatusError as exc:
            await self._send_text(target, f"Couldn't unbind: {http_error_detail(exc)}")
            return
        if not result.get("unbound"):
            await self._send_text(target, "This Zulip conversation is not bound.")
            return
        self._bindings.pop(chat_id, None)
        self._binding_users.pop(chat_id, None)
        await self._send_text(target, "Unbound. Future messages will use the default Zulip thread.")

    async def _cmd_stop(self, message: ZulipMessage, target: ZulipReplyTarget) -> None:
        user_id = await self.resolve_user_id(message.sender_id)
        if user_id is None:
            await self._reject_unlinked(target, message.sender_id)
            return
        thread_id = self._resolve_thread_id(message)
        try:
            await self.api.stop(thread_id, user_id=user_id)
        except httpx.HTTPStatusError as exc:
            await self._send_text(target, f"Couldn't stop the current run: {http_error_detail(exc)}")
            return
        await self._send_text(target, "Stopped the current run for this Zulip conversation.")

    async def _reject_unlinked(self, target: ZulipReplyTarget, sender_id: int) -> None:
        platform_user_id = make_platform_user_id(self.realm_key, sender_id)
        await self._send_text(
            target,
            "This Zulip account is not linked to a Nymeria user yet.\n"
            f"Use `link <code>` here, or ask an admin to run "
            f"`python3 run.py users link-platform <email> zulip {platform_user_id}`.",
        )

    async def _stream_to_zulip(
        self,
        *,
        message: str,
        thread_id: str,
        user_id: str,
        target: ZulipReplyTarget,
    ) -> None:
        handler = _ZulipStreamHandler(self, target)
        try:
            await consume_sse_stream(
                self.api.chat_stream(message, thread_id, user_id),
                handler,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Zulip streaming failed; falling back to sync")
            try:
                data = await self.api.chat(message, thread_id, user_id)
                response = str(data.get("response") or "")
                tool_count = data.get("tool_call_count") or 0
                if tool_count:
                    response += f"\n\nTool calls: {tool_count}"
                await self._send_text(target, response or "(no response)")
            except Exception as fallback_exc:  # noqa: BLE001
                logger.exception("Zulip sync fallback failed")
                await self._send_text(
                    target,
                    f"Sorry, I encountered an error: {fallback_exc or exc}",
                )

    async def _send_text(self, target: ZulipReplyTarget, content: str) -> None:
        for chunk in split_message(content, ZULIP_TEXT_LIMIT):
            await self.zulip.send_message(
                message_type=target.message_type,
                to=target.to,
                content=chunk,
                topic=target.topic,
            )

    async def _send_workspace_attachment(self, target: ZulipReplyTarget, path: str) -> None:
        """Download a generated workspace file, upload it to Zulip, and post a link."""
        result = await self.api.download_workspace_file(path)
        if result is None:
            await self._send_text(target, f"Workspace artifact: `{path}`")
            return
        raw_bytes, filename, content_type = result
        try:
            uri = await self.zulip.upload_file(
                filename=filename, data=raw_bytes, content_type=content_type
            )
            await self.zulip.send_message(
                message_type=target.message_type,
                to=target.to,
                content=f"[{filename}]({uri})",
                topic=target.topic,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to upload Zulip workspace attachment %s: %s", path, e)
            await self._send_text(target, f"Workspace artifact: `{path}`")

    async def _refresh_bindings(self) -> None:
        try:
            entries = await self.api.list_chatapp_bindings(provider="zulip")
        except Exception:  # noqa: BLE001
            logger.exception("Failed to refresh Zulip chat-app bindings; keeping current cache")
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
        logger.debug("Zulip chat-app bindings refreshed: %d entries", len(bindings))

    async def _bindings_refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(BINDING_REFRESH_INTERVAL_SECONDS)
            await self._refresh_bindings()

    async def _health_heartbeat_loop(self) -> None:
        while True:
            try:
                api_ok = await self.api.health()
                write_service_heartbeat(
                    "zulip-bot",
                    status="ok" if api_ok and self._running else "unhealthy",
                    details={
                        "api": api_ok,
                        "realm": self.realm_key,
                        "bot_user_id": self.bot_user_id,
                    },
                )
            except Exception:  # noqa: BLE001
                logger.warning("Zulip health heartbeat failed", exc_info=True)
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)


class _ZulipStreamHandler:
    """Render Nymeria SSE events into Zulip messages."""

    def __init__(self, bot: NymeriaZulipBot, target: ZulipReplyTarget) -> None:
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
        if len(self._buffer) >= ZULIP_TEXT_LIMIT:
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
            await self._bot._send_workspace_attachment(self._target, path)

    async def on_tool_reload(self, tools: List[str], ttl: str) -> None:
        names = ", ".join(tools) if tools else "tools"
        await self._bot._send_text(self._target, f"Tool binding: `{names}` ({ttl})")

    async def on_workspace_artifact(self, path: str) -> None:
        await self._bot._send_workspace_attachment(self._target, path)

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
