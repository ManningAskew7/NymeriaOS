"""CLIProxy subscription-OAuth branch: disclaimer, endpoint, provider, login, model.

Shown only when the auth step picked `CLIPROXY_OAUTH` (or a legacy per-provider
alias). The branch drives the proxy's `/v0/management` API end to end: detect
or generate a deployment, start the OAuth flow, complete it (native callback,
device approval, or the pasted-redirect path for headless hosts), poll to
done, then pick a model from the live list.

Two deliberate deviations from the collect-only step pattern, both forced by
OAuth being an interactive dance with an external service: the endpoint step
brings a generated deployment up immediately (login needs a running proxy
before finalize), and the login step talks to the proxy from a background
worker (`ModelStep._load_models` precedent).
"""

from __future__ import annotations

import asyncio
import logging
import webbrowser
from typing import TYPE_CHECKING, Any

import httpx
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Input, Static

from ...cliproxy.catalog import (
    CLIProxyProviderSpec,
    get_cliproxy_provider,
    list_cliproxy_providers,
)
from ...cliproxy.management_client import (
    CLIProxyAuthError,
    CLIProxyManagementClient,
    CLIProxyManagementError,
    CLIProxyUnsupported,
    confirm_login_landed,
)
from ...onboarding import ProviderAuthMethod
from ..cliproxy_deploy import (
    CLIPROXY_HOST_PORT,
    compose_up,
    docker_available,
    generate_cliproxy_deployment,
    mint_gatekeeper_key,
    mint_management_secret,
    read_existing_secrets,
)

# The pure (TUI-free) login helpers live in setup.cliproxy_login so the
# headless path can use them without importing Textual; re-imported here for
# the step classes and the module's historical import surface. These are
# by-value bindings: a test that wants to affect THIS module's behavior must
# monkeypatch nymeria.setup.steps.cliproxy, not nymeria.setup.cliproxy_login.
from ..cliproxy_login import (
    LOGIN_POLL_INTERVAL_SECONDS,
    CLIPROXY_BACKOFF_NOTE,
    LOGIN_TIMEOUT_SECONDS,
    _browser_launch_blocked,
    present_login_entry,
    ensure_claude_tool_prefix_disabled,
    ensure_gatekeeper_key,
    login_account_label,
    make_management_client,
    management_credentials,
)
from ..nav import Step
from ..state import WizardState
from ..widgets import ListItem, SearchableList
from .auth import is_cliproxy_auth
from .base import Choice, CircleRadioButton, FormStep, SingleSelectStep, WizardStep

if TYPE_CHECKING:
    from ..app import SetupWizardApp

logger = logging.getLogger(__name__)

DEFAULT_LOCAL_MANAGEMENT_URL = f"http://localhost:{CLIPROXY_HOST_PORT}"

TOS_DISCLAIMER = (
    "Subscription OAuth routes your personal AI subscription (Claude Max/Pro, "
    "ChatGPT Plus/Pro, Gemini, Kimi, Grok, ...) through CLIProxy so Nymeria "
    "can use it instead of a pay-per-token API key. Providers generally "
    "consider third-party use of their subscription clients a terms-of-service "
    "violation and may rate-limit, suspend, or ban the account. Use a "
    "dedicated account if that risk matters to you. Nothing in Nymeria "
    "requires this path; a direct API key does everything."
)




# --- disclaimer ---------------------------------------------------------------


