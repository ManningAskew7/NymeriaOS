"""Microsoft Teams Bot Framework webhook client for Nymeria."""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from collections.abc import AsyncIterable, Mapping
from dataclasses import dataclass
from typing import Any, Optional, Protocol
from urllib.parse import quote

import httpx
import jwt
from jwt import InvalidTokenError, PyJWKClient

from .message_splitter import split_teams_message as split_message
from .sse_consumer import consume_sse_stream

logger = logging.getLogger(__name__)

TEAMS_TEXT_LIMIT = 4000
SEEN_ACTIVITY_TTL_SECONDS = 10 * 60
SEEN_ACTIVITY_MAX = 5000
ACTIVE_CHAT_MAX = 5000
BOT_FRAMEWORK_TOKEN_URL = "https://login.microsoftonline.com/botframework.com/oauth2/v2.0/token"
BOT_FRAMEWORK_SCOPE = "https://api.botframework.com/.default"
BOT_FRAMEWORK_OPENID_CONFIG_URL = "https://login.botframework.com/v1/.well-known/openidconfiguration"
BOT_FRAMEWORK_ISSUER = "https://api.botframework.com"

_JWK_CLIENTS: dict[str, PyJWKClient] = {}


class BotAPIError(Exception):
    """User-facing error raised by the Nymeria API adapter."""

    def __init__(self, detail: str, *, status_code: int = 400) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


class TeamsNymeriaAPI(Protocol):
    """Small API surface the Teams client needs from Nymeria."""

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


class TeamsClientProtocol(Protocol):
    """Outbound Microsoft Teams reply surface."""

    async def send_message(self, target: "TeamsReplyTarget", text: str) -> None:
        ...

    async def close(self) -> None:
        ...


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value)).strip("-") or "unknown"


def normalize_conversation_id(raw: str) -> str:
    """Strip Bot Framework message suffixes from a Teams conversation ID."""
    return str(raw or "").split(";", 1)[0]


def extract_conversation_message_id(raw: str) -> Optional[str]:
    """Return the Teams thread root from a `;messageid=...` conversation ID."""
    match = re.search(r"(?:^|;)messageid=([^;]+)", str(raw or ""), flags=re.I)
    if not match:
        return None
    value = match.group(1).strip()
    return value or None


def _tenant_from_activity(payload: Mapping[str, Any]) -> str:
    channel_data = payload.get("channelData")
    if isinstance(channel_data, Mapping):
        tenant = channel_data.get("tenant")
        if isinstance(tenant, Mapping):
            tenant_id = str(tenant.get("id") or "").strip()
            if tenant_id:
                return tenant_id
    conversation = payload.get("conversation")
    if isinstance(conversation, Mapping):
        tenant_id = str(conversation.get("tenantId") or "").strip()
        if tenant_id:
            return tenant_id
    return "common"


def make_platform_user_id(
    tenant_id: str,
    *,
    aad_object_id: Optional[str] = None,
    teams_user_id: Optional[str] = None,
) -> str:
    """Return the Teams identity key stored in Nymeria platform links."""
    user_key = str(aad_object_id or teams_user_id or "unknown").strip()
    return f"{tenant_id or 'common'}:{user_key}"


def make_platform_chat_id(
    tenant_id: str,
    conversation_id: str,
    thread_id: Optional[str] = None,
) -> str:
    """Return the Teams chat key used by Nymeria chat-app bindings."""
    base = f"teams:{tenant_id or 'common'}:{normalize_conversation_id(conversation_id)}"
    if thread_id:
        return f"{base}:{thread_id}"
    return base


def make_thread_id(
    tenant_id: str,
    conversation_id: str,
    *,
    conversation_type: str = "",
    user_id: str = "",
    thread_id: Optional[str] = None,
) -> str:
    """Generate a native Nymeria thread ID for a Teams conversation."""
    tenant = _safe_id(tenant_id or "common")
    if conversation_type == "personal":
        base = f"teams_dm_{tenant}_{_safe_id(user_id)}"
    else:
        base = f"teams_{tenant}_{_safe_id(normalize_conversation_id(conversation_id))}"
    if thread_id:
        return f"{base}_thread_{_safe_id(thread_id)}"
    return base


def html_to_plain_text(value: str) -> str:
    """Convert Teams HTML-ish message text to readable plain text."""
    text = str(value or "")
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", r"\2 \1", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s+", "\n", text)
    return text.strip()


