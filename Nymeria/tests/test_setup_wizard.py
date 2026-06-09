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


def test_new_deployment_enums_have_ordered_choices_and_one_recommended():
    from nymeria.onboarding import (
        EXTERNAL_ACCESS_CHOICES,
        EXTERNAL_ACCESS_ORDER,
        IMAGE_TIER_CHOICES,
        IMAGE_TIER_ORDER,
        SECURITY_PROFILE_CHOICES,
        SECURITY_PROFILE_ORDER,
        ExternalAccess,
        ImageTier,
        SecurityProfile,
    )

    for order, table, enum_type, recommended in (
        (IMAGE_TIER_ORDER, IMAGE_TIER_CHOICES, ImageTier, ImageTier.MINIMAL),
        (
            SECURITY_PROFILE_ORDER,
            SECURITY_PROFILE_CHOICES,
            SecurityProfile,
            SecurityProfile.STANDARD,
        ),
        (
            EXTERNAL_ACCESS_ORDER,
            EXTERNAL_ACCESS_CHOICES,
            ExternalAccess,
            ExternalAccess.LOCAL_ONLY,
        ),
    ):
        assert set(order) == set(enum_type)  # every member is ordered
        assert set(table) == set(enum_type)  # every member has a choice
        flagged = [opt for opt in order if table[opt].recommended]
        assert flagged == [recommended]  # exactly one, the expected default


def test_core_tools_match_plan_section_a():
    """The Core toolset screen pins the 12 literal tools from
    core-toolset-plan.md Section A (the review screen prints len(CORE_TOOLSET_TARGET)).
    """
    from nymeria.setup.steps.core_tools import CORE_TOOLSET_TARGET

    names = [name for name, _note in CORE_TOOLSET_TARGET]
    assert len(CORE_TOOLSET_TARGET) == 12
    assert names == [
        "bash_execute",
        "file_read",
        "file_write",
        "memory_add",
        "memory_edit",
        "memory_read",
        "nym_todo",
        "nym_todo_delete",
        "nym_todo_list",
        "notify",
        "slash_command",
        "spawn_thread",
    ]


def test_seeded_tool_names_unions_built_families_and_ignores_placeholders():
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.placeholders import seeded_tool_names

    state = WizardState(
        extras={
            "web_search": ["web_search_tavily", "web_search_perplexity"],
            "fetch_url": ["fetch_url_nymeria"],
            "image_gen": ["image_gen_openai", "image_gen_fal"],
            # Placeholder families and single-select capabilities must not leak in.
            "rag_search": "openai-small",
            "tts": "__skip__",
        }
    )
    assert seeded_tool_names(state) == [
        "web_search_tavily",
        "web_search_perplexity",
        "fetch_url_nymeria",
        "image_gen_openai",
        "image_gen_fal",
    ]
    assert seeded_tool_names(WizardState()) == []  # nothing chosen -> empty


def test_default_thread_tools_unions_core_seed_with_picks():
    from nymeria.setup.state import WizardState
    from nymeria.setup.tool_seed import core_seed_tool_names, default_thread_tools_for_state

    core = core_seed_tool_names()
    assert core and "bash_execute" in core and "memory_read" in core
    state = WizardState(
        extras={
            "web_search": ["web_search_tavily"],
            "image_gen": ["image_gen_gemini"],
        }
    )
    result = default_thread_tools_for_state(state)
    # Core comes first, then the picks; deduped and order-preserving.
    assert result[: len(core)] == core
    assert result[len(core):] == ["web_search_tavily", "image_gen_gemini"]
    assert len(result) == len(set(result))  # no duplicates
    # No picks -> exactly the core seed.
    assert default_thread_tools_for_state(WizardState()) == core


def test_selected_global_skills_leads_with_self_improve_and_appends_picks():
    from nymeria.setup.state import WizardState
    from nymeria.setup.tool_seed import selected_global_skills_for_state

    # Explicit picks: self-improve first, then the chosen kits, order-preserving.
    state = WizardState(extras={"skill_kits": ["mcp-management", "tool-management"]})
    assert selected_global_skills_for_state(state) == [
        "self-improve",
        "mcp-management",
        "tool-management",
    ]
    # No picks recorded (step skipped): default to self-improve plus all kits.
    assert selected_global_skills_for_state(WizardState()) == [
        "self-improve",
        "tool-management",
        "skill-management",
        "mcp-management",
        "credential-management",
    ]


def test_required_backend_credentials_handles_url_keyless_and_primary_key():
    from nymeria.setup.state import WizardState
    from nymeria.setup.tool_keys import required_backend_credentials

    # SearXNG is a URL (not required as a secret); fetch_url_nymeria needs nothing;
    # Tavily/OpenAI are required keys.
    state = WizardState(
        extras={
            "web_search": ["web_search_tavily", "web_search_searxng"],
            "fetch_url": ["fetch_url_nymeria", "jina_reader_fetch_url"],
            "image_gen": ["image_gen_openai"],
        }
    )
    by_env = {spec.env_var: spec for spec in required_backend_credentials(state)}
    assert "FETCH" not in "".join(by_env)  # fetch_url_nymeria contributes no key
    assert by_env["TAVILY_API_KEY"].required is True
    assert by_env["SEARXNG_BASE_URL"].kind == "url" and not by_env["SEARXNG_BASE_URL"].required
    assert by_env["JINA_API_KEY"].kind == "optional_key" and not by_env["JINA_API_KEY"].required
    assert "OPENAI_API_KEY" in by_env

    # When OpenAI is the primary provider, its key already covers image_gen_openai.
    primary = WizardState(
        provider="openai",
        api_key="sk-test",
        extras={"image_gen": ["image_gen_openai", "image_gen_fal"]},
    )
    needed = {spec.env_var for spec in required_backend_credentials(primary)}
    assert "OPENAI_API_KEY" not in needed
    assert "FAL_API_KEY" in needed


def test_fetch_dependency_nudge_predicates():
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.placeholders import (
        has_nonperplexity_search,
        unmet_fetch_dependency,
    )

    # Perplexity is self-sufficient: no nudge.
    perplexity = WizardState(extras={"web_search": ["web_search_perplexity"]})
    assert has_nonperplexity_search(perplexity) is False
    assert unmet_fetch_dependency(perplexity) is False

    # A link-only backend with no fetch backend: unmet dependency.
    tavily = WizardState(extras={"web_search": ["web_search_tavily"]})
    assert has_nonperplexity_search(tavily) is True
    assert unmet_fetch_dependency(tavily) is True

    # Same, but a fetch backend was picked: satisfied.
    satisfied = WizardState(
        extras={"web_search": ["web_search_tavily"], "fetch_url": ["fetch_url_nymeria"]}
    )
    assert unmet_fetch_dependency(satisfied) is False


# --- environment detection --------------------------------------------------


def test_recommend_hosting_prefers_container_on_windows_with_docker():
    from nymeria.setup.environment import recommend_hosting

    assert (
        recommend_hosting(is_windows=True, has_docker=True) is HostingOption.DOCKER
    )
    assert (
        recommend_hosting(is_windows=True, has_docker=False) is HostingOption.LOCAL
    )
    assert (
        recommend_hosting(is_windows=False, has_docker=True) is HostingOption.LOCAL
    )


