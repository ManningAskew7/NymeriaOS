"""CLIProxy subscription branch, headless CLIProxy, and reconfigure across the branch boundary.

Split out of the former monolithic test_setup_wizard.py (dev-todo #54).
"""


from __future__ import annotations

from types import SimpleNamespace

import pytest
from nymeria.setup.runner import main as setup_main

from _setup_wizard_helpers import (  # type: ignore[import-not-found]
    _active_auth,
    _cliproxy_first_run,
    _env_line,
    _fake_cliproxy_client,
    _stub_llm,
)


# --- CLIProxy subscription branch ---------------------------------------------


def test_finalize_cliproxy_claude_local_writes_root_url_and_gatekeeper(
    monkeypatch, tmp_path
):
    """The Claude route: anthropic provider, proxy ROOT URL (no /v1), the cpx-
    gatekeeper in ANTHROPIC_API_KEY (never the DIRECT slot), and the management
    endpoint persisted for the backend's /cliproxy routes."""
    calls = _stub_llm(monkeypatch)
    fake = _fake_cliproxy_client(monkeypatch, auth_files=[_active_auth("claude")])
    root = tmp_path / "init"
    rc = setup_main(
        ["--auth-method", "cliproxy_oauth", "--cliproxy-provider", "claude",
         "--cliproxy-management-url", "http://localhost:8318",
         "--cliproxy-management-key", "cpm-secret",
         "--cliproxy-gatekeeper-key", "cpx-gate",
         "--hosting", "local", "--root", str(root), "--non-interactive"]
    )
    assert rc == 0
    content = (root / "config.env").read_text(encoding="utf-8")
    assert _env_line(content, "LLM_PROVIDER") == "anthropic"
    assert _env_line(content, "LLM_BASE_URL") == "http://localhost:8318"
    assert _env_line(content, "LLM_MODEL") == "claude-opus-5"
    assert _env_line(content, "ANTHROPIC_API_KEY") == "cpx-gate"
    assert "ANTHROPIC_DIRECT_API_KEY" not in content
    assert _env_line(content, "CLIPROXY_MANAGEMENT_URL") == "http://localhost:8318"
    assert _env_line(content, "CLIPROXY_MANAGEMENT_KEY") == "cpm-secret"
    # The live test is ONE real completion (#101 entry 2) through the
    # host-reachable proxy, on the chosen model, with the gatekeeper; the old
    # GET /models check passed with a dead credential and is not used.
    assert calls == []
    (probe,) = fake.probes
    assert probe.llm_base_url == "http://localhost:8318"
    assert probe.llm_model == "claude-opus-5"
    assert probe.api_key.get_secret_value() == "cpx-gate"


def test_finalize_cliproxy_codex_full_stack_writes_v1_responses(
    monkeypatch, tmp_path
):
    """The Codex route on the full stack: openai + /v1 + responses, gatekeeper
    in OPENAI_API_KEY, and the backend-facing URLs use the docker network
    alias while the live test used the host URL."""
    calls = _stub_llm(monkeypatch)
    fake = _fake_cliproxy_client(monkeypatch, auth_files=[_active_auth("codex")])
    root = tmp_path / "checkout"
    root.mkdir()
    rc = setup_main(
        ["--auth-method", "cliproxy_oauth", "--cliproxy-provider", "codex",
         "--cliproxy-management-url", "http://localhost:8318",
         "--cliproxy-management-key", "cpm-secret",
         "--cliproxy-gatekeeper-key", "cpx-gate",
         "--hosting", "docker", "--docker-stack", "full",
         "--root", str(root), "--non-interactive"]
    )
    assert rc == 0
    content = (root / ".env.docker").read_text(encoding="utf-8")
    assert _env_line(content, "LLM_PROVIDER") == "openai"
    assert _env_line(content, "LLM_BASE_URL") == "http://cli-proxy-api:8317/v1"
    assert _env_line(content, "LLM_MODEL") == "gpt-5.5"
    assert _env_line(content, "OPENAI_API_MODE") == "responses"
    assert _env_line(content, "OPENAI_API_KEY") == "cpx-gate"
    assert _env_line(content, "CLIPROXY_MANAGEMENT_URL") == "http://cli-proxy-api:8317"
    # Probed from the HOST (the written URL is a container alias).
    assert calls == []
    (probe,) = fake.probes
    assert probe.llm_base_url == "http://localhost:8318/v1"
    assert probe.llm_model == "gpt-5.5"
    assert probe.api_key.get_secret_value() == "cpx-gate"


def test_finalize_cliproxy_slim_docker_uses_host_gateway(monkeypatch, tmp_path):
    _stub_llm(monkeypatch)
    # Grok's auth files report under the xai provider name.
    _fake_cliproxy_client(monkeypatch, auth_files=[_active_auth("xai")])
    root = tmp_path / "checkout"
    root.mkdir()
    rc = setup_main(
        ["--auth-method", "cliproxy_oauth", "--cliproxy-provider", "grok",
         "--cliproxy-management-url", "http://localhost:8318",
         "--cliproxy-management-key", "cpm-secret",
         "--cliproxy-gatekeeper-key", "cpx-gate",
         "--hosting", "docker", "--docker-stack", "slim",
         "--root", str(root), "--non-interactive"]
    )
    assert rc == 0
    content = (root / ".env.docker").read_text(encoding="utf-8")
    assert _env_line(content, "LLM_BASE_URL") == "http://host.docker.internal:8318/v1"
    # responses since the 2026-08-07 kimi/grok pass (grok rides the codex
    # translator family; responses is its passthrough wire).
    assert _env_line(content, "OPENAI_API_MODE") == "responses"
    assert _env_line(content, "LLM_MODEL") == "grok-4.3"


def test_legacy_auth_method_flags_map_to_generic_branch(monkeypatch, tmp_path):
    """--auth-method cliproxy_claude_oauth (desktop back-compat) behaves as the
    generic branch pinned to Claude."""
    _stub_llm(monkeypatch)
    _fake_cliproxy_client(monkeypatch, auth_files=[_active_auth("claude")])
    root = tmp_path / "init"
    rc = setup_main(
        ["--auth-method", "cliproxy_claude_oauth",
         "--cliproxy-management-url", "http://localhost:8318",
         "--cliproxy-management-key", "cpm-secret",
         "--cliproxy-gatekeeper-key", "cpx-gate",
         "--hosting", "local", "--root", str(root), "--non-interactive"]
    )
    assert rc == 0
    content = (root / "config.env").read_text(encoding="utf-8")
    assert _env_line(content, "LLM_PROVIDER") == "anthropic"
    assert _env_line(content, "LLM_BASE_URL") == "http://localhost:8318"


def test_noninteractive_cliproxy_requires_provider_and_endpoint(tmp_path):
    with pytest.raises(SystemExit) as exc:
        setup_main(
            ["--auth-method", "cliproxy_oauth", "--hosting", "local",
             "--root", str(tmp_path / "a"), "--non-interactive"]
        )
    assert "--cliproxy-provider" in str(exc.value)

    with pytest.raises(SystemExit) as exc:
        setup_main(
            ["--auth-method", "cliproxy_oauth", "--cliproxy-provider", "kimi",
             "--hosting", "local", "--root", str(tmp_path / "b"),
             "--non-interactive"]
        )
    assert "--cliproxy-management-url" in str(exc.value)

    with pytest.raises(SystemExit) as exc:
        setup_main(
            ["--auth-method", "cliproxy_oauth", "--cliproxy-provider", "kimi",
             "--cliproxy-management-url", "http://localhost:8318",
             "--hosting", "local", "--root", str(tmp_path / "c"),
             "--non-interactive"]
        )
    # Either the gatekeeper itself or a management key (which lets setup read
    # or mint one) satisfies the branch now.
    assert "--cliproxy-gatekeeper-key" in str(exc.value)
    assert "--cliproxy-management-key" in str(exc.value)


# --- headless CLIProxy: preflight, gatekeeper fill, auth-file import, login --


_CLIPROXY_BASE_ARGS = [
    "--auth-method", "cliproxy_oauth", "--cliproxy-provider", "claude",
    "--cliproxy-management-url", "http://localhost:8318",
    "--cliproxy-management-key", "cpm-secret",
    "--hosting", "local", "--non-interactive",
]


