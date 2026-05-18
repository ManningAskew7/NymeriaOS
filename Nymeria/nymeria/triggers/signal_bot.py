"""Signal bot trigger for two-way Nymeria communication.

Signal does not provide a first-party hosted bot API. This integration talks
to a user-managed signal-cli-rest-api daemon in JSON-RPC/SSE mode and keeps the
Nymeria side as a thin client: inbound Signal envelopes are translated into
Nymeria REST/SSE calls, while all conversation state remains in the API service.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterable, Mapping
from dataclasses import dataclass
from typing import Any, Optional, Protocol
from urllib.parse import quote

import httpx

from .api_client import NymeriaAPIClient
from .bot_helpers import UserResolver, http_error_detail
from .message_splitter import split_signal_message as split_message
from .sse_consumer import consume_sse_stream
from ..core.service_health import HEARTBEAT_INTERVAL_SECONDS, write_service_heartbeat

logger = logging.getLogger(__name__)

SIGNAL_TEXT_LIMIT = 8000
BINDING_REFRESH_INTERVAL_SECONDS = 60
SEEN_EVENT_TTL_SECONDS = 10 * 60
SEEN_EVENT_MAX = 5000
OBJECT_REPLACEMENT = "\uFFFC"


class SignalNymeriaAPI(Protocol):
    """Small API surface the Signal client needs from Nymeria."""

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

    async def health(self) -> bool:
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

    async def close(self) -> None:
        ...


class SignalClientProtocol(Protocol):
    """Outbound and receive surface for signal-cli-rest-api."""

    async def check(self) -> bool:
        ...

    async def send_text(self, target: "SignalReplyTarget", text: str) -> dict[str, Any]:
        ...

    def stream_events(self, account: str) -> AsyncGenerator[Mapping[str, str], None]:
        ...

    async def close(self) -> None:
        ...


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value)).strip("-") or "unknown"


def normalize_phone_number(value: str) -> str:
    """Normalize a Signal phone number for identity storage."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    digits = re.sub(r"\D+", "", raw)
    if not digits:
        return _safe_id(raw)
    return f"+{digits}" if raw.startswith("+") else digits


def _looks_like_uuid(value: str) -> bool:
    return bool(
        re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            value.strip(),
            flags=re.I,
        )
        or re.fullmatch(r"[0-9a-f]{32}", value.strip(), flags=re.I)
    )


def make_platform_user_id(
    source_number: Optional[str] = None,
    source_uuid: Optional[str] = None,
) -> str:
    """Return the Signal identity key stored in Nymeria platform links."""
    if source_number:
        normalized = normalize_phone_number(source_number)
        if normalized:
            return normalized
    if source_uuid:
        raw_uuid = str(source_uuid).strip().lower()
        if raw_uuid:
            return f"uuid:{raw_uuid}"
    return "signal-unknown"