def test_detect_environment_returns_a_report(monkeypatch):
    from nymeria.setup import environment as env_mod

    monkeypatch.setattr(env_mod.shutil, "which", lambda _name: None)
    monkeypatch.setattr(env_mod, "port_free", lambda *_a, **_k: False)
    report = env_mod.detect_environment()

    assert report.docker_available is False
    assert report.port_8000_free is False
    assert report.recommended_hosting in tuple(HostingOption)
    # A busy port is surfaced as a note rather than silently dropped.
    assert any("8000" in note for note in report.notes)


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
    nav.advance()  # crosses the skipped conditional step "c" and lands on "d"
    assert nav.position() == (3, 3)  # the skip must not break the running count


def test_default_flow_order_and_conditional_image_tier():
    from nymeria.setup.nav import Navigator
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps import build_default_steps

    steps = build_default_steps()
    ids = [step.id for step in steps]
    assert ids[0] == "welcome"  # the plan's "detect environment" step comes first
    assert ids[-1] == "review"
    for required in (
        "image_tier",
        "security_profile",
        "auth_method",
        "external_access",
        # core-toolset-plan structure: core set shown, then the family pickers,
        # the keys those backends need, then the default skill-kit selection.
        "core_tools",
        "web_search",
        "fetch_url",
        "embedder",
        "reranker",
        "image_gen",
        "backend_keys",
        "skill_kits",
    ):
        assert required in ids
    assert "rag_search" not in ids  # retired: replaced by embedder + reranker
    # The abstract category "tools" step was superseded by the family pickers.
    assert "tools" not in ids
    # backend_keys must come after all three family pickers so it sees them all.
    assert ids.index("backend_keys") > ids.index("image_gen")

    def applicable_ids(state: "WizardState") -> list[str]:
        nav = Navigator(steps, state)
        nav.start()
        return [steps[i].id for i in nav.applicable_indices()]

    # image_tier is a property of a container image, so it only applies to a
    # container host.
    assert "image_tier" not in applicable_ids(WizardState(hosting=HostingOption.LOCAL))
    assert "image_tier" in applicable_ids(WizardState(hosting=HostingOption.DOCKER))

    # backend_keys is conditional: it appears only when a selected backend needs a
    # credential that has not already been provided.
    assert "backend_keys" not in applicable_ids(WizardState())
    assert "backend_keys" in applicable_ids(
        WizardState(extras={"web_search": ["web_search_tavily"]})
    )
    # A keyless pick (fetch_url_nymeria) does not trigger the step.
    assert "backend_keys" not in applicable_ids(
        WizardState(extras={"fetch_url": ["fetch_url_nymeria"]})
    )


# --- quick path -------------------------------------------------------------


def test_quick_path_gates_steps_to_essentials():
    from nymeria.setup.nav import Navigator
    from nymeria.setup.quick import QUICK_KEEP_STEP_IDS
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps import build_default_steps

    steps = build_default_steps()

    def applicable_ids(state: "WizardState") -> list[str]:
        nav = Navigator(steps, state)
        nav.start()
        return [steps[i].id for i in nav.applicable_indices()]

    skippable = (
        "image_tier", "security_profile", "auth_method", "core_tools",
        "web_search", "fetch_url", "embedder", "reranker", "image_gen",
        "backend_keys", "skill_kits", "tts", "stt", "agent_settings",
        "external_access",
    )

    # Full (default) path keeps the optional/placeholder steps. (reranker and
    # backend_keys are conditional on other picks, so they are excluded here.)
    full = applicable_ids(WizardState(hosting=HostingOption.LOCAL))
    for sid in ("security_profile", "auth_method", "core_tools", "web_search",
                "fetch_url", "embedder", "image_gen", "skill_kits", "external_access"):
        assert sid in full

    # Quick path keeps only the essentials; every skippable step is gated off.
    # A provider is set so the provider-gated model step is applicable (the
    # Navigator re-evaluates this as the real run advances).
    quick = applicable_ids(
        WizardState(hosting=HostingOption.LOCAL, provider="anthropic", quick=True)
    )
    assert set(quick) <= QUICK_KEEP_STEP_IDS
    for essential in ("welcome", "hosting", "provider", "model", "start_now", "review"):
        assert essential in quick
    for sid in skippable:
        assert sid not in quick


def test_apply_quick_defaults_seeds_keyless_fetch_and_local_rag():
    from nymeria.setup.quick import apply_quick_defaults
    from nymeria.setup.rag_catalog import QUICKSTART_EMBEDDER, QUICKSTART_RERANKER
    from nymeria.setup.state import WizardState
    from nymeria.setup.tool_seed import (
        default_thread_tools_for_state,
        selected_global_skills_for_state,
    )

    state = WizardState(quick=True)
    apply_quick_defaults(state)

    # Free local RAG, flagged as the quickstart default (no paid key).
    assert state.embedder == QUICKSTART_EMBEDDER
    assert state.reranker == QUICKSTART_RERANKER
    assert state.rag_quickstarted is True
    assert "EMBEDDING_API_KEY" not in state.optional_env
    # Keyless web fetch seeded; nothing that needs a search/image key.
    assert state.extras["fetch_url"] == ["fetch_url_nymeria"]
    assert "web_search" not in state.extras
    assert "image_gen" not in state.extras
    # The seeded default tools carry the keyless fetcher on top of the core seed.
    tools = default_thread_tools_for_state(state)
    assert "fetch_url_nymeria" in tools
    assert "bash_execute" in tools
    # Skill kits fall back to all-on (self-improve plus the four bundled kits).
    assert selected_global_skills_for_state(state) == [
        "self-improve",
        "tool-management",
        "skill-management",
        "mcp-management",
        "credential-management",
    ]


def test_apply_quick_defaults_respects_explicit_picks():
    from nymeria.setup.quick import apply_quick_defaults
    from nymeria.setup.state import WizardState

    # An explicit embedding key signals the keyed default: local RAG is not forced.
    # An explicit fetch family pick is left untouched.
    state = WizardState(
        quick=True,
        optional_env={"EMBEDDING_API_KEY": "sk-emb"},
        extras={"fetch_url": ["jina_reader_fetch_url"]},
    )
    apply_quick_defaults(state)
    assert state.embedder is None
    assert state.rag_quickstarted is False
    assert state.extras["fetch_url"] == ["jina_reader_fetch_url"]


def test_quick_and_custom_flags_resolve_on_state():
    from nymeria.setup.runner import _build_state, build_parser

    base = ["--provider", "anthropic", "--model", "m", "--api-key", "k"]

    assert _build_state(build_parser().parse_args(base + ["--quick"])).quick is True
    # --custom forces the full walk and wins over --quick.
    assert _build_state(build_parser().parse_args(base + ["--quick", "--custom"])).quick is False
    # Default is the full walk.
    assert _build_state(build_parser().parse_args(base)).quick is False


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
            "--security-profile", "secure",
            "--external-access", "tailscale",
            "--root", str(root),
            "--non-interactive",
            "--skip-llm-test",
        ]
    )

    out = capsys.readouterr().out
    config = (root / "config.env").read_text(encoding="utf-8")
    assert rc == 0
    # The placeholder choices are surfaced to the operator...
    assert "Deployment choices" in out
    assert "Secure" in out
    assert "Tailscale" in out
    # ...but not written as config the backend would ignore.
    assert "SECURITY_PROFILE" not in config
    assert "EXTERNAL_ACCESS" not in config


