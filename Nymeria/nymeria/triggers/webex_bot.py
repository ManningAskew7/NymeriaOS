"""Webex Messaging webhook client for two-way Nymeria communication."""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
from collections.abc import AsyncIterable, Mapping
from dataclasses import dataclass
from typing import Any, Optional, Protocol
from urllib.parse import quote

import httpx

from .bot_helpers import SeenEventCache, safe_id as _safe_id
from .message_splitter import split_webex_message as split_message
from .sse_consumer import consume_sse_stream

logger = logging.getLogger(__name__)

WEBEX_TEXT_LIMIT = 3500


class BotAPIError(Exception):
    """User-facing error raised by the Nymeria API adapter."""

    def __init__(self, detail: str, *, status_code: int = 400) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


class WebexNymeriaAPI(Protocol):
    """Small API surface the Webex client needs from Nymeria."""

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


def make_platform_user_id(person_id: str) -> str:
    """Return the Webex identity key stored in Nymeria platform links."""
    return str(person_id or "").strip()


def make_platform_chat_id(room_id: str, parent_id: Optional[str] = None) -> str:
    """Return the Webex chat key used by Nymeria chat-app bindings."""
    room = str(room_id or "").strip()
    parent = str(parent_id or "").strip()
    if parent:
        return f"webex:{room}:{parent}"
    return f"webex:{room}"


def make_thread_id(
    room_id: str,
    *,
    room_type: str = "",
    person_id: str = "",
    parent_id: Optional[str] = None,
) -> str:
    """Generate a native Nymeria thread ID for a Webex conversation."""
    if room_type == "direct" and person_id:
        base = f"webex_dm_{_safe_id(person_id)}"
    else:
        base = f"webex_{_safe_id(room_id)}"
    if parent_id:
        return f"{base}_thread_{_safe_id(parent_id)}"
    return base


def verify_webex_signature(
    raw_body: bytes,
    signature_header: Optional[str],
    webhook_secret: Optional[str],
) -> bool:
    """Verify Webex's X-Spark-Signature header."""
    if not webhook_secret:
        return False
    if not signature_header:
        return False
    provided = signature_header.strip()
    if provided.startswith("sha1="):
        provided = provided[5:]
    expected = hmac.new(
        webhook_secret.encode("utf-8"),
        raw_body,
        hashlib.sha1,
    ).hexdigest()
    return hmac.compare_digest(expected, provided)


def strip_webex_mention(
    text: str,
    *,
    bot_person_id: Optional[str] = None,
    bot_email: Optional[str] = None,
) -> str:
    """Remove Webex bot mentions from text or markdown."""
    cleaned = str(text or "")
    if bot_person_id:
        escaped_id = re.escape(bot_person_id)
        cleaned = re.sub(rf"<@personId:{escaped_id}\|[^>]*>\s*", "", cleaned)
        cleaned = cleaned.replace(bot_person_id, "")
    if bot_email:
        escaped_email = re.escape(bot_email)
        cleaned = re.sub(rf"<@personEmail:{escaped_email}\|[^>]*>\s*", "", cleaned, flags=re.I)
        cleaned = re.sub(escaped_email, "", cleaned, flags=re.I)
    return cleaned.strip()


@dataclass(frozen=True)
class WebexWebhookEvent:
    message_id: str
    room_id: str
    person_id: str
    person_email: Optional[str] = None
    created: Optional[str] = None
    webhook_id: Optional[str] = None


@dataclass(frozen=True)
class WebexMessage:
    message_id: str
    room_id: str
    person_id: str
    text: str
    markdown: str = ""
    room_type: str = ""
    person_email: Optional[str] = None
    parent_id: Optional[str] = None
    mentioned_people: tuple[str, ...] = ()
    created: Optional[str] = None

    @property
    def display_text(self) -> str:
        return (self.text or self.markdown).strip()


@dataclass(frozen=True)
class WebexReplyTarget:
    room_id: str
    parent_id: Optional[str] = None


