"""Tests for the rebuilt `nymeria init` wizard.

Three layers: the pure navigation model (forward/back/conditional skip), the
headless finalize path (config write + bootstrap token), and one Textual Pilot
smoke that drives the interactive wizard with real keypresses.
"""

import asyncio
import re
import stat
from pathlib import Path

import pytest

from nymeria.onboarding import (
    HOSTING_CHOICES,
    HOSTING_ORDER,
    HostingOption,
    choice_values,
    parse_choice,
)
from nymeria.setup import finalize as finalize_mod
from nymeria.setup.providers import LLMConnectionError, LLMConnectionResult
from nymeria.setup.runner import main as setup_main


# --- onboarding data model --------------------------------------------------


def test_hosting_option_values_match_new_design():
    assert choice_values(HostingOption) == ("local", "service", "docker")
    assert HOSTING_ORDER == (
        HostingOption.LOCAL,
        HostingOption.SERVICE,
        HostingOption.DOCKER,
    )
    assert HOSTING_CHOICES[HostingOption.LOCAL].recommended is True
    assert "nymeria slim" in HOSTING_CHOICES[HostingOption.LOCAL].description


def test_parse_choice_rejects_retired_hosting_value():
    assert parse_choice(HostingOption, "docker", option_name="--hosting") is HostingOption.DOCKER
    with pytest.raises(ValueError):
        parse_choice(HostingOption, "venv", option_name="--hosting")


# --- pure navigation model --------------------------------------------------


def _conditional_steps():
    from nymeria.setup.nav import Step

    return [
        Step("a", lambda _s: True, lambda *_a: None),
        Step("b", lambda _s: True, lambda *_a: None),
        Step("c", lambda s: s.provider == "openai", lambda *_a: None),
        Step("d", lambda _s: True, lambda *_a: None),
    ]


def test_navigator_forward_back_skips_conditional():
    from nymeria.setup.nav import Navigator
    from nymeria.setup.state import WizardState

    nav = Navigator(_conditional_steps(), WizardState())
    assert nav.start() == 0
    assert nav.advance() == 1
    assert nav.advance() == 3  # "c" skipped because provider is not openai
    assert nav.back() == 1
    assert nav.back() == 0
    assert nav.back() is None  # already at first step
    assert nav.at_start() is True


def test_navigator_includes_conditional_step_when_it_applies():
    from nymeria.setup.nav import Navigator
    from nymeria.setup.state import WizardState

    nav = Navigator(_conditional_steps(), WizardState(provider="openai"))
    assert nav.applicable_indices() == [0, 1, 2, 3]
    assert nav.start() == 0
    assert nav.advance() == 1
    assert nav.advance() == 2
    assert nav.advance() == 3


def test_navigator_position_reflects_applicable_total():
    from nymeria.setup.nav import Navigator
    from nymeria.setup.state import WizardState

    nav = Navigator(_conditional_steps(), WizardState())
    nav.start()
    assert nav.position() == (1, 3)  # conditional step excluded from the total
    nav.advance()
    assert nav.position() == (2, 3)


# --- headless finalize ------------------------------------------------------


def _stub_llm(monkeypatch):
    calls = []

    def fake(spec, model, api_key, *, base_url=None):
        calls.append((spec.id, model, api_key))
        return LLMConnectionResult(model=model)

    monkeypatch.setattr(finalize_mod, "check_llm_connection_for_spec", fake)
    return calls