def _read_secrets_key(text: str) -> str | None:
    match = re.search(r"^NYMERIA_SECRETS_KEY=(\S+)$", text, re.MULTILINE)
    return match.group(1) if match else None


def test_env_value_leaves_base64_unquoted():
    # A Fernet key is url-safe base64 ending in `=`; it must write unquoted so
    # Docker `env_file` does not treat the quotes literally.
    key = "5KFavWE8-H-C5jk11S6vogyg-s50WyVBvAPY6ZXzuns="
    assert finalize_mod._env_value(key) == key


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


def _capture_console():
    import io

    from rich.console import Console

    buf = io.StringIO()
    return Console(file=buf, width=100, force_terminal=False), buf


def test_review_summary_markup_surfaces_collected_choices():
    from nymeria.onboarding import (
        ExternalAccess,
        HostingOption,
        ImageTier,
        ProviderAuthMethod,
        SecurityProfile,
    )
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.review import _summary_markup

    state = WizardState(
        hosting=HostingOption.DOCKER,
        image_tier=ImageTier.STANDARD,
        security_profile=SecurityProfile.SECURE,
        auth_method=ProviderAuthMethod.API_KEY,
        provider="anthropic",
        model="claude-opus-4-8",
        api_key="sk-ant-x",
        external_access=ExternalAccess.TAILSCALE,
        extras={
            "web_search": ["web_search_perplexity"],
            "agent_settings": "thorough",
            "rag_search": "__skip__",
        },
    )
    markup = _summary_markup(state)

    assert "Hosting" in markup
    assert "Image" in markup  # docker host -> image tier surfaces
    assert "Security" in markup
    assert "claude-opus-4-8" in markup
    assert "Default thread tools:" in markup  # core seed + picks, now written
    assert "web_search_perplexity" in markup  # picked family member
    assert "Agent settings: thorough" in markup
    assert "RAG search" not in markup  # a skipped placeholder is not shown
    assert "Tailscale" in markup
    # The post-setup handoff is surfaced (default is print, not start).
    assert "Next" in markup
    assert "print the start command" in markup


def test_print_deployment_summary_suppresses_local_only_and_non_docker_image_tier():
    from nymeria.onboarding import (
        ExternalAccess,
        HostingOption,
        ImageTier,
        SecurityProfile,
    )
    from nymeria.setup.finalize import print_deployment_summary
    from nymeria.setup.state import WizardState

    console, buf = _capture_console()
    state = WizardState(
        hosting=HostingOption.LOCAL,  # not a container host
        image_tier=ImageTier.STANDARD,
        security_profile=SecurityProfile.STANDARD,
        external_access=ExternalAccess.LOCAL_ONLY,  # the recommended default
    )
    print_deployment_summary(state, console)
    out = buf.getvalue()

    assert "Image tier" not in out  # suppressed: image tier is container-only
    assert "External access" not in out  # suppressed: local-only is the default
    assert "Security profile: Standard" in out
    assert "recorded, not yet automated" in out  # honest placeholder framing


def test_print_deployment_summary_is_silent_without_recorded_choices():
    from nymeria.setup.finalize import print_deployment_summary
    from nymeria.setup.state import WizardState

    console, buf = _capture_console()
    print_deployment_summary(WizardState(), console)
    assert buf.getvalue().strip() == ""


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
            "--auth-method", "api_key",
            "--image-tier", "standard",
            "--security-profile", "secure",
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
    assert args.image_tier == "standard"
    assert args.security_profile == "secure"
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


# --- interactive Textual wizard (Pilot) -------------------------------------


async def _no_models(*_args, **_kwargs):
    """Stand-in for the live model fetch so Pilot tests never hit the network."""
    return []


# Step indices in the default flow (welcome, hosting, image_tier, security,
# auth, provider, connection, model, ...). image_tier (2) only applies to a
# container host, so on the default local path the provider step is index 5.
_HOSTING_STEP = 1
_SECURITY_STEP = 3
_AUTH_STEP = 4
_PROVIDER_STEP = 5
_CONNECTION_STEP = 6


async def _advance_to_provider(pilot) -> None:
    """Walk welcome -> hosting -> security -> auth on the default (local) path.

    Accepts every default (local hosting, Standard security, Direct API key) and
    leaves the provider picker focused. image_tier is skipped because the default
    hosting is not a container.
    """
    await pilot.press("enter")  # welcome -> hosting
    await pilot.pause()
    await pilot.press("enter")  # accept default hosting (local) -> security
    await pilot.pause()
    await pilot.press("enter")  # accept default security profile -> auth method
    await pilot.pause()
    await pilot.press("enter")  # accept default auth (Direct API key) -> provider
    await pilot.pause()


def test_wizard_pilot_forward_back_and_provider(monkeypatch):
    from textual.widgets import Input

    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState

    monkeypatch.setattr("nymeria.setup.steps.model.fetch_models_for_spec", _no_models)

    async def drive() -> WizardState:
        state = WizardState()
        app = SetupWizardApp(state)
        async with app.run_test() as pilot:
            assert app.nav.current() == 0  # welcome (environment detection)
            await pilot.press("enter")  # welcome -> hosting
            await pilot.pause()
            assert app.nav.current() == _HOSTING_STEP
            await pilot.press("enter")  # accept default hosting (local), advance
            await pilot.pause()
            assert state.hosting is HostingOption.LOCAL
            # image_tier (index 2) is skipped for a non-container host.
            assert app.nav.current() == _SECURITY_STEP
            await pilot.press("escape")  # back to hosting (does not exit)
            await pilot.pause()
            assert app.nav.current() == _HOSTING_STEP
            await pilot.press("enter")  # hosting -> security profile
            await pilot.pause()
            await pilot.press("enter")  # accept default security -> auth method
            await pilot.pause()
            assert app.nav.current() == _AUTH_STEP
            await pilot.press("enter")  # accept default auth (API key) -> provider
            await pilot.pause()
            assert app.nav.current() == _PROVIDER_STEP
            await pilot.press("enter")  # Enter in picker -> focus moves to key field
            await pilot.pause()
            app.screen.query_one("#api-key", Input).value = "sk-ant-xyz"
            await pilot.press("enter")  # key entered -> advance past provider
            await pilot.pause()
            assert state.provider == "anthropic"
            assert state.api_key == "sk-ant-xyz"
            # Anthropic has a fixed endpoint and no API-mode toggle, so the
            # connection step is skipped; the model step is next.
            assert _CONNECTION_STEP not in app.nav.applicable_indices()
            await pilot.press("enter")  # model step: accept default model, advance
            await pilot.pause()
        return state

    state = asyncio.run(drive())
    assert state.model == "claude-sonnet-4-6"  # provider default model filled in


