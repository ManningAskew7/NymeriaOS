"""Credential vault API schemas."""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

OwnerType = Literal["user", "system"]
CredentialStatus = Literal["active", "pending_setup", "invalid", "disabled"]


class CredentialResponse(BaseModel):
    id: str
    owner_type: OwnerType
    owner_user_id: Optional[str] = None
    name: str
    provider: str
    kind: str
    account_label: Optional[str] = None
    status: str
    metadata: dict[str, Any] = {}
    scopes: list[str] = []
    # A plain list, unlike the REQUEST models below. The None-means-unspecified
    # distinction is about what a client OMITS; a response always reports the
    # stored value, and ``record.public_dict()`` always has one. Making this
    # optional only loosened the published OpenAPI contract to ``array | null``
    # for a null that is never emitted.
    allowed_targets: list[str] = []
    expires_at: Optional[str] = None
    last_used_at: Optional[str] = None
    last_tested_at: Optional[str] = None
    disabled_at: Optional[str] = None
    created_by_user_id: Optional[str] = None
    created_at: str
    updated_at: str
    secret_fields: list[str] = []
    has_secret: bool = False


class CredentialListResponse(BaseModel):
    credentials: list[CredentialResponse]
    total: int


class CredentialCreateRequest(BaseModel):
    owner_type: OwnerType = "user"
    owner_user_id: Optional[str] = None
    name: str = Field(..., min_length=1, max_length=160)
    provider: str = Field(..., min_length=1, max_length=80)
    kind: str = Field(default="api_key", min_length=1, max_length=80)
    account_label: Optional[str] = Field(default=None, max_length=240)
    metadata: dict[str, Any] = {}
    scopes: list[str] = []
    # ``None``, not ``[]``. An empty list is a real instruction to the vault
    # ("nothing may read this"), so it must not also be what a client that
    # simply omitted the field sends. Omitting it now means "use the default
    # for this kind"; locking a credential down takes an explicit ``[]``.
    allowed_targets: Optional[list[str]] = None
    expires_at: Optional[str] = None
    status: CredentialStatus = "active"
    secret_fields: dict[str, str] = Field(default_factory=dict)


class CredentialUpdateRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=160)
    account_label: Optional[str] = Field(default=None, max_length=240)
    metadata: Optional[dict[str, Any]] = None
    scopes: Optional[list[str]] = None
    allowed_targets: Optional[list[str]] = None
    expires_at: Optional[str] = None
    status: Optional[CredentialStatus] = None
    secret_fields: Optional[dict[str, str]] = None


class CredentialBindingRequest(BaseModel):
    target_type: str = Field(..., min_length=1, max_length=80)
    target_id: str = Field(..., min_length=1, max_length=160)
    binding_name: Optional[str] = Field(default=None, max_length=80)


class CredentialBindingResponse(BaseModel):
    id: str
    credential_id: str
    target_type: str
    target_id: str
    binding_name: Optional[str] = None
    created_by_user_id: Optional[str] = None
    created_at: str


class CredentialSetupSessionRequest(BaseModel):
    provider: str = Field(..., min_length=1, max_length=80)
    kind: str = Field(default="api_key", min_length=1, max_length=80)
    name: str = Field(..., min_length=1, max_length=160)
    target_type: Optional[str] = Field(default=None, max_length=80)
    target_id: Optional[str] = Field(default=None, max_length=160)
    required_fields: list[str] = Field(default_factory=lambda: ["value"])
    metadata: dict[str, Any] = {}


def credential_to_response(record: Any) -> CredentialResponse:
    return CredentialResponse(**record.public_dict())
