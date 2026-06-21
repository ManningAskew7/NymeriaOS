"""Notification channel-type registry.

A *channel type* is a class of delivery target — Telegram, Discord, webhook,
FCM push, Outlook email, etc. Each type knows:

- The shape of its config (which keys, which are secret).
- How to send a message given a configured destination.
- Optionally, how to *auto-seed* a default destination from the global env
  config so existing webhook setups keep working without reconfiguration.

The :class:`~nymeria.tools.notify.notify` tool and the dispatch helpers in
:mod:`nymeria.core.notification_dispatch` resolve destination/profile names
through here. Adding a new delivery channel is just:

1. Subclass :class:`ChannelType`.
2. Register it via :func:`register_channel_type`.

The agent never sees the type list — it only sees user-defined *profile*
names — so growing the registry doesn't grow the agent's tool schema.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    List,
    Optional,
    Protocol,
    runtime_checkable,
)

if TYPE_CHECKING:
    from .notification_destinations import (
        NotificationDestination,
        NotificationDestinationsRepo,
    )

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = 30.0


def _send_with_egress_policy(
    method: str,
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    json: Optional[Any] = None,
    timeout: float = HTTP_TIMEOUT,
):
    """Send a notification request through the shared HTTP egress policy.

    Destination URLs are user-configured and therefore attacker-controllable,
    so every outbound notification is screened (private/loopback/link-local/
    metadata targets blocked, redirects re-validated, DNS pinned) before the
    request leaves the process. ``follow_redirects=False`` preserves the prior
    httpx default; a blocked target raises ``HTTPPolicyViolation`` (a
    ``RuntimeError``) which the callers already surface as a send error.
    """
    from .http_policy import httpx_request_with_policy

    response, _chain, _decision = httpx_request_with_policy(
        method,
        url,
        headers=headers,
        json=json,
        timeout=timeout,
        follow_redirects=False,
    )
    return response


# ---------------------------------------------------------------------------
# Send context + result
# ---------------------------------------------------------------------------


@dataclass
class SendContext:
    """Per-call delivery context shared with every channel type.

    ``settings`` is the global :class:`~nymeria.config.Settings` so legacy
    channel-type implementations can read shared config like
    ``telegram_bot_token`` during the auto-seed transition.
    """

    user_id: str
    thread_id: str
    settings: Any
    task_id: str = ""


@dataclass
class SendResult:
    """Outcome of a single destination send.

    ``ok=True`` means the message reached the channel (or was reliably queued
    for it, e.g. Telegram-bound thread event). ``detail`` is a short human
    string suitable for the in-app audit-log row.
    """

    ok: bool
    detail: str = ""

    @classmethod
    def success(cls, detail: str = "") -> "SendResult":
        return cls(ok=True, detail=detail)

    @classmethod
    def error(cls, detail: str) -> "SendResult":
        return cls(ok=False, detail=detail)


# ---------------------------------------------------------------------------
# Protocol + base
# ---------------------------------------------------------------------------


@runtime_checkable
class ChannelType(Protocol):
    """Contract every registered channel type implements."""

    name: str
    description: str
    config_fields: List[Dict[str, Any]]  # UI hints: [{"key": "...", "label": "...", "secret": False, "required": True}]

    def send(
        self,
        message: str,
        destination: "NotificationDestination",
        ctx: SendContext,
        repo: "NotificationDestinationsRepo",
    ) -> SendResult: ...

    def auto_seed(self, settings: Any) -> Optional[Dict[str, Any]]: ...


class _BaseChannel:
    """Convenience base — concrete channels only override what they need."""

    name: str = ""
    description: str = ""
    config_fields: List[Dict[str, Any]] = []

    def auto_seed(self, settings: Any) -> Optional[Dict[str, Any]]:
        """Return ``{"config": {...}, "secret_fields": {...}}`` to bootstrap a
        default destination from existing env vars, or ``None`` to skip.
        """
        return None


# ---------------------------------------------------------------------------
# Concrete channel types
# ---------------------------------------------------------------------------


class TelegramChannel(_BaseChannel):
    """Telegram. Preserves the thread-binding routing: if the current thread
    is bound to a Telegram chat, the message is queued via the event bus so
    the Telegram bot delivers it to the right chat; otherwise it falls back
    to the configured default chat for the destination.
    """

    name = "telegram"
    description = "Send via the Telegram Bot API"
    config_fields = [
        {"key": "chat_id", "label": "Chat ID", "secret": False, "required": False,
         "help": "Optional default chat. Leave blank to route through thread bindings only."},
        {"key": "bot_token", "label": "Bot Token", "secret": True, "required": False,
         "help": "Override TELEGRAM_BOT_TOKEN for this destination."},
    ]

    def send(self, message, destination, ctx, repo):
        from .notification_dispatch import (
            publish_telegram_thread_notification,
            send_telegram_default,
        )

        routed = publish_telegram_thread_notification(
            message, ctx.user_id, ctx.thread_id,
        )
        if routed is not None:
            return SendResult.success(routed)

        chat_id = destination.config.get("chat_id") or getattr(
            ctx.settings, "telegram_default_chat_id", None,
        )
        bot_token = repo.get_secret_field(
            dest_id=destination.id, field_name="bot_token",
        ) or getattr(ctx.settings, "telegram_bot_token", None)
        if not chat_id or not bot_token:
            return SendResult.error("Telegram not configured (chat_id or bot_token missing)")

        class _Shim:
            telegram_bot_token = bot_token
            telegram_default_chat_id = chat_id

        result = send_telegram_default(message, _Shim())
        if result is None:
            return SendResult.error("Telegram not configured")
        if result.startswith("Sent") or result.startswith("Queued"):
            return SendResult.success(result)
        return SendResult.error(result)

    def auto_seed(self, settings):
        if not getattr(settings, "telegram_bot_token", None):
            return None
        return {
            "config": {
                "chat_id": getattr(settings, "telegram_default_chat_id", "") or "",
            },
            "secret_fields": {},
        }


class DiscordChannel(_BaseChannel):
    """Discord via incoming webhook URL."""

    name = "discord"
    description = "Send via a Discord channel webhook"
    config_fields = [
        {"key": "webhook_url", "label": "Webhook URL", "secret": True, "required": True,
         "help": "Discord channel incoming webhook URL"},
        {"key": "username", "label": "Display name", "secret": False, "required": False,
         "help": "Sender display name (default: Nymeria)"},
    ]

    def send(self, message, destination, ctx, repo):
        webhook_url = repo.get_secret_field(
            dest_id=destination.id, field_name="webhook_url",
        )
        if not webhook_url:
            webhook_url = getattr(ctx.settings, "discord_webhook_url", None)
        if not webhook_url:
            return SendResult.error("Discord webhook URL not configured")
        username = destination.config.get("username") or "Nymeria"
        try:
            response = _send_with_egress_policy(
                "POST",
                webhook_url,
                json={"content": message, "username": username},
            )
            if response.status_code in (200, 204):
                return SendResult.success("Sent to Discord")
            return SendResult.error(f"Discord HTTP {response.status_code}")
        except Exception as exc:
            logger.error("Discord notification failed: %s", exc)
            return SendResult.error(f"Discord error: {exc}")

    def auto_seed(self, settings):
        url = getattr(settings, "discord_webhook_url", None)
        if not url:
            return None
        return {"config": {}, "secret_fields": {"webhook_url": url}}


class SlackChannel(_BaseChannel):
    """Slack via incoming webhook URL."""

    name = "slack"
    description = "Send via a Slack incoming webhook"
    config_fields = [
        {"key": "webhook_url", "label": "Webhook URL", "secret": True, "required": True,
         "help": "Slack incoming webhook URL"},
        {"key": "username", "label": "Display name", "secret": False, "required": False,
         "help": "Sender display name (default: Nymeria)"},
    ]

    def send(self, message, destination, ctx, repo):
        webhook_url = repo.get_secret_field(
            dest_id=destination.id, field_name="webhook_url",
        )
        if not webhook_url:
            webhook_url = getattr(ctx.settings, "slack_webhook_url", None)
        if not webhook_url:
            return SendResult.error("Slack webhook URL not configured")
        username = destination.config.get("username") or "Nymeria"
        try:
            response = _send_with_egress_policy(
                "POST",
                webhook_url,
                json={"text": message, "username": username},
            )
            if response.status_code == 200 and response.text == "ok":
                return SendResult.success("Sent to Slack")
            return SendResult.error(f"Slack error: {response.text[:100]}")
        except Exception as exc:
            logger.error("Slack notification failed: %s", exc)
            return SendResult.error(f"Slack error: {exc}")

    def auto_seed(self, settings):
        url = getattr(settings, "slack_webhook_url", None)
        if not url:
            return None
        return {"config": {}, "secret_fields": {"webhook_url": url}}


class TeamsChannel(_BaseChannel):
    """Microsoft Teams via Graph API + Outlook OAuth token cache."""

    name = "teams"
    description = "Post to a Microsoft Teams channel via Graph API"
    config_fields = [
        {"key": "team_id", "label": "Team ID", "secret": False, "required": True},
        {"key": "channel_id", "label": "Channel ID", "secret": False, "required": True},
        {"key": "account_id", "label": "Outlook account ID", "secret": False, "required": False,
         "help": "Which authenticated Microsoft account to send as. Defaults to the user's primary."},
    ]

    def send(self, message, destination, ctx, repo):
        team_id = destination.config.get("team_id")
        channel_id = destination.config.get("channel_id")
        if not team_id or not channel_id:
            return SendResult.error("Teams team_id / channel_id missing")
        try:
            from ..tools.outlook_email import GRAPH_BASE, get_access_token
        except ImportError:
            return SendResult.error("outlook_email module unavailable")

        account_id = destination.config.get("account_id") or getattr(
            ctx.settings, "teams_account_id", None,
        )
        token = get_access_token(ctx.user_id, account_id)
        if not token:
            return SendResult.error("No authenticated Microsoft account")

        url = f"{GRAPH_BASE}/teams/{team_id}/channels/{channel_id}/messages"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        payload = {"body": {"contentType": "text", "content": message}}
        try:
            response = _send_with_egress_policy(
                "POST", url, headers=headers, json=payload,
            )
            if response.status_code == 201:
                return SendResult.success("Sent to Teams")
            try:
                err = response.json().get("error", {}).get("message", response.text[:200])
            except Exception:
                err = response.text[:200]
            return SendResult.error(f"Teams HTTP {response.status_code}: {err}")
        except Exception as exc:
            logger.error("Teams notification failed: %s", exc)
            return SendResult.error(f"Teams error: {exc}")

    def auto_seed(self, settings):
        team_id = getattr(settings, "teams_team_id", None)
        channel_id = getattr(settings, "teams_channel_id", None)
        if not team_id or not channel_id:
            return None
        return {
            "config": {
                "team_id": team_id,
                "channel_id": channel_id,
                "account_id": getattr(settings, "teams_account_id", "") or "",
            },
            "secret_fields": {},
        }


class WebhookChannel(_BaseChannel):
    """Generic JSON-POST webhook. Lets users wire to anything (ntfy.sh,
    Home Assistant, IFTTT, custom backends) without a per-service channel
    type. Payload schema:

    ``{"message": str, "summary": str, "user_id": str, "thread_id": str,
    "task_id": str}``

    Optional bearer token (secret) and a small dict of custom headers (also
    secret since auth headers are common) are sent on the request.
    """

    name = "webhook"
    description = "POST JSON to any URL"
    config_fields = [
        {"key": "url", "label": "URL", "secret": False, "required": True,
         "help": "POST endpoint"},
        {"key": "method", "label": "HTTP method", "secret": False, "required": False,
         "help": "POST (default) or PUT"},
        {"key": "bearer_token", "label": "Bearer token", "secret": True, "required": False,
         "help": "Sent as Authorization: Bearer <token>"},
        {"key": "extra_headers_json", "label": "Extra headers (JSON)", "secret": True, "required": False,
         "help": "JSON object of additional headers, e.g. {\"X-Auth\": \"...\"}"},
    ]

    def send(self, message, destination, ctx, repo):
        import json as _json

        url = destination.config.get("url")
        if not url:
            return SendResult.error("Webhook URL missing")
        method = (destination.config.get("method") or "POST").upper()
        headers: Dict[str, str] = {"Content-Type": "application/json"}

        token = repo.get_secret_field(dest_id=destination.id, field_name="bearer_token")
        if token:
            headers["Authorization"] = f"Bearer {token}"

        extra = repo.get_secret_field(
            dest_id=destination.id, field_name="extra_headers_json",
        )
        if extra:
            try:
                headers.update(_json.loads(extra))
            except Exception as exc:
                logger.warning("Webhook extra_headers_json parse failed: %s", exc)

        payload = {
            "message": message,
            "summary": message[:200],
            "user_id": ctx.user_id,
            "thread_id": ctx.thread_id,
            "task_id": ctx.task_id,
        }
        try:
            response = _send_with_egress_policy(method, url, headers=headers, json=payload)
            if 200 <= response.status_code < 300:
                return SendResult.success(f"Sent webhook to {url}")
            return SendResult.error(f"Webhook HTTP {response.status_code}: {response.text[:120]}")
        except Exception as exc:
            logger.error("Webhook notification failed: %s", exc)
            return SendResult.error(f"Webhook error: {exc}")


class FCMChannel(_BaseChannel):
    """Firebase Cloud Messaging push notifications.

    Reuses the per-user FCM token registry built for autonomous task
    completions (see :mod:`nymeria.core.fcm`). When ``device_token`` is set
    on the destination, sends to that specific device only; otherwise
    broadcasts to every device the user has registered.
    """

    name = "fcm"
    description = "Push to mobile / WearOS devices via Firebase Cloud Messaging"
    config_fields = [
        {"key": "device_token", "label": "Device token", "secret": True, "required": False,
         "help": "Send only to this device. Leave blank to broadcast to all the user's devices."},
    ]

    def send(self, message, destination, ctx, repo):
        if not getattr(ctx.settings, "fcm_enabled", False):
            return SendResult.error("FCM not enabled in settings")
        try:
            from .fcm import send_push, send_to_all_devices
        except ImportError as exc:
            return SendResult.error(f"FCM unavailable: {exc}")

        token = repo.get_secret_field(
            dest_id=destination.id, field_name="device_token",
        )
        try:
            if token:
                send_push(
                    token=token,
                    text=message,
                    thread_id=ctx.thread_id,
                    task_id=ctx.task_id,
                    summary=message[:200],
                )
                return SendResult.success("Sent FCM push to device")
            send_to_all_devices(
                data_dir=str(ctx.settings.data_dir),
                text=message,
                thread_id=ctx.thread_id,
                task_id=ctx.task_id,
                summary=message[:200],
                user_id=ctx.user_id,
            )
            return SendResult.success("Sent FCM push to all user devices")
        except Exception as exc:
            logger.error("FCM notification failed: %s", exc)
            return SendResult.error(f"FCM error: {exc}")


class EmailOutlookChannel(_BaseChannel):
    """Email via the user's authenticated Outlook (Microsoft Graph) account.

    Reuses the OAuth token cache built for the ``outlook_*`` tools so users
    do NOT need to configure SMTP — if they have authenticated Outlook for
    any other Nymeria feature, this channel works immediately.
    """

    name = "email_outlook"
    description = "Send email via your authenticated Outlook account"
    config_fields = [
        {"key": "to", "label": "Recipient email", "secret": False, "required": True},
        {"key": "subject_prefix", "label": "Subject prefix", "secret": False, "required": False,
         "help": "Prepended to the subject, e.g. '[Nymeria]'"},
        {"key": "account_id", "label": "Outlook account ID", "secret": False, "required": False,
         "help": "Which authenticated Outlook account to send from. Defaults to the user's primary."},
    ]

    def send(self, message, destination, ctx, repo):
        to_addr = destination.config.get("to")
        if not to_addr:
            return SendResult.error("Email 'to' missing")
        try:
            from ..tools.outlook_email import GRAPH_BASE, get_access_token
        except ImportError:
            return SendResult.error("outlook_email module unavailable")

        account_id = destination.config.get("account_id") or None
        token = get_access_token(ctx.user_id, account_id)
        if not token:
            return SendResult.error("No authenticated Outlook account")

        prefix = (destination.config.get("subject_prefix") or "").strip()
        first_line = message.split("\n", 1)[0].strip() or "Nymeria notification"
        subject = f"{prefix} {first_line}".strip() if prefix else first_line
        subject = subject[:200]

        payload = {
            "message": {
                "subject": subject,
                "body": {"contentType": "Text", "content": message},
                "toRecipients": [{"emailAddress": {"address": to_addr}}],
            },
            "saveToSentItems": True,
        }
        try:
            response = _send_with_egress_policy(
                "POST",
                f"{GRAPH_BASE}/me/sendMail",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            if response.status_code in (200, 202):
                return SendResult.success(f"Sent email to {to_addr}")
            return SendResult.error(
                f"Email HTTP {response.status_code}: {response.text[:120]}",
            )
        except Exception as exc:
            logger.error("Email (Outlook) notification failed: %s", exc)
            return SendResult.error(f"Email error: {exc}")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


_REGISTRY: Dict[str, ChannelType] = {}


def register_channel_type(channel: ChannelType) -> None:
    """Register a channel type. Idempotent — later registrations replace
    earlier ones for the same ``name``.
    """
    _REGISTRY[channel.name] = channel
    logger.debug("Registered notification channel type: %s", channel.name)


def get_channel_type(name: str) -> Optional[ChannelType]:
    return _REGISTRY.get(name)


def list_channel_types() -> List[ChannelType]:
    return list(_REGISTRY.values())


def list_channel_type_names() -> List[str]:
    return sorted(_REGISTRY.keys())


# Register built-ins on import.
for _ch in (
    TelegramChannel(),
    DiscordChannel(),
    SlackChannel(),
    TeamsChannel(),
    WebhookChannel(),
    FCMChannel(),
    EmailOutlookChannel(),
):
    register_channel_type(_ch)


# ---------------------------------------------------------------------------
# Auto-seed
# ---------------------------------------------------------------------------


_AUTO_SEED_DEFAULT_NAMES: Dict[str, str] = {
    "telegram": "telegram-default",
    "discord": "discord-default",
    "slack": "slack-default",
    "teams": "teams-default",
}


def ensure_seeded_destinations(
    *,
    user_id: str,
    settings: Any,
    repo: "NotificationDestinationsRepo",
    default_profile_name: str = "default",
) -> List[str]:
    """Idempotent: for every channel type with an ``auto_seed`` payload and
    no existing destination of the same default name, create one. If the
    user has no profile named *default_profile_name*, create one whose
    destination list is the freshly-seeded set merged with whatever already
    exists with the default names.

    Returns the destination names that were created (empty if everything was
    already seeded). Safe to call on every startup or first notify call.
    """
    from .notification_destinations import DestinationAlreadyExists

    created: List[str] = []
    seeded_names: List[str] = []
    for channel in list_channel_types():
        seed = channel.auto_seed(settings)
        if not seed:
            continue
        default_name = _AUTO_SEED_DEFAULT_NAMES.get(channel.name)
        if not default_name:
            continue
        seeded_names.append(default_name)
        existing = repo.get_destination_by_name(user_id=user_id, name=default_name)
        if existing is not None:
            continue
        try:
            repo.create_destination(
                user_id=user_id,
                name=default_name,
                type=channel.name,
                config=seed.get("config") or {},
                secret_fields=seed.get("secret_fields") or {},
                enabled=True,
            )
            created.append(default_name)
        except DestinationAlreadyExists:
            # Race: another caller (or a prior partial seed) created it
            # between get_destination_by_name and create_destination. Safe
            # to ignore; idempotency is the whole point of this function.
            logger.debug("ensure_seeded_destinations: race for %s", default_name)

    profile = repo.get_profile_by_name(user_id=user_id, name=default_profile_name)
    if profile is None and seeded_names:
        try:
            repo.create_profile(
                user_id=user_id,
                name=default_profile_name,
                destination_names=seeded_names,
            )
        except Exception:
            logger.debug(
                "ensure_seeded_destinations: profile creation race for user=%s",
                user_id,
            )
    elif profile is not None and created:
        # Existing profile -- append newly-seeded names that aren't yet listed.
        merged = list(profile.destination_names)
        for name in created:
            if name not in merged:
                merged.append(name)
        if merged != profile.destination_names:
            repo.update_profile(
                user_id=user_id,
                profile_id=profile.id,
                destination_names=merged,
            )

    return created


# ---------------------------------------------------------------------------
# Profile resolution + dispatch
# ---------------------------------------------------------------------------


@dataclass
class DispatchResult:
    """Aggregate outcome of sending a notification through a profile."""

    profile_name: str
    attempted: List[str] = field(default_factory=list)
    delivered_to: List[str] = field(default_factory=list)
    errors: Dict[str, str] = field(default_factory=dict)

    def any_success(self) -> bool:
        return bool(self.delivered_to)


def resolve_profile_name(
    *,
    user_id: str,
    repo: "NotificationDestinationsRepo",
    explicit: Optional[str] = None,
    thread_default: Optional[str] = None,
    user_default: Optional[str] = None,
    fallback: str = "default",
) -> str:
    """Pick the active profile name in resolution order.

    Resolution: per-call ``explicit`` > thread override > user default >
    ``fallback`` (built-in "default"). Returned name is NOT validated to
    exist — :func:`dispatch_to_profile` handles missing profiles by treating
    them as empty (delivering nothing) so the caller's audit-log entry still
    records the attempt.
    """
    if explicit:
        return explicit
    if thread_default:
        return thread_default
    if user_default:
        return user_default
    return fallback


def dispatch_to_profile(
    *,
    message: str,
    profile_name: str,
    ctx: SendContext,
    repo: "NotificationDestinationsRepo",
) -> DispatchResult:
    """Look up *profile_name* and send to every destination it references.

    Disabled destinations are skipped (no error recorded). Missing
    destination names (referenced by the profile but deleted) ARE recorded
    as errors so the user can clean up stale references.
    """
    result = DispatchResult(profile_name=profile_name)
    profile = repo.get_profile_by_name(user_id=ctx.user_id, name=profile_name)
    if profile is None:
        return result

    for dest_name in profile.destination_names:
        destination = repo.get_destination_by_name(
            user_id=ctx.user_id, name=dest_name,
        )
        if destination is None:
            result.attempted.append(dest_name)
            result.errors[dest_name] = "destination not found"
            continue
        if not destination.enabled:
            continue
        result.attempted.append(dest_name)
        channel = get_channel_type(destination.type)
        if channel is None:
            result.errors[dest_name] = f"unknown channel type '{destination.type}'"
            continue
        try:
            send_result = channel.send(message, destination, ctx, repo)
        except Exception as exc:
            logger.exception(
                "Channel %s raised while sending to destination %s",
                destination.type,
                dest_name,
            )
            result.errors[dest_name] = str(exc)
            continue
        if send_result.ok:
            result.delivered_to.append(dest_name)
        else:
            result.errors[dest_name] = send_result.detail or "send failed"
    return result
