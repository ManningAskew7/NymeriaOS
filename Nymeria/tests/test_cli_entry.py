import json
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


def _make_cliproxy_root(
    tmp_path: Path,
    *,
    auth_payload: dict | None = None,
    auth_filename: str = "claude-test@example.com.json",
) -> Path:
    root = tmp_path / "cliproxy" / "temp" / "latest"
    auth_dir = root / "auths"
    auth_dir.mkdir(parents=True)
    (root / "config.yaml").write_text(
        "\n".join(
            [
                'host: "0.0.0.0"',
                "port: 8317",
                'auth-dir: "/root/.cli-proxy-api"',
                "api-keys:",
                '  - "cpx-<placeholder>"',
                "debug: true",
                "commercial-mode: false",
                "logging-to-file: false",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (root / "docker-compose.yml").write_text(
        "\n".join(
            [
                "services:",
                "  cli-proxy-api-latest:",
                "    image: pinned-test-image",
                "    ports:",
                '      - "8318:8317"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    if auth_payload is not None:
        (auth_dir / auth_filename).write_text(
            json.dumps(auth_payload),
            encoding="utf-8",
        )
    return root


def _stub_llm_connection(monkeypatch):
    calls = []

    def fake_connection(provider, model, api_key):
        calls.append((provider.name, model, api_key))
        return setup_wizard.LLMConnectionResult(model=model)

    monkeypatch.setattr(setup_wizard, "_test_llm_connection", fake_connection)
    return calls


def _stub_post_setup_doctor(monkeypatch):
    calls = []

    def fake_doctor(root: Path, *, skip_llm_test: bool):
        calls.append((root, skip_llm_test))
        return 0

    monkeypatch.setattr(setup_wizard, "_run_doctor_for_root", fake_doctor)
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


def test_init_noninteractive_explicit_print_commands_uses_command_handoff(
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
            "--next-action",
            "print_commands",
            "--root",
            str(root),
            "--non-interactive",
        ]
    )

    output = capsys.readouterr().out
    assert result == 0
    assert "Start Nymeria with:" in output
    assert "nymeria api" in output
    assert "Enter CLI chat with:" not in output
    assert "Process spawning is not reliable" not in output


def test_init_noninteractive_recommended_writes_minimal_default_config(
    monkeypatch,
    tmp_path: Path,
    capsys,
):
    llm_calls = _stub_llm_connection(monkeypatch)
    root = tmp_path / "runtime"

    result = setup_main(
        [
            "--provider",
            "openrouter",
            "--model",
            "anthropic/claude-test-model",
            "--api-key",
            "sk-or-test-key",
            "--setup-style",
            "recommended",
            "--root",
            str(root),
            "--non-interactive",
        ]
    )

    config = (root / "config.env").read_text(encoding="utf-8")
    output = capsys.readouterr().out
    assert result == 0
    assert "Recommended Defaults" in output
    assert "Deferred Guided Setup" in output
    assert "LLM_PROVIDER=openrouter" in config
    assert "LLM_MODEL=anthropic/claude-test-model" in config
    assert "OPENROUTER_API_KEY=sk-or-test-key" in config
    assert f"NYMERIA_DATA_DIR={root / 'data'}" in config
    assert "EMBEDDING_API_KEY=" not in config
    assert "OPENAI_API_KEY=" not in config
    assert "GEMINI_API_KEY=" not in config
    assert "PERPLEXITY_API_KEY=" not in config
    assert llm_calls == [
        ("openrouter", "anthropic/claude-test-model", "sk-or-test-key")
    ]


@pytest.mark.parametrize("hosting", ["bare_metal", "venv"])
def test_init_noninteractive_local_hosting_profiles_write_runtime_config(
    monkeypatch,
    tmp_path: Path,
    capsys,
    hosting: str,
):
    _stub_llm_connection(monkeypatch)
    root = tmp_path / f"runtime-{hosting}"

    result = setup_main(
        [
            "--provider",
            "anthropic",
            "--model",
            "claude-test-model",
            "--api-key",
            "sk-ant-test-key",
            "--hosting",
            hosting,
            "--root",
            str(root),
            "--non-interactive",
        ]
    )

    config = (root / "config.env").read_text(encoding="utf-8")
    output = capsys.readouterr().out
    assert result == 0
    assert "Docker Setup" not in output
    assert "DATABASE_BACKEND=sqlite" in config
    assert f"NYMERIA_DATA_DIR={root / 'data'}" in config
    assert (root / "data" / "accounts.db").exists()


def test_init_noninteractive_run_doctor_uses_quick_check_by_default(
    monkeypatch,
    tmp_path: Path,
    capsys,
):
    _stub_llm_connection(monkeypatch)
    doctor_calls = _stub_post_setup_doctor(monkeypatch)
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
            "--run-doctor",
        ]
    )

    output = capsys.readouterr().out
    assert result == 0
    assert doctor_calls == [(root.resolve(), True)]
    assert "nymeria doctor --skip-llm-test" in output


def test_init_noninteractive_full_doctor_includes_llm_check(
    monkeypatch,
    tmp_path: Path,
    capsys,
):
    _stub_llm_connection(monkeypatch)
    doctor_calls = _stub_post_setup_doctor(monkeypatch)
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
            "--full-doctor",
        ]
    )

    output = capsys.readouterr().out
    assert result == 0
    assert doctor_calls == [(root.resolve(), False)]
    assert "nymeria doctor --skip-llm-test" not in output
    assert "nymeria doctor" in output


def test_init_interactive_offers_quick_doctor_after_validated_llm(
    monkeypatch,
    tmp_path: Path,
    capsys,
):
    _stub_llm_connection(monkeypatch)
    doctor_calls = _stub_post_setup_doctor(monkeypatch)
    root = tmp_path / "runtime"
    answers = iter(
        [
            "",  # default hosting: venv
            "",  # default auth method: direct API key
            "1",  # provider: Anthropic
            "claude-test-model",
            "sk-ant-test-key",
            "",  # run post-init doctor
            "",  # quick doctor; do not repeat live LLM check
            "",  # default next action: print commands
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
        ]
    )

    output = capsys.readouterr().out
    assert result == 0
    assert doctor_calls == [(root.resolve(), True)]
    assert "Final Validation" in output
    assert "nymeria doctor --skip-llm-test" in output
    assert "Run nymeria doctor now? [Y/n] " in prompts
    assert (
        "Provider auth was already tested. Run the full doctor LLM check again? [y/N] "
        in prompts
    )


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
            "",  # default auth method: direct API key
            provider_choice,
            "",  # accept provider default model
            api_key,
            "n",  # skip post-init doctor
            "",  # default next action: print commands
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


def test_init_interactive_defaults_to_recommended_setup(
    monkeypatch,
    tmp_path: Path,
    capsys,
):
    root = tmp_path / "runtime"
    answers = iter(
        [
            "",  # default hosting: venv
            "",  # default auth method: direct API key
            "1",  # provider: Anthropic
            "",  # accept provider default model
            "sk-ant-test-key",
            "",  # default setup style: recommended
            "n",  # skip post-init doctor
            "",  # default next action: print commands
        ]
    )
    prompts = []

    def fake_prompt(text="", **kwargs):
        prompts.append(text)
        return next(answers)

    monkeypatch.setattr(setup_wizard, "prompt", fake_prompt)
    monkeypatch.setattr(setup_wizard, "_default_init_root", lambda: root.resolve())

    result = setup_main(
        [
            "--skip-llm-test",
        ]
    )

    config = (root / "config.env").read_text(encoding="utf-8")
    output = capsys.readouterr().out
    assert result == 0
    assert "Step 2: Provider Authentication" in output
    assert "Direct API key - default" in output
    assert "CLIProxy Claude OAuth - advanced" in output
    assert "Step 6: Setup Style" in output
    assert "Recommended Defaults" in output
    assert "Deferred Guided Setup" in output
    assert "frontend Settings panel" in output
    assert "inside a normal chat" in output
    assert "Advanced Optional Capabilities" not in output
    assert "Advanced Data Directory" not in output
    assert "Using data directory:" in output
    assert "LLM_PROVIDER=anthropic" in config
    assert "LLM_MODEL=claude-sonnet-4-6" in config
    assert "ANTHROPIC_API_KEY=sk-ant-test-key" in config
    assert "EMBEDDING_API_KEY=" not in config
    assert "GEMINI_API_KEY=" not in config
    assert "PERPLEXITY_API_KEY=" not in config
    assert "Enable semantic memory/RAG embeddings?" not in prompts


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
            "",  # default auth method: direct API key
            "1",  # provider: Anthropic
            "claude-test-model",
            "sk-ant-test-key",
            "2",  # setup style: advanced
            "n",
            "n",
            "n",
            "n",
            "",  # default data directory
            "n",  # skip post-init doctor
            "",  # default next action: print commands
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
    assert "Step 1: Hosting / Security" in output
    assert "Step 2: Provider Authentication" in output
    assert "Step 3: LLM Provider" in output
    assert output.index("Step 1: Hosting / Security") < output.index(
        "Step 2: Provider Authentication"
    )
    assert output.index("Step 2: Provider Authentication") < output.index(
        "Step 3: LLM Provider"
    )
    assert "not an OS security sandbox" in output
    assert "Docker" in output
    assert "Next Action" in output
    assert "Deferred Guided Setup" not in output
    assert (root / "config.env").exists()


def test_init_interactive_prompts_for_next_action(
    monkeypatch,
    tmp_path: Path,
    capsys,
):
    root = tmp_path / "runtime"
    answers = iter(
        [
            "",  # default hosting: venv
            "",  # default auth method: direct API key
            "1",  # provider: Anthropic
            "claude-test-model",
            "sk-ant-test-key",
            "2",  # setup style: advanced
            "n",
            "n",
            "n",
            "n",
            "",  # default data directory
            "n",  # skip post-init doctor
            "3",  # next action: CLI chat handoff
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
    assert prompts[-1] == "> [2] "
    assert "Next Action" in output
    assert "Start backend and open web UI" in output
    assert "Print commands only - default" in output
    assert "nymeria cli" in output


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
    assert "Step 2: Provider Authentication" not in output
    assert "Step 3: LLM Provider" not in output
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


def test_init_noninteractive_cliproxy_claude_auth_requires_active_oauth(
    monkeypatch,
    tmp_path: Path,
    capsys,
):
    cliproxy_root = _make_cliproxy_root(tmp_path)
    root = tmp_path / "runtime"

    monkeypatch.setattr(
        setup_wizard,
        "_ensure_cliproxy_container_ready",
        lambda *args, **kwargs: None,
    )

    result = setup_main(
        [
            "--auth-method",
            "cliproxy_claude_oauth",
            "--cliproxy-root",
            str(cliproxy_root),
            "--root",
            str(root),
            "--non-interactive",
        ]
    )

    output = capsys.readouterr().out
    assert result == 2
    assert "no active Claude OAuth auth JSON" in output
    assert "Manual CLIProxy Claude OAuth steps" in output
    assert "docker exec -it cli-proxy-api-latest" in output
    assert "No config.env or .env.docker was written" not in output
    assert not (root / "config.env").exists()


def test_init_noninteractive_cliproxy_claude_oauth_writes_verified_config(
    monkeypatch,
    tmp_path: Path,
    capsys,
):
    cliproxy_root = _make_cliproxy_root(
        tmp_path,
        auth_payload={
            "type": "claude",
            "email": "test@example.com",
            "access_token": "redacted",
        },
    )
    root = tmp_path / "runtime"
    smoke_calls = []
    restart_calls = []

    monkeypatch.setattr(
        setup_wizard,
        "_ensure_cliproxy_container_ready",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        setup_wizard,
        "_restart_cliproxy_container",
        lambda console: restart_calls.append(True),
    )

    def fake_smoke(base_url, *, gatekeeper_key, auth_dir, console):
        smoke_calls.append((base_url, gatekeeper_key, auth_dir))

    monkeypatch.setattr(setup_wizard, "_run_cliproxy_cloak_check", fake_smoke)

    result = setup_main(
        [
            "--auth-method",
            "cliproxy_claude_oauth",
            "--api-key",
            "cpx-claude-test",
            "--model",
            "claude-test-model",
            "--cliproxy-root",
            str(cliproxy_root),
            "--cliproxy-base-url",
            "http://localhost:8317",
            "--root",
            str(root),
            "--non-interactive",
        ]
    )

    config = (root / "config.env").read_text(encoding="utf-8")
    proxy_config = setup_wizard._read_cliproxy_config(cliproxy_root / "config.yaml")
    auth_json = json.loads(
        (cliproxy_root / "auths" / "claude-test@example.com.json").read_text(
            encoding="utf-8"
        )
    )
    output = capsys.readouterr().out
    assert result == 0
    assert "LLM_PROVIDER=anthropic" in config
    assert "LLM_MODEL=claude-test-model" in config
    assert "LLM_BASE_URL=http://localhost:8317" in config
    assert "ANTHROPIC_API_KEY=cpx-claude-test" in config
    assert "Bootstrap token:" in output
    assert proxy_config["api-keys"] == ["cpx-claude-test"]
    assert auth_json["tool_prefix_disabled"] is True
    assert smoke_calls == [
        (
            "http://localhost:8317",
            "cpx-claude-test",
            cliproxy_root / "auths",
        )
    ]
    assert restart_calls == [True]


def test_cliproxy_gatekeeper_key_can_be_generated(monkeypatch, tmp_path: Path):
    cliproxy_root = _make_cliproxy_root(tmp_path)
    deployment = setup_wizard.CLIProxyDeployment(
        root=cliproxy_root,
        config_path=cliproxy_root / "config.yaml",
        compose_path=cliproxy_root / "docker-compose.yml",
        auth_dir=cliproxy_root / "auths",
    )

    monkeypatch.setattr(setup_wizard.secrets, "token_urlsafe", lambda size: "fixed")

    key, changed = setup_wizard._resolve_cliproxy_gatekeeper_key(
        type("Args", (), {"api_key": None})(),
        deployment=deployment,
        console=setup_wizard.Console(),
        non_interactive=True,
    )

    proxy_config = setup_wizard._read_cliproxy_config(cliproxy_root / "config.yaml")
    assert key == "cpx-nymeria-fixed"
    assert changed is True
    assert proxy_config["api-keys"] == ["cpx-nymeria-fixed"]


def test_cliproxy_claude_smoke_failure_stops_before_config(
    monkeypatch,
    tmp_path: Path,
    capsys,
):
    cliproxy_root = _make_cliproxy_root(
        tmp_path,
        auth_payload={
            "type": "claude",
            "tool_prefix_disabled": True,
        },
    )
    root = tmp_path / "runtime"

    monkeypatch.setattr(
        setup_wizard,
        "_ensure_cliproxy_container_ready",
        lambda *args, **kwargs: None,
    )

    def fail_smoke(*args, **kwargs):
        raise setup_wizard.CLIProxySetupError("smoke failed")

    monkeypatch.setattr(setup_wizard, "_run_cliproxy_cloak_check", fail_smoke)

    result = setup_main(
        [
            "--auth-method",
            "cliproxy_claude_oauth",
            "--api-key",
            "cpx-claude-test",
            "--cliproxy-root",
            str(cliproxy_root),
            "--cliproxy-base-url",
            "http://localhost:8317",
            "--root",
            str(root),
            "--non-interactive",
        ]
    )

    output = capsys.readouterr().out
    assert result == 2
    assert "smoke failed" in output
    assert "Manual CLIProxy Claude OAuth steps" in output
    assert not (root / "config.env").exists()


def test_init_noninteractive_cliproxy_codex_auth_requires_active_oauth(
    monkeypatch,
    tmp_path: Path,
    capsys,
):
    cliproxy_root = _make_cliproxy_root(tmp_path)
    root = tmp_path / "runtime"

    monkeypatch.setattr(
        setup_wizard,
        "_ensure_cliproxy_container_ready",
        lambda *args, **kwargs: None,
    )

    result = setup_main(
        [
            "--auth-method",
            "cliproxy_codex_oauth",
            "--cliproxy-root",
            str(cliproxy_root),
            "--root",
            str(root),
            "--non-interactive",
        ]
    )

    output = capsys.readouterr().out
    normalized_output = " ".join(output.split())
    assert result == 2
    assert "no active Codex/OpenAI OAuth auth JSON" in normalized_output
    assert "Manual CLIProxy Codex/OpenAI OAuth steps" in output
    assert "docker exec -it cli-proxy-api-latest" in output
    assert not (root / "config.env").exists()


def test_init_noninteractive_cliproxy_codex_oauth_writes_verified_config(
    monkeypatch,
    tmp_path: Path,
    capsys,
):
    cliproxy_root = _make_cliproxy_root(
        tmp_path,
        auth_filename="codex-test@example.com-plus.json",
        auth_payload={
            "type": "codex",
            "email": "test@example.com",
            "access_token": "redacted",
        },
    )
    root = tmp_path / "runtime"
    verify_calls = []
    restart_calls = []

    monkeypatch.setattr(
        setup_wizard,
        "_ensure_cliproxy_container_ready",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        setup_wizard,
        "_restart_cliproxy_container",
        lambda console: restart_calls.append(True),
    )

    def fake_verify(base_url, *, gatekeeper_key, model, console):
        verify_calls.append((base_url, gatekeeper_key, model))

    monkeypatch.setattr(setup_wizard, "_run_cliproxy_codex_verification", fake_verify)

    result = setup_main(
        [
            "--auth-method",
            "cliproxy_codex_oauth",
            "--api-key",
            "cpx-codex-test",
            "--model",
            "gpt-test-model",
            "--embedding-api-key",
            "cpx-not-for-embeddings",
            "--cliproxy-root",
            str(cliproxy_root),
            "--cliproxy-base-url",
            "http://localhost:8317/v1",
            "--root",
            str(root),
            "--non-interactive",
        ]
    )

    config = (root / "config.env").read_text(encoding="utf-8")
    proxy_config = setup_wizard._read_cliproxy_config(cliproxy_root / "config.yaml")
    output = capsys.readouterr().out
    assert result == 0
    assert "LLM_PROVIDER=openai" in config
    assert "LLM_MODEL=gpt-test-model" in config
    assert "OPENAI_API_MODE=responses" in config
    assert "LLM_BASE_URL=http://localhost:8317/v1" in config
    assert "OPENAI_API_KEY=cpx-codex-test" in config
    assert "EMBEDDING_API_KEY=" not in config
    assert "EMBEDDING_API_KEY was not written" in output
    assert "Bootstrap token:" in output
    assert proxy_config["api-keys"] == ["cpx-codex-test"]
    assert verify_calls == [
        (
            "http://localhost:8317/v1",
            "cpx-codex-test",
            "gpt-test-model",
        )
    ]
    assert restart_calls == [True]


def test_cliproxy_codex_probe_failure_stops_before_config(
    monkeypatch,
    tmp_path: Path,
    capsys,
):
    cliproxy_root = _make_cliproxy_root(
        tmp_path,
        auth_filename="codex-test@example.com-plus.json",
        auth_payload={"type": "codex"},
    )
    root = tmp_path / "runtime"

    monkeypatch.setattr(
        setup_wizard,
        "_ensure_cliproxy_container_ready",
        lambda *args, **kwargs: None,
    )

    def fail_verify(*args, **kwargs):
        raise setup_wizard.CLIProxySetupError("responses probe failed")

    monkeypatch.setattr(setup_wizard, "_run_cliproxy_codex_verification", fail_verify)

    result = setup_main(
        [
            "--auth-method",
            "cliproxy_codex_oauth",
            "--api-key",
            "cpx-codex-test",
            "--cliproxy-root",
            str(cliproxy_root),
            "--cliproxy-base-url",
            "http://localhost:8317",
            "--root",
            str(root),
            "--non-interactive",
        ]
    )

    output = capsys.readouterr().out
    assert result == 2
    assert "responses probe failed" in output
    assert "Manual CLIProxy Codex/OpenAI OAuth steps" in output
    assert not (root / "config.env").exists()


def test_init_interactive_cliproxy_auth_can_cancel_setup(
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
    assert "CLIProxy Claude OAuth Setup" in output
    assert "Setup cancelled" in output
    assert "Step 3: LLM Provider" not in output


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
            "--cliproxy-root",
            "/tmp/cliproxy",
            "--cliproxy-base-url",
            "http://localhost:8317",
            "--data-dir",
            "/tmp/nymeria-data",
            "--non-interactive",
            "--skip-llm-test",
            "--run-doctor",
            "--full-doctor",
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
    assert args.cliproxy_root == "/tmp/cliproxy"
    assert args.cliproxy_base_url == "http://localhost:8317"
    assert args.data_dir == "/tmp/nymeria-data"
    assert args.run_doctor is True
    assert args.full_doctor is True


def test_run_cli_parser_accepts_tui_contract_defaults():
    import run as run_module

    args = run_module.build_parser().parse_args(["cli"])
    runtime_config = run_module.build_cli_runtime_config(args)

    assert args.thread is None
    assert runtime_config.transport == "api"
    assert runtime_config.renderer == "rich"
    assert runtime_config.api_url is None
    assert runtime_config.api_key is None
    assert runtime_config.user_id == "default"
    assert runtime_config.user_id_explicit is False
    assert runtime_config.alt_screen is True
    assert runtime_config.animation is True
    assert runtime_config.ascii_only is False
    assert runtime_config.color == "auto"
    assert runtime_config.rich_scroll_region is False
    assert runtime_config.startup_thread_ref is None
    assert runtime_config.list_threads_on_startup is False


def test_run_cli_parser_accepts_positional_list_command():
    import run as run_module

    args = run_module.build_parser().parse_args(["cli", "list"])
    runtime_config = run_module.build_cli_runtime_config(args)

    assert args.thread is None
    assert runtime_config.startup_thread_ref == "list"
    assert runtime_config.list_threads_on_startup is True


def test_run_cli_parser_thread_flag_can_open_thread_named_list():
    import run as run_module

    args = run_module.build_parser().parse_args(["cli", "--thread", "list"])
    runtime_config = run_module.build_cli_runtime_config(args)

    assert runtime_config.startup_thread_ref == "list"
    assert runtime_config.list_threads_on_startup is False


def test_run_cli_parser_accepts_positional_multi_word_thread_title():
    import run as run_module

    args = run_module.build_parser().parse_args(["cli", "Quarterly", "Planning"])
    runtime_config = run_module.build_cli_runtime_config(args)

    assert args.thread is None
    assert runtime_config.startup_thread_ref == "Quarterly Planning"
    assert runtime_config.list_threads_on_startup is False


@pytest.mark.parametrize(
    ("thread_flag", "thread_id"),
    [
        ("--thread", "alpha"),
        ("--thread-id", "beta"),
        ("-t", "gamma"),
    ],
)
def test_run_cli_parser_thread_aliases(thread_flag: str, thread_id: str):
    import run as run_module

    args = run_module.build_parser().parse_args(["cli", thread_flag, thread_id])
    runtime_config = run_module.build_cli_runtime_config(args)

    assert args.thread == thread_id
    assert runtime_config.startup_thread_ref == thread_id
    assert runtime_config.list_threads_on_startup is False


def test_run_cli_parser_carries_explicit_tui_contract_flags():
    import run as run_module

    args = run_module.build_parser().parse_args(
        [
            "cli",
            "--transport",
            "api",
            "--renderer",
            "plain",
            "--api-url",
            "http://localhost:8000",
            "--api-key",
            "nym_test",
            "--user-id",
            "owner",
            "--no-alt-screen",
            "--no-animation",
            "--ascii",
            "--color",
            "never",
            "--rich-scroll-region",
        ]
    )
    runtime_config = run_module.build_cli_runtime_config(args)

    assert runtime_config.transport == "api"
    assert runtime_config.renderer == "plain"
    assert runtime_config.api_url == "http://localhost:8000"
    assert runtime_config.api_key == "nym_test"
    assert runtime_config.user_id == "owner"
    assert runtime_config.user_id_explicit is True
    assert runtime_config.alt_screen is False
    assert runtime_config.animation is False
    assert runtime_config.ascii_only is True
    assert runtime_config.color == "never"
    assert runtime_config.rich_scroll_region is True


def test_run_cli_parser_accepts_rich_scroll_region_env(monkeypatch):
    import run as run_module

    monkeypatch.setenv("NYMERIA_CLI_RICH_SCROLL_REGION", "1")

    args = run_module.build_parser().parse_args(["cli"])
    runtime_config = run_module.build_cli_runtime_config(args)

    assert runtime_config.rich_scroll_region is True


def test_run_cli_default_starts_without_local_agent(monkeypatch):
    import run as run_module
    import nymeria.triggers.cli as cli_module

    captured = {}

    def fake_start_cli(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(cli_module, "run_cli", fake_start_cli)

    args = run_module.build_parser().parse_args(["cli"])
    run_module.run_cli(args)

    assert captured["agent"] is None
    assert captured["runtime_config"].transport == "api"


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


def test_init_interactive_advanced_writes_optional_keys_and_custom_data_dir(
    monkeypatch,
    tmp_path: Path,
):
    root = tmp_path / "runtime"
    custom_data_dir = tmp_path / "custom-data"
    answers = iter(
        [
            "",  # default hosting: venv
            "",  # default auth method: direct API key
            "1",  # provider: Anthropic
            "claude-test-model",
            "sk-ant-test-key",
            "2",  # setup style: advanced
            "y",
            "sk-embedding-test",
            "y",
            "sk-openai-test",
            "y",
            "gemini-test",
            "y",
            "pplx-test",
            "2",  # custom data directory
            str(custom_data_dir),
            "n",  # skip post-init doctor
            "",  # default next action: print commands
        ]
    )

    def fake_prompt(text="", **kwargs):
        return next(answers)

    monkeypatch.setattr(setup_wizard, "prompt", fake_prompt)

    result = setup_main(
        [
            "--root",
            str(root),
            "--skip-llm-test",
        ]
    )

    config = (root / "config.env").read_text(encoding="utf-8")
    assert result == 0
    assert f"NYMERIA_DATA_DIR={custom_data_dir}" in config
    assert "EMBEDDING_API_KEY=sk-embedding-test" in config
    assert "OPENAI_API_KEY=sk-openai-test" in config
    assert "GEMINI_API_KEY=gemini-test" in config
    assert "PERPLEXITY_API_KEY=pplx-test" in config
    assert (custom_data_dir / "accounts.db").exists()
    assert (custom_data_dir / "BOOTSTRAP_TOKEN.txt").exists()


def test_init_noninteractive_advanced_accepts_custom_data_dir(
    monkeypatch,
    tmp_path: Path,
):
    _stub_llm_connection(monkeypatch)
    root = tmp_path / "runtime"
    custom_data_dir = tmp_path / "custom-data"

    result = setup_main(
        [
            "--provider",
            "anthropic",
            "--model",
            "claude-test-model",
            "--api-key",
            "sk-ant-test-key",
            "--data-dir",
            str(custom_data_dir),
            "--root",
            str(root),
            "--non-interactive",
        ]
    )

    config = (root / "config.env").read_text(encoding="utf-8")
    assert result == 0
    assert f"NYMERIA_DATA_DIR={custom_data_dir}" in config
    assert (custom_data_dir / "accounts.db").exists()


def test_init_noninteractive_recommended_rejects_optional_capability_keys(
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
            "--embedding-api-key",
            "sk-embedding-test",
            "--setup-style",
            "recommended",
            "--root",
            str(root),
            "--non-interactive",
        ]
    )

    output = capsys.readouterr().out
    assert result == 2
    assert "Recommended setup writes only the primary provider credential" in output
    assert "EMBEDDING_API_KEY" in output
    assert not (root / "config.env").exists()


def test_init_noninteractive_recommended_rejects_custom_data_dir(
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
            "--data-dir",
            str(tmp_path / "custom-data"),
            "--setup-style",
            "recommended",
            "--root",
            str(root),
            "--non-interactive",
        ]
    )

    output = capsys.readouterr().out
    assert result == 2
    assert "Recommended setup uses the default data directory" in output
    assert "--setup-style advanced" in " ".join(output.split())
    assert not (root / "config.env").exists()


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
