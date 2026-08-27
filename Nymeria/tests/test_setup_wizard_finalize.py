"""Headless finalize: config write, bootstrap token, run.py init subparser, registry providers.

Split out of the former monolithic test_setup_wizard.py (dev-todo #54).
"""


from __future__ import annotations

import asyncio
import re
import stat
from pathlib import Path
import pytest
from nymeria.setup import finalize as finalize_mod
from nymeria.setup.runner import main as setup_main

from _setup_wizard_helpers import (  # type: ignore[import-not-found]
    _capture_console,
    _init_state,
    _read_secrets_key,
    _stub_llm,
)


# --- headless finalize ------------------------------------------------------


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


def test_noninteractive_bootstrap_mint_honors_configured_ttl(monkeypatch, tmp_path):
    # Backlog #107 fold-in: finalize's AccountsRepo construction was the third
    # site still on constructor defaults, so the wizard's bootstrap-admin mint
    # ignored ACCOUNT_BOOTSTRAP_TOKEN_TTL_HOURS. Mirrors
    # test_cli_users.py::test_issue_token_honors_configured_ttl.
    from datetime import timedelta

    from nymeria.core.accounts import AccountsRepo, _parse_timestamp

    _stub_llm(monkeypatch)
    monkeypatch.setenv("ACCOUNT_BOOTSTRAP_TOKEN_TTL_HOURS", "48")
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
    assert rc == 0

    repo = AccountsRepo(root / "data" / "accounts.db")
    tokens = [
        t for t in repo.list_tokens_for_user("default") if t.label == "bootstrap"
    ]
    assert len(tokens) == 1
    created = _parse_timestamp(tokens[0].created_at)
    expires = _parse_timestamp(tokens[0].expires_at)
    assert created is not None and expires is not None
    assert expires - created == timedelta(hours=48)


def test_noninteractive_init_survives_invalid_preexisting_account_env(
    monkeypatch, tmp_path
):
    # init is deliberately validation-free (run.py registers it with
    # full_validation=False) so a wizard re-run can FIX a broken config. The
    # #107 fold-in's settings read must therefore never crash finalize on an
    # invalid pre-existing value: it falls back to constructor defaults and
    # the bootstrap admin still mints (review finding, 2026-07-26).
    _stub_llm(monkeypatch)
    monkeypatch.setenv("ACCOUNT_TOKEN_TTL_DAYS", "0")  # ge=1: invalid
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

    assert rc == 0
    assert (root / "data" / "BOOTSTRAP_TOKEN.txt").exists()


def test_default_init_root_honors_runtime_project_root(monkeypatch, tmp_path):
    # Regression: an editable/source install resolves NYMERIA_PROJECT_ROOT to the
    # checkout, and the runtime (settings._get_project_root) honors it first. init
    # must write THERE too, or it writes config the backend never loads. A prior
    # version rejected checkout roots and fell back to ~/.nymeria, splitting the
    # two apart (wrong provider/model, a second accounts.db).
    checkout = tmp_path / "Nymeria"
    (checkout / "nymeria" / "config").mkdir(parents=True)
    (checkout / "run.py").write_text("", encoding="utf-8")
    (checkout / "nymeria" / "config" / "soul.md").write_text("", encoding="utf-8")
    monkeypatch.setenv("NYMERIA_PROJECT_ROOT", str(checkout))
    assert finalize_mod.default_init_root() == checkout.resolve()

    plain = tmp_path / "plain-root"
    plain.mkdir()
    monkeypatch.setenv("NYMERIA_PROJECT_ROOT", str(plain))
    assert finalize_mod.default_init_root() == plain.resolve()


def test_print_credentials_default_on_interactive_off_headless():
    # Interactive wizard (no --non-interactive): show the URL + token by default.
    assert _init_state().print_credentials is True
    # Headless: hide by default so a token never lands in captured/scripted stdout.
    assert _init_state("--non-interactive").print_credentials is False
    # An explicit flag wins in either mode.
    assert _init_state("--non-interactive", "--print-creds").print_credentials is True
    assert _init_state("--print-creds").print_credentials is True
    assert _init_state("--no-print-creds").print_credentials is False
    assert _init_state("--non-interactive", "--no-print-creds").print_credentials is False


