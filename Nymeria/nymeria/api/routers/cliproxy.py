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


def create_cliproxy_router(
    require_admin_user: Callable,
    get_agent_fn: Callable[[], Any],
    get_settings_fn: Callable[[], Any],
) -> APIRouter:
    router = APIRouter(prefix="/cliproxy", tags=["CLIProxy"])

    probe_cache: dict[tuple[str, str], tuple[float, dict[str, bool]]] = {}

    def _client_or_none() -> Optional[CLIProxyManagementClient]:
        settings = get_settings_fn()
        url = (getattr(settings, "cliproxy_management_url", None) or "").strip()
        key = (getattr(settings, "cliproxy_management_key", None) or "").strip()
        if not url or not key:
            return None
        return CLIProxyManagementClient(url, key)

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
        key = (client.base_url, client._secret)
        now = time.monotonic()
        if not refresh:
            cached = probe_cache.get(key)
            if cached and now - cached[0] < _PROBE_CACHE_TTL_SECONDS:
                return cached[1]
        probed = await client.probe_providers(refresh=True)
        probe_cache[key] = (now, probed)
        return probed

    def _raise_for(error: CLIProxyManagementError) -> HTTPException:
        if isinstance(error, CLIProxyUnsupported):
            return HTTPException(status_code=422, detail=str(error))
        if isinstance(error, CLIProxyNotFound):
            return HTTPException(status_code=404, detail=str(error))
        if isinstance(error, CLIProxyConflict):
            return HTTPException(status_code=409, detail=str(error))
        # Unreachable and auth failures are both upstream problems: 502 so
        # frontends distinguish them from their own bad requests.
        return HTTPException(status_code=502, detail=str(error))

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
        """Poll a pending login; on success runs the Claude post-login fixup."""
        client = _client_or_400()
        try:
            status = await client.auth_status(state)
        except CLIProxyManagementError as error:
            raise _raise_for(error) from error
        if status == "ok" and provider:
            spec = get_cliproxy_provider(provider)
            if spec is not None and spec.id == "claude":
                await _fixup_claude_auth_files(client)
        if status not in ("wait", "ok", "error"):
            status = "error"
        return CLIProxyOAuthStatusResponse(status=status)  # type: ignore[arg-type]

    async def _fixup_claude_auth_files(
        client: CLIProxyManagementClient,
    ) -> None:
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
            logger.warning(
                "Claude tool_prefix_disabled fixup failed: %s", error
            )

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

    @router.get("/config")
    async def get_config(
        admin=Depends(require_admin_user),
    ) -> dict[str, Any]:
        client = _client_or_400()
        try:
            return {"knobs": await client.get_config_knobs()}
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
        client = _client_or_400()
        try:
            for path, value in request.knobs.items():
                await client.set_config_knob(path, value)
            return {"knobs": await client.get_config_knobs()}
        except CLIProxyManagementError as error:
            raise _raise_for(error) from error

    @router.post("/apply-route")
    async def apply_route(
        request: CLIProxyApplyRouteRequest,
        admin=Depends(require_admin_user),
    ) -> CLIProxyApplyRouteResponse:
        """Turn a catalog entry into Nymeria LLM settings (global or thread).

        The single source of the route-shape math: base URL derived from the
        management URL (the LLM caller is the same process as the management
        caller, so the host is reachable by construction), root vs /v1 and
        the key slot from the catalog. The cpx- gatekeeper goes to
        ANTHROPIC_API_KEY / OPENAI_API_KEY, never the *_DIRECT_* slots (see
        core/agent_llm_config.py).
        """
        spec = _require_spec(request.provider)
        settings = get_settings_fn()
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
            gatekeeper = (getattr(settings, spec.key_setting, None) or "").strip()
        if not gatekeeper:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"No gatekeeper key available for {spec.id}; pass "
                    "gatekeeper_key (a cpx- key from the proxy's api-keys list)"
                ),
            )

        agent = get_agent_fn()
        if request.scope == "thread":
            if not request.thread_id:
                raise HTTPException(
                    status_code=400,
                    detail="thread_id is required when scope is 'thread'",
                )
            manager = agent.thread_config_manager
            config = manager.get_config(request.thread_id) or ThreadConfig(
                thread_id=request.thread_id
            )
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
            return CLIProxyApplyRouteResponse(
                scope="thread",
                provider=spec.nymeria_provider,
                model=model,
                base_url=base_url,
                api_mode=spec.api_mode,
                thread_id=request.thread_id,
            )

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

    return router
