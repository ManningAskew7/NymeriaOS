import sys
from pathlib import Path

import pytest

from nymeria import _runtime_paths
from nymeria import setup_wizard
from nymeria.onboarding import (
    HOSTING_CHOICES,
    HOSTING_ORDER,
    NEXT_ACTION_ORDER,
    PROVIDER_AUTH_METHOD_CHOICES,
    PROVIDER_AUTH_METHOD_ORDER,
    SETUP_STYLE_CHOICES,
    SETUP_STYLE_ORDER,
    HostingOption,
    NextAction,
    ProviderAuthMethod,
    SetupStyle,
    choice_values,
    parse_choice,
)
from nymeria.config import settings as settings_module
from nymeria.setup_wizard import main as setup_main


def _stub_llm_connection(monkeypatch):
    calls = []

    def fake_connection(provider, model, api_key):
        calls.append((provider.name, model, api_key))
        return setup_wizard.LLMConnectionResult(model=model)

    monkeypatch.setattr(setup_wizard, "_test_llm_connection", fake_connection)
    return calls


def test_onboarding_data_model_matches_plan_values():
    assert choice_values(HostingOption) == ("bare_metal", "venv", "docker")
    assert choice_values(ProviderAuthMethod) == (
        "api_key",
        "cliproxy_claude_oauth",
        "cliproxy_codex_oauth",
    )
    assert choice_values(SetupStyle) == ("recommended", "advanced")
    assert choice_values(NextAction) == (
        "start_api_open_frontend",
        "print_commands",
        "cli",
    )
    assert HOSTING_ORDER == (
        HostingOption.BARE_METAL,
        HostingOption.VENV,
        HostingOption.DOCKER,
    )
    assert PROVIDER_AUTH_METHOD_ORDER == (
        ProviderAuthMethod.API_KEY,
        ProviderAuthMethod.CLIPROXY_CLAUDE_OAUTH,
        ProviderAuthMethod.CLIPROXY_CODEX_OAUTH,
    )
    assert SETUP_STYLE_ORDER == (SetupStyle.RECOMMENDED, SetupStyle.ADVANCED)
    assert NEXT_ACTION_ORDER == (
        NextAction.START_API_OPEN_FRONTEND,
        NextAction.PRINT_COMMANDS,
        NextAction.CLI,
    )


def test_onboarding_choice_metadata_captures_safety_constraints():
    venv_description = HOSTING_CHOICES[HostingOption.VENV].description
    docker_choice = HOSTING_CHOICES[HostingOption.DOCKER]
    claude_oauth = PROVIDER_AUTH_METHOD_CHOICES[
        ProviderAuthMethod.CLIPROXY_CLAUDE_OAUTH
    ]

    assert "not an OS security sandbox" in venv_description
    assert docker_choice.recommended is True
    assert claude_oauth.advanced is True
    assert SETUP_STYLE_CHOICES[SetupStyle.RECOMMENDED].recommended is True


def test_onboarding_choice_parser_reports_allowed_values():
    result = parse_choice(HostingOption, "docker", option_name="--hosting")

    assert result is HostingOption.DOCKER

    try:
        parse_choice(NextAction, "invalid", option_name="--next-action")
    except ValueError as exc:
        assert str(exc) == (
            "--next-action must be one of: "
            "start_api_open_frontend, print_commands, cli"
        )
    else:
        raise AssertionError("expected invalid onboarding choice to fail")


def test_runtime_bootstrap_uses_source_checkout_when_markers_exist(
    monkeypatch,
    tmp_path: Path,
):
    root = tmp_path / "Nymeria"
    config_dir = root / "nymeria" / "config"
    config_dir.mkdir(parents=True)
    (root / "run.py").write_text("", encoding="utf-8")
    (config_dir / "soul.md").write_text("", encoding="utf-8")

    monkeypatch.delenv("NYMERIA_PROJECT_ROOT", raising=False)

    resolved = _runtime_paths.configure_project_root(root / "nymeria" / "cli_entry.py")

    assert resolved == root.resolve()


def test_runtime_bootstrap_uses_user_root_for_installed_package(
    monkeypatch,
    tmp_path: Path,
):
    fake_entry = tmp_path / "site-packages" / "nymeria" / "cli_entry.py"
    fake_entry.parent.mkdir(parents=True)
    fake_entry.write_text("", encoding="utf-8")
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("NYMERIA_PROJECT_ROOT", raising=False)

    resolved = _runtime_paths.configure_project_root(fake_entry)

    assert resolved == (home / ".nymeria").resolve()