def strip_teams_mentions(
    text: str,
    *,
    bot_id: Optional[str] = None,
    bot_name: Optional[str] = None,
) -> str:
    """Remove Teams bot mentions and normalize user-visible text."""
    cleaned = str(text or "")
    cleaned = re.sub(r"<at[^>]*>.*?</at>", " ", cleaned, flags=re.I | re.S)
    if bot_id:
        cleaned = cleaned.replace(bot_id, " ")
    if bot_name:
        cleaned = re.sub(re.escape(bot_name), " ", cleaned, flags=re.I)
    return html_to_plain_text(cleaned)


def activity_mentions_bot(activity: "TeamsActivity") -> bool:
    """Return True when a Teams activity explicitly mentions the bot."""
    if activity.recipient_id:
        for entity in activity.entities:
            if not isinstance(entity, Mapping) or entity.get("type") != "mention":
                continue
            mentioned = entity.get("mentioned")
            if isinstance(mentioned, Mapping) and mentioned.get("id") == activity.recipient_id:
                return True
    if activity.recipient_name:
        pattern = rf"<at[^>]*>\s*{re.escape(activity.recipient_name)}\s*</at>"
        if re.search(pattern, activity.raw_text, flags=re.I):
            return True
    return False


async def validate_bot_framework_authorization(
    authorization_header: Optional[str],
    *,
    app_id: str,
    service_url: Optional[str] = None,
    openid_config_url: str = BOT_FRAMEWORK_OPENID_CONFIG_URL,
) -> dict[str, Any]:
    """Validate a Bot Framework connector-to-bot JWT.

    This uses the OpenID metadata endpoint published by Bot Framework and
    verifies issuer/audience. If the token includes a serviceUrl claim, it must
    match the incoming activity serviceUrl.
    """
    if not authorization_header or not authorization_header.lower().startswith("bearer "):
        raise BotAPIError("Missing Bot Framework bearer token", status_code=401)
    if not app_id:
        raise BotAPIError("TEAMS_BOT_APP_ID is required for Teams auth validation", status_code=503)
    token = authorization_header.split(" ", 1)[1].strip()
    jwk_client = _JWK_CLIENTS.get(openid_config_url)
    if jwk_client is None:
        metadata = httpx.get(openid_config_url, timeout=10).json()
        jwks_uri = str(metadata.get("jwks_uri") or "")
        if not jwks_uri:
            raise BotAPIError("Bot Framework OpenID metadata missing jwks_uri", status_code=503)
        jwk_client = PyJWKClient(jwks_uri)
        _JWK_CLIENTS[openid_config_url] = jwk_client

    try:
        signing_key = await asyncio.to_thread(jwk_client.get_signing_key_from_jwt, token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=app_id,
            issuer=BOT_FRAMEWORK_ISSUER,
        )
    except InvalidTokenError as exc:
        raise BotAPIError("Invalid Bot Framework bearer token", status_code=401) from exc

    token_service_url = claims.get("serviceurl") or claims.get("serviceUrl")
    if token_service_url and service_url:
        if str(token_service_url).rstrip("/") != str(service_url).rstrip("/"):
            raise BotAPIError("Bot Framework token serviceUrl mismatch", status_code=401)
    return dict(claims)


@dataclass(frozen=True)
class TeamsActivity:
    activity_id: str
    service_url: str
    conversation_id: str
    conversation_type: str
    tenant_id: str
    from_id: str
    from_name: str
    text: str
    raw_text: str = ""
    from_aad_object_id: Optional[str] = None
    recipient_id: Optional[str] = None
    recipient_name: Optional[str] = None
    reply_to_id: Optional[str] = None
    timestamp: Optional[str] = None
    entities: tuple[Mapping[str, Any], ...] = ()
    attachments: tuple[Mapping[str, Any], ...] = ()

    @property
    def is_direct(self) -> bool:
        return self.conversation_type == "personal"

    @property
    def thread_root_id(self) -> Optional[str]:
        if self.is_direct:
            return None
        return extract_conversation_message_id(self.conversation_id) or self.reply_to_id

    @property
    def platform_user_id(self) -> str:
        return make_platform_user_id(
            self.tenant_id,
            aad_object_id=self.from_aad_object_id,
            teams_user_id=self.from_id,
        )


@dataclass(frozen=True)
class TeamsReplyTarget:
    service_url: str
    conversation_id: str
    activity_id: Optional[str] = None
    platform_chat_id: Optional[str] = None


