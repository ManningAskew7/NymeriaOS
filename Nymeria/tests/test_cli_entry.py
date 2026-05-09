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
    token_path = root / "data" / "BOOTSTRAP_TOKEN.txt"
    token_file = token_path.read_text(encoding="utf-8")
    raw_token = token_file.split("Token: ", 1)[1].splitlines()[0]
    output = capsys.readouterr().out
    assert token_path.exists()
    assert "Bootstrap token:" in output
    assert str(token_path) in output
    assert "Desktop/Mobile Setup Wizard wants the `nym_...` account token" in output
    assert "not your Anthropic, OpenAI, or OpenRouter provider API key" in output
    assert "shell history" in output
    assert raw_token not in output
    assert llm_calls == [("anthropic", "claude-test-model", "sk-ant-test-key")]


def test_bootstrap_token_copy_command_uses_macos_clipboard(monkeypatch):
    token_path = Path("/tmp/Nymeria Data/BOOTSTRAP_TOKEN.txt")

    monkeypatch.setattr(setup_wizard.sys, "platform", "darwin")

    hint = setup_wizard._bootstrap_token_copy_command(token_path)

    assert hint.copies_to_clipboard is True
    assert "pbcopy" in hint.command
    assert "nym_[A-Za-z0-9_-]+" in hint.command
    assert "BOOTSTRAP_TOKEN.txt" in hint.command


def test_bootstrap_token_copy_command_uses_windows_clipboard(monkeypatch):
    token_path = Path(r"C:\Users\Owner\.nymeria\data\BOOTSTRAP_TOKEN.txt")

    monkeypatch.setattr(setup_wizard.sys, "platform", "win32")

    hint = setup_wizard._bootstrap_token_copy_command(token_path)

    assert hint.copies_to_clipboard is True
    assert "Set-Clipboard" in hint.command
    assert "Select-String" in hint.command
    assert "nym_[A-Za-z0-9_-]+" in hint.command


def test_bootstrap_token_copy_command_falls_back_without_linux_clipboard(monkeypatch):
    token_path = Path("/home/owner/.nymeria/data/BOOTSTRAP_TOKEN.txt")

    monkeypatch.setattr(setup_wizard.sys, "platform", "linux")
    monkeypatch.setattr(setup_wizard.shutil, "which", lambda name: None)

    hint = setup_wizard._bootstrap_token_copy_command(token_path)

    assert hint.copies_to_clipboard is False
    assert hint.command == (
        "grep -oE 'nym_[A-Za-z0-9_-]+' "
        "/home/owner/.nymeria/data/BOOTSTRAP_TOKEN.txt | head -n 1"
    )


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


@pytest.mark.parametrize(
    ("provider_choice", "provider_name", "api_key"),
    [
        ("1", "anthropic", "sk-ant-test-key"),
        ("2", "openai", "sk-openai-test-key"),
        ("3", "openrouter", "sk-or-test-key"),
    ],
)
def test_init_interactive_accepts_provider_default_model(
    monkeypatch,
    tmp_path: Path,
    capsys,
    provider_choice: str,
    provider_name: str,
    api_key: str,
):
    root = tmp_path / "runtime"
    provider = setup_wizard.PROVIDERS[provider_name]
    answers = iter(
        [
            "",  # default hosting: venv
            provider_choice,
            "",  # accept provider default model
            api_key,
        ]
    )
    prompts = []

    def fake_prompt(text="", **kwargs):
        prompts.append(text)
        return next(answers)

    monkeypatch.setattr(setup_wizard, "prompt", fake_prompt)

    result = setup_main(
        [
            "--setup-style",
            "recommended",
            "--root",
            str(root),
            "--skip-llm-test",
        ]
    )

    config = (root / "config.env").read_text(encoding="utf-8")
    output = capsys.readouterr().out
    assert result == 0
    assert f"LLM_PROVIDER={provider_name}" in config
    assert f"LLM_MODEL={provider.default_model}" in config
    assert f"{provider.env_var}={api_key}" in config
    assert f"> [{provider.default_model}] " in prompts
    assert "Recommended default:" in output
    assert provider.default_model in output


def test_init_noninteractive_still_requires_explicit_model(
    monkeypatch,
    tmp_path: Path,
):
    root = tmp_path / "runtime"

    def fail_connection(*args, **kwargs):
        raise AssertionError("LLM connection test should not run without --model")

    monkeypatch.setattr(setup_wizard, "_test_llm_connection", fail_connection)

    with pytest.raises(SystemExit) as exc_info:
        setup_main(
            [
                "--provider",
                "anthropic",
                "--api-key",
                "sk-ant-test-key",
                "--root",
                str(root),
                "--non-interactive",
            ]
        )

    assert str(exc_info.value) == "--model is required with --non-interactive"
    assert not (root / "config.env").exists()