def test_noninteractive_print_creds_echoes_token_url_and_cli(
    monkeypatch, tmp_path, capsys
):
    # The opt-in headless path prints the connection details so an automated
    # provision can capture them: the token value, the URL, and `nymeria cli`.
    _stub_llm(monkeypatch)
    root = tmp_path / "runtime"

    rc = setup_main(
        [
            "--provider", "anthropic",
            "--model", "claude-test-model",
            "--api-key", "sk-ant-test-key",
            "--root", str(root),
            "--non-interactive",
            "--print-creds",
        ]
    )

    out = capsys.readouterr().out
    token_path = root / "data" / "BOOTSTRAP_TOKEN.txt"
    raw_token = re.search(  # type: ignore[missing-attribute]
        r"nym_[A-Za-z0-9_-]+", token_path.read_text(encoding="utf-8")
    ).group(0)

    assert rc == 0
    assert raw_token in out  # opted in -> the token value is printed
    assert "nymeria cli" in out
    assert "http://localhost:8000" in out


def test_review_token_row_is_reconfigure_honest():
    # The review 'Token' row must reflect what finalize will actually print, so it
    # never promises a token on a reconfigure (where none is freshly minted).
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.review import _token_summary, print_creds_applies
    from nymeria.onboarding import HostingOption, NextAction

    fresh_on = WizardState(hosting=HostingOption.LOCAL, print_credentials=True)
    fresh_off = WizardState(hosting=HostingOption.LOCAL, print_credentials=False)
    assert _token_summary(fresh_on) == "print URL + token on finish"
    assert _token_summary(fresh_off) == "hidden (saved to file)"

    # Reconfigure: the token was already issued, so the row says so regardless of
    # the toggle (the finalizer will not reprint it).
    reconfig = WizardState(
        hosting=HostingOption.LOCAL, print_credentials=True, reconfigure=True
    )
    assert _token_summary(reconfig) == "already issued (see data/BOOTSTRAP_TOKEN.txt)"

    # Shapes that never print a host-side token get no row at all.
    docker = WizardState(hosting=HostingOption.DOCKER, print_credentials=True)
    cli = WizardState(
        hosting=HostingOption.LOCAL,
        print_credentials=True,
        next_action=NextAction.CLI,
    )
    assert _token_summary(docker) is None
    assert _token_summary(cli) is None
    assert print_creds_applies(docker) is False


def test_review_hint_advertises_toggle_only_when_actionable():
    # The footer hint should advertise `t` only where it does something.
    from nymeria.setup.steps.review import make_review_step
    from nymeria.setup.state import WizardState
    from nymeria.onboarding import HostingOption

    class _Wiz:
        def __init__(self, state):
            self.state = state

    build = make_review_step().build
    fresh = build(_Wiz(WizardState(hosting=HostingOption.LOCAL)), 1, 1)
    docker = build(_Wiz(WizardState(hosting=HostingOption.DOCKER)), 1, 1)
    reconfig = build(
        _Wiz(WizardState(hosting=HostingOption.LOCAL, reconfigure=True)), 1, 1
    )
    assert "t toggle token" in fresh._hint  # type: ignore[missing-attribute]
    assert "t toggle token" not in docker._hint  # type: ignore[missing-attribute]
    assert "t toggle token" not in reconfig._hint  # type: ignore[missing-attribute]


def test_review_pilot_t_key_toggles_token_print():
    # The toggle lives on the review screen itself (no separate step, so it shows
    # identically in quickstart and full). Pressing `t` flips print_credentials.
    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.review import ReviewStep
    from nymeria.onboarding import HostingOption

    async def drive() -> WizardState:
        state = WizardState(hosting=HostingOption.LOCAL, print_credentials=True)
        app = SetupWizardApp(state)
        async with app.run_test() as pilot:
            await app.push_screen(
                ReviewStep(
                    app, 1, 1, step_id="review", title="Review", note="", hint=""
                )
            )
            await pilot.pause()
            await pilot.press("t")
            await pilot.pause()
            assert state.print_credentials is False
            await pilot.press("t")
            await pilot.pause()
            assert state.print_credentials is True
        return state

    asyncio.run(drive())


def test_review_pilot_t_key_is_inert_on_reconfigure():
    # On a reconfigure there is no fresh token to print, so the toggle does
    # nothing (and the row says "already issued").
    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.review import ReviewStep
    from nymeria.onboarding import HostingOption

    async def drive() -> WizardState:
        state = WizardState(
            hosting=HostingOption.LOCAL, print_credentials=True, reconfigure=True
        )
        app = SetupWizardApp(state)
        async with app.run_test() as pilot:
            await app.push_screen(
                ReviewStep(
                    app, 1, 1, step_id="review", title="Review", note="", hint=""
                )
            )
            await pilot.pause()
            await pilot.press("t")
            await pilot.pause()
            assert state.print_credentials is True  # unchanged
        return state

    asyncio.run(drive())