class WebexCommand:
    """Small parser for Webex text commands handled before agent routing."""

    def __init__(self, name: str, arg: str = "") -> None:
        self.name = name
        self.arg = arg

    @classmethod
    def parse(
        cls,
        text: str,
        *,
        bot_person_id: Optional[str] = None,
        bot_email: Optional[str] = None,
    ) -> Optional["WebexCommand"]:
        cleaned = strip_webex_mention(
            text,
            bot_person_id=bot_person_id,
            bot_email=bot_email,
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


def extract_webhook_events(payload: Mapping[str, Any]) -> list[WebexWebhookEvent]:
    """Extract Webex message-created events from a webhook payload."""
    if payload.get("resource") != "messages" or payload.get("event") != "created":
        return []
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return []
    message_id = str(data.get("id") or "")
    room_id = str(data.get("roomId") or "")
    person_id = str(data.get("personId") or "")
    if not message_id or not room_id or not person_id:
        return []
    return [
        WebexWebhookEvent(
            message_id=message_id,
            room_id=room_id,
            person_id=person_id,
            person_email=str(data.get("personEmail") or "") or None,
            created=str(data.get("created") or "") or None,
            webhook_id=str(payload.get("id") or "") or None,
        )
    ]


def message_from_details(
    details: Mapping[str, Any],
    *,
    fallback: WebexWebhookEvent,
) -> Optional[WebexMessage]:
    """Build a normalized inbound message from Webex message details."""
    message_id = str(details.get("id") or fallback.message_id)
    room_id = str(details.get("roomId") or fallback.room_id)
    person_id = str(details.get("personId") or fallback.person_id)
    text = str(details.get("text") or "").strip()
    markdown = str(details.get("markdown") or "").strip()
    if not message_id or not room_id or not person_id or not (text or markdown):
        return None
    mentioned = details.get("mentionedPeople")
    mentioned_people: tuple[str, ...] = ()
    if isinstance(mentioned, list):
        mentioned_people = tuple(str(item) for item in mentioned if item)
    return WebexMessage(
        message_id=message_id,
        room_id=room_id,
        person_id=person_id,
        text=text,
        markdown=markdown,
        room_type=str(details.get("roomType") or ""),
        person_email=str(details.get("personEmail") or fallback.person_email or "") or None,
        parent_id=str(details.get("parentId") or "") or None,
        mentioned_people=mentioned_people,
        created=str(details.get("created") or fallback.created or "") or None,
    )


class WebexMessagingClient:
    """Minimal async Webex Messaging REST client."""

    def __init__(
        self,
        *,
        access_token: str,
        base_url: str = "https://webexapis.com/v1",
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.access_token = access_token
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=30, write=10, pool=10)
        )
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def get_me(self) -> dict[str, Any]:
        response = await self._client.get(
            f"{self.base_url}/people/me",
            headers=self._headers,
        )
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, dict) else {}

    async def get_message(self, message_id: str) -> dict[str, Any]:
        response = await self._client.get(
            f"{self.base_url}/messages/{quote(message_id, safe='')}",
            headers=self._headers,
        )
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, dict) else {}

    async def send_message(
        self,
        *,
        room_id: str,
        markdown: str,
        parent_id: Optional[str] = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "roomId": room_id,
            "markdown": markdown,
        }
        if parent_id:
            payload["parentId"] = parent_id
        response = await self._client.post(
            f"{self.base_url}/messages",
            headers=self._headers,
            json=payload,
        )
        response.raise_for_status()
        if not response.content:
            return {}
        data = response.json()
        return data if isinstance(data, dict) else {}


