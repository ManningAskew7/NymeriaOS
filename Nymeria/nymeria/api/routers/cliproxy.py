"""Admin-only routes for driving a CLIProxy sidecar over its management API.

Everything here is opt-in: the routes only act when
`cliproxy_management_url` / `cliproxy_management_key` are configured, and
`GET /cliproxy/status` degrades to `{configured/reachable: false}` instead of
erroring so frontends can render an empty state. Route-shape math (root vs
/v1, which key setting receives the gatekeeper) lives in
`nymeria.cliproxy.catalog`, and `/cliproxy/apply-route` is the single place
that turns a catalog entry into Nymeria LLM settings, so frontends never
re-derive it.
"""

import logging
import time
from collections.abc import Callable
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException

from ...cliproxy.catalog import (
    CLIProxyProviderSpec,
    cliproxy_data_plane_url,
    get_cliproxy_provider,
    list_cliproxy_providers,
)
from ...cliproxy.management_client import (
    CONFIG_KNOB_PATHS,
    CLIProxyConflict,
    CLIProxyManagementClient,
    CLIProxyManagementError,
    CLIProxyNotFound,
    CLIProxyUnsupported,
    configured_gatekeeper_keys,
    confirm_login_landed,
    resolve_or_mint_gatekeeper,
)
from ...core.thread_config import ThreadConfig, ThreadLLMConfig
from ..schemas.cliproxy import (
    CLIProxyApplyRouteRequest,
    CLIProxyApplyRouteResponse,
    CLIProxyAuthFilePatchRequest,
    CLIProxyConfigPatchRequest,
    CLIProxyOAuthCallbackRequest,
    CLIProxyOAuthStartRequest,
    CLIProxyOAuthStartResponse,
    CLIProxyOAuthStatusResponse,
    CLIProxyProviderInfo,
    CLIProxyStatusResponse,
)
from ..schemas.settings import ServerSettingsUpdate

logger = logging.getLogger(__name__)

# A status poll from a frontend would otherwise re-probe every provider's
# auth-url (each probe registers a short-lived pending session server-side),
# so probe results are cached per (url, secret) for this long.
_PROBE_CACHE_TTL_SECONDS = 900.0


def _provider_info(
    spec: CLIProxyProviderSpec,
    *,
    supported: Optional[bool] = None,
    logged_in: Optional[bool] = None,
) -> CLIProxyProviderInfo:
    return CLIProxyProviderInfo(
        id=spec.id,
        label=spec.label,
        description=spec.description,
        flow=spec.flow,
        nymeria_provider=spec.nymeria_provider,
        url_shape=spec.url_shape,
        api_mode=spec.api_mode,
        key_env_var=spec.key_env_var,
        default_model=spec.default_model,
        tos_warning=spec.tos_warning,
        auth_file_provider=spec.auth_file_provider,
        supported=supported,
        logged_in=logged_in,
    )


def _require_spec(provider: str) -> CLIProxyProviderSpec:
    spec = get_cliproxy_provider(provider)
    if spec is None:
        raise HTTPException(
            status_code=404, detail=f"Unknown CLIProxy provider: {provider}"
        )
    return spec


def _apply_route_thread(
    *,
    agent: Any,
    admin: Any,
    spec: CLIProxyProviderSpec,
    thread_id: Optional[str],
    model: str,
    base_url: str,
    gatekeeper: str,
    require_thread_access_fn: Optional[Callable[[Any, str], Any]],
) -> CLIProxyApplyRouteResponse:
    """Apply a resolved route to one thread's saved LLM config (scope thread)."""
    if not thread_id:
        raise HTTPException(
            status_code=400,
            detail="thread_id is required when scope is 'thread'",
        )
    if require_thread_access_fn is not None:
        # Same gate as PATCH /threads/{id}/config: guards shared-channel
        # ids and claims a fresh personal thread for the caller so it
        # cannot be TOFU-claimed by a later user.
        require_thread_access_fn(admin, thread_id)
    manager = agent.thread_config_manager
    config = manager.get_config(thread_id) or ThreadConfig(thread_id=thread_id)
    config.active_llm_fallback = None
    if config.llm_config is None:
        config.llm_config = ThreadLLMConfig()
    llm = config.llm_config
    llm.provider = spec.nymeria_provider
    llm.model = model
    llm.base_url = base_url
    llm.api_key = gatekeeper
    llm.openai_api_mode = spec.api_mode or None
    manager.save_config(config)
    # Evict the cached per-thread graph so the route applies to the
    # next turn (mirrors PATCH /threads/{id}/config).
    invalidate = getattr(agent, "invalidate_thread_config_cache", None)
    if callable(invalidate):
        invalidate(thread_id)
    return CLIProxyApplyRouteResponse(
        scope="thread",
        provider=spec.nymeria_provider,
        model=model,
        base_url=base_url,
        api_mode=spec.api_mode,
        thread_id=thread_id,
    )