def test_noninteractive_cliproxy_gatekeeper_optional_with_management_key(
    monkeypatch, tmp_path, capsys
):
    """With a management key, setup reads the proxy's existing cpx- key itself."""
    calls = _stub_llm(monkeypatch)
    fake = _fake_cliproxy_client(
        monkeypatch,
        auth_files=[_active_auth("claude")],
        knobs={"api-keys": ["cpx-existing"]},
    )
    root = tmp_path / "init"
    rc = setup_main(_CLIPROXY_BASE_ARGS + ["--root", str(root)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Verified" in out
    content = (root / "config.env").read_text(encoding="utf-8")
    assert _env_line(content, "ANTHROPIC_API_KEY") == "cpx-existing"
    assert calls == []
    assert [p.api_key.get_secret_value() for p in fake.probes] == ["cpx-existing"]


def test_noninteractive_cliproxy_mints_gatekeeper_when_proxy_has_none(
    monkeypatch, tmp_path
):
    _stub_llm(monkeypatch)
    fake = _fake_cliproxy_client(
        monkeypatch,
        auth_files=[_active_auth("claude")],
        knobs={"api-keys": []},
    )
    root = tmp_path / "init"
    rc = setup_main(_CLIPROXY_BASE_ARGS + ["--root", str(root)])
    assert rc == 0
    knob_calls = [c for c in fake.calls if c[0] == "set_config_knob"]
    assert knob_calls and knob_calls[0][1][0] == "api-keys"
    minted_keys = knob_calls[0][1][1]
    assert minted_keys and minted_keys[0].startswith("cpx-")
    content = (root / "config.env").read_text(encoding="utf-8")
    assert _env_line(content, "ANTHROPIC_API_KEY") == minted_keys[0]


def test_noninteractive_cliproxy_preflight_blocks_when_not_logged_in(
    monkeypatch, tmp_path, capsys
):
    """An unauthenticated proxy fails the run with the remedies listed, instead
    of writing a config whose chats are broken."""
    _stub_llm(monkeypatch)
    _fake_cliproxy_client(monkeypatch, auth_files=[])
    root = tmp_path / "init"
    rc = setup_main(
        _CLIPROXY_BASE_ARGS + ["--cliproxy-gatekeeper-key", "cpx-gate",
                               "--root", str(root)]
    )
    out = capsys.readouterr().out
    assert rc == 2
    assert "No active Claude" in out
    assert "--cliproxy-login" in out
    assert "--cliproxy-auth-file" in out
    assert "--skip-llm-test" in out
    assert not (root / "config.env").exists()


def test_noninteractive_cliproxy_preflight_still_blocks_on_backing_off_login(
    monkeypatch, tmp_path, capsys
):
    """#149 boundary: the finalize preflight deliberately stays on the STRICT
    active predicate. A backing-off login skips the pointless re-login
    elsewhere, but proving the credential SERVES is this gate's whole job."""
    _stub_llm(monkeypatch)
    backing_off = _active_auth("claude")
    backing_off["unavailable"] = True
    _fake_cliproxy_client(monkeypatch, auth_files=[backing_off])
    root = tmp_path / "init"
    rc = setup_main(
        _CLIPROXY_BASE_ARGS + ["--cliproxy-gatekeeper-key", "cpx-gate",
                               "--root", str(root)]
    )
    out = capsys.readouterr().out
    assert rc == 2
    assert "No active Claude" in out
    assert not (root / "config.env").exists()


def test_cliproxy_console_login_skips_oauth_for_backing_off_login(
    monkeypatch, tmp_path, capsys
):
    """#149: a present-but-backing-off login must NOT trigger a fresh OAuth
    (the backoff clears on its own); the skip says so honestly."""
    _stub_llm(monkeypatch)
    backing_off = _active_auth("claude", account="max@example.com")
    backing_off["unavailable"] = True
    fake = _fake_cliproxy_client(
        monkeypatch,
        auth_files=[backing_off],
        knobs={"api-keys": ["cpx-existing"]},
    )

    root = tmp_path / "init"
    rc = setup_main(
        _CLIPROXY_BASE_ARGS + ["--cliproxy-login", "--root", str(root)]
    )
    out = capsys.readouterr().out
    assert "Already logged in" in out
    assert "max@example.com" in out
    assert "backing off" in out
    # Both causes named: the self-clearing backoff and the revoked login
    # that presents identically (where a fresh OAuth IS the fix).
    assert "revoked" in out
    assert not [c for c in fake.calls if c[0] == "start_oauth"]
    assert rc == 0


def test_cliproxy_console_login_backing_off_confirm_forces_fresh_oauth(
    monkeypatch, tmp_path, capsys
):
    """The console escape for the revoked-credential case: confirming the
    prompt on a backing-off login runs a real OAuth instead of skipping."""
    import queue as queue_mod

    from nymeria.setup import cliproxy_login as cliproxy_login_mod

    _stub_llm(monkeypatch)
    backing_off = _active_auth("claude", account="max@example.com")
    backing_off["unavailable"] = True
    fake = _fake_cliproxy_client(
        monkeypatch,
        auth_files=[backing_off],
        knobs={"api-keys": ["cpx-existing"]},
        status_script=["wait", "ok"],
        login_lands=_active_auth("claude", account="max@example.com"),
    )
    pasted = queue_mod.Queue()
    pasted.put("http://localhost:54545/callback?code=abc&state=s1")
    monkeypatch.setattr(cliproxy_login_mod, "_start_paste_reader", lambda: pasted)
    monkeypatch.setattr(cliproxy_login_mod, "_browser_launch_blocked", lambda: True)
    monkeypatch.setattr(cliproxy_login_mod, "LOGIN_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(
        cliproxy_login_mod, "_confirm_backoff_relogin", lambda: True
    )

    root = tmp_path / "init"
    rc = setup_main(
        _CLIPROXY_BASE_ARGS + ["--cliproxy-login", "--root", str(root)]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "backing off" in out
    assert [c for c in fake.calls if c[0] == "start_oauth"]
    assert "Logged in to Claude" in out


def test_noninteractive_cliproxy_preflight_warns_on_unreachable_with_gatekeeper(
    monkeypatch, tmp_path, capsys
):
    from nymeria.cliproxy.management_client import CLIProxyUnreachable

    _stub_llm(monkeypatch)
    _fake_cliproxy_client(
        monkeypatch, raise_on_list=CLIProxyUnreachable("proxy down")
    )
    root = tmp_path / "init"
    rc = setup_main(
        _CLIPROXY_BASE_ARGS + ["--cliproxy-gatekeeper-key", "cpx-gate",
                               "--root", str(root)]
    )
    out = capsys.readouterr().out
    # The proxy URL may be backend-facing (a docker alias); with an explicit
    # gatekeeper the run can still produce a working config.
    assert rc == 0
    assert "Could not reach the proxy" in out
    assert "unverified" in out
    assert (root / "config.env").exists()


def test_noninteractive_cliproxy_unreachable_fatal_when_gatekeeper_needed(
    monkeypatch, tmp_path, capsys
):
    from nymeria.cliproxy.management_client import CLIProxyUnreachable

    _stub_llm(monkeypatch)
    _fake_cliproxy_client(
        monkeypatch, raise_on_list=CLIProxyUnreachable("proxy down")
    )
    root = tmp_path / "init"
    rc = setup_main(_CLIPROXY_BASE_ARGS + ["--root", str(root)])
    out = capsys.readouterr().out
    # No gatekeeper flag means the management API must answer; it cannot.
    assert rc == 2
    assert "gatekeeper" in out


def test_noninteractive_cliproxy_skip_llm_test_bypasses_preflight(
    monkeypatch, tmp_path
):
    _stub_llm(monkeypatch)
    fake = _fake_cliproxy_client(
        monkeypatch, raise_on_list=RuntimeError("must not be called")
    )
    root = tmp_path / "init"
    rc = setup_main(
        _CLIPROXY_BASE_ARGS + ["--cliproxy-gatekeeper-key", "cpx-gate",
                               "--root", str(root), "--skip-llm-test"]
    )
    assert rc == 0
    assert fake.calls == []


def test_cliproxy_auth_file_uploads_verifies_and_fixes_claude(
    monkeypatch, tmp_path, capsys
):
    _stub_llm(monkeypatch)
    fake = _fake_cliproxy_client(
        monkeypatch,
        auth_files=[],
        knobs={"api-keys": ["cpx-existing"]},
        login_lands=_active_auth("claude"),
    )
    auth_path = tmp_path / "claude-backup.json"
    auth_path.write_text('{"type": "claude", "refresh_token": "rt"}', "utf-8")
    root = tmp_path / "init"
    rc = setup_main(
        _CLIPROXY_BASE_ARGS + ["--cliproxy-auth-file", str(auth_path),
                               "--root", str(root)]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "Imported claude-backup.json" in out
    uploads = [c for c in fake.calls if c[0] == "upload_auth_file"]
    assert uploads and uploads[0][1][0] == "claude-backup.json"
    # The Claude rollback guard ran against the registered file.
    assert any(c[0] == "ensure_tool_prefix_disabled" for c in fake.calls)
    content = (root / "config.env").read_text(encoding="utf-8")
    assert _env_line(content, "ANTHROPIC_API_KEY") == "cpx-existing"


def test_cliproxy_auth_file_rejects_unverified_upload(monkeypatch, tmp_path, capsys):
    _stub_llm(monkeypatch)
    _fake_cliproxy_client(monkeypatch, auth_files=[], login_lands=None)
    auth_path = tmp_path / "claude-backup.json"
    auth_path.write_text('{"type": "claude"}', "utf-8")
    root = tmp_path / "init"
    rc = setup_main(
        _CLIPROXY_BASE_ARGS + ["--cliproxy-auth-file", str(auth_path),
                               "--root", str(root)]
    )
    out = capsys.readouterr().out
    assert rc == 2
    assert "does not list a file under that name" in out
    assert not (root / "config.env").exists()


def test_cliproxy_console_login_browser_paste_flow(monkeypatch, tmp_path, capsys):
    import queue as queue_mod

    from nymeria.setup import cliproxy_login as cliproxy_login_mod

    _stub_llm(monkeypatch)
    fake = _fake_cliproxy_client(
        monkeypatch,
        auth_files=[],
        knobs={"api-keys": ["cpx-existing"]},
        status_script=["wait", "ok"],
        login_lands=_active_auth("claude", account="max@example.com"),
    )
    pasted = queue_mod.Queue()
    pasted.put("http://localhost:54545/callback?code=abc&state=s1")
    monkeypatch.setattr(cliproxy_login_mod, "_start_paste_reader", lambda: pasted)
    monkeypatch.setattr(cliproxy_login_mod, "_browser_launch_blocked", lambda: True)
    monkeypatch.setattr(cliproxy_login_mod, "LOGIN_POLL_INTERVAL_SECONDS", 0.01)

    root = tmp_path / "init"
    rc = setup_main(
        _CLIPROXY_BASE_ARGS + ["--cliproxy-login", "--root", str(root)]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "https://auth.example/login" in out
    assert "Logged in to Claude" in out and "max@example.com" in out
    callbacks = [c for c in fake.calls if c[0] == "oauth_callback"]
    assert callbacks and callbacks[0][1][1].startswith("http://localhost:54545/")
    content = (root / "config.env").read_text(encoding="utf-8")
    assert _env_line(content, "ANTHROPIC_API_KEY") == "cpx-existing"


def test_cliproxy_console_login_device_flow_polls_only(monkeypatch, tmp_path, capsys):
    from nymeria.setup import cliproxy_login as cliproxy_login_mod

    _stub_llm(monkeypatch)
    fake = _fake_cliproxy_client(
        monkeypatch,
        auth_files=[],
        status_script=["wait", "ok"],
        login_lands=_active_auth("kimi"),
    )
    monkeypatch.setattr(
        cliproxy_login_mod, "_start_paste_reader",
        lambda: pytest.fail("device flows must not read stdin"),
    )
    monkeypatch.setattr(cliproxy_login_mod, "LOGIN_POLL_INTERVAL_SECONDS", 0.01)

    root = tmp_path / "init"
    rc = setup_main(
        ["--auth-method", "cliproxy_oauth", "--cliproxy-provider", "kimi",
         "--cliproxy-management-url", "http://localhost:8318",
         "--cliproxy-management-key", "cpm-secret",
         "--cliproxy-gatekeeper-key", "cpx-gate",
         "--cliproxy-login", "--hosting", "local",
         "--root", str(root), "--non-interactive"]
    )
    assert rc == 0
    assert not any(c[0] == "oauth_callback" for c in fake.calls)
    assert "approve the login" in capsys.readouterr().out


def test_cliproxy_console_login_go_quirk_false_ok(monkeypatch, tmp_path, capsys):
    """The proxy's status endpoint answers ok for unknown/expired sessions, so
    a bare ok with no auth file must fail, not declare success."""
    from nymeria.setup import cliproxy_login as cliproxy_login_mod

    _stub_llm(monkeypatch)
    _fake_cliproxy_client(
        monkeypatch, auth_files=[], status_script=["ok"], login_lands=None
    )
    monkeypatch.setattr(cliproxy_login_mod, "_browser_launch_blocked", lambda: True)
    monkeypatch.setattr(
        cliproxy_login_mod, "_start_paste_reader", lambda: __import__("queue").Queue()
    )

    root = tmp_path / "init"
    rc = setup_main(
        _CLIPROXY_BASE_ARGS + ["--cliproxy-login", "--root", str(root)]
    )
    out = capsys.readouterr().out
    assert rc == 2
    assert "lists no active" in out or "answers ok for unknown" in out


def test_cliproxy_console_login_timeout(monkeypatch, tmp_path, capsys):
    from nymeria.setup import cliproxy_login as cliproxy_login_mod

    _stub_llm(monkeypatch)
    _fake_cliproxy_client(monkeypatch, auth_files=[], status_script=[])
    monkeypatch.setattr(cliproxy_login_mod, "_browser_launch_blocked", lambda: True)
    monkeypatch.setattr(
        cliproxy_login_mod, "_start_paste_reader", lambda: __import__("queue").Queue()
    )
    monkeypatch.setattr(cliproxy_login_mod, "LOGIN_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(cliproxy_login_mod, "LOGIN_TIMEOUT_SECONDS", 0.05)

    root = tmp_path / "init"
    rc = setup_main(
        _CLIPROXY_BASE_ARGS + ["--cliproxy-login", "--root", str(root)]
    )
    out = capsys.readouterr().out
    assert rc == 2
    assert "expired" in out


def test_cliproxy_login_flags_require_non_interactive(tmp_path):
    with pytest.raises(SystemExit) as exc:
        setup_main(["--cliproxy-login", "--root", str(tmp_path / "a")])
    assert "--non-interactive" in str(exc.value)

    with pytest.raises(SystemExit) as exc:
        setup_main(
            ["--cliproxy-auth-file", "x.json", "--root", str(tmp_path / "b")]
        )
    assert "--non-interactive" in str(exc.value)

    with pytest.raises(SystemExit) as exc:
        setup_main(
            ["--cliproxy-login", "--cliproxy-auth-file", "x.json",
             "--root", str(tmp_path / "c"), "--non-interactive"]
        )
    assert "not both" in str(exc.value)

    # The flags are CLIProxy-branch directives; the API-key branch rejects them.
    with pytest.raises(SystemExit) as exc:
        setup_main(
            ["--cliproxy-login", "--provider", "anthropic", "--model", "m",
             "--api-key", "k", "--root", str(tmp_path / "d"),
             "--non-interactive", "--skip-llm-test"]
        )
    assert "cliproxy_oauth" in str(exc.value)


# --- scripted reconfigure across the CLIProxy branch boundary ----------------


def test_provider_flag_pins_api_key_branch():
    from nymeria.onboarding import ProviderAuthMethod
    from nymeria.setup.runner import _build_state, build_parser

    # An explicit --provider (or --api-key) is a statement of the API-key
    # branch: hydrate's CLIProxy inference must not override it (it would
    # write the direct key into the proxy's gatekeeper slot).
    state = _build_state(build_parser().parse_args(["--provider", "anthropic"]))
    assert state.auth_method is ProviderAuthMethod.API_KEY
    assert state.auth_method_explicit is True

    state = _build_state(build_parser().parse_args(["--api-key", "sk-x"]))
    assert state.auth_method_explicit is True

    # Without either, inference stays available (reconfigure of a routed install).
    state = _build_state(build_parser().parse_args([]))
    assert state.auth_method_explicit is False


def test_noninteractive_switch_to_direct_provider_exits_cliproxy_route(
    monkeypatch, tmp_path
):
    """Leaving the subscription branch by flags retires the proxy route: the
    hydrated base URL/API mode came from the route, not the user, and keeping
    them would leave chats silently flowing through the abandoned proxy."""
    root = tmp_path / "init"
    _cliproxy_first_run(monkeypatch, root)
    before = (root / "config.env").read_text(encoding="utf-8")
    assert _env_line(before, "LLM_BASE_URL") == "http://localhost:8318"

    rc = setup_main(
        ["--provider", "anthropic", "--model", "claude-direct",
         "--api-key", "sk-ant-direct", "--root", str(root),
         "--non-interactive", "--skip-llm-test"]
    )
    assert rc == 0
    after = (root / "config.env").read_text(encoding="utf-8")
    assert _env_line(after, "LLM_PROVIDER") == "anthropic"
    assert _env_line(after, "LLM_MODEL") == "claude-direct"
    assert _env_line(after, "ANTHROPIC_DIRECT_API_KEY") == "sk-ant-direct"
    # The proxy route lines retire with the branch.
    assert "LLM_BASE_URL" not in after
    assert "CLIPROXY_MANAGEMENT_URL" not in after
    assert "CLIPROXY_MANAGEMENT_KEY" not in after
    # The abandoned proxy's gatekeeper retires too (#152): it was once left
    # as "inert", but the direct media callers read these slots and would
    # send it to the real vendor.
    assert "ANTHROPIC_API_KEY" not in after


def test_noninteractive_leaving_cliproxy_requires_api_key(monkeypatch, tmp_path):
    """On a branch exit the on-disk key slot holds the cpx- gatekeeper, not a
    provider key, so it must not satisfy the --api-key requirement (for codex
    the gatekeeper sits in OPENAI_API_KEY, the exact slot the relaxed check
    would otherwise accept, making the switch a silent no-op)."""
    root = tmp_path / "init"
    _cliproxy_first_run(monkeypatch, root, provider="codex")

    with pytest.raises(SystemExit) as exc:
        setup_main(
            ["--provider", "openai", "--model", "gpt-5.5", "--root", str(root),
             "--non-interactive", "--skip-llm-test"]
        )
    assert "--api-key" in str(exc.value)


@pytest.mark.parametrize(
    "cliproxy_provider, provider, model, expected",
    [
        # codex keeps its gatekeeper in OPENAI_API_KEY, the openai route's
        # own slot: the case that read as "the wizard lost my key".
        ("codex", "openai", "gpt-5.5",
         "OPENAI_API_KEY holds the CLIProxy route's gatekeeper key, which only "
         "works with that gateway, not with OpenAI. --api-key is required with "
         "--non-interactive (the key for this route)."),
        # The direct anthropic route reads ANTHROPIC_DIRECT_API_KEY, which holds
        # nothing: no key on disk was passed over, so the plain refusal stays.
        ("claude", "anthropic", "claude-direct",
         "--api-key is required with --non-interactive"),
        # A non-cpx key in the proxy's slot (an api-key set by hand on the
        # proxy) is the old route's key, not a gatekeeper hydrate recognised:
        # the "gatekeeper" claim needs that classification, never presence.
        ("codex-own-key", "openai", "gpt-5.5",
         "OPENAI_API_KEY holds the old gateway route's key, which only works "
         "with that gateway, not with OpenAI. --api-key is required with "
         "--non-interactive (the key for this route)."),
    ],
)
def test_noninteractive_leaving_cliproxy_says_when_the_slot_holds_the_gatekeeper(
    monkeypatch, tmp_path, cliproxy_provider, provider, model, expected
):
    """The bare "--api-key is required" read as a wizard that lost the key
    (#434 review, the #433 D5 wording on its sibling path): when the new
    route's slot holds the proxy's gatekeeper, the refusal names the slot and
    why its key does not count, never the key, and writes nothing."""
    root = tmp_path / "init"
    config = root / "config.env"
    if cliproxy_provider == "codex-own-key":
        _cliproxy_first_run(monkeypatch, root, provider="codex")
        text = config.read_text(encoding="utf-8")
        config.write_text(
            text.replace("OPENAI_API_KEY=cpx-gate", "OPENAI_API_KEY=proxy-own-434"),
            encoding="utf-8",
        )
        assert _env_line(config.read_text(encoding="utf-8"), "OPENAI_API_KEY") == (
            "proxy-own-434"
        )
    else:
        _cliproxy_first_run(monkeypatch, root, provider=cliproxy_provider)
    before = config.read_bytes()

    with pytest.raises(SystemExit) as exc:
        setup_main(
            ["--provider", provider, "--model", model, "--root", str(root),
             "--non-interactive", "--skip-llm-test"]
        )

    message = str(exc.value.code)
    assert message == expected
    assert "cpx-gate" not in message and "cpm-secret" not in message
    assert "proxy-own-434" not in message
    assert config.read_bytes() == before


def test_noninteractive_leaving_cliproxy_keeps_a_real_key_already_in_the_new_slot(
    monkeypatch, tmp_path
):
    """Leaving the subscription branch, a real key already in the new route's
    slot is the key for that route: the gatekeeper sits in a different slot
    (ANTHROPIC_API_KEY), so no --api-key is needed and the real key stays
    (#434 delta review). A direct install reconfigured onto CLIProxy keeps
    its direct key exactly this way (#431), so the way back must honour it."""
    root = tmp_path / "init"
    _cliproxy_first_run(monkeypatch, root, provider="claude")
    config = root / "config.env"
    config.write_text(
        config.read_text(encoding="utf-8") + "ANTHROPIC_DIRECT_API_KEY=sk-ant-real-434\n",
        encoding="utf-8",
    )

    rc = setup_main(
        ["--provider", "anthropic", "--model", "claude-direct", "--root", str(root),
         "--non-interactive", "--skip-llm-test"]
    )

    assert rc == 0
    after = config.read_text(encoding="utf-8")
    assert _env_line(after, "LLM_PROVIDER") == "anthropic"
    assert _env_line(after, "LLM_MODEL") == "claude-direct"
    assert _env_line(after, "ANTHROPIC_DIRECT_API_KEY") == "sk-ant-real-434"
    # The abandoned proxy route and its gatekeeper retire as on any exit.
    assert "LLM_BASE_URL" not in after
    assert "ANTHROPIC_API_KEY=" not in after.replace("ANTHROPIC_DIRECT_API_KEY=", "")
    assert "cpx-gate" not in after


def test_noninteractive_keeps_custom_base_url_on_non_cliproxy_install(
    monkeypatch, tmp_path
):
    """The branch-exit clearing needs hydrate's second signal: a direct-key
    install pointing at some unrelated 8318 endpoint keeps its base URL."""
    _stub_llm(monkeypatch)
    root = tmp_path / "init"
    assert setup_main(
        ["--provider", "openai", "--model", "m", "--api-key", "sk-x",
         "--base-url", "http://my-ollama-box:8318/v1",
         "--api-mode", "chat_completions",
         "--root", str(root), "--non-interactive", "--skip-llm-test"]
    ) == 0

    rc = setup_main(
        ["--model", "m2", "--root", str(root),
         "--non-interactive", "--skip-llm-test"]
    )
    assert rc == 0
    after = (root / "config.env").read_text(encoding="utf-8")
    assert _env_line(after, "LLM_BASE_URL") == "http://my-ollama-box:8318/v1"
    assert _env_line(after, "LLM_MODEL") == "m2"


def test_noninteractive_reconfigure_ambiguous_cliproxy_provider_skips_prep(
    monkeypatch, tmp_path, capsys
):
    """Every non-claude/codex CLI shares the openai+/v1 route shape, so hydrate
    infers the branch but not the CLI. A plain reconfigure must keep the
    working route untouched instead of demanding --cliproxy-provider."""
    root = tmp_path / "init"
    _cliproxy_first_run(monkeypatch, root, provider="grok", auth_provider="xai")
    before = (root / "config.env").read_text(encoding="utf-8")
    capsys.readouterr()

    fake = _fake_cliproxy_client(
        monkeypatch, raise_on_list=RuntimeError("must not be called")
    )
    rc = setup_main(
        ["--reasoning-effort", "high", "--root", str(root),
         "--non-interactive", "--skip-llm-test"]
    )
    assert rc == 0
    assert fake.calls == []
    after = (root / "config.env").read_text(encoding="utf-8")
    assert _env_line(after, "LLM_BASE_URL") == _env_line(before, "LLM_BASE_URL")
    assert _env_line(after, "LLM_REASONING_EFFORT") == "high"


def test_noninteractive_scoped_non_llm_section_skips_llm_checks(
    monkeypatch, tmp_path, capsys
):
    """A scoped jump to a non-LLM section edits only that section: no login
    preflight (a network call) and no LLM required-flag checks."""
    root = tmp_path / "init"
    _cliproxy_first_run(monkeypatch, root)
    capsys.readouterr()

    fake = _fake_cliproxy_client(
        monkeypatch, raise_on_list=RuntimeError("must not be called")
    )
    rc = setup_main(
        ["tts", "--tts", "none", "--root", str(root), "--non-interactive"]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert fake.calls == []
    assert "Updated the tts settings" in out


def test_noninteractive_lenient_preflight_warns_on_plain_reconfigure(
    monkeypatch, tmp_path, capsys
):
    """A reconfigure that does not touch the subscription must not be blocked
    by a lapsed login: the preflight downgrades to a warning."""
    root = tmp_path / "init"
    _cliproxy_first_run(monkeypatch, root)
    capsys.readouterr()

    _fake_cliproxy_client(monkeypatch, auth_files=[])  # login lapsed
    rc = setup_main(
        ["--reasoning-effort", "high", "--root", str(root), "--non-interactive"]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "No active Claude" in out
    assert "Continuing anyway" in out
    after = (root / "config.env").read_text(encoding="utf-8")
    assert _env_line(after, "LLM_REASONING_EFFORT") == "high"


def test_family_flag_empty_value_rejected():
    from nymeria.setup.runner import _build_state, build_parser

    with pytest.raises(SystemExit) as exc:
        _build_state(build_parser().parse_args(["--web-search", ""]))
    assert "empty value" in str(exc.value)
    assert "none" in str(exc.value)


def test_hydrate_infers_cliproxy_branch_from_base_url(monkeypatch, tmp_path):
    from nymeria.onboarding import ProviderAuthMethod
    from nymeria.setup.hydrate import hydrate_state_from_disk
    from nymeria.setup.state import WizardState

    _stub_llm(monkeypatch)
    _fake_cliproxy_client(monkeypatch, auth_files=[_active_auth("claude")])
    root = tmp_path / "init"
    setup_main(
        ["--auth-method", "cliproxy_oauth", "--cliproxy-provider", "claude",
         "--cliproxy-management-url", "http://localhost:8318",
         "--cliproxy-management-key", "cpm-secret",
         "--cliproxy-gatekeeper-key", "cpx-gate",
         "--hosting", "local", "--root", str(root), "--non-interactive"]
    )

    state = WizardState(root=root)
    assert hydrate_state_from_disk(state) is True
    assert state.auth_method is ProviderAuthMethod.CLIPROXY_OAUTH
    assert state.cliproxy_provider == "claude"
    assert state.cliproxy_management_url == "http://localhost:8318"
    # The secret is presence-only, never read back into state.
    assert "CLIPROXY_MANAGEMENT_KEY" in state.present_env_keys
    assert state.cliproxy_management_key == ""


def test_hydrate_infers_codex_from_v1_responses_shape(monkeypatch, tmp_path):
    from nymeria.onboarding import ProviderAuthMethod
    from nymeria.setup.hydrate import hydrate_state_from_disk
    from nymeria.setup.state import WizardState

    _stub_llm(monkeypatch)
    _fake_cliproxy_client(monkeypatch, auth_files=[_active_auth("codex")])
    root = tmp_path / "init"
    setup_main(
        ["--auth-method", "cliproxy_oauth", "--cliproxy-provider", "codex",
         "--cliproxy-management-url", "http://localhost:8318",
         "--cliproxy-management-key", "cpm-secret",
         "--cliproxy-gatekeeper-key", "cpx-gate",
         "--hosting", "local", "--root", str(root), "--non-interactive"]
    )

    state = WizardState(root=root)
    assert hydrate_state_from_disk(state) is True
    assert state.auth_method is ProviderAuthMethod.CLIPROXY_OAUTH
    assert state.cliproxy_provider == "codex"


def test_hydrate_explicit_api_key_flag_wins_over_inference(monkeypatch, tmp_path):
    from nymeria.onboarding import ProviderAuthMethod
    from nymeria.setup.hydrate import hydrate_state_from_disk
    from nymeria.setup.state import WizardState

    _stub_llm(monkeypatch)
    _fake_cliproxy_client(monkeypatch, auth_files=[_active_auth("claude")])
    root = tmp_path / "init"
    setup_main(
        ["--auth-method", "cliproxy_oauth", "--cliproxy-provider", "claude",
         "--cliproxy-management-url", "http://localhost:8318",
         "--cliproxy-management-key", "cpm-secret",
         "--cliproxy-gatekeeper-key", "cpx-gate",
         "--hosting", "local", "--root", str(root), "--non-interactive"]
    )

    state = WizardState(root=root, auth_method_explicit=True)
    assert hydrate_state_from_disk(state) is True
    assert state.auth_method is ProviderAuthMethod.API_KEY


def test_cliproxy_untouched_reconfigure_rewrites_identical_route(
    monkeypatch, tmp_path
):
    """An untouched reconfigure re-derives the same route lines (deterministic
    from pick + shape), so the merge is byte-stable for the LLM block. The
    route must be RE-DERIVED, not accidentally preserved by a no-op write, so
    this also pins that the provider survives hydration (the keep-existing-key
    check must see the gatekeeper slot, ANTHROPIC_API_KEY, as present)."""
    from rich.console import Console

    from nymeria.setup.finalize import finalize as finalize_fn
    from nymeria.setup.hydrate import hydrate_state_from_disk
    from nymeria.setup.state import WizardState

    _stub_llm(monkeypatch)
    _fake_cliproxy_client(monkeypatch, auth_files=[_active_auth("claude")])
    root = tmp_path / "init"
    setup_main(
        ["--auth-method", "cliproxy_oauth", "--cliproxy-provider", "claude",
         "--cliproxy-management-url", "http://localhost:8318",
         "--cliproxy-management-key", "cpm-secret",
         "--cliproxy-gatekeeper-key", "cpx-gate",
         "--hosting", "local", "--root", str(root), "--non-interactive"]
    )
    before = (root / "config.env").read_text(encoding="utf-8")

    state = WizardState(root=root, skip_llm_test=True)
    assert hydrate_state_from_disk(state) is True
    # The branch must survive hydration as a configured provider, not be
    # downgraded because the gatekeeper slot was not recorded as present.
    assert "ANTHROPIC_API_KEY" in state.present_env_keys
    rc = finalize_fn(
        state, console=Console(quiet=True), non_interactive=True, merge=True
    )
    assert rc == 0
    after = (root / "config.env").read_text(encoding="utf-8")
    for key in ("LLM_PROVIDER", "LLM_BASE_URL", "LLM_MODEL",
                "ANTHROPIC_API_KEY", "CLIPROXY_MANAGEMENT_URL",
                "CLIPROXY_MANAGEMENT_KEY"):
        assert _env_line(after, key) == _env_line(before, key), key


def test_cliproxy_claude_reconfigure_applies_a_model_change(monkeypatch, tmp_path):
    """Regression: a Claude-subscription reconfigure must apply edits instead
    of silently downgrading to 'no provider' (the gatekeeper lives in the
    SECOND Anthropic key slot, which present-key recording must cover)."""
    from rich.console import Console

    from nymeria.setup.finalize import finalize as finalize_fn
    from nymeria.setup.hydrate import hydrate_state_from_disk
    from nymeria.setup.state import WizardState

    _stub_llm(monkeypatch)
    _fake_cliproxy_client(monkeypatch, auth_files=[_active_auth("claude")])
    root = tmp_path / "init"
    setup_main(
        ["--auth-method", "cliproxy_oauth", "--cliproxy-provider", "claude",
         "--cliproxy-management-url", "http://localhost:8318",
         "--cliproxy-management-key", "cpm-secret",
         "--cliproxy-gatekeeper-key", "cpx-gate",
         "--hosting", "local", "--root", str(root), "--non-interactive"]
    )

    state = WizardState(root=root, skip_llm_test=True)
    assert hydrate_state_from_disk(state) is True
    state.model = "claude-sonnet-4-6"
    rc = finalize_fn(
        state, console=Console(quiet=True), non_interactive=True, merge=True
    )
    assert rc == 0
    after = (root / "config.env").read_text(encoding="utf-8")
    assert _env_line(after, "LLM_MODEL") == "claude-sonnet-4-6"
    assert _env_line(after, "LLM_PROVIDER") == "anthropic"
    assert _env_line(after, "ANTHROPIC_API_KEY") == "cpx-gate"


def test_hydrate_does_not_infer_cliproxy_from_port_alone(monkeypatch, tmp_path):
    """A direct-key install pointing at some unrelated 8318 endpoint must stay
    on the API-key branch: the port heuristic needs a second signal (cliproxy
    hostname or a recorded management URL)."""
    from nymeria.onboarding import ProviderAuthMethod
    from nymeria.setup.hydrate import hydrate_state_from_disk
    from nymeria.setup.state import WizardState

    _stub_llm(monkeypatch)
    root = tmp_path / "init"
    setup_main(
        ["--provider", "openai", "--model", "m", "--api-key", "sk-x",
         "--base-url", "http://my-ollama-box:8318/v1",
         "--hosting", "local", "--root", str(root), "--non-interactive"]
    )

    state = WizardState(root=root)
    assert hydrate_state_from_disk(state) is True
    assert state.auth_method is ProviderAuthMethod.API_KEY
    assert state.cliproxy_provider is None


def test_switching_back_to_api_key_retires_management_lines(monkeypatch, tmp_path):
    """Leaving the subscription branch must retire CLIPROXY_MANAGEMENT_* so the
    backend's /cliproxy routes stop pointing at an abandoned proxy."""
    from rich.console import Console

    from nymeria.onboarding import ProviderAuthMethod
    from nymeria.setup.finalize import finalize as finalize_fn
    from nymeria.setup.hydrate import hydrate_state_from_disk
    from nymeria.setup.state import WizardState

    _stub_llm(monkeypatch)
    _fake_cliproxy_client(monkeypatch, auth_files=[_active_auth("claude")])
    root = tmp_path / "init"
    setup_main(
        ["--auth-method", "cliproxy_oauth", "--cliproxy-provider", "claude",
         "--cliproxy-management-url", "http://localhost:8318",
         "--cliproxy-management-key", "cpm-secret",
         "--cliproxy-gatekeeper-key", "cpx-gate",
         "--hosting", "local", "--root", str(root), "--non-interactive"]
    )

    state = WizardState(root=root, skip_llm_test=True)
    assert hydrate_state_from_disk(state) is True
    state.auth_method = ProviderAuthMethod.API_KEY
    state.auth_method_explicit = True
    state.provider = "openai"
    state.model = "gpt-5.5"
    state.api_key = "sk-direct"
    state.base_url = ""
    rc = finalize_fn(
        state, console=Console(quiet=True), non_interactive=True, merge=True
    )
    assert rc == 0
    after = (root / "config.env").read_text(encoding="utf-8")
    assert "CLIPROXY_MANAGEMENT_URL" not in after
    assert "CLIPROXY_MANAGEMENT_KEY" not in after
    # The proxy base URL retires with the branch (it described the route).
    assert "LLM_BASE_URL" not in after
    assert _env_line(after, "LLM_PROVIDER") == "openai"


def test_finalize_errors_when_login_succeeded_but_no_gatekeeper(monkeypatch, tmp_path):
    """A completed OAuth login with no usable gatekeeper must hard-stop, not
    write a silent no-provider config behind a yellow note."""
    from rich.console import Console

    from nymeria.onboarding import HostingOption, ProviderAuthMethod
    from nymeria.setup.finalize import finalize as finalize_fn
    from nymeria.setup.state import WizardState

    _stub_llm(monkeypatch)
    state = WizardState(
        hosting=HostingOption.LOCAL,
        auth_method=ProviderAuthMethod.CLIPROXY_OAUTH,
        cliproxy_provider="claude",
        cliproxy_management_url="http://localhost:8318",
        cliproxy_management_key="cpm-secret",
        cliproxy_logged_in=True,
        root=tmp_path / "init",
        skip_llm_test=True,
    )
    rc = finalize_fn(state, console=Console(quiet=True), non_interactive=True)
    assert rc == 2


def test_read_existing_secrets_round_trips_a_deployment(tmp_path):
    """A provisioning re-run must reuse the original secrets (the container
    already bcrypt-hashed the first management secret)."""
    from nymeria.setup.cliproxy_deploy import (
        generate_cliproxy_deployment,
        read_existing_secrets,
    )

    generate_cliproxy_deployment(
        tmp_path / "cliproxy",
        management_secret="cpm-original",
        gatekeeper_key="cpx-original",
    )
    secret, gatekeeper = read_existing_secrets(tmp_path / "cliproxy")
    assert secret == "cpm-original"
    assert gatekeeper == "cpx-original"
    assert read_existing_secrets(tmp_path / "nope") == (None, None)


def test_generate_cliproxy_deployment_writes_pinned_files(tmp_path):
    from nymeria.setup.cliproxy_deploy import (
        CLIPROXY_PINNED_IMAGE,
        generate_cliproxy_deployment,
    )

    deployment = generate_cliproxy_deployment(
        tmp_path / "cliproxy",
        management_secret="cpm-test",
        gatekeeper_key="cpx-test",
        join_network="nymeria_edge",
    )
    compose = (tmp_path / "cliproxy" / "docker-compose.yml").read_text()
    config = (tmp_path / "cliproxy" / "config.yaml").read_text()
    assert CLIPROXY_PINNED_IMAGE in compose
    # Loopback-bound by default: Docker-published ports bypass UFW, so the
    # proxy (OAuth subscriptions + management API) must not be published on
    # all interfaces unless the deployment shape requires the bridge path.
    assert '"127.0.0.1:8318:8317"' in compose
    assert "nymeria_edge" in compose and "cli-proxy-api" in compose
    assert 'secret-key: "cpm-test"' in config
    assert '"cpx-test"' in config
    secret_file = tmp_path / "cliproxy" / "MANAGEMENT_SECRET.txt"
    assert secret_file.stat().st_mode & 0o777 == 0o600
    assert deployment.management_url == "http://localhost:8318"

    # No external network block when not joining one; still loopback-bound.
    generate_cliproxy_deployment(
        tmp_path / "solo",
        management_secret="cpm-test",
        gatekeeper_key="cpx-test",
    )
    solo = (tmp_path / "solo" / "docker-compose.yml").read_text()
    assert "external" not in solo
    assert '"127.0.0.1:8318:8317"' in solo

    # The single-container backend shape opts out (host.docker.internal
    # traffic arrives on the Docker bridge, which a loopback bind cannot
    # serve); the generated compose carries the firewall warning instead.
    generate_cliproxy_deployment(
        tmp_path / "bridge",
        management_secret="cpm-test",
        gatekeeper_key="cpx-test",
        loopback_only=False,
    )
    bridge = (tmp_path / "bridge" / "docker-compose.yml").read_text()
    assert '"8318:8317"' in bridge
    assert '"127.0.0.1:8318:8317"' not in bridge
    assert "DOCKER-USER" in bridge


def test_provider_trio_drops_on_cliproxy_branch_despite_residual_provider():
    """The CLIProxy branch replaces the provider/connection/model trio.

    Regression for backlog #101 log entry 5: `state.provider` left behind by
    an abandoned API-key pick (or a hydrated reconfigure) resurfaced the
    generic model step mid-CLIProxy-branch, asking for the model a second
    time with a listing that cannot succeed. The trio's `applies` predicates
    must all treat the CLIProxy branch as out of scope; only finalize's
    `_apply_cliproxy_route` may stamp the shared LLM fields.
    """
    from nymeria.onboarding import ProviderAuthMethod
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.model import make_model_step
    from nymeria.setup.steps.provider import make_connection_step, make_provider_step

    residual = WizardState(
        auth_method=ProviderAuthMethod.CLIPROXY_OAUTH, provider="anthropic"
    )
    assert not make_provider_step().applies(residual)
    assert not make_connection_step().applies(residual)
    assert not make_model_step().applies(residual)

    # Legacy per-provider auth values are the same branch.
    legacy = WizardState(
        auth_method=ProviderAuthMethod.CLIPROXY_CLAUDE_OAUTH, provider="anthropic"
    )
    assert not make_model_step().applies(legacy)

    # The API-key branch keeps its trio (connection stays spec-gated).
    direct = WizardState(auth_method=ProviderAuthMethod.API_KEY, provider="anthropic")
    assert make_provider_step().applies(direct)
    assert make_model_step().applies(direct)


# --- fresh-host full stack: the edge network before the stack (#313) ----------


class _FakeDocker:
    """Scripted `subprocess.run` stand-in for the CLIProxy bring-up.

    ``outcomes`` maps a docker verb ("inspect", "create", "up") to
    ``(returncode, stderr)``; every invocation is recorded in ``calls``.
    """

    # `docker network <verb>` and `docker compose <verb>` are different
    # commands that happen to carry their verb in the same slot, so the fake
    # keys on the pair and refuses anything it was not built to answer: a
    # silent returncode 0 for an unrecognised invocation would let a wrong
    # command pass for a right one.
    _VERBS = {
        ("network", "inspect"): "inspect",
        ("network", "create"): "create",
        ("compose", "up"): "up",
    }

    def __init__(self, **outcomes: tuple[int, str]) -> None:
        self.outcomes = outcomes
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        from types import SimpleNamespace

        self.calls.append(list(argv))
        argv = list(argv)
        try:
            verb = self._VERBS[(argv[1], argv[2])]
        except (KeyError, IndexError):
            raise AssertionError(f"unexpected docker invocation: {argv}") from None
        returncode, stderr = self.outcomes.get(verb, (0, ""))
        return SimpleNamespace(returncode=returncode, stdout="", stderr=stderr)


_NETWORK_CREATE = [
    "docker", "network", "create",
    "--label", "com.docker.compose.network=edge",
    "--label", "com.docker.compose.project=nymeria",
    "nymeria_edge",
]


def test_compose_up_creates_the_missing_edge_network_with_compose_labels(
    monkeypatch, tmp_path
):
    """Fresh host: the proxy joins `nymeria_edge` as an external network, so it
    must exist before the stack that normally creates it. It is created with
    compose's own labels so the later `up` adopts it instead of refusing."""
    from nymeria.setup import cliproxy_deploy

    docker = _FakeDocker(inspect=(1, "Error: No such network: nymeria_edge"))
    monkeypatch.setattr(cliproxy_deploy.subprocess, "run", docker)

    ok, detail = cliproxy_deploy.compose_up(tmp_path, join_network="nymeria_edge")

    assert (ok, detail) == (True, "")
    assert docker.calls == [
        ["docker", "network", "inspect", "nymeria_edge"],
        _NETWORK_CREATE,
        ["docker", "compose", "up", "-d"],
    ]


def test_compose_up_leaves_an_existing_edge_network_alone(monkeypatch, tmp_path):
    """Second run (or the stack already up): the network is present, so nothing
    is created and the bring-up proceeds."""
    from nymeria.setup import cliproxy_deploy

    docker = _FakeDocker(inspect=(0, ""))
    monkeypatch.setattr(cliproxy_deploy.subprocess, "run", docker)

    ok, detail = cliproxy_deploy.compose_up(tmp_path, join_network="nymeria_edge")

    assert (ok, detail) == (True, "")
    assert docker.calls == [
        ["docker", "network", "inspect", "nymeria_edge"],
        ["docker", "compose", "up", "-d"],
    ]


def test_compose_up_reports_the_network_command_when_creation_fails(
    monkeypatch, tmp_path
):
    """No Docker permission: the step must hand the operator the exact command
    to run by hand and must not go on to claim the proxy started."""
    from nymeria.setup import cliproxy_deploy

    docker = _FakeDocker(
        inspect=(1, "permission denied while trying to connect to the Docker daemon socket"),
        create=(1, "permission denied while trying to connect to the Docker daemon socket"),
    )
    monkeypatch.setattr(cliproxy_deploy.subprocess, "run", docker)

    ok, detail = cliproxy_deploy.compose_up(tmp_path, join_network="nymeria_edge")

    assert ok is False
    assert "nymeria_edge" in detail
    assert " ".join(_NETWORK_CREATE) in detail
    assert "permission denied" in detail
    # Never reached `docker compose up`: a failed prerequisite is not success.
    assert ["docker", "compose", "up", "-d"] not in docker.calls


def test_compose_up_without_a_joined_network_touches_no_network(monkeypatch, tmp_path):
    """The single-container and native shapes join nothing: no network calls."""
    from nymeria.setup import cliproxy_deploy

    docker = _FakeDocker()
    monkeypatch.setattr(cliproxy_deploy.subprocess, "run", docker)

    assert cliproxy_deploy.compose_up(tmp_path) == (True, "")
    assert docker.calls == [["docker", "compose", "up", "-d"]]


def _run_wizard_login(monkeypatch, *, with_state=False, **fake_kwargs):
    """Drive the TUI login step against the scripted proxy; return the status
    panel's (plain text, markup spans, bottom margin), plus the state when
    ``with_state``. Waits past the transient "Starting"/"Checking" lines."""
    import asyncio

    from textual.widgets import Static

    from nymeria.onboarding import ProviderAuthMethod
    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps import cliproxy as cliproxy_steps

    _fake_cliproxy_client(monkeypatch, knobs={"api-keys": ["cpx-existing"]}, **fake_kwargs)
    monkeypatch.setattr(cliproxy_steps, "_browser_launch_blocked", lambda: True)
    monkeypatch.setattr(cliproxy_steps, "LOGIN_POLL_INTERVAL_SECONDS", 0.01)
    state = WizardState(
        auth_method=ProviderAuthMethod.CLIPROXY_OAUTH,
        cliproxy_provider="claude",
        cliproxy_management_url="http://localhost:8318",
        cliproxy_management_key="cpm-secret",
    )

    async def drive():
        app = SetupWizardApp(state, steps=[cliproxy_steps.make_cliproxy_login_step()])
        async with app.run_test() as pilot:
            for _ in range(100):
                await pilot.pause(0.02)
                panel = app.screen.query_one("#cliproxy-login-status", Static)
                text = str(panel.render())
                if "Starting" not in text and "Checking" not in text:
                    break
            rendered = panel.render()
            spans = " ".join(str(span.style) for span in getattr(rendered, "spans", []))
            return str(rendered), spans, panel.styles.margin.bottom

    result = asyncio.run(drive())
    return (*result, state) if with_state else result


def test_wizard_login_success_is_green_and_set_apart(monkeypatch):
    """#101 entry 3: the success line rendered plain, easy to miss beside the
    OAuth URL block. Both success shapes are green; the panel keeps a blank
    row between the status and the URL block below it."""
    from nymeria.setup.steps.base import SUCCESS

    text, spans, margin = _run_wizard_login(
        monkeypatch, auth_files=[_active_auth("claude", account="max@example.com")]
    )
    assert text.startswith("Already logged in as max@example.com")
    assert "answered a test request" in text
    assert SUCCESS in spans
    assert margin == 1

    text, spans, _ = _run_wizard_login(
        monkeypatch,
        auth_files=[],
        status_script=["ok"],
        login_lands=_active_auth("claude", account="new@example.com"),
    )
    assert text.startswith("Login complete as new@example.com")
    assert SUCCESS in spans


def test_wizard_login_failure_is_not_green_and_proxy_text_is_literal(monkeypatch):
    from nymeria.cliproxy.management_client import CLIProxyManagementError
    from nymeria.setup.steps.base import SUCCESS

    text, spans, _ = _run_wizard_login(
        monkeypatch, raise_on_list=CLIProxyManagementError("bad [bold]gateway[/bold]")
    )
    assert text == "Cannot reach the proxy: bad [bold]gateway[/bold]"
    assert SUCCESS not in spans


# --- #101 entry 2: a listed login is not a working login ----------------------
# The wizard used to say "Already logged in" from the proxy's auth-file LIST,
# and finalize's "Connected" came from a GET /models the proxy answers from its
# own registry: a revoked token passed both. One real completion now decides.

_REJECTED = SimpleNamespace(
    ok=False, status_code=401, message="authentication_error: Invalid authentication credentials"
)
_QUOTA = SimpleNamespace(ok=False, status_code=429, message="rate_limit_error")


def test_wizard_login_with_a_rejected_credential_is_not_logged_in(monkeypatch):
    from nymeria.setup.steps.base import SUCCESS

    for fake_kwargs, prefix in (
        ({"auth_files": [_active_auth("claude", account="max@example.com")]},
         "Already logged in as max@example.com"),
        ({"auth_files": [], "status_script": ["ok"],
          "login_lands": _active_auth("claude", account="new@example.com")},
         "Login complete as new@example.com"),
    ):
        text, spans, _, state = _run_wizard_login(
            monkeypatch, with_state=True, probe=_REJECTED, **fake_kwargs
        )
        assert text.startswith(prefix)
        assert "rejected" in text and "Ctrl+R" in text
        assert SUCCESS not in spans
        # Enter no longer passes the step on a dead login.
        assert state.cliproxy_logged_in is False


def test_wizard_login_inconclusive_check_never_blocks(monkeypatch):
    from nymeria.setup.steps.base import SUCCESS

    text, spans, _, state = _run_wizard_login(
        monkeypatch,
        with_state=True,
        probe=_QUOTA,
        auth_files=[_active_auth("claude", account="max@example.com")],
    )
    assert text.startswith("Already logged in as max@example.com")
    assert "could not confirm" in text.lower()
    assert "Press Enter to continue" in text
    assert SUCCESS not in spans
    assert state.cliproxy_logged_in is True


def test_finalize_stops_when_the_subscription_rejects_the_stored_login(
    monkeypatch, tmp_path, capsys
):
    _stub_llm(monkeypatch)
    _fake_cliproxy_client(
        monkeypatch,
        auth_files=[_active_auth("claude")],
        knobs={"api-keys": ["cpx-existing"]},
        probe=_REJECTED,
    )
    root = tmp_path / "init"
    rc = setup_main(_CLIPROXY_BASE_ARGS + ["--root", str(root)])
    out = " ".join(capsys.readouterr().out.split())
    assert rc == 2
    assert "rejected" in out and "--cliproxy-login" in out
    assert "Connected" not in out
    assert not (root / "config.env").exists()


def test_finalize_proceeds_with_a_note_when_the_check_is_inconclusive(
    monkeypatch, tmp_path, capsys
):
    _stub_llm(monkeypatch)
    _fake_cliproxy_client(
        monkeypatch,
        auth_files=[_active_auth("claude")],
        knobs={"api-keys": ["cpx-existing"]},
        probe=_QUOTA,
    )
    root = tmp_path / "init"
    rc = setup_main(_CLIPROXY_BASE_ARGS + ["--root", str(root)])
    out = " ".join(capsys.readouterr().out.split())
    assert rc == 0
    assert "could not confirm" in out.lower()
    assert "Connected" not in out
    assert (root / "config.env").exists()


def test_finalize_ok_says_the_login_answered_and_skip_skips_it(
    monkeypatch, tmp_path, capsys
):
    _stub_llm(monkeypatch)
    fake = _fake_cliproxy_client(
        monkeypatch, auth_files=[_active_auth("claude")], knobs={"api-keys": ["cpx-existing"]}
    )
    rc = setup_main(
        _CLIPROXY_BASE_ARGS + ["--root", str(tmp_path / "a"), "--model", "claude-sonnet-5"]
    )
    out = " ".join(capsys.readouterr().out.split())
    assert rc == 0
    # The CHOSEN model is what gets exercised, not the spec default.
    assert [p.llm_model for p in fake.probes] == ["claude-sonnet-5"]
    assert "Connected: claude-sonnet-5 (it answered a test request)" in out

    fake.probes.clear()
    rc = setup_main(
        _CLIPROXY_BASE_ARGS + ["--root", str(tmp_path / "b"), "--skip-llm-test"]
    )
    assert rc == 0
    assert fake.probes == []


# --- #101 entry 4: the model picker says what Enter will take -----------------


def test_model_picker_shows_what_enter_takes_and_collect_agrees(monkeypatch):
    """Enter both selects and advances, so the dev left the step unsure which
    model it took. A live "Enter uses: X" line mirrors exactly what collect
    stores: the highlighted row, else the typed id, else the spec default."""
    import asyncio

    from textual.widgets import Static

    from nymeria.onboarding import ProviderAuthMethod
    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps import cliproxy as cliproxy_steps
    from nymeria.setup.widgets import ListItem, SearchableList

    # No management URL: the live model fetch stops at once (the list is fed
    # below as if it had answered).
    state = WizardState(
        auth_method=ProviderAuthMethod.CLIPROXY_OAUTH, cliproxy_provider="claude"
    )

    async def drive() -> list[str]:
        app = SetupWizardApp(state, steps=[cliproxy_steps.make_cliproxy_model_step()])
        seen: list[str] = []
        async with app.run_test() as pilot:
            await pilot.pause()

            def line() -> str:
                return str(app.screen.query_one("#cliproxy-model-pick", Static).render())

            seen.append(line())  # nothing listed yet: the default
            picker = app.screen.query_one(SearchableList)
            picker.set_items(
                [ListItem(value=m, primary=m) for m in ("claude-haiku-4-5", "claude-opus-5", "claude-sonnet-5")]
            )
            await pilot.pause()
            seen.append(line())  # the default is highlighted in the list
            await pilot.press("down", "down")
            await pilot.pause()
            seen.append(line())  # the cursor moved one row
            picker.focus()  # back to the search box
            await pilot.press(*"my-custom-id")
            await pilot.pause()
            seen.append(line())  # no row matches: the typed id
            await pilot.press("enter")
            await pilot.pause()
        return seen

    seen = asyncio.run(drive())
    assert seen == [
        "Enter uses: claude-opus-5",
        "Enter uses: claude-opus-5",
        "Enter uses: claude-sonnet-5",
        "Enter uses: my-custom-id",
    ]
    assert state.model == "my-custom-id"


def test_enter_waits_for_the_login_check(monkeypatch):
    """An early Enter must not carry a login past the step while the check
    that may reject it is still running."""
    import asyncio

    from textual.widgets import Static

    from nymeria.onboarding import ProviderAuthMethod
    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps import cliproxy as cliproxy_steps

    _fake_cliproxy_client(
        monkeypatch,
        knobs={"api-keys": ["cpx-existing"]},
        auth_files=[_active_auth("claude", account="max@example.com")],
    )
    release = asyncio.Event()

    async def slow_check(*_args, **_kwargs):
        await release.wait()
        return "ok", "claude-opus-5"

    monkeypatch.setattr(cliproxy_steps, "verify_login_serves", slow_check)
    state = WizardState(
        auth_method=ProviderAuthMethod.CLIPROXY_OAUTH,
        cliproxy_provider="claude",
        cliproxy_management_url="http://localhost:8318",
        cliproxy_management_key="cpm-secret",
    )

    async def drive() -> tuple[str, bool, str]:
        app = SetupWizardApp(state, steps=[cliproxy_steps.make_cliproxy_login_step()])
        async with app.run_test() as pilot:
            for _ in range(100):
                await pilot.pause(0.02)
                status = str(app.screen.query_one("#cliproxy-login-status", Static).render())
                if "Checking" in status:
                    break
            await pilot.press("enter")
            await pilot.pause()
            error = str(app.screen.query_one("#wizard-error", Static).render())
            held = not app.completed
            release.set()
            await pilot.pause(0.1)
            after = str(app.screen.query_one("#wizard-error", Static).render())
            return status, held, error, after

    status, held, error, after = asyncio.run(drive())
    assert "Checking" in status
    assert held
    assert "Checking the login" in error
    assert after.strip() == ""  # the hold message does not outlive the check


def test_an_unfaked_cliproxy_run_never_reaches_a_real_proxy(monkeypatch, tmp_path, capsys):
    """The suite-wide guard: a test that forgets `_fake_cliproxy_client` gets
    the offline refusal, never a management request to a live proxy (which
    bans an IP after 5 bad attempts)."""
    _stub_llm(monkeypatch)
    import httpx

    class _NoNetwork:
        def __init__(self, *_args, **_kwargs) -> None:
            raise AssertionError("an unfaked CLIProxy test reached httpx")

    # Belt and braces: were the guard ever missing, this fails the run
    # instead of spending one of the live proxy's five bad attempts.
    monkeypatch.setattr(httpx, "AsyncClient", _NoNetwork)
    rc = setup_main(
        _CLIPROXY_BASE_ARGS + ["--cliproxy-gatekeeper-key", "cpx-gate", "--root", str(tmp_path / "x")]
    )
    out = " ".join(capsys.readouterr().out.split())
    # The preflight hits the refusal; finalize's check probes the stub's
    # `.invalid` data plane (never the live proxy), which this test's httpx
    # stand-in refuses too. Both carry on unverified.
    assert "list_auth_files tried to reach the proxy at http://localhost:8318" in out
    assert "Could not confirm the Claude" in out
    assert "an unfaked CLIProxy test reached httpx" in out
    assert rc == 0  # unverifiable, never a block


def test_the_check_uses_the_gatekeeper_being_written(monkeypatch, tmp_path, capsys):
    """A mistyped --cliproxy-gatekeeper-key must fail HERE: probing with the
    proxy's own key would pass and ship a config whose every call 401s (the
    old GET check used the written key; review finding)."""
    _stub_llm(monkeypatch)
    fake = _fake_cliproxy_client(
        monkeypatch, auth_files=[_active_auth("claude")], knobs={"api-keys": ["cpx-proxy"]}
    )
    rc = setup_main(
        _CLIPROXY_BASE_ARGS
        + ["--cliproxy-gatekeeper-key", "cpx-typo", "--root", str(tmp_path / "init")]
    )
    assert rc == 0
    assert [p.api_key.get_secret_value() for p in fake.probes] == ["cpx-typo"]


def test_a_headless_relogin_on_an_installed_system_is_checked(monkeypatch, tmp_path, capsys):
    """The case --cliproxy-login exists for: the gatekeeper is already on
    disk (so the key field is blank), and the check used to be skipped with
    it. A strict run now checks, and a rejection stops before writing."""
    root = tmp_path / "init"
    _cliproxy_first_run(monkeypatch, root)
    before = (root / "config.env").read_text(encoding="utf-8")
    capsys.readouterr()
    fake = _fake_cliproxy_client(
        monkeypatch,
        auth_files=[],
        knobs={"api-keys": ["cpx-gate"]},
        status_script=["ok"],
        login_lands=_active_auth("claude"),
        probe=_REJECTED,
    )
    monkeypatch.setattr("nymeria.setup.cliproxy_login._browser_launch_blocked", lambda: True)
    monkeypatch.setattr("nymeria.setup.cliproxy_login.LOGIN_POLL_INTERVAL_SECONDS", 0.01)
    rc = setup_main(
        ["--cliproxy-login", "--root", str(root), "--non-interactive"]
    )
    out = " ".join(capsys.readouterr().out.split())
    assert len(fake.probes) == 1
    assert "rejected" in out
    assert rc == 2
    assert (root / "config.env").read_text(encoding="utf-8") == before


def test_a_rejection_outside_a_strict_run_is_a_note_not_a_block(monkeypatch, tmp_path):
    """Interactive (the login step already offered Ctrl+R, and the user may
    have chosen Ctrl+S to log in later) and lenient reconfigures write the
    config with the remedy, never strand the run at the end."""
    from rich.console import Console

    from nymeria.onboarding import HostingOption, ProviderAuthMethod
    from nymeria.setup.finalize import finalize as finalize_fn
    from nymeria.setup.state import WizardState

    _stub_llm(monkeypatch)
    fake = _fake_cliproxy_client(
        monkeypatch, auth_files=[_active_auth("claude")], probe=_REJECTED
    )
    root = tmp_path / "init"
    state = WizardState(
        hosting=HostingOption.LOCAL,
        auth_method=ProviderAuthMethod.CLIPROXY_OAUTH,
        cliproxy_provider="claude",
        cliproxy_management_url="http://localhost:8318",
        cliproxy_management_key="cpm-secret",
        cliproxy_gatekeeper_key="cpx-gate",
        cliproxy_logged_in=False,  # the user skipped the login step
        root=root,
    )
    console = Console(record=True, width=200)
    rc = finalize_fn(state, console=console, non_interactive=False)
    out = console.export_text()
    assert len(fake.probes) == 1
    assert "rejected the proxy's stored login" in out
    assert "Writing the config anyway" in out
    # P1 (#101 entry 6): the re-run command reaches THIS root, not the default.
    assert f"re-run `nymeria --root {root} init`" in " ".join(out.split())
    assert rc == 0
    assert (root / "config.env").exists()


def test_the_credential_check_does_not_load_the_ml_stack():
    """`nymeria init` imports the API layer for the check (#101 entry 2). The
    settings router once imported the LLM providers module at module scope,
    which drags in torch and transformers (about 8 s cold) and froze the
    wizard's TUI mid-check."""
    import subprocess
    import sys

    code = (
        "import sys; import nymeria.api.routers.cliproxy, nymeria.api.routers.settings; "
        "heavy = [m for m in ('torch', 'transformers', "
        "'nymeria.vendor.react_agent.providers') if m in sys.modules]; "
        "print(heavy); sys.exit(1 if heavy else 0)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stdout + result.stderr
