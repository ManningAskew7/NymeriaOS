"""Start-side helpers for OAuth flows triggered by ``request_credential``.

The tool calls :func:`start_oauth_flow` with the provider id, the agent's
optional ``flow`` override, and the ``use_localhost`` flag. The helper:

1. Looks up the provider descriptor.
2. Decides whether to run ``auth_code`` or ``device_code``, auto-degrading
   to ``device_code`` when ``NYMERIA_PUBLIC_URL`` is unset and the agent did
   not opt-in to ``use_localhost``.
3. Loads the client config (env vars / Google JSON file).
4. For ``auth_code``: builds the authorize URL with state + PKCE.
   For ``device_code``: POSTs the device-authorization endpoint and spawns
   the polling task.
5. Returns a structured result the tool merges into its SSE event payload
   and ``PendingPrompt.metadata``.

Errors come back as :class:`OAuthStartError` instead of exceptions so the
tool can JSON-encode them and hand them to the agent verbatim.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlencode

import httpx

from ..config.oauth_providers import (
    OAuthFlow,
    OAuthProviderDescriptor,
    get_oauth_provider,
)
from .auth_prompt_coordinator import hash_prompt_token, new_prompt_token

logger = logging.getLogger(__name__)


_DEVICE_REQUEST_TIMEOUT_SECONDS = 30.0
_DEFAULT_API_PORT = 8000


@dataclass(frozen=True)
class OAuthStartError:
    """Returned when the OAuth flow cannot start. Agent sees the message."""

    status: str  # "unknown_provider" | "unsupported_flow" | "missing_public_url" | "client_config_missing" | "device_code_request_failed"
    message: str


@dataclass
class OAuthStartResult:
    """Returned when the OAuth flow successfully started.

    ``event_extras`` is merged into the SSE ``auth_prompt`` payload (visible
    to clients). ``prompt_state`` is stashed on ``PendingPrompt.metadata
    ["_oauth_state"]`` for the callback / poller to read back. ``device_poll``
    is non-None only for ``device_code`` flows; the caller spawns the
    indicated coroutine after registering the prompt.

    For ``auth_code`` flows, ``prompt_state`` carries a hashed
    ``state_token_hash`` distinct from the hosted-form bearer token: the
    raw state appears in the user's browser URL, so we don't want it to
    double as a form-access credential.
    """

    flow: OAuthFlow
    event_extras: dict[str, Any]
    prompt_state: dict[str, Any]
    device_poll: Optional[dict[str, Any]] = field(default=None)


# ---------------------------------------------------------------------------
# Client-config loading
# ---------------------------------------------------------------------------


def _load_google_oauth_client_config(env_var: str) -> dict[str, Any]:
    path = os.environ.get(env_var)
    if not path:
        return {}
    expanded = os.path.expanduser(path)
    if not os.path.exists(expanded):
        return {}
    try:
        with open(expanded, encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception:
        logger.debug("Failed to load Google OAuth client config from %s", expanded, exc_info=True)
        return {}
    data = payload.get("installed") or payload.get("web")
    return data if isinstance(data, dict) else {}


def _resolve_client_config(
    descriptor: OAuthProviderDescriptor,
) -> dict[str, str] | OAuthStartError:
    """Return ``{client_id, client_secret, auth_uri}`` or an error.

    For Google: read the installed-app JSON pointed to by
    ``GOOGLE_OAUTH_CREDENTIALS``. For Microsoft (and other env-driven
    providers): read ``client_id_env`` / ``client_secret_env`` directly.

    Deliberately no ``token_uri``. The token endpoint decides where the grant
    proof is POSTed, so it comes from the descriptor at the point of use
    (``oauth_callback_handler``, ``oauth_device_flow``) and is never carried
    alongside the client secret. Honouring the client JSON's own ``token_uri``
    here, the way ``auth_uri`` above is honoured, was already dead code; it is
    removed rather than left as a seam a future refactor could re-wire.
    """
    if descriptor.client_config_file_env:
        creds = _load_google_oauth_client_config(descriptor.client_config_file_env)
        if not creds.get("client_id"):
            return OAuthStartError(
                status="client_config_missing",
                message=(
                    f"{descriptor.client_config_file_env} is not set or points to a "
                    "missing/invalid OAuth client JSON file. Ask the user to create a "
                    "Google Cloud OAuth client (Desktop app type), download the JSON, "
                    f"and set {descriptor.client_config_file_env}=/absolute/path/to/credentials.json."
                ),
            )
        return {
            "client_id": str(creds["client_id"]),
            "client_secret": str(creds.get("client_secret") or ""),
            "auth_uri": str(creds.get("auth_uri") or descriptor.auth_uri),
        }

    if descriptor.client_id_env:
        client_id = os.environ.get(descriptor.client_id_env) or ""
        if not client_id and descriptor.client_id_fallback:
            # Public client ID baked into the descriptor (e.g. Microsoft Graph
            # sample app). Mirrors the legacy outlook_auth.get_client_id()
            # fallback so device-code works out of the box.
            client_id = descriptor.client_id_fallback
        if not client_id:
            return OAuthStartError(
                status="client_config_missing",
                message=(
                    f"{descriptor.client_id_env} is not set. Ask the user to register "
                    f"an OAuth app with {descriptor.display_name} and set this env var "
                    "to its client ID."
                ),
            )
        client_secret = ""
        if descriptor.client_secret_env:
            client_secret = os.environ.get(descriptor.client_secret_env) or ""
        return {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": descriptor.auth_uri,
        }

    return OAuthStartError(
        status="client_config_missing",
        message=(
            f"{descriptor.display_name} has no OAuth client config wiring. "
            "This is a server-side bug; the provider descriptor is missing an "
            "env var or client config file reference."
        ),
    )


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------


def _pkce_pair() -> tuple[str, str]:
    """Returns ``(code_verifier, code_challenge_S256)``."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