class NymeriaWebexBot:
    """Webex webhook processor."""

    def __init__(
        self,
        api: WebexNymeriaAPI,
        webex: WebexMessagingClient,
        *,
        bot_person_id: Optional[str] = None,
        bot_email: Optional[str] = None,
        show_tool_events: bool = False,
        seen_cache: Optional[SeenEventCache] = None,
    ) -> None:
        self.api = api
        self.webex = webex
        self.bot_person_id = (bot_person_id or "").strip() or None
        self.bot_email = (bot_email or "").strip() or None
        self.show_tool_events = show_tool_events
        self._seen = seen_cache or SeenEventCache()
        self._bindings: dict[str, str] = {}

    async def refresh_bindings(self) -> None:
        try:
            entries = await self.api.list_chatapp_bindings(provider="webex")
        except Exception:  # noqa: BLE001
            logger.exception("Failed to refresh Webex chat-app bindings")
            return
        bindings: dict[str, str] = {}
        for entry in entries:
            chat_id = str(entry.get("platform_chat_id") or "")
            thread_id = str(entry.get("thread_id") or "")
            if chat_id and thread_id:
                bindings[chat_id] = thread_id
        self._bindings = bindings

    async def resolve_bot_identity(self) -> None:
        if self.bot_person_id and self.bot_email:
            return
        try:
            me = await self.webex.get_me()
        except Exception:  # noqa: BLE001
            logger.warning("Unable to resolve Webex bot identity", exc_info=True)
            return
        if not self.bot_person_id:
            self.bot_person_id = str(me.get("id") or "") or None
        if not self.bot_email:
            emails = me.get("emails")
            if isinstance(emails, list) and emails:
                self.bot_email = str(emails[0] or "") or None

    async def handle_webhook(self, payload: Mapping[str, Any]) -> dict[str, int]:
        events = extract_webhook_events(payload)
        if not events:
            return {"processed": 0, "skipped": 0}
        await self.refresh_bindings()
        await self.resolve_bot_identity()
        processed = 0
        skipped = 0
        for event in events:
            if self._seen.mark_seen(event.message_id):
                skipped += 1
                continue
            try:
                details = await self.webex.get_message(event.message_id)
            except Exception:  # noqa: BLE001
                logger.exception("Failed to fetch Webex message %s", event.message_id)
                skipped += 1
                continue
            message = message_from_details(details, fallback=event)
            if message is None:
                skipped += 1
                continue
            if self.bot_person_id and message.person_id == self.bot_person_id:
                skipped += 1
                continue
            if not self._should_respond(message):
                skipped += 1
                continue
            await self.handle_message(message)
            processed += 1
        return {"processed": processed, "skipped": skipped}

    def _should_respond(self, message: WebexMessage) -> bool:
        if message.room_type == "direct":
            return True
        if not self.bot_person_id:
            return True
        if self.bot_person_id in message.mentioned_people:
            return True
        text = f"{message.text}\n{message.markdown}"
        return bool(
            strip_webex_mention(
                text,
                bot_person_id=self.bot_person_id,
                bot_email=self.bot_email,
            ) != text.strip()
        )

    async def handle_message(self, message: WebexMessage) -> None:
        target = WebexReplyTarget(room_id=message.room_id, parent_id=message.parent_id)
        command_text = message.display_text
        command = WebexCommand.parse(
            command_text,
            bot_person_id=self.bot_person_id,
            bot_email=self.bot_email,
        )
        if command is not None:
            await self._handle_command(command, message=message, target=target)
            return

        platform_user_id = make_platform_user_id(message.person_id)
        user_id = await self.api.resolve_platform_user("webex", platform_user_id)
        if user_id is None:
            await self._reject_unlinked(target, platform_user_id)
            return

        thread_id = self._resolve_thread_id(message)
        prompt = strip_webex_mention(
            message.display_text,
            bot_person_id=self.bot_person_id,
            bot_email=self.bot_email,
        )
        await self._stream_to_webex(
            message=prompt,
            thread_id=thread_id,
            user_id=user_id,
            target=target,
        )

    def _resolve_thread_id(self, message: WebexMessage) -> str:
        chat_id = make_platform_chat_id(message.room_id, message.parent_id)
        if chat_id in self._bindings:
            return self._bindings[chat_id]
        room_chat_id = make_platform_chat_id(message.room_id)
        if room_chat_id in self._bindings:
            return self._bindings[room_chat_id]
        return make_thread_id(
            message.room_id,
            room_type=message.room_type,
            person_id=message.person_id,
            parent_id=message.parent_id,
        )

    def _chat_id_for_message(self, message: WebexMessage) -> str:
        return make_platform_chat_id(message.room_id, message.parent_id)

    async def _handle_command(
        self,
        command: WebexCommand,
        *,
        message: WebexMessage,
        target: WebexReplyTarget,
    ) -> None:
        if command.name == "link":
            await self._cmd_link(command.arg, message.person_id, target)
        elif command.name == "bind":
            await self._cmd_bind(command.arg, message, target)
        elif command.name == "unbind":
            await self._cmd_unbind(message, target)
        elif command.name == "stop":
            await self._cmd_stop(message, target)

    async def _cmd_link(
        self,
        code: str,
        person_id: str,
        target: WebexReplyTarget,
    ) -> None:
        if not code:
            await self._send_text(target, "Usage: `link <code>`")
            return
        platform_user_id = make_platform_user_id(person_id)
        try:
            result = await self.api.claim_platform_link_code(
                code=code,
                provider="webex",
                platform_user_id=platform_user_id,
            )
        except BotAPIError as exc:
            await self._send_text(target, f"Couldn't link: {exc.detail}")
            return
        await self._send_text(
            target,
            f"Linked this Webex account to Nymeria user `{result.get('user_id')}`.",
        )

    async def _cmd_bind(
        self,
        code: str,
        message: WebexMessage,
        target: WebexReplyTarget,
    ) -> None:
        if not code:
            await self._send_text(target, "Usage: `bind <code>`")
            return
        chat_id = self._chat_id_for_message(message)
        platform_user_id = make_platform_user_id(message.person_id)
        try:
            result = await self.api.claim_thread_bind_code(
                code=code,
                provider="webex",
                platform_chat_id=chat_id,
                expected_provider_user_id=platform_user_id,
            )
        except BotAPIError as exc:
            await self._send_text(target, f"Couldn't bind: {exc.detail}")
            return
        thread_id = str(result.get("thread_id") or "")
        if thread_id:
            self._bindings[chat_id] = thread_id
        await self._send_text(target, f"Bound this Webex chat to `{thread_id}`.")

    async def _cmd_unbind(self, message: WebexMessage, target: WebexReplyTarget) -> None:
        platform_user_id = make_platform_user_id(message.person_id)
        user_id = await self.api.resolve_platform_user("webex", platform_user_id)
        if user_id is None:
            await self._reject_unlinked(target, platform_user_id)
            return
        chat_id = self._chat_id_for_message(message)
        try:
            result = await self.api.unbind_chatapp_by_chat(
                provider="webex",
                platform_chat_id=chat_id,
                user_id=user_id,
            )
        except BotAPIError as exc:
            await self._send_text(target, f"Couldn't unbind: {exc.detail}")
            return
        if not result.get("unbound"):
            await self._send_text(target, "This Webex chat is not bound.")
            return
        self._bindings.pop(chat_id, None)
        await self._send_text(target, "Unbound. Future messages will use the default Webex thread.")

    async def _cmd_stop(self, message: WebexMessage, target: WebexReplyTarget) -> None:
        platform_user_id = make_platform_user_id(message.person_id)
        user_id = await self.api.resolve_platform_user("webex", platform_user_id)
        if user_id is None:
            await self._reject_unlinked(target, platform_user_id)
            return
        thread_id = self._resolve_thread_id(message)
        try:
            await self.api.stop(thread_id, user_id=user_id)
        except BotAPIError as exc:
            await self._send_text(target, f"Couldn't stop the current run: {exc.detail}")
            return
        await self._send_text(target, "Stopped the current run for this Webex chat.")

    async def _reject_unlinked(self, target: WebexReplyTarget, platform_user_id: str) -> None:
        await self._send_text(
            target,
            "This Webex account is not linked to a Nymeria user yet.\n"
            "Use `link <code>` here, or ask an admin to run "
            f"`python3 run.py users link-platform <email> webex {platform_user_id}`.",
        )

    async def _stream_to_webex(
        self,
        *,
        message: str,
        thread_id: str,
        user_id: str,
        target: WebexReplyTarget,
    ) -> None:
        handler = _WebexStreamHandler(self, target)
        try:
            await consume_sse_stream(self.api.chat_stream(message, thread_id, user_id), handler)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Webex streaming failed; falling back to sync")
            try:
                data = await self.api.chat(message, thread_id, user_id)
                response = str(data.get("response") or "")
                tool_count = data.get("tool_call_count") or 0
                if tool_count:
                    response += f"\n\nTool calls: {tool_count}"
                await self._send_text(target, response or "(no response)")
            except Exception as fallback_exc:  # noqa: BLE001
                logger.exception("Webex sync fallback failed")
                await self._send_text(
                    target,
                    f"Sorry, I encountered an error: {fallback_exc or exc}",
                )

    async def _send_text(self, target: WebexReplyTarget, content: str) -> None:
        for chunk in split_message(content, WEBEX_TEXT_LIMIT):
            await self.webex.send_message(
                room_id=target.room_id,
                markdown=chunk,
                parent_id=target.parent_id,
            )


class _WebexStreamHandler:
    """Render Nymeria SSE events into Webex messages."""

    def __init__(self, bot: NymeriaWebexBot, target: WebexReplyTarget) -> None:
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
        if len(self._buffer) >= WEBEX_TEXT_LIMIT:
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