def make_cliproxy_disclaimer_step() -> Step:
    choices = [
        Choice(
            value="accept",
            label="I understand the risk; continue",
            description="Proceed to pick a CLI subscription and log in.",
        ),
        Choice(
            value="decline",
            label="Use a direct API key instead",
            description="Switch back to the recommended, TOS-clean path.",
        ),
    ]

    def get_initial(_state: WizardState) -> str:
        return "accept"

    def store(state: WizardState, value: Any) -> None:
        if value == "decline":
            # The API-key trio re-applies and the rest of this branch drops out.
            state.auth_method = ProviderAuthMethod.API_KEY
        else:
            # Explicit re-set so accepting again after a decline (e.g. via
            # back-navigation onto this screen) re-enters the branch.
            state.auth_method = ProviderAuthMethod.CLIPROXY_OAUTH

    def build(wizard: "SetupWizardApp", number: int, total: int) -> SingleSelectStep:
        return SingleSelectStep(
            wizard,
            number,
            total,
            step_id="cliproxy_disclaimer",
            title="Subscription OAuth: read this first",
            choices=choices,
            get_initial=get_initial,
            store=store,
            note=TOS_DISCLAIMER,
        )

    return Step(id="cliproxy_disclaimer", applies=is_cliproxy_auth, build=build)


# --- endpoint (detect or generate the proxy) -----------------------------------