# ---------------------------------------------------------------------------
# Flow resolution
# ---------------------------------------------------------------------------


def _resolve_flow(
    descriptor: OAuthProviderDescriptor,
    requested: Optional[str],
    have_redirect: bool,
) -> OAuthFlow | OAuthStartError:
    """Pick ``auth_code`` or ``device_code`` respecting overrides + capabilities."""
    requested_norm = (requested or "").strip().lower()
    if requested_norm:
        if requested_norm not in {"auth_code", "device_code"}:
            return OAuthStartError(
                status="unsupported_flow",
                message=(
                    f"Unknown flow '{requested}'. Use 'auth_code', 'device_code', "
                    "or omit to use the provider default."
                ),
            )
        if requested_norm not in descriptor.supported_flows:
            return OAuthStartError(
                status="unsupported_flow",
                message=(
                    f"{descriptor.display_name} does not support flow='{requested_norm}'. "
                    f"Supported flows: {sorted(descriptor.supported_flows)}."
                ),
            )
        return requested_norm  # type: ignore[return-value]

    default = descriptor.default_flow
    if default == "auth_code" and not have_redirect and "device_code" in descriptor.supported_flows:
        return "device_code"
    return default


# ---------------------------------------------------------------------------
# Redirect URI
# ---------------------------------------------------------------------------


_OAUTH_CALLBACK_PATH = "/connect/credentials/oauth/callback"


def _build_redirect_uri(
    public_url: Optional[str],
    api_port: int,
    use_localhost: bool,
) -> Optional[str]:
    """Return the static OAuth redirect URI for this Nymeria install.

    The path is the same for every prompt so the user only has to register
    one redirect URI per origin in Google Cloud Console / Azure Portal. The
    per-prompt identity travels in the OAuth ``state`` parameter instead;
    see :func:`_pack_state` and :func:`unpack_state`.
    """
    if public_url:
        base = public_url.strip().rstrip("/")
        if base:
            return f"{base}{_OAUTH_CALLBACK_PATH}"
    if use_localhost:
        return f"http://localhost:{int(api_port or _DEFAULT_API_PORT)}{_OAUTH_CALLBACK_PATH}"
    return None


_STATE_SEPARATOR = ":"


def _pack_state(prompt_id: str, nonce: str) -> str:
    """Encode ``prompt_id`` and a verification nonce as a single ``state``.

    ``prompt_id`` is a UUID, so a colon is a safe separator. The callback
    parser uses :func:`unpack_state` to reverse this.
    """
    return f"{prompt_id}{_STATE_SEPARATOR}{nonce}"