def test_noninteractive_writes_config_and_bootstrap_token(monkeypatch, tmp_path, capsys):
    calls = _stub_llm(monkeypatch)
    root = tmp_path / "runtime"

    rc = setup_main(
        [
            "--provider", "anthropic",
            "--model", "claude-test-model",
            "--api-key", "sk-ant-test-key",
            "--root", str(root),
            "--non-interactive",
        ]
    )

    config = (root / "config.env").read_text(encoding="utf-8")
    out = capsys.readouterr().out
    token_path = root / "data" / "BOOTSTRAP_TOKEN.txt"
    raw_token = re.search(r"nym_[A-Za-z0-9_-]+", token_path.read_text(encoding="utf-8"))

    assert rc == 0
    assert "LLM_PROVIDER=anthropic" in config
    assert "LLM_MODEL=claude-test-model" in config
    # The registry's highest-priority Anthropic key env var is the direct one.
    assert "ANTHROPIC_DIRECT_API_KEY=sk-ant-test-key" in config
    assert "DATABASE_BACKEND=sqlite" in config
    assert f"NYMERIA_DATA_DIR={root / 'data'}" in config
    assert (root / "data" / "accounts.db").exists()
    assert token_path.exists()
    assert stat.S_IMODE((root / "config.env").stat().st_mode) == 0o600
    assert "Bootstrap token:" in out
    assert str(token_path) in out
    assert raw_token is not None and raw_token.group(0) not in out  # never echo the token
    assert "Capabilities" in out
    assert calls == [("anthropic", "claude-test-model", "sk-ant-test-key")]


def test_noninteractive_writes_base_url_and_api_mode(monkeypatch, tmp_path):
    _stub_llm(monkeypatch)
    root = tmp_path / "runtime"

    rc = setup_main(
        [
            "--provider", "openrouter",
            "--model", "anthropic/claude-test",
            "--api-key", "sk-or-test",
            "--base-url", "http://localhost:8317/v1",
            "--api-mode", "responses",
            "--root", str(root),
            "--non-interactive",
        ]
    )

    config = (root / "config.env").read_text(encoding="utf-8")
    assert rc == 0
    assert "LLM_BASE_URL=http://localhost:8317/v1" in config
    assert "OPENAI_API_MODE=responses" in config
    assert "OPENROUTER_API_KEY=sk-or-test" in config


def test_noninteractive_writes_optional_capability_keys(monkeypatch, tmp_path):
    _stub_llm(monkeypatch)
    root = tmp_path / "runtime"

    rc = setup_main(
        [
            "--provider", "anthropic",
            "--model", "claude-test-model",
            "--api-key", "sk-ant-test-key",
            "--embedding-api-key", "sk-embedding-test",
            "--openai-api-key", "sk-openai-test",
            "--gemini-api-key", "gemini-test",
            "--perplexity-api-key", "pplx-test",
            "--root", str(root),
            "--non-interactive",
        ]
    )

    config = (root / "config.env").read_text(encoding="utf-8")
    assert rc == 0
    assert "EMBEDDING_API_KEY=sk-embedding-test" in config
    assert "OPENAI_API_KEY=sk-openai-test" in config
    assert "GEMINI_API_KEY=gemini-test" in config
    assert "PERPLEXITY_API_KEY=pplx-test" in config


def test_noninteractive_does_not_duplicate_primary_openai_key(monkeypatch, tmp_path):
    _stub_llm(monkeypatch)
    root = tmp_path / "runtime"

    rc = setup_main(
        [
            "--provider", "openai",
            "--model", "openai-test-model",
            "--api-key", "sk-openai-primary",
            "--openai-api-key", "sk-openai-primary",
            "--root", str(root),
            "--non-interactive",
        ]
    )

    config = (root / "config.env").read_text(encoding="utf-8")
    assert rc == 0
    assert config.count("OPENAI_API_KEY=") == 1


def test_noninteractive_custom_data_dir(monkeypatch, tmp_path):
    _stub_llm(monkeypatch)
    root = tmp_path / "runtime"
    data_dir = tmp_path / "custom-data"

    rc = setup_main(
        [
            "--provider", "anthropic",
            "--model", "claude-test-model",
            "--api-key", "sk-ant-test-key",
            "--data-dir", str(data_dir),
            "--root", str(root),
            "--non-interactive",
        ]
    )

    config = (root / "config.env").read_text(encoding="utf-8")
    assert rc == 0
    assert f"NYMERIA_DATA_DIR={data_dir}" in config
    assert (data_dir / "accounts.db").exists()


