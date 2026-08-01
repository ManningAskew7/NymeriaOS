"""Credential vault API schemas."""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator

OwnerType = Literal["user", "system"]
CredentialStatus = Literal["active", "pending_setup", "invalid", "disabled"]


def _reject_blank_secret_fields(
    value: Optional[dict[str, str]],
) -> Optional[dict[str, str]]:
    """Refuse secret fields with a blank name or a blank value.

    A SECURITY control, not input tidiness, and it mirrors the rule the
    agent-facing twin has always enforced (``tools/auth_manager.py::auth_write``).
    Until this landed, the REST surface was the looser of the two, and any
    authenticated user could reach it.

    A blank VALUE is the load-bearing half. The destination-join control asks
    which record may supply the address for a request, and answers it by asking
    which record possesses the provider's anchor credentials. A record that names
    an anchor with an empty value used to satisfy that question by NAME while
    contributing nothing to the lookup that consumes the answer, so it could take
    the destination right and let a DIFFERENT record's secret ride to its
    address. That is fixed on the read side too, by judging possession by value
    (``core/credential_vault.has_secret_values``), and both halves stay: this one
    stops the shape being stored, that one covers rows stored before it existed
    and any writer that does not come through these schemas.

    Note this cannot be a "clear the field" idiom that we are breaking. The write
    is an UPSERT with no delete arm, so an empty value never cleared anything; it
    stored an encrypted empty string, which is precisely the shape above.
    """
    if value is None:
        return None
    bad = [
        name
        for name, secret in value.items()
        if not isinstance(name, str)
        or not isinstance(secret, str)
        or not name.strip()
        or not secret.strip()
    ]
    if bad:
        raise ValueError(
            "secret_fields must map non-empty field names to non-empty string "
            f"values; offending field(s): {', '.join(sorted(map(str, bad)))}"
        )
    return value


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

    _no_blank_secrets = field_validator("secret_fields")(_reject_blank_secret_fields)


class CredentialUpdateRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=160)
    account_label: Optional[str] = Field(default=None, max_length=240)
    metadata: Optional[dict[str, Any]] = None
    scopes: Optional[list[str]] = None
    allowed_targets: Optional[list[str]] = None
    expires_at: Optional[str] = None
    status: Optional[CredentialStatus] = None
    secret_fields: Optional[dict[str, str]] = None

    _no_blank_secrets = field_validator("secret_fields")(_reject_blank_secret_fields)


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