def test_wizard_pilot_arrow_keys_move_focus_and_description_space_selects():
    """Arrow keys move focus (and the per-option description) WITHOUT changing the
    selection; Space selects the focused option. The selection dot only moves on
    Space (or Enter), not on arrow navigation.
    """
    from textual.widgets import Static

    from nymeria.onboarding import HOSTING_CHOICES, HOSTING_ORDER
    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.base import CircleRadioButton

    def desc_for(index: int) -> str:
        return HOSTING_CHOICES[HOSTING_ORDER[index]].description

    async def drive() -> None:
        app = SetupWizardApp(WizardState())
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")  # welcome -> hosting (the radio screen)
            await pilot.pause()
            scr = app.screen
            panel = scr.query_one("#choice-desc", Static)
            buttons = list(scr.query(CircleRadioButton))

            # Default selection + focus + description all sit on index 0 (LOCAL).
            assert buttons[0].value is True
            assert scr.focused is buttons[0]
            assert str(panel.render()) == desc_for(0)

            await pilot.press("down")
            await pilot.pause()
            # Focus + description moved, but the selection did NOT.
            assert scr.focused is buttons[1]
            assert str(panel.render()) == desc_for(1)
            assert buttons[0].value is True and buttons[1].value is False

            await pilot.press("space")
            await pilot.pause()
            # Space selects the focused option; the old default is cleared.
            assert buttons[1].value is True and buttons[0].value is False
            assert not app.completed  # selecting does not advance

    asyncio.run(drive())


def test_wizard_radio_renders_bare_circles_without_box():
    """The radio indicator is a bare circle (outline when off, filled when on),
    never the stock ``BUTTON_LEFT/RIGHT`` half-block box. Regression for removing
    the blue box around the dial: the box came from those side glyphs, so their
    absence is what proves it is gone.
    """
    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.base import CircleRadioButton

    async def drive() -> None:
        app = SetupWizardApp(WizardState())
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")  # welcome -> hosting (the radio screen)
            await pilot.pause()
            buttons = list(app.screen.query(CircleRadioButton))
            assert buttons  # the hosting step is a single-select radio screen
            for button in buttons:
                glyph = button._button.plain
                assert "▐" not in glyph and "▌" not in glyph  # no box sides
                assert glyph.strip() in {"○", "●"}  # outline / filled circle
            on = [b for b in buttons if b.value]
            assert len(on) == 1  # exactly the selected (highlighted) row
            assert on[0]._button.plain.strip() == "●"
            assert all(
                b._button.plain.strip() == "○" for b in buttons if not b.value
            )

    asyncio.run(drive())


def test_wizard_radio_focused_label_is_bold_and_bright_no_bar():
    """The highlight is the FOCUSED option, marked by bold + brighter text alone,
    with no background bar (the filled white circle pins the selection).
    """
    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.base import CircleRadioButton

    def luma(color) -> int:
        return color.r + color.g + color.b

    async def drive() -> None:
        app = SetupWizardApp(WizardState())
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")  # welcome -> hosting (the radio screen)
            await pilot.pause()
            buttons = list(app.screen.query(CircleRadioButton))
            focused = app.screen.focused
            assert focused in buttons  # the highlight is the focused option
            others = [b for b in buttons if b is not focused]
            assert others

            # No row paints a background: there is no selection bar.
            assert all(b.styles.background.a == 0 for b in buttons)

            foc_label = focused.get_visual_style("toggle--label")
            assert foc_label.bold is True  # the focused row is bold...
            # ...and brighter than every unfocused label, which stay un-bold.
            foc_luma = luma(foc_label.foreground)
            for other in others:
                other_label = other.get_visual_style("toggle--label")
                assert other_label.bold is not True
                assert foc_luma >= luma(other_label.foreground)

    asyncio.run(drive())


def test_wizard_pilot_placeholder_default_preserved_and_enter_locks_focus():
    """A placeholder step's stored default is the trailing "Skip" row. Entering
    keeps that default selected and focused; Enter without moving records "Skip".
    Arrowing moves focus (not selection); Enter then locks the focused option in.
    """
    from textual.widgets import Static

    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.base import CircleRadioButton
    from nymeria.setup.steps.placeholders import make_tts_step

    # tts options (cartesia, openai, gemini, qwen3), with the appended "Skip for
    # now" row last (index 4).
    skip_index = 4

    async def drive_enter_only() -> dict:
        app = SetupWizardApp(WizardState(), steps=[make_tts_step()])
        async with app.run_test() as pilot:
            await pilot.pause()
            scr = app.screen
            buttons = list(scr.query(CircleRadioButton))
            panel = scr.query_one("#choice-desc", Static)
            # Selection, focus, and description all sit on the stored default.
            assert buttons[skip_index].value is True
            assert scr.focused is buttons[skip_index]
            assert str(panel.render()) == "Configure this later."
            await pilot.press("enter")  # commit without moving
            await pilot.pause()
        return dict(app.state.extras)

    async def drive_down_then_enter() -> dict:
        app = SetupWizardApp(WizardState(), steps=[make_tts_step()])
        async with app.run_test() as pilot:
            await pilot.pause()
            scr = app.screen
            buttons = list(scr.query(CircleRadioButton))
            await pilot.press("down")  # Skip is last, so focus_next wraps to first
            await pilot.pause()
            assert scr.focused is buttons[0]  # focus moved...
            assert buttons[skip_index].value is True  # ...but selection did not
            await pilot.press("enter")  # Enter locks the focused option in + advances
            await pilot.pause()
        return dict(app.state.extras)

    assert asyncio.run(drive_enter_only()) == {"tts": "__skip__"}
    assert asyncio.run(drive_down_then_enter()) == {"tts": "cartesia"}


def test_wizard_pilot_selects_non_default_provider(monkeypatch):
    from textual.widgets import Input

    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState

    monkeypatch.setattr("nymeria.setup.steps.model.fetch_models_for_spec", _no_models)

    async def drive() -> SetupWizardApp:
        app = SetupWizardApp(WizardState())
        async with app.run_test() as pilot:
            await _advance_to_provider(pilot)  # land on the provider picker
            await pilot.press(*"openrouter")  # filter the provider list
            await pilot.pause()
            await pilot.press("enter")  # Enter in picker -> focus key field
            await pilot.pause()
            app.screen.query_one("#api-key", Input).value = "sk-or-test"  # model left blank
            await pilot.press("enter")  # advance past provider -> connection step
            await pilot.pause()
            # OpenRouter supports API mode, so the connection step applies.
            assert _CONNECTION_STEP in app.nav.applicable_indices()
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
            await _advance_to_provider(pilot)  # land on the provider picker
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
            await _advance_to_provider(pilot)  # land on the provider picker
            await pilot.press("enter")  # Enter in picker -> focus key field
            await pilot.pause()
            assert app.nav.current() == _PROVIDER_STEP  # picker enter does not advance
            await pilot.press("enter")  # blank key -> error, does not skip
            await pilot.pause()
            assert app.nav.current() == _PROVIDER_STEP
            assert app.state.provider is None
            await pilot.press("ctrl+s")  # explicit skip
            await pilot.pause()
        return app

    app = asyncio.run(drive())
    assert app.state.provider is None
    assert app.nav.current() != _PROVIDER_STEP  # advanced past the provider step


