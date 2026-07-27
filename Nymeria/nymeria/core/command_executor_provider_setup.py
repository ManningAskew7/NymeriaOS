"""Provider setup + CLIProxy guidance command bodies (backlog #110).

House style: backend handler families live in their own domain mixin module
rather than being appended to ``command_service.py`` (see the sibling
``command_executor_llm.py``). ``_CommandExecutor`` inherits
:class:`ProviderSetupCommandsMixin`, which owns the chained
``/provider setup`` configure flow (step-rail tabbed forms, masked key
entry, test-first atomic apply; in-flight state in ``core.provider_setup``)
and the ``/provider cliproxy`` subscription-OAuth chain (target detail,
in-REPL OAuth login over the admin ``/cliproxy`` facade with server-
confirmed status, model pick, route apply). Handler methods are resolved
by ``CommandService.execute`` via ``getattr(executor, "_cmd_<path>")``.

Runtime leaf: imports the sibling leaf ``command_executor_llm`` (shared
provider constants) and ``command_forms``, never ``command_service``, so
there is no import cycle.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import shlex
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

import httpx

if TYPE_CHECKING:
    from ..cliproxy.catalog import CLIProxyProviderSpec
    from ..config.llm_providers import LLMProviderSpec
    from .provider_setup import PendingCliproxyLogin

from .command_executor_llm import OPENAI_API_MODES, _TIER_BADGES
from .command_forms import (
    CommandOutput,
    command_data,
    form_option,
    form_payload,
    form_tab,
    radio_field,
    search_field,
    text_field,
)

logger = logging.getLogger(__name__)


class ProviderSetupCommandsMixin:
    """Provider-setup command bodies mixed into ``_CommandExecutor``.

    The host provides ``api`` and ``user_id``; ``_unknown_provider_error``
    comes from the sibling :class:`LLMCommandsMixin` on the same host. The
    annotations and stubs below let the static checker see them on the
    mixin in isolation.
    """

    api: Any
    thread_id: str
    user_id: str
    actor: str
    is_admin: bool | None

    if TYPE_CHECKING:
        @staticmethod
        def _unknown_provider_error(provider: str) -> str: ...

    # ── CLIProxy subscription OAuth chain (backlog #110 phase 2) ──────────
    #
    # Same shape as the setup chain below: one registered path
    # (``provider cliproxy``) whose first argument is either a catalog
    # target id (start the chain at the target step) or a reserved step
    # token dispatched by the form the previous step returned. In-flight
    # state (the proxy's pending-session token, the auth URL, the cached
    # model list) lives in ``core.provider_setup``'s parallel store with
    # the same sliding 10-minute TTL, which also matches the proxy's own
    # OAuth session lifetime.
    #
    # INVARIANT (docs/private/cliproxy.md): a polled "ok" from the proxy
    # proves nothing (it answers ok for unknown/expired sessions), so
    # every completion check goes through the server-side confirmed
    # status (``cliproxy_oauth_status``), which verifies an active auth
    # file exists before reporting ok.

    _CLIPROXY_URL_SHAPES = {
        "root": "proxy root URL",
        "v1": "proxy /v1 URL",
    }
    _CLIPROXY_STEPS = frozenset(
        {
            "login",
            "use",
            "relogin",
            "restart",
            "paste",
            "check",
            "model",
            "apply",
            "cancel",
        }
    )
    _CLIPROXY_GONE = (
        "[Error]: No CLIProxy login is in progress (or it expired). "
        "Start one with /provider cliproxy <target>."
    )
    # Just under the proxy's ~10-minute OAuth session TTL: past this, a
    # polled ok with no delivered callback is treated as the stale-session
    # trap (see the check step).
    _CLIPROXY_SESSION_OK_GUARD_SECONDS = 540.0

    async def _cmd_provider_cliproxy(
        self, args: list[str], rest: str
    ) -> str | CommandOutput:
        """CLIProxy subscription OAuth: overview, login chain, route apply."""
        from ..cliproxy.catalog import get_cliproxy_provider, list_cliproxy_providers
        from . import provider_setup as setup_store

        if not args:
            # Resume an in-flight login at its current phase (the sibling
            # /provider setup bare-resume idiom: a lost form has a way
            # back); the overview otherwise.
            pending = setup_store.get_cliproxy_login(self.user_id)
            spec = (
                get_cliproxy_provider(pending.target)
                if pending is not None
                else None
            )
            if pending is not None and spec is not None:
                note = [
                    f"Resuming the in-flight {spec.label} login"
                    " (/provider cliproxy cancel to abandon it)."
                ]
                if pending.account or pending.model is not None:
                    return await self._cliproxy_model_chain(
                        pending, spec, note_lines=note
                    )
                if pending.oauth_state:
                    return self._cliproxy_login_rail(
                        pending, spec, note_lines=note
                    )
                return await self._cliproxy_target(spec)
            return await self._cliproxy_overview()
        token = args[0].strip().lower()
        if token in self._CLIPROXY_STEPS:
            return await self._cliproxy_step(token, args[1:], rest)
        spec = get_cliproxy_provider(token)
        if spec is None:
            ids = ", ".join(s.id for s in list_cliproxy_providers())
            return f"[Error]: Unknown CLIProxy target: {args[0]}. Known: {ids}."
        return await self._cliproxy_target(spec)

    async def _cliproxy_overview(self) -> str:
        """The catalog overview with logged-in badges from the auth files."""
        from ..cliproxy.catalog import list_cliproxy_providers
        from ..cliproxy.management_client import (
            active_login_entry,
            login_account_label,
        )

        url_set, key_set = await self._cliproxy_management_status()
        configured = url_set and key_set
        files: list[dict[str, Any]] | None = None
        probe_note = ""
        if configured:
            try:
                files = await self.api.cliproxy_auth_files(user_id=self.user_id)
            except Exception as error:  # noqa: BLE001 - badges degrade.
                probe_note = (
                    "Login state unavailable: "
                    + self._cliproxy_error_detail(error)
                )
        lines = ["CLIProxy subscription providers"]
        for spec in list_cliproxy_providers():
            badge = ""
            if files is not None:
                entry = active_login_entry(files, spec)
                if entry is not None:
                    account = login_account_label(entry)
                    badge = (
                        f"  [logged in: {account}]" if account else "  [logged in]"
                    )
            lines.append(f"  {spec.id:<12} {spec.label}{badge}")
        lines.append(
            "Management API: "
            + ("configured" if configured else "not configured")
        )
        if probe_note:
            lines.append(probe_note)
        if configured:
            lines.append("Log in or route: /provider cliproxy <target>")
        else:
            lines.append(self._cliproxy_unconfigured_next(url_set, key_set))
        return "[Info]: " + "\n".join(lines)

    @staticmethod
    def _cliproxy_unconfigured_next(url_set: bool, key_set: bool) -> str:
        missing = [
            name
            for name, is_set in (
                ("cliproxy_management_url", url_set),
                ("cliproxy_management_key", key_set),
            )
            if not is_set
        ]
        return (
            "Next: set "
            + " and ".join(missing)
            + " (/env set <name> <value>) to enable CLIProxy management,"
            " then rerun this command. See docs: configuration reference,"
            " CLIProxy section."
        )

    async def _cliproxy_management_status(self) -> tuple[bool, bool]:
        """(url_set, key_set) for the CLIProxy management pair, best effort."""
        try:
            data = await self.api.get_env_vars(user_id=self.user_id)
        except Exception:  # noqa: BLE001 - guidance degrades, never fails.
            return (False, False)
        url_set = key_set = False
        for entry in data.get("entries", []):
            if not isinstance(entry, Mapping):
                continue
            name = str(entry.get("name") or "")
            if name == "cliproxy_management_url":
                url_set = bool(entry.get("is_set"))
            elif name == "cliproxy_management_key":
                key_set = bool(entry.get("is_set"))
        return (url_set, key_set)

    async def _cliproxy_target(
        self, spec: "CLIProxyProviderSpec"
    ) -> str | CommandOutput:
        """The target step: detail brief + login-state-aware action form."""
        from ..cliproxy.management_client import (
            active_login_entry,
            login_account_label,
        )
        from . import provider_setup as setup_store

        account = ""
        logged_in = False
        unconfigured = False
        state_note = ""
        try:
            files = await self.api.cliproxy_auth_files(
                spec.id, user_id=self.user_id
            )
        except httpx.HTTPError as error:
            response = getattr(error, "response", None)
            if response is not None and response.status_code == 400:
                # Management not configured: brief + guidance, no chain.
                unconfigured = True
            else:
                state_note = (
                    "Login state unavailable: "
                    + self._cliproxy_error_detail(error)
                )
        else:
            entry = active_login_entry(files, spec)
            if entry is not None:
                logged_in = True
                account = login_account_label(entry)

        shape = self._CLIPROXY_URL_SHAPES.get(spec.url_shape, spec.url_shape)
        rows = [
            ("Routes as", spec.nymeria_provider + (
                f" ({spec.api_mode})" if spec.api_mode else ""
            )),
            ("Base URL", shape),
            ("Default model", spec.default_model or "-"),
        ]
        if logged_in:
            rows.append(("Logged in", account or "yes"))
        width = max(len(label) for label, _value in rows)
        lines = [f"CLIProxy: {spec.label}", f"  {spec.description}"]
        for label, value in rows:
            lines.append(f"  {label:<{width}}  {value}")
        if spec.tos_warning:
            lines.append(f"  Warning: {spec.tos_warning}")
        if state_note:
            lines.append(state_note)
        if spec.flow == "device":
            lines.append(
                "Device flow: approve the login on the provider's site; no"
                " callback URL to paste."
            )
        if unconfigured:
            url_set, key_set = await self._cliproxy_management_status()
            lines.append("Management API: not configured")
            lines.append(self._cliproxy_unconfigured_next(url_set, key_set))
            return "[Info]: " + "\n".join(lines)

        setup_store.start_cliproxy_login(self.user_id, spec.id)
        if account:
            setup_store.update_cliproxy_login(self.user_id, account=account)

        options: list[dict[str, Any]] = []
        if logged_in:
            options.append(
                form_option(
                    "use", label="Use the existing login", current=True
                )
            )
            options.append(form_option("relogin", label="Log in again"))
        else:
            options.append(
                form_option("login", label="Log in with OAuth", current=True)
            )
        options.append(form_option("cancel", label="Cancel"))
        lines.append(
            "Choose: /provider cliproxy "
            + " | ".join(str(option["id"]) for option in options)
        )
        tab = form_tab(
            spec.label,
            [radio_field("action", options)],
            submit_command="provider cliproxy {action}",
        )
        return self._chain_form_output(
            f"CLIProxy: {spec.label}",
            [tab],
            tab,
            lines,
            fallback_text=f"CLIProxy target {spec.label}.",
        )

    async def _cliproxy_step(
        self, token: str, args: list[str], rest: str
    ) -> str | CommandOutput:
        """Dispatch one reserved step token against the pending login."""
        from ..cliproxy.catalog import get_cliproxy_provider
        from . import provider_setup as setup_store

        if token == "cancel":
            cleared = setup_store.clear_cliproxy_login(self.user_id)
            return (
                "[Info]: CLIProxy login cancelled. Nothing was applied."
                if cleared
                else "[Info]: No CLIProxy login was in progress."
            )
        pending = setup_store.get_cliproxy_login(self.user_id)
        if pending is None:
            return self._CLIPROXY_GONE
        spec = get_cliproxy_provider(pending.target)
        if spec is None:
            setup_store.clear_cliproxy_login(self.user_id)
            return self._CLIPROXY_GONE

        if token in ("login", "relogin", "restart"):
            return await self._cliproxy_start_login(pending, spec)
        if token == "use":
            if not pending.account:
                # "use" is only offered when logged in, but it is typed-
                # reachable; applying a global route with no login would
                # break every turn, so re-verify.
                from ..cliproxy.management_client import (
                    active_login_entry,
                    login_account_label,
                )

                try:
                    files = await self.api.cliproxy_auth_files(
                        spec.id, user_id=self.user_id
                    )
                except httpx.HTTPError:
                    files = []
                entry = active_login_entry(files, spec)
                if entry is None:
                    return (
                        f"[Error]: Not logged in to {spec.label}."
                        " Use /provider cliproxy login first."
                    )
                setup_store.update_cliproxy_login(
                    self.user_id, account=login_account_label(entry)
                )
            return await self._cliproxy_model_chain(pending, spec)
        if token == "paste":
            return await self._cliproxy_paste(pending, spec, rest)
        if token == "check":
            return await self._cliproxy_check(pending, spec)
        if token == "model":
            value = self._setup_rest_value(rest)
            if not value:
                return "[Error]: Usage: /provider cliproxy model <model-id|custom>"
            if value.lower() == "custom":
                updated = setup_store.update_cliproxy_login(
                    self.user_id, model_custom=True
                )
            else:
                updated = setup_store.update_cliproxy_login(
                    self.user_id, model=value, model_custom=False
                )
            if updated is None:
                return self._CLIPROXY_GONE
            return await self._cliproxy_model_chain(pending, spec)
        if token == "apply":
            return await self._cliproxy_apply(pending, spec)
        return self._CLIPROXY_GONE

    async def _cliproxy_start_login(
        self, pending: "PendingCliproxyLogin", spec: "CLIProxyProviderSpec"
    ) -> str | CommandOutput:
        """Start (or restart) the OAuth session and render the login rail."""
        from . import provider_setup as setup_store

        try:
            started = await self.api.cliproxy_oauth_start(spec.id, user_id=self.user_id)
        except httpx.HTTPError as error:
            return (
                f"[Error]: Could not start the {spec.label} login: "
                + self._cliproxy_error_detail(error)
            )
        updated = setup_store.update_cliproxy_login(
            self.user_id,
            oauth_state=str(started.get("state") or ""),
            auth_url=str(started.get("url") or ""),
            flow=str(started.get("flow") or spec.flow),
            oauth_started_at=time.monotonic(),
            callback_delivered=False,
        )
        if updated is None:
            return self._CLIPROXY_GONE
        return self._cliproxy_login_rail(updated, spec)

    def _cliproxy_login_rail(
        self,
        pending: "PendingCliproxyLogin",
        spec: "CLIProxyProviderSpec",
        *,
        note_lines: list[str] | None = None,
    ) -> CommandOutput:
        """The login rail: full auth URL, remote guidance, paste + status."""
        lines = list(note_lines or [])
        lines.append(f"Log in to {spec.label}: open this URL in a browser.")
        lines.append(pending.auth_url or "(no auth URL; restart the login)")
        hint = self._cliproxy_tunnel_hint(pending.auth_url)
        if hint:
            lines.append(hint)
        device = (pending.flow or spec.flow) == "device"
        if device:
            lines.append(
                "Approve the login there, then: /provider cliproxy check"
            )
        else:
            lines.append(
                "After approving, the browser lands on a localhost URL (it"
                " may show a connection error; that is fine). Paste that full"
                " URL, or just the code, here."
            )
            lines.append("Type: /provider cliproxy paste <redirect-url|code>")
        lines.append("Also: /provider cliproxy check | restart | cancel")

        status_options = [
            form_option("check", label="Check login status", current=True),
            form_option("restart", label="Restart the login"),
            form_option("cancel", label="Cancel"),
        ]
        status_tab = form_tab(
            "Status",
            [radio_field("action", status_options)],
            submit_command="provider cliproxy {action}",
        )
        tabs: list[dict[str, Any]] = []
        if device:
            active_tab = status_tab
            tabs.append(status_tab)
        else:
            paste_tab = form_tab(
                "Paste",
                [
                    text_field(
                        "callback",
                        label="Redirect URL or code",
                        placeholder="http://localhost:.../callback?code=...",
                        # The value carries a single-use authorization code:
                        # masked entry keeps it out of CLI input history
                        # (the same reasoning that bot-excludes the command).
                        secret=True,
                    )
                ],
                submit_command="provider cliproxy paste {callback}",
            )
            tabs.extend([paste_tab, status_tab])
            active_tab = paste_tab
        return self._chain_form_output(
            f"CLIProxy login: {spec.label}",
            tabs,
            active_tab,
            lines,
            fallback_text=f"Logging in to {spec.label}.",
        )

    async def _cliproxy_paste(
        self,
        pending: "PendingCliproxyLogin",
        spec: "CLIProxyProviderSpec",
        rest: str,
    ) -> str | CommandOutput:
        """Deliver the pasted callback, then briefly poll for confirmation.

        Accepts what a user plausibly pastes (the hermes idiom): the full
        redirect URL, a bare ``?code=...&state=...`` query, or just the
        code (the stored session state fills the absent one).
        """
        from . import provider_setup as setup_store

        if (pending.flow or spec.flow) == "device":
            return self._cliproxy_login_rail(
                pending,
                spec,
                note_lines=[
                    "This login has no paste step; approve it in the browser"
                    " and check the status."
                ],
            )
        if not pending.oauth_state:
            return "[Error]: Start the login first: /provider cliproxy login"
        value, hygiene = setup_store.clean_pasted_secret(
            self._setup_rest_value(rest)
        )
        if not value:
            return self._cliproxy_login_rail(
                pending,
                spec,
                note_lines=[
                    "The pasted value was empty after cleanup. Paste it again."
                ],
            )
        notes = [f"Warning: {warning}" for warning in hygiene]
        redirect_url: str | None = None
        code: str | None = None
        state: str | None = None
        if "://" in value:
            redirect_url = value
            pasted_state = self._cliproxy_pasted_state(value)
        elif "code=" in value:
            # A query string (with or without a path prefix). A bare "=" is
            # NOT enough to mean query: base64ish codes end in "=" padding.
            query = value.split("?", 1)[1] if "?" in value else value
            parsed = parse_qs(query)
            code = str((parsed.get("code") or [""])[0])
            pasted_state = str((parsed.get("state") or [""])[0])
            state = pasted_state or pending.oauth_state
            if not code:
                return self._cliproxy_login_rail(
                    pending,
                    spec,
                    note_lines=notes
                    + [
                        "No code= found in the pasted value. Paste the full"
                        " redirect URL instead."
                    ],
                )
        else:
            code = value
            state = pending.oauth_state
            pasted_state = ""
        if pasted_state and pasted_state != pending.oauth_state:
            # A stale callback (an earlier login attempt's URL) would be
            # delivered to the OTHER session while this chain polls its own
            # state forever; refuse honestly instead.
            return self._cliproxy_login_rail(
                pending,
                spec,
                note_lines=notes
                + [
                    "The pasted callback belongs to a different login"
                    " session (state mismatch); it is probably from an"
                    " earlier attempt. Use the newest URL, or restart the"
                    " login.",
                ],
            )
        try:
            await self.api.cliproxy_oauth_callback(
                spec.id,
                redirect_url=redirect_url,
                code=code,
                state=state,
                user_id=self.user_id,
            )
        except httpx.HTTPError as error:
            return self._cliproxy_login_rail(
                pending,
                spec,
                note_lines=notes
                + [
                    "Callback delivery failed: "
                    + self._cliproxy_error_detail(error),
                    "Paste again, or restart the login.",
                ],
            )
        setup_store.update_cliproxy_login(self.user_id, callback_delivered=True)
        # Delivered; give the proxy a moment to persist the auth file,
        # then confirm (the status is server-confirmed against the
        # auth-file list, never the bare poll).
        for attempt in range(2):
            status, detail = await self._cliproxy_poll_once(pending, spec)
            if status == "ok":
                return await self._cliproxy_confirmed(pending, spec, detail)
            if status == "error":
                return self._cliproxy_login_rail(
                    pending,
                    spec,
                    note_lines=notes
                    + [
                        f"Login failed: {detail}" if detail else "Login failed.",
                        "Restart the login to try again.",
                    ],
                )
            if status == "unreachable":
                return self._cliproxy_login_rail(
                    pending,
                    spec,
                    note_lines=notes
                    + [
                        f"Could not check the login status: {detail}",
                        "Try: /provider cliproxy check",
                    ],
                )
            if attempt == 0:
                await asyncio.sleep(1)
        return self._cliproxy_login_rail(
            pending,
            spec,
            note_lines=notes
            + [
                "Callback delivered; the proxy has not confirmed the login"
                " yet. Check the status in a moment."
            ],
        )

    async def _cliproxy_check(
        self, pending: "PendingCliproxyLogin", spec: "CLIProxyProviderSpec"
    ) -> str | CommandOutput:
        """One server-confirmed status poll."""
        if not pending.oauth_state:
            return "[Error]: Start the login first: /provider cliproxy login"
        status, detail = await self._cliproxy_poll_once(pending, spec)
        if status == "ok":
            session_age = time.monotonic() - (pending.oauth_started_at or 0.0)
            if (
                not pending.callback_delivered
                and session_age > self._CLIPROXY_SESSION_OK_GUARD_SECONDS
            ):
                # The proxy answers ok for a session it no longer knows
                # (expired), and confirm-on-ok would then bless a PRE-
                # EXISTING auth file (the relogin trap). Within the
                # session's lifetime an unknown-session ok cannot happen
                # for our state, and a pasted callback that landed proves
                # the session was alive, so only an OLD, paste-less ok is
                # refused.
                return self._cliproxy_login_rail(
                    pending,
                    spec,
                    note_lines=[
                        "The proxy answered ok, but this login session is"
                        " old enough to have expired, so that is likely a"
                        " stale-session answer against an older login."
                        " Restart the login to be sure.",
                    ],
                )
            return await self._cliproxy_confirmed(pending, spec, detail)
        if status == "wait":
            return self._cliproxy_login_rail(
                pending,
                spec,
                note_lines=["Still waiting for the login to complete."],
            )
        if status == "unreachable":
            return self._cliproxy_login_rail(
                pending,
                spec,
                note_lines=[
                    f"Could not check the login status: {detail}",
                    "Try again in a moment.",
                ],
            )
        return self._cliproxy_login_rail(
            pending,
            spec,
            note_lines=[
                f"Login failed: {detail}" if detail else "Login failed.",
                "Restart the login to try again.",
            ],
        )

    async def _cliproxy_poll_once(
        self, pending: "PendingCliproxyLogin", spec: "CLIProxyProviderSpec"
    ) -> tuple[str, str]:
        """(status, detail): ok/wait/error from the server-confirmed route,
        or "unreachable" when the status call itself failed."""
        try:
            payload = await self.api.cliproxy_oauth_status(
                pending.oauth_state, spec.id, user_id=self.user_id
            )
        except httpx.HTTPError as error:
            # HTTPError (not just HTTPStatusError): against a remote
            # backend a connection failure raises ConnectError/ReadTimeout,
            # which must land in this branch, not kill the chain form.
            return "unreachable", self._cliproxy_error_detail(error)
        status = str(payload.get("status") or "wait")
        if status not in ("ok", "wait", "error"):
            status = "wait"
        return status, str(payload.get("detail") or "")

    async def _cliproxy_confirmed(
        self,
        pending: "PendingCliproxyLogin",
        spec: "CLIProxyProviderSpec",
        account: str,
    ) -> str | CommandOutput:
        """A confirmed login: record the account, move to the model step."""
        from . import provider_setup as setup_store

        updated = setup_store.update_cliproxy_login(
            self.user_id, account=account or pending.account
        )
        if updated is None:
            return self._CLIPROXY_GONE
        note = f"Logged in to {spec.label}" + (
            f" as {account}." if account else "."
        )
        return await self._cliproxy_model_chain(
            updated, spec, note_lines=[note]
        )

    async def _cliproxy_model_chain(
        self,
        pending: "PendingCliproxyLogin",
        spec: "CLIProxyProviderSpec",
        *,
        note_lines: list[str] | None = None,
    ) -> CommandOutput:
        """The post-login rail: Model tab, then Apply once a model is set."""
        chain = [await self._cliproxy_model_tab(pending, spec)]
        if chain[-1][1]:
            chain.append(self._cliproxy_apply_tab(pending, spec))
        active_tab, _decided, guidance = chain[-1]
        tabs = [tab for tab, _d, _l in chain]
        return self._chain_form_output(
            f"CLIProxy route: {spec.label}",
            tabs,
            active_tab,
            list(note_lines or []) + guidance,
            fallback_text=f"Choose a model for {spec.label}.",
        )

    async def _cliproxy_model_tab(
        self, pending: "PendingCliproxyLogin", spec: "CLIProxyProviderSpec"
    ) -> tuple[dict[str, Any], bool, list[str]]:
        """The model step: live proxy list cached on the record, degrading
        to the spec default; custom escape hatch."""
        from . import provider_setup as setup_store

        if pending.model_custom:
            tab = form_tab(
                "Model",
                [
                    text_field(
                        "model",
                        label="Model id",
                        placeholder=spec.default_model or "model-id",
                    )
                ],
                submit_command="provider cliproxy model {model}",
            )
            return tab, False, [
                "Type the model id to use.",
                "Type: /provider cliproxy model <model-id>",
            ]

        if pending.model_options is None:
            try:
                models = await self.api.cliproxy_models(user_id=self.user_id)
            except Exception:  # noqa: BLE001 - the list degrades, never blocks.
                logger.debug("cliproxy chain: model list failed", exc_info=True)
                models = []
            raw: list[dict[str, Any]] = []
            for entry in models or []:
                model_id = str(entry.get("id") or "")
                if not model_id:
                    continue
                raw.append(
                    {"id": model_id, "meta": str(entry.get("owned_by") or "")}
                )
            note = (
                f"{len(raw)} models listed from the proxy (all logged-in"
                " providers)."
                if raw
                else (
                    "Model list unavailable from the proxy; showing the"
                    " known default."
                )
            )
            # An empty list is never cached (None keeps the next render
            # retrying): the proxy may just be settling after the login.
            setup_store.update_cliproxy_login(
                self.user_id, model_options=raw or None, models_note=note
            )

        preselect = str(pending.model or "") or str(spec.default_model or "")
        options: list[dict[str, Any]] = []
        for entry in pending.model_options or []:
            model_id = str(entry.get("id") or "")
            options.append(
                form_option(
                    model_id,
                    meta=str(entry.get("meta") or ""),
                    current=model_id == preselect,
                )
            )
        if not options and spec.default_model:
            options.append(
                form_option(
                    spec.default_model,
                    meta="spec default",
                    current=spec.default_model == preselect,
                )
            )
        if preselect and all(option["id"] != preselect for option in options):
            insert_meta = (
                "spec default"
                if preselect == str(spec.default_model or "")
                else "custom"
            )
            options.insert(
                0, form_option(preselect, meta=insert_meta, current=True)
            )
        options.append(form_option("custom", label="Custom model id…"))
        tab = form_tab(
            "Model",
            [
                search_field("filter", placeholder="Filter models…"),
                radio_field("model", options),
            ],
            submit_command="provider cliproxy model {model}",
        )
        return tab, pending.model is not None, [
            str(pending.models_note or ""),
            "Choose: /provider cliproxy model <model-id>|custom",
        ]

    def _cliproxy_apply_tab(
        self, pending: "PendingCliproxyLogin", spec: "CLIProxyProviderSpec"
    ) -> tuple[dict[str, Any], bool, list[str]]:
        shape = self._CLIPROXY_URL_SHAPES.get(spec.url_shape, spec.url_shape)
        rows = [
            ("Target", spec.label),
            ("Routes as", spec.nymeria_provider + (
                f" ({spec.api_mode})" if spec.api_mode else ""
            )),
            ("Base URL", shape),
            ("Model", pending.model or "?"),
        ]
        if pending.account:
            rows.append(("Account", pending.account))
        width = max(len(label) for label, _value in rows)
        lines = ["Review the CLIProxy route (applies globally)"]
        for label, value in rows:
            lines.append(f"  {label:<{width}}  {value}")
        lines.append("Choose: /provider cliproxy apply | cancel")
        options = [
            form_option("apply", label="Apply the route", current=True),
            form_option("cancel", label="Cancel"),
        ]
        tab = form_tab(
            "Apply",
            [radio_field("action", options)],
            submit_command="provider cliproxy {action}",
        )
        # Apply is never "decided": it terminates the chain, last and active.
        return tab, False, lines

    async def _cliproxy_apply(
        self, pending: "PendingCliproxyLogin", spec: "CLIProxyProviderSpec"
    ) -> str | CommandOutput:
        """Apply the route globally (gatekeeper resolved server-side)."""
        from . import provider_setup as setup_store

        model = str(pending.model or "").strip()
        if not model:
            # Reached apply without a decided model (stale form): re-chain.
            return await self._cliproxy_model_chain(pending, spec)
        try:
            result = await self.api.cliproxy_apply_route(
                spec.id, model, user_id=self.user_id
            )
        except httpx.HTTPError as error:
            return await self._cliproxy_model_chain(
                pending,
                spec,
                note_lines=[
                    "Apply failed: " + self._cliproxy_error_detail(error)
                ],
            )
        setup_store.clear_cliproxy_login(self.user_id)
        provider = str(result.get("provider") or spec.nymeria_provider)
        base_url = str(result.get("base_url") or "")
        api_mode = str(result.get("api_mode") or "")
        message = f"CLIProxy route applied: provider {provider}, model {model}"
        if base_url:
            message += f", base URL {base_url}"
        if api_mode:
            message += f", API mode {api_mode}"
        message += "."
        if result.get("restart_required"):
            message += " Restart required for some changes."
        message += " Verify with /provider test."
        return f"[Success]: {message}"

    @staticmethod
    def _cliproxy_error_detail(error: Exception) -> str:
        """Concise user-facing detail from a facade error.

        Delegates to ``command_service.http_error_detail`` via a call-time
        import (module-scope would be a cycle: command_service imports this
        leaf; by call time it is long since initialized).
        """
        if isinstance(error, httpx.HTTPStatusError):
            from .command_service import http_error_detail

            return http_error_detail(error)
        return str(error)

    @staticmethod
    def _cliproxy_pasted_state(redirect_url: str) -> str:
        """The state parameter inside a pasted redirect URL ("" when absent)."""
        try:
            query = parse_qs(urlparse(redirect_url).query)
        except ValueError:
            return ""
        return str((query.get("state") or [""])[0])

    @staticmethod
    def _cliproxy_tunnel_hint(auth_url: str) -> str:
        """A ready-to-adapt SSH forward for a browser on another machine,
        derived from the redirect_uri port inside the auth URL ("" when no
        local callback port is present, e.g. device flows)."""
        try:
            query = parse_qs(urlparse(auth_url).query)
            redirect = str((query.get("redirect_uri") or [""])[0])
            port = urlparse(redirect).port
        except ValueError:
            return ""
        if not port:
            return ""
        return (
            "Browser on another machine? Forward the callback first: "
            f"ssh -N -L {port}:127.0.0.1:{port} <user>@<this-backend-host>"
            " then open the URL there. (Or open it anywhere and paste the"
            " final redirect URL below.)"
        )

    # ── Provider setup flow (chained configure forms, backlog #110) ───────
    #
    # One registered path (``provider setup``) serves the whole chain: the
    # first argument is either a provider id (start/restart) or a reserved
    # step token dispatched by the form the previous step returned. Every
    # response re-renders the chain as ONE tabbed form: each step reached so
    # far is a tab (API key, API mode, Base URL, Model, Review) and the
    # first undecided step carries the contract's ``active`` flag, so in a
    # rich client the tab bar is a step rail and arrowing left revisits an
    # earlier decision. Form-less frontends read the same flow as guided
    # markdown. In-flight state, including the pasted key (which must NEVER
    # ride a form payload), lives server-side in ``core.provider_setup``
    # with a 10-minute TTL.

    _SETUP_STEPS = frozenset(
        {
            "key",
            "keep",
            "replace",
            "clear",
            "mode",
            "baseurl",
            "model",
            "apply",
            "notest",
            "cancel",
        }
    )
    _SETUP_GONE = (
        "[Error]: No provider setup is in progress (or it expired). "
        "Start one with /provider setup <provider>."
    )

    async def _cmd_provider_setup(
        self, args: list[str], rest: str
    ) -> str | CommandOutput:
        """Guided provider configuration: key -> connection -> model -> apply."""
        from ..config.llm_providers import get_llm_provider_spec
        from . import provider_setup as setup_store

        if not args:
            pending = setup_store.get_setup(self.user_id)
            if pending is None:
                return (
                    "[Error]: Usage: /provider setup <provider> "
                    "(see /provider list for the registered providers)."
                )
            spec = get_llm_provider_spec(pending.provider)
            if spec is None:
                setup_store.clear_setup(self.user_id)
                return self._unknown_provider_error(pending.provider)
            # Resume: re-render the chain at its first undecided step.
            settings = await self.api.get_settings()
            return await self._setup_chain(pending, spec, settings)

        token = args[0].strip().lower()
        if token in self._SETUP_STEPS:
            return await self._setup_step(token, args[1:], rest)
        spec = get_llm_provider_spec(args[0])
        if spec is None:
            return self._unknown_provider_error(args[0])
        return await self._setup_start(spec)

    async def _setup_start(self, spec: "LLMProviderSpec") -> str | CommandOutput:
        """Start (or restart) the chain: provider brief, then the step rail."""
        from . import provider_setup as setup_store

        pending = setup_store.start_setup(self.user_id, spec.id)
        existing = await self._setup_existing_key_fields(spec)
        setup_store.update_setup(
            self.user_id,
            server_key_envs=tuple(env_var for env_var, _field in existing),
            server_key_fields=self._setup_clearable_fields(existing),
        )
        settings = await self.api.get_settings()
        lines = [f"Configure {spec.label} {_TIER_BADGES.get(spec.tier, '')}".rstrip()]
        if spec.notes_for_user:
            lines.append(f"Note: {spec.notes_for_user}")
        if spec.signup_url:
            lines.append(f"Get a key: {spec.signup_url}")
        if spec.signup_guidance:
            lines.append(spec.signup_guidance)
        if not spec.requires_api_key:
            setup_store.update_setup(self.user_id, key_choice="none")
            lines.append("No API key is required for this provider.")
        return await self._setup_chain(pending, spec, settings, note_lines=lines)

    def _setup_update(self, **fields: Any) -> bool:
        """Apply pending-setup fields; False when the record is gone.

        The store's TTL is sliding, so a mid-step expiry is rare, but a
        silent drop would render a chain form that contradicts the (empty)
        server state; callers return ``_SETUP_GONE`` on False instead.
        """
        from . import provider_setup as setup_store

        return setup_store.update_setup(self.user_id, **fields) is not None

    async def _setup_step(
        self, token: str, args: list[str], rest: str
    ) -> str | CommandOutput:
        """Dispatch one reserved step token against the pending setup."""
        from ..config.llm_providers import get_llm_provider_spec
        from . import provider_setup as setup_store

        if token == "cancel":
            cleared = setup_store.clear_setup(self.user_id)
            return (
                "[Info]: Provider setup cancelled. Nothing was saved."
                if cleared
                else "[Info]: No provider setup was in progress."
            )
        pending = setup_store.get_setup(self.user_id)
        if pending is None:
            return self._SETUP_GONE
        spec = get_llm_provider_spec(pending.provider)
        if spec is None:
            setup_store.clear_setup(self.user_id)
            return self._unknown_provider_error(pending.provider)
        settings = await self.api.get_settings()

        if token == "keep":
            if pending.key_choice == "paste" and pending.api_key:
                # Keeping the already-pasted key: only leave entry mode.
                updated = self._setup_update(key_entry=False)
            else:
                updated = self._setup_update(
                    key_choice="keep", api_key="", key_entry=False
                )
            if not updated:
                return self._SETUP_GONE
            return await self._setup_chain(pending, spec, settings)
        if token == "replace":
            if not self._setup_update(key_entry=True):
                return self._SETUP_GONE
            return await self._setup_chain(pending, spec, settings)
        if token == "clear":
            if not self._setup_update(
                key_choice="clear", api_key="", key_entry=False
            ):
                return self._SETUP_GONE
            lines = ["The stored key will be cleared when you apply."]
            if spec.requires_api_key:
                lines.append(
                    "Warning: this provider requires an API key; it will stop"
                    " working after apply until a new one is set."
                )
            return await self._setup_chain(pending, spec, settings, note_lines=lines)
        if token == "key":
            return await self._setup_take_key(pending, spec, settings, rest)
        if token == "mode":
            value = (args[0] if args else "").strip().lower()
            if value not in OPENAI_API_MODES:
                return (
                    "[Error]: Usage: /provider setup mode"
                    " <responses|chat_completions>"
                )
            if not self._setup_update(api_mode=value):
                return self._SETUP_GONE
            return await self._setup_chain(pending, spec, settings)
        if token == "baseurl":
            return await self._setup_take_base_url(pending, spec, settings, rest)
        if token == "model":
            value = self._setup_rest_value(rest)
            if not value:
                return "[Error]: Usage: /provider setup model <model-id|custom>"
            if value.lower() == "custom":
                updated = self._setup_update(model_custom=True)
            else:
                updated = self._setup_update(model=value, model_custom=False)
            if not updated:
                return self._SETUP_GONE
            return await self._setup_chain(pending, spec, settings)
        if token == "apply":
            # Typed path: "/provider setup apply notest" also skips the test.
            notest = bool(args) and args[0].strip().lower() == "notest"
            return await self._setup_apply(pending, spec, settings, notest=notest)
        if token == "notest":
            # Form path: option ids must be single tokens (the client
            # shell-quotes multi-word values into one backend token).
            return await self._setup_apply(pending, spec, settings, notest=True)
        return self._SETUP_GONE

    async def _setup_take_key(
        self,
        pending: Any,
        spec: "LLMProviderSpec",
        settings: Mapping[str, Any],
        rest: str,
    ) -> str | CommandOutput:
        """Store a pasted key after hygiene, then re-render the chain."""
        from ..setup.providers import valid_key_format_for_spec
        from . import provider_setup as setup_store

        value, hygiene = setup_store.clean_pasted_secret(
            self._setup_rest_value(rest)
        )
        if not value:
            if not self._setup_update(key_entry=True):
                return self._SETUP_GONE
            return await self._setup_chain(
                pending,
                spec,
                settings,
                note_lines=[
                    "The pasted key was empty after cleanup. Paste it again."
                ],
            )
        lines: list[str] = []
        ok, prefix = valid_key_format_for_spec(spec, value)
        if not ok and prefix:
            lines.append(
                f"Warning: {spec.label} keys usually start with `{prefix}`;"
                " continuing anyway (the model list and the apply test"
                " validate it for real)."
            )
        lines.extend(f"Warning: {warning}" for warning in hygiene)
        if not self._setup_update(
            api_key=value, key_choice="paste", key_entry=False
        ):
            return self._SETUP_GONE
        return await self._setup_chain(pending, spec, settings, note_lines=lines)

    async def _setup_take_base_url(
        self,
        pending: Any,
        spec: "LLMProviderSpec",
        settings: Mapping[str, Any],
        rest: str,
    ) -> str | CommandOutput:
        value = self._setup_rest_value(rest)
        lowered = value.lower()
        if not value:
            return "[Error]: Usage: /provider setup baseurl <url|default|custom>"
        if lowered == "custom":
            if not self._setup_update(base_url_custom=True):
                return self._SETUP_GONE
            return await self._setup_chain(pending, spec, settings)
        if lowered == "default":
            if not self._setup_update(base_url="", base_url_custom=False):
                return self._SETUP_GONE
            return await self._setup_chain(pending, spec, settings)
        if not lowered.startswith(("http://", "https://")):
            if not self._setup_update(base_url_custom=True):
                return self._SETUP_GONE
            return await self._setup_chain(
                pending,
                spec,
                settings,
                note_lines=[
                    "That does not look like a URL (expected http:// or"
                    " https://). Try again."
                ],
            )
        if not self._setup_update(
            base_url=value.rstrip("/"), base_url_custom=False
        ):
            return self._SETUP_GONE
        return await self._setup_chain(pending, spec, settings)

    # -- the chain form (step rail) ----------------------------------------

    async def _setup_chain(
        self,
        pending: Any,
        spec: "LLMProviderSpec",
        settings: Mapping[str, Any],
        *,
        note_lines: list[str] | None = None,
    ) -> CommandOutput:
        """Render the chain as one tabbed form.

        Steps are appended in order until the first undecided one (tabs grow
        as the user advances); that step carries the contract's ``active``
        flag and contributes the guidance markdown. When every step is
        decided the Review tab (always "undecided") lands last and active,
        with the earlier tabs still present for revision.
        """
        steps: list[tuple[bool, Any]] = [
            (spec.requires_api_key, lambda: self._setup_key_tab(pending, spec)),
            (
                spec.supports_responses,
                lambda: self._setup_mode_tab(pending, spec, settings),
            ),
            (
                self._setup_needs_base_url(spec, settings),
                lambda: self._setup_base_url_tab(pending, spec, settings),
            ),
            (True, lambda: self._setup_model_tab(pending, spec, settings)),
            (True, lambda: self._setup_review_tab(pending, spec)),
        ]
        chain: list[tuple[dict[str, Any], bool, list[str]]] = []
        for applies, build in steps:
            if not applies:
                continue
            entry = build()
            if inspect.isawaitable(entry):
                entry = await entry
            chain.append(entry)
            if not entry[1]:
                # First undecided step: the rail stops growing here (Review
                # is never "decided", so it terminates a fully decided
                # chain as the last, active tab).
                break

        active_tab, _decided, guidance = chain[-1]
        tabs = [tab for tab, _d, _l in chain]
        return self._chain_form_output(
            f"Setup: {spec.label}",
            tabs,
            active_tab,
            list(note_lines or []) + guidance,
            fallback_text=f"Configuring {spec.label}.",
        )

    @staticmethod
    def _chain_footer(active_tab: dict[str, Any], tab_count: int) -> str:
        """Word the Enter action for the active tab's field kind: a text tab
        submits what was typed, a radio tab applies the selection."""
        enter_word = (
            "Enter submit"
            if any(
                field.get("kind") == "text"
                for field in active_tab.get("fields") or []
            )
            else "Enter apply"
        )
        return (
            f"←→ step · {enter_word} · Esc close"
            if tab_count > 1
            else f"{enter_word} · Esc close"
        )

    def _chain_form_output(
        self,
        title: str,
        tabs: list[dict[str, Any]],
        active_tab: dict[str, Any],
        lines: list[str],
        *,
        fallback_text: str,
    ) -> CommandOutput:
        """Render one chain response: tabs, one active, guidance markdown."""
        active_tab["active"] = True
        submit = active_tab.get("submit") or {}
        form = form_payload(
            title,
            tabs,
            submit_command=str(submit.get("command") or ""),
            footer_hint=self._chain_footer(active_tab, len(tabs)),
        )
        text = "\n".join(line for line in lines if line) or fallback_text
        return CommandOutput("[Info]: " + text, data=command_data(form=form))

    def _setup_key_tab(
        self, pending: Any, spec: "LLMProviderSpec"
    ) -> tuple[dict[str, Any], bool, list[str]]:
        """The API key step: masked entry, or Keep/Replace/Clear when there
        is something to keep (the hermes reconfigure idiom)."""
        server_envs = tuple(pending.server_key_envs or ())
        has_pasted = pending.key_choice == "paste" and bool(pending.api_key)
        entry_mode = pending.key_entry or (
            not has_pasted
            and not server_envs
            and pending.key_choice not in ("keep", "clear")
        )
        if entry_mode:
            from ..setup.providers import key_prefix_for_spec

            prefix = key_prefix_for_spec(spec)
            placeholder = (
                f"{prefix}..." if prefix else f"paste your {spec.label} API key"
            )
            tab = form_tab(
                "API key",
                [
                    text_field(
                        "api_key",
                        label="API key",
                        placeholder=placeholder,
                        secret=True,
                    )
                ],
                submit_command="provider setup key {api_key}",
            )
            return tab, False, [
                "Paste the API key (input is masked).",
                "Type: /provider setup key <api-key>",
            ]

        options: list[dict[str, Any]] = []
        if has_pasted:
            options.append(
                form_option("keep", label="Keep the pasted key", current=True)
            )
        elif server_envs:
            options.append(
                form_option(
                    "keep",
                    label="Keep the existing key",
                    meta=", ".join(server_envs),
                    current=pending.key_choice in ("", "keep"),
                )
            )
        options.append(form_option("replace", label="Paste a new key"))
        if tuple(pending.server_key_fields or ()):
            # Clearing writes ""-patches to the provider's key settings
            # fields; only offered when at least one such field is
            # patchable (see _setup_clearable_fields).
            options.append(
                form_option(
                    "clear",
                    label="Clear the stored key",
                    meta=(
                        "provider requires a key"
                        if spec.requires_api_key
                        else ""
                    ),
                    current=pending.key_choice == "clear",
                )
            )
        tab = form_tab(
            "API key",
            [radio_field("key_choice", options)],
            submit_command="provider setup {key_choice}",
        )
        decided = pending.key_choice in ("paste", "keep", "clear", "none")
        lines = (
            [f"A server API key is already configured ({', '.join(server_envs)})."]
            if server_envs
            else []
        )
        lines.append(
            "Choose: /provider setup "
            + " | ".join(str(option["id"]) for option in options)
        )
        return tab, decided, lines

    def _setup_mode_tab(
        self,
        pending: Any,
        spec: "LLMProviderSpec",
        settings: Mapping[str, Any],
    ) -> tuple[dict[str, Any], bool, list[str]]:
        current = (
            pending.api_mode
            or str(settings.get("openai_api_mode", "") or "").strip().lower()
        )
        if current not in OPENAI_API_MODES:
            current = str(spec.default_api_mode or "chat_completions")
        options = [
            form_option(
                "responses",
                label="Responses API",
                meta="stateful; richer reasoning passback",
                current=current == "responses",
            ),
            form_option(
                "chat_completions",
                label="Chat Completions",
                meta="widest gateway compatibility",
                current=current == "chat_completions",
            ),
        ]
        tab = form_tab(
            "API mode",
            [radio_field("api_mode", options)],
            submit_command="provider setup mode {api_mode}",
        )
        return (
            tab,
            pending.api_mode is not None,
            [
                "Choose the OpenAI-compatible API mode.",
                "Type: /provider setup mode responses|chat_completions",
            ],
        )

    def _setup_base_url_tab(
        self,
        pending: Any,
        spec: "LLMProviderSpec",
        settings: Mapping[str, Any],
    ) -> tuple[dict[str, Any], bool, list[str]]:
        if pending.base_url_custom:
            tab = form_tab(
                "Base URL",
                [
                    text_field(
                        "base_url",
                        label="Base URL",
                        placeholder=spec.default_base_url
                        or "https://host[:port]/v1",
                    )
                ],
                submit_command="provider setup baseurl {base_url}",
            )
            return tab, False, [
                "Type the base URL to use.",
                "Type: /provider setup baseurl <url>",
            ]

        current = str(settings.get("llm_base_url", "") or "").strip()
        decided = pending.base_url is not None
        options: list[dict[str, Any]] = []
        seen: set[str] = set()
        if current:
            options.append(
                form_option(
                    current,
                    label=f"Keep current ({current})",
                    current=pending.base_url == current or not decided,
                )
            )
            seen.add(current)
        if pending.base_url and pending.base_url not in seen:
            options.append(
                form_option(
                    pending.base_url,
                    label=f"Custom ({pending.base_url})",
                    current=True,
                )
            )
            seen.add(pending.base_url)
        options.append(
            form_option(
                "default",
                label="Provider default",
                meta=spec.default_base_url or "SDK default",
                current=pending.base_url == "" or (not decided and not current),
            )
        )
        options.append(form_option("custom", label="Custom URL…"))
        tab = form_tab(
            "Base URL",
            [radio_field("base_url_choice", options)],
            submit_command="provider setup baseurl {base_url_choice}",
        )
        return tab, decided, [
            "Choose the API base URL.",
            "Type: /provider setup baseurl <url>|default|custom",
        ]

    async def _setup_model_tab(
        self,
        pending: Any,
        spec: "LLMProviderSpec",
        settings: Mapping[str, Any],
    ) -> tuple[dict[str, Any], bool, list[str]]:
        """The model step: live list using the PENDING credentials (the
        hermes trick: a successful list doubles as the credential probe),
        cached on the record so revisiting the tab never refetches with
        unchanged credentials; degrades to known defaults with an honest
        source line, never blocking the chain."""
        from ..config.llm_providers import get_llm_provider_spec
        from . import provider_setup as setup_store

        if pending.model_custom:
            tab = form_tab(
                "Model",
                [
                    text_field(
                        "model",
                        label="Model id",
                        placeholder=spec.default_model or "model-id",
                    )
                ],
                submit_command="provider setup model {model}",
            )
            return tab, False, [
                "Type the model id to use.",
                "Type: /provider setup model <model-id>",
            ]

        # The fingerprint keys the cached list to the credentials that
        # produced it; the key rides as a digest so the raw secret never
        # sits in a non-redacted store field.
        key_digest = (
            hashlib.sha256(pending.api_key.encode("utf-8")).hexdigest()[:16]
            if pending.api_key
            else ""
        )
        fingerprint = (
            f"{pending.key_choice}|{key_digest}|{pending.base_url!r}"
        )
        if (
            pending.model_options is None
            or pending.models_fingerprint != fingerprint
        ):
            api_key = pending.api_key or None
            base_url = pending.base_url or None
            if pending.base_url == "" and spec.default_base_url:
                # Cleared-to-default: list against the provider default
                # instead of letting the facade adopt the current (cleared)
                # custom URL.
                base_url = spec.default_base_url
            try:
                models = await self.api.list_available_models(
                    spec.id, self.user_id, api_key=api_key, base_url=base_url
                )
            except Exception:  # noqa: BLE001 - the list degrades, never blocks.
                logger.debug("provider setup: model list failed", exc_info=True)
                models = []
            raw: list[dict[str, Any]] = []
            for entry in models or []:
                model_id = str(entry.get("id") or entry.get("name") or "")
                if not model_id:
                    continue
                ctx_len = entry.get("context_length") or entry.get(
                    "context_window"
                )
                raw.append(
                    {
                        "id": model_id,
                        "meta": f"{self._fmt_ctx(ctx_len)} ctx" if ctx_len else "",
                    }
                )
            note = (
                f"{len(raw)} models listed from {spec.label}."
                if raw
                else (
                    "Model list unavailable (credentials not accepted yet, or"
                    " the provider is unreachable); showing known defaults."
                )
            )
            # An empty list is never cached (model_options=None keeps the
            # next render retrying): the failure may be transient, and the
            # user may fix the credentials on the key tab in between.
            setup_store.update_setup(
                self.user_id,
                model_options=raw or None,
                models_fingerprint=fingerprint,
                models_note=note,
            )

        active_spec = get_llm_provider_spec(
            str(settings.get("llm_provider", "") or "")
        )
        active = active_spec is not None and active_spec.id == spec.id
        current_model = (
            str(settings.get("llm_model", "") or "").strip() if active else ""
        )
        preselect = (
            str(pending.model or "")
            or current_model
            or str(spec.default_model or "")
        )
        options: list[dict[str, Any]] = []
        for entry in pending.model_options or []:
            model_id = str(entry.get("id") or "")
            options.append(
                form_option(
                    model_id,
                    meta=str(entry.get("meta") or ""),
                    current=model_id == preselect,
                )
            )
        if not options:
            for fallback_id in dict.fromkeys(
                value
                for value in (current_model, str(spec.default_model or ""))
                if value
            ):
                options.append(
                    form_option(
                        fallback_id,
                        meta=(
                            "current model"
                            if fallback_id == current_model
                            else "spec default"
                        ),
                        current=fallback_id == preselect,
                    )
                )
        if preselect and all(option["id"] != preselect for option in options):
            # Not in the listed set (a custom model typed earlier, or the
            # current/default model missing from the live list): surface it
            # as a row so the tab renders the decision it holds.
            insert_meta = (
                "current model"
                if preselect == current_model
                else "spec default"
                if preselect == str(spec.default_model or "")
                else "custom"
            )
            options.insert(
                0, form_option(preselect, meta=insert_meta, current=True)
            )
        options.append(form_option("custom", label="Custom model id…"))
        tab = form_tab(
            "Model",
            [
                search_field("filter", placeholder="Filter models…"),
                radio_field("model", options),
            ],
            submit_command="provider setup model {model}",
        )
        return tab, pending.model is not None, [
            str(pending.models_note or ""),
            "Choose: /provider setup model <model-id>|custom",
        ]

    def _setup_review_tab(
        self, pending: Any, spec: "LLMProviderSpec"
    ) -> tuple[dict[str, Any], bool, list[str]]:
        key_label = {
            "paste": "new key (pasted)",
            "keep": "keep the existing server key",
            "clear": "CLEAR the stored key",
            "none": "not required",
        }.get(pending.key_choice, "unchanged")
        if pending.base_url is None:
            base_label = "unchanged"
        elif pending.base_url == "":
            base_label = (
                f"provider default ({spec.default_base_url or 'SDK default'})"
            )
        else:
            base_label = pending.base_url
        rows = [
            ("Provider", spec.label),
            ("API key", key_label),
            ("Model", pending.model or "?"),
            ("Base URL", base_label),
        ]
        if spec.supports_responses and pending.api_mode:
            rows.append(("API mode", pending.api_mode))
        width = max(len(label) for label, _value in rows)
        lines = ["Review the pending provider change"]
        for label, value in rows:
            lines.append(f"  {label:<{width}}  {value}")
        if pending.key_choice == "clear" and spec.requires_api_key:
            lines.append(
                "Warning: the connectivity test still uses the OLD stored key"
                " (the clear happens on apply), so a passing test does not"
                " mean the provider will work afterwards."
            )
        lines.append("Nothing is saved until you apply.")
        lines.append("Choose: /provider setup apply | notest | cancel")
        options = [
            form_option("apply", label="Test and apply", current=True),
            form_option("notest", label="Apply without testing"),
            form_option("cancel", label="Cancel"),
        ]
        tab = form_tab(
            "Review",
            [radio_field("action", options)],
            submit_command="provider setup {action}",
        )
        # Review is never "decided": it is always the chain's pending
        # decision, so it lands last and active once everything else is set.
        return tab, False, lines

    @staticmethod
    def _setup_needs_base_url(
        spec: "LLMProviderSpec", settings: Mapping[str, Any]
    ) -> bool:
        """Mirror the wizard ConnectionStep conditions, plus "a base URL is
        currently configured": switching providers must force an explicit
        keep/clear decision so a stale gateway URL cannot silently leak into
        the new provider's config."""
        current = str(settings.get("llm_base_url", "") or "").strip()
        return bool(
            spec.requires_base_url
            or spec.default_base_url is None
            or not spec.requires_api_key
            or current
        )

    @staticmethod
    def _setup_clearable_fields(
        existing: list[tuple[str, str]]
    ) -> tuple[str, ...]:
        """Settings fields a ""-patch can actually clear.

        ``ServerSettingsUpdate`` silently drops unknown fields
        (``extra='ignore'``), so a clear is only offered (and applied) for
        key fields the update model declares; anything else would be a
        silent no-op.
        """
        from ..api.schemas.settings import ServerSettingsUpdate

        return tuple(
            field_name
            for _env_var, field_name in existing
            if field_name and field_name in ServerSettingsUpdate.model_fields
        )

    async def _setup_existing_key_fields(
        self, spec: "LLMProviderSpec"
    ) -> list[tuple[str, str]]:
        """(env_var, settings_field) pairs for this provider's SET key vars.

        Resolved via the admin env listing (masked values only; presence is
        all this flow needs). Best effort: empty when nothing is set or the
        listing is unavailable.
        """
        wanted = set(spec.api_key_env_vars or ())
        if not wanted:
            return []
        try:
            data = await self.api.get_env_vars(user_id=self.user_id)
        except Exception:  # noqa: BLE001 - presence check degrades, never fails.
            return []
        matches: list[tuple[str, str]] = []
        for entry in data.get("entries", []):
            if not isinstance(entry, Mapping):
                continue
            env_var = str(entry.get("env_var") or "")
            if env_var in wanted and entry.get("is_set"):
                matches.append((env_var, str(entry.get("name") or "")))
        return matches

    async def _setup_apply(
        self,
        pending: Any,
        spec: "LLMProviderSpec",
        settings: Mapping[str, Any],
        *,
        notest: bool,
    ) -> str | CommandOutput:
        """Test-first atomic apply: nothing is written on a failed test, and
        the write is ONE ``update_settings`` patch (the applier routes the
        virtual ``llm_api_key`` to the patch's provider in the same pass)."""
        from ..config.llm_providers import get_llm_provider_spec
        from . import provider_setup as setup_store

        model = str(pending.model or "").strip()
        if not model:
            # Reached apply without a decided model (stale form): re-chain.
            return await self._setup_chain(pending, spec, settings)

        if not notest:
            request: dict[str, Any] = {"llm_provider": spec.id, "llm_model": model}
            resolved_base = pending.base_url
            if resolved_base is None:
                active_spec = get_llm_provider_spec(
                    str(settings.get("llm_provider", "") or "")
                )
                if active_spec is not None and active_spec.id == spec.id:
                    resolved_base = str(
                        settings.get("llm_base_url", "") or ""
                    ).strip()
            if resolved_base:
                request["llm_base_url"] = resolved_base
            if spec.supports_responses and pending.api_mode:
                request["openai_api_mode"] = pending.api_mode
            if pending.key_choice == "paste" and pending.api_key:
                request["api_key"] = pending.api_key
            result = await self.api.test_llm_provider_config(
                request, user_id=self.user_id
            )
            if not bool(result.get("ok", False)):
                message = (
                    str(result.get("message", "") or "").strip()
                    or "unknown error"
                )
                options = [
                    form_option("apply", label="Retry the test", current=True),
                    form_option("notest", label="Apply anyway"),
                    form_option("replace", label="Re-enter the API key"),
                    form_option("cancel", label="Cancel"),
                ]
                form = form_payload(
                    "Provider test failed",
                    [
                        form_tab(
                            "Next",
                            [radio_field("action", options)],
                            submit_command="provider setup {action}",
                        )
                    ],
                    submit_command="provider setup {action}",
                    footer_hint="Enter select · Esc cancel",
                )
                return CommandOutput(
                    f"[Info]: {spec.label} provider test FAILED: {message}\n"
                    "Nothing was saved.",
                    data=command_data(form=form),
                )

        patch: dict[str, Any] = {"llm_provider": spec.id, "llm_model": model}
        if pending.base_url is not None:
            patch["llm_base_url"] = pending.base_url
        if spec.supports_responses and pending.api_mode:
            patch["openai_api_mode"] = pending.api_mode
        if pending.key_choice == "paste" and pending.api_key:
            patch["llm_api_key"] = pending.api_key
        elif pending.key_choice == "clear":
            # Re-derive the clearable fields at apply time (the world may
            # have changed since start); an empty result must be an honest
            # refusal, because the ""-patches would silently drop
            # (ServerSettingsUpdate ignores unknown fields) and the user
            # would believe the key is gone.
            clearable = self._setup_clearable_fields(
                await self._setup_existing_key_fields(spec)
            )
            if not clearable:
                return (
                    f"[Error]: No clearable stored key was found for"
                    f" {spec.label}: its key field cannot be patched through"
                    " settings, so nothing was written. Unset the environment"
                    " variable directly (see /env) and rerun /provider setup."
                )
            for field_name in clearable:
                patch[field_name] = ""
        result = await self.api.update_settings(user_id=self.user_id, **patch)
        setup_store.clear_setup(self.user_id)
        updated = result.get("updated") or sorted(patch)
        message = (
            f"Switched to {spec.label} with model {model}."
            f" Updated: {', '.join(str(item) for item in updated)}."
        )
        if not notest:
            message = f"Provider test passed. {message}"
        if result.get("restart_required"):
            message += " Restart required for some changes."
        return f"[Success]: {message}"

    @staticmethod
    def _setup_rest_value(rest: str) -> str:
        """Everything after the step token, whitespace-trimmed, case intact.

        Derived from ``rest`` (not the shlex ``args``) so secrets and URLs
        survive characters the arg splitter would mangle. One exception: the
        form client shell-quotes a value containing whitespace, so a value
        that arrives as exactly one quoted token is unquoted back.
        """
        parts = rest.split(None, 1)
        value = parts[1].strip() if len(parts) > 1 else ""
        if value[:1] in ("'", '"'):
            try:
                tokens = shlex.split(value)
            except ValueError:
                return value
            if len(tokens) == 1:
                return tokens[0]
        return value

    @staticmethod
    def _fmt_ctx(value: Any) -> str:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return ""
        return f"{number // 1000}k" if number >= 1000 else str(number)
