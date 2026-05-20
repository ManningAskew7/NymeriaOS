"""REST endpoints for notification destinations, profiles, and per-user
notification preferences.

All routes are user-scoped via ``Act-As`` resolution: each call operates on
the destinations/profiles owned by the authenticated user (or by the user
the admin is acting as). Admins can manage any user's notification setup by
passing ``user_id`` explicitly in the query string the same way the activity
and notifications routes do.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from ...core.accounts import AuthenticatedUser
from ...core.notification_channels import (
    SendContext,
    ensure_seeded_destinations,
    get_channel_type,
    list_channel_types,
)
from ...core.notification_destinations import (
    DestinationAlreadyExists,
    DestinationNotFound,
    ProfileAlreadyExists,
    ProfileNotFound,
    get_destinations_repo,
)
from ..schemas.notifications_config import (
    ChannelTypeField,
    ChannelTypeResponse,
    ChannelTypesListResponse,
    DestinationCreateRequest,
    DestinationResponse,
    DestinationsListResponse,
    DestinationTestRequest,
    DestinationTestResponse,
    DestinationUpdateRequest,
    NotificationPreferencesResponse,
    NotificationPreferencesUpdateRequest,
    ProfileCreateRequest,
    ProfileResponse,
    ProfilesListResponse,
    ProfileUpdateRequest,
)

logger = logging.getLogger(__name__)


def _destination_to_response(
    dest, secret_field_names,
) -> DestinationResponse:
    return DestinationResponse(
        id=dest.id,
        name=dest.name,
        type=dest.type,
        config=dest.config,
        secret_field_names=list(secret_field_names),
        enabled=dest.enabled,
        created_at=dest.created_at,
        updated_at=dest.updated_at,
    )


def _profile_to_response(profile) -> ProfileResponse:
    return ProfileResponse(
        id=profile.id,
        name=profile.name,
        destination_names=list(profile.destination_names),
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )


def create_notifications_config_router(
    verify_api_key: Callable[..., Any],
    authed_user_id: Callable[..., Any],
    get_agent_fn: Callable[[], Any],
    get_settings_fn: Callable[[], Any],
) -> APIRouter:
    """Build the notifications-config router with app deps injected."""
    router = APIRouter(tags=["Notifications"])

    # -- channel types -----------------------------------------------------

    @router.get(
        "/notifications/channel-types",
        response_model=ChannelTypesListResponse,
    )
    async def list_channel_types_endpoint(
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Return the registered channel types and their UI hints.

        The frontend uses this to render the destination editor: for each
        channel type it shows the right config keys with the right labels
        and marks which ones are secrets.
        """
        types = []
        for ch in list_channel_types():
            types.append(
                ChannelTypeResponse(
                    name=ch.name,
                    description=ch.description,
                    config_fields=[
                        ChannelTypeField(**field) for field in ch.config_fields
                    ],
                )
            )
        return ChannelTypesListResponse(channel_types=types)

    # -- destinations ------------------------------------------------------

    def _list_destinations(user_id: str):
        repo = get_destinations_repo()
        settings = get_settings_fn()
        # First-touch auto-seed so users with existing global env config
        # see their telegram/discord/slack/teams destinations immediately.
        try:
            ensure_seeded_destinations(
                user_id=user_id, settings=settings, repo=repo,
            )
        except Exception as exc:
            logger.debug("auto-seed skipped for %s: %s", user_id, exc)
        rows = []
        for dest in repo.list_destinations(user_id=user_id):
            rows.append(
                _destination_to_response(
                    dest,
                    repo.list_secret_field_names(dest_id=dest.id),
                )
            )
        return rows

    @router.get(
        "/notifications/destinations",
        response_model=DestinationsListResponse,
    )
    async def list_destinations_endpoint(
        user_id: str = Depends(authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        return DestinationsListResponse(destinations=_list_destinations(user_id))

    @router.post(
        "/notifications/destinations",
        response_model=DestinationResponse,
    )
    async def create_destination_endpoint(
        request: DestinationCreateRequest,
        user_id: str = Depends(authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        if get_channel_type(request.type) is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Unknown channel type '{request.type}'. Call "
                    "GET /notifications/channel-types to list available types."
                ),
            )
        repo = get_destinations_repo()
        try:
            dest = repo.create_destination(
                user_id=user_id,
                name=request.name,
                type=request.type,
                config=request.config,
                secret_fields=request.secret_fields,
                enabled=request.enabled,
            )
        except DestinationAlreadyExists as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _destination_to_response(
            dest, repo.list_secret_field_names(dest_id=dest.id),
        )

    @router.get(
        "/notifications/destinations/{dest_id}",
        response_model=DestinationResponse,
    )
    async def get_destination_endpoint(
        dest_id: str,
        user_id: str = Depends(authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        repo = get_destinations_repo()
        dest = repo.get_destination(user_id=user_id, dest_id=dest_id)
        if dest is None:
            raise HTTPException(status_code=404, detail="Destination not found")
        return _destination_to_response(
            dest, repo.list_secret_field_names(dest_id=dest.id),
        )

    @router.patch(
        "/notifications/destinations/{dest_id}",
        response_model=DestinationResponse,
    )
    async def update_destination_endpoint(
        dest_id: str,
        request: DestinationUpdateRequest,
        user_id: str = Depends(authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        repo = get_destinations_repo()
        try:
            dest = repo.update_destination(
                user_id=user_id,
                dest_id=dest_id,
                name=request.name,
                config=request.config,
                secret_fields=request.secret_fields,
                enabled=request.enabled,
            )
        except DestinationNotFound as exc:
            raise HTTPException(status_code=404, detail="Destination not found") from exc
        except DestinationAlreadyExists as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _destination_to_response(
            dest, repo.list_secret_field_names(dest_id=dest.id),
        )

    @router.delete("/notifications/destinations/{dest_id}")
    async def delete_destination_endpoint(
        dest_id: str,
        user_id: str = Depends(authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        repo = get_destinations_repo()
        if not repo.delete_destination(user_id=user_id, dest_id=dest_id):
            raise HTTPException(status_code=404, detail="Destination not found")
        return {"status": "ok", "destination_id": dest_id}

    @router.post(
        "/notifications/destinations/{dest_id}/test",
        response_model=DestinationTestResponse,
    )
    async def test_destination_endpoint(
        dest_id: str,
        request: DestinationTestRequest = Body(default_factory=DestinationTestRequest),
        user_id: str = Depends(authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Send a test notification to a single destination.

        Useful for verifying setup interactively (the frontend wires this to
        a "Send test" button on each destination card). Does NOT write to
        the in-app feed — the result is only returned in the response.
        """
        repo = get_destinations_repo()
        dest = repo.get_destination(user_id=user_id, dest_id=dest_id)
        if dest is None:
            raise HTTPException(status_code=404, detail="Destination not found")
        channel = get_channel_type(dest.type)
        if channel is None:
            return DestinationTestResponse(
                ok=False, detail=f"Unknown channel type '{dest.type}'",
            )
        settings = get_settings_fn()
        ctx = SendContext(
            user_id=user_id, thread_id="", settings=settings,
        )
        try:
            result = channel.send(request.message, dest, ctx, repo)
        except Exception as exc:
            logger.exception(
                "Destination test raised for %s/%s", dest.type, dest.name,
            )
            return DestinationTestResponse(ok=False, detail=str(exc))
        return DestinationTestResponse(ok=result.ok, detail=result.detail)

    # -- profiles ----------------------------------------------------------

    @router.get(
        "/notifications/profiles",
        response_model=ProfilesListResponse,
    )
    async def list_profiles_endpoint(
        user_id: str = Depends(authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        repo = get_destinations_repo()
        settings = get_settings_fn()
        try:
            ensure_seeded_destinations(
                user_id=user_id, settings=settings, repo=repo,
            )
        except Exception as exc:
            logger.debug("auto-seed skipped for %s: %s", user_id, exc)
        return ProfilesListResponse(
            profiles=[_profile_to_response(p) for p in repo.list_profiles(user_id=user_id)],
        )

    @router.post(
        "/notifications/profiles",
        response_model=ProfileResponse,
    )
    async def create_profile_endpoint(
        request: ProfileCreateRequest,
        user_id: str = Depends(authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        repo = get_destinations_repo()
        try:
            profile = repo.create_profile(
                user_id=user_id,
                name=request.name,
                destination_names=request.destination_names,
            )
        except ProfileAlreadyExists as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _profile_to_response(profile)

    @router.get(
        "/notifications/profiles/{profile_id}",
        response_model=ProfileResponse,
    )
    async def get_profile_endpoint(
        profile_id: str,
        user_id: str = Depends(authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        repo = get_destinations_repo()
        profile = repo.get_profile(user_id=user_id, profile_id=profile_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="Profile not found")
        return _profile_to_response(profile)

    @router.patch(
        "/notifications/profiles/{profile_id}",
        response_model=ProfileResponse,
    )
    async def update_profile_endpoint(
        profile_id: str,
        request: ProfileUpdateRequest,
        user_id: str = Depends(authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        repo = get_destinations_repo()
        try:
            profile = repo.update_profile(
                user_id=user_id,
                profile_id=profile_id,
                name=request.name,
                destination_names=request.destination_names,
            )
        except ProfileNotFound as exc:
            raise HTTPException(status_code=404, detail="Profile not found") from exc
        except ProfileAlreadyExists as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _profile_to_response(profile)

    @router.delete("/notifications/profiles/{profile_id}")
    async def delete_profile_endpoint(
        profile_id: str,
        user_id: str = Depends(authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        repo = get_destinations_repo()
        if not repo.delete_profile(user_id=user_id, profile_id=profile_id):
            raise HTTPException(status_code=404, detail="Profile not found")
        return {"status": "ok", "profile_id": profile_id}

    # -- preferences -------------------------------------------------------

    @router.get(
        "/notifications/preferences",
        response_model=NotificationPreferencesResponse,
    )
    async def get_preferences_endpoint(
        user_id: str = Depends(authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        agent = get_agent_fn()
        profile = agent.profile_manager.get_profile(user_id)
        prefs = profile.get_notification_preferences()
        return NotificationPreferencesResponse(**prefs)

    @router.patch(
        "/notifications/preferences",
        response_model=NotificationPreferencesResponse,
    )
    async def update_preferences_endpoint(
        request: NotificationPreferencesUpdateRequest,
        user_id: str = Depends(authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        agent = get_agent_fn()
        with agent.profile_manager.atomic_update(user_id) as profile:
            if request.default_profile is not None:
                profile.set_notification_preference(
                    "default_profile", request.default_profile,
                )
            prefs = profile.get_notification_preferences()
        return NotificationPreferencesResponse(**prefs)

    return router