def activity_from_payload(payload: Mapping[str, Any]) -> Optional[TeamsActivity]:
    """Normalize a Bot Framework activity into a Teams message activity."""
    if payload.get("type") != "message":
        return None
    channel_id = str(payload.get("channelId") or "")
    if channel_id and channel_id != "msteams":
        return None

    from_data = payload.get("from")
    conversation = payload.get("conversation")
    if not isinstance(from_data, Mapping) or not isinstance(conversation, Mapping):
        return None

    activity_id = str(payload.get("id") or "").strip()
    service_url = str(payload.get("serviceUrl") or "").strip()
    conversation_id = str(conversation.get("id") or "").strip()
    from_id = str(from_data.get("id") or "").strip()
    if not activity_id or not service_url or not conversation_id or not from_id:
        return None

    attachments: list[Mapping[str, Any]] = [
        attachment for attachment in payload.get("attachments") or [] if isinstance(attachment, Mapping)
    ]
    raw_text = str(payload.get("text") or "")
    text = html_to_plain_text(raw_text)
    if not text:
        text = _extract_text_from_html_attachments(attachments)

    recipient = payload.get("recipient")
    recipient_id = recipient.get("id") if isinstance(recipient, Mapping) else None
    recipient_name = recipient.get("name") if isinstance(recipient, Mapping) else None
    entities = tuple(entity for entity in payload.get("entities") or [] if isinstance(entity, Mapping))

    return TeamsActivity(
        activity_id=activity_id,
        service_url=service_url,
        conversation_id=conversation_id,
        conversation_type=str(conversation.get("conversationType") or "personal"),
        tenant_id=_tenant_from_activity(payload),
        from_id=from_id,
        from_name=str(from_data.get("name") or from_id),
        from_aad_object_id=str(from_data.get("aadObjectId") or "") or None,
        text=text,
        raw_text=raw_text,
        recipient_id=str(recipient_id or "") or None,
        recipient_name=str(recipient_name or "") or None,
        reply_to_id=str(payload.get("replyToId") or "") or None,
        timestamp=str(payload.get("timestamp") or "") or None,
        entities=entities,
        attachments=tuple(attachments),
    )


def _extract_text_from_html_attachments(attachments: list[Mapping[str, Any]]) -> str:
    for attachment in attachments:
        if str(attachment.get("contentType") or "").lower() != "text/html":
            continue
        content = attachment.get("content")
        if isinstance(content, str):
            text = html_to_plain_text(content)
        elif isinstance(content, Mapping):
            text = html_to_plain_text(str(content.get("text") or content.get("body") or ""))
        else:
            text = ""
        if text:
            return text
    return ""