class CLIProxyEndpointStep(FormStep):
    """Point at an existing CLIProxy or generate-and-start a new deployment."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._deploying = False
        self._deploy_done = False

    def compose_body(self) -> ComposeResult:
        url, _key = management_credentials(self.state)
        keep_hint = (
            " (blank keeps the existing secret)"
            if "CLIPROXY_MANAGEMENT_KEY" in self.state.present_env_keys
            else ""
        )
        yield Static("CLIProxy URL (host root, no /v1)", classes="field-label")
        yield Input(
            value=url or DEFAULT_LOCAL_MANAGEMENT_URL,
            placeholder=DEFAULT_LOCAL_MANAGEMENT_URL,
            id="cliproxy-url",
        )
        yield Static(f"Management secret{keep_hint}", classes="field-label")
        yield Input(
            value="",
            placeholder=f"cpm-...{keep_hint}",
            password=True,
            id="cliproxy-key",
        )
        with Vertical(classes="radio-group"):
            yield CircleRadioButton("Use the CLIProxy at the URL above", value=True)
            yield CircleRadioButton(
                "Set up a new CLIProxy container here (Docker required)",
                value=False,
            )
        yield Static("", id="cliproxy-endpoint-status")

    def on_mount(self) -> None:
        self.query_one("#cliproxy-url", Input).focus()
        self._detect_existing()

    @work(exclusive=True)
    async def _detect_existing(self) -> None:
        """Best-effort reachability hint for the prefilled URL (no auth sent)."""
        url = self.query_one("#cliproxy-url", Input).value.strip().rstrip("/")
        if not url:
            return
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                await client.get(url + "/")
        except httpx.TransportError:
            if docker_available():
                self._status(
                    "Nothing answered at that URL. You can set up a new "
                    "CLIProxy container below."
                )
            return
        if self.is_mounted:
            self._status("A server is answering at that URL.")

    def _status(self, message: str) -> None:
        if self.is_mounted:
            self.query_one("#cliproxy-endpoint-status", Static).update(message)

    def _deploy_selected(self) -> bool:
        buttons = list(self.query_one(".radio-group").query(CircleRadioButton))
        return len(buttons) > 1 and bool(buttons[1].value)

    def collect(self) -> bool:
        url = self.query_one("#cliproxy-url", Input).value.strip().rstrip("/")
        key = self.query_one("#cliproxy-key", Input).value.strip()

        if self._deploy_selected():
            if self._deploy_done:
                return True
            if self._deploying:
                self.show_error("Still starting the CLIProxy container...")
                return False
            if not docker_available():
                self.show_error(
                    "docker is not on PATH. Install Docker or point at an "
                    "existing CLIProxy instead."
                )
                return False
            self._deploying = True
            self._provision()
            self.show_error("")
            self._status("Generating and starting the CLIProxy container...")
            return False

        if not url:
            self.query_one("#cliproxy-url", Input).focus()
            self.show_error("Enter the CLIProxy URL (for example http://localhost:8318).")
            return False
        keep_existing = (
            not key
            and self.state.reconfigure
            and "CLIPROXY_MANAGEMENT_KEY" in self.state.present_env_keys
        )
        if not key and not keep_existing:
            self.query_one("#cliproxy-key", Input).focus()
            self.show_error(
                "Enter the proxy's remote-management secret (the plaintext "
                "value, found next to its config.yaml)."
            )
            return False
        self.state.cliproxy_management_url = url
        self.state.cliproxy_management_key = key
        self.state.cliproxy_deploy = False
        return True

    @work(exclusive=True)
    async def _provision(self) -> None:
        from .. import finalize as finalize_mod
        from ...onboarding import DockerStack, HostingOption

        state = self.state
        root = finalize_mod.resolve_runtime_root(
            state, for_docker=state.hosting is HostingOption.DOCKER
        )
        # Reuse a prior run's secrets: the container (if it came up before a
        # back-out) already bcrypt-hashed the original management secret, so
        # minting a fresh one would lock the wizard out of its own proxy.
        existing_secret, existing_gatekeeper = read_existing_secrets(root / "cliproxy")
        secret = existing_secret or mint_management_secret()
        gatekeeper = existing_gatekeeper or mint_gatekeeper_key()
        join_network = (
            "nymeria_edge"
            if state.hosting is HostingOption.DOCKER
            and state.docker_stack is DockerStack.FULL
            else None
        )
        # The single-container backend reaches the proxy through
        # host.docker.internal (the Docker bridge, not loopback), so only
        # that shape keeps the all-interfaces publish; every other shape
        # binds the host port to 127.0.0.1 because Docker-published ports
        # bypass UFW and the proxy must never be internet-facing.
        needs_bridge_publish = (
            state.hosting is HostingOption.DOCKER
            and state.docker_stack is not DockerStack.FULL
        )
        deployment = generate_cliproxy_deployment(
            root / "cliproxy",
            management_secret=secret,
            gatekeeper_key=gatekeeper,
            join_network=join_network,
            loopback_only=not needs_bridge_publish,
        )
        # Record the endpoint before the (cancellable) bring-up so a back-out
        # mid-provision does not lose the credentials of a container that may
        # still finish starting.
        state.cliproxy_management_url = deployment.management_url
        state.cliproxy_management_key = secret
        state.cliproxy_gatekeeper_key = gatekeeper
        # The full stack's edge network is external to the proxy's compose
        # and does not exist yet on a fresh host: compose_up creates it first.
        ok, detail = await asyncio.to_thread(
            compose_up, deployment.directory, join_network=join_network
        )
        if not ok:
            self._deploying = False
            self.show_error(f"Could not start CLIProxy: {detail}")
            self._status("")
            return
        client = CLIProxyManagementClient(deployment.management_url, secret)
        deadline = asyncio.get_event_loop().time() + 90.0
        while True:
            try:
                await client.list_auth_files()
                break
            except CLIProxyManagementError:
                if asyncio.get_event_loop().time() > deadline:
                    self._deploying = False
                    self.show_error(
                        "CLIProxy started but its management API did not "
                        "answer within 90s; check `docker logs nymeria-cliproxy`."
                    )
                    return
                await asyncio.sleep(2.0)
        state.cliproxy_deploy = True
        self._deploy_done = True
        self._deploying = False
        self._status(f"CLIProxy is running at {deployment.management_url}.")
        self._wizard.advance()


def make_cliproxy_endpoint_step() -> Step:
    def build(wizard: "SetupWizardApp", number: int, total: int) -> CLIProxyEndpointStep:
        return CLIProxyEndpointStep(
            wizard,
            number,
            total,
            step_id="cliproxy_endpoint",
            title="Where is CLIProxy?",
            note=(
                "CLIProxy is a small separate container that holds the "
                "subscription logins. Point at one you already run, or let "
                "setup generate and start a pinned deployment here."
            ),
        )

    return Step(id="cliproxy_endpoint", applies=is_cliproxy_auth, build=build)


# --- provider pick --------------------------------------------------------------


def _provider_choices() -> list[Choice]:
    out: list[Choice] = []
    for spec in list_cliproxy_providers():
        description = spec.description
        if spec.tos_warning:
            description = f"{description} WARNING: {spec.tos_warning}"
        out.append(Choice(value=spec.id, label=spec.label, description=description))
    return out


def make_cliproxy_provider_step() -> Step:
    def get_initial(state: WizardState) -> str:
        return state.cliproxy_provider or "claude"

    def store(state: WizardState, value: Any) -> None:
        if state.cliproxy_provider != value:
            # A different CLI invalidates the previous login/model picks.
            state.cliproxy_logged_in = False
        state.cliproxy_provider = str(value)

    def build(wizard: "SetupWizardApp", number: int, total: int) -> SingleSelectStep:
        return SingleSelectStep(
            wizard,
            number,
            total,
            step_id="cliproxy_provider",
            title="Which subscription should Nymeria use?",
            choices=_provider_choices(),
            get_initial=get_initial,
            store=store,
            note=(
                "The pick is probe-gated against the running proxy on the "
                "next step; an older proxy build may not support every entry."
            ),
        )

    return Step(id="cliproxy_provider", applies=is_cliproxy_auth, build=build)


# --- login ----------------------------------------------------------------------


class CLIProxyLoginStep(WizardStep):
    """Run the OAuth dance through the proxy's management API."""

    BINDINGS = WizardStep.BINDINGS + [
        Binding("ctrl+r", "relogin", "Re-login", priority=True),
    ]

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._oauth_state: str | None = None
        self._force_relogin = False
        self._spec: CLIProxyProviderSpec | None = None

    def compose_body(self) -> ComposeResult:
        yield Static("Starting login...", id="cliproxy-login-status")
        yield Static("", id="cliproxy-login-url")
        yield Static("If the browser ends on a dead localhost page, paste that page's full URL here:", classes="field-label")
        yield Input(placeholder="http://localhost:.../callback?code=...&state=...", id="cliproxy-callback")

    def on_mount(self) -> None:
        self._spec = get_cliproxy_provider(self.state.cliproxy_provider or "")
        self._run_login()

    def _status(self, message: str) -> None:
        if self.is_mounted:
            self.query_one("#cliproxy-login-status", Static).update(message)

    def action_relogin(self) -> None:
        self._force_relogin = True
        self.state.cliproxy_logged_in = False
        self._run_login()

    @work(exclusive=True)
    async def _run_login(self) -> None:
        spec = self._spec
        if spec is None:
            self._status("No CLI selected; press Esc and pick one.")
            return
        client = make_management_client(self.state)
        if client is None:
            self._status(
                "The CLIProxy endpoint is not configured; press Esc and fill "
                "in the URL and management secret."
            )
            return
        try:
            files = await client.list_auth_files()
        except CLIProxyAuthError:
            self._status(
                "The proxy rejected the management secret; press Esc and "
                "correct it (the proxy bans an IP after 5 bad attempts)."
            )
            return
        except CLIProxyManagementError as error:
            self._status(f"Cannot reach the proxy: {error}")
            return

        # PRESENT, not active (#149): a login in the proxy's error backoff
        # still exists; starting a fresh OAuth for it is pointless (the
        # backoff clears on its own) and used to happen here.
        entry = present_login_entry(files, spec)
        if entry is not None and not self._force_relogin:
            account = login_account_label(entry)
            backoff_note = (
                f" ({CLIPROXY_BACKOFF_NOTE})" if entry.get("unavailable") else ""
            )
            await self._post_login(client, spec)
            self._status(
                f"Already logged in{f' as {account}' if account else ''}"
                f"{backoff_note}. "
                "Press Enter to continue, or Ctrl+R to log in again."
            )
            return

        try:
            started = await client.start_oauth(spec)
        except CLIProxyUnsupported:
            self._status(
                f"This proxy build does not support {spec.label}; press Esc "
                "and pick another subscription (or upgrade the proxy)."
            )
            return
        except CLIProxyManagementError as error:
            self._status(f"Could not start the login: {error}")
            return

        self._oauth_state = started["state"]
        url = started["url"]
        if self.is_mounted:
            self.query_one("#cliproxy-login-url", Static).update(url)
        if spec.flow == "device":
            self._status(
                "Open the link above on any device and approve the login; "
                "this screen advances automatically."
            )
        else:
            self._status(
                "Approve the login in your browser. If it ends on an "
                "unreachable localhost page, paste that page's full URL below."
            )
            if not _browser_launch_blocked():
                try:
                    webbrowser.open(url)
                except Exception as exc:
                    # Purely best-effort; the URL is on screen either way.
                    logger.debug("webbrowser.open failed: %s", exc)

        deadline = asyncio.get_event_loop().time() + LOGIN_TIMEOUT_SECONDS
        while asyncio.get_event_loop().time() < deadline:
            # confirm_login_landed is THE confirm-on-ok implementation (a
            # bare polled ok proves nothing; see management_client).
            try:
                status, detail = await confirm_login_landed(
                    client, self._oauth_state, spec
                )
            except CLIProxyManagementError as error:
                self._status(f"Lost contact with the proxy: {error}")
                return
            if status == "ok":
                await self._post_login(client, spec)
                self._status(
                    f"Login complete{f' as {detail}' if detail else ''}. "
                    "Press Enter to continue."
                )
                return
            if status == "error":
                self._status(
                    (detail or "The provider reported a login error.")
                    + " Ctrl+R to try again."
                )
                return
            await asyncio.sleep(LOGIN_POLL_INTERVAL_SECONDS)
        self._status("The login session expired (10 minutes). Ctrl+R to restart it.")

    async def _post_login(
        self, client: CLIProxyManagementClient, spec: CLIProxyProviderSpec
    ) -> None:
        if spec.id == "claude":
            try:
                await ensure_claude_tool_prefix_disabled(client)
            except CLIProxyManagementError as exc:
                # Best-effort hardening; never fail a completed login over it.
                logger.warning("tool_prefix_disabled fixup failed: %s", exc)
        gatekeeper_warning = ""
        try:
            await ensure_gatekeeper_key(self.state, client)
        except CLIProxyManagementError as exc:
            logger.warning("could not resolve a gatekeeper api-key: %s", exc)
            gatekeeper_warning = (
                " WARNING: no cpx- gatekeeper key could be read or minted from "
                "the proxy; finalize will stop until one is supplied."
            )
        self.state.cliproxy_logged_in = True
        if gatekeeper_warning:
            self.show_error(gatekeeper_warning.strip())

    def collect(self) -> bool:
        pasted = self.query_one("#cliproxy-callback", Input).value.strip()
        if pasted and not self.state.cliproxy_logged_in:
            self._deliver_callback(pasted)
            self.show_error("")
            self._status("Delivering the callback to the proxy...")
            return False
        if not self.state.cliproxy_logged_in:
            self.show_error(
                "Complete the login first (or Ctrl+S to skip and log in "
                "later from Settings)."
            )
            return False
        return True

    @work(exclusive=False)
    async def _deliver_callback(self, redirect_url: str) -> None:
        spec = self._spec
        client = make_management_client(self.state)
        if spec is None or client is None:
            return
        try:
            await client.oauth_callback(spec, redirect_url=redirect_url)
        except CLIProxyManagementError as error:
            self._status(f"The proxy rejected the callback: {error}")
            return
        if self.is_mounted:
            self.query_one("#cliproxy-callback", Input).value = ""
        # The polling loop in _run_login observes the ok and finishes up.


