"""CLIProxy subscription branch, headless CLIProxy, and reconfigure across the branch boundary.

Split out of the former monolithic test_setup_wizard.py (dev-todo #54).
"""


from __future__ import annotations

import pytest
from nymeria.setup.runner import main as setup_main

from _setup_wizard_helpers import (
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
    _fake_cliproxy_client(monkeypatch, auth_files=[_active_auth("claude")])
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
    assert _env_line(content, "LLM_MODEL") == "claude-opus-4-7"
    assert _env_line(content, "ANTHROPIC_API_KEY") == "cpx-gate"
    assert "ANTHROPIC_DIRECT_API_KEY" not in content
    assert _env_line(content, "CLIPROXY_MANAGEMENT_URL") == "http://localhost:8318"
    assert _env_line(content, "CLIPROXY_MANAGEMENT_KEY") == "cpm-secret"
    # The live test ran against the host-reachable proxy with the gatekeeper.
    assert calls and calls[0][2] == "cpx-gate"


def test_finalize_cliproxy_codex_full_stack_writes_v1_responses(
    monkeypatch, tmp_path
):
    """The Codex route on the full stack: openai + /v1 + responses, gatekeeper
    in OPENAI_API_KEY, and the backend-facing URLs use the docker network
    alias while the live test used the host URL."""
    calls = _stub_llm(monkeypatch)
    _fake_cliproxy_client(monkeypatch, auth_files=[_active_auth("codex")])
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
    assert calls and calls[0][1] == "gpt-5.5"


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
    assert _env_line(content, "OPENAI_API_MODE") == "chat_completions"
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
    _fake_cliproxy_client(
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
    assert calls and calls[0][2] == "cpx-existing"


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
    assert "lists no active" in out
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
    # Known residue: the old gatekeeper line stays but is inert without the
    # proxy base URL (the direct route reads the DIRECT slot).
    assert _env_line(after, "ANTHROPIC_API_KEY") == "cpx-gate"


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
    assert '"8318:8317"' in compose
    assert "nymeria_edge" in compose and "cli-proxy-api" in compose
    assert 'secret-key: "cpm-test"' in config
    assert '"cpx-test"' in config
    secret_file = tmp_path / "cliproxy" / "MANAGEMENT_SECRET.txt"
    assert secret_file.stat().st_mode & 0o777 == 0o600
    assert deployment.management_url == "http://localhost:8318"

    # No external network block when not joining one.
    generate_cliproxy_deployment(
        tmp_path / "solo",
        management_secret="cpm-test",
        gatekeeper_key="cpx-test",
    )
    solo = (tmp_path / "solo" / "docker-compose.yml").read_text()
    assert "external" not in solo
