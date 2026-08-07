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
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal, cast
from urllib.parse import parse_qs, urlparse

import httpx

if TYPE_CHECKING:
    from ..api.schemas.cliproxy import CLIProxyVerifyVerdict
    from ..cliproxy.catalog import CLIProxyProviderSpec
    from .provider_setup import PendingCliproxyLogin

from .command_executor_llm import (
    CLAUDE_VIA_OPENAI_ROUTE_WARNING,
    custom_model_tab,
    model_pick_tab,
)
from .command_forms import (
    CommandOutput,
    chain_form_output,
    command_error,
    command_success,
    form_option,
    form_tab,
    radio_field,
    rest_value,
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
    # file exists before reporting ok AND refuses a paste-less ok on a
    # session old enough to have expired (the relogin trap; the session
    # ledger + guard live in ``cliproxy/management_client.py``, stamped
    # inside the client's start_oauth/oauth_callback, so every surface
    # shares one predicate).

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
    # Returned as-is by every step whose pending record vanished; a frozen
    # CommandOutput is safe to share.
    _CLIPROXY_GONE = command_error(
        "No CLIProxy login is in progress (or it expired). "
        "Start one with /provider cliproxy <target>."
    )
    # Paste-time confirm window: ~10s total. Long enough for post-callback
    # provider onboarding (see the loop comment in _cliproxy_paste), short
    # enough that a genuinely stuck login still hands back a prompt.
    _PASTE_CONFIRM_ATTEMPTS = 6
    _PASTE_CONFIRM_DELAY_SECONDS = 2.0

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
                if pending.logged_in:
                    return await self._cliproxy_model_chain(
                        pending, spec, note_lines=note
                    )
                if pending.oauth_state:
                    return self._cliproxy_login_rail(
                        pending, spec, note_lines=note, fresh_rail=True
                    )
                return await self._cliproxy_target(spec)
            return await self._cliproxy_overview()
        token = args[0].strip().lower()
        if token in self._CLIPROXY_STEPS:
            return await self._cliproxy_step(token, args[1:], rest)
        spec = get_cliproxy_provider(token)
        if spec is None:
            ids = ", ".join(s.id for s in list_cliproxy_providers())
            return command_error(
                f"Unknown CLIProxy target: {args[0]}. Known: {ids}."
            )
        return await self._cliproxy_target(spec)

    async def _cliproxy_overview(self) -> str:
        """The catalog overview with logged-in badges from the auth files."""
        from ..cliproxy.catalog import list_cliproxy_providers
        from ..cliproxy.management_client import (
            login_account_label,
            present_login_entry,
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
                entry = present_login_entry(files, spec)
                if entry is not None:
                    account = login_account_label(entry)
                    qualifier = (
                        ", backing off" if entry.get("unavailable") else ""
                    )
                    badge = (
                        f"  [logged in: {account}{qualifier}]"
                        if account
                        else f"  [logged in{qualifier}]"
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
        return "\n".join(lines)

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
            login_account_label,
            present_login_entry,
        )
        from . import provider_setup as setup_store

        account = ""
        logged_in = False
        backing_off = False
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
            # Presence, not availability: an error-backoff entry is still a
            # login (a re-login would not clear a model suspension anyway),
            # so it keeps use/relogin on the table and gets an honest note.
            entry = present_login_entry(files, spec)
            if entry is not None:
                logged_in = True
                backing_off = bool(entry.get("unavailable"))
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
        if backing_off:
            lines.append(
                "  Note: the proxy reports this login temporarily"
                " unavailable (error backoff after upstream failures). It"
                " usually clears on its own; a re-login does not speed it"
                " up."
            )
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
            return "\n".join(lines)

        pending = setup_store.start_cliproxy_login(self.user_id, spec.id)
        updated = setup_store.update_cliproxy_login(
            self.user_id,
            logged_in=logged_in,
            account=account,
            backing_off=backing_off,
        )
        if updated is not None:
            pending = updated

        tab = self._cliproxy_target_tab(pending, spec)
        lines.append(
            "Choose: /provider cliproxy "
            + " | ".join(
                str(option["id"]) for option in tab["fields"][0]["options"]
            )
        )
        return chain_form_output(
            f"CLIProxy: {spec.label}",
            [tab],
            tab,
            lines,
            fallback_text=f"CLIProxy target {spec.label}.",
        )

    @staticmethod
    def _cliproxy_target_tab(
        pending: "PendingCliproxyLogin", spec: "CLIProxyProviderSpec"
    ) -> dict[str, Any]:
        """The Target tab, rendered from STORED record state (no network),
        so it rides EVERY phase of the rail: after a login starts, relogin
        and cancel stay one arrow-left away (the tab-level submit template
        carries the action while this tab is active)."""
        options: list[dict[str, Any]] = []
        if pending.logged_in:
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
        # Short step noun like every rail sibling (the provider identity
        # rides the form title, "CLIProxy: <label>").
        return form_tab(
            "Target",
            [radio_field("action", options)],
            submit_command="provider cliproxy {action}",
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
                "CLIProxy login cancelled. Nothing was applied."
                if cleared
                else "No CLIProxy login was in progress."
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
            if not pending.logged_in:
                # "use" is only offered when logged in, but it is typed-
                # reachable; applying a global route with no login would
                # break every turn, so re-verify. Presence suffices (the
                # target step's stance): a backoff entry still routes once
                # the proxy's cooldown lapses.
                from ..cliproxy.management_client import (
                    login_account_label,
                    present_login_entry,
                )

                try:
                    files = await self.api.cliproxy_auth_files(
                        spec.id, user_id=self.user_id
                    )
                except httpx.HTTPError:
                    files = []
                entry = present_login_entry(files, spec)
                if entry is None:
                    return command_error(
                        f"Not logged in to {spec.label}."
                        " Use /provider cliproxy login first."
                    )
                setup_store.update_cliproxy_login(
                    self.user_id,
                    account=login_account_label(entry),
                    logged_in=True,
                    backing_off=bool(entry.get("unavailable")),
                )
            return await self._cliproxy_model_chain(pending, spec)
        if token == "paste":
            return await self._cliproxy_paste(pending, spec, rest)
        if token == "check":
            return await self._cliproxy_check(pending, spec)
        if token == "model":
            value = rest_value(rest)
            if not value:
                return command_error(
                    "Usage: /provider cliproxy model <model-id|custom>"
                )
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
            return command_error(
                f"Could not start the {spec.label} login: "
                + self._cliproxy_error_detail(error)
            )
        updated = setup_store.update_cliproxy_login(
            self.user_id,
            oauth_state=str(started.get("state") or ""),
            auth_url=str(started.get("url") or ""),
            flow=str(started.get("flow") or spec.flow),
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
        active_step: Literal["input", "status"] = "input",
        fresh_rail: bool = False,
    ) -> CommandOutput:
        """The login rail: full auth URL, remote guidance, paste + status.

        ``active_step`` picks the tab the form opens on: ``"input"`` (the
        paste tab; the default for a fresh rail) or ``"status"`` (used when
        the next sensible action is a status check, e.g. a delivered but
        not-yet-confirmed callback). Device flows have no paste tab and
        always land on Status.

        ``fresh_rail`` marks a render whose rail content is NEW to this
        surface (the bare-command resume): such a response must not attach
        ``notes``, or a form-rendering client would print only the note and
        drop the auth URL, the one thing a resumed user came back for. The
        paste/check re-rails leave it False: there the rail was printed one
        step ago and only the delta is news.
        """
        lines = [f"Log in to {spec.label}: open this URL in a browser."]
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
        # The Target tab rides every phase (relogin/cancel one arrow-left
        # away); the login tabs retire once the login confirms.
        tabs: list[dict[str, Any]] = [self._cliproxy_target_tab(pending, spec)]
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
            active_tab = status_tab if active_step == "status" else paste_tab
        return chain_form_output(
            f"CLIProxy login: {spec.label}",
            tabs,
            active_tab,
            lines,
            fallback_text=f"Logging in to {spec.label}.",
            notes=[] if fresh_rail else list(note_lines or []),
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
            return command_error(
                "Start the login first: /provider cliproxy login"
            )
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
            # 404/409 mean the proxy is not waiting on this state anymore, which
            # includes "the login already succeeded" (it deletes the session on
            # success, and wipes sibling sessions for the provider). Reporting a
            # delivery failure there dead-ends the rail on a login that WORKED,
            # so fall through to the confirm poll and let the auth-file check
            # decide. Only a genuine transport or server fault is a failure.
            if not self._cliproxy_delivery_may_have_landed(error):
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
        # Delivered (the management client's session ledger records it, so
        # the server-confirmed status keeps trusting this session); give
        # the proxy a moment to persist the auth file, then confirm (the
        # status is server-confirmed against the auth-file list, never the
        # bare poll). The window is ~10s, not one or two polls: some
        # providers do real work between the callback and the auth file
        # (measured 2026-08-02: Gemini's GCP project onboarding took ~5s,
        # so a ~1.5s window made the success rail unreachable on paste).
        for attempt in range(self._PASTE_CONFIRM_ATTEMPTS):
            status, detail = await self._cliproxy_poll_once(pending, spec)
            if status == "ok":
                return await self._cliproxy_confirmed(pending, spec, detail)
            if status == "error":
                # Past the paste: re-pasting the same callback cannot fix a
                # failed login, so land on Status (like every post-delivery
                # rail; the note says how to restart).
                return self._cliproxy_login_rail(
                    pending,
                    spec,
                    note_lines=notes
                    + [
                        f"Login failed: {detail}" if detail else "Login failed.",
                        "Restart the login to try again.",
                    ],
                    active_step="status",
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
                    active_step="status",
                )
            if attempt < self._PASTE_CONFIRM_ATTEMPTS - 1:
                await asyncio.sleep(self._PASTE_CONFIRM_DELAY_SECONDS)
        return self._cliproxy_login_rail(
            pending,
            spec,
            note_lines=notes
            + [
                "Callback delivered; the proxy has not confirmed the login"
                " yet (some providers finish account onboarding a few"
                " seconds after the callback). Check the status in a"
                " moment; this is not a failure sign."
            ],
            active_step="status",
        )

    async def _cliproxy_check(
        self, pending: "PendingCliproxyLogin", spec: "CLIProxyProviderSpec"
    ) -> str | CommandOutput:
        """One server-confirmed status poll."""
        if not pending.oauth_state:
            return command_error(
                "Start the login first: /provider cliproxy login"
            )
        status, detail = await self._cliproxy_poll_once(pending, spec)
        if status == "ok":
            # The stale-session guard (an old, paste-less ok is the proxy's
            # unknown-session answer blessed by a pre-existing auth file)
            # runs server-side in confirm_login_landed; a refusal arrives
            # here as status "error" with the explanation as detail.
            return await self._cliproxy_confirmed(pending, spec, detail)
        if status == "wait":
            return self._cliproxy_login_rail(
                pending,
                spec,
                note_lines=["Still waiting for the login to complete."],
                active_step="status",
            )
        if status == "unreachable":
            return self._cliproxy_login_rail(
                pending,
                spec,
                note_lines=[
                    f"Could not check the login status: {detail}",
                    "Try again in a moment.",
                ],
                active_step="status",
            )
        return self._cliproxy_login_rail(
            pending,
            spec,
            note_lines=[
                f"Login failed: {detail}" if detail else "Login failed.",
                "Restart the login to try again.",
            ],
            active_step="status",
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
            self.user_id, account=account or pending.account, logged_in=True
        )
        if updated is None:
            return self._CLIPROXY_GONE
        note = f"Logged in to {spec.label}" + (
            f" as {account}." if account else "."
        )
        # The confirm above only proves the proxy LISTS an enabled auth file, so
        # exercise the credential for real before telling the user they are in.
        verdict, detail = await self._cliproxy_verify_credential(spec)
        if verdict == "auth_failed":
            # The auth file exists but the upstream rejects it, so advancing to
            # a model picker would hand over a route that cannot serve a turn.
            return self._cliproxy_login_rail(
                updated,
                spec,
                note_lines=[
                    note,
                    f"But the credential was rejected upstream: {detail}",
                    "Restart the login; the stored auth file will not serve"
                    " traffic.",
                ],
            )
        if verdict == "ok":
            note += f" Credential verified against {detail}."
        else:
            note += (
                f" Could not verify the credential yet ({detail});"
                " usually a slow or unreachable upstream rather than a bad"
                " login. Continuing, and /provider test will retest after apply."
            )
        return await self._cliproxy_model_chain(
            updated, spec, note_lines=[note]
        )

    async def _cliproxy_verify_credential(
        self, spec: "CLIProxyProviderSpec"
    ) -> tuple["CLIProxyVerifyVerdict", str]:
        """One real data-plane call, returning ``(verdict, detail)``.

        Never fatal to the login chain: any transport or facade fault degrades to
        "inconclusive" so a flaky probe cannot block a good login. An unknown
        verdict string degrades the same way, since this is the far side of a
        wire and an older backend can answer with anything.
        """

        from ..api.schemas.cliproxy import VERIFY_VERDICTS

        try:
            payload = await self.api.cliproxy_verify_credential(
                spec.id, user_id=self.user_id
            )
        except Exception as error:  # noqa: BLE001 - a probe must not break the rail.
            return "inconclusive", self._cliproxy_error_detail(error)
        verdict = str((payload or {}).get("verdict") or "").strip()
        detail = str((payload or {}).get("detail") or "").strip()
        if verdict not in VERIFY_VERDICTS:
            return "inconclusive", detail or "the probe returned no verdict"
        return cast("CLIProxyVerifyVerdict", verdict), detail

    async def _cliproxy_model_chain(
        self,
        pending: "PendingCliproxyLogin",
        spec: "CLIProxyProviderSpec",
        *,
        note_lines: list[str] | None = None,
    ) -> CommandOutput:
        """The post-login rail: Target, Model, then Apply once a model is
        set (Target persists so relogin/cancel stay one arrow-left away)."""
        chain = [await self._cliproxy_model_tab(pending, spec)]
        if chain[-1][1]:
            chain.append(self._cliproxy_apply_tab(pending, spec))
        active_tab, _decided, guidance = chain[-1]
        tabs = [self._cliproxy_target_tab(pending, spec)]
        tabs.extend(tab for tab, _d, _l in chain)
        # No notes here, deliberately: this chain's guidance is load-bearing
        # beyond the panel (the degraded-model-list honesty line, and the
        # Apply step's review table, which is the confirmation summary for a
        # GLOBAL route change). Notes-only printing would drop both.
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
            tab = custom_model_tab(
                spec.default_model or "model-id",
                "provider cliproxy model {model}",
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
            # The proxy's /v1/models is one flat pool across every
            # logged-in subscription, but entries carry owned_by, so the
            # TARGET's own models list first when the catalog knows the
            # owner (dogfood 2026-08-05: the flat 28-model view led to a
            # guessed gemini id that no provider served). Zero matches or
            # an unset owner degrades to the unpartitioned list: never
            # hide a pickable model behind a guessed mapping.
            owner = str(spec.model_owner or "").casefold()
            mine = [
                entry
                for entry in raw
                if owner and str(entry.get("meta") or "").casefold() == owner
            ]
            if mine and len(mine) < len(raw):
                others = [entry for entry in raw if entry not in mine]
                raw = mine + others
                noun = "model" if len(mine) == 1 else "models"
                note = (
                    f"{len(mine)} {spec.label} {noun} listed first;"
                    f" {len(others)} from other logged-in subscriptions"
                    " below."
                )
            elif raw:
                note = (
                    f"{len(raw)} models listed from the proxy (all logged-in"
                    " providers)."
                )
            else:
                note = (
                    "Model list unavailable from the proxy; showing the"
                    " known default."
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
        insert_meta = (
            "spec default"
            if preselect == str(spec.default_model or "")
            else "custom"
        )
        tab = model_pick_tab(
            options, preselect, insert_meta, "provider cliproxy model {model}"
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
        # A custom-typed id the proxy does not list will fail at turn time
        # with the proxy's raw "unknown provider for model" (its registry
        # is exact-match, no prefix routing). Warn, never block: the
        # cached list can be stale or degraded, and --force-shaped escape
        # hatches stay escape hatches.
        model = str(pending.model or "")
        listed = pending.model_options or []
        if (
            model
            and listed
            and all(str(entry.get("id") or "") != model for entry in listed)
        ):
            lines.append(
                f"  Warning: {model} is not in the proxy's current model"
                " list, so no logged-in subscription serves it and the"
                " route will likely fail. Check the id or re-pick from the"
                " Model tab."
            )
        # B13: a claude-* model DOES serve on a non-anthropic-routed target,
        # but without the anthropic-path CLIProxy treatment; warn at the
        # review so identity drift is a choice, not a surprise. Gate is
        # "not anthropic" rather than "== openai": the antigravity pool
        # carries claude entries too, and a google-native request for a
        # claude model is a harder failure than the one this warning was
        # written for.
        if (
            spec.nymeria_provider != "anthropic"
            and model.casefold().startswith("claude")
        ):
            lines.append(f"  Warning: {CLAUDE_VIA_OPENAI_ROUTE_WARNING}")
        if pending.backing_off:
            # Carry the target step's honesty to the point of commitment:
            # the route can apply now, but it may not serve until the
            # proxy's error backoff on this login clears.
            lines.append(
                "  Note: this login is in error backoff at the proxy; the"
                " route applies now but may not serve until the backoff"
                " clears (it does so on its own)."
            )
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
        return command_success(message)

    @staticmethod
    def _cliproxy_delivery_may_have_landed(error: Exception) -> bool:
        """True when a failed callback delivery might mean "already logged in".

        The proxy answers 404 ("unknown or expired state") or 409 ("oauth flow is
        not pending") once it has stopped waiting on a state, and a SUCCESSFUL
        login is one of the ways that happens. Neither status can be read as a
        failure without checking the auth files first.

        This reads the FACADE's status, which also covers Nymeria's own 404s (an
        unknown provider, or a remote backend too old to have the route). That
        conflation is deliberate and safe: both still route to the confirm poll,
        which reports honestly that nothing landed, and mistaking a real failure
        for "go check" costs one poll, while the reverse costs a user their
        single-use OAuth code.
        """

        from ..cliproxy.management_client import DELIVERY_SETTLED_STATUSES

        response = getattr(error, "response", None)
        status = int(getattr(response, "status_code", 0) or 0)
        return status in DELIVERY_SETTLED_STATUSES

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
        from ..cliproxy.management_client import oauth_state_from_redirect_url

        return oauth_state_from_redirect_url(redirect_url)

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