def test_wizard_pilot_ctrl_s_skips_without_recording():
    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState

    async def drive() -> SetupWizardApp:
        app = SetupWizardApp(WizardState())
        async with app.run_test() as pilot:
            await pilot.press("enter")  # welcome -> hosting
            await pilot.pause()
            await pilot.press("ctrl+s")  # skip hosting without choosing
            await pilot.pause()
        return app

    app = asyncio.run(drive())
    assert app.state.hosting is None
    # Hosting unset means a non-container host, so image_tier is skipped and the
    # security profile step is next.
    assert app.nav.current() == _SECURITY_STEP


def test_wizard_pilot_web_search_multiselect_seeds_real_backend():
    """The web search step is a real multi-select over the built web_search_*
    tool names (not abstract categories), and a pick is recorded as a concrete
    tool name that the init flow would seed.
    """
    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.placeholders import make_web_search_step, seeded_tool_names

    async def drive() -> WizardState:
        state = WizardState()
        app = SetupWizardApp(state, steps=[make_web_search_step()])
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("space")  # toggle the first (highlighted) backend
            await pilot.pause()
            await pilot.press("enter")  # advance, storing the selection
            await pilot.pause()
        return state

    state = asyncio.run(drive())
    assert state.extras["web_search"] == ["web_search_perplexity"]
    assert seeded_tool_names(state) == ["web_search_perplexity"]


def test_wizard_pilot_fetch_url_multiselect_defaults_nymeria_on():
    """The web fetch step defaults the keyless built-in fetcher (fetch_url_nymeria)
    checked, so accepting the default records it with no toggling. It needs no key
    (it distills pages with the configured LLM), making it a safe default.
    """
    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.placeholders import make_fetch_url_step, seeded_tool_names

    async def drive() -> WizardState:
        state = WizardState()
        app = SetupWizardApp(state, steps=[make_fetch_url_step()])
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")  # accept the default (nymeria fetch pre-checked)
            await pilot.pause()
        return state

    state = asyncio.run(drive())
    assert state.extras["fetch_url"] == ["fetch_url_nymeria"]
    assert seeded_tool_names(state) == ["fetch_url_nymeria"]


def test_wizard_pilot_image_gen_multiselect_seeds_real_backend():
    """The image generation step is a real multi-select over the built
    image_gen_* provider tool names (parallel to web search / web fetch), and a
    pick is recorded as a concrete tool name the init flow would seed.
    """
    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.placeholders import make_image_gen_step, seeded_tool_names

    async def drive() -> WizardState:
        state = WizardState()
        app = SetupWizardApp(state, steps=[make_image_gen_step()])
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("space")  # toggle the first (highlighted) provider
            await pilot.pause()
            await pilot.press("enter")  # advance, storing the selection
            await pilot.pause()
        return state

    state = asyncio.run(drive())
    assert state.extras["image_gen"] == ["image_gen_openai"]
    assert seeded_tool_names(state) == ["image_gen_openai"]


def test_wizard_pilot_backend_keys_step_collects_key():
    """The backend-keys step renders one input per selected backend and writes
    the value into optional_env under the canonical env var.
    """
    from textual.widgets import Input

    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.backend_keys import make_backend_keys_step

    async def drive() -> WizardState:
        state = WizardState(extras={"web_search": ["web_search_tavily"]})
        app = SetupWizardApp(state, steps=[make_backend_keys_step()])
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one("#backend-key-tavily_api_key", Input).value = "tav-secret"
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
        return state

    state = asyncio.run(drive())
    assert state.optional_env["TAVILY_API_KEY"] == "tav-secret"


def test_wizard_pilot_skill_kits_multiselect_defaults_all_on_and_records_list():
    """The skill-kits step is a real multi-select: all kits are checked by default
    and the chosen kit names are recorded as a list in extras.
    """
    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.placeholders import make_skill_kits_step, seeded_global_skills

    async def drive() -> WizardState:
        state = WizardState()
        app = SetupWizardApp(state, steps=[make_skill_kits_step()])
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")  # accept the all-checked default
            await pilot.pause()
        return state

    state = asyncio.run(drive())
    assert state.extras["skill_kits"] == [
        "tool-management",
        "skill-management",
        "mcp-management",
        "credential-management",
    ]
    assert seeded_global_skills(state) == state.extras["skill_kits"]


def test_wizard_pilot_embedder_enter_jumps_to_empty_key_then_advances():
    """Enter on a cloud model with no key does not advance: it locks the model in,
    focuses the empty key field, and shows an error. Filling it and pressing Enter
    advances (retrieval keeps its Hybrid default, which Enter skips).
    """
    from textual.widgets import Input, Static

    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.rag import make_embedder_step

    async def drive():
        state = WizardState()
        app = SetupWizardApp(state, steps=[make_embedder_step()])
        async with app.run_test() as pilot:
            await pilot.pause()
            scr = app.screen
            await pilot.press("enter")  # default premium-cohere needs a key
            await pilot.pause()
            key_input = scr.query_one("#rag-key", Input)
            assert key_input.has_focus  # jumped to the missing field
            assert not app.completed  # did not advance
            assert str(scr.query_one("#wizard-error", Static).render())
            key_input.value = "test-key"
            await pilot.press("enter")  # key filled -> nothing else required -> advance
            await pilot.pause()
        return state, app

    state, app = asyncio.run(drive())
    assert app.completed
    assert state.embedder == "premium-cohere"
    assert state.optional_env["EMBEDDING_API_KEY"] == "test-key"
    assert state.rag_retrieval_mode == "hybrid"  # default kept (Enter skipped it)


def test_wizard_pilot_embedder_local_hides_key_and_enter_advances():
    """Selecting the local (keyless) embedder hides the key field; Enter then needs
    no key and advances.
    """
    from textual.widgets import Input, RadioButton

    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.rag import make_embedder_step

    async def drive():
        from nymeria.setup.rag_catalog import EMBEDDERS

        granite_idx = next(
            i for i, o in enumerate(EMBEDDERS) if o.id == "local-granite"
        )
        state = WizardState()
        app = SetupWizardApp(state, steps=[make_embedder_step()])
        async with app.run_test() as pilot:
            await pilot.pause()
            scr = app.screen
            models = list(scr.query_one("#model-group").query(RadioButton))
            for _ in range(granite_idx):  # arrow down to the local (keyless) option
                await pilot.press("down")
            await pilot.pause()
            assert scr.focused is models[granite_idx]  # local-granite
            await pilot.press("space")  # select local
            await pilot.pause()
            assert not scr.query_one("#rag-key", Input).display  # key hidden
            await pilot.press("enter")  # local needs no key -> advance
            await pilot.pause()
        return state, app

    state, app = asyncio.run(drive())
    assert app.completed
    assert state.embedder == "local-granite"
    assert "EMBEDDING_API_KEY" not in state.optional_env
    assert state.rag_retrieval_mode == "hybrid"


