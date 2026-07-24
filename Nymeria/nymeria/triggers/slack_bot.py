"""Slack bot trigger for two-way Nymeria communication.

Thin client architecture: the bot receives Slack Events API payloads over
Socket Mode, resolves Slack users to Nymeria accounts, and calls the Nymeria
REST/SSE API for all agent work.
"""

from __future__ import annotations

import asyncio
import html
import json as _json
import logging
import re
from dataclasses import dataclass
from functools import partial
from typing import Any, Dict, List, Mapping, Optional

import httpx

from . import attachment_helpers
from .api_client import NymeriaAPIClient
from .bot_helpers import (
    SeenEventCache,
    UserResolver,
    forward_backend_command,
    http_error_detail,
    safe_id as _safe_id,
)
from .message_splitter import split_slack_message as split_message
from .sse_consumer import consume_sse_stream
from ..core.service_health import HEARTBEAT_INTERVAL_SECONDS, write_service_heartbeat

try:  # pragma: no cover - exercised through import-guard tests/mocks.
    from slack_bolt.async_app import AsyncApp
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
except ImportError:  # pragma: no cover - local dev may not have Slack deps.
    AsyncApp = None  # type: ignore[assignment]
    AsyncSocketModeHandler = None  # type: ignore[assignment]

#: True when slack-bolt is importable. run.py checks this for a friendly error.
SDK_AVAILABLE = AsyncApp is not None

logger = logging.getLogger(__name__)

SLACK_TEXT_LIMIT = 3500
BINDING_REFRESH_INTERVAL_SECONDS = 60
ACTIVE_THREAD_MAX = 5000


def _safe_ts(value: str) -> str:
    return _safe_id(value.replace(".", "-"))


def make_platform_user_id(team_id: str, user_id: str) -> str:
    """Return the Slack identity key stored in Nymeria platform links."""
    team = _safe_id(team_id or "workspace")
    user = _safe_id(user_id or "unknown")
    return f"{team}:{user}"


def make_platform_chat_id(
    team_id: str,
    channel_id: str,
    thread_ts: Optional[str] = None,
) -> str:
    """Return the Slack chat key used by Nymeria chat-app bindings."""
    team = _safe_id(team_id or "workspace")
    channel = _safe_id(channel_id)
    if thread_ts:
        return f"{team}:{channel}:{_safe_ts(thread_ts)}"
    return f"{team}:{channel}"


def make_thread_id(
    team_id: str,
    channel_id: str,
    *,
    user_id: Optional[str] = None,
    thread_ts: Optional[str] = None,
    is_dm: bool = False,
) -> str:
    """Generate a native Nymeria thread ID for a Slack conversation."""
    team = _safe_id(team_id or "workspace")
    if is_dm and user_id:
        base = f"slack_dm_{team}_{_safe_id(user_id)}"
    else:
        base = f"slack_{team}_{_safe_id(channel_id)}"
    if thread_ts:
        return f"{base}_thread_{_safe_ts(thread_ts)}"
    return base


def slack_thread_key(team_id: str, channel_id: str, thread_ts: str) -> str:
    """Stable key for in-memory Slack thread participation tracking."""
    return make_platform_chat_id(team_id, channel_id, thread_ts)


def strip_bot_mention(text: str, bot_user_id: Optional[str]) -> str:
    """Remove Slack bot mentions from user text."""
    if not bot_user_id or not text:
        return text.strip()
    return re.sub(rf"<@{re.escape(bot_user_id)}>\s*", "", text).strip()


def slack_markdown(text: str) -> str:
    """Render plain Markdown-ish text safely for Slack mrkdwn.

    This intentionally stays conservative: it escapes Slack's control
    characters and maps common Markdown bold markers to Slack's single-star
    style without attempting a full Markdown parse.
    """
    if not text:
        return ""
    escaped = html.escape(str(text), quote=False)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"*\1*", escaped)
    escaped = re.sub(r"__(.+?)__", r"*\1*", escaped)
    escaped = re.sub(r"~~(.+?)~~", r"~\1~", escaped)
    return escaped


