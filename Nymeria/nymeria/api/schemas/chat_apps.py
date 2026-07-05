"""Chat-app binding and user-owned Telegram bot API schemas."""

from typing import Literal

from pydantic import BaseModel


ChatAppProvider = Literal[
    "discord",
    "telegram",
    "twitch",
    "slack",
    "whatsapp",
    "teams",
]


class ChatAppProviderField(BaseModel):
    """Body fragment shared by chatapp endpoints."""

    provider: ChatAppProvider


class ChatAppBindCodeRequest(ChatAppProviderField):
    """Request a short-lived code to bind a chat app to a Nymeria thread."""


class ChatAppBindCodeResponse(BaseModel):
    code: str
    expires_at: str
    bot_username: str | None = None
    deep_link: str | None = None


class PlatformLinkCodeRequest(ChatAppProviderField):
    """Request a code to link the caller's chat-app identity to their account."""


# Response models use a plain str provider, not ChatAppProvider: the DB can
# hold binding/link rows for platforms whose bots were later removed (e.g. the
# 2026-07-05 bot cull), and reads must serialize those rows instead of 500ing.
# Request models stay strictly validated via ChatAppProvider.


class ChatAppBindingResponse(BaseModel):
    id: int
    thread_id: str
    provider: str
    platform_chat_id: str
    created_at: str
    user_telegram_bot_id: int | None = None


class AdminBindingLookupResponse(BaseModel):
    id: int
    thread_id: str
    provider: str
    platform_chat_id: str
    user_id: str
    created_at: str
    user_telegram_bot_id: int | None = None


class AdminChatAppBindClaimRequest(BaseModel):
    code: str
    provider: ChatAppProvider
    platform_chat_id: str
    expected_provider_user_id: str


class AdminChatAppBindClaimResponse(BaseModel):
    binding_id: int
    thread_id: str
    user_id: str


class AdminChatAppSwitchRequest(BaseModel):
    provider: ChatAppProvider
    platform_chat_id: str
    thread_id: str
    user_id: str
    user_telegram_bot_id: int | None = None


class AdminChatAppSwitchResponse(BaseModel):
    binding_id: int
    thread_id: str
    user_id: str
    previous_thread_id: str | None = None


class AdminPlatformLinkClaimRequest(BaseModel):
    code: str
    provider: ChatAppProvider
    platform_user_id: str


class AdminPlatformLinkClaimResponse(BaseModel):
    user_id: str
    provider: str
    provider_user_id: str
    created_at: str


class MyTelegramBotResponse(BaseModel):
    """User-facing bot record. Token material is never exposed here."""

    id: int
    bot_username: str
    enabled: bool
    created_at: str
    last_seen_at: str | None


class RegisterTelegramBotRequest(BaseModel):
    """Body for user-owned Telegram bot registration."""

    bot_token: str


class AdminTelegramBotResponse(BaseModel):
    """Admin-only bot record including decrypted token for the supervisor."""

    id: int
    owner_user_id: str
    bot_username: str
    bot_token: str
    enabled: bool
    created_at: str
    last_seen_at: str | None


class AdminChatAppBindClaimViaBotRequest(BaseModel):
    code: str
    provider: Literal["telegram"]
    platform_chat_id: str
    via_user_telegram_bot_id: int