def test_wizard_pilot_embedder_arrows_cross_fields_both_ways():
    """The core 'change your mind' fix: arrows move from the model list into the
    key field and the retrieval group, and back UP from the key field to the model
    list (focus is never trapped in one control).
    """
    from textual.widgets import Input, RadioButton

    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.rag import make_embedder_step

    async def drive() -> None:
        app = SetupWizardApp(WizardState(), steps=[make_embedder_step()])
        async with app.run_test() as pilot:
            await pilot.pause()
            scr = app.screen
            models = list(scr.query_one("#model-group").query(RadioButton))
            retrieval = list(scr.query_one("#retrieval-group").query(RadioButton))
            key_input = scr.query_one("#rag-key", Input)
            assert scr.focused is models[0]
            # Down through every model option lands on the key field.
            for _ in range(len(models)):
                await pilot.press("down")
                await pilot.pause()
            assert scr.focused is key_input
            await pilot.press("down")  # into the retrieval group
            await pilot.pause()
            assert scr.focused is retrieval[0]
            # Back up: retrieval -> key -> the last model option.
            await pilot.press("up")
            await pilot.pause()
            assert scr.focused is key_input
            await pilot.press("up")
            await pilot.pause()
            assert scr.focused is models[-1]

    asyncio.run(drive())


def test_wizard_pilot_reranker_none_advances_without_key():
    """The default 'No reranker' option needs no key, so Enter advances directly."""
    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.rag import make_reranker_step

    async def drive() -> WizardState:
        state = WizardState(embedder="local-granite")
        app = SetupWizardApp(state, steps=[make_reranker_step()])
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")  # "No reranker" -> advance (no key prompt)
            await pilot.pause()
        return state

    state = asyncio.run(drive())
    assert state.reranker == "none"


def test_wizard_pilot_auth_skip_pins_api_key_even_on_oauth_row():
    """Ctrl+S on the auth step must not leave a deferred OAuth value in state
    (which would silently disable the provider/connection/model steps). It coerces
    to the only wired method.
    """
    from nymeria.onboarding import ProviderAuthMethod
    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.auth import make_auth_method_step

    async def drive() -> WizardState:
        state = WizardState()
        app = SetupWizardApp(state, steps=[make_auth_method_step()])
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("down")  # highlight a deferred CLIProxy OAuth row
            await pilot.pause()
            await pilot.press("ctrl+s")  # skip must pin API key, not keep OAuth
            await pilot.pause()
        return state

    state = asyncio.run(drive())
    assert state.auth_method is ProviderAuthMethod.API_KEY


def test_wizard_pilot_provider_switch_clears_stale_connection_state():
    """Switching providers clears api_mode/base_url, so a back-nav that skips the
    now-inapplicable connection step cannot leak the old provider's connection
    details into the written config.
    """
    from textual.widgets import Input

    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.provider import make_provider_step

    async def drive() -> WizardState:
        # Stand in for a prior OpenRouter connection-step result, then switch.
        state = WizardState(
            provider="openrouter", api_mode="responses", base_url="http://x/v1"
        )
        app = SetupWizardApp(state, steps=[make_provider_step()])
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press(*"anthropic")  # filter to a different provider
            await pilot.pause()
            await pilot.press("enter")  # picker enter -> focus key field
            await pilot.pause()
            app.screen.query_one("#api-key", Input).value = "sk-ant-x"
            await pilot.press("enter")  # advance, committing the provider switch
            await pilot.pause()
        return state

    state = asyncio.run(drive())
    assert state.provider == "anthropic"
    assert state.api_mode == ""  # cleared on the switch
    assert state.base_url == ""  # cleared on the switch


def test_wizard_pilot_auth_step_blocks_deferred_oauth():
    """The auth step shows subscription OAuth for orientation but refuses to
    advance on it (the branch is deferred). Direct API key is accepted.
    """
    from nymeria.onboarding import ProviderAuthMethod
    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState

    async def drive() -> SetupWizardApp:
        app = SetupWizardApp(WizardState())
        async with app.run_test() as pilot:
            await pilot.press("enter")  # welcome -> hosting
            await pilot.pause()
            await pilot.press("enter")  # accept hosting -> security profile
            await pilot.pause()
            await pilot.press("enter")  # accept security profile -> auth method
            await pilot.pause()
            assert app.nav.current() == _AUTH_STEP
            await pilot.press("down")  # focus a deferred CLIProxy OAuth method
            await pilot.pause()
            await pilot.press("enter")  # gated: error, focus returns to API key, stay
            await pilot.pause()
            assert app.nav.current() == _AUTH_STEP
            await pilot.press("enter")  # focus is now Direct API key -> accepted
            await pilot.pause()
        return app

    app = asyncio.run(drive())
    assert app.state.auth_method is ProviderAuthMethod.API_KEY
    assert app.nav.current() == _PROVIDER_STEP


def test_wizard_pilot_provider_picker_up_arrow_focus_flow():
    """In the provider picker, down enters the list and up at the first row hands
    focus back to the search box (instead of wrapping the highlight to the
    bottom); up from a lower row stays in the list and moves up by one.
    """
    from textual.widgets import Input

    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState
    from nymeria.setup.widgets import PickerOptionList

    async def drive() -> None:
        app = SetupWizardApp(WizardState())
        async with app.run_test() as pilot:
            await _advance_to_provider(pilot)  # focus the provider picker search
            search = app.screen.query_one("#provider-search", Input)
            option_list = app.screen.query_one(PickerOptionList)
            first = option_list._first_selectable_index()
            assert app.focused is search

            await pilot.press("down")  # into the list, highlight on the first row
            await pilot.pause()
            assert app.focused is option_list
            assert option_list.highlighted == first

            await pilot.press("down")  # move down one selectable row
            await pilot.pause()
            assert option_list.highlighted == first + 1

            await pilot.press("up")  # back to the first row, still in the list
            await pilot.pause()
            assert app.focused is option_list
            assert option_list.highlighted == first

            await pilot.press("up")  # at the first row -> focus returns to search
            await pilot.pause()
            assert app.focused is search

    asyncio.run(drive())


def test_wizard_pilot_provider_picker_first_row_reveals_top_header():
    """Returning to the first selectable row scrolls the list fully to the top so
    the leading group header is revealed, rather than staying clipped above the
    viewport (a disabled header is never highlighted, so the stock scroll never
    returns to it on its own).
    """
    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState
    from nymeria.setup.widgets import PickerOptionList

    async def drive() -> None:
        app = SetupWizardApp(WizardState())
        async with app.run_test() as pilot:
            await _advance_to_provider(pilot)  # land on the provider picker
            option_list = app.screen.query_one(PickerOptionList)
            # The registry yields more providers than fit, so the list scrolls.
            assert option_list.option_count > 14
            option_list.focus()

            await pilot.press("end")  # jump to the last row: list scrolls down
            await pilot.pause()
            assert option_list.scroll_offset.y > 0

            await pilot.press("home")  # back to the first row: header revealed
            await pilot.pause()
            assert option_list.highlighted == option_list._first_selectable_index()
            assert option_list.scroll_offset.y == 0

    asyncio.run(drive())


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


# --- opt-in start-now -------------------------------------------------------