def _apply_route_global(
    *,
    agent: Any,
    settings: Any,
    spec: CLIProxyProviderSpec,
    model: str,
    base_url: str,
    gatekeeper: str,
    get_settings_fn: Callable[[], Any],
) -> CLIProxyApplyRouteResponse:
    """Apply a resolved route to the global server LLM settings (scope global)."""
    # Function-local to avoid a settings <-> cliproxy import cycle AND to keep
    # the call-time attribute-resolution seam the global apply-route tests patch
    # (`mock.patch.object(settings_router_module, "apply_server_settings_update")`).
    from .settings import apply_server_settings_update

    updates = ServerSettingsUpdate(
        llm_provider=spec.nymeria_provider,
        llm_model=model,
        llm_base_url=base_url,
    )
    # key_setting is a ServerSettingsUpdate field name by catalog invariant.
    setattr(updates, spec.key_setting, gatekeeper)
    if spec.api_mode:
        updates.openai_api_mode = spec.api_mode
    result = apply_server_settings_update(
        updates,
        settings=settings,
        agent=agent,
        get_settings_fn=get_settings_fn,
    )
    return CLIProxyApplyRouteResponse(
        scope="global",
        provider=spec.nymeria_provider,
        model=model,
        base_url=base_url,
        api_mode=spec.api_mode,
        restart_required=bool(result.get("restart_required")),
    )


def management_error_status(error: CLIProxyManagementError) -> int:
    """HTTP status for a management-client error (shared with the facade).

    Unreachable and auth failures are both upstream problems: 502 so
    frontends distinguish them from their own bad requests.
    """
    if isinstance(error, CLIProxyUnsupported):
        return 422
    if isinstance(error, CLIProxyNotFound):
        return 404
    if isinstance(error, CLIProxyConflict):
        return 409
    return 502


def management_client_from_settings(
    settings: Any,
) -> Optional[CLIProxyManagementClient]:
    """Build a management client from settings, or None when unconfigured.

    Module-scope (the `_available_models` precedent): the single client
    factory behind the REST routes AND the in-process command facade, so the
    two TurnExecutor shapes cannot drift. Uses this module's
    `CLIProxyManagementClient` symbol on purpose: tests stub the client by
    monkeypatching it here.
    """
    url = (getattr(settings, "cliproxy_management_url", None) or "").strip()
    key = (getattr(settings, "cliproxy_management_key", None) or "").strip()
    if not url or not key:
        return None
    return CLIProxyManagementClient(url, key)


async def _fixup_claude_auth_files(client: CLIProxyManagementClient) -> None:
    """Keep tool_prefix_disabled true on Claude auth files (idempotent).

    Inert on v7 binaries but required if the proxy is ever rolled back to
    v6.9.36. Best-effort: a failure here must not fail the login.
    """
    try:
        for entry in await client.list_auth_files():
            if str(entry.get("provider") or "").lower() != "claude":
                continue
            name = str(entry.get("name") or "")
            if name:
                await client.ensure_tool_prefix_disabled(name)
    except CLIProxyManagementError as error:
        logger.warning("Claude tool_prefix_disabled fixup failed: %s", error)


