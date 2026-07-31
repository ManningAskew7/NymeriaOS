"""Shared finalize logic for OAuth flows.

Both the auth-code callback endpoint (``GET /connect/credentials/oauth/callback``)
and the device-code poller end here. Given a fresh token bundle from the
provider, this module:

1. Fetches userinfo (email, name) to derive ``account_id`` and label the
   credential.
2. Upserts the pending vault placeholder into an ``active`` ``oauth_token``
   credential, writing ``access_token``/``refresh_token`` as encrypted secret
   fields and stashing ``scopes``/``email``/``expires_at`` in metadata.
3. Fires the descriptor's optional ``post_save_hook`` (e.g. gmail MCP
   credential export).
4. Resolves the coordinator future so the prompt done-callback can run.
5. Publishes ``auth_prompt_resolved`` so clients can close the active prompt.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import hmac

import httpx

from ..config.oauth_providers import OAuthProviderDescriptor, get_oauth_provider
from . import secrets as nymeria_secrets
from .auth_prompt_coordinator import (
    AuthPromptCoordinator,
    PendingPrompt,
    get_auth_prompt_coordinator,
    hash_prompt_token,
)
from .credential_vault import (
    CredentialVaultRepo,
    get_credential_vault_repo,
)
from .event_bus import publish_autonomous_event

logger = logging.getLogger(__name__)


_USERINFO_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class OAuthFinalizeResult:
    ok: bool
    status: str  # "active" | "user_exited" | "denied" | "expired" | "error"
    credential_id: Optional[str]
    email: Optional[str]
    name: Optional[str]
    scopes: list[str]
    message: str
    hook_message: Optional[str] = None


def _fetch_userinfo(
    descriptor: OAuthProviderDescriptor,
    access_token: str,
) -> tuple[str, str, Optional[str]]:
    """Fetch ``(email, display_name, userinfo_sub)``. Falls back to placeholders.

    Microsoft Graph's ``/me`` returns ``mail``/``userPrincipalName``/``displayName``.
    Google's ``oauth2/v2/userinfo`` returns ``email``/``name``/``id``.
    """
    try:
        response = httpx.get(
            descriptor.userinfo_uri,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=_USERINFO_TIMEOUT_SECONDS,
        )
        if response.status_code != 200:
            logger.debug(
                "userinfo fetch returned %s for %s",
                response.status_code,
                descriptor.provider_id,
            )
            return "unknown", "Unknown User", None
        payload = response.json()
    except Exception:
        logger.debug("userinfo fetch failed for %s", descriptor.provider_id, exc_info=True)
        return "unknown", "Unknown User", None

    if "userPrincipalName" in payload or "displayName" in payload:
        email = payload.get("mail") or payload.get("userPrincipalName") or "unknown"
        name = payload.get("displayName") or "Unknown User"
        sub = payload.get("id")
    else:
        email = payload.get("email") or "unknown"
        name = payload.get("name") or "Unknown User"
        sub = payload.get("id") or payload.get("sub")
    return email, name, sub


def _account_id(descriptor: OAuthProviderDescriptor, email: str, sub: Optional[str]) -> str:
    if descriptor.account_id_strategy == "userinfo_sub" and sub:
        return str(sub)
    safe = (email or "unknown").lower().replace("@", "_at_").replace(".", "_")
    return safe or "unknown"


def _now_plus_seconds_iso(seconds: int) -> tuple[float, str]:
    seconds = max(0, int(seconds or 0))
    wall_seconds = time.time() + seconds
    iso = datetime.fromtimestamp(wall_seconds, tz=timezone.utc).isoformat(timespec="seconds")
    return wall_seconds, iso


def _matching_oauth_credentials(
    *,
    repo: CredentialVaultRepo,
    current: Any,
    account_id: str,
) -> list[Any]:
    """Find active/pending same-account OAuth rows that this reconnect replaces."""
    owner_user_id = getattr(current, "owner_user_id", None)
    if not owner_user_id:
        return []
    try:
        records = repo.list_credentials(owner_user_id=owner_user_id, include_disabled=False)
    except Exception:
        logger.debug("Failed to list credentials while checking OAuth duplicates", exc_info=True)
        return []
    matches: list[Any] = []
    for record in records:
        if record.id == current.id:
            continue
        metadata = record.metadata or {}
        if record.kind == "legacy_token_cache":
            provider_matches = record.provider == current.provider or (
                current.provider == "outlook" and record.provider == "microsoft"
            )
            legacy_account_ids = {
                str(account.get("account_id"))
                for account in metadata.get("accounts") or []
                if isinstance(account, dict) and account.get("account_id")
            }
            if provider_matches and account_id in legacy_account_ids:
                matches.append(record)
            continue
        if record.provider != current.provider or record.kind != "oauth_token":
            continue
        record_account_id = str(metadata.get("account_id") or "")
        if record_account_id == account_id:
            matches.append(record)
            continue
        if record.status == "pending_setup" and metadata.get("oauth_pending") is True:
            matches.append(record)
    return matches


def _merged_allowed_targets(current: Any, replacements: list[Any]) -> list[str]:
    """Union the reach of every row folded into the survivor.

    This is also why ``_copy_replacement_bindings`` does not need to widen: a
    replaced row's grants arrive here, so re-pointing its bindings cannot leave
    the survivor bound to a target it may not read.

    Returns the empty union as-is rather than falling back to a default. An
    empty list is now a deliberate lockout, so a truthy fallback would silently
    re-grant, on the next token refresh, a row an operator had locked.
    """
    merged: list[str] = []
    for record in [current, *replacements]:
        for target in record.allowed_targets or []:
            if target not in merged:
                merged.append(target)
    return merged


def _copy_replacement_bindings(
    *,
    repo: CredentialVaultRepo,
    replacements: list[Any],
    target_credential_id: str,
    actor_user_id: Optional[str],
) -> None:
    for replacement in replacements:
        try:
            bindings = repo.list_bindings(replacement.id)
        except Exception:
            logger.debug("Failed to list bindings for replaced credential %s", replacement.id, exc_info=True)
            continue
        for binding in bindings:
            try:
                repo.bind_credential(
                    target_credential_id,
                    target_type=str(binding.get("target_type") or ""),
                    target_id=str(binding.get("target_id") or ""),
                    binding_name=binding.get("binding_name"),
                    actor_user_id=actor_user_id,
                )
            except Exception:
                logger.debug(
                    "Failed to copy binding %s from %s to %s",
                    binding.get("id"),
                    replacement.id,
                    target_credential_id,
                    exc_info=True,
                )


def _disable_replaced_credentials(
    *,
    repo: CredentialVaultRepo,
    replacements: list[Any],
    actor_user_id: Optional[str],
) -> None:
    for replacement in replacements:
        try:
            repo.disable_credential(replacement.id, actor_user_id=actor_user_id)
        except Exception:
            logger.debug("Failed to disable replaced OAuth credential %s", replacement.id, exc_info=True)


def _fail(
    coordinator: AuthPromptCoordinator,
    prompt: PendingPrompt,
    message: str,
    *,
    credential_id: Optional[str],
) -> OAuthFinalizeResult:
    """Resolve the pending prompt with an error payload and return the matching
    failure result.

    Shared by the finalize failure branches so the coordinator resolve dict and
    the returned ``OAuthFinalizeResult`` cannot drift. Callers that need to log
    do so before calling this. ``coordinator.resolve`` already merges
    ``attempts``/``last_test_error`` into the payload, so this must not.
    """
    coordinator.resolve(
        prompt.prompt_id,
        {
            "ok": False,
            "status": "error",
            "message": message,
            "credential_id": credential_id,
        },
    )
    return OAuthFinalizeResult(
        ok=False,
        status="error",
        credential_id=credential_id,
        email=None,
        name=None,
        scopes=[],
        message=message,
    )


def finalize_oauth_credential(
    *,
    prompt: PendingPrompt,
    descriptor: OAuthProviderDescriptor,
    token_data: dict[str, Any],
    source: str = "oauth_callback",
    repo: Optional[CredentialVaultRepo] = None,
) -> OAuthFinalizeResult:
    """Promote the pending credential to ``active`` and resolve the future.

    ``token_data`` is the JSON body from a successful token-exchange (auth-code)
    or device-code poll. Required keys: ``access_token``. Optional:
    ``refresh_token``, ``expires_in``, ``scope`` (space-separated).

    ``source`` is stamped on ``metadata["source"]`` so operators can tell
    whether the token landed via the browser callback or the device-code
    poller. Use ``"oauth_device_flow"`` for the latter.
    """
    coordinator = get_auth_prompt_coordinator()
    if repo is None:
        repo = get_credential_vault_repo()

    access_token = str(token_data.get("access_token") or "")
    if not access_token:
        return _fail(
            coordinator,
            prompt,
            "Provider returned no access_token.",
            credential_id=prompt.credential_id,
        )

    refresh_token = token_data.get("refresh_token") or ""
    expires_in = int(token_data.get("expires_in") or 3600)
    scope_field = token_data.get("scope")
    if isinstance(scope_field, str) and scope_field.strip():
        granted_scopes = scope_field.split()
    else:
        granted_scopes = list(descriptor.scopes)

    email, name, sub = _fetch_userinfo(descriptor, access_token)
    account_id = _account_id(descriptor, email, sub)
    _expires_wall, expires_at_iso = _now_plus_seconds_iso(expires_in)

    current = repo.get_credential(prompt.credential_id)
    if current is None:
        return _fail(
            coordinator,
            prompt,
            "Pending credential record vanished before completion.",
            credential_id=None,
        )

    replacements = _matching_oauth_credentials(
        repo=repo,
        current=current,
        account_id=account_id,
    )
    replacement_ids = [record.id for record in replacements]

    metadata = dict(current.metadata or {})
    oauth_state = metadata.get("_oauth_state") or {}
    client_id_from_state = oauth_state.get("client_id") if isinstance(oauth_state, dict) else None
    metadata.update(
        {
            "provider_id": descriptor.provider_id,
            "account_id": account_id,
            "email": email,
            "name": name,
            "scopes": granted_scopes,
            "expires_at": expires_at_iso,
            # Informational only. Nothing reads this back: metadata is
            # caller-writable over `POST`/`PATCH /credentials`, so every refresh
            # and exchange path resolves the endpoint from the registry instead
            # (`auth_cache_utils._resolve_provider_token_uri`). Kept because the
            # documented record shape includes it and readers may display it.
            "token_uri": descriptor.token_uri,
            "source": source,
            "userinfo_sub": sub,
        }
    )
    if replacement_ids:
        metadata["replaced_credential_ids"] = replacement_ids
    if client_id_from_state and not metadata.get("client_id"):
        # Stash the client_id at the top level so refresh paths in
        # auth_cache_utils can find it without re-reading _oauth_state
        # (which gets popped below).
        metadata["client_id"] = str(client_id_from_state)
    metadata.pop("oauth_pending", None)
    metadata.pop("oauth_state", None)
    metadata.pop("_oauth_state", None)

    secret_fields = {"access_token": access_token}
    if refresh_token:
        secret_fields["refresh_token"] = str(refresh_token)

    try:
        updated = repo.upsert_credential(
            credential_id=current.id,
            owner_type=current.owner_type,
            owner_user_id=current.owner_user_id,
            name=name if name and name != "Unknown User" else current.name,
            provider=current.provider,
            kind="oauth_token",
            account_label=email if email and email != "unknown" else current.account_label,
            metadata=metadata,
            scopes=granted_scopes,
            allowed_targets=_merged_allowed_targets(current, replacements),
            expires_at=expires_at_iso,
            status="active",
            secret_fields=secret_fields,
            actor_user_id=current.owner_user_id,
        )
    except (nymeria_secrets.SecretsKeyMissing, nymeria_secrets.SecretsKeyInvalid) as exc:
        message = (
            f"Cannot encrypt OAuth tokens: {exc}. Ask the user to configure "
            "NYMERIA_SECRETS_KEY before retrying."
        )
        logger.error("OAuth finalize failed due to vault secrets-key error: %s", exc)
        return _fail(coordinator, prompt, message, credential_id=prompt.credential_id)
    except Exception as exc:  # noqa: BLE001 - safety net so the agent never hangs.
        logger.exception("OAuth finalize crashed during upsert_credential")
        return _fail(
            coordinator,
            prompt,
            f"Failed to store OAuth credential: {exc}",
            credential_id=prompt.credential_id,
        )

    if replacements:
        _copy_replacement_bindings(
            repo=repo,
            replacements=replacements,
            target_credential_id=updated.id,
            actor_user_id=current.owner_user_id,
        )
        _disable_replaced_credentials(
            repo=repo,
            replacements=replacements,
            actor_user_id=current.owner_user_id,
        )

    hook_message: Optional[str] = None
    if descriptor.post_save_hook is not None and current.owner_user_id:
        try:
            hook_message = descriptor.post_save_hook(current.owner_user_id, account_id)
        except Exception:
            logger.warning(
                "OAuth post_save_hook failed for provider=%s account=%s",
                descriptor.provider_id,
                account_id,
                exc_info=True,
            )
            hook_message = (
                f"Connected, but provider-specific post-save step failed for {descriptor.display_name}."
            )

    connected_msg = (
        f"Connected {descriptor.display_name} as {email}."
        if email and email != "unknown"
        else f"Connected {descriptor.display_name}."
    )
    coordinator.resolve(
        prompt.prompt_id,
        {
            "ok": True,
            "status": "active",
            "credential_id": updated.id,
            "email": email,
            "name": name,
            "scopes": granted_scopes,
            "account_label": updated.account_label,
            "message": connected_msg,
            "hook_message": hook_message,
        },
    )
    publish_autonomous_event(
        event_type="auth_prompt_resolved",
        thread_id=prompt.thread_id,
        user_id=prompt.user_id,
        task_id="",
        data={
            "prompt_id": prompt.prompt_id,
            "credential_id": updated.id,
            "status": "active",
            "message": connected_msg,
            "email": email,
            "name": name,
        },
    )

    return OAuthFinalizeResult(
        ok=True,
        status="active",
        credential_id=updated.id,
        email=email,
        name=name,
        scopes=granted_scopes,
        message=connected_msg,
        hook_message=hook_message,
    )


def fail_oauth_prompt(
    *,
    prompt: PendingPrompt,
    status: str,
    message: str,
    last_error: Optional[str] = None,
) -> None:
    """Resolve a pending OAuth prompt with a failure result."""
    coordinator = get_auth_prompt_coordinator()
    coordinator.resolve(
        prompt.prompt_id,
        {
            "ok": False,
            "status": status,
            "credential_id": prompt.credential_id,
            "message": message,
            "last_test_error": last_error,
        },
    )
    publish_autonomous_event(
        event_type="auth_prompt_cancelled",
        thread_id=prompt.thread_id,
        user_id=prompt.user_id,
        task_id="",
        data={"prompt_id": prompt.prompt_id, "reason": status, "message": message},
    )


_TOKEN_EXCHANGE_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class AuthCodeCallbackResult:
    """Returned by :func:`handle_auth_code_callback`. ``title`` and ``message``
    feed the success/failure HTML page the user sees in the browser."""

    ok: bool
    title: str
    message: str
    status_code: int


async def handle_auth_code_callback(
    *,
    code: Optional[str],
    state: Optional[str],
    error: Optional[str] = None,
) -> AuthCodeCallbackResult:
    """Resolve a pending OAuth ``auth_code`` prompt from a provider redirect.

    The static callback path does not carry the prompt id; both the
    ``prompt_id`` and the verification nonce are packed into ``state``
    by :func:`oauth_start._pack_state` and unpacked here. The nonce is
    then compared against the hash stored on the prompt at start time.
    """
    from .oauth_start import unpack_state

    coordinator = get_auth_prompt_coordinator()

    if not state:
        return AuthCodeCallbackResult(
            ok=False,
            title="Authentication Failed",
            message="Missing state parameter. Start the flow again from Nymeria.",
            status_code=400,
        )

    unpacked = unpack_state(state)
    if unpacked is None:
        return AuthCodeCallbackResult(
            ok=False,
            title="Authentication Failed",
            message="State parameter was malformed. Start the flow again from Nymeria.",
            status_code=400,
        )
    prompt_id, nonce = unpacked

    prompt = coordinator.get(prompt_id)
    if prompt is None:
        return AuthCodeCallbackResult(
            ok=False,
            title="Authentication Failed",
            message="This sign-in link has expired or already been used. Start the flow again from Nymeria.",
            status_code=400,
        )

    # Verify the OAuth ``state`` against the dedicated state-hash stashed at
    # start time. Distinct from the hosted-form bearer token because the raw
    # state ends up in the user's browser URL during the provider redirect.
    expected_hash = ((prompt.metadata or {}).get("_oauth_state") or {}).get("state_token_hash")
    if not expected_hash or not hmac.compare_digest(hash_prompt_token(nonce), expected_hash):
        return AuthCodeCallbackResult(
            ok=False,
            title="Authentication Failed",
            message="State parameter did not match. Start the flow again from Nymeria.",
            status_code=400,
        )

    if error:
        fail_oauth_prompt(
            prompt=prompt,
            status="denied",
            message=f"Provider returned: {error}",
            last_error=error,
        )
        return AuthCodeCallbackResult(
            ok=False,
            title="Authentication Denied",
            message=f"Provider returned: {error}",
            status_code=400,
        )

    if not code:
        fail_oauth_prompt(
            prompt=prompt,
            status="error",
            message="No authorization code in callback URL.",
        )
        return AuthCodeCallbackResult(
            ok=False,
            title="Authentication Failed",
            message="No authorization code was returned. Start the flow again from Nymeria.",
            status_code=400,
        )

    oauth_state = (prompt.metadata or {}).get("_oauth_state") or {}
    descriptor = get_oauth_provider(oauth_state.get("provider_id") or prompt.provider)
    if descriptor is None:
        fail_oauth_prompt(
            prompt=prompt,
            status="error",
            message="Provider descriptor not found at callback time.",
        )
        return AuthCodeCallbackResult(
            ok=False,
            title="Authentication Failed",
            message="Provider descriptor not found. This is a bug.",
            status_code=500,
        )

    client_id = oauth_state.get("client_id") or ""
    client_secret = oauth_state.get("client_secret") or ""
    redirect_uri = oauth_state.get("redirect_uri") or ""
    code_verifier = oauth_state.get("code_verifier")
    if not client_id or not redirect_uri:
        fail_oauth_prompt(
            prompt=prompt,
            status="error",
            message="OAuth state was incomplete at callback time.",
        )
        return AuthCodeCallbackResult(
            ok=False,
            title="Authentication Failed",
            message="Internal OAuth state is incomplete. Start again from Nymeria.",
            status_code=500,
        )

    data = {
        "code": code,
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    if client_secret:
        data["client_secret"] = client_secret
    if code_verifier:
        data["code_verifier"] = code_verifier

    try:
        async with httpx.AsyncClient(timeout=_TOKEN_EXCHANGE_TIMEOUT_SECONDS) as client:
            response = await client.post(descriptor.token_uri, data=data)
    except httpx.HTTPError as exc:
        fail_oauth_prompt(
            prompt=prompt,
            status="error",
            message=f"Token exchange failed: {exc}",
            last_error=str(exc),
        )
        return AuthCodeCallbackResult(
            ok=False,
            title="Authentication Failed",
            message=f"Could not reach {descriptor.display_name} to exchange the code.",
            status_code=502,
        )

    if response.status_code != 200:
        body_text = response.text[:300] if response.text else ""
        fail_oauth_prompt(
            prompt=prompt,
            status="error",
            message=f"Token exchange returned HTTP {response.status_code}",
            last_error=body_text,
        )
        return AuthCodeCallbackResult(
            ok=False,
            title="Authentication Failed",
            message=(
                f"{descriptor.display_name} rejected the token exchange "
                f"(HTTP {response.status_code})."
            ),
            status_code=400,
        )

    try:
        token_data = response.json()
    except Exception:
        fail_oauth_prompt(
            prompt=prompt,
            status="error",
            message="Token endpoint returned non-JSON response.",
        )
        return AuthCodeCallbackResult(
            ok=False,
            title="Authentication Failed",
            message=f"{descriptor.display_name} returned an unexpected response.",
            status_code=502,
        )

    # Run the (synchronous, blocking) finalize off the event loop: it does a
    # blocking userinfo HTTP fetch plus synchronous SQLite writes, and a slow
    # provider would otherwise stall every request on this worker. Its side
    # effects are thread-safe (coordinator.resolve wakes the future via
    # call_soon_threadsafe; the event bus uses a thread-safe queue).
    result = await asyncio.to_thread(
        finalize_oauth_credential,
        prompt=prompt,
        descriptor=descriptor,
        token_data=token_data,
    )
    if not result.ok:
        return AuthCodeCallbackResult(
            ok=False,
            title="Authentication Failed",
            message=result.message,
            status_code=500,
        )

    return AuthCodeCallbackResult(
        ok=True,
        title="Authentication Successful",
        message=(
            f"You can close this tab and return to Nymeria. "
            f"Connected as {result.email}."
            if result.email and result.email != "unknown"
            else "You can close this tab and return to Nymeria."
        ),
        status_code=200,
    )


__all__ = [
    "AuthCodeCallbackResult",
    "OAuthFinalizeResult",
    "finalize_oauth_credential",
    "fail_oauth_prompt",
    "handle_auth_code_callback",
]