def make_cliproxy_login_step() -> Step:
    def build(wizard: "SetupWizardApp", number: int, total: int) -> CLIProxyLoginStep:
        return CLIProxyLoginStep(
            wizard,
            number,
            total,
            step_id="cliproxy_login",
            title="Log in to the subscription",
            note=(
                "The proxy performs the OAuth flow and stores the resulting "
                "token in its own auth directory; Nymeria never sees the "
                "provider password."
            ),
            hint="enter continue/deliver   ctrl+r re-login   ctrl+s skip   esc back   ctrl+q quit",
        )

    return Step(
        id="cliproxy_login",
        applies=lambda state: is_cliproxy_auth(state) and bool(state.cliproxy_provider),
        build=build,
    )


# --- model pick -------------------------------------------------------------------


class CLIProxyModelStep(WizardStep):
    """Searchable model list fetched from the proxy's /v1/models."""

    def compose_body(self) -> ComposeResult:
        spec = get_cliproxy_provider(self.state.cliproxy_provider or "")
        default_model = self.state.model or (spec.default_model if spec else "")
        yield Static("Model", classes="field-label")
        yield SearchableList(
            [],
            placeholder="Loading models from the proxy...",
            initial_value=default_model or None,
            search_id="cliproxy-model-search",
            list_id="cliproxy-model-options",
            empty_text="No models listed - type the exact model id",
        )

    def on_mount(self) -> None:
        self.query_one(SearchableList).focus()
        self._load_models()

    @work(exclusive=True)
    async def _load_models(self) -> None:
        url, _key = management_credentials(self.state)
        gatekeeper = self.state.cliproxy_gatekeeper_key.strip()
        if not url or not gatekeeper:
            self.show_error(
                "No proxy data-plane key available; type the model id instead."
            )
            return
        models: list[str] = []
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    f"{url}/v1/models",
                    headers={"Authorization": f"Bearer {gatekeeper}"},
                )
                response.raise_for_status()
                payload = response.json()
                models = sorted(
                    str(item.get("id"))
                    for item in payload.get("data", [])
                    if item.get("id")
                )
        except (httpx.HTTPError, ValueError):
            # Fall through to the type-the-exact-id placeholder below.
            pass
        if not self.is_mounted:
            return
        picker = self.query_one(SearchableList)
        if models:
            picker.set_items([ListItem(value=m, primary=m) for m in models])
            picker.set_placeholder("Type to filter models...")
        else:
            picker.set_placeholder("type the exact model id")
            self.show_error(
                "Could not list models from the proxy. Type the exact model "
                "id and press Enter."
            )

    def collect(self) -> bool:
        picker = self.query_one(SearchableList)
        spec = get_cliproxy_provider(self.state.cliproxy_provider or "")
        model = (picker.selected_value or picker.search_value or "").strip()
        if not model and spec is not None:
            model = spec.default_model
        if not model:
            self.show_error("Enter a model id.")
            return False
        self.state.model = model
        return True


def make_cliproxy_model_step() -> Step:
    def build(wizard: "SetupWizardApp", number: int, total: int) -> CLIProxyModelStep:
        return CLIProxyModelStep(
            wizard,
            number,
            total,
            step_id="cliproxy_model",
            title="Choose a model",
            note=(
                "Models are listed live from the proxy across every "
                "logged-in subscription; pick one served by the CLI you just "
                "set up."
            ),
            hint="type filter   down to list   enter next   ctrl+s skip   esc back",
        )

    return Step(
        id="cliproxy_model",
        applies=lambda state: is_cliproxy_auth(state) and bool(state.cliproxy_provider),
        build=build,
    )


__all__ = [
    "CLIProxyEndpointStep",
    "CLIProxyLoginStep",
    "CLIProxyModelStep",
    "TOS_DISCLAIMER",
    "ensure_gatekeeper_key",
    "make_cliproxy_disclaimer_step",
    "make_cliproxy_endpoint_step",
    "make_cliproxy_login_step",
    "make_cliproxy_model_step",
    "make_cliproxy_provider_step",
    "make_management_client",
    "management_credentials",
]