def test_noninteractive_requires_model(tmp_path):
    root = tmp_path / "runtime"
    with pytest.raises(SystemExit) as exc_info:
        setup_main(
            [
                "--provider", "anthropic",
                "--api-key", "sk-ant-test-key",
                "--root", str(root),
                "--non-interactive",
            ]
        )
    assert str(exc_info.value) == "--model is required with --non-interactive"
    assert not (root / "config.env").exists()


def test_noninteractive_stops_when_llm_connection_fails(monkeypatch, tmp_path):
    root = tmp_path / "runtime"

    def fail(*_args, **_kwargs):
        raise LLMConnectionError("bad key")

    monkeypatch.setattr(finalize_mod, "check_llm_connection_for_spec", fail)

    rc = setup_main(
        [
            "--provider", "anthropic",
            "--model", "claude-test-model",
            "--api-key", "sk-ant-test-key",
            "--root", str(root),
            "--non-interactive",
        ]
    )
    assert rc == 2
    assert not (root / "config.env").exists()


def test_noninteractive_skip_llm_test_does_not_call_provider(monkeypatch, tmp_path):
    root = tmp_path / "runtime"

    def fail(*_args, **_kwargs):
        raise AssertionError("LLM connection test should have been skipped")

    monkeypatch.setattr(finalize_mod, "check_llm_connection_for_spec", fail)

    rc = setup_main(
        [
            "--provider", "anthropic",
            "--model", "claude-test-model",
            "--api-key", "sk-ant-test-key",
            "--root", str(root),
            "--non-interactive",
            "--skip-llm-test",
        ]
    )
    assert rc == 0
    assert (root / "config.env").exists()


def test_noninteractive_rejects_bad_key_prefix(monkeypatch, tmp_path):
    _stub_llm(monkeypatch)
    root = tmp_path / "runtime"

    rc = setup_main(
        [
            "--provider", "anthropic",
            "--model", "claude-test-model",
            "--api-key", "not-an-anthropic-key",
            "--root", str(root),
            "--non-interactive",
            "--skip-llm-test",
        ]
    )
    assert rc == 2
    assert not (root / "config.env").exists()


# --- bootstrap token copy command -------------------------------------------


def test_token_copy_command_uses_macos_clipboard(monkeypatch):
    monkeypatch.setattr(finalize_mod.sys, "platform", "darwin")
    hint = finalize_mod.bootstrap_token_copy_command(
        Path("/tmp/Nymeria Data/BOOTSTRAP_TOKEN.txt")
    )
    assert hint.copies_to_clipboard is True
    assert "pbcopy" in hint.command
    assert "nym_[A-Za-z0-9_-]+" in hint.command


def test_token_copy_command_uses_windows_clipboard(monkeypatch):
    monkeypatch.setattr(finalize_mod.sys, "platform", "win32")
    hint = finalize_mod.bootstrap_token_copy_command(
        Path(r"C:\Users\Owner\.nymeria\data\BOOTSTRAP_TOKEN.txt")
    )
    assert hint.copies_to_clipboard is True
    assert "Set-Clipboard" in hint.command
    assert "Select-String" in hint.command


def test_token_copy_command_falls_back_without_linux_clipboard(monkeypatch):
    monkeypatch.setattr(finalize_mod.sys, "platform", "linux")
    monkeypatch.setattr(finalize_mod.shutil, "which", lambda _name: None)
    hint = finalize_mod.bootstrap_token_copy_command(
        Path("/home/owner/.nymeria/data/BOOTSTRAP_TOKEN.txt")
    )
    assert hint.copies_to_clipboard is False
    assert hint.command == (
        "grep -oE 'nym_[A-Za-z0-9_-]+' "
        "/home/owner/.nymeria/data/BOOTSTRAP_TOKEN.txt | head -n 1"
    )