def _event_text(event: Mapping[str, Any]) -> str:
    text = str(event.get("text") or "")
    if text:
        return text
    blocks = event.get("blocks")
    if not isinstance(blocks, list):
        return ""
    parts: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            item_type = value.get("type")
            if item_type == "text" and isinstance(value.get("text"), str):
                parts.append(value["text"])
            elif item_type == "user" and isinstance(value.get("user_id"), str):
                parts.append(f"<@{value['user_id']}>")
            elif item_type == "channel" and isinstance(value.get("channel_id"), str):
                parts.append(f"<#{value['channel_id']}>")
            else:
                for child in value.values():
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(blocks)
    return "".join(parts).strip()


@dataclass(frozen=True)
class SlackReplyTarget:
    channel_id: str
    thread_ts: Optional[str] = None


class SlackCommand:
    """Small parser for Slack text commands handled before agent routing."""

    def __init__(self, name: str, arg: str = "") -> None:
        self.name = name
        self.arg = arg

    @classmethod
    def parse(cls, text: str, bot_user_id: Optional[str] = None) -> Optional["SlackCommand"]:
        cleaned = strip_bot_mention(text, bot_user_id).strip()
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


class NymeriaSlackBot:
    """Slack Socket Mode bot — thin client for Nymeria."""

    def __init__(
        self,
        api: NymeriaAPIClient,
        *,
        bot_token: str,
        app_token: str,
        respond_mode: str = "mention",
        show_tool_events: bool = False,
    ) -> None:
        self.api = api
        self.bot_token = bot_token
        self.app_token = app_token
        self.respond_mode = respond_mode if respond_mode in {"mention", "all"} else "mention"
        self.show_tool_events = show_tool_events

        self._app: Any = None
        self._handler: Any = None
        self._bot_user_id: Optional[str] = None
        self._team_id: Optional[str] = None
        self._team_name: Optional[str] = None
        self._seen = SeenEventCache()
        self._active_threads: set[str] = set()
        self._bindings: Dict[str, str] = {}
        self._user_resolver = UserResolver(self.api, "slack", logger=logger)
        self._health_task: Optional[asyncio.Task] = None
        self._bindings_task: Optional[asyncio.Task] = None

    @property
    def bot_user_id(self) -> Optional[str]:
        return self._bot_user_id

    async def start(self) -> None:
        """Connect to Slack and run until the Socket Mode handler stops."""
        if AsyncApp is None or AsyncSocketModeHandler is None:
            raise RuntimeError(
                "Slack dependencies are not installed. Install slack-bolt and slack-sdk."
            )

        self._app = AsyncApp(token=self.bot_token)
        auth = await self._app.client.auth_test()
        self._bot_user_id = str(auth.get("user_id") or "")
        self._team_id = str(auth.get("team_id") or "")
        self._team_name = str(auth.get("team") or "")

        self._register_handlers()
        await self._refresh_bindings()
        self._bindings_task = asyncio.create_task(self._bindings_refresh_loop())
        self._health_task = asyncio.create_task(self._health_heartbeat_loop())

        logger.info(
            "Slack bot authenticated as %s in workspace %s (%s)",
            self._bot_user_id or "unknown",
            self._team_name or "unknown",
            self._team_id or "unknown",
        )
        self._handler = AsyncSocketModeHandler(self._app, self.app_token)
        await self._handler.start_async()

    async def close(self) -> None:
        """Close Slack/API resources owned by the bot."""
        for task in (self._bindings_task, self._health_task):
            if task:
                task.cancel()
        if self._handler is not None:
            try:
                await self._handler.close_async()
            except Exception:  # noqa: BLE001
                logger.debug("Slack Socket Mode close failed", exc_info=True)
        await self.api.close()

    def run(self) -> None:
        """Synchronous entry point used by run.py."""
        asyncio.run(self.start())

    def _register_handlers(self) -> None:
        @self._app.event("message")
        async def handle_message(event, body=None, say=None):  # noqa: ANN001
            await self.handle_slack_event(event, source="message")

        @self._app.event("app_mention")
        async def handle_app_mention(event, body=None, say=None):  # noqa: ANN001
            await self.handle_slack_event(event, source="app_mention")

        for ignored_event in ("file_shared", "file_created", "file_change"):
            self._app.event(ignored_event)(self._ignore_event)

    async def _ignore_event(self, event=None, body=None, say=None) -> None:  # noqa: ANN001
        return None

    async def resolve_user_id(self, slack_user_id: str, team_id: Optional[str] = None) -> Optional[str]:
        platform_user_id = make_platform_user_id(team_id or self._team_id or "", slack_user_id)
        return await self._user_resolver.resolve(platform_user_id)

    async def _refresh_bindings(self) -> None:
        try:
            entries = await self.api.list_chatapp_bindings(provider="slack")
        except Exception:  # noqa: BLE001
            logger.exception("Failed to refresh Slack chat-app bindings; keeping current cache")
            return
        bindings: Dict[str, str] = {}
        for entry in entries:
            chat_id = str(entry.get("platform_chat_id") or "")
            thread_id = str(entry.get("thread_id") or "")
            if chat_id and thread_id:
                bindings[chat_id] = thread_id
        self._bindings = bindings
        logger.debug("Slack chat-app bindings refreshed: %d entries", len(bindings))

    async def _bindings_refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(BINDING_REFRESH_INTERVAL_SECONDS)
            await self._refresh_bindings()

    async def _health_heartbeat_loop(self) -> None:
        while True:
            try:
                api_ok = await self.api.health()
                connected = self._handler is not None
                write_service_heartbeat(
                    "slack-bot",
                    status="ok" if api_ok and connected else "unhealthy",
                    details={
                        "api": api_ok,
                        "connected": connected,
                        "workspace": self._team_id,
                    },
                )
            except Exception:  # noqa: BLE001
                logger.warning("Slack health heartbeat failed", exc_info=True)
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)

    async def handle_slack_event(self, event: Mapping[str, Any], *, source: str) -> None:
        """Handle a Slack message or app_mention event."""
        if not isinstance(event, Mapping):
            return
        subtype = str(event.get("subtype") or "")
        if subtype in {"message_changed", "message_deleted"}:
            return
        if subtype == "bot_message" or event.get("bot_id"):
            return

        channel_id = str(event.get("channel") or "")
        slack_user_id = str(event.get("user") or "")
        ts = str(event.get("ts") or event.get("event_ts") or "")
        if not channel_id or not slack_user_id or not ts:
            return
        if self._bot_user_id and slack_user_id == self._bot_user_id:
            return

        seen_key = f"{channel_id}:{ts}"
        if self._seen.mark_seen(seen_key):
            return

        team_id = str(
            event.get("team")
            or event.get("team_id")
            or self._team_id
            or "workspace"
        )
        channel_type = str(event.get("channel_type") or "")
        is_dm = channel_type == "im" or channel_id.startswith("D")
        text = _event_text(event)
        mentioned = source == "app_mention" or (
            bool(self._bot_user_id) and f"<@{self._bot_user_id}>" in text
        )
        thread_ts = str(event.get("thread_ts") or "")
        is_thread_reply = bool(thread_ts and thread_ts != ts)
        reply_thread_ts = thread_ts if thread_ts else (ts if not is_dm else None)
        active_key = (
            slack_thread_key(team_id, channel_id, reply_thread_ts)
            if reply_thread_ts
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

        clean_text = strip_bot_mention(text, self._bot_user_id)
        attachments, attachment_errors = await self._collect_attachments(event)
        target = SlackReplyTarget(channel_id=channel_id, thread_ts=reply_thread_ts)

        command = SlackCommand.parse(clean_text, self._bot_user_id)
        if command is not None:
            await self._handle_command(
                command,
                team_id=team_id,
                slack_user_id=slack_user_id,
                channel_id=channel_id,
                thread_ts=reply_thread_ts,
                target=target,
            )
            return

        for error in attachment_errors:
            await self._send_text(target, error)

        if not clean_text.strip() and not attachments:
            return
        if not clean_text.strip() and attachments:
            clean_text = "[attachment]" if len(attachments) == 1 else "[attachments]"

        user_id = await self.resolve_user_id(slack_user_id, team_id=team_id)
        if user_id is None:
            await self._reject_unlinked(target, slack_user_id, team_id)
            return

        thread_id = self._resolve_thread_id(
            team_id=team_id,
            channel_id=channel_id,
            slack_user_id=slack_user_id,
            is_dm=is_dm,
            thread_ts=reply_thread_ts,
        )

        # Generic backend slash-command passthrough. Local commands
        # (link/bind/unbind/stop) were consumed above; any other "/" text is a
        # backend command, except chat_stream-kind commands (e.g. /skill),
        # which fall through to the normal chat path below. Slack's client
        # intercepts leading-"/" messages as Slack-native slash commands
        # (unregistered ones never reach the bot), so "!command" is accepted
        # as an alternative prefix and normalized before forwarding; the
        # mention-then-command form ("@bot /status") arrives intact.
        stripped_text = clean_text.strip()
        if re.match(r"^![A-Za-z]", stripped_text):
            stripped_text = "/" + stripped_text[1:]
        if stripped_text.startswith("/"):
            handled = await forward_backend_command(
                self.api,
                stripped_text,
                thread_id=thread_id,
                user_id=user_id,
                surface="slack",
                send=partial(self._send_text, target),
                logger=logger,
            )
            if handled:
                return

        if active_key:
            self._remember_active_thread(active_key)

        prompt = clean_text
        if not is_dm:
            prompt = f"[Slack <@{slack_user_id}> in {channel_id}]\n{clean_text}"

        await self._stream_to_slack(
            message=prompt,
            thread_id=thread_id,
            user_id=user_id,
            target=target,
            attachments=attachments or None,
        )

    def _resolve_thread_id(
        self,
        *,
        team_id: str,
        channel_id: str,
        slack_user_id: str,
        is_dm: bool,
        thread_ts: Optional[str],
    ) -> str:
        candidates: list[str] = []
        if thread_ts:
            candidates.append(make_platform_chat_id(team_id, channel_id, thread_ts))
        candidates.append(make_platform_chat_id(team_id, channel_id))
        for key in candidates:
            bound = self._bindings.get(key)
            if bound:
                return bound
        return make_thread_id(
            team_id,
            channel_id,
            user_id=slack_user_id,
            thread_ts=thread_ts,
            is_dm=is_dm,
        )

    def _remember_active_thread(self, key: str) -> None:
        self._active_threads.add(key)
        if len(self._active_threads) > ACTIVE_THREAD_MAX:
            for old_key in list(self._active_threads)[: ACTIVE_THREAD_MAX // 2]:
                self._active_threads.discard(old_key)

    async def _handle_command(
        self,
        command: SlackCommand,
        *,
        team_id: str,
        slack_user_id: str,
        channel_id: str,
        thread_ts: Optional[str],
        target: SlackReplyTarget,
    ) -> None:
        if command.name == "link":
            await self._cmd_link(command.arg, team_id, slack_user_id, target)
        elif command.name == "bind":
            await self._cmd_bind(command.arg, team_id, slack_user_id, channel_id, thread_ts, target)
        elif command.name == "unbind":
            await self._cmd_unbind(team_id, slack_user_id, channel_id, thread_ts, target)
        elif command.name == "stop":
            await self._cmd_stop(team_id, slack_user_id, channel_id, thread_ts, target)

    async def _cmd_link(
        self,
        code: str,
        team_id: str,
        slack_user_id: str,
        target: SlackReplyTarget,
    ) -> None:
        if not code:
            await self._send_text(target, "Usage: `link <code>`")
            return
        platform_user_id = make_platform_user_id(team_id, slack_user_id)
        try:
            result = await self.api.claim_platform_link_code(
                code=code,
                provider="slack",
                platform_user_id=platform_user_id,
            )
        except httpx.HTTPStatusError as exc:
            await self._send_text(target, f"Couldn't link: {http_error_detail(exc)}")
            return
        self._user_resolver.invalidate(platform_user_id)
        await self._send_text(
            target,
            f"Linked this Slack account to Nymeria user `{result.get('user_id')}`.",
        )

    async def _cmd_bind(
        self,
        code: str,
        team_id: str,
        slack_user_id: str,
        channel_id: str,
        thread_ts: Optional[str],
        target: SlackReplyTarget,
    ) -> None:
        if not code:
            await self._send_text(target, "Usage: `bind <code>`")
            return
        chat_id = make_platform_chat_id(team_id, channel_id, thread_ts)
        platform_user_id = make_platform_user_id(team_id, slack_user_id)
        try:
            result = await self.api.claim_thread_bind_code(
                code=code,
                provider="slack",
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
        if thread_id:
            self._bindings[chat_id] = thread_id
        await self._send_text(target, f"Bound this Slack conversation to `{thread_id}`.")

    async def _cmd_unbind(
        self,
        team_id: str,
        slack_user_id: str,
        channel_id: str,
        thread_ts: Optional[str],
        target: SlackReplyTarget,
    ) -> None:
        user_id = await self.resolve_user_id(slack_user_id, team_id=team_id)
        if user_id is None:
            await self._reject_unlinked(target, slack_user_id, team_id)
            return
        chat_id = make_platform_chat_id(team_id, channel_id, thread_ts)
        try:
            result = await self.api.unbind_chatapp_by_chat(
                provider="slack",
                platform_chat_id=chat_id,
                user_id=user_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Slack unbind failed")
            await self._send_text(target, f"Couldn't unbind: {exc}")
            return
        if not result.get("unbound"):
            await self._send_text(target, "This Slack conversation is not bound.")
            return
        self._bindings.pop(chat_id, None)
        await self._send_text(target, "Unbound. Future messages will use the default Slack thread.")

    async def _cmd_stop(
        self,
        team_id: str,
        slack_user_id: str,
        channel_id: str,
        thread_ts: Optional[str],
        target: SlackReplyTarget,
    ) -> None:
        user_id = await self.resolve_user_id(slack_user_id, team_id=team_id)
        if user_id is None:
            await self._reject_unlinked(target, slack_user_id, team_id)
            return
        thread_id = self._resolve_thread_id(
            team_id=team_id,
            channel_id=channel_id,
            slack_user_id=slack_user_id,
            is_dm=channel_id.startswith("D"),
            thread_ts=thread_ts,
        )
        try:
            result = await self.api.stop(thread_id, user_id=user_id)
        except Exception as exc:  # noqa: BLE001
            await self._send_text(target, f"Couldn't stop the current run: {exc}")
            return
        from ..core.pending_prompt_queue import restored_prompts_notice

        message = "Stopped the current run for this Slack thread."
        notice = restored_prompts_notice(result.get("restored_prompts") or [])
        if notice:
            message = f"{message}\n\n{notice}"
        await self._send_text(target, message)

    async def _reject_unlinked(
        self,
        target: SlackReplyTarget,
        slack_user_id: str,
        team_id: str,
    ) -> None:
        platform_user_id = make_platform_user_id(team_id, slack_user_id)
        await self._send_text(
            target,
            "This Slack account is not linked to a Nymeria user yet.\n"
            f"Use `link <code>` here, or ask an admin to run "
            f"`python3 run.py users link-platform <email> slack {platform_user_id}`.",
        )

    async def _collect_attachments(
        self,
        event: Mapping[str, Any],
    ) -> tuple[List[Dict[str, Any]], List[str]]:
        files = event.get("files")
        if not isinstance(files, list):
            return [], []
        attachments: List[Dict[str, Any]] = []
        errors: List[str] = []
        for file_obj in files:
            if not isinstance(file_obj, Mapping):
                continue
            name = str(file_obj.get("name") or file_obj.get("title") or "slack-file")
            mime = str(file_obj.get("mimetype") or "") or None
            size = file_obj.get("size")
            ok, size_error = attachment_helpers.size_within_limit(
                int(size) if isinstance(size, int) else None,
                mime,
                name,
            )
            if not ok and size_error:
                errors.append(size_error)
                continue
            url = str(file_obj.get("url_private_download") or file_obj.get("url_private") or "")
            if not url:
                errors.append(f"Couldn't download {name}; Slack did not include a file URL.")
                continue
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    response = await client.get(
                        url,
                        headers={"Authorization": f"Bearer {self.bot_token}"},
                    )
                    response.raise_for_status()
                    raw = response.content
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to download Slack file %s: %s", name, exc)
                errors.append(f"Couldn't download {name}. Try resending it.")
                continue
            built, error = attachment_helpers.build_attachment(raw, mime, name)
            if built:
                attachments.append(built)
            elif error:
                errors.append(error)
            if len(attachments) >= attachment_helpers.MAX_FILES_PER_MESSAGE:
                break
        extra = max(0, len(files) - attachment_helpers.MAX_FILES_PER_MESSAGE)
        if extra:
            errors.append(
                f"Skipped {extra} extra file(s). Max is "
                f"{attachment_helpers.MAX_FILES_PER_MESSAGE} per message."
            )
        return attachments, errors

    async def _stream_to_slack(
        self,
        *,
        message: str,
        thread_id: str,
        user_id: str,
        target: SlackReplyTarget,
        attachments: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        handler = _SlackStreamHandler(self, target)
        try:
            await consume_sse_stream(
                self.api.chat_stream(
                    message,
                    thread_id,
                    user_id,
                    attachments=attachments,
                    force_unsupported_attachments=bool(attachments),
                ),
                handler,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Slack streaming failed; falling back to sync")
            try:
                data = await self.api.chat(
                    message,
                    thread_id,
                    user_id,
                    attachments=attachments,
                    force_unsupported_attachments=bool(attachments),
                )
                response = str(data.get("response") or "")
                tool_count = data.get("tool_call_count") or 0
                if tool_count:
                    response += f"\n\n_Tool calls: {tool_count}_"
                await self._send_text(target, response or "(no response)")
            except Exception as fallback_exc:  # noqa: BLE001
                logger.exception("Slack sync fallback failed")
                await self._send_text(
                    target,
                    f"Sorry, I encountered an error: {fallback_exc or exc}",
                )

    async def _send_text(self, target: SlackReplyTarget, content: str) -> None:
        if not self._app:
            return
        formatted = slack_markdown(content)
        chunks = split_message(formatted, SLACK_TEXT_LIMIT)
        for index, chunk in enumerate(chunks or [""]):
            kwargs: Dict[str, Any] = {
                "channel": target.channel_id,
                "text": chunk,
                "mrkdwn": True,
            }
            if target.thread_ts:
                kwargs["thread_ts"] = target.thread_ts
            await self._app.client.chat_postMessage(**kwargs)
            if index < len(chunks) - 1:
                await asyncio.sleep(1.05)

    async def _send_workspace_attachment(self, target: SlackReplyTarget, path: str) -> None:
        """Download a generated workspace file and upload it to Slack."""
        if not self._app:
            return
        result = await self.api.download_workspace_file(path)
        if result is None:
            await self._send_text(target, f"Workspace artifact: `{path}`")
            return
        raw_bytes, filename, _content_type = result
        try:
            kwargs: Dict[str, Any] = {
                "channel": target.channel_id,
                "file": raw_bytes,
                "filename": filename,
                "title": filename,
            }
            if target.thread_ts:
                kwargs["thread_ts"] = target.thread_ts
            await self._app.client.files_upload_v2(**kwargs)
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to upload Slack workspace attachment %s: %s", path, e)
            await self._send_text(target, f"Workspace artifact: `{path}`")


class _SlackStreamHandler:
    """Render Nymeria SSE events into Slack messages."""

    def __init__(self, bot: NymeriaSlackBot, target: SlackReplyTarget) -> None:
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
        if len(self._buffer) >= SLACK_TEXT_LIMIT:
            await self.flush_text()

    async def on_compacting(self, message: str) -> None:
        await self._bot._send_text(self._target, f"_{message}_")

    async def on_compacted(
        self,
        summary: str,
        messages_removed: int,
        title: str,
    ) -> None:
        detail = f"{title}"
        if messages_removed:
            detail += f" ({messages_removed} messages summarized)"
        if summary:
            detail += f"\n>{summary[:900]}"
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
        body = f"*Tool:* `{name}`"
        if args_text:
            body += f"\n```{args_text}```"
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
            await self._bot._send_text(self._target, f"*Result:*\n```{result_text}```")
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

    async def on_turn_rewound(self, content: str) -> None:
        # A provider refusal was rewound server-side (backlog #105).
        await self._bot._send_text(self._target, content)

    async def on_done(self, tool_call_count: int) -> None:
        if tool_call_count and self._buffer:
            self._buffer += f"\n\n_Tool calls: {tool_call_count}_"
        await self.flush_text(final=True)
        self._done = True

    async def on_stream_end(self, tool_call_count: int) -> None:
        if self._done:
            return
        if tool_call_count and self._buffer:
            self._buffer += f"\n\n_Tool calls: {tool_call_count}_"
        await self.flush_text(final=True)
