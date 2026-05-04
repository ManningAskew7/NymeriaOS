"""Account, token, and platform identity API schemas."""

from typing import Any, Literal, Optional

from pydantic import BaseModel


class AdminUserResponse(BaseModel):
    id: str
    email: str
    display_name: str
    role: Literal["user", "admin"]
    disabled: bool
    created_at: str
    updated_at: str
    token_count: int
    last_token_use: Optional[str]
    thread_count: Optional[int] = None
    todo_count: Optional[int] = None
    platform_count: Optional[int] = None


class AdminUserCreateRequest(BaseModel):
    email: str
    display_name: Optional[str] = None
    role: Literal["user", "admin"] = "user"
    id: Optional[str] = None
    token_label: Optional[str] = None


class AdminUserUpdateRequest(BaseModel):
    display_name: Optional[str] = None
    role: Optional[Literal["user", "admin"]] = None
    disabled: Optional[bool] = None


class TokenInfoResponse(BaseModel):
    token_hash_prefix: str
    label: Optional[str]
    created_at: str
    last_used_at: Optional[str]
    revoked_at: Optional[str]


class TokenIssueRequest(BaseModel):
    label: Optional[str] = None


class IssuedTokenResponse(BaseModel):
    raw_token: str
    metadata: TokenInfoResponse


class RotatedTokensResponse(BaseModel):
    raw_token: str
    metadata: TokenInfoResponse
    revoked_count: int


class PlatformIdentityResponse(BaseModel):
    provider: Literal["discord", "telegram", "twitch"]
    provider_user_id: str
    created_at: str


class PlatformLinkRequest(BaseModel):
    provider: Literal["discord", "telegram", "twitch"]
    provider_user_id: str


class MeUpdateRequest(BaseModel):
    display_name: Optional[str] = None


TOKEN_HASH_PREFIX_LEN = 8


def token_info(record: Any) -> TokenInfoResponse:
    """Project a token record without exposing raw token material."""
    return TokenInfoResponse(
        token_hash_prefix=record.token_hash[:TOKEN_HASH_PREFIX_LEN],
        label=record.label,
        created_at=record.created_at,
        last_used_at=record.last_used_at,
        revoked_at=record.revoked_at,
    )
