"""Tests for the /provider cliproxy OAuth chain (backlog #110 phase 2).

Server-side coverage of the subscription-login chain: the overview badges,
the target step's login-state-aware form, the login rail (auth URL, tunnel
hint, paste/status tabs, device-flow shape), paste parsing variants and the
post-delivery confirmation polls, the check step, the model step's cache
and degrade path, route apply, cancel, and the parallel TTL store. The CLI
renderer/adapter halves are covered by test_cli_form_panel.py and
test_cli_form_contract.py; the /cliproxy route bodies the facade shares
are covered by test_cliproxy_router.py.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import pytest

from cli_fixtures import run
from nymeria.core import command_executor_cliproxy as chain_module
from nymeria.core import provider_setup
from nymeria.core.command_service import CommandContext, CommandService


@pytest.fixture(autouse=True)
def _clear_pending_stores():
    provider_setup._pending.clear()
    provider_setup._pending_cliproxy.clear()
    yield
    provider_setup._pending.clear()
    provider_setup._pending_cliproxy.clear()


def _http_error(status: int, detail: str) -> httpx.HTTPStatusError:
    """A wire-shaped facade error (what both command clients raise)."""
    request = httpx.Request("GET", "http://test/cliproxy")
    response = httpx.Response(status, json={"detail": detail}, request=request)
    return httpx.HTTPStatusError(detail, request=request, response=response)


MANAGEMENT_ENV = [
    {"name": "cliproxy_management_url", "is_set": True},
    {"name": "cliproxy_management_key", "is_set": True},
]

AUTH_URL = (
    "https://auth.example/authorize?client_id=x"
    "&redirect_uri=http%3A%2F%2Flocalhost%3A1455%2Fcallback&state=st-1"
)


class FakeCliproxyApi:
    """Minimal API facade for the /provider cliproxy handlers."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.env_entries: list[dict[str, Any]] = [dict(e) for e in MANAGEMENT_ENV]
        self.auth_files: list[dict[str, Any]] = []
        self.start_result: dict[str, Any] = {
            "provider": "claude",
            "flow": "browser",
            "url": AUTH_URL,
            "state": "st-1",
        }
        # Consumed left to right by successive status polls (last repeats).
        self.status_results: list[dict[str, Any]] = [{"status": "wait"}]
        self.models: list[dict[str, Any]] = []
        # The post-confirm credential probe (one real data-plane call in prod).
        self.verify_result: dict[str, Any] = {
            "verdict": "ok",
            "detail": "claude-opus-4-7",
        }
        self.apply_result: dict[str, Any] = {
            "scope": "global",
            "provider": "anthropic",
            "model": "claude-opus-4-7",
            "base_url": "http://localhost:8317",
            "api_mode": "",
            "restart_required": False,
        }

    async def get_env_vars(self, *, user_id: str | None = None) -> dict[str, Any]:
        return {"entries": [dict(entry) for entry in self.env_entries]}

    async def cliproxy_auth_files(
        self, provider: str | None = None, *, user_id: str | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append(("auth_files", {"provider": provider}))
        files = [dict(entry) for entry in self.auth_files]
        if provider:
            # Mirrors the real backend filter (auth_entry_matches_spec), so
            # a version-dependent listing spelling exercises the same code
            # the slim shape runs.
            from nymeria.cliproxy.catalog import get_cliproxy_provider
            from nymeria.cliproxy.management_client import (
                auth_entry_matches_spec,
            )

            spec = get_cliproxy_provider(provider)
            if spec is not None:
                files = [
                    entry
                    for entry in files
                    if auth_entry_matches_spec(entry, spec)
                ]
            else:
                files = [
                    entry
                    for entry in files
                    if str(entry.get("provider") or "").lower() == provider
                ]
        return files

    async def cliproxy_oauth_start(
        self, provider: str, *, user_id: str | None = None
    ) -> dict[str, Any]:
        self.calls.append(("oauth_start", {"provider": provider}))
        return dict(self.start_result)

    async def cliproxy_oauth_callback(
        self,
        provider: str,
        *,
        redirect_url: str | None = None,
        code: str | None = None,
        state: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "oauth_callback",
                {
                    "provider": provider,
                    "redirect_url": redirect_url,
                    "code": code,
                    "state": state,
                },
            )
        )
        return {"status": "ok"}

    async def cliproxy_oauth_status(
        self, state: str, provider: str, *, user_id: str | None = None
    ) -> dict[str, Any]:
        self.calls.append(("oauth_status", {"state": state, "provider": provider}))
        if len(self.status_results) > 1:
            return dict(self.status_results.pop(0))
        return dict(self.status_results[0])

    async def cliproxy_models(
        self, *, user_id: str | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append(("models", {}))
        return [dict(entry) for entry in self.models]

    async def cliproxy_verify_credential(
        self,
        provider: str,
        *,
        model: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(("verify", {"provider": provider, "model": model}))
        return dict(self.verify_result)

    async def cliproxy_apply_route(
        self,
        provider: str,
        model: str,
        *,
        scope: str = "global",
        user_id: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            ("apply_route", {"provider": provider, "model": model, "scope": scope})
        )
        return dict(self.apply_result)


def _run(api: FakeCliproxyApi, command: str, *, is_admin: bool = True):
    return run(
        CommandService().execute(
            CommandContext(
                user_id="alice",
                thread_id="thread-1",
                actor="user",
                surface="cli",
                is_admin=is_admin,
                supports_forms=True,
            ),
            command,
            api=api,
        )
    )


def _form(result) -> dict[str, Any]:
    form = (result.data or {}).get("form")
    assert form is not None, f"expected a form on: {result.markdown}"
    return form


def _active_tab(form: dict[str, Any]) -> dict[str, Any]:
    flagged = [tab for tab in form["tabs"] if tab.get("active")]
    assert len(flagged) == 1, f"expected one active tab: {form['tabs']}"
    return flagged[0]


def _calls(api: FakeCliproxyApi, name: str) -> list[dict[str, Any]]:
    return [payload for called, payload in api.calls if called == name]


LOGGED_IN_CLAUDE = {
    "provider": "claude",
    "account": "alice@example.com",
    "disabled": False,
    "unavailable": False,
}


# ── overview ────────────────────────────────────────────────────────────────


def test_overview_shows_logged_in_badges() -> None:
    api = FakeCliproxyApi()
    api.auth_files = [dict(LOGGED_IN_CLAUDE)]

    result = _run(api, "/provider cliproxy")

    assert result.success is True
    # A catalog readout: info level, so no outcome artifact leads the body.
    assert result.level == "info"
    assert not result.markdown.startswith(("**Error:**", "**Done.**"))
    assert "Management API: configured" in result.markdown
    assert "[logged in: alice@example.com]" in result.markdown
    # Only the claude line carries the badge.
    assert result.markdown.count("[logged in") == 1
    assert "/provider cliproxy <target>" in result.markdown


def test_overview_unconfigured_guides_env_and_skips_probe() -> None:
    api = FakeCliproxyApi()
    api.env_entries = []

    result = _run(api, "/provider cliproxy")

    assert result.success is True
    assert "not configured" in result.markdown
    assert "cliproxy_management_url" in result.markdown
    assert _calls(api, "auth_files") == []


def test_overview_degrades_when_auth_files_unreachable() -> None:
    class _BrokenFilesApi(FakeCliproxyApi):
        async def cliproxy_auth_files(
            self, provider: str | None = None, *, user_id: str | None = None
        ):
            raise _http_error(502, "proxy unreachable")

    result = _run(_BrokenFilesApi(), "/provider cliproxy")

    assert result.success is True
    assert "Login state unavailable" in result.markdown
    assert "proxy unreachable" in result.markdown


# ── target step ─────────────────────────────────────────────────────────────


def test_target_not_logged_in_offers_login() -> None:
    api = FakeCliproxyApi()

    result = _run(api, "/provider cliproxy claude")

    assert result.success is True
    assert "Routes as" in result.markdown
    tab = _active_tab(_form(result))
    option_ids = [o["id"] for o in tab["fields"][0]["options"]]
    assert option_ids == ["login", "cancel"]
    assert tab["submit"] == {"command": "provider cliproxy {action}"}
    pending = provider_setup.get_cliproxy_login("alice")
    assert pending is not None and pending.target == "claude"


def test_target_logged_in_offers_use_and_relogin() -> None:
    api = FakeCliproxyApi()
    api.auth_files = [dict(LOGGED_IN_CLAUDE)]

    result = _run(api, "/provider cliproxy claude")

    assert "alice@example.com" in result.markdown
    tab = _active_tab(_form(result))
    option_ids = [o["id"] for o in tab["fields"][0]["options"]]
    assert option_ids == ["use", "relogin", "cancel"]
    pending = provider_setup.get_cliproxy_login("alice")
    assert pending is not None and pending.account == "alice@example.com"


def test_target_logged_in_under_the_v7_listing_spelling() -> None:
    """The dogfood repro (2026-08-02): v7.1.61 lists a completed Gemini
    login as provider "gemini-cli" while the catalog says "gemini"; the
    target step must still offer use/relogin, not just login."""
    api = FakeCliproxyApi()
    api.auth_files = [
        {
            "provider": "gemini-cli",
            "account": "alice@example.com",
            "disabled": False,
            "unavailable": False,
        }
    ]

    result = _run(api, "/provider cliproxy gemini-cli")

    tab = _active_tab(_form(result))
    option_ids = [o["id"] for o in tab["fields"][0]["options"]]
    assert option_ids == ["use", "relogin", "cancel"]
    assert "alice@example.com" in result.markdown


def test_target_shows_tos_warning() -> None:
    result = _run(FakeCliproxyApi(), "/provider cliproxy gemini-cli")
    assert "Warning:" in result.markdown
    assert "policy" in result.markdown


def test_bare_resume_keys_on_logged_in_not_the_account_label() -> None:
    """A confirmed login whose auth-file entry carries no account/email
    still resumes into the model chain: the resume gate reads the stored
    logged_in flag, not the possibly-empty account label."""
    api = FakeCliproxyApi()
    api.auth_files = [
        {"provider": "claude", "disabled": False, "unavailable": False},
    ]
    _run(api, "/provider cliproxy claude")

    result = _run(api, "/provider cliproxy")

    assert "Resuming" in result.markdown
    form = _form(result)
    assert [tab["label"] for tab in form["tabs"]][:2] == ["Target", "Model"]


def test_target_management_unconfigured_degrades_to_guidance() -> None:
    class _UnconfiguredApi(FakeCliproxyApi):
        def __init__(self) -> None:
            super().__init__()
            self.env_entries = []

        async def cliproxy_auth_files(
            self, provider: str | None = None, *, user_id: str | None = None
        ):
            raise _http_error(400, "CLIProxy management is not configured")

    result = _run(_UnconfiguredApi(), "/provider cliproxy claude")

    assert result.success is True
    assert "not configured" in result.markdown
    assert (result.data or {}).get("form") is None


def test_unknown_target_is_refused() -> None:
    result = _run(FakeCliproxyApi(), "/provider cliproxy bogus")
    assert result.success is False
    # The authored level, not a string prefix, drives the rendered artifact
    # every surface reads (#132).
    assert result.level == "error"
    assert result.markdown.startswith("**Error:** Unknown CLIProxy target")


def test_requires_admin() -> None:
    result = _run(FakeCliproxyApi(), "/provider cliproxy", is_admin=False)
    assert result.success is False


# ── login rail ──────────────────────────────────────────────────────────────


def test_login_renders_rail_with_url_and_tunnel_hint() -> None:
    api = FakeCliproxyApi()
    _run(api, "/provider cliproxy claude")

    result = _run(api, "/provider cliproxy login")

    assert result.success is True
    assert AUTH_URL in result.markdown
    assert "ssh -N -L 1455:127.0.0.1:1455" in result.markdown
    assert "Type: /provider cliproxy paste" in result.markdown
    form = _form(result)
    # The Target tab rides the rail (relogin/cancel one arrow-left away);
    # short step noun, the provider identity rides the form title.
    assert "Claude (Max/Pro subscription)" in form["title"]
    assert [tab["label"] for tab in form["tabs"]] == [
        "Target",
        "Paste",
        "Status",
    ]
    tab = _active_tab(form)
    assert tab["label"] == "Paste"
    assert tab["fields"][0]["kind"] == "text"
    # The pasted value carries a single-use authorization code, so the field must
    # stay masked. Nothing pinned this before, so a regression that dropped the
    # flag would have shipped a plaintext code on screen.
    assert tab["fields"][0]["secret"] is True
    assert tab["submit"] == {"command": "provider cliproxy paste {callback}"}
    # On a text step the arrows edit the value and only cross steps from the
    # ends, so the rail advertises Tab as the reliable step key.
    assert form["footer_hint"] == "←→ move · Tab step · Enter submit · Esc close"
    target_tab = form["tabs"][0]
    assert target_tab["submit"] == {"command": "provider cliproxy {action}"}
    assert [o["id"] for o in target_tab["fields"][0]["options"]] == [
        "login",
        "cancel",
    ]
    pending = provider_setup.get_cliproxy_login("alice")
    assert pending is not None and pending.oauth_state == "st-1"


def test_device_flow_login_gets_status_tab_only() -> None:
    api = FakeCliproxyApi()
    api.start_result = {
        "provider": "kimi",
        "flow": "device",
        "url": "https://kimi.example/device",
        "state": "st-dev",
    }
    _run(api, "/provider cliproxy kimi")

    result = _run(api, "/provider cliproxy login")

    form = _form(result)
    assert [tab["label"] for tab in form["tabs"]] == [
        "Target",
        "Status",
    ]
    tab = _active_tab(form)
    option_ids = [o["id"] for o in tab["fields"][0]["options"]]
    assert option_ids == ["check", "restart", "cancel"]
    assert "check" in result.markdown


def test_login_start_failure_is_honest() -> None:
    class _NoStartApi(FakeCliproxyApi):
        async def cliproxy_oauth_start(
            self, provider: str, *, user_id: str | None = None
        ):
            raise _http_error(422, "provider not supported by this proxy")

    api = _NoStartApi()
    _run(api, "/provider cliproxy grok")

    result = _run(api, "/provider cliproxy login")

    assert result.success is False
    assert "not supported" in result.markdown


def test_step_without_pending_record_is_refused() -> None:
    result = _run(FakeCliproxyApi(), "/provider cliproxy login")
    assert result.success is False
    assert result.level == "error"
    assert result.markdown.startswith("**Error:** No CLIProxy login is in progress")


# ── paste variants + confirmation ───────────────────────────────────────────


def _start_login(api: FakeCliproxyApi, target: str = "claude") -> None:
    _run(api, f"/provider cliproxy {target}")
    _run(api, "/provider cliproxy login")


def test_paste_full_url_delivers_redirect_and_confirms() -> None:
    api = FakeCliproxyApi()
    api.status_results = [{"status": "ok", "detail": "alice@example.com"}]
    api.models = [{"id": "claude-opus-4-7", "owned_by": "anthropic"}]
    _start_login(api)

    result = _run(
        api,
        "/provider cliproxy paste http://localhost:1455/callback?code=abc&state=st-1",
    )

    assert result.success is True
    deliveries = _calls(api, "oauth_callback")
    assert deliveries == [
        {
            "provider": "claude",
            "redirect_url": "http://localhost:1455/callback?code=abc&state=st-1",
            "code": None,
            "state": None,
        }
    ]
    assert "Logged in to" in result.markdown
    assert "alice@example.com" in result.markdown
    tab = _active_tab(_form(result))
    assert tab["label"] == "Model"


def test_paste_query_parses_code_and_state() -> None:
    api = FakeCliproxyApi()
    api.status_results = [{"status": "ok", "detail": ""}]
    _start_login(api)

    _run(api, "/provider cliproxy paste code=abc&state=st-1")

    assert _calls(api, "oauth_callback") == [
        {
            "provider": "claude",
            "redirect_url": None,
            "code": "abc",
            "state": "st-1",
        }
    ]


def test_paste_state_mismatch_is_refused() -> None:
    """A stale callback (an earlier attempt's URL) is refused honestly
    instead of being delivered to the other session and polled forever."""
    api = FakeCliproxyApi()
    _start_login(api)

    result = _run(
        api,
        "/provider cliproxy paste"
        " http://localhost:1455/callback?code=abc&state=STALE",
    )

    assert "state mismatch" in result.markdown
    assert _calls(api, "oauth_callback") == []
    assert _active_tab(_form(result))["label"] == "Paste"


def test_paste_bare_code_with_base64_padding_is_a_code() -> None:
    """A bare '=' is not enough to mean query string: base64ish codes end
    in '=' padding and must ride as the code with the stored state."""
    api = FakeCliproxyApi()
    api.status_results = [{"status": "ok", "detail": ""}]
    _start_login(api)

    _run(api, "/provider cliproxy paste YWJjZGVm==")

    assert _calls(api, "oauth_callback") == [
        {
            "provider": "claude",
            "redirect_url": None,
            "code": "YWJjZGVm==",
            "state": "st-1",
        }
    ]


def test_paste_bare_code_uses_stored_state() -> None:
    api = FakeCliproxyApi()
    api.status_results = [{"status": "ok", "detail": ""}]
    _start_login(api)

    _run(api, "/provider cliproxy paste abc-code")

    assert _calls(api, "oauth_callback") == [
        {
            "provider": "claude",
            "redirect_url": None,
            "code": "abc-code",
            "state": "st-1",
        }
    ]


def test_paste_unconfirmed_keeps_the_rail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = FakeCliproxyApi()
    api.status_results = [{"status": "wait"}]
    _start_login(api)

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(chain_module.asyncio, "sleep", _no_sleep)
    result = _run(api, "/provider cliproxy paste abc-code")

    assert result.success is True
    assert "not confirmed" in result.markdown
    # The full ~10s confirm window (collapsed by the patched sleep): some
    # providers finish onboarding seconds after the callback (Gemini).
    from nymeria.core.command_executor_cliproxy import CliproxyCommandsMixin

    assert (
        len(_calls(api, "oauth_status"))
        == CliproxyCommandsMixin._PASTE_CONFIRM_ATTEMPTS
    )
    # The rail lands on Status: the next sensible action is a check, and
    # the note says the wait is not a failure sign.
    tab = _active_tab(_form(result))
    assert tab["label"] == "Status"
    assert "not a failure sign" in result.markdown
    # The delta rides form.notes so form-rendering clients can print it
    # alone instead of reprinting the whole rail.
    notes = _form(result).get("notes")
    assert isinstance(notes, list) and len(notes) == 1
    assert notes[0].startswith("Callback delivered")
    assert notes[0] in result.markdown


def test_paste_confirms_on_a_late_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A login whose auth file lands mid-window (post-callback onboarding)
    still reaches the success rail from the paste itself."""

    api = FakeCliproxyApi()
    api.status_results = [
        {"status": "wait"},
        {"status": "wait"},
        {"status": "wait"},
        {"status": "ok", "detail": "alice@example.com"},
    ]
    _start_login(api)

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(chain_module.asyncio, "sleep", _no_sleep)
    result = _run(api, "/provider cliproxy paste abc-code")

    assert result.success is True
    assert "Logged in to" in result.markdown
    assert len(_calls(api, "oauth_status")) == 4
    tab = _active_tab(_form(result))
    assert tab["label"] == "Model"


def test_paste_delivery_failure_rearms_the_rail() -> None:
    class _BrokenCallbackApi(FakeCliproxyApi):
        async def cliproxy_oauth_callback(self, provider: str, **kwargs: Any):
            raise _http_error(502, "proxy rejected the callback")

    api = _BrokenCallbackApi()
    _start_login(api)

    result = _run(api, "/provider cliproxy paste abc-code")

    assert result.success is True
    assert "Callback delivery failed" in result.markdown
    assert "proxy rejected the callback" in result.markdown
    # The rail form survives so the user can paste again.
    assert _active_tab(_form(result))["label"] == "Paste"


def test_confirmed_login_verifies_the_credential_and_says_so() -> None:
    """A login confirm proves an auth file is listed, not that it serves traffic.

    confirm_login_landed only checks the proxy's auth-file list, so a revoked or
    expired refresh token passes it. Submitting the paste therefore ends with one
    real data-plane call, and the outcome is stated plainly.
    """

    api = FakeCliproxyApi()
    api.status_results = [{"status": "ok", "detail": "alice@example.com"}]
    api.verify_result = {"verdict": "ok", "detail": "claude-opus-4-7"}
    _start_login(api)

    result = _run(api, "/provider cliproxy paste abc-code")

    assert result.success is True
    assert "Logged in to" in result.markdown
    assert "Credential verified against claude-opus-4-7" in result.markdown
    assert _calls(api, "verify") == [{"provider": "claude", "model": None}]


def test_rejected_credential_keeps_the_login_rail() -> None:
    """An upstream rejection must not advance to the model picker."""

    api = FakeCliproxyApi()
    api.status_results = [{"status": "ok", "detail": "alice@example.com"}]
    api.verify_result = {
        "verdict": "auth_failed",
        "detail": "401 invalid_grant",
    }
    _start_login(api)

    result = _run(api, "/provider cliproxy paste abc-code")

    assert "rejected upstream" in result.markdown
    assert "401 invalid_grant" in result.markdown
    # Still the login rail, not the route chain.
    assert "CLIProxy login" in _form(result)["title"]
    assert _calls(api, "apply_route") == []


def test_inconclusive_verification_does_not_block_the_login() -> None:
    """A flaky probe must not fail a login that the auth-file check confirmed."""

    api = FakeCliproxyApi()
    api.status_results = [{"status": "ok", "detail": "alice@example.com"}]
    api.verify_result = {"verdict": "inconclusive", "detail": "proxy timeout"}
    _start_login(api)

    result = _run(api, "/provider cliproxy paste abc-code")

    assert "Logged in to" in result.markdown
    assert "Could not verify the credential yet" in result.markdown
    assert "proxy timeout" in result.markdown
    # Advanced to the route chain regardless.
    assert "CLIProxy route" in _form(result)["title"]


def test_verification_failure_degrades_to_inconclusive() -> None:
    """A probe that raises is inconclusive, never a failed login."""

    class _BrokenVerifyApi(FakeCliproxyApi):
        async def cliproxy_verify_credential(self, provider: str, **kwargs: Any):
            raise _http_error(502, "verify endpoint exploded")

    api = _BrokenVerifyApi()
    api.status_results = [{"status": "ok", "detail": "alice@example.com"}]
    _start_login(api)

    result = _run(api, "/provider cliproxy paste abc-code")

    assert "Logged in to" in result.markdown
    assert "Could not verify the credential yet" in result.markdown
    assert "CLIProxy route" in _form(result)["title"]


def test_paste_404_confirms_instead_of_reporting_failure() -> None:
    """A 404 on delivery can mean the login already landed, so confirm first.

    The proxy deletes an OAuth session when the login SUCCEEDS (and wipes
    sibling sessions for the same provider), so a second delivery of the same
    callback answers 404 "unknown or expired state". Reporting that as a
    delivery failure dead-ended the rail on a login that had actually worked,
    which is exactly what happened in the field: the auth file was written and
    the models registered, and the user was told delivery failed.
    """

    class _GoneSessionApi(FakeCliproxyApi):
        async def cliproxy_oauth_callback(self, provider: str, **kwargs: Any):
            raise _http_error(404, "unknown or expired state")

    api = _GoneSessionApi()
    # The proxy answers ok for a session it no longer knows, and the auth-file
    # confirm behind this is what makes the ok trustworthy.
    api.status_results = [{"status": "ok", "detail": "alice@example.com"}]
    _start_login(api)

    result = _run(api, "/provider cliproxy paste abc-code")

    assert result.success is True
    assert "Callback delivery failed" not in result.markdown
    # Confirmed against the auth-file list, so the rail advances to the route.
    assert "Logged in to" in result.markdown
    assert "alice@example.com" in result.markdown
    assert len(_calls(api, "oauth_status")) == 1


def test_paste_404_on_a_relogin_still_gets_the_stale_session_refusal() -> None:
    """The fall-through does NOT disarm the relogin guard, by design.

    A 404 cannot tell "the login landed and the proxy dropped the session" from
    "the session expired", so delivery is deliberately left unmarked in the
    ledger. On a RE-login (an auth file already existed) past the guard window
    that keeps the server-side refusal armed, and the user gets a conservative
    "restart the login" instead of a false "logged in". That is the intended
    trade: the recoverable outcome over the unrecoverable one. Pinned here
    because the happy-path 404 tests stub the status poll and cannot see it.
    """

    class _GoneSessionApi(FakeCliproxyApi):
        async def cliproxy_oauth_callback(self, provider: str, **kwargs: Any):
            raise _http_error(404, "unknown or expired state")

    api = _GoneSessionApi()
    api.auth_files = [dict(LOGGED_IN_CLAUDE)]
    # What confirm_login_landed returns once stale_session_refusal fires.
    api.status_results = [
        {
            "status": "error",
            "detail": (
                "The proxy answered ok, but this login session is old"
                " enough to have expired and no callback was delivered,"
                " so that is likely a stale-session answer blessing an"
                " older login. Restart the login to be sure."
            ),
        }
    ]
    _start_login(api)

    result = _run(api, "/provider cliproxy paste abc-code")

    assert "Callback delivery failed" not in result.markdown
    assert "stale-session" in result.markdown
    assert "Restart the login" in result.markdown
    # Still on the login rail (never the model picker), landing on Status:
    # re-pasting cannot fix a post-delivery failure.
    assert _active_tab(_form(result))["label"] == "Status"


def test_paste_409_also_confirms_instead_of_reporting_failure() -> None:
    class _NotPendingApi(FakeCliproxyApi):
        async def cliproxy_oauth_callback(self, provider: str, **kwargs: Any):
            raise _http_error(409, "oauth flow is not pending")

    api = _NotPendingApi()
    api.status_results = [{"status": "ok", "detail": ""}]
    _start_login(api)

    result = _run(api, "/provider cliproxy paste abc-code")

    assert "Callback delivery failed" not in result.markdown
    assert "Logged in to" in result.markdown


def test_paste_404_still_reports_failure_when_no_login_landed() -> None:
    """The fall-through defers to the confirm; it does not invent a success."""

    class _GoneSessionNoAuthApi(FakeCliproxyApi):
        async def cliproxy_oauth_callback(self, provider: str, **kwargs: Any):
            raise _http_error(404, "unknown or expired state")

        async def cliproxy_oauth_status(self, *args: Any, **kwargs: Any):
            return {"status": "error", "detail": "no active auth file"}

    api = _GoneSessionNoAuthApi()
    _start_login(api)

    result = _run(api, "/provider cliproxy paste abc-code")

    assert "Login failed" in result.markdown
    # Post-delivery failure: the rail lands on Status, not Paste.
    assert _active_tab(_form(result))["label"] == "Status"


def test_paste_on_device_flow_points_at_status() -> None:
    api = FakeCliproxyApi()
    api.start_result = {
        "provider": "kimi",
        "flow": "device",
        "url": "https://kimi.example/device",
        "state": "st-dev",
    }
    _start_login(api, "kimi")

    result = _run(api, "/provider cliproxy paste whatever")

    assert "no paste step" in result.markdown
    assert _calls(api, "oauth_callback") == []


# ── check step ──────────────────────────────────────────────────────────────


def test_check_wait_ok_and_error_paths() -> None:
    api = FakeCliproxyApi()
    api.status_results = [
        {"status": "wait"},
        {"status": "ok", "detail": "alice@example.com"},
    ]
    _start_login(api)

    waiting = _run(api, "/provider cliproxy check")
    assert "Still waiting" in waiting.markdown

    confirmed = _run(api, "/provider cliproxy check")
    assert "alice@example.com" in confirmed.markdown
    assert _active_tab(_form(confirmed))["label"] == "Model"

    api.status_results = [{"status": "error", "detail": "no active auth file"}]
    _run(api, "/provider cliproxy relogin")
    failed = _run(api, "/provider cliproxy check")
    assert "Login failed: no active auth file" in failed.markdown
    assert "Restart the login" in failed.markdown
    assert _active_tab(_form(failed))["label"] == "Status"


def test_check_renders_server_side_stale_session_refusal() -> None:
    """The relogin trap is guarded SERVER-SIDE since the session ledger
    moved into the management client (confirm_login_landed refuses an old,
    paste-less ok); the chain has no local guard and renders the refusal
    detail on the login rail like any other status error. The guard itself
    is pinned in test_cliproxy_management_client.py."""
    api = FakeCliproxyApi()
    api.auth_files = [dict(LOGGED_IN_CLAUDE)]
    api.status_results = [
        {
            "status": "error",
            "detail": (
                "The proxy answered ok, but this login session is old"
                " enough to have expired and no callback was delivered,"
                " so that is likely a stale-session answer blessing an"
                " older login. Restart the login to be sure."
            ),
        }
    ]
    _start_login(api)

    refused = _run(api, "/provider cliproxy check")
    assert "stale-session" in refused.markdown
    assert "Restart the login" in refused.markdown
    # A check failure lands on Status like every post-delivery rail.
    assert _active_tab(_form(refused))["label"] == "Status"


def test_check_before_login_is_refused() -> None:
    api = FakeCliproxyApi()
    _run(api, "/provider cliproxy claude")

    result = _run(api, "/provider cliproxy check")

    assert result.success is False
    assert "Start the login first" in result.markdown


# ── model step ──────────────────────────────────────────────────────────────


def test_use_existing_login_lists_models_and_caches() -> None:
    api = FakeCliproxyApi()
    api.auth_files = [dict(LOGGED_IN_CLAUDE)]
    api.models = [
        {"id": "claude-opus-4-7", "owned_by": "anthropic"},
        {"id": "gpt-5.5", "owned_by": "openai"},
    ]
    _run(api, "/provider cliproxy claude")

    result = _run(api, "/provider cliproxy use")

    assert "2 models listed" in result.markdown
    tab = _active_tab(_form(result))
    option_ids = [o["id"] for o in tab["fields"][1]["options"]]
    assert option_ids == ["claude-opus-4-7", "gpt-5.5", "custom"]
    # The spec default is preselected.
    current = [o["id"] for o in tab["fields"][1]["options"] if o.get("current")]
    assert current == ["claude-opus-4-7"]

    # Revisiting the model step never refetches (cached on the record).
    _run(api, "/provider cliproxy model gpt-5.5")
    assert len(_calls(api, "models")) == 1


def test_model_list_failure_degrades_to_spec_default() -> None:
    class _BrokenModelsApi(FakeCliproxyApi):
        async def cliproxy_models(self, *, user_id: str | None = None):
            raise _http_error(502, "proxy offline")

    api = _BrokenModelsApi()
    api.auth_files = [dict(LOGGED_IN_CLAUDE)]
    _run(api, "/provider cliproxy claude")

    result = _run(api, "/provider cliproxy use")

    assert "Model list unavailable" in result.markdown
    tab = _active_tab(_form(result))
    option_ids = [o["id"] for o in tab["fields"][1]["options"]]
    assert option_ids == ["claude-opus-4-7", "custom"]


def test_model_custom_switches_to_text_entry() -> None:
    api = FakeCliproxyApi()
    api.auth_files = [dict(LOGGED_IN_CLAUDE)]
    _run(api, "/provider cliproxy claude")
    _run(api, "/provider cliproxy use")

    result = _run(api, "/provider cliproxy model custom")

    tab = _active_tab(_form(result))
    assert tab["label"] == "Model"
    assert tab["fields"][0]["kind"] == "text"
    assert tab["submit"] == {"command": "provider cliproxy model {model}"}


# ── apply + cancel + store ──────────────────────────────────────────────────


def test_model_pick_then_apply_routes_globally_and_clears() -> None:
    api = FakeCliproxyApi()
    api.auth_files = [dict(LOGGED_IN_CLAUDE)]
    api.models = [{"id": "claude-opus-4-7", "owned_by": "anthropic"}]
    _run(api, "/provider cliproxy claude")
    _run(api, "/provider cliproxy use")

    picked = _run(api, "/provider cliproxy model claude-opus-4-7")
    form = _form(picked)
    assert [tab["label"] for tab in form["tabs"]] == [
        "Target",
        "Model",
        "Apply",
    ]
    assert _active_tab(form)["label"] == "Apply"
    # The persistent Target tab offers the logged-in action set.
    assert [o["id"] for o in form["tabs"][0]["fields"][0]["options"]] == [
        "use",
        "relogin",
        "cancel",
    ]
    assert "applies globally" in picked.markdown.lower()

    applied = _run(api, "/provider cliproxy apply")
    assert applied.success is True
    assert applied.level == "success"
    assert applied.markdown.startswith("**Done.** CLIProxy route applied")
    assert _calls(api, "apply_route") == [
        {"provider": "claude", "model": "claude-opus-4-7", "scope": "global"}
    ]
    assert "provider anthropic" in applied.markdown
    assert "/provider test" in applied.markdown
    assert provider_setup.get_cliproxy_login("alice") is None


def test_apply_failure_keeps_the_chain() -> None:
    class _BrokenApplyApi(FakeCliproxyApi):
        async def cliproxy_apply_route(self, provider: str, model: str, **kw: Any):
            raise _http_error(422, "no gatekeeper key could be resolved")

    api = _BrokenApplyApi()
    api.auth_files = [dict(LOGGED_IN_CLAUDE)]
    api.models = [{"id": "claude-opus-4-7", "owned_by": "anthropic"}]
    _run(api, "/provider cliproxy claude")
    _run(api, "/provider cliproxy use")
    _run(api, "/provider cliproxy model claude-opus-4-7")

    result = _run(api, "/provider cliproxy apply")

    assert result.success is True  # Info-level so the form survives.
    assert "Apply failed" in result.markdown
    assert "gatekeeper" in result.markdown
    assert _active_tab(_form(result))["label"] == "Apply"
    assert provider_setup.get_cliproxy_login("alice") is not None


def test_cancel_clears_the_pending_login() -> None:
    api = FakeCliproxyApi()
    _run(api, "/provider cliproxy claude")
    assert provider_setup.get_cliproxy_login("alice") is not None

    result = _run(api, "/provider cliproxy cancel")

    assert "cancelled" in result.markdown
    assert provider_setup.get_cliproxy_login("alice") is None


def test_pending_login_expires_after_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_setup.start_cliproxy_login("alice", "claude")
    assert provider_setup.get_cliproxy_login("alice") is not None
    real_monotonic = time.monotonic
    monkeypatch.setattr(
        provider_setup.time,
        "monotonic",
        lambda: real_monotonic() + provider_setup.PENDING_SETUP_TTL_SECONDS + 1,
    )
    assert provider_setup.get_cliproxy_login("alice") is None


def test_pending_login_ttl_slides_on_activity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = provider_setup.start_cliproxy_login("alice", "claude")
    first_expiry = record.expires_at
    real_monotonic = time.monotonic
    half_ttl = provider_setup.PENDING_SETUP_TTL_SECONDS * 0.5
    monkeypatch.setattr(
        provider_setup.time, "monotonic", lambda: real_monotonic() + half_ttl
    )
    assert provider_setup.get_cliproxy_login("alice") is record
    assert record.expires_at > first_expiry
    monkeypatch.setattr(
        provider_setup.time,
        "monotonic",
        lambda: real_monotonic() + half_ttl * 2.5,
    )
    assert provider_setup.get_cliproxy_login("alice") is record


def test_stores_are_independent() -> None:
    """A /provider setup chain and a cliproxy login can coexist."""
    provider_setup.start_setup("alice", "openai")
    provider_setup.start_cliproxy_login("alice", "claude")
    assert provider_setup.clear_setup("alice") is True
    assert provider_setup.get_cliproxy_login("alice") is not None