def test_noninteractive_no_print_creds_suppresses_token(monkeypatch, tmp_path, capsys):
    # Even on a fresh bootstrap, --no-print-creds keeps the token value off stdout
    # (the non-secret `nymeria cli` command still prints).
    _stub_llm(monkeypatch)
    root = tmp_path / "runtime"

    rc = setup_main(
        [
            "--provider", "anthropic",
            "--model", "claude-test-model",
            "--api-key", "sk-ant-test-key",
            "--root", str(root),
            "--non-interactive",
            "--no-print-creds",
        ]
    )

    out = capsys.readouterr().out
    token_path = root / "data" / "BOOTSTRAP_TOKEN.txt"
    raw_token = re.search(  # type: ignore[missing-attribute]
        r"nym_[A-Za-z0-9_-]+", token_path.read_text(encoding="utf-8")
    ).group(0)

    assert rc == 0
    assert raw_token not in out  # opted out -> token value never echoed
    assert "nymeria cli" in out


def test_noninteractive_write_avoids_leftover_env_docker(monkeypatch, tmp_path):
    # A fresh local init must not capture its config into a leftover `.env.docker`
    # (e.g. from a prior Docker init in this root), which get_env_write_path would
    # otherwise pick as the highest-precedence existing dotenv. --force skips the
    # hydrate that would auto-detect the Docker shape, so the guard is exercised.
    _stub_llm(monkeypatch)
    root = tmp_path / "runtime"
    root.mkdir()
    (root / ".env.docker").write_text("NYMERIA_VERSION=0.0.0\n", encoding="utf-8")

    rc = setup_main(
        [
            "--provider", "anthropic",
            "--model", "claude-test-model",
            "--api-key", "sk-ant-test-key",
            "--root", str(root),
            "--hosting", "local",
            "--force",
            "--non-interactive",
        ]
    )

    assert rc == 0
    assert "LLM_PROVIDER=anthropic" in (root / "config.env").read_text(encoding="utf-8")
    # The Docker-shape file is left untouched, not repurposed for local config.
    assert "LLM_PROVIDER" not in (root / ".env.docker").read_text(encoding="utf-8")


def test_noninteractive_defaults_to_free_local_rag_when_no_embedder_chosen(
    monkeypatch, tmp_path
):
    # A run that names no embedder (the wizard RAG steps skipped, or a headless
    # run) still gets working out-of-the-box semantic memory: the free, private
    # local stack (granite + Ettin), no API key required.
    _stub_llm(monkeypatch)
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
    assert rc == 0
    assert "EMBEDDING_PROVIDER=local" in config
    assert "EMBEDDING_MODEL=ibm-granite/granite-embedding-small-english-r2" in config
    assert "EMBEDDING_DIMENSIONS=384" in config
    assert "RAG_RERANK_ENABLED=true" in config
    assert "RAG_RERANK_PROVIDER=local" in config
    # Nothing paid is implied: no embedding/rerank API key is written.
    assert "EMBEDDING_API_KEY=" not in config
    assert "RAG_RERANK_API_KEY=" not in config


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


def test_noninteractive_writes_backend_keys_and_seeds_default_tools(monkeypatch, tmp_path):
    import json

    _stub_llm(monkeypatch)
    root = tmp_path / "runtime"

    rc = setup_main(
        [
            "--provider", "anthropic",
            "--model", "claude-test-model",
            "--api-key", "sk-ant-test-key",
            "--web-search", "web_search_tavily",
            "--tavily-api-key", "tav-secret",
            "--fetch-url", "fetch_url_nymeria",
            "--image-gen", "image_gen_gemini",
            "--gemini-api-key", "gemini-secret",
            "--skill-kit", "tool-management",
            "--skill-kit", "mcp-management",
            "--root", str(root),
            "--non-interactive",
        ]
    )

    assert rc == 0
    config = (root / "config.env").read_text(encoding="utf-8")
    assert "TAVILY_API_KEY=tav-secret" in config
    assert "GEMINI_API_KEY=gemini-secret" in config

    profile_path = root / "data" / "users" / "default" / "profile.json"
    assert profile_path.exists()
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    default_tools = profile["tool_preferences"]["default_thread_tools"]
    # Picked optional backends are promoted into the default thread tools...
    assert "web_search_tavily" in default_tools
    assert "image_gen_gemini" in default_tools
    assert "fetch_url_nymeria" in default_tools
    # ...alongside the always-on core seed.
    assert "bash_execute" in default_tools and "memory_read" in default_tools
    # The chosen capability kits seed enabled_global_skills, always led by the
    # self-improve guidance skill.
    assert profile["enabled_global_skills"] == [
        "self-improve",
        "tool-management",
        "mcp-management",
    ]