async def confirmed_oauth_status(
    client: CLIProxyManagementClient,
    state: str,
    provider: Optional[str],
) -> tuple[str, str]:
    """(status, detail) for a pending login, with confirm-on-ok.

    Thin wrapper over the ONE confirm-on-ok implementation
    (`management_client.confirm_login_landed`); this layer resolves the
    provider spec and runs the Claude post-login fixup on a confirmed ok.
    Without a known provider the bare (unconfirmable) status is returned.
    """
    spec = get_cliproxy_provider(provider) if provider else None
    if spec is None:
        status = await client.auth_status(state)
        if status not in ("wait", "ok", "error"):
            status = "error"
        return status, ""
    status, detail = await confirm_login_landed(client, state, spec)
    if status == "ok" and spec.id == "claude":
        await _fixup_claude_auth_files(client)
    return status, detail


async def list_cliproxy_models(
    client: CLIProxyManagementClient,
    management_url: str,
) -> list[dict[str, str]]:
    """Live model list through the proxy's data plane.

    The proxy's OpenAI-compatible /v1/models spans every logged-in
    subscription (there is no per-provider attribution), so the list is
    unfiltered. The gatekeeper is READ through the management API (the
    api-keys knob is masked on REST reads, so no frontend can resolve one
    itself); a key-less proxy has an OPEN data plane, so the request then
    goes out unauthenticated. Never mints: a read must not flip the proxy
    to key-required (that is apply-route's job). Raises
    CLIProxyManagementError / httpx.HTTPError upward.
    """
    keys = await configured_gatekeeper_keys(client)
    headers = {"Authorization": f"Bearer {keys[0]}"} if keys else {}
    base = (management_url or "").strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")].rstrip("/")
    async with httpx.AsyncClient(timeout=10.0) as http:
        response = await http.get(f"{base}/v1/models", headers=headers)
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError:
            payload = None
    data = payload.get("data") if isinstance(payload, dict) else None
    models = [
        {
            "id": str(item.get("id") or ""),
            "owned_by": str(item.get("owned_by") or ""),
        }
        for item in (data if isinstance(data, list) else [])
        if isinstance(item, dict) and item.get("id")
    ]
    models.sort(key=lambda entry: entry["id"])
    return models