# --- run.py init subparser --------------------------------------------------


def test_run_init_parser_accepts_new_flags():
    import run as run_module

    args = run_module.build_parser().parse_args(
        [
            "init",
            "provider",
            "--hosting", "local",
            "--provider", "anthropic",
            "--model", "claude-test-model",
            "--api-key", "sk-ant-test-key",
            "--base-url", "http://localhost:8317/v1",
            "--api-mode", "responses",
            "--next-action", "print_commands",
            "--data-dir", "/tmp/nymeria-data",
            "--quick",
            "--non-interactive",
            "--skip-llm-test",
            "--run-doctor",
            "--full-doctor",
        ]
    )
    assert args.section == "provider"
    assert args.hosting == "local"
    assert args.provider == "anthropic"
    assert args.base_url == "http://localhost:8317/v1"
    assert args.api_mode == "responses"
    assert args.next_action == "print_commands"
    assert args.quick is True
    assert args.run_doctor is True
    assert args.full_doctor is True


# --- interactive Textual wizard (Pilot) -------------------------------------


async def _no_models(*_args, **_kwargs):
    """Stand-in for the live model fetch so Pilot tests never hit the network."""
    return []


def test_wizard_pilot_forward_back_and_provider(monkeypatch):
    from textual.widgets import Input

    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState

    monkeypatch.setattr("nymeria.setup.steps.model.fetch_models_for_spec", _no_models)

    async def drive() -> WizardState:
        state = WizardState()
        app = SetupWizardApp(state)
        async with app.run_test() as pilot:
            assert app.nav.current() == 0
            await pilot.press("enter")  # accept default hosting (local), advance
            await pilot.pause()
            assert state.hosting is HostingOption.LOCAL
            assert app.nav.current() == 1
            await pilot.press("escape")  # back to hosting (does not exit)
            await pilot.pause()
            assert app.nav.current() == 0
            await pilot.press("enter")  # forward to provider (focus picker search)
            await pilot.pause()
            await pilot.press("enter")  # Enter in picker -> focus moves to key field
            await pilot.pause()
            app.screen.query_one("#api-key", Input).value = "sk-ant-xyz"
            await pilot.press("enter")  # key entered -> advance past provider
            await pilot.pause()
            assert state.provider == "anthropic"
            assert state.api_key == "sk-ant-xyz"
            # Anthropic has a fixed endpoint and no API-mode toggle, so the
            # connection step is skipped; the model step is next.
            assert 2 not in app.nav.applicable_indices()
            await pilot.press("enter")  # model step: accept default model, advance
            await pilot.pause()
        return state

    state = asyncio.run(drive())
    assert state.model == "claude-sonnet-4-6"  # provider default model filled in


def test_wizard_pilot_selects_non_default_provider(monkeypatch):
    from textual.widgets import Input

    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState

    monkeypatch.setattr("nymeria.setup.steps.model.fetch_models_for_spec", _no_models)

    async def drive() -> SetupWizardApp:
        app = SetupWizardApp(WizardState())
        async with app.run_test() as pilot:
            await pilot.press("enter")  # hosting -> provider (focus picker search)
            await pilot.pause()
            await pilot.press(*"openrouter")  # filter the provider list
            await pilot.pause()
            await pilot.press("enter")  # Enter in picker -> focus key field
            await pilot.pause()
            app.screen.query_one("#api-key", Input).value = "sk-or-test"  # model left blank
            await pilot.press("enter")  # advance past provider -> connection step
            await pilot.pause()
            # OpenRouter supports API mode, so the connection step applies.
            assert 2 in app.nav.applicable_indices()
            await pilot.press("enter")  # connection: accept defaults, advance
            await pilot.pause()
            await pilot.press("enter")  # model step: accept default model, advance
            await pilot.pause()
        return app

    app = asyncio.run(drive())
    assert app.state.provider == "openrouter"
    # Blank model falls back to the chosen provider's registry default.
    assert app.state.model == "anthropic/claude-sonnet-4.5"