def test_init_does_not_clobber_existing_profile(monkeypatch, tmp_path):
    import json

    from nymeria.core.user_profile import UserProfileManager

    _stub_llm(monkeypatch)
    root = tmp_path / "runtime"
    data_dir = root / "data"

    # First run seeds the profile with a Tavily pick.
    assert setup_main(
        [
            "--provider", "anthropic", "--model", "m", "--api-key", "sk-ant-x",
            "--web-search", "web_search_tavily", "--tavily-api-key", "k",
            "--root", str(root), "--non-interactive",
        ]
    ) == 0

    # Simulate the user customizing their defaults afterward.
    manager = UserProfileManager(data_dir)
    profile = manager.get_profile("default")
    profile.tool_preferences.default_thread_tools = ["bash_execute"]
    manager.save_profile(profile)

    # Re-running init must not overwrite the customized profile.
    assert setup_main(
        [
            "--provider", "anthropic", "--model", "m", "--api-key", "sk-ant-x",
            "--web-search", "web_search_brave", "--brave-api-key", "k2",
            "--root", str(root), "--non-interactive", "--force",
        ]
    ) == 0
    after = json.loads(
        (data_dir / "users" / "default" / "profile.json").read_text(encoding="utf-8")
    )
    assert after["tool_preferences"]["default_thread_tools"] == ["bash_execute"]


def test_docker_shape_carries_init_picks_in_env_docker(monkeypatch, tmp_path):
    """Docker cannot seed the container's volume, so the picks ride in `.env.docker`.

    The host writes no profile.json for the Docker shape (the container mints its
    own); instead the bootstrap admin's default_thread_tools and
    enabled_global_skills are carried as `:`-joined, UNQUOTED env vars the container
    reads on first boot.
    """
    from nymeria.config.init_seed_env import (
        INIT_DEFAULT_THREAD_TOOLS_ENV,
        INIT_ENABLED_GLOBAL_SKILLS_ENV,
    )

    _stub_llm(monkeypatch)
    root = tmp_path / "checkout"

    rc = setup_main(
        [
            "--hosting", "docker",
            "--provider", "anthropic", "--model", "m", "--api-key", "sk-ant-x",
            "--web-search", "web_search_tavily", "--tavily-api-key", "k",
            "--image-gen", "image_gen_gemini", "--gemini-api-key", "g",
            "--skill-kit", "tool-management", "--skill-kit", "mcp-management",
            "--root", str(root), "--non-interactive",
        ]
    )
    assert rc == 0

    # Docker writes `.env.docker`, not config.env, and seeds no host profile.
    assert not (root / "config.env").exists()
    assert not (root / "data" / "users" / "default" / "profile.json").exists()
    env_docker = (root / ".env.docker").read_text(encoding="utf-8")

    lines = {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in env_docker.splitlines()
        if "=" in line and not line.startswith("#")
    }
    tools_value = lines[INIT_DEFAULT_THREAD_TOOLS_ENV]
    assert '"' not in tools_value  # unquoted: Docker env_file quoting never exercised
    tools = tools_value.split(":")
    # Picked optional backends ride alongside the always-on core seed.
    assert "web_search_tavily" in tools and "image_gen_gemini" in tools
    assert "bash_execute" in tools and "memory_read" in tools
    # Skills: self-improve plus exactly the picked kits, order preserved.
    assert lines[INIT_ENABLED_GLOBAL_SKILLS_ENV].split(":") == [
        "self-improve",
        "tool-management",
        "mcp-management",
    ]


