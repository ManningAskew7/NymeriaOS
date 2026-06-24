"""Mattermost bot trigger for two-way Nymeria communication.

Thin client architecture: the bot receives Mattermost WebSocket events,
resolves Mattermost users to Nymeria accounts, and calls the Nymeria REST/SSE
API for all agent work.
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
from .bot_helpers import (
    UserResolver,
    http_error_detail,
    join_api_base,
    normalize_base_url as _normalize_base_url,
    safe_id as _safe_id,
)
from .message_splitter import split_mattermost_message as split_message
from .sse_consumer import consume_sse_stream
from ..core.service_health import HEARTBEAT_INTERVAL_SECONDS, write_service_heartbeat

logger = logging.getLogger(__name__)

MATTERMOST_TEXT_LIMIT = 4000
BINDING_REFRESH_INTERVAL_SECONDS = 60
SEEN_EVENT_TTL_SECONDS = 10 * 60
SEEN_EVENT_MAX = 5000
ACTIVE_THREAD_MAX = 5000


def _websocket_url(base_url: str) -> str:
    normalized = _normalize_base_url(base_url)
    if normalized.endswith("/api/v4"):
        normalized = normalized[: -len("/api/v4")]
    parsed = urlsplit(normalized)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    path = f"{parsed.path.rstrip('/')}/api/v4/websocket"
    return urlunsplit((scheme, parsed.netloc, path, "", ""))


def make_server_key(base_url: str) -> str:
    """Return the Mattermost server key used to scope users and chats."""
    parsed = urlsplit(_normalize_base_url(base_url))
    return _safe_id(parsed.netloc or parsed.path or "mattermost")


def make_platform_user_id(server_key: str, user_id: str) -> str:
    """Return the Mattermost identity key stored in Nymeria platform links."""
    return f"{_safe_id(server_key)}:{_safe_id(user_id)}"


def make_platform_chat_id(
    server_key: str,
    channel_id: str,
    root_id: Optional[str] = None,
) -> str:
    """Return the Mattermost chat key used by Nymeria chat-app bindings."""
    base = f"{_safe_id(server_key)}:{_safe_id(channel_id)}"
    if root_id:
        return f"{base}:{_safe_id(root_id)}"
    return base


def make_thread_id(
    server_key: str,
    channel_id: str,
    *,
    user_id: Optional[str] = None,
    root_id: Optional[str] = None,
    is_dm: bool = False,
) -> str:
    """Generate a native Nymeria thread ID for a Mattermost conversation."""
    server = _safe_id(server_key)
    if is_dm and user_id:
        base = f"mattermost_dm_{server}_{_safe_id(user_id)}"
    else:
        base = f"mattermost_{server}_{_safe_id(channel_id)}"
    if root_id:
        return f"{base}_thread_{_safe_id(root_id)}"
    return base


def strip_bot_mention(
    text: str,
    *,
    bot_username: Optional[str] = None,
    bot_user_id: Optional[str] = None,
) -> str:
    """Remove simple Mattermost bot mentions from user text."""
    cleaned = text.strip()
    for value in (bot_username, bot_user_id):
        if not value:
            continue
        cleaned = re.sub(rf"(^|\s)@{re.escape(value)}\b\s*", " ", cleaned).strip()
    return cleaned


def post_mentions_bot(
    post: "MattermostPost",
    *,
    bot_username: Optional[str] = None,
    bot_user_id: Optional[str] = None,
) -> bool:
    """Return True when a Mattermost post explicitly mentions the bot."""
    message = post.message or ""
    for value in (bot_username, bot_user_id):
        if value and re.search(rf"(^|\s)@{re.escape(value)}\b", message):
            return True
    mentions = post.props_dict.get("mentions")
    if isinstance(mentions, list):
        return any(
            str(item) in {bot_username or "", bot_user_id or ""}
            for item in mentions
        )
    return False


@dataclass(frozen=True)
class MattermostPost:
    id: str
    channel_id: str
    user_id: str
    message: str
    root_id: Optional[str] = None
    team_id: Optional[str] = None
    channel_type: Optional[str] = None
    sender_name: Optional[str] = None
    type: str = ""
    props: Mapping[str, Any] | None = None

    @property
    def props_dict(self) -> Mapping[str, Any]:
        return self.props or {}


@dataclass(frozen=True)
class MattermostReplyTarget:
    channel_id: str
    root_id: Optional[str] = None


class MattermostClientProtocol(Protocol):
    async def get_me(self) -> Mapping[str, Any]:
        """Return the authenticated Mattermost bot user."""

    async def create_post(
        self,
        *,
        channel_id: str,
        message: str,
        root_id: Optional[str] = None,
        file_ids: Optional[list[str]] = None,
    ) -> Mapping[str, Any]:
        """Create a Mattermost post."""

    async def upload_file(
        self, *, channel_id: str, filename: str, data: bytes, content_type: str
    ) -> str:
        """Upload a file to a channel and return its file id."""

    def websocket_events(self) -> AsyncGenerator[Mapping[str, Any], None]:
        """Yield Mattermost WebSocket event frames."""
        ...

    async def close(self) -> None:
        """Close client resources."""


def post_from_websocket_event(event: Mapping[str, Any]) -> Optional[MattermostPost]:
    """Normalize a Mattermost `posted` WebSocket event into a post object."""
    if event.get("event") != "posted":
        return None
    data = event.get("data")
    if not isinstance(data, Mapping):
        return None
    raw_post = data.get("post")
    post_obj: Any
    if isinstance(raw_post, str):
        try:
            post_obj = _json.loads(raw_post)
        except _json.JSONDecodeError:
            return None
    else:
        post_obj = raw_post
    if not isinstance(post_obj, Mapping):
        return None

    post_id = str(post_obj.get("id") or "")
    channel_id = str(post_obj.get("channel_id") or data.get("channel_id") or "")
    user_id = str(post_obj.get("user_id") or "")
    if not post_id or not channel_id or not user_id:
        return None

    props = post_obj.get("props")
    if not isinstance(props, Mapping):
        props = {}
    return MattermostPost(
        id=post_id,
        channel_id=channel_id,
        user_id=user_id,
        message=str(post_obj.get("message") or ""),
        root_id=str(post_obj.get("root_id") or "") or None,
        team_id=str(post_obj.get("team_id") or data.get("team_id") or "") or None,
        channel_type=str(data.get("channel_type") or post_obj.get("channel_type") or "") or None,
        sender_name=str(data.get("sender_name") or post_obj.get("sender_name") or "") or None,
        type=str(post_obj.get("type") or ""),
        props=props,
    )


class _SeenPostCache:
    """TTL cache for Mattermost post dedupe."""

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


class MattermostCommand:
    """Small parser for Mattermost text commands handled before agent routing."""

    def __init__(self, name: str, arg: str = "") -> None:
        self.name = name
        self.arg = arg

    @classmethod
    def parse(
        cls,
        text: str,
        *,
        bot_username: Optional[str] = None,
        bot_user_id: Optional[str] = None,
    ) -> Optional["MattermostCommand"]:
        cleaned = strip_bot_mention(
            text,
            bot_username=bot_username,
            bot_user_id=bot_user_id,
        ).strip()
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


class MattermostHTTPClient:
    """Minimal async Mattermost REST + WebSocket wrapper."""

    def __init__(self, base_url: str, access_token: str) -> None:
        self.base_url = _normalize_base_url(base_url)
        self.api_base_url = join_api_base(base_url, "/api/v4")
        self.access_token = access_token
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=30, write=10, pool=10))

    async def close(self) -> None:
        await self._client.aclose()

    async def get_me(self) -> Mapping[str, Any]:
        return await self._request("GET", "/users/me")

    async def create_post(
        self,
        *,
        channel_id: str,
        message: str,
        root_id: Optional[str] = None,
        file_ids: Optional[list[str]] = None,
    ) -> Mapping[str, Any]:
        body: dict[str, Any] = {"channel_id": channel_id, "message": message}
        if root_id:
            body["root_id"] = root_id
        if file_ids:
            body["file_ids"] = list(file_ids)
        return await self._request("POST", "/posts", json=body)

    async def upload_file(
        self, *, channel_id: str, filename: str, data: bytes, content_type: str
    ) -> str:
        """Upload a file to a channel and return its file id (for create_post)."""
        response = await self._client.post(
            f"{self.api_base_url}/files",
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "Accept": "application/json",
            },
            params={"channel_id": channel_id},
            files={"files": (filename, data, content_type or "application/octet-stream")},
        )
        response.raise_for_status()
        payload = response.json()
        infos = payload.get("file_infos") if isinstance(payload, Mapping) else None
        file_id = ""
        if isinstance(infos, list) and infos and isinstance(infos[0], Mapping):
            file_id = str(infos[0].get("id") or "")
        if not file_id:
            raise RuntimeError("Mattermost upload did not return a file id")
        return file_id

    async def websocket_events(self) -> AsyncGenerator[Mapping[str, Any], None]:
        seq = 1
        async with websockets.connect(_websocket_url(self.base_url), ping_interval=30, ping_timeout=30) as ws:
            await ws.send(
                _json.dumps(
                    {
                        "seq": seq,
                        "action": "authentication_challenge",
                        "data": {"token": self.access_token},
                    }
                )
            )
            async for raw in ws:
                try:
                    data = _json.loads(raw)
                except (TypeError, _json.JSONDecodeError):
                    logger.debug("Ignoring non-JSON Mattermost WebSocket frame")
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
            "Authorization": f"Bearer {self.access_token}",
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


class NymeriaMattermostBot:
    """Mattermost WebSocket bot — thin client for Nymeria."""

    def __init__(
        self,
        api: NymeriaAPIClient,
        *,
        base_url: str,
        access_token: str,
        respond_mode: str = "mention",
        show_tool_events: bool = False,
        mattermost_client: Optional[MattermostClientProtocol] = None,
    ) -> None:
        self.api = api
        self.base_url = _normalize_base_url(base_url)
        self.server_key = make_server_key(base_url)
        self.respond_mode = respond_mode if respond_mode in {"mention", "all"} else "mention"
        self.show_tool_events = show_tool_events
        self.mattermost = mattermost_client or MattermostHTTPClient(base_url, access_token)
        self.bot_user_id: Optional[str] = None
        self.bot_username: Optional[str] = None
        self._running = False
        self._seen = _SeenPostCache()
        self._active_threads: set[str] = set()
        self._bindings: Dict[str, str] = {}
        self._binding_users: Dict[str, str] = {}
        self._user_resolver = UserResolver(self.api, "mattermost", logger=logger)
        self._health_task: Optional[asyncio.Task] = None
        self._bindings_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        identity = await self.mattermost.get_me()
        self.bot_user_id = str(identity.get("id") or "")
        self.bot_username = str(identity.get("username") or "") or None
        if not self.bot_user_id:
            raise RuntimeError("Mattermost account identity could not be resolved.")
        await self._refresh_bindings()
        self._running = True
        self._bindings_task = asyncio.create_task(self._bindings_refresh_loop())
        self._health_task = asyncio.create_task(self._health_heartbeat_loop())
        logger.info(
            "Mattermost bot authenticated as %s on %s",
            self.bot_username or self.bot_user_id,
            self.server_key,
        )
        await self._listen_loop()

    async def close(self) -> None:
        self._running = False
        for task in (self._bindings_task, self._health_task):
            if task:
                task.cancel()
        await self.mattermost.close()
        await self.api.close()

    def run(self) -> None:
        """Synchronous entry point used by run.py."""
        asyncio.run(self.start())

    async def _listen_loop(self) -> None:
        reconnect_delay = 2
        while self._running:
            try:
                async for event in self.mattermost.websocket_events():
                    if not self._running:
                        break
                    await self.handle_websocket_event(event)
                reconnect_delay = 2
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Mattermost WebSocket loop failed; reconnecting")
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 60)

    async def resolve_user_id(self, mattermost_user_id: str) -> Optional[str]:
        platform_user_id = make_platform_user_id(self.server_key, mattermost_user_id)
        return await self._user_resolver.resolve(platform_user_id)

    async def handle_websocket_event(self, event: Mapping[str, Any]) -> None:
        """Handle one Mattermost WebSocket event frame."""
        post = post_from_websocket_event(event)
        if post is None:
            return
        if post.type and post.type.startswith("system_"):
            return
        if self.bot_user_id and post.user_id == self.bot_user_id:
            return
        if self._seen.mark_seen(post.id):
            return

        channel_type = (post.channel_type or "").upper()
        is_dm = channel_type == "D"
        text = post.message.strip()
        if not text:
            return

        mentioned = post_mentions_bot(
            post,
            bot_username=self.bot_username,
            bot_user_id=self.bot_user_id,
        )
        incoming_root = post.root_id
        is_thread_reply = bool(incoming_root and incoming_root != post.id)
        reply_root = incoming_root if incoming_root else (post.id if not is_dm else None)
        active_key = (
            make_platform_chat_id(self.server_key, post.channel_id, reply_root)
            if reply_root
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
            text,
            bot_username=self.bot_username,
            bot_user_id=self.bot_user_id,
        )
        target = MattermostReplyTarget(channel_id=post.channel_id, root_id=reply_root)

        command = MattermostCommand.parse(
            clean_text,
            bot_username=self.bot_username,
            bot_user_id=self.bot_user_id,
        )
        if command is not None:
            await self._handle_command(command, post=post, target=target)
            return

        if not clean_text.strip():
            return

        user_id = await self.resolve_user_id(post.user_id)
        if user_id is None:
            await self._reject_unlinked(target, post.user_id)
            return

        thread_id = self._resolve_thread_id(
            channel_id=post.channel_id,
            mattermost_user_id=post.user_id,
            is_dm=is_dm,
            root_id=reply_root,
        )

        if active_key:
            self._remember_active_thread(active_key)

        prompt = clean_text
        if not is_dm:
            sender = f"@{post.sender_name}" if post.sender_name else post.user_id
            prompt = f"[Mattermost {sender} in {post.channel_id}]\n{clean_text}"

        await self._stream_to_mattermost(
            message=prompt,
            thread_id=thread_id,
            user_id=user_id,
            target=target,
        )

    def _resolve_thread_id(
        self,
        *,
        channel_id: str,
        mattermost_user_id: str,
        is_dm: bool,
        root_id: Optional[str],
    ) -> str:
        candidates: list[str] = []
        if root_id:
            candidates.append(make_platform_chat_id(self.server_key, channel_id, root_id))
        candidates.append(make_platform_chat_id(self.server_key, channel_id))
        for key in candidates:
            bound = self._bindings.get(key)
            if bound:
                return bound
        return make_thread_id(
            self.server_key,
            channel_id,
            user_id=mattermost_user_id,
            root_id=root_id if not is_dm else None,
            is_dm=is_dm,
        )

    def _remember_active_thread(self, key: str) -> None:
        self._active_threads.add(key)
        if len(self._active_threads) > ACTIVE_THREAD_MAX:
            for old_key in list(self._active_threads)[: ACTIVE_THREAD_MAX // 2]:
                self._active_threads.discard(old_key)

    async def _handle_command(
        self,
        command: MattermostCommand,
        *,
        post: MattermostPost,
        target: MattermostReplyTarget,
    ) -> None:
        if command.name == "link":
            await self._cmd_link(command.arg, post.user_id, target)
        elif command.name == "bind":
            await self._cmd_bind(command.arg, post.user_id, post.channel_id, target.root_id, target)
        elif command.name == "unbind":
            await self._cmd_unbind(post.user_id, post.channel_id, target.root_id, target)
        elif command.name == "stop":
            await self._cmd_stop(
                post.user_id,
                post.channel_id,
                target.root_id,
                (post.channel_type or "").upper() == "D",
                target,
            )

    async def _cmd_link(
        self,
        code: str,
        mattermost_user_id: str,
        target: MattermostReplyTarget,
    ) -> None:
        if not code:
            await self._send_text(target, "Usage: `link <code>`")
            return
        platform_user_id = make_platform_user_id(self.server_key, mattermost_user_id)
        try:
            result = await self.api.claim_platform_link_code(
                code=code,
                provider="mattermost",
                platform_user_id=platform_user_id,
            )
        except httpx.HTTPStatusError as exc:
            await self._send_text(target, f"Couldn't link: {http_error_detail(exc)}")
            return
        self._user_resolver.invalidate(platform_user_id)
        await self._send_text(
            target,
            f"Linked this Mattermost account to Nymeria user `{result.get('user_id')}`.",
        )

    async def _cmd_bind(
        self,
        code: str,
        mattermost_user_id: str,
        channel_id: str,
        root_id: Optional[str],
        target: MattermostReplyTarget,
    ) -> None:
        if not code:
            await self._send_text(target, "Usage: `bind <code>`")
            return
        chat_id = make_platform_chat_id(self.server_key, channel_id, root_id)
        platform_user_id = make_platform_user_id(self.server_key, mattermost_user_id)
        try:
            result = await self.api.claim_thread_bind_code(
                code=code,
                provider="mattermost",
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
        await self._send_text(target, f"Bound this Mattermost conversation to `{thread_id}`.")

    async def _cmd_unbind(
        self,
        mattermost_user_id: str,
        channel_id: str,
        root_id: Optional[str],
        target: MattermostReplyTarget,
    ) -> None:
        user_id = await self.resolve_user_id(mattermost_user_id)
        if user_id is None:
            await self._reject_unlinked(target, mattermost_user_id)
            return
        chat_id = make_platform_chat_id(self.server_key, channel_id, root_id)
        try:
            result = await self.api.unbind_chatapp_by_chat(
                provider="mattermost",
                platform_chat_id=chat_id,
                user_id=user_id,
            )
        except httpx.HTTPStatusError as exc:
            await self._send_text(target, f"Couldn't unbind: {http_error_detail(exc)}")
            return
        if not result.get("unbound"):
            await self._send_text(target, "This Mattermost conversation is not bound.")
            return
        self._bindings.pop(chat_id, None)
        self._binding_users.pop(chat_id, None)
        await self._send_text(target, "Unbound. Future messages will use the default Mattermost thread.")

    async def _cmd_stop(
        self,
        mattermost_user_id: str,
        channel_id: str,
        root_id: Optional[str],
        is_dm: bool,
        target: MattermostReplyTarget,
    ) -> None:
        user_id = await self.resolve_user_id(mattermost_user_id)
        if user_id is None:
            await self._reject_unlinked(target, mattermost_user_id)
            return
        thread_id = self._resolve_thread_id(
            channel_id=channel_id,
            mattermost_user_id=mattermost_user_id,
            is_dm=is_dm,
            root_id=root_id,
        )
        try:
            await self.api.stop(thread_id, user_id=user_id)
        except httpx.HTTPStatusError as exc:
            await self._send_text(target, f"Couldn't stop the current run: {http_error_detail(exc)}")
            return
        await self._send_text(target, "Stopped the current run for this Mattermost thread.")

    async def _reject_unlinked(self, target: MattermostReplyTarget, mattermost_user_id: str) -> None:
        platform_user_id = make_platform_user_id(self.server_key, mattermost_user_id)
        await self._send_text(
            target,
            "This Mattermost account is not linked to a Nymeria user yet.\n"
            f"Use `link <code>` here, or ask an admin to run "
            f"`python3 run.py users link-platform <email> mattermost {platform_user_id}`.",
        )

    async def _stream_to_mattermost(
        self,
        *,
        message: str,
        thread_id: str,
        user_id: str,
        target: MattermostReplyTarget,
    ) -> None:
        handler = _MattermostStreamHandler(self, target)
        try:
            await consume_sse_stream(
                self.api.chat_stream(message, thread_id, user_id),
                handler,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Mattermost streaming failed; falling back to sync")
            try:
                data = await self.api.chat(message, thread_id, user_id)
                response = str(data.get("response") or "")
                tool_count = data.get("tool_call_count") or 0
                if tool_count:
                    response += f"\n\nTool calls: {tool_count}"
                await self._send_text(target, response or "(no response)")
            except Exception as fallback_exc:  # noqa: BLE001
                logger.exception("Mattermost sync fallback failed")
                await self._send_text(
                    target,
                    f"Sorry, I encountered an error: {fallback_exc or exc}",
                )

    async def _send_text(self, target: MattermostReplyTarget, content: str) -> None:
        chunks = split_message(content, MATTERMOST_TEXT_LIMIT)
        for index, chunk in enumerate(chunks):
            await self.mattermost.create_post(
                channel_id=target.channel_id,
                message=chunk,
                root_id=target.root_id,
            )
            if index < len(chunks) - 1:
                await asyncio.sleep(1.05)

    async def _send_workspace_attachment(self, target: MattermostReplyTarget, path: str) -> None:
        """Download a generated workspace file and upload it to Mattermost."""
        result = await self.api.download_workspace_file(path)
        if result is None:
            await self._send_text(target, f"Workspace artifact: `{path}`")
            return
        raw_bytes, filename, content_type = result
        try:
            file_id = await self.mattermost.upload_file(
                channel_id=target.channel_id,
                filename=filename,
                data=raw_bytes,
                content_type=content_type,
            )
            await self.mattermost.create_post(
                channel_id=target.channel_id,
                message=f"Generated file: {filename}",
                root_id=target.root_id,
                file_ids=[file_id],
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to upload Mattermost workspace attachment %s: %s", path, e)
            await self._send_text(target, f"Workspace artifact: `{path}`")

    async def _refresh_bindings(self) -> None:
        try:
            entries = await self.api.list_chatapp_bindings(provider="mattermost")
        except Exception:  # noqa: BLE001
            logger.exception("Failed to refresh Mattermost chat-app bindings; keeping current cache")
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
        logger.debug("Mattermost chat-app bindings refreshed: %d entries", len(bindings))

    async def _bindings_refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(BINDING_REFRESH_INTERVAL_SECONDS)
            await self._refresh_bindings()

    async def _health_heartbeat_loop(self) -> None:
        while True:
            try:
                api_ok = await self.api.health()
                write_service_heartbeat(
                    "mattermost-bot",
                    status="ok" if api_ok and self._running else "unhealthy",
                    details={
                        "api": api_ok,
                        "server": self.server_key,
                        "bot_user_id": self.bot_user_id,
                    },
                )
            except Exception:  # noqa: BLE001
                logger.warning("Mattermost health heartbeat failed", exc_info=True)
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)


class _MattermostStreamHandler:
    """Render Nymeria SSE events into Mattermost posts."""

    def __init__(self, bot: NymeriaMattermostBot, target: MattermostReplyTarget) -> None:
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
        if len(self._buffer) >= MATTERMOST_TEXT_LIMIT:
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

    async def on_tool_result(
        self,
        call_id: str,
        result: str,
        attachments: List[str],
    ) -> None:
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
