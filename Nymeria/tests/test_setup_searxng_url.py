"""`nymeria init` refuses a SearXNG base URL that web_search_searxng cannot use.

A scheme-less or malformed value (``searxng:8080``, ``localhost:8080``,
``user:pw@127.0.0.1:9``) used to be written as given: the install "succeeded"
and every search then failed (httpx refuses the protocol), with only `nymeria
doctor` noticing. The rule is the one doctor applies,
``core/searxng_health.base_url_problem``; this file pins the wizard's two doors
(the ``--searxng-base-url`` flag, the backend-keys step) and the rule's table.

The refusal never quotes the value: a scheme-less value parses with its user as
the scheme, so credentials in it cannot be found to redact.

Edges skipped on purpose: a hydrated (reconfigure) value is not judged by init
(a lenient reconfigure of another section must not die over it; doctor flags
it), and the other URL inputs (LLM base URL, CLIProxy, public URL) keep their
own handling (reasons in the It43 shipped note, shipped/08).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from dotenv import dotenv_values

from _setup_wizard_helpers import _stub_llm  # type: ignore[import-not-found]
from nymeria.setup.runner import main as setup_main

_SHAPE = "http://host:port"

# (value, reason) for values httpx cannot use (measured 2026-10-02 against the
# pinned httpx: UnsupportedProtocol, InvalidURL, or a DNS lookup of garbage).
_UNUSABLE = [
    ("searxng:8080", "it does not start with http:// or https://"),
    ("localhost:8080", "it does not start with http:// or https://"),
    ("127.0.0.1:8080", "it does not start with http:// or https://"),
    ("sx-user:s3cret@127.0.0.1:9", "it does not start with http:// or https://"),
    ("ftp://sx-user:s3cret@searx.example", "it does not start with http:// or https://"),
    ("//searx.example:8080", "it does not start with http:// or https://"),
    ("http:///s3cret", "it has no host"),
    ("http://:8080", "it has no host"),
    ("http://sx-user:s3cret@[bad", "it is not a well-formed URL"),
    ("http://sx-user:s3cret@127.0.0.1:99999", "its port is not a number from 1 to 65535"),
    ("http://searx.example:s3cret", "its port is not a number from 1 to 65535"),
    ("http://searx.example:0", "its port is not a number from 1 to 65535"),
    ("http://my searx:8080", "its host contains whitespace"),
    ("http://searx.example/\x1b[2Js3cret", "it contains a control character"),
    ("http://searx\n.example", "it contains a control character"),
]
_UNUSABLE_IDS = [
    "schemeless-service", "schemeless-localhost", "schemeless-ip",
    "schemeless-credentials", "non-http-scheme", "scheme-relative", "no-host",
    "port-only", "bad-bracket", "port-out-of-range", "port-not-a-number",
    "port-zero", "space-in-host", "escape-sequence", "newline",
]

# Values that work today and must keep working: a path (SearXNG behind a
# prefix), userinfo (basic auth in front of it), an IPv6 literal, any scheme
# case, and surrounding whitespace (stripped).
_USABLE = [
    "http://searxng:8080",
    "https://searx.example/searx",
    "http://sx-user:s3cret@127.0.0.1:8888",
    "http://[::1]:8888",
    "HTTP://Searx.Example:8080",
    "https://searx.example:443/?unused=1",
]


# --- the shared rule ------------------------------------------------------------


@pytest.mark.parametrize(("value", "reason"), _UNUSABLE, ids=_UNUSABLE_IDS)
def test_base_url_problem_names_the_reason_without_the_value(value, reason) -> None:
    from nymeria.core.searxng_health import base_url_problem

    assert base_url_problem(value) == reason
    # The reasons are fixed clauses, so nothing of the value can ride along.
    assert "s3cret" not in base_url_problem(value)  # type: ignore[operator]


@pytest.mark.parametrize("value", _USABLE)
def test_base_url_problem_accepts_what_httpx_can_use(value) -> None:
    from nymeria.core.searxng_health import base_url_problem

    assert base_url_problem(value) is None
    assert base_url_problem(f"  {value}\t") is None


@pytest.mark.parametrize("value", ["", "   ", None])
def test_base_url_problem_calls_a_blank_value_empty(value) -> None:
    from nymeria.core.searxng_health import base_url_problem

    assert base_url_problem(value) == "it is empty"  # type: ignore[arg-type]


# --- the flag door --------------------------------------------------------------


def _flag_run(root: Path, url: str, *extra: str) -> int:
    return setup_main(
        [
            "--provider", "anthropic",
            "--model", "claude-test-model",
            "--api-key", "sk-ant-test-key",
            "--web-search", "web_search_searxng",
            "--searxng-base-url", url,
            "--root", str(root),
            "--no-server-browser",
            "--skip-llm-test",
            *extra,
        ]
    )


@pytest.mark.parametrize(("value", "reason"), _UNUSABLE, ids=_UNUSABLE_IDS)
def test_searxng_base_url_flag_refuses_an_unusable_value_before_writing(
    monkeypatch, tmp_path, value, reason
) -> None:
    _stub_llm(monkeypatch)
    root = tmp_path / "runtime"

    with pytest.raises(SystemExit) as exc:
        _flag_run(root, value, "--non-interactive")

    message = str(exc.value.code)
    assert message == (
        f"--searxng-base-url is not usable: {reason}. Expected the SearXNG "
        f"address as {_SHAPE}, for example http://localhost:8080"
    )
    assert "s3cret" not in message and "sx-user" not in message
    # Refused before anything touched disk: no runtime root, no config file.
    assert not root.exists()


def test_searxng_base_url_flag_is_refused_before_the_interactive_wizard_too(
    monkeypatch, tmp_path
) -> None:
    # The flag is judged where every flag is parsed, so an interactive run
    # given a bad value stops before the terminal check (which would otherwise
    # answer "needs an interactive terminal" under pytest and return 2).
    _stub_llm(monkeypatch)
    root = tmp_path / "runtime"

    with pytest.raises(SystemExit) as exc:
        _flag_run(root, "sx-user:s3cret@searxng:8080")

    message = str(exc.value.code)
    assert message.startswith("--searxng-base-url is not usable: it does not start with")
    assert "s3cret" not in message
    assert not root.exists()


@pytest.mark.parametrize("value", _USABLE)
def test_searxng_base_url_flag_writes_a_usable_value(monkeypatch, tmp_path, value) -> None:
    _stub_llm(monkeypatch)
    root = tmp_path / "runtime"

    rc = _flag_run(root, f" {value} ", "--non-interactive")

    assert rc == 0
    written = dotenv_values(root / "config.env")
    assert written["SEARXNG_BASE_URL"] == value


@pytest.mark.parametrize("value", ["", "   "], ids=["empty", "whitespace"])
def test_searxng_base_url_flag_left_blank_is_ignored(monkeypatch, tmp_path, value) -> None:
    # As for every optional flag: blank means "not given", never a refusal.
    _stub_llm(monkeypatch)
    root = tmp_path / "runtime"

    rc = _flag_run(root, value, "--non-interactive")

    assert rc == 0
    written = dotenv_values(root / "config.env")
    assert written.get("LLM_PROVIDER") == "anthropic"
    assert "SEARXNG_BASE_URL" not in written


# --- the interactive door (backend-keys step) -------------------------------------


_FIELD = "#backend-key-searxng_base_url"
_TAVILY_FIELD = "#backend-key-tavily_api_key"


def _drive_backend_keys(entries: list[str]):
    """Fill the step's two fields (a Tavily key, then the SearXNG URL) and
    press Enter once per SearXNG entry, recording what the step showed after
    each: (advanced, error text, SearXNG field focused, what optional_env
    holds).

    Focus is cleared before each Enter so the step's own refusal is what puts
    the cursor back on the offending field, and the key field sits BEFORE the
    URL so a step that recorded fields as it went would have stored the key.
    """
    from textual.widgets import Input, Static

    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.backend_keys import make_backend_keys_step

    seen: list[tuple[bool, str, bool, dict[str, str]]] = []

    async def drive() -> None:
        state = WizardState(
            extras={"web_search": ["web_search_tavily", "web_search_searxng"]}
        )
        app = SetupWizardApp(state, steps=[make_backend_keys_step()])
        async with app.run_test() as pilot:
            await pilot.pause()
            for entry in entries:
                if app.completed:
                    break
                scr = app.screen
                scr.query_one(_TAVILY_FIELD, Input).value = "tav-key"
                scr.query_one(_FIELD, Input).value = entry
                scr.set_focus(None)
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                stored = dict(state.optional_env)
                if app.completed:
                    # The one-step app finished: the step accepted the entry.
                    seen.append((True, "", False, stored))
                    continue
                error = scr.query_one("#wizard-error", Static)
                seen.append(
                    (
                        False,
                        str(error.render()) if error.display else "",
                        scr.query_one(_FIELD, Input).has_focus,
                        stored,
                    )
                )

    asyncio.run(drive())
    return seen


def test_backend_keys_step_reasks_for_an_unusable_searxng_url_then_takes_a_fixed_one() -> None:
    seen = _drive_backend_keys(["searxng:8080", "http://localhost:8080"])

    assert len(seen) == 2
    advanced, error, focused, stored = seen[0]
    assert not advanced  # stayed on the step: the user is asked again
    assert error == (
        "SearXNG base URL is not usable: it does not start with http:// or "
        f"https://. Enter it as {_SHAPE} (for example http://localhost:8080), or "
        "leave it blank to add it later."
    )
    assert focused  # the refusal put the cursor on the offending field
    assert stored == {}  # nothing recorded for finalize, not even the valid key
    assert seen[1] == (
        True,
        "",
        False,
        {"TAVILY_API_KEY": "tav-key", "SEARXNG_BASE_URL": "http://localhost:8080"},
    )


def test_backend_keys_step_never_shows_credentials_from_an_unusable_url() -> None:
    seen = _drive_backend_keys(["sx-user:s3cret@127.0.0.1:9"])

    advanced, error, _focused, stored = seen[0]
    assert not advanced
    assert error.startswith("SearXNG base URL is not usable: it does not start with")
    assert "s3cret" not in error and "sx-user" not in error
    assert stored == {}


def test_backend_keys_step_still_advances_with_the_searxng_url_left_blank() -> None:
    seen = _drive_backend_keys([""])

    assert seen == [(True, "", False, {"TAVILY_API_KEY": "tav-key"})]