def test_wizard_pilot_model_step_lists_and_selects(monkeypatch):
    from textual.widgets import Input

    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.providers import ModelChoice
    from nymeria.setup.state import WizardState

    async def fake_fetch(spec, *, api_key, base_url=None, timeout=8.0):
        return [
            ModelChoice(id="claude-sonnet-4-6", context_length=200000),
            ModelChoice(id="claude-opus-4-8", context_length=200000),
            ModelChoice(id="claude-haiku-4-5", context_length=200000),
        ]

    monkeypatch.setattr("nymeria.setup.steps.model.fetch_models_for_spec", fake_fetch)

    async def drive() -> WizardState:
        state = WizardState()
        app = SetupWizardApp(state)
        async with app.run_test() as pilot:
            await pilot.press("enter")  # hosting -> provider
            await pilot.pause()
            await pilot.press("enter")  # picker -> focus key
            await pilot.pause()
            app.screen.query_one("#api-key", Input).value = "sk-ant-xyz"
            await pilot.press("enter")  # advance to model step (connection skipped)
            await pilot.pause()
            await pilot.pause()  # let the fetch worker populate the list
            await pilot.press(*"opus")  # filter to the one matching model
            await pilot.pause()
            await pilot.press("enter")  # commit the highlighted model, advance
            await pilot.pause()
        return state

    state = asyncio.run(drive())
    assert state.model == "claude-opus-4-8"


def test_wizard_pilot_blank_key_requires_explicit_skip():
    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState

    async def drive() -> SetupWizardApp:
        app = SetupWizardApp(WizardState())
        async with app.run_test() as pilot:
            await pilot.press("enter")  # hosting -> provider (focus picker search)
            await pilot.pause()
            await pilot.press("enter")  # Enter in picker -> focus key field
            await pilot.pause()
            assert app.nav.current() == 1  # Enter in picker does not advance
            await pilot.press("enter")  # blank key -> error, does not skip
            await pilot.pause()
            assert app.nav.current() == 1
            assert app.state.provider is None
            await pilot.press("ctrl+s")  # explicit skip
            await pilot.pause()
        return app

    app = asyncio.run(drive())
    assert app.state.provider is None
    assert app.nav.current() != 1  # advanced past the provider step


def test_wizard_pilot_ctrl_s_skips_without_recording():
    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState

    async def drive() -> SetupWizardApp:
        app = SetupWizardApp(WizardState())
        async with app.run_test() as pilot:
            await pilot.press("ctrl+s")  # skip hosting without choosing
            await pilot.pause()
        return app

    app = asyncio.run(drive())
    assert app.state.hosting is None
    assert app.nav.current() == 1


def test_finalize_writes_config_without_provider_when_skipped(tmp_path):
    from rich.console import Console

    from nymeria.setup.state import WizardState

    root = tmp_path / "runtime"
    state = WizardState(provider=None, root=root, skip_llm_test=True)

    rc = finalize_mod.finalize(
        state, console=Console(), non_interactive=False, overwrite_confirmed=True
    )

    config = (root / "config.env").read_text(encoding="utf-8")
    assert rc == 0
    assert "LLM_PROVIDER" not in config
    assert "DATABASE_BACKEND=sqlite" in config
    assert (root / "data" / "accounts.db").exists()


# --- registry-backed provider catalog ---------------------------------------