def test_runtime_bootstrap_uses_user_root_for_frozen_build(
    monkeypatch,
    tmp_path: Path,
):
    fake_meipass_entry = tmp_path / "_MEIPASS" / "nymeria" / "_runtime_paths.py"
    fake_config_dir = tmp_path / "_MEIPASS" / "nymeria" / "config"
    fake_config_dir.mkdir(parents=True)
    (tmp_path / "_MEIPASS" / "run.py").write_text("", encoding="utf-8")
    (fake_config_dir / "soul.md").write_text("", encoding="utf-8")
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("NYMERIA_PROJECT_ROOT", raising=False)
    monkeypatch.setattr(_runtime_paths.sys, "frozen", True, raising=False)

    resolved = _runtime_paths.configure_project_root(fake_meipass_entry)

    assert resolved == (home / ".nymeria").resolve()


def test_settings_package_paths_do_not_follow_runtime_project_root(
    monkeypatch,
    tmp_path: Path,
):
    runtime_root = tmp_path / "runtime"
    package_root = tmp_path / "site-packages" / "nymeria"
    monkeypatch.delenv("NYMERIA_DATA_DIR", raising=False)
    monkeypatch.delenv("SQLITE_PATH", raising=False)
    monkeypatch.setattr(settings_module, "PROJECT_ROOT", runtime_root)
    monkeypatch.setattr(settings_module, "PACKAGE_ROOT", package_root)

    settings = settings_module.Settings(_env_file=None)

    assert settings.project_root == runtime_root
    assert settings.data_dir == runtime_root / "data"
    assert settings.soul_path == package_root / "config" / "soul.md"
    assert settings.bundled_skills_dir == package_root / "skills_bundled"


def test_init_noninteractive_writes_config_and_bootstrap_token(
    monkeypatch,
    tmp_path: Path,
):
    llm_calls = _stub_llm_connection(monkeypatch)
    root = tmp_path / "runtime"

    result = setup_main(
        [
            "--provider",
            "anthropic",
            "--model",
            "claude-test-model",
            "--api-key",
            "sk-ant-test-key",
            "--root",
            str(root),
            "--non-interactive",
        ]
    )

    config = (root / "config.env").read_text(encoding="utf-8")
    assert result == 0
    assert "LLM_PROVIDER=anthropic" in config
    assert "LLM_MODEL=claude-test-model" in config
    assert "ANTHROPIC_API_KEY=sk-ant-test-key" in config
    assert f"NYMERIA_DATA_DIR={root / 'data'}" in config
    assert (root / "data" / "accounts.db").exists()
    assert (root / "data" / "BOOTSTRAP_TOKEN.txt").exists()
    assert llm_calls == [("anthropic", "claude-test-model", "sk-ant-test-key")]


def test_init_noninteractive_accepts_stable_onboarding_flags(
    monkeypatch,
    tmp_path: Path,
    capsys,
):
    llm_calls = _stub_llm_connection(monkeypatch)
    root = tmp_path / "runtime"

    result = setup_main(
        [
            "--provider",
            "anthropic",
            "--model",
            "claude-test-model",
            "--api-key",
            "sk-ant-test-key",
            "--hosting",
            "bare_metal",
            "--auth-method",
            "api_key",
            "--setup-style",
            "advanced",
            "--next-action",
            "cli",
            "--root",
            str(root),
            "--non-interactive",
        ]
    )

    config = (root / "config.env").read_text(encoding="utf-8")
    output = capsys.readouterr().out
    assert result == 0
    assert "LLM_PROVIDER=anthropic" in config
    assert "ANTHROPIC_API_KEY=sk-ant-test-key" in config
    assert "nymeria cli" in output
    assert llm_calls == [("anthropic", "claude-test-model", "sk-ant-test-key")]


def test_init_noninteractive_defaults_to_print_commands(
    monkeypatch,
    tmp_path: Path,
    capsys,
):
    _stub_llm_connection(monkeypatch)
    root = tmp_path / "runtime"

    result = setup_main(
        [
            "--provider",
            "anthropic",
            "--model",
            "claude-test-model",
            "--api-key",
            "sk-ant-test-key",
            "--root",
            str(root),
            "--non-interactive",
        ]
    )

    output = capsys.readouterr().out
    assert result == 0
    assert "nymeria api" in output
    assert "nymeria cli" not in output


def test_init_rejects_future_docker_hosting_without_writing_config(
    tmp_path: Path,
    capsys,
):
    root = tmp_path / "runtime"

    result = setup_main(
        [
            "--provider",
            "anthropic",
            "--model",
            "claude-test-model",
            "--api-key",
            "sk-ant-test-key",
            "--hosting",
            "docker",
            "--root",
            str(root),
            "--non-interactive",
            "--skip-llm-test",
        ]
    )

    output = capsys.readouterr().out
    assert result == 2
    assert "--hosting docker" in output
    assert not (root / "config.env").exists()


def test_init_rejects_future_cliproxy_auth_method_without_api_key_prompt(capsys):
    result = setup_main(
        [
            "--auth-method",
            "cliproxy_claude_oauth",
            "--non-interactive",
        ]
    )

    output = capsys.readouterr().out
    assert result == 2
    assert "CLIProxy OAuth onboarding is not implemented yet" in output
    assert "--provider is required" not in output