def test_docker_shape_no_picks_writes_no_init_seed_vars(monkeypatch, tmp_path):
    """No optional picks -> NO carriers at all (the container's own core-seed
    and default-skill migrations run unchanged). Since 2026-08-27 the
    wizard's default-checked kit set matches the backend's
    DEFAULT_GLOBAL_SKILLS fallback (membership AND order), so the skills
    carrier is written only when the user's picks actually differ."""
    from nymeria.config.init_seed_env import (
        INIT_DEFAULT_THREAD_TOOLS_ENV,
        INIT_ENABLED_GLOBAL_SKILLS_ENV,
    )

    _stub_llm(monkeypatch)
    root = tmp_path / "checkout"

    rc = setup_main(
        [
            "--hosting", "docker",
            "--provider", "anthropic", "--model", "m", "--api-key", "sk-ant-x",
            "--root", str(root), "--non-interactive",
        ]
    )
    assert rc == 0
    env_docker = (root / ".env.docker").read_text(encoding="utf-8")
    assert INIT_DEFAULT_THREAD_TOOLS_ENV not in env_docker
    assert INIT_ENABLED_GLOBAL_SKILLS_ENV not in env_docker


def test_local_shape_writes_no_init_seed_vars(monkeypatch, tmp_path):
    """The carrier vars are Docker-only: local hosting seeds the profile directly,
    so config.env must never carry them even when picks are made."""
    from nymeria.config.init_seed_env import (
        INIT_DEFAULT_THREAD_TOOLS_ENV,
        INIT_ENABLED_GLOBAL_SKILLS_ENV,
    )

    _stub_llm(monkeypatch)
    root = tmp_path / "runtime"

    rc = setup_main(
        [
            "--hosting", "local",
            "--provider", "anthropic", "--model", "m", "--api-key", "sk-ant-x",
            "--web-search", "web_search_tavily", "--tavily-api-key", "k",
            "--root", str(root), "--non-interactive",
        ]
    )
    assert rc == 0
    config = (root / "config.env").read_text(encoding="utf-8")
    assert INIT_DEFAULT_THREAD_TOOLS_ENV not in config
    assert INIT_ENABLED_GLOBAL_SKILLS_ENV not in config


def test_noninteractive_records_deployment_choices_without_dead_config(
    monkeypatch, tmp_path, capsys
):
    _stub_llm(monkeypatch)
    root = tmp_path / "runtime"

    rc = setup_main(
        [
            "--provider", "anthropic",
            "--model", "claude-test-model",
            "--api-key", "sk-ant-test-key",
            "--security-profile", "unleashed",
            "--external-access", "tailscale",
            "--root", str(root),
            "--non-interactive",
            "--skip-llm-test",
        ]
    )

    out = capsys.readouterr().out
    config = (root / "config.env").read_text(encoding="utf-8")
    assert rc == 0
    # Both choices are surfaced to the operator...
    assert "Security profile" in out
    assert "Unleashed" in out
    assert "External access" in out and "Tailscale" in out
    # ...security profile stays recorded-only (no dead config), while the
    # external-access choice round-trips through its env marker so a
    # reconfigure can hydrate it (no public URL was set up, so none writes).
    assert "SECURITY_PROFILE" not in config
    assert "NYMERIA_EXTERNAL_ACCESS=tailscale" in config
    assert "NYMERIA_PUBLIC_URL" not in config


@pytest.mark.parametrize("profile", ["secure", "standard"])
def test_security_profile_flag_rejects_unbuilt_profiles(tmp_path, profile):
    """secure/standard enforce nothing yet; accepting them would hand scripted
    installs a false sense of security, so the flag fails loudly.
    """
    root = tmp_path / "runtime"
    with pytest.raises(SystemExit) as exc_info:
        setup_main(
            [
                "--provider", "anthropic",
                "--model", "claude-test-model",
                "--api-key", "sk-ant-test-key",
                "--security-profile", profile,
                "--root", str(root),
                "--non-interactive",
                "--skip-llm-test",
            ]
        )
    assert "not available yet" in str(exc_info.value)
    assert "unleashed" in str(exc_info.value)
    assert not (root / "config.env").exists()


def test_env_value_leaves_base64_unquoted():
    # A Fernet key is url-safe base64 ending in `=`; it must write unquoted so
    # Docker `env_file` does not treat the quotes literally. Asserts against the
    # canonical `format_env_value`; finalize's `_env_value` is just an alias of it.
    from nymeria.config.env_file import format_env_value

    key = "5KFavWE8-H-C5jk11S6vogyg-s50WyVBvAPY6ZXzuns="
    assert format_env_value(key) == key