def test_grouped_provider_specs_tiers_and_membership():
    from nymeria.config.llm_providers import ALL_LLM_PROVIDERS
    from nymeria.setup.providers import grouped_provider_specs

    groups = grouped_provider_specs()
    assert [label for label, _ in groups][:3] == [
        "Native reasoning",
        "Gateway",
        "Unverified",
    ]

    tier_of = {spec.id: label for label, specs in groups for spec in specs}
    assert tier_of["anthropic"] == "Native reasoning"
    assert tier_of["openai"] == "Native reasoning"
    assert tier_of["openrouter"] == "Gateway"
    assert tier_of["groq"] == "Unverified"

    all_ids = [spec.id for _label, specs in groups for spec in specs]
    assert len(all_ids) == len(set(all_ids))  # no duplicates
    assert set(all_ids) == set(ALL_LLM_PROVIDERS)  # every provider present

    for _label, specs in groups:  # each group stays label-sorted
        labels = [spec.label.lower() for spec in specs]
        assert labels == sorted(labels)


def test_filter_items_substring_and_headers():
    from nymeria.setup.widgets import ListItem, filter_items

    items = [
        ListItem(value="", primary="Native", is_header=True),
        ListItem(value="anthropic", primary="Anthropic"),
        ListItem(value="openai", primary="OpenAI"),
        ListItem(value="", primary="Gateway", is_header=True),
        ListItem(value="openrouter", primary="OpenRouter"),
    ]
    assert filter_items(items, "") == items  # empty query returns everything

    result = filter_items(items, "OPEN")  # case-insensitive substring
    assert [i.value for i in result if not i.is_header] == ["openai", "openrouter"]
    # a header survives only when its group still has a visible row
    assert [i.primary for i in result if i.is_header] == ["Native", "Gateway"]

    assert filter_items(items, "zzzzz") == []  # no matches -> nothing (no headers)


# --- live model listing -----------------------------------------------------


class _FakeModelsClient:
    response_status = 200
    response_body: dict | None = None
    calls: list[dict] = []

    def __init__(self, *, timeout):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    async def get(self, url, *, headers):
        import httpx

        type(self).calls.append({"url": url, "headers": headers})
        return httpx.Response(
            self.response_status,
            json=self.response_body or {"data": []},
            request=httpx.Request("GET", url),
        )


def _install_fake_models_client(monkeypatch, *, status=200, body=None):
    import httpx

    _FakeModelsClient.calls = []
    _FakeModelsClient.response_status = status
    _FakeModelsClient.response_body = body
    monkeypatch.setattr(httpx, "AsyncClient", _FakeModelsClient)


def test_fetch_models_for_spec_anthropic(monkeypatch):
    from nymeria.config.llm_providers import get_llm_provider_spec
    from nymeria.setup.providers import fetch_models_for_spec

    _install_fake_models_client(
        monkeypatch, body={"data": [{"id": "claude-3"}, {"id": "claude-2"}]}
    )
    models = asyncio.run(
        fetch_models_for_spec(get_llm_provider_spec("anthropic"), api_key="sk-ant-x")
    )

    assert [m.id for m in models] == ["claude-2", "claude-3"]  # sorted by id
    call = _FakeModelsClient.calls[0]
    assert call["url"] == "https://api.anthropic.com/v1/models"
    assert call["headers"]["x-api-key"] == "sk-ant-x"
    assert call["headers"]["anthropic-version"] == "2023-06-01"


def test_fetch_models_for_spec_openai_compatible(monkeypatch):
    from nymeria.config.llm_providers import get_llm_provider_spec
    from nymeria.setup.providers import fetch_models_for_spec

    _install_fake_models_client(
        monkeypatch, body={"data": [{"id": "deepseek-chat", "name": "DeepSeek Chat"}]}
    )
    models = asyncio.run(
        fetch_models_for_spec(get_llm_provider_spec("deepseek"), api_key="sk-deepseek")
    )

    assert [(m.id, m.name) for m in models] == [("deepseek-chat", "DeepSeek Chat")]
    call = _FakeModelsClient.calls[0]
    assert call["url"] == "https://api.deepseek.com/models"
    assert call["headers"]["Authorization"] == "Bearer sk-deepseek"


