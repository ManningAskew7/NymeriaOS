"""CLIProxy management API schemas (admin-only /cliproxy routes)."""

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class CLIProxyProviderInfo(BaseModel):
    """One catalog entry, optionally merged with live probe/login state."""

    id: str
    label: str
    description: str = ""
    flow: Literal["browser", "device"]
    nymeria_provider: str
    url_shape: Literal["root", "v1"]
    api_mode: str = ""
    key_env_var: str
    default_model: str = ""
    tos_warning: str = ""
    # Live state (None when the proxy is unreachable / not yet probed).
    supported: Optional[bool] = None
    logged_in: Optional[bool] = None


class CLIProxyStatusResponse(BaseModel):
    configured: bool
    reachable: bool = False
    management_html_url: Optional[str] = None
    detail: str = ""
    providers: list[CLIProxyProviderInfo] = Field(default_factory=list)


class CLIProxyOAuthStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    # Gemini CLI only: pin the GCP project instead of auto-selecting.
    project_id: Optional[str] = None


class CLIProxyOAuthStartResponse(BaseModel):
    provider: str
    flow: Literal["browser", "device"]
    url: str
    state: str


class CLIProxyOAuthCallbackRequest(BaseModel):
    """The headless paste path: deliver the redirect the browser landed on."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    redirect_url: Optional[str] = None
    code: Optional[str] = None
    state: Optional[str] = None


class CLIProxyOAuthStatusResponse(BaseModel):
    status: Literal["wait", "ok", "error"]


class CLIProxyAuthFilePatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    disabled: Optional[bool] = None
    priority: Optional[int] = None


class CLIProxyConfigPatchRequest(BaseModel):
    """Partial update of the surfaced knob subset, keyed by knob path."""

    model_config = ConfigDict(extra="forbid")

    knobs: dict[str, Any]


class CLIProxyApplyRouteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str = ""
    scope: Literal["global", "thread"] = "global"
    thread_id: Optional[str] = None
    # The cpx- gatekeeper key; omitted = keep the currently configured one.
    gatekeeper_key: Optional[str] = None


class CLIProxyApplyRouteResponse(BaseModel):
    scope: Literal["global", "thread"]
    provider: str
    model: str
    base_url: str
    api_mode: str = ""
    thread_id: Optional[str] = None
    restart_required: bool = False