# --- bootstrap profile seeding (_apply_profile_picks shared core) -----------


def _picks_state():
    from nymeria.setup.state import WizardState

    return WizardState()


def test_apply_profile_picks_sets_and_persists(tmp_path):
    # The shared apply-and-save core writes the recomputed picks to disk and
    # reports their counts, identical to what both callers expect.
    from nymeria.core.user_profile import UserProfileManager
    from nymeria.setup.tool_seed import (
        default_thread_tools_for_state,
        selected_global_skills_for_state,
    )

    state = _picks_state()
    expected_tools = default_thread_tools_for_state(state)
    expected_skills = selected_global_skills_for_state(state)

    manager = UserProfileManager(tmp_path)
    profile = manager.get_profile("default")
    counts = finalize_mod._apply_profile_picks(manager, profile, state)

    assert counts == (len(expected_tools), len(expected_skills))
    reloaded = UserProfileManager(tmp_path).get_profile("default")
    assert reloaded.tool_preferences.default_thread_tools == expected_tools
    assert reloaded.enabled_global_skills == expected_skills


def test_seed_bootstrap_profile_writes_then_is_idempotent(tmp_path):
    from nymeria.core.user_profile import UserProfileManager
    from nymeria.setup.tool_seed import default_thread_tools_for_state

    state = _picks_state()
    console, _ = _capture_console()
    finalize_mod.seed_bootstrap_profile(tmp_path, state, console)

    mgr = UserProfileManager(tmp_path)
    assert (
        mgr.get_profile("default").tool_preferences.default_thread_tools
        == default_thread_tools_for_state(state)
    )

    # A second seed must never clobber a profile the user has since customized.
    profile = mgr.get_profile("default")
    profile.tool_preferences.default_thread_tools = ["only_custom_tool"]
    mgr.save_profile(profile)
    finalize_mod.seed_bootstrap_profile(tmp_path, state, console)
    assert UserProfileManager(tmp_path).get_profile(
        "default"
    ).tool_preferences.default_thread_tools == ["only_custom_tool"]


def test_update_bootstrap_profile_recomputes_existing(tmp_path):
    from nymeria.core.user_profile import UserProfileManager
    from nymeria.setup.tool_seed import (
        default_thread_tools_for_state,
        selected_global_skills_for_state,
    )

    state = _picks_state()
    console, _ = _capture_console()
    finalize_mod.seed_bootstrap_profile(tmp_path, state, console)

    # Simulate a stale profile, then reconfigure it in place.
    mgr = UserProfileManager(tmp_path)
    profile = mgr.get_profile("default")
    profile.tool_preferences.default_thread_tools = ["stale"]
    profile.enabled_global_skills = ["stale-skill"]
    mgr.save_profile(profile)

    finalize_mod.update_bootstrap_profile(tmp_path, state, console)

    reloaded = UserProfileManager(tmp_path).get_profile("default")
    assert reloaded.tool_preferences.default_thread_tools == default_thread_tools_for_state(state)
    assert reloaded.enabled_global_skills == selected_global_skills_for_state(state)


def test_update_bootstrap_profile_scoped_noop_then_seed_fallback(tmp_path):
    from nymeria.core.user_profile import UserProfileManager
    from nymeria.setup.tool_seed import default_thread_tools_for_state

    state = _picks_state()
    console, _ = _capture_console()
    manager = UserProfileManager(tmp_path)

    # A scoped jump outside the pick sections cannot have changed the profile.
    finalize_mod.update_bootstrap_profile(
        tmp_path, state, console, scoped_section="provider"
    )
    assert not manager._get_profile_path("default").exists()

    # An in-pick scoped jump with no profile yet falls back to seeding.
    finalize_mod.update_bootstrap_profile(
        tmp_path, state, console, scoped_section="web_search"
    )
    assert (
        UserProfileManager(tmp_path)
        .get_profile("default")
        .tool_preferences.default_thread_tools
        == default_thread_tools_for_state(state)
    )