def test_fetch_models_for_spec_local_substitutes_not_needed(monkeypatch):
    from nymeria.config.llm_providers import get_llm_provider_spec
    from nymeria.setup.providers import fetch_models_for_spec

    _install_fake_models_client(monkeypatch, body={"data": [{"id": "local-model"}]})
    models = asyncio.run(
        fetch_models_for_spec(
            get_llm_provider_spec("lmstudio"),
            api_key="",
            base_url="http://localhost:1234/v1",
        )
    )

    assert [m.id for m in models] == ["local-model"]
    call = _FakeModelsClient.calls[0]
    assert call["url"] == "http://localhost:1234/v1/models"
    assert call["headers"]["Authorization"] == "Bearer not-needed"


def test_fetch_models_for_spec_returns_empty_on_http_error(monkeypatch):
    from nymeria.config.llm_providers import get_llm_provider_spec
    from nymeria.setup.providers import fetch_models_for_spec

    _install_fake_models_client(monkeypatch, status=401, body={"error": "nope"})
    models = asyncio.run(
        fetch_models_for_spec(get_llm_provider_spec("openai"), api_key="sk-bad")
    )
    assert models == []


# --- finalize for registry providers beyond the original three --------------


def test_noninteractive_writes_registry_provider(monkeypatch, tmp_path):
    from nymeria.config.llm_providers import get_llm_provider_spec

    _stub_llm(monkeypatch)
    root = tmp_path / "runtime"

    rc = setup_main(
        [
            "--provider", "deepseek",
            "--model", "deepseek-chat",
            "--api-key", "sk-deepseek-test",
            "--root", str(root),
            "--non-interactive",
            "--skip-llm-test",
        ]
    )

    config = (root / "config.env").read_text(encoding="utf-8")
    env_var = get_llm_provider_spec("deepseek").api_key_env_vars[0]
    assert rc == 0
    assert "LLM_PROVIDER=deepseek" in config
    assert f"{env_var}=sk-deepseek-test" in config


def test_noninteractive_requires_base_url_provider(monkeypatch, tmp_path):
    from nymeria.config.llm_providers import get_llm_provider_spec

    _stub_llm(monkeypatch)
    root = tmp_path / "runtime"

    rc = setup_main(
        [
            "--provider", "azure-openai",
            "--model", "gpt-4o",
            "--api-key", "az-test-key",
            "--base-url", "https://example.openai.azure.com",
            "--root", str(root),
            "--non-interactive",
            "--skip-llm-test",
        ]
    )

    config = (root / "config.env").read_text(encoding="utf-8")
    env_var = get_llm_provider_spec("azure-openai").api_key_env_vars[0]
    assert rc == 0
    assert "LLM_PROVIDER=azure-openai" in config
    assert "LLM_BASE_URL=https://example.openai.azure.com" in config
    assert f"{env_var}=az-test-key" in config


def test_noninteractive_requires_base_url_provider_errors_without_base_url(tmp_path):
    root = tmp_path / "runtime"
    with pytest.raises(SystemExit) as exc_info:
        setup_main(
            [
                "--provider", "azure-openai",
                "--model", "gpt-4o",
                "--api-key", "az-test-key",
                "--root", str(root),
                "--non-interactive",
                "--skip-llm-test",
            ]
        )
    assert "base-url" in str(exc_info.value)
    assert not (root / "config.env").exists()


def test_noninteractive_local_provider_without_key(monkeypatch, tmp_path):
    _stub_llm(monkeypatch)
    root = tmp_path / "runtime"

    rc = setup_main(
        [
            "--provider", "lmstudio",
            "--model", "local-model",
            "--root", str(root),
            "--non-interactive",
            "--skip-llm-test",
        ]
    )

    config = (root / "config.env").read_text(encoding="utf-8")
    assert rc == 0
    assert "LLM_PROVIDER=lmstudio" in config
    assert "LMSTUDIO_API_KEY" not in config  # no key line for a keyless provider