def test_run_init_parser_accepts_onboarding_flags(monkeypatch):
    import run as run_module

    captured = {}

    def fake_run_init(args):
        captured["args"] = args
        return 0

    monkeypatch.setattr(run_module, "run_init", fake_run_init)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run.py",
            "init",
            "--provider",
            "anthropic",
            "--model",
            "claude-test-model",
            "--api-key",
            "sk-ant-test-key",
            "--hosting",
            "venv",
            "--auth-method",
            "api_key",
            "--setup-style",
            "advanced",
            "--next-action",
            "print_commands",
            "--non-interactive",
            "--skip-llm-test",
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        run_module.main()

    args = captured["args"]
    assert exc_info.value.code == 0
    assert args.hosting == "venv"
    assert args.auth_method == "api_key"
    assert args.setup_style == "advanced"
    assert args.next_action == "print_commands"


def test_init_noninteractive_writes_optional_capability_keys(
    monkeypatch,
    tmp_path: Path,
):
    _stub_llm_connection(monkeypatch)
    root = tmp_path / "runtime"

    result = setup_main(
        [
            "--provider",
            "anthropic",
            "--model",
            "claude-test-model",
            "--api-key",
            "sk-ant-test-key",
            "--embedding-api-key",
            "sk-embedding-test",
            "--openai-api-key",
            "sk-openai-test",
            "--gemini-api-key",
            "gemini-test",
            "--perplexity-api-key",
            "pplx-test",
            "--root",
            str(root),
            "--non-interactive",
        ]
    )

    config = (root / "config.env").read_text(encoding="utf-8")
    assert result == 0
    assert "EMBEDDING_API_KEY=sk-embedding-test" in config
    assert "OPENAI_API_KEY=sk-openai-test" in config
    assert "GEMINI_API_KEY=gemini-test" in config
    assert "PERPLEXITY_API_KEY=pplx-test" in config


def test_init_noninteractive_does_not_duplicate_primary_openai_key(
    monkeypatch,
    tmp_path: Path,
):
    _stub_llm_connection(monkeypatch)
    root = tmp_path / "runtime"

    result = setup_main(
        [
            "--provider",
            "openai",
            "--model",
            "openai-test-model",
            "--api-key",
            "sk-openai-primary",
            "--openai-api-key",
            "sk-openai-primary",
            "--root",
            str(root),
            "--non-interactive",
        ]
    )

    config = (root / "config.env").read_text(encoding="utf-8")
    assert result == 0
    assert config.count("OPENAI_API_KEY=") == 1


def test_init_noninteractive_skip_llm_test_does_not_call_provider(
    monkeypatch,
    tmp_path: Path,
):
    root = tmp_path / "runtime"

    def fail_connection(*args, **kwargs):
        raise AssertionError("LLM connection test should have been skipped")

    monkeypatch.setattr(setup_wizard, "_test_llm_connection", fail_connection)

    result = setup_main(
        [
            "--provider",
            "anthropic",
            "--model",
            "claude-test-model",
            "--api-key",
            "sk-ant-test-key",
            "--root",
            str(root),
            "--non-interactive",
            "--skip-llm-test",
        ]
    )

    assert result == 0
    assert (root / "config.env").exists()


def test_init_noninteractive_stops_when_llm_connection_fails(
    monkeypatch,
    tmp_path: Path,
):
    root = tmp_path / "runtime"

    def fail_connection(*args, **kwargs):
        raise setup_wizard.LLMConnectionError("bad key")

    monkeypatch.setattr(setup_wizard, "_test_llm_connection", fail_connection)

    result = setup_main(
        [
            "--provider",
            "anthropic",
            "--model",
            "claude-test-model",
            "--api-key",
            "sk-ant-test-key",
            "--root",
            str(root),
            "--non-interactive",
        ]
    )

    assert result == 2
    assert not (root / "config.env").exists()


def test_llm_connection_uses_provider_specific_endpoint(monkeypatch):
    calls = []

    def fake_post_json(url, *, headers, json):
        calls.append((url, headers, json))

    monkeypatch.setattr(setup_wizard, "_post_json", fake_post_json)

    result = setup_wizard._test_llm_connection(
        setup_wizard.PROVIDERS["openrouter"],
        "anthropic/claude-test-model",
        "sk-or-test-key",
    )

    assert result == setup_wizard.LLMConnectionResult(
        model="anthropic/claude-test-model"
    )
    assert calls == [
        (
            "https://openrouter.ai/api/v1/responses",
            {
                "Authorization": "Bearer sk-or-test-key",
                "HTTP-Referer": "https://github.com/ManningAskew7/NymeriaOS",
                "X-Title": "Nymeria",
            },
            {
                "model": "anthropic/claude-test-model",
                "input": "Reply with ok.",
                "max_output_tokens": 16,
            },
        )
    ]
