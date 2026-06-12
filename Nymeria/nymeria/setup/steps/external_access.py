"""Guided external-access setup: Tailscale serve/funnel and Cloudflare tunnel.

Shown only when the external-access choice picked TAILSCALE or CLOUDFLARE.
Like the CLIProxy login step, these deviate from the collect-only pattern
because tunnel setup is an interactive dance with an external service: Enter
kicks a background worker that detects the tool, drives login/provisioning,
exposes the backend, and verifies the resulting URL; the step advances once a
public origin is stored on `WizardState.public_url` (finalize then writes
NYMERIA_PUBLIC_URL and CORS_ORIGINS). Ctrl+S skips, leaving the choice
recorded but nothing configured.

Verification at this point is tunnel-level: the backend usually is not
running yet, so a relay answer of "bad gateway" counts as success (the tunnel
works; the origin comes up later). The full health + streaming check runs
after a start-now (finalize.verify_public_url_now).
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from rich.markup import escape as escape_markup
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Input, RadioButton, Static

from ...onboarding import ExternalAccess
from ..external_access import (
    CLOUDFLARED_DOWNLOAD_URL,
    TAILSCALE_INSTALL_COMMAND,
    CloudflareError,
    CloudflareTunnelClient,
    TailscaleLoginFlow,
    check_public_health,
    check_public_sse,
    enable_tailscale_serve,
    find_cloudflared,
    find_tailscale,
    permission_hint,
    persist_cloudflared_assets,
    public_origin,
    qr_ascii,
    start_cloudflared_connector,
    tailscale_status,
    tunnel_name_for_hostname,
)
from ..nav import Step
from ..state import WizardState
from .base import CircleRadioButton, FormStep

if TYPE_CHECKING:
    from ..app import SetupWizardApp

LOGIN_WAIT_SECONDS = 300.0
CONNECTOR_HEALTH_WAIT_SECONDS = 30.0


def _applies_tailscale(state: WizardState) -> bool:
    return state.external_access is ExternalAccess.TAILSCALE


def _applies_cloudflare(state: WizardState) -> bool:
    return state.external_access is ExternalAccess.CLOUDFLARE


async def _tunnel_level_check(step: "_TunnelStepBase", url: str) -> None:
    """Probe the new URL and record the outcome on state.

    When the backend already answers through the tunnel, the SSE probe runs
    too, so `public_url_verified` always means "health AND streaming worked".
    "origin_down" is still a pass at this point (the relay answered; the
    backend just is not running yet); anything else is surfaced as a warning
    but does not block continuing.
    """
    probe = await check_public_health(url)
    state = step.state
    state.public_url = url
    state.public_url_verified = False
    if probe.status == "healthy":
        sse = await check_public_sse(url)
        if sse.ok:
            state.public_url_verified = True
            step.set_status(
                f"[green]Verified:[/green] {url} answers and streams events "
                "through the tunnel. Press Enter to continue."
            )
        else:
            step.set_status(
                f"[yellow]{url} answers, but streaming looks broken through "
                f"it ({escape_markup(sse.detail)}). Chat will not stream "
                "until that is fixed.[/yellow] Press Enter to continue, or "
                "Ctrl+R to re-check."
            )
    elif probe.tunnel_reached:
        step.set_status(
            f"[green]Tunnel ready:[/green] {url} answers "
            f"({escape_markup(probe.detail)}). It is verified fully once the "
            "backend is running. Press Enter to continue."
        )
    else:
        step.set_status(
            f"[yellow]Configured, but {url} did not answer "
            f"({escape_markup(probe.detail)}). It may need a minute (DNS, "
            "relay start); Ctrl+R re-checks.[/yellow] Press Enter to continue."
        )


class _TunnelStepBase(FormStep):
    """Shared collect/worker scaffolding for the two tunnel steps."""

    BINDINGS = FormStep.BINDINGS + [
        Binding("ctrl+r", "retry", "Retry", priority=True),
    ]

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._working = False
        self._done = False
        self._last_status = ""

    def set_status(self, message: str) -> None:
        self._last_status = message
        if self.is_mounted:
            self.query_one("#external-status", Static).update(message)

    def set_qr(self, content: str) -> None:
        if self.is_mounted:
            self.query_one("#external-qr", Static).update(content)

    def _show_working_error(self) -> None:
        self.show_error(
            "Still working; wait for the status above (or Ctrl+S to skip)."
        )

    def action_next(self) -> None:
        # Guard BEFORE FormStep's _lock_focused_option, so a mid-flight Enter
        # cannot flip the radio UI away from what the worker is configuring.
        if self._working:
            self._show_working_error()
            return
        super().action_next()

    def action_retry(self) -> None:
        """Ctrl+R: re-run the setup with the CURRENT widget values."""
        if self._working:
            self._show_working_error()
            return
        if not self._retry_runs_worker():
            return
        self._done = False
        self.set_qr("")
        self.show_error("")
        if not self._collect_inputs():
            return
        self._working = True
        self._set_form_disabled(True)
        self._run_setup()

    def _retry_runs_worker(self) -> bool:
        """Hook: veto Ctrl+R when the current branch has nothing to re-run."""
        return True

    def _set_form_disabled(self, disabled: bool) -> None:
        """Freeze the form while the worker runs, so selections and inputs
        cannot drift away from what is actually being configured."""
        for button in self.query(RadioButton):
            button.disabled = disabled
        for field in self.query(Input):
            field.disabled = disabled

    def _lock_focused_option(self) -> None:
        # FormStep locks the focused radio synchronously, but the Changed
        # message that resets `_done` arrives async, AFTER collect() has read
        # it. Detect the selection change here, in the same key event.
        before = tuple(button.value for button in self.query(RadioButton))
        super()._lock_focused_option()
        after = tuple(button.value for button in self.query(RadioButton))
        if before != after and not self._working:
            self._done = False

    def action_skip(self) -> None:
        # A skipped setup must not keep mutating state from a hidden screen:
        # cancel this node's workers (the tailscale login subprocess dies via
        # the worker's finally) before advancing.
        self.app.workers.cancel_node(self)
        self._working = False
        super().action_skip()

    def on_input_changed(self, _event: object) -> None:
        # Edited inputs invalidate a completed run, so the next Enter re-runs
        # with the new values instead of silently advancing on stale ones.
        if not self._working:
            self._done = False

    def _on_single_select(self, group: object, button: RadioButton) -> None:
        if not self._working:
            self._done = False

    def collect(self) -> bool:
        if self._done:
            return True
        if self._working:
            self._show_working_error()
            return False
        if not self._collect_inputs():
            return False
        self._working = True
        self._set_form_disabled(True)
        self._run_setup()
        return False

    def _collect_inputs(self) -> bool:
        """Validate and store widget values before the worker starts."""
        return True

    def _finish(self, *, done: bool) -> None:
        self._working = False
        self._done = done
        self._set_form_disabled(False)

    def _run_setup(self) -> object:
        # Subclasses provide a @work(exclusive=True) override (whose return
        # type is the spawned Worker, hence `object` here). This stub only
        # fires when a subclass forgot to define one at all.
        raise NotImplementedError


# --- tailscale -----------------------------------------------------------------


class TailscaleSetupStep(_TunnelStepBase):
    """Detect tailscale, drive the login (URL + QR), enable serve or funnel."""

    def compose_body(self) -> ComposeResult:
        funnel = self.state.tailscale_exposure == "funnel"
        with Vertical(classes="radio-group"):
            yield CircleRadioButton(
                "Private to my devices (Tailscale Serve, recommended)",
                value=not funnel,
            )
            yield CircleRadioButton(
                "Public on the internet (Tailscale Funnel)", value=funnel
            )
        yield Static("", id="external-status")
        yield Static("", id="external-qr")

    def on_mount(self) -> None:
        buttons = list(self.query_one(".radio-group").query(CircleRadioButton))
        if buttons:
            next((b for b in buttons if b.value), buttons[0]).focus()
        self._detect()

    def _funnel_selected(self) -> bool:
        buttons = list(self.query_one(".radio-group").query(CircleRadioButton))
        return len(buttons) > 1 and bool(buttons[1].value)

    @work(exclusive=False)
    async def _detect(self) -> None:
        """Best-effort status hint on entry; Enter does the actual work."""
        binary = await asyncio.to_thread(find_tailscale)
        if binary is None:
            self.set_status(
                "tailscale is not installed. Install it, then Ctrl+R:\n"
                f"  {TAILSCALE_INSTALL_COMMAND}"
            )
            return
        status = await asyncio.to_thread(tailscale_status, binary)
        if status is not None and status.logged_in:
            self.set_status(
                "tailscale is installed and logged in. Press Enter to expose "
                "Nymeria and get your URL."
            )
        else:
            self.set_status(
                "tailscale is installed but not logged in. Press Enter to "
                "start the login (a link and QR code will appear)."
            )

    def _collect_inputs(self) -> bool:
        self.state.tailscale_exposure = (
            "funnel" if self._funnel_selected() else "serve"
        )
        return True

    @work(exclusive=True)
    async def _run_setup(self) -> None:
        try:
            await self._do_setup()
        except Exception as exc:  # noqa: BLE001 - a worker crash kills the app
            self.set_status(
                "[yellow]Tailscale setup hit an unexpected error:[/yellow] "
                f"{escape_markup(f'{type(exc).__name__}: {exc}')} "
                "Ctrl+R to retry."
            )
            self._finish(done=False)

    async def _do_setup(self) -> None:
        funnel = self.state.tailscale_exposure == "funnel"
        binary = await asyncio.to_thread(find_tailscale)
        if binary is None:
            self.set_status(
                "tailscale is not installed. Install it, then Ctrl+R:\n"
                f"  {TAILSCALE_INSTALL_COMMAND}\n"
                "(or Ctrl+S to skip and set it up later)"
            )
            self._finish(done=False)
            return

        status = await asyncio.to_thread(tailscale_status, binary)
        if status is None:
            self.set_status(
                "Could not talk to tailscale (is tailscaled running?). "
                "Start it and Ctrl+R."
            )
            self._finish(done=False)
            return

        if not status.logged_in:
            self.set_status("Starting the Tailscale login...")
            flow = await TailscaleLoginFlow.start(binary)
            try:
                url = await flow.wait_for_auth_url()
                if url:
                    qr = qr_ascii(url)
                    self.set_status(
                        f"Approve this device: {url}\n"
                        "Open the link (or scan the code) on any logged-in "
                        "device; this screen continues automatically."
                    )
                    if qr:
                        self.set_qr(qr)
                ok = await flow.wait_until_done(LOGIN_WAIT_SECONDS)
                self.set_qr("")
                if not ok:
                    detail = permission_hint(flow.error or "timed out")
                    self.set_status(
                        f"[yellow]Tailscale login did not complete:[/yellow] "
                        f"{escape_markup(detail)}. Ctrl+R to retry."
                    )
                    self._finish(done=False)
                    return
            finally:
                flow.terminate()
            status = await asyncio.to_thread(tailscale_status, binary)
            if status is None or not status.logged_in:
                self.set_status(
                    "[yellow]Login finished but tailscale still reports not "
                    "logged in.[/yellow] Ctrl+R to retry."
                )
                self._finish(done=False)
                return

        mode = "funnel (public)" if funnel else "serve (tailnet-only)"
        backend_port = self.state.resolved_api_port()
        self.set_status(f"Enabling tailscale {mode} for port {backend_port}...")
        ok, detail = await asyncio.to_thread(
            enable_tailscale_serve, binary, backend_port, funnel=funnel
        )
        if not ok:
            self.set_status(
                f"[yellow]tailscale {'funnel' if funnel else 'serve'} "
                f"failed:[/yellow] {escape_markup(detail)}\nCtrl+R to retry."
            )
            self._finish(done=False)
            return

        status = await asyncio.to_thread(tailscale_status, binary)
        url = status.https_url if status else None
        if not url:
            self.set_status(
                "[yellow]Exposure is on, but no tailnet hostname could be "
                "resolved (is MagicDNS enabled?).[/yellow] Ctrl+R to retry."
            )
            self._finish(done=False)
            return

        self.set_status(f"Checking {url} through the tailnet...")
        await _tunnel_level_check(self, url)
        self._finish(done=True)


def make_external_access_tailscale_step() -> Step:
    def build(wizard: "SetupWizardApp", number: int, total: int) -> TailscaleSetupStep:
        return TailscaleSetupStep(
            wizard,
            number,
            total,
            step_id="external_access_tailscale",
            title="Set up Tailscale",
            note=(
                "Serve gives Nymeria an automatic HTTPS URL reachable only "
                "from your Tailscale devices (install the Tailscale app on "
                "your phone/laptop and log in with the same account). Funnel "
                "makes the same URL public. Enter runs the setup; Ctrl+S "
                "skips it."
            ),
            hint="arrows move   space select   enter run setup   ctrl+r retry   ctrl+s skip   esc back",
        )

    return Step(
        id="external_access_tailscale",
        applies=_applies_tailscale,
        build=build,
    )


# --- cloudflare ------------------------------------------------------------------


class CloudflareSetupStep(_TunnelStepBase):
    """Drive a Cloudflare named tunnel over the API, or accept an existing URL."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._token = ""
        self._hostname = ""

    def compose_body(self) -> ComposeResult:
        with Vertical(classes="radio-group"):
            yield CircleRadioButton(
                "Set up a named tunnel with an API token (recommended)",
                value=True,
            )
            yield CircleRadioButton(
                "I already have a public URL (tunnel, Caddy, reverse proxy)",
                value=False,
            )
        yield Static(
            "API token (needs Account > Cloudflare Tunnel: Edit, "
            "Zone > Zone: Read, Zone > DNS: Edit)",
            classes="field-label",
        )
        yield Input(value="", placeholder="paste the token", password=True, id="cf-token")
        yield Static("Hostname for Nymeria (on a domain using Cloudflare DNS)", classes="field-label")
        yield Input(value="", placeholder="nymeria.example.com", id="cf-hostname")
        yield Static("Or the existing public URL", classes="field-label")
        yield Input(
            value=self.state.public_url,
            placeholder="https://nymeria.example.com",
            id="cf-url",
        )
        yield Static("", id="external-status")
        yield Static("", id="external-qr")

    def on_mount(self) -> None:
        buttons = list(self.query_one(".radio-group").query(CircleRadioButton))
        if buttons:
            buttons[0].focus()

    def _manual_selected(self) -> bool:
        buttons = list(self.query_one(".radio-group").query(CircleRadioButton))
        return len(buttons) > 1 and bool(buttons[1].value)

    def _retry_runs_worker(self) -> bool:
        # On the manual branch there is no API run to retry; running the
        # worker anyway would use stale/empty token+hostname and overwrite
        # the manually entered URL with the previous run's hostname.
        if self._manual_selected():
            self.show_error("")
            self.set_status(
                "Nothing to re-run for a manually entered URL; press Enter "
                "to continue."
            )
            return False
        return True

    def _collect_inputs(self) -> bool:
        if self._manual_selected():
            url = self.query_one("#cf-url", Input).value.strip()
            if not url.lower().startswith(("http://", "https://")):
                self.query_one("#cf-url", Input).focus()
                self.show_error("Enter the full public URL (https://...).")
                return False
            self.state.public_url = public_origin(url)
            self.state.public_url_verified = False
            self._done = True
            return True
        token = self.query_one("#cf-token", Input).value.strip()
        hostname = self.query_one("#cf-hostname", Input).value.strip().lower()
        # Tolerate a pasted URL, then insist on a bare hostname.
        if "://" in hostname:
            hostname = hostname.split("://", 1)[1]
        hostname = hostname.split("/", 1)[0].strip(".")
        if not token:
            self.query_one("#cf-token", Input).focus()
            self.show_error(
                "Paste a Cloudflare API token (create one at "
                "dash.cloudflare.com > My Profile > API Tokens with "
                "Account > Cloudflare Tunnel: Edit, Zone > Zone: Read, and "
                "Zone > DNS: Edit)."
            )
            return False
        if (
            not hostname
            or "." not in hostname
            or ":" in hostname
            or any(ch.isspace() for ch in hostname)
        ):
            self.query_one("#cf-hostname", Input).focus()
            self.show_error(
                "Enter a bare hostname (for example nymeria.example.com)."
            )
            return False
        self._token = token
        self._hostname = hostname
        return True

    def collect(self) -> bool:
        if self._working:
            # Must come before the manual short-circuit, or switching to the
            # manual branch mid-flight advances while the API worker keeps
            # running and later overwrites the manually entered URL.
            self._show_working_error()
            return False
        # The manual branch stores the URL synchronously and advances at once.
        if not self._done and self._manual_selected():
            return self._collect_inputs()
        return super().collect()

    @work(exclusive=True)
    async def _run_setup(self) -> None:
        token: str = self._token
        hostname: str = self._hostname
        client = CloudflareTunnelClient(token)
        try:
            self.set_status("Verifying the API token...")
            await client.verify_token()
            self.set_status(f"Finding the Cloudflare zone for {hostname}...")
            zone_id, zone_name, account_id = await client.find_zone(hostname)
            if not account_id:
                account_id = await client.find_account_id()
            self.set_status(f"Creating the tunnel (zone {zone_name})...")
            tunnel_id, tunnel_token = await client.get_or_create_tunnel(
                account_id, tunnel_name_for_hostname(hostname)
            )
            await client.put_ingress(
                account_id,
                tunnel_id,
                hostname,
                service=f"http://localhost:{self.state.resolved_api_port()}",
            )
            await client.ensure_dns_record(zone_id, hostname, tunnel_id)
            self.state.cloudflare_tunnel_token = tunnel_token

            from .. import finalize as finalize_mod
            from ...onboarding import HostingOption

            root = finalize_mod.resolve_runtime_root(
                self.state, for_docker=self.state.hosting is HostingOption.DOCKER
            )
            token_path = await asyncio.to_thread(
                persist_cloudflared_assets, root, tunnel_token
            )

            binary = await asyncio.to_thread(find_cloudflared)
            connector_note = ""
            if binary is None:
                connector_note = (
                    f"\n[yellow]cloudflared is not installed[/yellow]; install "
                    f"it ({CLOUDFLARED_DOWNLOAD_URL}) and run the command in "
                    f"{token_path.parent}/README.txt to bring the tunnel up."
                )
            else:
                self.set_status("Starting the cloudflared connector...")
                ok, detail = await asyncio.to_thread(
                    start_cloudflared_connector,
                    binary,
                    tunnel_token,
                    log_path=root / "cloudflared" / "cloudflared.log",
                )
                if ok:
                    connected = False
                    deadline = time.monotonic() + CONNECTOR_HEALTH_WAIT_SECONDS
                    while time.monotonic() < deadline:
                        try:
                            connected = await client.tunnel_healthy(
                                account_id, tunnel_id
                            )
                        except CloudflareError:
                            # Transient API hiccup mid-poll must not turn an
                            # already-provisioned setup into a failure report.
                            connected = False
                        if connected:
                            break
                        await asyncio.sleep(2.0)
                    if not connected:
                        connector_note = (
                            "\n[yellow]The connector started but Cloudflare "
                            "does not report the tunnel healthy yet; check "
                            f"{token_path.parent}/cloudflared.log if the URL "
                            "stays down.[/yellow]"
                        )
                else:
                    connector_note = f"\n[yellow]{escape_markup(detail)}[/yellow]"

            url = f"https://{hostname}"
            self.set_status(f"Checking {url} through Cloudflare...")
            await _tunnel_level_check(self, url)
            if connector_note:
                self.set_status(self._last_status + connector_note)
            self._finish(done=True)
        except CloudflareError as error:
            self.set_status(
                f"[yellow]Cloudflare setup failed:[/yellow] "
                f"{escape_markup(str(error))}\n"
                "Fix the token/hostname and Ctrl+R to retry (or Ctrl+S to skip)."
            )
            self._finish(done=False)
        except Exception as exc:  # noqa: BLE001 - a worker crash kills the app
            self.set_status(
                "[yellow]Cloudflare setup hit an unexpected error:[/yellow] "
                f"{escape_markup(f'{type(exc).__name__}: {exc}')} "
                "Ctrl+R to retry."
            )
            self._finish(done=False)
        finally:
            await client.aclose()


def make_external_access_cloudflare_step() -> Step:
    def build(wizard: "SetupWizardApp", number: int, total: int) -> CloudflareSetupStep:
        return CloudflareSetupStep(
            wizard,
            number,
            total,
            step_id="external_access_cloudflare",
            title="Set up a Cloudflare tunnel",
            note=(
                "A named tunnel gives Nymeria a stable public HTTPS hostname "
                "with no port forwarding; any browser reaches it with no "
                "extra apps. Needs a free Cloudflare account with your domain "
                "on Cloudflare DNS, and an API token. Quick tunnels "
                "(trycloudflare.com) are not offered: they cannot carry "
                "Nymeria's chat stream (no SSE support)."
            ),
            hint="arrows move   space select   enter run setup   ctrl+r retry   ctrl+s skip   esc back",
        )

    return Step(
        id="external_access_cloudflare",
        applies=_applies_cloudflare,
        build=build,
    )


__all__ = [
    "CloudflareSetupStep",
    "TailscaleSetupStep",
    "make_external_access_cloudflare_step",
    "make_external_access_tailscale_step",
]