def test_run_init_parser_and_state_accept_start_flag():
    from nymeria.onboarding import NextAction
    from nymeria.setup.runner import _build_state, build_parser

    args = build_parser().parse_args(
        ["--hosting", "docker", "--provider", "anthropic",
         "--model", "m", "--api-key", "k", "--start"]
    )
    assert args.start is True
    state = _build_state(args)
    assert state.next_action is NextAction.START_API_OPEN_FRONTEND
    # Pre-seeded so the interactive start-now step reflects the flag.
    assert state.extras.get("start_now") is NextAction.START_API_OPEN_FRONTEND

    # --start wins over --next-action.
    args2 = build_parser().parse_args(
        ["--provider", "anthropic", "--model", "m", "--api-key", "k",
         "--next-action", "print_commands", "--start"]
    )
    assert _build_state(args2).next_action is NextAction.START_API_OPEN_FRONTEND

    # Without an explicit choice, nothing is pre-seeded (shape default applies).
    args3 = build_parser().parse_args(
        ["--hosting", "docker", "--provider", "anthropic", "--model", "m",
         "--api-key", "k"]
    )
    assert _build_state(args3).next_action is NextAction.PRINT_COMMANDS
    assert "start_now" not in _build_state(args3).extras


def test_start_now_step_applies_and_choices_are_shape_aware():
    from nymeria.onboarding import NextAction
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.start_now import start_now_applies, start_now_choices

    assert start_now_applies(WizardState(hosting=HostingOption.LOCAL)) is True
    assert start_now_applies(WizardState(hosting=HostingOption.DOCKER)) is True
    # Background-service start is not wired, so the step is skipped there.
    assert start_now_applies(WizardState(hosting=HostingOption.SERVICE)) is False
    assert start_now_applies(WizardState()) is False  # no hosting chosen yet
    # A CLI handoff chosen by flag suppresses the start-now question.
    assert (
        start_now_applies(
            WizardState(hosting=HostingOption.LOCAL, next_action=NextAction.CLI)
        )
        is False
    )

    docker = start_now_choices(HostingOption.DOCKER)
    assert docker[0].value is NextAction.START_API_OPEN_FRONTEND  # docker -> start
    local = start_now_choices(HostingOption.LOCAL)
    assert local[0].value is NextAction.PRINT_COMMANDS  # local -> print
    for choices in (docker, local):
        assert {c.value for c in choices} == {
            NextAction.START_API_OPEN_FRONTEND,
            NextAction.PRINT_COMMANDS,
        }


def test_start_now_is_in_default_flow_before_review():
    from nymeria.setup.nav import Navigator
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps import build_default_steps

    steps = build_default_steps()
    ids = [step.id for step in steps]
    assert "start_now" in ids
    assert ids.index("start_now") < ids.index("review")

    def applicable(state: "WizardState") -> list[str]:
        nav = Navigator(steps, state)
        nav.start()
        return [steps[i].id for i in nav.applicable_indices()]

    assert "start_now" in applicable(WizardState(hosting=HostingOption.LOCAL))
    assert "start_now" in applicable(WizardState(hosting=HostingOption.DOCKER))
    assert "start_now" not in applicable(WizardState(hosting=HostingOption.SERVICE))