def create_cliproxy_router(
    require_admin_user: Callable,
    get_agent_fn: Callable[[], Any],
    get_settings_fn: Callable[[], Any],
    require_thread_access_fn: Optional[Callable[[Any, str], Any]] = None,
) -> APIRouter:
    router = APIRouter(prefix="/cliproxy", tags=["CLIProxy"])

    # Keyed by base_url only (never by the secret, so rotated secrets are not
    # retained in memory) and bounded; one backend talks to one proxy.
    probe_cache: dict[str, tuple[float, dict[str, bool]]] = {}

    def _client_or_none() -> Optional[CLIProxyManagementClient]:
        return management_client_from_settings(get_settings_fn())

    def _client_or_400() -> CLIProxyManagementClient:
        client = _client_or_none()
        if client is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "CLIProxy management is not configured; set "
                    "CLIPROXY_MANAGEMENT_URL and CLIPROXY_MANAGEMENT_KEY first"
                ),
            )
        return client

    async def _cached_probe(
        client: CLIProxyManagementClient, *, refresh: bool = False
    ) -> dict[str, bool]:
        key = client.base_url
        now = time.monotonic()
        if not refresh:
            cached = probe_cache.get(key)
            if cached and now - cached[0] < _PROBE_CACHE_TTL_SECONDS:
                return cached[1]
        probed = await client.probe_providers(refresh=True)
        if len(probe_cache) > 8:
            probe_cache.clear()
        probe_cache[key] = (now, probed)
        return probed

    def _raise_for(error: CLIProxyManagementError) -> HTTPException:
        return HTTPException(
            status_code=management_error_status(error), detail=str(error)
        )

    @router.get("/catalog")
    async def get_catalog(
        admin=Depends(require_admin_user),
    ) -> list[CLIProxyProviderInfo]:
        """The static provider catalog (no proxy contact)."""
        return [_provider_info(spec) for spec in list_cliproxy_providers()]

    @router.get("/status")
    async def get_status(
        refresh: bool = False,
        admin=Depends(require_admin_user),
    ) -> CLIProxyStatusResponse:
        """Reachability + per-provider support and login state.

        Never raises for an absent or unreachable proxy; frontends render the
        degraded payload instead.
        """
        client = _client_or_none()
        if client is None:
            return CLIProxyStatusResponse(
                configured=False,
                detail="CLIPROXY_MANAGEMENT_URL / CLIPROXY_MANAGEMENT_KEY not set",
                providers=[_provider_info(s) for s in list_cliproxy_providers()],
            )
        try:
            probed = await _cached_probe(client, refresh=refresh)
            auth_files = await client.list_auth_files()
        except CLIProxyManagementError as error:
            return CLIProxyStatusResponse(
                configured=True,
                reachable=False,
                management_html_url=client.management_html_url,
                detail=str(error),
                providers=[_provider_info(s) for s in list_cliproxy_providers()],
            )
        active_providers = {
            str(entry.get("provider") or "").lower()
            for entry in auth_files
            if not entry.get("disabled") and not entry.get("unavailable")
        }
        providers = [
            _provider_info(
                spec,
                supported=probed.get(spec.id),
                logged_in=spec.auth_file_provider in active_providers,
            )
            for spec in list_cliproxy_providers()
        ]
        return CLIProxyStatusResponse(
            configured=True,
            reachable=True,
            management_html_url=client.management_html_url,
            providers=providers,
        )

    @router.post("/oauth/start")
    async def start_oauth(
        request: CLIProxyOAuthStartRequest,
        admin=Depends(require_admin_user),
    ) -> CLIProxyOAuthStartResponse:
        spec = _require_spec(request.provider)
        client = _client_or_400()
        try:
            started = await client.start_oauth(
                spec, project_id=request.project_id
            )
        except CLIProxyManagementError as error:
            raise _raise_for(error) from error
        return CLIProxyOAuthStartResponse(
            provider=spec.id,
            flow=spec.flow,
            url=started["url"],
            state=started["state"],
        )

    @router.post("/oauth/callback")
    async def oauth_callback(
        request: CLIProxyOAuthCallbackRequest,
        admin=Depends(require_admin_user),
    ) -> dict[str, str]:
        spec = _require_spec(request.provider)
        if not request.redirect_url and not (request.code and request.state):
            raise HTTPException(
                status_code=400,
                detail="Provide redirect_url or code and state",
            )
        client = _client_or_400()
        try:
            await client.oauth_callback(
                spec,
                redirect_url=request.redirect_url,
                code=request.code,
                state=request.state,
            )
        except CLIProxyManagementError as error:
            raise _raise_for(error) from error
        return {"status": "ok"}

    @router.get("/oauth/status")
    async def oauth_status(
        state: str,
        provider: Optional[str] = None,
        admin=Depends(require_admin_user),
    ) -> CLIProxyOAuthStatusResponse:
        """Poll a pending login: confirm-on-ok + the Claude post-login fixup.

        With a provider, ok means an active auth file was VERIFIED (detail
        carries the account label); a bare proxy ok with no auth file comes
        back as error with the explanation (the proxy answers ok for unknown
        or expired sessions).
        """
        client = _client_or_400()
        try:
            status, detail = await confirmed_oauth_status(
                client, state, provider
            )
        except CLIProxyManagementError as error:
            raise _raise_for(error) from error
        return CLIProxyOAuthStatusResponse(status=status, detail=detail)  # type: ignore[arg-type]

    @router.get("/models")
    async def get_models(
        admin=Depends(require_admin_user),
    ) -> dict[str, Any]:
        """Live model list via the proxy data plane (server-resolved cpx- key).

        Spans every logged-in subscription; the gatekeeper never leaves the
        backend (the api-keys knob is masked on REST reads by design).
        """
        settings = get_settings_fn()
        client = _client_or_400()
        management_url = (
            getattr(settings, "cliproxy_management_url", None) or ""
        ).strip()
        try:
            models = await list_cliproxy_models(client, management_url)
        except CLIProxyManagementError as error:
            raise _raise_for(error) from error
        except httpx.HTTPError as error:
            raise HTTPException(
                status_code=502,
                detail=f"CLIProxy model list failed: {error}",
            ) from error
        return {"models": models}

    @router.get("/auth-files")
    async def list_auth_files(
        provider: Optional[str] = None,
        admin=Depends(require_admin_user),
    ) -> list[dict[str, Any]]:
        client = _client_or_400()
        try:
            files = await client.list_auth_files()
        except CLIProxyManagementError as error:
            raise _raise_for(error) from error
        if provider:
            spec = _require_spec(provider)
            files = [
                entry
                for entry in files
                if str(entry.get("provider") or "").lower()
                == spec.auth_file_provider
            ]
        return files

    @router.patch("/auth-files/{name}")
    async def patch_auth_file(
        name: str,
        request: CLIProxyAuthFilePatchRequest,
        admin=Depends(require_admin_user),
    ) -> dict[str, str]:
        if request.disabled is None and request.priority is None:
            raise HTTPException(status_code=400, detail="No fields to update")
        client = _client_or_400()
        try:
            if request.disabled is not None:
                await client.set_auth_file_disabled(
                    name, disabled=request.disabled
                )
            if request.priority is not None:
                await client.set_auth_file_fields(
                    name, {"priority": request.priority}
                )
        except CLIProxyManagementError as error:
            raise _raise_for(error) from error
        return {"status": "ok"}

    @router.delete("/auth-files/{name}")
    async def delete_auth_file(
        name: str,
        admin=Depends(require_admin_user),
    ) -> dict[str, str]:
        client = _client_or_400()
        try:
            await client.delete_auth_file(name)
        except CLIProxyManagementError as error:
            raise _raise_for(error) from error
        return {"status": "ok"}

    def _mask_knobs(knobs: dict[str, Any]) -> dict[str, Any]:
        # The api-keys list holds the cpx- gatekeepers; echoing them verbatim
        # would bypass the masking discipline /settings/env applies to the
        # same material. PATCH still accepts full values for replacement.
        keys = knobs.get("api-keys")
        if isinstance(keys, list):
            knobs = dict(knobs)
            knobs["api-keys"] = [
                f"{key[:8]}…{key[-2:]}" if isinstance(key, str) and len(key) > 12 else "***"
                for key in keys
            ]
        return knobs

    @router.get("/config")
    async def get_config(
        admin=Depends(require_admin_user),
    ) -> dict[str, Any]:
        client = _client_or_400()
        try:
            return {"knobs": _mask_knobs(await client.get_config_knobs())}
        except CLIProxyManagementError as error:
            raise _raise_for(error) from error

    @router.patch("/config")
    async def patch_config(
        request: CLIProxyConfigPatchRequest,
        admin=Depends(require_admin_user),
    ) -> dict[str, Any]:
        unknown = set(request.knobs) - set(CONFIG_KNOB_PATHS)
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown config knobs: {sorted(unknown)}",
            )
        # GET masks the gatekeeper list, so a naive read-edit-write round trip
        # would replace the proxy's real keys with masked garbage; reject any
        # value that looks masked instead of clobbering.
        api_keys = request.knobs.get("api-keys")
        if isinstance(api_keys, list) and any(
            isinstance(key, str) and ("\u2026" in key or key == "***")
            for key in api_keys
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    "api-keys contains masked values; send the full key list "
                    "(reads of this knob are masked, so edit-and-resend is "
                    "not supported for it)"
                ),
            )
        client = _client_or_400()
        try:
            for path, value in request.knobs.items():
                await client.set_config_knob(path, value)
            return {"knobs": _mask_knobs(await client.get_config_knobs())}
        except CLIProxyManagementError as error:
            raise _raise_for(error) from error

    @router.post("/apply-route")
    async def apply_route(
        request: CLIProxyApplyRouteRequest,
        admin=Depends(require_admin_user),
    ) -> CLIProxyApplyRouteResponse:
        """Turn a catalog entry into Nymeria LLM settings (global or thread)."""
        return await perform_apply_route(
            request,
            settings=get_settings_fn(),
            agent=get_agent_fn(),
            get_settings_fn=get_settings_fn,
            admin=admin,
            require_thread_access_fn=require_thread_access_fn,
        )

    return router


