"""CLIProxy management API schemas (admin-only /cliproxy routes)."""

from typing import Any, Literal, Optional, get_args

from pydantic import BaseModel, ConfigDict, Field

# The one spelling of the verification verdicts, shared by the route, both
# TurnExecutor facade twins and the CLI login chain, so the three cannot drift.
CLIProxyVerifyVerdict = Literal["ok", "auth_failed", "inconclusive"]
# The same set at runtime, for validating a verdict that arrived over the wire
# (an older backend can answer with anything).
VERIFY_VERDICTS: frozenset[str] = frozenset(get_args(CLIProxyVerifyVerdict))


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
    # The provider string this CLI's entries carry in the auth-file list.
    auth_file_provider: str = ""
    # All accepted spellings of that string (version-dependent; the union
    # of auth_file_provider and the id). Clients filter with THIS, never
    # by re-deriving the union from the single field.
    auth_file_providers: list[str] = []
    # Live state (None when the proxy is unreachable / not yet probed).
    supported: Optional[bool] = None
    logged_in: Optional[bool] = None
    # True when an enabled login EXISTS but the proxy currently reports it
    # unavailable (error backoff), in which case logged_in stays False
    # (its meaning, "an active entry serves", is unchanged for existing
    # clients). Presence-vs-availability rationale: management_client.
    # present_login_entry.
    unavailable: Optional[bool] = None


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
    # Confirmed ok: the account label when known. Error: what went wrong
    # (notably the unconfirmed-ok trap: the proxy answers ok for unknown or
    # expired sessions, so ok without an active auth file reports here).
    detail: str = ""


class CLIProxyAuthFilePatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    disabled: Optional[bool] = None
    priority: Optional[int] = None


class CLIProxyAuthFileImportRequest(BaseModel):
    """Import an existing auths/*.json document (e.g. from another host).

    ``content`` is the file's JSON text (no multipart; auth files are a few
    KB of JSON and every sibling route speaks JSON bodies).
    """

    model_config = ConfigDict(extra="forbid")

    provider: str
    name: str
    content: str


class CLIProxyAuthFileImportResponse(BaseModel):
    # "ok": the proxy lists an active login for the provider after the
    # upload. "inactive": the upload was ACCEPTED but no active login is
    # listed (disabled, expired, or a different provider's file); the same
    # trust rule as confirm-on-ok, reported honestly instead of a false
    # success.
    status: Literal["ok", "inactive"]
    account: str = ""
    detail: str = ""


class CLIProxyConfigPatchRequest(BaseModel):
    """Partial update of the surfaced knob subset, keyed by knob path."""

    model_config = ConfigDict(extra="forbid")

    knobs: dict[str, Any]


class CLIProxyVerifyRequest(BaseModel):
    """Ask whether a logged-in subscription actually serves traffic."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    # Omitted = the provider spec's default model.
    model: str = ""


class CLIProxyVerifyResponse(BaseModel):
    """``auth_failed`` is the only verdict that means the login is no good.

    ``inconclusive`` covers every non-auth miss and must not be read as
    failure: transient faults (proxy down, unknown model, timeout) and the
    genuine 429 quota window (#161: the probe carries the billing
    fingerprint, so a 429 means the window is exhausted, not that the login
    is bad). Neither may block a good login.
    """

    verdict: CLIProxyVerifyVerdict
    detail: str = ""


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