def unpack_state(state: str) -> Optional[tuple[str, str]]:
    """Inverse of :func:`_pack_state`. Returns ``None`` on malformed input."""
    if not state:
        return None
    parts = state.split(_STATE_SEPARATOR, 1)
    if len(parts) != 2:
        return None
    prompt_id, nonce = parts[0].strip(), parts[1].strip()
    if not prompt_id or not nonce:
        return None
    return prompt_id, nonce


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------


async def start_oauth_flow(
    *,
    provider_id: str,
    flow_override: Optional[str],
    use_localhost: bool,
    prompt_id: str,
    public_url: Optional[str],
    api_port: int,
) -> OAuthStartResult | OAuthStartError:
    """Run the start-side logic for the OAuth branch of ``request_credential``.

    The caller publishes the SSE event and stashes ``prompt_state`` on the
    coordinator's ``PendingPrompt.metadata['_oauth_state']``. For
    ``device_code`` the caller spawns ``device_poll['poll_coroutine']`` as an
    asyncio task; the poller resolves the coordinator future on completion.
    """
    descriptor = get_oauth_provider(provider_id)
    if descriptor is None:
        return OAuthStartError(
            status="unknown_provider",
            message=(
                f"Provider '{provider_id}' is not in the OAuth registry. "
                "Use kind='api_key' for an API-key-style credential, or pick one of: "
                f"{sorted(_known_providers())}."
            ),
        )

    client_config = _resolve_client_config(descriptor)
    if isinstance(client_config, OAuthStartError):
        return client_config

    redirect_uri = _build_redirect_uri(public_url, api_port, use_localhost)
    flow_or_err = _resolve_flow(
        descriptor,
        flow_override,
        have_redirect=redirect_uri is not None,
    )
    if isinstance(flow_or_err, OAuthStartError):
        return flow_or_err
    flow: OAuthFlow = flow_or_err

    if flow == "auth_code" and redirect_uri is None:
        return OAuthStartError(
            status="missing_public_url",
            message=(
                "NYMERIA_PUBLIC_URL is not configured and this provider does not "
                "support device_code. Ask the user whether to set it: an admin "
                "can run `/env set NYMERIA_PUBLIC_URL <public https url>`, which "
                "applies immediately with no restart and works for bots and "
                "remote browsers. Or retry this tool call with "
                "use_localhost=True (only works if the user's browser is on the "
                "same machine as Nymeria)."
            ),
        )

    if flow == "auth_code":
        return _build_auth_code_result(
            descriptor=descriptor,
            client_config=client_config,
            redirect_uri=redirect_uri or "",
            prompt_id=prompt_id,
        )
    return await _build_device_code_result(
        descriptor=descriptor,
        client_config=client_config,
        prompt_id=prompt_id,
    )


def _known_providers() -> list[str]:
    from ..config.oauth_providers import OAUTH_PROVIDERS

    return list(OAUTH_PROVIDERS.keys())


