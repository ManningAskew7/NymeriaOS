"""WhatsApp Cloud API webhook client for two-way Nymeria communication."""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterable, Mapping
from dataclasses import dataclass
from functools import partial
from typing import Any, Optional, Protocol

import httpx

from .bot_helpers import SeenEventCache, forward_backend_command, safe_id as _safe_id
from .message_splitter import split_whatsapp_message as split_message
from .sse_consumer import consume_sse_stream

logger = logging.getLogger(__name__)

WHATSAPP_TEXT_LIMIT = 4096


class BotAPIError(Exception):
    """User-facing error raised by the Nymeria API adapter."""

    def __init__(self, detail: str, *, status_code: int = 400) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


class WhatsAppNymeriaAPI(Protocol):
    """Small API surface the WhatsApp client needs from Nymeria."""

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

    async def execute_command(
        self,
        command: str,
        *,
        thread_id: Optional[str] = None,
        source: str = "user",
        actor: Optional[str] = None,
        surface: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> dict[str, Any]:
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


def normalize_sender_id(value: str) -> str:
    """Normalize Cloud API sender IDs for platform-link storage."""
    cleaned = re.sub(r"\D+", "", str(value or ""))
    return cleaned or _safe_id(str(value or "unknown"))


def make_platform_user_id(sender_id: str) -> str:
    """Return the WhatsApp identity key stored in Nymeria platform links."""
    return normalize_sender_id(sender_id)


def make_platform_chat_id(sender_id: str) -> str:
    """Return the WhatsApp chat key used by Nymeria chat-app bindings."""
    return f"whatsapp:{normalize_sender_id(sender_id)}"


def make_thread_id(sender_id: str) -> str:
    """Generate a native Nymeria thread ID for a WhatsApp direct chat."""
    return f"whatsapp_{_safe_id(normalize_sender_id(sender_id))}"


@dataclass(frozen=True)
class WhatsAppInboundMessage:
    message_id: str
    sender_id: str
    text: str
    message_type: str
    phone_number_id: Optional[str] = None
    contact_name: Optional[str] = None
    timestamp: Optional[str] = None


@dataclass(frozen=True)
class WhatsAppReplyTarget:
    to: str


class WhatsAppCommand:
    """Small parser for WhatsApp text commands handled before agent routing."""

    def __init__(self, name: str, arg: str = "") -> None:
        self.name = name
        self.arg = arg

    @classmethod
    def parse(cls, text: str) -> Optional["WhatsAppCommand"]:
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


def _contact_names_by_wa_id(contacts: Any) -> dict[str, str]:
    if not isinstance(contacts, list):
        return {}
    names: dict[str, str] = {}
    for contact in contacts:
        if not isinstance(contact, Mapping):
            continue
        wa_id = normalize_sender_id(str(contact.get("wa_id") or ""))
        profile = contact.get("profile")
        name = ""
        if isinstance(profile, Mapping):
            name = str(profile.get("name") or "")
        if wa_id and name:
            names[wa_id] = name
    return names


def _extract_text(message: Mapping[str, Any]) -> tuple[Optional[str], str]:
    message_type = str(message.get("type") or "")
    if message_type == "text":
        text = message.get("text")
        if isinstance(text, Mapping):
            return str(text.get("body") or "").strip() or None, message_type
    if message_type == "button":
        button = message.get("button")
        if isinstance(button, Mapping):
            value = str(button.get("text") or button.get("payload") or "").strip()
            return value or None, message_type
    if message_type == "interactive":
        interactive = message.get("interactive")
        if isinstance(interactive, Mapping):
            for key in ("button_reply", "list_reply"):
                reply = interactive.get(key)
                if isinstance(reply, Mapping):
                    value = str(reply.get("title") or reply.get("id") or "").strip()
                    return value or None, message_type
    return None, message_type


def extract_inbound_messages(payload: Mapping[str, Any]) -> list[WhatsAppInboundMessage]:
    """Extract text-like inbound messages from a WhatsApp Cloud API webhook."""
    entries = payload.get("entry")
    if not isinstance(entries, list):
        return []
    out: list[WhatsAppInboundMessage] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        changes = entry.get("changes")
        if not isinstance(changes, list):
            continue
        for change in changes:
            if not isinstance(change, Mapping):
                continue
            value = change.get("value")
            if not isinstance(value, Mapping):
                continue
            metadata = value.get("metadata")
            phone_number_id = None
            if isinstance(metadata, Mapping):
                phone_number_id = str(metadata.get("phone_number_id") or "") or None
            names = _contact_names_by_wa_id(value.get("contacts"))
            messages = value.get("messages")
            if not isinstance(messages, list):
                continue
            for message in messages:
                if not isinstance(message, Mapping):
                    continue
                message_id = str(message.get("id") or "")
                sender_id = normalize_sender_id(str(message.get("from") or ""))
                text, message_type = _extract_text(message)
                if not message_id or not sender_id or not text:
                    continue
                out.append(
                    WhatsAppInboundMessage(
                        message_id=message_id,
                        sender_id=sender_id,
                        text=text,
                        message_type=message_type,
                        phone_number_id=phone_number_id,
                        contact_name=names.get(sender_id),
                        timestamp=str(message.get("timestamp") or "") or None,
                    )
                )
    return out


class WhatsAppCloudClient:
    """Minimal async WhatsApp Cloud API sender."""

    def __init__(
        self,
        *,
        access_token: str,
        phone_number_id: str,
        base_url: str = "https://graph.facebook.com/v19.0",
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.access_token = access_token
        self.phone_number_id = phone_number_id
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=30, write=10, pool=10)
        )
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def send_text(self, to: str, body: str, *, preview_url: bool = False) -> dict[str, Any]:
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": normalize_sender_id(to),
            "type": "text",
            "text": {
                "preview_url": preview_url,
                "body": body,
            },
        }
        response = await self._client.post(
            f"{self.base_url}/{self.phone_number_id}/messages",
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


class NymeriaWhatsAppBot:
    """WhatsApp Cloud API webhook processor."""

    def __init__(
        self,
        api: WhatsAppNymeriaAPI,
        whatsapp: WhatsAppCloudClient,
        *,
        show_tool_events: bool = False,
        seen_cache: Optional[SeenEventCache] = None,
    ) -> None:
        self.api = api
        self.whatsapp = whatsapp
        self.show_tool_events = show_tool_events
        self._seen = seen_cache or SeenEventCache()
        self._bindings: dict[str, str] = {}

    async def refresh_bindings(self) -> None:
        try:
            entries = await self.api.list_chatapp_bindings(provider="whatsapp")
        except Exception:  # noqa: BLE001
            logger.exception("Failed to refresh WhatsApp chat-app bindings")
            return
        bindings: dict[str, str] = {}
        for entry in entries:
            chat_id = str(entry.get("platform_chat_id") or "")
            thread_id = str(entry.get("thread_id") or "")
            if chat_id and thread_id:
                bindings[chat_id] = thread_id
        self._bindings = bindings

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

    async def handle_message(self, message: WhatsAppInboundMessage) -> None:
        target = WhatsAppReplyTarget(to=message.sender_id)
        command = WhatsAppCommand.parse(message.text)
        if command is not None:
            await self._handle_command(command, message=message, target=target)
            return

        platform_user_id = make_platform_user_id(message.sender_id)
        user_id = await self.api.resolve_platform_user("whatsapp", platform_user_id)
        if user_id is None:
            await self._reject_unlinked(target, platform_user_id)
            return

        thread_id = self._resolve_thread_id(message.sender_id)

        # Generic backend slash-command passthrough. Local commands
        # (link/bind/unbind/stop) were consumed above; any other "/" text is a
        # backend command, except chat_stream-kind commands (e.g. /skill),
        # which fall through to the normal chat path below.
        stripped_text = message.text.strip()
        if stripped_text.startswith("/"):
            handled = await forward_backend_command(
                self.api,
                stripped_text,
                thread_id=thread_id,
                user_id=user_id,
                surface="whatsapp",
                send=partial(self._send_text, target),
                logger=logger,
            )
            if handled:
                return

        prompt = message.text
        await self._stream_to_whatsapp(
            message=prompt,
            thread_id=thread_id,
            user_id=user_id,
            target=target,
        )

    def _resolve_thread_id(self, sender_id: str) -> str:
        chat_id = make_platform_chat_id(sender_id)
        return self._bindings.get(chat_id) or make_thread_id(sender_id)

    async def _handle_command(
        self,
        command: WhatsAppCommand,
        *,
        message: WhatsAppInboundMessage,
        target: WhatsAppReplyTarget,
    ) -> None:
        if command.name == "link":
            await self._cmd_link(command.arg, message.sender_id, target)
        elif command.name == "bind":
            await self._cmd_bind(command.arg, message.sender_id, target)
        elif command.name == "unbind":
            await self._cmd_unbind(message.sender_id, target)
        elif command.name == "stop":
            await self._cmd_stop(message.sender_id, target)

    async def _cmd_link(
        self,
        code: str,
        sender_id: str,
        target: WhatsAppReplyTarget,
    ) -> None:
        if not code:
            await self._send_text(target, "Usage: `link <code>`")
            return
        platform_user_id = make_platform_user_id(sender_id)
        try:
            result = await self.api.claim_platform_link_code(
                code=code,
                provider="whatsapp",
                platform_user_id=platform_user_id,
            )
        except BotAPIError as exc:
            await self._send_text(target, f"Couldn't link: {exc.detail}")
            return
        await self._send_text(
            target,
            f"Linked this WhatsApp account to Nymeria user `{result.get('user_id')}`.",
        )

    async def _cmd_bind(
        self,
        code: str,
        sender_id: str,
        target: WhatsAppReplyTarget,
    ) -> None:
        if not code:
            await self._send_text(target, "Usage: `bind <code>`")
            return
        chat_id = make_platform_chat_id(sender_id)
        platform_user_id = make_platform_user_id(sender_id)
        try:
            result = await self.api.claim_thread_bind_code(
                code=code,
                provider="whatsapp",
                platform_chat_id=chat_id,
                expected_provider_user_id=platform_user_id,
            )
        except BotAPIError as exc:
            await self._send_text(target, f"Couldn't bind: {exc.detail}")
            return
        thread_id = str(result.get("thread_id") or "")
        if thread_id:
            self._bindings[chat_id] = thread_id
        await self._send_text(target, f"Bound this WhatsApp chat to `{thread_id}`.")

    async def _cmd_unbind(self, sender_id: str, target: WhatsAppReplyTarget) -> None:
        platform_user_id = make_platform_user_id(sender_id)
        user_id = await self.api.resolve_platform_user("whatsapp", platform_user_id)
        if user_id is None:
            await self._reject_unlinked(target, platform_user_id)
            return
        chat_id = make_platform_chat_id(sender_id)
        try:
            result = await self.api.unbind_chatapp_by_chat(
                provider="whatsapp",
                platform_chat_id=chat_id,
                user_id=user_id,
            )
        except BotAPIError as exc:
            await self._send_text(target, f"Couldn't unbind: {exc.detail}")
            return
        if not result.get("unbound"):
            await self._send_text(target, "This WhatsApp chat is not bound.")
            return
        self._bindings.pop(chat_id, None)
        await self._send_text(target, "Unbound. Future messages will use the default WhatsApp thread.")

    async def _cmd_stop(self, sender_id: str, target: WhatsAppReplyTarget) -> None:
        platform_user_id = make_platform_user_id(sender_id)
        user_id = await self.api.resolve_platform_user("whatsapp", platform_user_id)
        if user_id is None:
            await self._reject_unlinked(target, platform_user_id)
            return
        thread_id = self._resolve_thread_id(sender_id)
        try:
            result = await self.api.stop(thread_id, user_id=user_id)
        except BotAPIError as exc:
            await self._send_text(target, f"Couldn't stop the current run: {exc.detail}")
            return
        from ..core.pending_prompt_queue import restored_prompts_notice

        message = "Stopped the current run for this WhatsApp chat."
        notice = restored_prompts_notice(result.get("restored_prompts") or [])
        if notice:
            message = f"{message}\n\n{notice}"
        await self._send_text(target, message)

    async def _reject_unlinked(self, target: WhatsAppReplyTarget, platform_user_id: str) -> None:
        await self._send_text(
            target,
            "This WhatsApp account is not linked to a Nymeria user yet.\n"
            "Use `link <code>` here, or ask an admin to run "
            f"`python3 run.py users link-platform <email> whatsapp {platform_user_id}`.",
        )

    async def _stream_to_whatsapp(
        self,
        *,
        message: str,
        thread_id: str,
        user_id: str,
        target: WhatsAppReplyTarget,
    ) -> None:
        handler = _WhatsAppStreamHandler(self, target)
        try:
            await consume_sse_stream(self.api.chat_stream(message, thread_id, user_id), handler)
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, BotAPIError) and exc.status_code == 429:
                # Capacity shed (backlog #83): the sync fallback re-enters
                # the same admission gate, so retrying at saturation only
                # doubles the shed latency. Relay the busy notice directly.
                logger.info("WhatsApp turn shed at interactive capacity")
                await self._send_text(target, exc.detail)
                return
            logger.exception("WhatsApp streaming failed; falling back to sync")
            try:
                data = await self.api.chat(message, thread_id, user_id)
                response = str(data.get("response") or "")
                tool_count = data.get("tool_call_count") or 0
                if tool_count:
                    response += f"\n\nTool calls: {tool_count}"
                await self._send_text(target, response or "(no response)")
            except Exception as fallback_exc:  # noqa: BLE001
                logger.exception("WhatsApp sync fallback failed")
                await self._send_text(
                    target,
                    f"Sorry, I encountered an error: {fallback_exc or exc}",
                )

    async def _send_text(self, target: WhatsAppReplyTarget, content: str) -> None:
        for chunk in split_message(content, WHATSAPP_TEXT_LIMIT):
            await self.whatsapp.send_text(target.to, chunk)


class _WhatsAppStreamHandler:
    """Render Nymeria SSE events into WhatsApp Cloud API text messages."""

    def __init__(self, bot: NymeriaWhatsAppBot, target: WhatsAppReplyTarget) -> None:
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
        if len(self._buffer) >= WHATSAPP_TEXT_LIMIT:
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

    async def on_turn_rewound(self, content: str) -> None:
        # A provider refusal was rewound server-side (backlog #105).
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
