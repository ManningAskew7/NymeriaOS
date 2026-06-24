"""Google Chat webhook client for Nymeria."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import AsyncIterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Protocol
from urllib.parse import quote

import httpx
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import id_token, service_account

from .bot_helpers import SeenEventCache, safe_id as _safe_id
from .message_splitter import split_googlechat_message as split_message
from .sse_consumer import consume_sse_stream

logger = logging.getLogger(__name__)

GOOGLECHAT_TEXT_LIMIT = 4000
GOOGLECHAT_SCOPE = "https://www.googleapis.com/auth/chat.bot"
GOOGLECHAT_API_BASE_URL = "https://chat.googleapis.com/v1"
GOOGLECHAT_ISSUER = "chat@system.gserviceaccount.com"
GOOGLECHAT_CERTS_URL = (
    "https://www.googleapis.com/service_accounts/v1/metadata/x509/"
    "chat@system.gserviceaccount.com"
)


class BotAPIError(Exception):
    """User-facing error raised by the Nymeria API adapter."""

    def __init__(self, detail: str, *, status_code: int = 400) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


class GoogleChatNymeriaAPI(Protocol):
    """Small API surface the Google Chat client needs from Nymeria."""

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


class GoogleChatClientProtocol(Protocol):
    """Outbound Google Chat reply surface."""

    async def send_message(self, target: "GoogleChatReplyTarget", text: str) -> None:
        ...

    async def close(self) -> None:
        ...


def _resource_tail(value: str, marker: str) -> str:
    text = str(value or "").strip()
    if marker in text:
        return text.rsplit(marker, 1)[-1]
    return text.rsplit("/", 1)[-1] if "/" in text else text


def make_platform_user_id(user_name: str, email: Optional[str] = None) -> str:
    """Return the Google Chat identity key stored in Nymeria platform links."""
    user = str(user_name or "").strip()
    if user:
        return user
    if email:
        return f"email:{email.strip().lower()}"
    return "users/unknown"


def make_platform_chat_id(space_name: str, thread_name: Optional[str] = None) -> str:
    """Return the Google Chat conversation key used by chat-app bindings."""
    base = f"googlechat:{space_name}"
    if thread_name:
        return f"{base}:{thread_name}"
    return base


def make_thread_id(
    space_name: str,
    *,
    space_type: str = "",
    user_id: str = "",
    thread_name: Optional[str] = None,
) -> str:
    """Generate a native Nymeria thread ID for a Google Chat conversation."""
    if space_type.upper() == "DM":
        return f"googlechat_dm_{_safe_id(user_id)}"
    base = f"googlechat_{_safe_id(_resource_tail(space_name, 'spaces/'))}"
    if thread_name:
        return f"{base}_thread_{_safe_id(_resource_tail(thread_name, '/threads/'))}"
    return base


def strip_googlechat_mentions(
    text: str,
    *,
    bot_name: Optional[str] = None,
) -> str:
    """Remove Google Chat app mentions from visible text."""
    cleaned = str(text or "")
    cleaned = re.sub(r"<users/app>", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"@app\b", " ", cleaned, flags=re.I)
    if bot_name:
        cleaned = re.sub(rf"@?{re.escape(bot_name)}", " ", cleaned, flags=re.I)
    return re.sub(r"\s+", " ", cleaned).strip()


def event_mentions_app(event: "GoogleChatEvent") -> bool:
    """Return True when a Google Chat message explicitly invokes the app."""
    if event.argument_text and event.argument_text != event.raw_text:
        return True
    if "<users/app>" in event.raw_text:
        return True
    for annotation in event.annotations:
        if not isinstance(annotation, Mapping):
            continue
        if str(annotation.get("type") or "").upper() != "USER_MENTION":
            continue
        mention = annotation.get("userMention")
        if not isinstance(mention, Mapping):
            continue
        user = mention.get("user")
        if isinstance(user, Mapping) and str(user.get("name") or "") == "users/app":
            return True
    return False


async def validate_googlechat_authorization(
    authorization_header: Optional[str],
    *,
    audience: str,
    audience_type: str = "app-url",
) -> dict[str, Any]:
    """Validate a Google Chat HTTPS request bearer token."""
    if not authorization_header or not authorization_header.lower().startswith("bearer "):
        raise BotAPIError("Missing Google Chat bearer token", status_code=401)
    if not audience:
        raise BotAPIError("Google Chat auth audience is required", status_code=503)

    token = authorization_header.split(" ", 1)[1].strip()
    request = GoogleAuthRequest()
    try:
        if audience_type == "project-number":
            claims = await asyncio.to_thread(
                id_token.verify_token,
                token,
                request,
                audience,
                GOOGLECHAT_CERTS_URL,
            )
            if claims.get("iss") != GOOGLECHAT_ISSUER:
                raise BotAPIError("Invalid Google Chat token issuer", status_code=401)
        else:
            claims = await asyncio.to_thread(
                id_token.verify_oauth2_token,
                token,
                request,
                audience,
            )
            if claims.get("email") != GOOGLECHAT_ISSUER:
                raise BotAPIError("Invalid Google Chat token issuer", status_code=401)
            if claims.get("email_verified") is False:
                raise BotAPIError("Google Chat token email is not verified", status_code=401)
    except BotAPIError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise BotAPIError("Invalid Google Chat bearer token", status_code=401) from exc
    return dict(claims)


@dataclass(frozen=True)
class GoogleChatEvent:
    event_id: str
    event_type: str
    space_name: str
    space_type: str
    space_display_name: str
    sender_name: str
    sender_display_name: str
    sender_email: Optional[str]
    sender_type: str
    text: str
    raw_text: str
    argument_text: str
    message_name: str
    thread_name: Optional[str] = None
    event_time: Optional[str] = None
    annotations: tuple[Mapping[str, Any], ...] = ()

    @property
    def is_direct(self) -> bool:
        return self.space_type.upper() == "DM"

    @property
    def platform_user_id(self) -> str:
        return make_platform_user_id(self.sender_name, self.sender_email)


@dataclass(frozen=True)
class GoogleChatReplyTarget:
    space_name: str
    thread_name: Optional[str] = None
    platform_chat_id: Optional[str] = None


def event_from_payload(payload: Mapping[str, Any]) -> Optional[GoogleChatEvent]:
    """Normalize a Google Chat interaction payload into a message event."""
    event_type = str(payload.get("type") or payload.get("eventType") or "").strip()
    if event_type != "MESSAGE":
        return None

    message = payload.get("message")
    space = payload.get("space")
    if not isinstance(message, Mapping) or not isinstance(space, Mapping):
        return None

    sender = message.get("sender") or payload.get("user")
    if not isinstance(sender, Mapping):
        return None

    space_name = str(space.get("name") or "").strip()
    sender_name = str(sender.get("name") or "").strip()
    message_name = str(message.get("name") or "").strip()
    if not space_name or not sender_name or not message_name:
        return None

    raw_text = str(message.get("text") or "")
    argument_text = str(message.get("argumentText") or "").strip()
    text = argument_text or strip_googlechat_mentions(raw_text)
    if not text:
        return None

    thread = message.get("thread")
    thread_name = thread.get("name") if isinstance(thread, Mapping) else None
    annotations = tuple(
        annotation for annotation in message.get("annotations") or [] if isinstance(annotation, Mapping)
    )
    event_id = str(payload.get("eventId") or payload.get("eventTime") or message_name)

    return GoogleChatEvent(
        event_id=event_id,
        event_type=event_type,
        space_name=space_name,
        space_type=str(space.get("type") or ""),
        space_display_name=str(space.get("displayName") or ""),
        sender_name=sender_name,
        sender_display_name=str(sender.get("displayName") or sender_name),
        sender_email=str(sender.get("email") or "") or None,
        sender_type=str(sender.get("type") or ""),
        text=text,
        raw_text=raw_text,
        argument_text=argument_text,
        message_name=message_name,
        thread_name=str(thread_name or "") or None,
        event_time=str(payload.get("eventTime") or "") or None,
        annotations=annotations,
    )


class GoogleChatRESTClient:
    """Minimal Google Chat REST client for app-auth replies."""

    def __init__(
        self,
        *,
        service_account_json: Optional[str] = None,
        service_account_file: Optional[str] = None,
        api_base_url: str = GOOGLECHAT_API_BASE_URL,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.service_account_json = service_account_json
        self.service_account_file = service_account_file
        self.api_base_url = api_base_url.rstrip("/")
        self._client = http_client or httpx.AsyncClient(timeout=30)
        self._owns_client = http_client is None
        self._credentials: Any = None
        self._access_token = ""
        self._access_token_expires_at = 0.0

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _load_credentials(self) -> Any:
        if self._credentials is not None:
            return self._credentials
        if self.service_account_json:
            raw = self.service_account_json.strip()
            if raw.startswith("{"):
                info = json.loads(raw)
                self._credentials = service_account.Credentials.from_service_account_info(
                    info,
                    scopes=[GOOGLECHAT_SCOPE],
                )
            else:
                self._credentials = service_account.Credentials.from_service_account_file(
                    raw,
                    scopes=[GOOGLECHAT_SCOPE],
                )
            return self._credentials
        if self.service_account_file:
            self._credentials = service_account.Credentials.from_service_account_file(
                self.service_account_file,
                scopes=[GOOGLECHAT_SCOPE],
            )
            return self._credentials

        try:
            import google.auth
        except ImportError as exc:  # pragma: no cover - dependency guard.
            raise RuntimeError("google-auth is required for Google Chat ADC") from exc
        self._credentials, _project = google.auth.default(scopes=[GOOGLECHAT_SCOPE])
        return self._credentials

    async def _get_access_token(self) -> str:
        now = time.time()
        if self._access_token and self._access_token_expires_at > now + 60:
            return self._access_token
        credentials = self._load_credentials()
        if not credentials.valid or not getattr(credentials, "token", None):
            await asyncio.to_thread(credentials.refresh, GoogleAuthRequest())
        token = str(getattr(credentials, "token", "") or "")
        if not token:
            raise RuntimeError("Google Chat credentials did not produce an access token")
        expiry = getattr(credentials, "expiry", None)
        if isinstance(expiry, datetime):
            expires_at = expiry.replace(tzinfo=timezone.utc).timestamp()
        else:
            expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()
        self._access_token = token
        self._access_token_expires_at = expires_at
        return token

    async def send_message(self, target: GoogleChatReplyTarget, text: str) -> None:
        token = await self._get_access_token()
        parent = quote(target.space_name, safe="/")
        url = f"{self.api_base_url}/{parent}/messages"
        body: dict[str, Any] = {"text": text}
        params: dict[str, str] = {}
        if target.thread_name:
            body["thread"] = {"name": target.thread_name}
            params["messageReplyOption"] = "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
        response = await self._client.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            params=params,
            json=body,
        )
        response.raise_for_status()


class GoogleChatCommand:
    """Small parser for Google Chat text commands handled before agent routing."""

    def __init__(self, name: str, arg: str = "") -> None:
        self.name = name
        self.arg = arg

    @classmethod
    def parse(
        cls,
        text: str,
        *,
        bot_name: Optional[str] = None,
    ) -> Optional["GoogleChatCommand"]:
        cleaned = strip_googlechat_mentions(text, bot_name=bot_name)
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


class NymeriaGoogleChatBot:
    """Google Chat webhook bot - thin client for Nymeria."""

    def __init__(
        self,
        *,
        api: GoogleChatNymeriaAPI,
        googlechat_client: GoogleChatClientProtocol,
        bot_name: str = "Nymeria",
        respond_mode: str = "mention",
        show_tool_events: bool = False,
        seen_cache: Optional[SeenEventCache] = None,
    ) -> None:
        self.api = api
        self.googlechat = googlechat_client
        self.bot_name = bot_name
        self.respond_mode = respond_mode
        self.show_tool_events = show_tool_events
        self._seen = seen_cache or SeenEventCache()
        self._bindings: dict[str, str] = {}
        self._binding_users: dict[str, str] = {}
        self._active_chats: set[str] = set()

    async def close(self) -> None:
        await self.googlechat.close()

    async def handle_payload(self, payload: Mapping[str, Any]) -> None:
        event = event_from_payload(payload)
        if event is None:
            return
        await self.handle_event(event)

    async def handle_event(self, event: GoogleChatEvent) -> None:
        if self._seen.mark_seen(event.event_id):
            return
        if event.sender_type.upper() == "BOT" or event.sender_name == "users/app":
            return
        if not event.text:
            return

        await self.refresh_bindings()
        mentioned = event_mentions_app(event)
        target = GoogleChatReplyTarget(
            space_name=event.space_name,
            thread_name=event.thread_name,
            platform_chat_id=self._chat_id_for_event(event),
        )
        clean_text = event.argument_text or strip_googlechat_mentions(
            event.raw_text,
            bot_name=self.bot_name,
        ) or event.text
        command = GoogleChatCommand.parse(
            event.argument_text or event.raw_text or event.text,
            bot_name=self.bot_name,
        )

        accepted = event.is_direct or mentioned or self.respond_mode == "all"
        if not accepted and target.platform_chat_id in self._active_chats:
            accepted = True
        if not accepted:
            return

        if command is not None:
            await self._handle_command(command, event=event, target=target)
            return

        user_id = await self.api.resolve_platform_user("googlechat", event.platform_user_id)
        if user_id is None:
            await self._reject_unlinked(target, event.platform_user_id)
            return

        thread_id = self._resolve_thread_id(event)
        sender = event.sender_display_name or event.sender_name
        if event.is_direct:
            prompt = clean_text
        else:
            space = event.space_display_name or event.space_name
            prompt = f"[Google Chat {sender} in {space}]\n{clean_text}"
        self._active_chats.add(target.platform_chat_id or self._chat_id_for_event(event))
        await self._stream_to_googlechat(
            message=prompt,
            thread_id=thread_id,
            user_id=user_id,
            target=target,
        )

    async def refresh_bindings(self) -> None:
        try:
            entries = await self.api.list_chatapp_bindings(provider="googlechat")
        except Exception:  # noqa: BLE001
            logger.exception("Failed to refresh Google Chat bindings; keeping current cache")
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

    def _resolve_thread_id(self, event: GoogleChatEvent) -> str:
        thread_chat_id = self._chat_id_for_event(event)
        if thread_chat_id in self._bindings:
            return self._bindings[thread_chat_id]
        base_chat_id = make_platform_chat_id(event.space_name)
        if base_chat_id in self._bindings:
            return self._bindings[base_chat_id]
        return make_thread_id(
            event.space_name,
            space_type=event.space_type,
            user_id=event.sender_name,
            thread_name=event.thread_name,
        )

    def _chat_id_for_event(self, event: GoogleChatEvent) -> str:
        return make_platform_chat_id(event.space_name, event.thread_name)

    async def _handle_command(
        self,
        command: GoogleChatCommand,
        *,
        event: GoogleChatEvent,
        target: GoogleChatReplyTarget,
    ) -> None:
        if command.name == "link":
            await self._cmd_link(command.arg, event, target)
        elif command.name == "bind":
            await self._cmd_bind(command.arg, event, target)
        elif command.name == "unbind":
            await self._cmd_unbind(event, target)
        elif command.name == "stop":
            await self._cmd_stop(event, target)

    async def _cmd_link(
        self,
        code: str,
        event: GoogleChatEvent,
        target: GoogleChatReplyTarget,
    ) -> None:
        if not code:
            await self._send_text(target, "Usage: `link <code>`")
            return
        try:
            result = await self.api.claim_platform_link_code(
                code=code,
                provider="googlechat",
                platform_user_id=event.platform_user_id,
            )
        except BotAPIError as exc:
            await self._send_text(target, f"Couldn't link: {exc.detail}")
            return
        await self._send_text(
            target,
            f"Linked this Google Chat account to Nymeria user `{result.get('user_id')}`.",
        )

    async def _cmd_bind(
        self,
        code: str,
        event: GoogleChatEvent,
        target: GoogleChatReplyTarget,
    ) -> None:
        if not code:
            await self._send_text(target, "Usage: `bind <code>`")
            return
        chat_id = self._chat_id_for_event(event)
        try:
            result = await self.api.claim_thread_bind_code(
                code=code,
                provider="googlechat",
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
        await self._send_text(target, f"Bound this Google Chat conversation to `{thread_id}`.")

    async def _cmd_unbind(self, event: GoogleChatEvent, target: GoogleChatReplyTarget) -> None:
        user_id = await self.api.resolve_platform_user("googlechat", event.platform_user_id)
        if user_id is None:
            await self._reject_unlinked(target, event.platform_user_id)
            return
        chat_id = self._chat_id_for_event(event)
        try:
            result = await self.api.unbind_chatapp_by_chat(
                provider="googlechat",
                platform_chat_id=chat_id,
                user_id=user_id,
            )
        except BotAPIError as exc:
            await self._send_text(target, f"Couldn't unbind: {exc.detail}")
            return
        if not result.get("unbound"):
            await self._send_text(target, "This Google Chat conversation is not bound.")
            return
        self._bindings.pop(chat_id, None)
        self._binding_users.pop(chat_id, None)
        await self._send_text(
            target,
            "Unbound. Future messages will use the default Google Chat thread.",
        )

    async def _cmd_stop(self, event: GoogleChatEvent, target: GoogleChatReplyTarget) -> None:
        user_id = await self.api.resolve_platform_user("googlechat", event.platform_user_id)
        if user_id is None:
            await self._reject_unlinked(target, event.platform_user_id)
            return
        thread_id = self._resolve_thread_id(event)
        try:
            await self.api.stop(thread_id, user_id=user_id)
        except BotAPIError as exc:
            await self._send_text(target, f"Couldn't stop the current run: {exc.detail}")
            return
        await self._send_text(target, "Stopped the current run for this Google Chat conversation.")

    async def _reject_unlinked(self, target: GoogleChatReplyTarget, platform_user_id: str) -> None:
        await self._send_text(
            target,
            "This Google Chat account is not linked to a Nymeria user yet.\n"
            "Use `link <code>` here, or ask an admin to run "
            f"`python3 run.py users link-platform <email> googlechat {platform_user_id}`.",
        )

    async def _stream_to_googlechat(
        self,
        *,
        message: str,
        thread_id: str,
        user_id: str,
        target: GoogleChatReplyTarget,
    ) -> None:
        handler = _GoogleChatStreamHandler(self, target)
        try:
            await consume_sse_stream(self.api.chat_stream(message, thread_id, user_id), handler)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Google Chat streaming failed; falling back to sync")
            try:
                data = await self.api.chat(message, thread_id, user_id)
                response = str(data.get("response") or "")
                tool_count = data.get("tool_call_count") or 0
                if tool_count:
                    response += f"\n\nTool calls: {tool_count}"
                await self._send_text(target, response or "(no response)")
            except Exception as fallback_exc:  # noqa: BLE001
                logger.exception("Google Chat sync fallback failed")
                await self._send_text(
                    target,
                    f"Sorry, I encountered an error: {fallback_exc or exc}",
                )

    async def _send_text(self, target: GoogleChatReplyTarget, content: str) -> None:
        for chunk in split_message(content, GOOGLECHAT_TEXT_LIMIT):
            await self.googlechat.send_message(target, chunk)


class _GoogleChatStreamHandler:
    """Render Nymeria SSE events into Google Chat messages."""

    def __init__(self, bot: NymeriaGoogleChatBot, target: GoogleChatReplyTarget) -> None:
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
        if len(self._buffer) >= GOOGLECHAT_TEXT_LIMIT:
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


def credential_source_present(
    *,
    service_account_json: Optional[str],
    service_account_file: Optional[str],
    use_adc: bool,
) -> bool:
    if service_account_json and service_account_json.strip():
        return True
    if service_account_file and Path(service_account_file).expanduser():
        return True
    return use_adc
