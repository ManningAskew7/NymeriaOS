"""Pydantic schemas for the notification destinations / profiles router."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ChannelTypeField(BaseModel):
    """UI hint for one config key on a channel type."""

    key: str
    label: str
    secret: bool = False
    required: bool = False
    help: Optional[str] = None


class ChannelTypeResponse(BaseModel):
    """Describes a channel type the user can pick when creating a destination."""

    name: str
    description: str
    config_fields: List[ChannelTypeField] = Field(default_factory=list)


class ChannelTypesListResponse(BaseModel):
    channel_types: List[ChannelTypeResponse]


class DestinationCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    type: str = Field(..., min_length=1, max_length=120)
    config: Dict[str, Any] = Field(default_factory=dict)
    secret_fields: Dict[str, str] = Field(default_factory=dict)
    enabled: bool = True


class DestinationUpdateRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    config: Optional[Dict[str, Any]] = None
    # ``secret_fields`` values: a string sets/replaces the field; an explicit
    # null deletes it. Omit the key entirely to leave the stored secret as-is.
    secret_fields: Optional[Dict[str, Optional[str]]] = None
    enabled: Optional[bool] = None


class DestinationResponse(BaseModel):
    id: str
    name: str
    type: str
    config: Dict[str, Any]
    secret_field_names: List[str] = Field(default_factory=list)
    enabled: bool
    created_at: str
    updated_at: str


class DestinationsListResponse(BaseModel):
    destinations: List[DestinationResponse]


class DestinationTestRequest(BaseModel):
    message: str = Field(
        default="Test notification from Nymeria",
        max_length=2000,
    )


class DestinationTestResponse(BaseModel):
    ok: bool
    detail: str


class ProfileCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    destination_names: List[str] = Field(default_factory=list)


class ProfileUpdateRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    destination_names: Optional[List[str]] = None


class ProfileResponse(BaseModel):
    id: str
    name: str
    destination_names: List[str]
    created_at: str
    updated_at: str


class ProfilesListResponse(BaseModel):
    profiles: List[ProfileResponse]


class NotificationPreferencesResponse(BaseModel):
    default_profile: str


class NotificationPreferencesUpdateRequest(BaseModel):
    default_profile: Optional[str] = Field(default=None, min_length=1, max_length=120)