def normalize_signal_allow_entry(value: str) -> str:
    """Normalize one SIGNAL_ALLOWED_USERS entry for comparison."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw == "*":
        return "*"
    stripped = re.sub(r"^signal:", "", raw, flags=re.I).strip()
    if stripped.lower().startswith("uuid:"):
        return f"uuid:{stripped[5:].strip().lower()}"
    if _looks_like_uuid(stripped):
        return f"uuid:{stripped.lower()}"
    return normalize_phone_number(stripped)


def make_platform_chat_id(
    *,
    sender_id: Optional[str] = None,
    group_id: Optional[str] = None,
) -> str:
    """Return the Signal conversation key used by chat-app bindings."""
    if group_id:
        return f"signal:group:{str(group_id).strip()}"
    return f"signal:dm:{sender_id or 'signal-unknown'}"


def make_thread_id(*, sender_id: Optional[str] = None, group_id: Optional[str] = None) -> str:
    """Generate a native Nymeria thread ID for a Signal conversation."""
    if group_id:
        return f"signal_group_{_safe_id(str(group_id))}"
    return f"signal_dm_{_safe_id(sender_id or 'signal-unknown')}"


def _csv_set(value: str | list[str] | tuple[str, ...] | None) -> set[str]:
    if not value:
        return set()
    if isinstance(value, str):
        raw_items = value.replace("\n", ",").split(",")
    else:
        raw_items = value
    return {str(item).strip() for item in raw_items if str(item).strip()}


def render_signal_mentions(message: str, mentions: Any) -> str:
    """Render Signal mention placeholders into visible @number/@uuid text."""
    text = str(message or "")
    if not text or not isinstance(mentions, list):
        return text
    replacements: list[tuple[int, int, str]] = []
    for item in mentions:
        if not isinstance(item, Mapping):
            continue
        identifier = item.get("number") or item.get("uuid") or item.get("name")
        if not identifier:
            continue
        raw_start = item.get("start")
        raw_length = item.get("length")
        if raw_start is None or raw_length is None:
            continue
        try:
            start = int(raw_start)
            length = int(raw_length)
        except (TypeError, ValueError):
            continue
        if start < 0 or length <= 0:
            continue
        end = min(len(text), start + length)
        if start >= end or OBJECT_REPLACEMENT not in text[start:end]:
            continue
        replacements.append((start, end, f"@{identifier}"))
    for start, end, value in sorted(replacements, reverse=True):
        text = text[:start] + value + text[end:]
    return text


def signal_mentions_bot(
    text: str,
    mentions: Any,
    *,
    account: Optional[str],
    account_uuid: Optional[str] = None,
) -> bool:
    """Return True when a Signal message explicitly mentions this bot account."""
    normalized_account = normalize_phone_number(account or "") if account else ""
    normalized_uuid = str(account_uuid or "").strip().lower()
    if isinstance(mentions, list):
        for item in mentions:
            if not isinstance(item, Mapping):
                continue
            number = normalize_phone_number(str(item.get("number") or ""))
            uuid_value = str(item.get("uuid") or "").strip().lower()
            if normalized_account and number == normalized_account:
                return True
            if normalized_uuid and uuid_value == normalized_uuid:
                return True

    candidates = {str(account or "").strip(), normalized_account}
    if account_uuid:
        candidates.add(str(account_uuid).strip())
    return any(value and (f"@{value}" in text or value in text) for value in candidates)


def strip_signal_mention(text: str, *, account: Optional[str], account_uuid: Optional[str] = None) -> str:
    """Remove simple Signal bot account mentions from visible text."""
    cleaned = str(text or "")
    for value in {
        str(account or "").strip(),
        normalize_phone_number(account or "") if account else "",
        str(account_uuid or "").strip(),
    }:
        if not value:
            continue
        cleaned = cleaned.replace(f"@{value}", " ")
        cleaned = cleaned.replace(value, " ")
    return re.sub(r"\s+", " ", cleaned).strip()


@dataclass(frozen=True)
class SignalReplyTarget:
    recipient: Optional[str] = None
    group_id: Optional[str] = None

    @property
    def is_group(self) -> bool:
        return bool(self.group_id)


@dataclass(frozen=True)
class SignalInboundMessage:
    event_id: str
    sender_id: str
    sender_recipient: str
    sender_name: str
    text: str
    raw_text: str
    target: SignalReplyTarget
    group_id: Optional[str] = None
    group_name: Optional[str] = None
    timestamp: Optional[int] = None
    was_mentioned: bool = False

    @property
    def is_group(self) -> bool:
        return bool(self.group_id)

    @property
    def platform_chat_id(self) -> str:
        return make_platform_chat_id(sender_id=self.sender_id, group_id=self.group_id)


class _SeenEventCache:
    """TTL cache for Signal event dedupe by envelope timestamp/sender."""

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
            stale = [
                key
                for key, expiry in self._items.items()
                if expiry <= now
            ] + list(self._items)[:stale_count]
        for key in stale:
            self._items.pop(key, None)


class SignalCommand:
    """Small parser for Signal text commands handled before agent routing."""

    def __init__(self, name: str, arg: str = "") -> None:
        self.name = name
        self.arg = arg

    @classmethod
    def parse(
        cls,
        text: str,
        *,
        account: Optional[str] = None,
        account_uuid: Optional[str] = None,
    ) -> Optional["SignalCommand"]:
        cleaned = strip_signal_mention(text, account=account, account_uuid=account_uuid)
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


def _envelope_from_receive_payload(payload: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    envelope = payload.get("envelope", payload)
    return envelope if isinstance(envelope, Mapping) else None


def _data_message_from_envelope(envelope: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    data_message = envelope.get("dataMessage")
    if isinstance(data_message, Mapping):
        return data_message
    edit = envelope.get("editMessage")
    if isinstance(edit, Mapping):
        edited = edit.get("dataMessage")
        if isinstance(edited, Mapping):
            return edited
    return None


def _timestamp_value(envelope: Mapping[str, Any], data_message: Mapping[str, Any]) -> Optional[int]:
    for source in (envelope, data_message):
        value = source.get("timestamp")
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _signal_event_id(
    *,
    sender_id: str,
    group_id: Optional[str],
    timestamp: Optional[int],
    text: str,
) -> str:
    if timestamp:
        return f"{sender_id}:{group_id or 'dm'}:{timestamp}"
    fallback = uuid.uuid5(uuid.NAMESPACE_URL, f"{sender_id}:{group_id or 'dm'}:{text}")
    return f"{sender_id}:{group_id or 'dm'}:{fallback.hex}"


def inbound_message_from_payload(
    payload: Mapping[str, Any],
    *,
    account: Optional[str] = None,
    account_uuid: Optional[str] = None,
) -> Optional[SignalInboundMessage]:
    """Normalize a signal-cli-rest-api receive payload into a text event."""
    if payload.get("exception"):
        logger.warning("Signal receive exception: %s", payload.get("exception"))

    envelope = _envelope_from_receive_payload(payload)
    if not envelope:
        return None

    # signal-cli may include syncMessage as null; property existence is enough
    # to classify it as an own-device sync/receipt event rather than inbound user text.
    if "syncMessage" in envelope:
        return None

    source_number = str(envelope.get("sourceNumber") or "").strip()
    source_uuid = str(envelope.get("sourceUuid") or "").strip()
    source = str(envelope.get("source") or "").strip()
    source_is_uuid = _looks_like_uuid(source)
    sender_number = source_number or (
        source if source and not source_is_uuid and (source.startswith("+") or re.search(r"\d", source)) else ""
    )
    sender_uuid = source_uuid or (source if source_is_uuid else "")
    sender_id = make_platform_user_id(sender_number, sender_uuid)
    if not sender_id or sender_id == "signal-unknown":
        return None

    normalized_account = normalize_phone_number(account or "") if account else ""
    if normalized_account and sender_id == normalized_account:
        return None
    if account_uuid and sender_id == f"uuid:{account_uuid.strip().lower()}":
        return None

    data_message = _data_message_from_envelope(envelope)
    if not data_message:
        return None

    raw_text = str(data_message.get("message") or "")
    mentions = data_message.get("mentions")
    text = render_signal_mentions(raw_text, mentions).strip()
    if not text:
        return None

    group_info = data_message.get("groupInfo")
    if not isinstance(group_info, Mapping):
        group_info = {}
    group_id = str(group_info.get("groupId") or "").strip() or None
    group_name = str(group_info.get("groupName") or "").strip() or None
    timestamp = _timestamp_value(envelope, data_message)
    sender_recipient = sender_number or sender_uuid or sender_id.removeprefix("uuid:")
    target = (
        SignalReplyTarget(group_id=group_id)
        if group_id
        else SignalReplyTarget(recipient=sender_recipient)
    )

    return SignalInboundMessage(
        event_id=_signal_event_id(
            sender_id=sender_id,
            group_id=group_id,
            timestamp=timestamp,
            text=text,
        ),
        sender_id=sender_id,
        sender_recipient=sender_recipient,
        sender_name=str(envelope.get("sourceName") or "").strip(),
        text=strip_signal_mention(text, account=account, account_uuid=account_uuid),
        raw_text=text,
        target=target,
        group_id=group_id,
        group_name=group_name,
        timestamp=timestamp,
        was_mentioned=signal_mentions_bot(
            text,
            mentions,
            account=account,
            account_uuid=account_uuid,
        ),
    )


class SignalCliRestClient:
    """Minimal async wrapper for signal-cli-rest-api JSON-RPC/SSE mode."""

    def __init__(
        self,
        base_url: str,
        *,
        account: str,
        timeout: float = 30.0,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.base_url = self._normalize_base_url(base_url)
        self.account = account
        self.timeout = timeout
        self._client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=timeout, write=10, pool=10),
        )

    @staticmethod
    def _normalize_base_url(value: str) -> str:
        trimmed = str(value or "").strip()
        if not trimmed:
            raise ValueError("Signal base URL is required")
        if not re.match(r"^https?://", trimmed, flags=re.I):
            trimmed = f"http://{trimmed}"
        return trimmed.rstrip("/")

    async def close(self) -> None:
        await self._client.aclose()

    async def check(self) -> bool:
        response = await self._client.get(f"{self.base_url}/api/v1/check", timeout=10.0)
        return 200 <= response.status_code < 300

    async def rpc(self, method: str, params: Mapping[str, Any]) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": dict(params),
            "id": f"{method}-{uuid.uuid4().hex}",
        }
        response = await self._client.post(
            f"{self.base_url}/api/v1/rpc",
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        if response.status_code == 201 or not response.content:
            return None
        data = response.json()
        if isinstance(data, Mapping) and data.get("error"):
            raise RuntimeError(f"Signal RPC {method} error: {data['error']}")
        return data.get("result") if isinstance(data, Mapping) else data

    async def send_text(self, target: SignalReplyTarget, text: str) -> dict[str, Any]:
        params: dict[str, Any] = {
            "account": self.account,
            "message": text,
        }
        if target.group_id:
            params["groupId"] = target.group_id
        elif target.recipient:
            params["recipient"] = [target.recipient]
        else:
            raise ValueError("Signal reply target requires a recipient or group ID")
        result = await self.rpc("send", params)
        return result if isinstance(result, dict) else {}

    async def stream_events(self, account: str) -> AsyncGenerator[Mapping[str, str], None]:
        url = f"{self.base_url}/api/v1/events?account={quote(account, safe='')}"
        async with self._client.stream(
            "GET",
            url,
            headers={"Accept": "text/event-stream"},
            timeout=httpx.Timeout(connect=10, read=None, write=10, pool=10),
        ) as response:
            response.raise_for_status()
            event: dict[str, str] = {}
            async for line in response.aiter_lines():
                if line == "":
                    if event:
                        yield event
                        event = {}
                    continue
                if line.startswith(":"):
                    continue
                field, sep, raw_value = line.partition(":")
                if not sep:
                    continue
                value = raw_value[1:] if raw_value.startswith(" ") else raw_value
                if field == "event":
                    event["event"] = value
                elif field == "data":
                    if "data" in event:
                        event["data"] = f"{event['data']}\n{value}"
                    else:
                        event["data"] = value
                elif field == "id":
                    event["id"] = value
            if event:
                yield event


class NymeriaSignalBot:
    """Signal SSE-loop bot thin client for Nymeria."""

    def __init__(
        self,
        api: SignalNymeriaAPI,
        signal: SignalClientProtocol,
        *,
        account: str,
        account_uuid: Optional[str] = None,
        respond_mode: str = "mention",
        allowed_users: str | list[str] | tuple[str, ...] | None = None,
        allowed_groups: str | list[str] | tuple[str, ...] | None = None,
        show_tool_events: bool = False,
        seen_cache: Optional[_SeenEventCache] = None,
    ) -> None:
        self.api = api
        self.signal = signal
        self.account = account
        self.account_uuid = account_uuid
        self.respond_mode = respond_mode if respond_mode in {"mention", "all"} else "mention"
        self.allowed_users = {
            normalize_signal_allow_entry(item)
            for item in _csv_set(allowed_users)
            if normalize_signal_allow_entry(item)
        }
        self.allowed_groups = _csv_set(allowed_groups)
        self.show_tool_events = show_tool_events
        self._seen = seen_cache or _SeenEventCache()
        self._bindings: dict[str, str] = {}
        self._binding_users: dict[str, str] = {}
        self._user_resolver = UserResolver(self.api, "signal", logger=logger)
        self._running = False
        self._health_task: Optional[asyncio.Task] = None
        self._bindings_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        if not await self.signal.check():
            raise RuntimeError("Signal daemon health check failed")
        await self._refresh_bindings()
        self._running = True
        self._bindings_task = asyncio.create_task(self._bindings_refresh_loop())
        self._health_task = asyncio.create_task(self._health_heartbeat_loop())
        logger.info("Signal bot listening for account %s", self.account)
        await self._event_loop()

    def run(self) -> None:
        asyncio.run(self.start())

    async def close(self) -> None:
        self._running = False
        for task in (self._bindings_task, self._health_task):
            if task:
                task.cancel()
        await self.signal.close()
        await self.api.close()

    async def _event_loop(self) -> None:
        while self._running:
            try:
                async for event in self.signal.stream_events(self.account):
                    if not self._running:
                        break
                    await self.handle_signal_event(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Signal event stream failed; reconnecting")
                await asyncio.sleep(5)

    async def handle_signal_event(self, event: Mapping[str, Any]) -> None:
        if event.get("event") not in {None, "", "receive"}:
            return
        raw_data = event.get("data")
        if isinstance(raw_data, str):
            try:
                payload = json.loads(raw_data)
            except json.JSONDecodeError:
                logger.debug("Signal SSE event had malformed JSON: %s", raw_data[:100])
                return
        elif isinstance(raw_data, Mapping):
            payload = raw_data
        else:
            payload = event
        if not isinstance(payload, Mapping):
            return

        message = inbound_message_from_payload(
            payload,
            account=self.account,
            account_uuid=self.account_uuid,
        )
        if message is None:
            return
        if self._seen.mark_seen(message.event_id):
            return
        await self.handle_message(message)

    async def handle_message(self, message: SignalInboundMessage) -> None:
        if not self._sender_allowed(message.sender_id):
            logger.debug("Signal sender %s not in allowlist", message.sender_id)
            return
        if message.group_id and not self._group_allowed(message.group_id):
            logger.debug("Signal group %s not in allowlist", message.group_id)
            return

        should_respond = (
            not message.is_group
            or self.respond_mode == "all"
            or message.was_mentioned
        )
        if not should_respond:
            return

        command = SignalCommand.parse(
            message.raw_text,
            account=self.account,
            account_uuid=self.account_uuid,
        )
        if command is not None:
            await self._handle_command(command, message=message)
            return

        user_id = await self.resolve_user_id(message.sender_id)
        if user_id is None:
            await self._reject_unlinked(message.target, message.sender_id)
            return

        thread_id = self._resolve_thread_id(message)
        prompt = message.text
        if message.is_group:
            sender = message.sender_name or message.sender_id
            group = message.group_name or message.group_id or "group"
            prompt = f"[Signal {sender} in {group}]\n{message.text}"
        await self._stream_to_signal(
            message=prompt,
            thread_id=thread_id,
            user_id=user_id,
            target=message.target,
        )

    async def resolve_user_id(self, platform_user_id: str) -> Optional[str]:
        return await self._user_resolver.resolve(platform_user_id)

    def _sender_allowed(self, sender_id: str) -> bool:
        if not self.allowed_users:
            return True
        return "*" in self.allowed_users or sender_id in self.allowed_users

    def _group_allowed(self, group_id: str) -> bool:
        if not self.allowed_groups:
            return True
        return "*" in self.allowed_groups or group_id in self.allowed_groups

    def _resolve_thread_id(self, message: SignalInboundMessage) -> str:
        bound = self._bindings.get(message.platform_chat_id)
        if bound:
            return bound
        return make_thread_id(sender_id=message.sender_id, group_id=message.group_id)

    async def _handle_command(
        self,
        command: SignalCommand,
        *,
        message: SignalInboundMessage,
    ) -> None:
        if command.name == "link":
            await self._cmd_link(command.arg, message.sender_id, message.target)
        elif command.name == "bind":
            await self._cmd_bind(command.arg, message, message.target)
        elif command.name == "unbind":
            await self._cmd_unbind(message, message.target)
        elif command.name == "stop":
            await self._cmd_stop(message, message.target)

    async def _cmd_link(
        self,
        code: str,
        sender_id: str,
        target: SignalReplyTarget,
    ) -> None:
        if not code:
            await self._send_text(target, "Usage: `link <code>`")
            return
        try:
            result = await self.api.claim_platform_link_code(
                code=code,
                provider="signal",
                platform_user_id=sender_id,
            )
        except httpx.HTTPStatusError as exc:
            await self._send_text(target, f"Couldn't link: {http_error_detail(exc)}")
            return
        self._user_resolver.invalidate(sender_id)
        await self._send_text(
            target,
            f"Linked this Signal account to Nymeria user `{result.get('user_id')}`.",
        )

    async def _cmd_bind(
        self,
        code: str,
        message: SignalInboundMessage,
        target: SignalReplyTarget,
    ) -> None:
        if not code:
            await self._send_text(target, "Usage: `bind <code>`")
            return
        chat_id = message.platform_chat_id
        try:
            result = await self.api.claim_thread_bind_code(
                code=code,
                provider="signal",
                platform_chat_id=chat_id,
                expected_provider_user_id=message.sender_id,
            )
        except httpx.HTTPStatusError as exc:
            await self._send_text(target, f"Couldn't bind: {http_error_detail(exc)}")
            return
        thread_id = str(result.get("thread_id") or "")
        user_id = str(result.get("user_id") or "")
        if thread_id:
            self._bindings[chat_id] = thread_id
        if user_id:
            self._binding_users[chat_id] = user_id
        await self._send_text(target, f"Bound this Signal conversation to `{thread_id}`.")

    async def _cmd_unbind(
        self,
        message: SignalInboundMessage,
        target: SignalReplyTarget,
    ) -> None:
        user_id = await self.resolve_user_id(message.sender_id)
        if user_id is None:
            await self._reject_unlinked(target, message.sender_id)
            return
        chat_id = message.platform_chat_id
        try:
            result = await self.api.unbind_chatapp_by_chat(
                provider="signal",
                platform_chat_id=chat_id,
                user_id=user_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Signal unbind failed")
            await self._send_text(target, f"Couldn't unbind: {exc}")
            return
        if not result.get("unbound"):
            await self._send_text(target, "This Signal conversation is not bound.")
            return
        self._bindings.pop(chat_id, None)
        self._binding_users.pop(chat_id, None)
        await self._send_text(target, "Unbound. Future messages will use the default Signal thread.")

    async def _cmd_stop(
        self,
        message: SignalInboundMessage,
        target: SignalReplyTarget,
    ) -> None:
        user_id = await self.resolve_user_id(message.sender_id)
        if user_id is None:
            await self._reject_unlinked(target, message.sender_id)
            return
        thread_id = self._resolve_thread_id(message)
        try:
            await self.api.stop(thread_id, user_id=user_id)
        except Exception as exc:  # noqa: BLE001
            await self._send_text(target, f"Couldn't stop the current run: {exc}")
            return
        await self._send_text(target, "Stopped the current run for this Signal thread.")

    async def _reject_unlinked(self, target: SignalReplyTarget, sender_id: str) -> None:
        await self._send_text(
            target,
            "This Signal account is not linked to a Nymeria user yet.\n"
            f"Use `link <code>` here, or ask an admin to run "
            f"`python3 run.py users link-platform <email> signal {sender_id}`.",
        )

    async def _stream_to_signal(
        self,
        *,
        message: str,
        thread_id: str,
        user_id: str,
        target: SignalReplyTarget,
    ) -> None:
        handler = _SignalStreamHandler(self, target)
        try:
            await consume_sse_stream(self.api.chat_stream(message, thread_id, user_id), handler)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Signal streaming failed; falling back to sync")
            try:
                data = await self.api.chat(message, thread_id, user_id)
                response = str(data.get("response") or "")
                tool_count = data.get("tool_call_count") or 0
                if tool_count:
                    response += f"\n\nTool calls: {tool_count}"
                await self._send_text(target, response or "(no response)")
            except Exception as fallback_exc:  # noqa: BLE001
                logger.exception("Signal sync fallback failed")
                await self._send_text(
                    target,
                    f"Sorry, I encountered an error: {fallback_exc or exc}",
                )

    async def _send_text(self, target: SignalReplyTarget, content: str) -> None:
        for chunk in split_message(content, SIGNAL_TEXT_LIMIT):
            await self.signal.send_text(target, chunk)

    async def _refresh_bindings(self) -> None:
        try:
            entries = await self.api.list_chatapp_bindings(provider="signal")
        except Exception:  # noqa: BLE001
            logger.exception("Failed to refresh Signal chat-app bindings; keeping current cache")
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
        logger.debug("Signal chat-app bindings refreshed: %d entries", len(bindings))

    async def _bindings_refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(BINDING_REFRESH_INTERVAL_SECONDS)
            await self._refresh_bindings()

    async def _health_heartbeat_loop(self) -> None:
        while True:
            try:
                api_ok = await self.api.health()
                signal_ok = await self.signal.check()
                write_service_heartbeat(
                    "signal-bot",
                    status="ok" if api_ok and signal_ok and self._running else "unhealthy",
                    details={
                        "api": api_ok,
                        "signal": signal_ok,
                        "account": self.account,
                    },
                )
            except Exception:  # noqa: BLE001
                logger.exception("Signal heartbeat failed")
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)


class _SignalStreamHandler:
    """Render Nymeria SSE events into Signal text messages."""

    def __init__(self, bot: NymeriaSignalBot, target: SignalReplyTarget) -> None:
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
        if len(self._buffer) >= SIGNAL_TEXT_LIMIT:
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


def create_signal_bot_from_settings(
    api: NymeriaAPIClient,
    *,
    settings: Any,
) -> NymeriaSignalBot:
    """Construct a Signal bot from global settings."""
    if not settings.signal_http_url:
        raise RuntimeError("SIGNAL_HTTP_URL is required for signal-bot")
    if not settings.signal_account:
        raise RuntimeError("SIGNAL_ACCOUNT is required for signal-bot")
    signal = SignalCliRestClient(
        settings.signal_http_url,
        account=settings.signal_account,
        timeout=float(getattr(settings, "signal_http_timeout", 30.0)),
    )
    return NymeriaSignalBot(
        api,
        signal,
        account=settings.signal_account,
        account_uuid=getattr(settings, "signal_account_uuid", None),
        respond_mode=getattr(settings, "signal_respond_mode", "mention"),
        allowed_users=getattr(settings, "signal_allowed_users", ""),
        allowed_groups=getattr(settings, "signal_allowed_groups", ""),
        show_tool_events=bool(getattr(settings, "signal_show_tool_events", False)),
    )