def test_finalize_starts_docker_when_opted_in(monkeypatch, tmp_path, capsys):
    _stub_llm(monkeypatch)
    root = tmp_path / "runtime"
    calls: list[tuple[list[str], object]] = []
    fake_token = "nym_dockertoken_abc123"

    class _Result:
        def __init__(self, stdout=""):
            self.returncode = 0
            self.stdout = stdout

    def fake_run(cmd, *args, **kwargs):
        calls.append((cmd, kwargs.get("cwd")))
        # After a clean start the wizard execs into the container to read the
        # real bootstrap token from its /data volume.
        if "exec" in cmd:
            return _Result(stdout=f"Token: {fake_token}\n")
        return _Result()

    health: list[dict] = []
    monkeypatch.setattr(finalize_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(
        finalize_mod, "wait_for_health", lambda **kw: bool(health.append(kw)) or True
    )

    rc = setup_main(
        ["--provider", "anthropic", "--model", "claude-test-model",
         "--api-key", "sk-ant-test-key", "--hosting", "docker",
         "--root", str(root), "--start", "--non-interactive"]
    )
    out = capsys.readouterr().out

    assert rc == 0
    # The compose `up -d` runs first, in the runtime root...
    assert calls[0][0] == ["docker", "compose", "-f", "docker-compose.single.yml", "up", "-d"]
    assert calls[0][1] == str(root)
    assert health  # polled for health after a clean start
    assert "Nymeria is up" in out
    # ...then the wizard execs in to surface the container-minted token (there is
    # no host token for the Docker shape).
    assert any("exec" in cmd and "cat" in cmd for cmd, _cwd in calls)
    assert fake_token in out


def test_finalize_starts_local_foreground_when_opted_in(monkeypatch, tmp_path):
    _stub_llm(monkeypatch)
    root = tmp_path / "runtime"
    calls: list[tuple[list[str], object, dict]] = []

    class _Result:
        returncode = 0

    def fake_run(cmd, *args, **kwargs):
        calls.append((cmd, kwargs.get("cwd"), kwargs.get("env") or {}))
        return _Result()

    monkeypatch.setattr(finalize_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(
        finalize_mod,
        "wait_for_health",
        lambda **kw: pytest.fail("local foreground start must not health-poll"),
    )

    rc = setup_main(
        ["--provider", "anthropic", "--model", "claude-test-model",
         "--api-key", "sk-ant-test-key", "--hosting", "local",
         "--root", str(root), "--start", "--non-interactive"]
    )

    assert rc == 0
    assert len(calls) == 1
    cmd, cwd, env = calls[0]
    assert cmd[0] == finalize_mod.sys.executable
    assert cmd[-1] == "slim"
    assert cwd == str(root)
    assert env.get("NYMERIA_PROJECT_ROOT") == str(root)


def test_finalize_default_does_not_start_a_process(monkeypatch, tmp_path, capsys):
    _stub_llm(monkeypatch)
    root = tmp_path / "runtime"

    def boom(*_a, **_k):
        pytest.fail("the default handoff must not launch a process")

    monkeypatch.setattr(finalize_mod.subprocess, "run", boom)

    rc = setup_main(
        ["--provider", "anthropic", "--model", "claude-test-model",
         "--api-key", "sk-ant-test-key", "--hosting", "docker",
         "--root", str(root), "--non-interactive"]
    )
    out = capsys.readouterr().out

    assert rc == 0
    assert "docker compose -f docker-compose.single.yml up -d" in out


def test_finalize_docker_start_failure_falls_back_to_printing(monkeypatch, tmp_path, capsys):
    _stub_llm(monkeypatch)
    root = tmp_path / "runtime"

    class _Result:
        returncode = 1  # compose failed

    monkeypatch.setattr(finalize_mod.subprocess, "run", lambda *a, **k: _Result())
    monkeypatch.setattr(
        finalize_mod,
        "wait_for_health",
        lambda **kw: pytest.fail("must not health-poll after a failed start"),
    )

    rc = setup_main(
        ["--provider", "anthropic", "--model", "claude-test-model",
         "--api-key", "sk-ant-test-key", "--hosting", "docker",
         "--root", str(root), "--start", "--non-interactive"]
    )
    out = capsys.readouterr().out

    assert rc == 0  # config was written; a failed start is non-fatal
    assert "did not start cleanly" in out
    assert "docker compose -f docker-compose.single.yml up -d" in out


def test_wizard_pilot_start_now_docker_defaults_to_start_and_can_switch():
    from nymeria.onboarding import NextAction
    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.start_now import make_start_now_step

    async def drive(switch: bool) -> WizardState:
        state = WizardState(hosting=HostingOption.DOCKER)
        app = SetupWizardApp(state, steps=[make_start_now_step()])
        async with app.run_test() as pilot:
            await pilot.pause()
            if switch:
                await pilot.press("down")   # to "Just print the command"
                await pilot.press("space")  # select it
                await pilot.pause()
            await pilot.press("enter")      # lock the highlighted option + finish
            await pilot.pause()
        return state

    # Docker default: Enter accepts the highlighted first option (start now).
    assert asyncio.run(drive(False)).next_action is NextAction.START_API_OPEN_FRONTEND
    # Arrow + space switches to print before Enter records it.
    assert asyncio.run(drive(True)).next_action is NextAction.PRINT_COMMANDS


# --- expanded RAG catalog ---------------------------------------------------

# Providers the production backend can actually drive (memory_index.py embedders /
# rag_quality.py rerankers). An option whose provider is not here would write
# config the backend cannot honor, so the catalog must never offer one.
_BACKEND_EMBED_PROVIDERS = {"openai", "cohere", "gemini", "local"}
_BACKEND_RERANK_PROVIDERS = {"voyage", "cohere", "zeroentropy", "local", "none"}


def test_rag_catalog_expanded_options_are_backend_supported_and_metricked():
    from nymeria.setup.rag_catalog import (
        EMBEDDERS,
        RECOMMENDED_COMBOS,
        RERANKERS,
        get_embedder,
        get_reranker,
        recommended_reranker_for,
    )

    emb_ids = {o.id for o in EMBEDDERS}
    rer_ids = {o.id for o in RERANKERS}
    # The new same-provider options are present.
    assert {"premium-cohere-1536", "premium-voyage-large", "value-openai-small"} <= emb_ids
    assert {"premium-cohere-pro", "local-ettin-32m"} <= rer_ids
    # First options are stable: pilots and the shape-aware defaults rely on them.
    assert EMBEDDERS[0].id == "premium-cohere"
    assert RERANKERS[0].id == "none"
    # Every provider is one the backend can drive (guards against e.g. a Jina
    # reranker, which the eval tested but production has no endpoint for).
    assert all(o.provider in _BACKEND_EMBED_PROVIDERS for o in EMBEDDERS)
    assert all(o.provider in _BACKEND_RERANK_PROVIDERS for o in RERANKERS)
    assert all(o.provider != "jina" for o in RERANKERS)
    # Every option carries a description; every non-baseline one carries a metric.
    for o in EMBEDDERS:
        assert o.description and o.metrics
    for o in RERANKERS:
        assert o.description
        if o.provider != "none":
            assert o.metrics
    # Recommended combos reference real ids.
    for _tier, emb_id, rer_id, _blurb in RECOMMENDED_COMBOS:
        assert get_embedder(emb_id) is not None
        assert get_reranker(rer_id) is not None
    # Every embedder maps to a recommended reranker that exists.
    for o in EMBEDDERS:
        rec = recommended_reranker_for(o.id)
        assert rec is not None, o.id
        assert get_reranker(rec[0]) is not None


def test_rag_env_for_state_handles_new_embedder_and_reranker():
    from nymeria.setup.rag_catalog import rag_env_for_state
    from nymeria.setup.state import WizardState

    # Voyage 4 large embedder + Cohere rerank-v4.0-pro.
    env = rag_env_for_state(
        WizardState(embedder="premium-voyage-large", reranker="premium-cohere-pro")
    )
    assert env["EMBEDDING_PROVIDER"] == "openai"
    assert env["EMBEDDING_MODEL"] == "voyage-4-large"
    assert env["EMBEDDING_DIMENSIONS"] == "1024"
    assert env["EMBEDDING_BASE_URL"] == "https://api.voyageai.com/v1"
    assert env["EMBEDDING_INPUT_TYPE"] == "voyage"
    assert env["RAG_RERANK_ENABLED"] == "true"
    assert env["RAG_RERANK_PROVIDER"] == "cohere"
    assert env["RAG_RERANK_MODEL"] == "rerank-v4.0-pro"

    # OpenAI text-embedding-3-small uses the default OpenAI endpoint (no base URL).
    env2 = rag_env_for_state(WizardState(embedder="value-openai-small"))
    assert env2["EMBEDDING_PROVIDER"] == "openai"
    assert env2["EMBEDDING_MODEL"] == "text-embedding-3-small"
    assert env2["EMBEDDING_DIMENSIONS"] == "1536"
    assert "EMBEDDING_BASE_URL" not in env2

    # Cohere 1536-d variant emits the full width.
    env3 = rag_env_for_state(WizardState(embedder="premium-cohere-1536"))
    assert env3["EMBEDDING_PROVIDER"] == "cohere"
    assert env3["EMBEDDING_DIMENSIONS"] == "1536"

    # Local Ettin 32m reranker.
    env4 = rag_env_for_state(WizardState(embedder="local-granite", reranker="local-ettin-32m"))
    assert env4["RAG_RERANK_PROVIDER"] == "local"
    assert env4["RAG_RERANK_MODEL"] == "cross-encoder/ettin-reranker-32m-v1"


def test_wizard_pilot_reranker_reuses_cohere_key_for_cohere_pro():
    """Cohere embedder + Cohere rerank-v4.0-pro share one key: the reranker step
    reuses the embedding key (no second prompt) and stores it for the reranker."""
    from textual.widgets import Input

    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.rag_catalog import RERANKERS
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.rag import make_reranker_step

    cohere_pro_idx = next(
        i for i, o in enumerate(RERANKERS) if o.id == "premium-cohere-pro"
    )

    async def drive() -> WizardState:
        state = WizardState(
            embedder="premium-cohere",
            optional_env={"EMBEDDING_API_KEY": "co-secret"},
        )
        app = SetupWizardApp(state, steps=[make_reranker_step()])
        async with app.run_test() as pilot:
            await pilot.pause()
            scr = app.screen
            for _ in range(cohere_pro_idx):
                await pilot.press("down")
            await pilot.press("space")  # select Cohere rerank-v4.0-pro
            await pilot.pause()
            assert not scr.query_one("#rag-key", Input).display  # key reused, hidden
            assert scr.query_one("#combo-rec")  # the pairing hint rendered
            await pilot.press("enter")
            await pilot.pause()
        return state

    state = asyncio.run(drive())
    assert state.reranker == "premium-cohere-pro"
    # The Cohere embedding key was reused for the reranker, not re-prompted.
    assert state.optional_env["RAG_RERANK_API_KEY"] == "co-secret"