def _build_auth_code_result(
    *,
    descriptor: OAuthProviderDescriptor,
    client_config: dict[str, str],
    redirect_uri: str,
    prompt_id: str,
) -> OAuthStartResult:
    code_verifier: Optional[str] = None
    code_challenge: Optional[str] = None
    if descriptor.uses_pkce:
        code_verifier, code_challenge = _pkce_pair()

    # State token is distinct from the hosted-form bearer token. The raw
    # state ends up in the user's browser URL during the redirect, so it
    # must not double as a credential the callback could use to access
    # other endpoints. The packed state also carries the prompt_id so the
    # static-path callback knows which prompt this redirect belongs to.
    nonce = new_prompt_token()
    state_hash = hash_prompt_token(nonce)
    state_token = _pack_state(prompt_id, nonce)

    params: dict[str, str] = {
        "client_id": client_config["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(descriptor.scopes),
        "state": state_token,
    }
    if code_challenge:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
    for k, v in descriptor.extra_authorize_params.items():
        params.setdefault(k, v)

    auth_url = f"{client_config['auth_uri']}?{urlencode(params)}"

    event_extras = {
        "mode": "oauth",
        "flow": "auth_code",
        "provider_id": descriptor.provider_id,
        "display_name": descriptor.display_name,
        "auth_url": auth_url,
        "scopes": list(descriptor.scopes),
        "notes": descriptor.notes,
        "uses_pkce": descriptor.uses_pkce,
        "fields": [],
        "connect_url": None,
        "connect_url_required": False,
        "connect_url_error": None,
    }
    prompt_state = {
        "provider_id": descriptor.provider_id,
        "flow": "auth_code",
        "redirect_uri": redirect_uri,
        "client_id": client_config["client_id"],
        "client_secret": client_config.get("client_secret", ""),
        "code_verifier": code_verifier,
        "scopes": list(descriptor.scopes),
        "state_token_hash": state_hash,
    }
    return OAuthStartResult(
        flow="auth_code",
        event_extras=event_extras,
        prompt_state=prompt_state,
    )


async def _build_device_code_result(
    *,
    descriptor: OAuthProviderDescriptor,
    client_config: dict[str, str],
    prompt_id: str,
) -> OAuthStartResult | OAuthStartError:
    if not descriptor.device_authorization_uri:
        return OAuthStartError(
            status="unsupported_flow",
            message=(
                f"{descriptor.display_name} descriptor does not declare a "
                "device_authorization_uri. This is a server-side bug."
            ),
        )

    data = {
        "client_id": client_config["client_id"],
        "scope": " ".join(descriptor.scopes),
    }
    try:
        async with httpx.AsyncClient(timeout=_DEVICE_REQUEST_TIMEOUT_SECONDS) as client:
            response = await client.post(descriptor.device_authorization_uri, data=data)
    except httpx.HTTPError as exc:
        return OAuthStartError(
            status="device_code_request_failed",
            message=(
                f"Could not reach {descriptor.display_name} device authorization "
                f"endpoint: {exc}"
            ),
        )

    if response.status_code != 200:
        body_text = response.text[:300] if response.text else ""
        return OAuthStartError(
            status="device_code_request_failed",
            message=(
                f"{descriptor.display_name} device authorization rejected the "
                f"request (HTTP {response.status_code}): {body_text}"
            ),
        )

    try:
        payload = response.json()
    except Exception:
        return OAuthStartError(
            status="device_code_request_failed",
            message=(
                f"{descriptor.display_name} device authorization returned non-JSON."
            ),
        )

    device_code = str(payload.get("device_code") or "")
    user_code = str(payload.get("user_code") or "")
    verification_uri = str(payload.get("verification_uri") or "")
    verification_uri_complete = str(payload.get("verification_uri_complete") or "")
    expires_in = int(payload.get("expires_in") or 900)
    interval = int(payload.get("interval") or 5)

    if not device_code or not user_code or not verification_uri:
        return OAuthStartError(
            status="device_code_request_failed",
            message=f"{descriptor.display_name} device authorization response was incomplete.",
        )

    event_extras = {
        "mode": "oauth_device",
        "flow": "device_code",
        "provider_id": descriptor.provider_id,
        "display_name": descriptor.display_name,
        "user_code": user_code,
        "verification_uri": verification_uri,
        "verification_uri_complete": verification_uri_complete or None,
        "expires_in": expires_in,
        "interval": interval,
        "scopes": list(descriptor.scopes),
        "notes": descriptor.notes,
        "fields": [],
        "connect_url": None,
        "connect_url_required": False,
        "connect_url_error": None,
    }
    prompt_state = {
        "provider_id": descriptor.provider_id,
        "flow": "device_code",
        "client_id": client_config["client_id"],
        "client_secret": client_config.get("client_secret", ""),
        "device_code": device_code,
        "interval": interval,
        "expires_in": expires_in,
        "scopes": list(descriptor.scopes),
    }
    device_poll = {
        "prompt_id": prompt_id,
        "descriptor": descriptor,
        "client_id": client_config["client_id"],
        "client_secret": client_config.get("client_secret", ""),
        "device_code": device_code,
        "interval": interval,
        "expires_in": expires_in,
    }
    return OAuthStartResult(
        flow="device_code",
        event_extras=event_extras,
        prompt_state=prompt_state,
        device_poll=device_poll,
    )


__all__ = [
    "OAuthStartError",
    "OAuthStartResult",
    "start_oauth_flow",
    "unpack_state",
]