class _SeenActivityCache:
    """TTL cache for Teams webhook activity dedupe."""

    def __init__(
        self,
        *,
        ttl_seconds: int = SEEN_ACTIVITY_TTL_SECONDS,
        max_items: int = SEEN_ACTIVITY_MAX,
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


class TeamsBotFrameworkClient:
    """Minimal Bot Connector REST client for Teams replies."""

    def __init__(
        self,
        *,
        app_id: str,
        app_password: str,
        token_url: str = BOT_FRAMEWORK_TOKEN_URL,
        scope: str = BOT_FRAMEWORK_SCOPE,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.app_id = app_id
        self.app_password = app_password
        self.token_url = token_url
        self.scope = scope
        self._client = http_client or httpx.AsyncClient(timeout=30)
        self._owns_client = http_client is None
        self._access_token = ""
        self._access_token_expires_at = 0.0

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _get_access_token(self) -> str:
        now = time.time()
        if self._access_token and self._access_token_expires_at > now + 60:
            return self._access_token
        response = await self._client.post(
            self.token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self.app_id,
                "client_secret": self.app_password,
                "scope": self.scope,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response.raise_for_status()
        data = response.json()
        token = str(data.get("access_token") or "")
        if not token:
            raise RuntimeError("Bot Framework token response did not include access_token")
        expires_in = int(data.get("expires_in") or 3600)
        self._access_token = token
        self._access_token_expires_at = now + expires_in
        return token

    async def send_message(self, target: TeamsReplyTarget, text: str) -> None:
        token = await self._get_access_token()
        base = target.service_url.rstrip("/")
        conversation_id = quote(target.conversation_id, safe="")
        if target.activity_id:
            activity_id = quote(target.activity_id, safe="")
            url = f"{base}/v3/conversations/{conversation_id}/activities/{activity_id}"
        else:
            url = f"{base}/v3/conversations/{conversation_id}/activities"
        body: dict[str, Any] = {
            "type": "message",
            "text": text,
        }
        if target.activity_id:
            body["replyToId"] = target.activity_id
        response = await self._client.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=body,
        )
        response.raise_for_status()


class TeamsCommand:
    """Small parser for Teams text commands handled before agent routing."""

    def __init__(self, name: str, arg: str = "") -> None:
        self.name = name
        self.arg = arg

    @classmethod
    def parse(
        cls,
        text: str,
        *,
        bot_id: Optional[str] = None,
        bot_name: Optional[str] = None,
    ) -> Optional["TeamsCommand"]:
        cleaned = strip_teams_mentions(text, bot_id=bot_id, bot_name=bot_name)
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


class NymeriaTeamsBot:
    """Microsoft Teams webhook bot — thin client for Nymeria."""

    def __init__(
        self,
        *,
        api: TeamsNymeriaAPI,
        teams_client: TeamsClientProtocol,
        respond_mode: str = "mention",
        show_tool_events: bool = False,
        seen_cache: Optional[_SeenActivityCache] = None,
    ) -> None:
        self.api = api
        self.teams = teams_client
        self.respond_mode = respond_mode
        self.show_tool_events = show_tool_events
        self._seen = seen_cache or _SeenActivityCache()
        self._bindings: dict[str, str] = {}
        self._binding_users: dict[str, str] = {}
        self._active_chats: set[str] = set()

    async def close(self) -> None:
        await self.teams.close()

    async def handle_payload(self, payload: Mapping[str, Any]) -> None:
        activity = activity_from_payload(payload)
        if activity is None:
            return
        await self.handle_activity(activity)

    async def handle_activity(self, activity: TeamsActivity) -> None:
        if self._seen.mark_seen(activity.activity_id):
            return
        if activity.recipient_id and activity.from_id == activity.recipient_id:
            return
        if not activity.text:
            return

        await self.refresh_bindings()
        mentioned = activity_mentions_bot(activity)
        target = TeamsReplyTarget(
            service_url=activity.service_url,
            conversation_id=activity.conversation_id,
            activity_id=activity.activity_id,
            platform_chat_id=self._chat_id_for_activity(activity),
        )
        clean_text = strip_teams_mentions(
            activity.raw_text or activity.text,
            bot_id=activity.recipient_id,
            bot_name=activity.recipient_name,
        ) or activity.text
        command = TeamsCommand.parse(
            activity.raw_text or activity.text,
            bot_id=activity.recipient_id,
            bot_name=activity.recipient_name,
        )

        accepted = activity.is_direct or mentioned or self.respond_mode == "all"
        if not accepted and target.platform_chat_id in self._active_chats:
            accepted = True
        if not accepted:
            return

        if command is not None:
            await self._handle_command(command, activity=activity, target=target)
            return

        user_id = await self.api.resolve_platform_user("teams", activity.platform_user_id)
        if user_id is None:
            await self._reject_unlinked(target, activity.platform_user_id)
            return

        thread_id = self._resolve_thread_id(activity)
        sender = activity.from_name or activity.from_id
        if activity.is_direct:
            prompt = clean_text
        else:
            prompt = f"[Microsoft Teams {sender} in {activity.conversation_type}]\n{clean_text}"
        self._remember_active_chat(target.platform_chat_id or self._chat_id_for_activity(activity))
        await self._stream_to_teams(
            message=prompt,
            thread_id=thread_id,
            user_id=user_id,
            target=target,
        )

    def _remember_active_chat(self, key: str) -> None:
        self._active_chats.add(key)
        if len(self._active_chats) > ACTIVE_CHAT_MAX:
            for old_key in list(self._active_chats)[: ACTIVE_CHAT_MAX // 2]:
                self._active_chats.discard(old_key)

    async def refresh_bindings(self) -> None:
        try:
            entries = await self.api.list_chatapp_bindings(provider="teams")
        except Exception:  # noqa: BLE001
            logger.exception("Failed to refresh Teams chat-app bindings; keeping current cache")
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

    def _resolve_thread_id(self, activity: TeamsActivity) -> str:
        thread_chat_id = self._chat_id_for_activity(activity)
        if thread_chat_id in self._bindings:
            return self._bindings[thread_chat_id]
        base_chat_id = make_platform_chat_id(activity.tenant_id, activity.conversation_id)
        if base_chat_id in self._bindings:
            return self._bindings[base_chat_id]
        return make_thread_id(
            activity.tenant_id,
            activity.conversation_id,
            conversation_type=activity.conversation_type,
            user_id=activity.from_aad_object_id or activity.from_id,
            thread_id=activity.thread_root_id,
        )

    def _chat_id_for_activity(self, activity: TeamsActivity) -> str:
        return make_platform_chat_id(
            activity.tenant_id,
            activity.conversation_id,
            activity.thread_root_id,
        )

    async def _handle_command(
        self,
        command: TeamsCommand,
        *,
        activity: TeamsActivity,
        target: TeamsReplyTarget,
    ) -> None:
        if command.name == "link":
            await self._cmd_link(command.arg, activity, target)
        elif command.name == "bind":
            await self._cmd_bind(command.arg, activity, target)
        elif command.name == "unbind":
            await self._cmd_unbind(activity, target)
        elif command.name == "stop":
            await self._cmd_stop(activity, target)

    async def _cmd_link(
        self,
        code: str,
        activity: TeamsActivity,
        target: TeamsReplyTarget,
    ) -> None:
        if not code:
            await self._send_text(target, "Usage: `link <code>`")
            return
        try:
            result = await self.api.claim_platform_link_code(
                code=code,
                provider="teams",
                platform_user_id=activity.platform_user_id,
            )
        except BotAPIError as exc:
            await self._send_text(target, f"Couldn't link: {exc.detail}")
            return
        await self._send_text(
            target,
            f"Linked this Microsoft Teams account to Nymeria user `{result.get('user_id')}`.",
        )

    async def _cmd_bind(
        self,
        code: str,
        activity: TeamsActivity,
        target: TeamsReplyTarget,
    ) -> None:
        if not code:
            await self._send_text(target, "Usage: `bind <code>`")
            return
        chat_id = self._chat_id_for_activity(activity)
        try:
            result = await self.api.claim_thread_bind_code(
                code=code,
                provider="teams",
                platform_chat_id=chat_id,
                expected_provider_user_id=activity.platform_user_id,
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
        await self._send_text(target, f"Bound this Microsoft Teams chat to `{thread_id}`.")

    async def _cmd_unbind(self, activity: TeamsActivity, target: TeamsReplyTarget) -> None:
        user_id = await self.api.resolve_platform_user("teams", activity.platform_user_id)
        if user_id is None:
            await self._reject_unlinked(target, activity.platform_user_id)
            return
        chat_id = self._chat_id_for_activity(activity)
        try:
            result = await self.api.unbind_chatapp_by_chat(
                provider="teams",
                platform_chat_id=chat_id,
                user_id=user_id,
            )
        except BotAPIError as exc:
            await self._send_text(target, f"Couldn't unbind: {exc.detail}")
            return
        if not result.get("unbound"):
            await self._send_text(target, "This Microsoft Teams chat is not bound.")
            return
        self._bindings.pop(chat_id, None)
        self._binding_users.pop(chat_id, None)
        await self._send_text(
            target,
            "Unbound. Future messages will use the default Microsoft Teams thread.",
        )

    async def _cmd_stop(self, activity: TeamsActivity, target: TeamsReplyTarget) -> None:
        user_id = await self.api.resolve_platform_user("teams", activity.platform_user_id)
        if user_id is None:
            await self._reject_unlinked(target, activity.platform_user_id)
            return
        thread_id = self._resolve_thread_id(activity)
        try:
            await self.api.stop(thread_id, user_id=user_id)
        except BotAPIError as exc:
            await self._send_text(target, f"Couldn't stop the current run: {exc.detail}")
            return
        await self._send_text(target, "Stopped the current run for this Microsoft Teams chat.")

    async def _reject_unlinked(self, target: TeamsReplyTarget, platform_user_id: str) -> None:
        await self._send_text(
            target,
            "This Microsoft Teams account is not linked to a Nymeria user yet.\n"
            "Use `link <code>` here, or ask an admin to run "
            f"`python3 run.py users link-platform <email> teams {platform_user_id}`.",
        )

    async def _stream_to_teams(
        self,
        *,
        message: str,
        thread_id: str,
        user_id: str,
        target: TeamsReplyTarget,
    ) -> None:
        handler = _TeamsStreamHandler(self, target)
        try:
            await consume_sse_stream(self.api.chat_stream(message, thread_id, user_id), handler)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Teams streaming failed; falling back to sync")
            try:
                data = await self.api.chat(message, thread_id, user_id)
                response = str(data.get("response") or "")
                tool_count = data.get("tool_call_count") or 0
                if tool_count:
                    response += f"\n\nTool calls: {tool_count}"
                await self._send_text(target, response or "(no response)")
            except Exception as fallback_exc:  # noqa: BLE001
                logger.exception("Teams sync fallback failed")
                await self._send_text(
                    target,
                    f"Sorry, I encountered an error: {fallback_exc or exc}",
                )

    async def _send_text(self, target: TeamsReplyTarget, content: str) -> None:
        for chunk in split_message(content, TEAMS_TEXT_LIMIT):
            await self.teams.send_message(target, chunk)


class _TeamsStreamHandler:
    """Render Nymeria SSE events into Teams messages."""

    def __init__(self, bot: NymeriaTeamsBot, target: TeamsReplyTarget) -> None:
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
        if len(self._buffer) >= TEAMS_TEXT_LIMIT:
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
