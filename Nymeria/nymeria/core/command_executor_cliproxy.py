"""CLIProxy subscription-OAuth command bodies (backlog #110 phase 2).

House style: backend handler families live in their own domain mixin module
rather than being appended to ``command_service.py`` (see the siblings
``command_executor_llm.py`` and ``command_executor_provider_setup.py``).
``_CommandExecutor`` inherits :class:`CliproxyCommandsMixin`, which owns the
``/provider cliproxy`` chain: catalog overview with logged-in badges, target
detail, in-REPL OAuth login over the admin ``/cliproxy`` facade with
server-confirmed status, model pick, route apply. Handler methods are
resolved by ``CommandService.execute`` via ``getattr(executor,
"_cmd_<path>")``.

Runtime leaf: imports ``command_forms`` (incl. the shared step-rail
renderer), never ``command_service`` at module scope, so there is no import
cycle.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

import httpx

if TYPE_CHECKING:
    from ..cliproxy.catalog import CLIProxyProviderSpec
    from .provider_setup import PendingCliproxyLogin

from .command_forms import (
    CommandOutput,
    chain_form_output,
    form_option,
    form_tab,
    radio_field,
    rest_value,
    search_field,
    text_field,
)

logger = logging.getLogger(__name__)


class CliproxyCommandsMixin:
    """CLIProxy command bodies mixed into ``_CommandExecutor``.

    The host provides ``api`` and ``user_id``; the annotations below let
    the static checker see them on the mixin in isolation.
    """

    api: Any
    user_id: str

    # ── CLIProxy subscription OAuth chain (backlog #110 phase 2) ──────────
    #
    # Same shape as the /provider setup chain (sibling module): one
    # registered path (``provider cliproxy``) whose first argument is
    # either a catalog target id (start the chain at the target step) or a
    # reserved step token dispatched by the form the previous step
    # returned. In-flight state (the proxy's pending-session token, the
    # auth URL, the cached model list) lives in ``core.provider_setup``'s
    # parallel store with the same sliding 10-minute TTL, which also
    # matches the proxy's own OAuth session lifetime.
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
        return chain_form_output(
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
            value = rest_value(rest)
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
        return chain_form_output(
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
        value, hygiene = setup_store.clean_pasted_secret(rest_value(rest))
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
        return chain_form_output(
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