async def perform_apply_route(
    request: CLIProxyApplyRouteRequest,
    *,
    settings: Any,
    agent: Any,
    get_settings_fn: Callable[[], Any],
    admin: Any,
    require_thread_access_fn: Optional[Callable[[Any, str], Any]] = None,
) -> CLIProxyApplyRouteResponse:
    """The POST /cliproxy/apply-route body, module-scope for facade sharing.

    The single source of the route-shape math: base URL derived from the
    management URL (the LLM caller is the same process as the management
    caller, so the host is reachable by construction), root vs /v1 and the
    key slot from the catalog. The cpx- gatekeeper goes to
    ANTHROPIC_API_KEY / OPENAI_API_KEY, never the *_DIRECT_* slots (see
    core/agent_llm_config.py). Raises HTTPException; the in-process command
    facade converts it to the wire-shaped error.
    """
    spec = _require_spec(request.provider)
    management_url = (
        getattr(settings, "cliproxy_management_url", None) or ""
    ).strip()
    if not management_url:
        raise HTTPException(
            status_code=400,
            detail=(
                "CLIPROXY_MANAGEMENT_URL is not set; configure the proxy "
                "before applying a route"
            ),
        )
    base_url = cliproxy_data_plane_url(management_url, spec)
    model = request.model.strip() or spec.default_model
    gatekeeper = (request.gatekeeper_key or "").strip()
    if not gatekeeper:
        # The settings slot may hold a REAL provider key (e.g. an sk-
        # OPENAI_API_KEY on a codex route): adopting that as the proxy
        # gatekeeper would 401 every call on a key-required proxy. A cpx-
        # key is trusted by shape; anything else is validated against the
        # proxy's api-keys list, falling through to read-or-mint (the
        # fresh-install fallback: no frontend can supply the key because
        # the api-keys knob is masked on REST reads by design).
        candidate = (getattr(settings, spec.key_setting, None) or "").strip()
        if candidate.startswith("cpx-"):
            gatekeeper = candidate
        else:
            client = management_client_from_settings(settings)
            if client is not None:
                try:
                    keys = await configured_gatekeeper_keys(client)
                    if candidate and candidate in keys:
                        gatekeeper = candidate
                    elif keys:
                        gatekeeper = keys[0]
                    elif candidate:
                        # Open data plane: any value works; keep the
                        # existing key rather than minting (a mint would
                        # flip the proxy to key-required for every other
                        # consumer).
                        gatekeeper = candidate
                    else:
                        gatekeeper = await resolve_or_mint_gatekeeper(client)
                except CLIProxyManagementError as error:
                    logger.warning(
                        "apply-route gatekeeper auto-resolve failed: %s", error
                    )
                    # Management read failed: an existing settings key is
                    # still the best available answer (legacy behavior).
                    gatekeeper = candidate
            else:
                gatekeeper = candidate
    if not gatekeeper:
        raise HTTPException(
            status_code=422,
            detail=(
                f"No gatekeeper key available for {spec.id}: none in the "
                "request or settings, and one could not be read or minted "
                "through the management API; pass gatekeeper_key (a cpx- "
                "key from the proxy's api-keys list)"
            ),
        )

    if request.scope == "thread":
        if require_thread_access_fn is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Thread-scoped apply is not supported on this surface; "
                    "use scope=global or the REST route"
                ),
            )
        return _apply_route_thread(
            agent=agent,
            admin=admin,
            spec=spec,
            thread_id=request.thread_id,
            model=model,
            base_url=base_url,
            gatekeeper=gatekeeper,
            require_thread_access_fn=require_thread_access_fn,
        )
    return _apply_route_global(
        agent=agent,
        settings=settings,
        spec=spec,
        model=model,
        base_url=base_url,
        gatekeeper=gatekeeper,
        get_settings_fn=get_settings_fn,
    )