def test_init_interactive_prompts_for_hosting_before_provider(
    monkeypatch,
    tmp_path: Path,
    capsys,
):
    root = tmp_path / "runtime"
    answers = iter(
        [
            "",  # default hosting: venv
            "1",  # provider: Anthropic
            "claude-test-model",
            "sk-ant-test-key",
            "n",
            "n",
            "n",
            "n",
        ]
    )
    prompts = []

    def fake_prompt(text="", **kwargs):
        prompts.append(text)
        return next(answers)

    monkeypatch.setattr(setup_wizard, "prompt", fake_prompt)

    result = setup_main(
        [
            "--root",
            str(root),
            "--skip-llm-test",
        ]
    )

    output = capsys.readouterr().out
    assert result == 0
    assert prompts[0] == "> [2] "
    assert "Step 1/7: Hosting / Security" in output
    assert "Step 2/7: LLM Provider" in output
    assert output.index("Step 1/7: Hosting / Security") < output.index(
        "Step 2/7: LLM Provider"
    )
    assert "not an OS security sandbox" in output
    assert "Docker" in output
    assert (root / "config.env").exists()


def test_init_interactive_docker_hosting_prints_handoff_without_provider_prompt(
    monkeypatch,
    tmp_path: Path,
    capsys,
):
    root = tmp_path / "runtime"
    answers = iter(["3"])
    prompts = []

    def fake_prompt(text="", **kwargs):
        prompts.append(text)
        return next(answers)

    monkeypatch.setattr(setup_wizard, "prompt", fake_prompt)

    result = setup_main(
        [
            "--root",
            str(root),
            "--skip-llm-test",
        ]
    )

    output = capsys.readouterr().out
    assert result == 0
    assert prompts == ["> [2] "]
    assert "Docker Setup" in output
    assert "docker compose" in output
    assert "--env-file .env.docker" in output
    assert "Provider flags, if supplied" in output
    assert "were not" in output
    assert "written" in output
    assert "Step 2/7: LLM Provider" not in output
    assert not (root / "config.env").exists()


def test_init_docker_hosting_prints_handoff_without_writing_config(
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
    assert result == 0
    assert "Docker Setup" in output
    assert "No config.env or .env.docker was written" in output
    assert not (root / "config.env").exists()


def test_init_noninteractive_cliproxy_claude_auth_prints_planning_handoff(capsys):
    result = setup_main(
        [
            "--auth-method",
            "cliproxy_claude_oauth",
            "--non-interactive",
        ]
    )

    output = capsys.readouterr().out
    assert result == 0
    assert "CLIProxy Claude OAuth Planning Gate" in output
    assert "CLIProxy OAuth is advanced" in output
    assert "Docker" in output
    assert "LLM_PROVIDER=anthropic" in output
    assert "LLM_BASE_URL=http://localhost:8318" in output
    assert "ANTHROPIC_API_KEY=cpx-<your-claude-gatekeeper-key>" in output
    assert "root URL, no /v1" in output
    assert "check_cliproxy_cloak.py" in output
    assert "tool_prefix_disabled" in output
    assert "gatekeeper key" in output
    assert "No config.env or .env.docker was written" in output
    assert "[/bold]" not in output
    assert "--provider is required" not in output


def test_init_noninteractive_cliproxy_codex_auth_prints_planning_handoff(
    tmp_path: Path,
    capsys,
):
    root = tmp_path / "runtime"

    result = setup_main(
        [
            "--provider",
            "openai",
            "--model",
            "gpt-5.5",
            "--api-key",
            "sk-not-written",
            "--auth-method",
            "cliproxy_codex_oauth",
            "--root",
            str(root),
            "--non-interactive",
        ]
    )

    output = capsys.readouterr().out
    normalized_output = " ".join(output.split())
    assert result == 0
    assert "CLIProxy Codex/OpenAI OAuth Planning Gate" in output
    assert "LLM_PROVIDER=openai" in output
    assert "OPENAI_API_MODE=responses" in output
    assert "LLM_BASE_URL=http://localhost:8318/v1" in output
    assert "OPENAI_API_KEY=cpx-<your-codex-gatekeeper-key>" in output
    assert "must end in /v1" in output
    assert "not an upstream Anthropic or OpenAI API key" in normalized_output
    assert (
        "do not reuse the cpx-* gatekeeper key for embeddings or voice"
        in normalized_output
    )
    assert "No config.env or .env.docker was written" in output
    assert not (root / "config.env").exists()


def test_init_interactive_cliproxy_auth_can_cancel_planning_gate(
    monkeypatch,
    capsys,
):
    answers = iter(["n"])

    def fake_prompt(text="", **kwargs):
        return next(answers)

    monkeypatch.setattr(setup_wizard, "prompt", fake_prompt)

    result = setup_main(["--hosting", "venv", "--auth-method", "cliproxy_claude_oauth"])

    output = capsys.readouterr().out
    assert result == 1
    assert "CLIProxy OAuth Setup" in output
    assert "Setup cancelled" in output
    assert "Step 2/7: LLM Provider" not in output


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