def test_noninteractive_mints_and_preserves_secrets_key(monkeypatch, tmp_path):
    from cryptography.fernet import Fernet

    _stub_llm(monkeypatch)
    # Resolution reads the existing key from the env; isolate the test from any
    # NYMERIA_SECRETS_KEY in the ambient environment.
    monkeypatch.delenv("NYMERIA_SECRETS_KEY", raising=False)
    root = tmp_path / "runtime"
    args = [
        "--provider", "anthropic", "--model", "m", "--api-key", "sk-ant-x",
        "--root", str(root), "--non-interactive",
    ]

    assert setup_main(args) == 0
    key1 = _read_secrets_key((root / "config.env").read_text(encoding="utf-8"))
    assert key1 is not None
    Fernet(key1.encode("ascii"))  # a valid Fernet key, does not raise

    # Re-running (even with --force) preserves the same key: rotating it would
    # orphan every secret already encrypted with it.
    assert setup_main(args + ["--force"]) == 0
    key2 = _read_secrets_key((root / "config.env").read_text(encoding="utf-8"))
    assert key2 == key1


def test_noninteractive_docker_writes_env_docker_not_config(monkeypatch, tmp_path):
    from cryptography.fernet import Fernet

    _stub_llm(monkeypatch)
    monkeypatch.delenv("NYMERIA_SECRETS_KEY", raising=False)
    root = tmp_path / "checkout"
    root.mkdir()

    rc = setup_main(
        ["--provider", "anthropic", "--model", "claude-test-model",
         "--api-key", "sk-ant-x", "--hosting", "docker",
         "--root", str(root), "--non-interactive"]
    )
    assert rc == 0

    # Docker hosting writes `.env.docker` (read by the single-container compose),
    # never `config.env`.
    env_docker = root / ".env.docker"
    assert env_docker.exists()
    assert not (root / "config.env").exists()
    content = env_docker.read_text(encoding="utf-8")
    assert "LLM_PROVIDER=anthropic" in content
    assert "ANTHROPIC_DIRECT_API_KEY=sk-ant-x" in content
    assert "API_PORT=8000" in content

    # The credential-vault key is minted for Docker too, and is valid + unquoted.
    key = _read_secrets_key(content)
    assert key is not None
    Fernet(key.encode("ascii"))

    # Host/storage lines are omitted: the compose sets NYMERIA_DATA_DIR=/data and
    # slim forces sqlite, so a host data dir here would only mislead.
    assert "NYMERIA_DATA_DIR" not in content
    assert "DATABASE_BACKEND" not in content
    assert "API_HOST" not in content

    # The container owns its data and mints its own token on first boot, so no
    # host-side bootstrap artifacts are written.
    assert not (root / "data" / "accounts.db").exists()
    assert not (root / "data" / "BOOTSTRAP_TOKEN.txt").exists()


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
            "--auth-method", "api_key",
            "--docker-stack", "full",
            "--security-profile", "unleashed",
            "--external-access", "tailscale",
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
    assert args.auth_method == "api_key"
    assert args.docker_stack == "full"
    assert args.security_profile == "unleashed"
    assert args.external_access == "tailscale"
    assert args.provider == "anthropic"
    assert args.base_url == "http://localhost:8317/v1"
    assert args.api_mode == "responses"
    assert args.next_action == "print_commands"
    assert args.quick is True
    assert args.run_doctor is True
    assert args.full_doctor is True


def test_run_init_parser_accepts_backend_key_and_family_flags():
    from nymeria.setup.runner import _build_state, build_parser

    args = build_parser().parse_args(
        [
            "--provider", "anthropic", "--model", "m", "--api-key", "sk-ant-x",
            "--tavily-api-key", "tav", "--searxng-base-url", "https://sx.example",
            "--fal-api-key", "fal", "--bfl-api-key", "bfl",
            "--web-search", "web_search_tavily", "--web-search", "web_search_searxng",
            "--image-gen", "image_gen_fal",
        ]
    )
    state = _build_state(args)
    assert state.optional_env["TAVILY_API_KEY"] == "tav"
    assert state.optional_env["SEARXNG_BASE_URL"] == "https://sx.example"
    assert state.optional_env["FAL_API_KEY"] == "fal"
    assert state.optional_env["BFL_API_KEY"] == "bfl"
    # Repeatable family flags accumulate into state.extras as concrete tool names.
    assert state.extras["web_search"] == ["web_search_tavily", "web_search_searxng"]
    assert state.extras["image_gen"] == ["image_gen_fal"]


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
    env_var = get_llm_provider_spec("deepseek").api_key_env_vars[0]  # type: ignore[missing-attribute]
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
    env_var = get_llm_provider_spec("azure-openai").api_key_env_vars[0]  # type: ignore[missing-attribute]
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
