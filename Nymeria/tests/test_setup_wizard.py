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


def test_new_deployment_enums_have_ordered_choices_and_recommended_markers():
    from nymeria.onboarding import (
        DOCKER_STACK_CHOICES,
        DOCKER_STACK_ORDER,
        EXTERNAL_ACCESS_CHOICES,
        EXTERNAL_ACCESS_ORDER,
        SECURITY_PROFILE_CHOICES,
        SECURITY_PROFILE_ORDER,
        DockerStack,
        ExternalAccess,
        SecurityProfile,
    )

    # SecurityProfile carries no recommended marker while Unleashed is the
    # only selectable profile ("(recommended)" implies alternatives; Secure
    # and Standard are greyed out until the approval gate ships).
    for order, table, enum_type, recommended in (
        (DOCKER_STACK_ORDER, DOCKER_STACK_CHOICES, DockerStack, DockerStack.SLIM),
        (SECURITY_PROFILE_ORDER, SECURITY_PROFILE_CHOICES, SecurityProfile, None),
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
        expected = [recommended] if recommended is not None else []
        assert flagged == expected  # at most one, the expected default

    coming_soon = {opt for opt in SecurityProfile if SECURITY_PROFILE_CHOICES[opt].coming_soon}
    assert coming_soon == {SecurityProfile.SECURE, SecurityProfile.STANDARD}
    for table in (DOCKER_STACK_CHOICES, EXTERNAL_ACCESS_CHOICES):
        assert not any(meta.coming_soon for meta in table.values())


def test_security_profile_choices_disable_coming_soon_rows():
    """Secure and Standard render greyed out with a "(to come)" suffix until
    the approval gate ships (docs/private/security-profiles.md); Unleashed is
    the only selectable row and carries no "(recommended)" marker.
    """
    from nymeria.onboarding import SECURITY_PROFILE_CHOICES, SECURITY_PROFILE_ORDER
    from nymeria.setup.steps.deployment import _choices

    by_label = {c.label: c for c in _choices(SECURITY_PROFILE_ORDER, SECURITY_PROFILE_CHOICES)}
    assert set(by_label) == {"Secure (to come)", "Standard (to come)", "Unleashed"}
    assert by_label["Secure (to come)"].disabled
    assert by_label["Standard (to come)"].disabled
    assert not by_label["Unleashed"].disabled
    assert not any("(recommended)" in label for label in by_label)


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


# --- declarative family catalog (drift guards) ------------------------------


def test_family_catalog_web_search_matches_runtime_tools():
    from nymeria.setup import family_catalog
    from nymeria.tools import WEB_SEARCH_SERVICE_TOOLS, WEB_SEARCH_INTEGRATION_TOOLS

    expected = [t.name for t in (*WEB_SEARCH_SERVICE_TOOLS, *WEB_SEARCH_INTEGRATION_TOOLS)]
    assert [c.value for c in family_catalog.web_search_choices()] == expected
    # Perplexity (the self-sufficient backend) leads, matching the prior order.
    assert family_catalog.web_search_choices()[0].value == "web_search_perplexity"


def test_family_catalog_fetch_url_includes_nymeria_and_jina():
    from nymeria.setup import family_catalog

    assert [c.value for c in family_catalog.fetch_url_choices()] == [
        "fetch_url_nymeria",
        "jina_reader_fetch_url",
    ]
    assert family_catalog.default_checked_fetch_url() == ["fetch_url_nymeria"]


def test_family_catalog_image_gen_matches_runtime_tools():
    from nymeria.setup import family_catalog
    from nymeria.tools import IMAGE_GEN_INTEGRATION_TOOLS

    assert [c.value for c in family_catalog.image_gen_choices()] == [
        t.name for t in IMAGE_GEN_INTEGRATION_TOOLS
    ]


def test_family_catalog_skill_kits_discovered_live_and_default_checked():
    from nymeria.setup import family_catalog
    from nymeria.core.user_profile import DEFAULT_GLOBAL_SKILLS

    offered = {c.value for c in family_catalog.skill_kit_choices()}
    default_checked = family_catalog.default_checked_skill_kits()
    # The curated default-on set is DEFAULT_GLOBAL_SKILLS minus the guidance skill.
    assert default_checked == [s for s in DEFAULT_GLOBAL_SKILLS if s != "self-improve"]
    # Every default-checked kit is actually offered (discovered live from bundled).
    assert set(default_checked) <= offered
    # self-improve is a guidance skill, not a selectable kit.
    assert "self-improve" not in offered
    # Discovery is drift-proof: it surfaces bundled kits beyond the legacy four.
    assert offered  # non-empty even if the scan path changes


def test_every_keyed_catalog_backend_has_a_key_spec():
    """Drift guard: a new web_search/image_gen backend needs a BACKEND_KEY_SPECS entry."""
    from nymeria.setup import family_catalog
    from nymeria.setup.tool_keys import BACKEND_KEY_SPECS

    # fetch_url_nymeria is intentionally keyless (uses the configured primary LLM).
    keyless = {"fetch_url_nymeria"}
    keyed_backends = [
        c.value
        for c in (
            *family_catalog.web_search_choices(),
            *family_catalog.image_gen_choices(),
            *family_catalog.fetch_url_choices(),
        )
        if c.value not in keyless
    ]
    missing = [name for name in keyed_backends if name not in BACKEND_KEY_SPECS]
    assert not missing, f"backends missing a BACKEND_KEY_SPECS entry: {missing}"


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


def test_default_flow_order_and_conditional_docker_stack():
    from nymeria.setup.nav import Navigator
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps import build_default_steps

    steps = build_default_steps()
    ids = [step.id for step in steps]
    assert ids[0] == "welcome"  # the plan's "detect environment" step comes first
    assert ids[-1] == "review"
    for required in (
        "docker_stack",
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

    # docker_stack (slim vs full) is a property of a Docker deployment, so it only
    # applies to a Docker host.
    assert "docker_stack" not in applicable_ids(WizardState(hosting=HostingOption.LOCAL))
    assert "docker_stack" in applicable_ids(WizardState(hosting=HostingOption.DOCKER))

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
        "docker_stack", "security_profile", "auth_method", "core_tools",
        "web_search", "fetch_url", "embedder", "reranker", "image_gen",
        "backend_keys", "skill_kits", "tts", "stt", "llm_tuning",
        "context", "agent_limits", "external_access",
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


def test_apply_quick_defaults_seeds_slim_docker_stack():
    from nymeria.onboarding import DockerStack
    from nymeria.setup.quick import apply_quick_defaults
    from nymeria.setup.state import WizardState

    state = WizardState(quick=True)
    apply_quick_defaults(state)
    # The quick path defaults the Docker shape to slim (the docker_stack step is
    # gated off in quick mode), so review and finalize see a concrete value.
    assert state.docker_stack is DockerStack.SLIM

    # An explicit pick is left untouched.
    full = WizardState(quick=True, docker_stack=DockerStack.FULL)
    apply_quick_defaults(full)
    assert full.docker_stack is DockerStack.FULL


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
    """No optional picks -> the carrier vars are absent, so the container's own
    core-seed and default-skill migrations run unchanged (no new noise)."""
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


# --- reconfigure mode: RAG inverse-mappers ----------------------------------


def test_embedder_inverse_roundtrips_every_option_and_has_no_collisions():
    from nymeria.setup.rag_catalog import EMBEDDERS, embedder_id_for_env

    keys = {}
    for opt in EMBEDDERS:
        key = (opt.provider, opt.model, opt.dimensions)
        assert key not in keys, f"colliding embedder key {key}: {keys.get(key)} vs {opt.id}"
        keys[key] = opt.id
        assert embedder_id_for_env(opt.provider, opt.model, opt.dimensions) == opt.id


def test_reranker_inverse_roundtrips_and_disabled_maps_to_none():
    from nymeria.setup.rag_catalog import RERANKERS, reranker_id_for_env

    for opt in RERANKERS:
        enabled = "false" if opt.provider == "none" else "true"
        assert reranker_id_for_env(opt.provider, opt.model, enabled) == opt.id
    # A disabled reranker resolves to the explicit "none" option regardless of provider.
    assert reranker_id_for_env("voyage", "rerank-2.5", "false") is not None
    assert reranker_id_for_env("voyage", "rerank-2.5", "false") != "voyage"


def test_rag_env_forward_then_inverse_recovers_ids():
    from nymeria.setup.rag_catalog import (
        EMBEDDERS,
        RERANKERS,
        embedder_id_for_env,
        rag_env_for_state,
        reranker_id_for_env,
    )
    from nymeria.setup.state import WizardState

    emb_id = EMBEDDERS[0].id
    rer_id = next(o.id for o in RERANKERS if o.provider != "none")
    state = WizardState(embedder=emb_id, reranker=rer_id)
    env = rag_env_for_state(state)
    assert (
        embedder_id_for_env(
            env["EMBEDDING_PROVIDER"], env["EMBEDDING_MODEL"], env.get("EMBEDDING_DIMENSIONS")
        )
        == emb_id
    )
    assert (
        reranker_id_for_env(
            env.get("RAG_RERANK_PROVIDER"),
            env.get("RAG_RERANK_MODEL"),
            env.get("RAG_RERANK_ENABLED"),
        )
        == rer_id
    )


# --- reconfigure mode: hydration --------------------------------------------


def _first_run(monkeypatch, root, *extra):
    """Write a first-run config + bootstrap profile under ``root`` via the flag path."""
    _stub_llm(monkeypatch)
    monkeypatch.delenv("NYMERIA_SECRETS_KEY", raising=False)
    args = [
        "--provider", "anthropic", "--model", "claude-test-model",
        "--api-key", "sk-ant-x", "--root", str(root),
        "--non-interactive", "--skip-llm-test", *extra,
    ]
    assert setup_main(args) == 0


def test_hydrate_returns_false_on_fresh_install(tmp_path):
    from nymeria.setup.hydrate import hydrate_state_from_disk
    from nymeria.setup.state import WizardState

    state = WizardState(root=tmp_path / "empty")
    assert hydrate_state_from_disk(state) is False
    assert state.provider is None and state.reconfigure is False


def test_hydrate_reads_provider_model_and_marks_key_present(monkeypatch, tmp_path):
    from nymeria.setup.hydrate import hydrate_state_from_disk
    from nymeria.setup.state import WizardState

    root = tmp_path / "runtime"
    _first_run(monkeypatch, root)

    state = WizardState(root=root)
    assert hydrate_state_from_disk(state) is True
    assert state.reconfigure is True
    assert state.provider == "anthropic"
    assert state.model == "claude-test-model"
    # The key value is never read into state; only its presence is recorded.
    assert state.api_key == ""
    assert "ANTHROPIC_DIRECT_API_KEY" in state.present_env_keys
    # A keyless first run still equips local RAG, so the embedder round-trips.
    assert state.embedder == "local-granite"


def test_hydrate_is_fill_only_if_unset(monkeypatch, tmp_path):
    from nymeria.setup.hydrate import hydrate_state_from_disk
    from nymeria.setup.state import WizardState

    root = tmp_path / "runtime"
    _first_run(monkeypatch, root)

    # An explicit flag value (provider already set) must survive hydration.
    state = WizardState(root=root, provider="openai", model="gpt-x")
    assert hydrate_state_from_disk(state) is True
    assert state.provider == "openai"
    assert state.model == "gpt-x"


def test_hydrate_recovers_picks_and_captures_unmanaged(monkeypatch, tmp_path):
    from nymeria.core.user_profile import UserProfileManager
    from nymeria.setup.hydrate import hydrate_state_from_disk
    from nymeria.setup.state import WizardState

    root = tmp_path / "runtime"
    data_dir = root / "data"
    _first_run(
        monkeypatch, root,
        "--web-search", "web_search_tavily", "--tavily-api-key", "k",
        "--image-gen", "image_gen_gemini",
    )
    # Simulate a user-added (non-core, non-family) tool in the profile.
    manager = UserProfileManager(data_dir)
    profile = manager.get_profile("default")
    tools = list(profile.tool_preferences.default_thread_tools or [])
    tools.append("my_custom_tool")
    profile.tool_preferences.default_thread_tools = tools
    manager.save_profile(profile)

    state = WizardState(root=root)
    assert hydrate_state_from_disk(state) is True
    assert state.extras.get("web_search") == ["web_search_tavily"]
    assert state.extras.get("image_gen") == ["image_gen_gemini"]
    assert state.unmanaged_tools == ["my_custom_tool"]


def test_hydrate_docker_recovers_picks_from_env_carriers(monkeypatch, tmp_path):
    from nymeria.setup.hydrate import hydrate_state_from_disk
    from nymeria.setup.state import WizardState

    root = tmp_path / "checkout"
    root.mkdir()
    _first_run(
        monkeypatch, root, "--hosting", "docker",
        "--web-search", "web_search_tavily", "--tavily-api-key", "k",
    )
    assert "NYMERIA_INIT_DEFAULT_THREAD_TOOLS" in (
        (root / ".env.docker").read_text(encoding="utf-8")
    )

    # The container profile is unreadable from the host; the carriers in
    # .env.docker are the env-file layer's own record of the picks, so a
    # reconfigure round-trips them instead of starting from defaults.
    state = WizardState(root=root)
    assert hydrate_state_from_disk(state) is True
    assert state.extras.get("web_search") == ["web_search_tavily"]

    # An explicit flag still wins over the hydrated carrier.
    flagged = WizardState(root=root)
    flagged.extras["web_search"] = ["web_search_exa"]
    assert hydrate_state_from_disk(flagged) is True
    assert flagged.extras["web_search"] == ["web_search_exa"]


def test_hydrate_infers_docker_vs_local(monkeypatch, tmp_path):
    from nymeria.onboarding import HostingOption
    from nymeria.setup.hydrate import hydrate_state_from_disk
    from nymeria.setup.state import WizardState

    # Docker first run writes .env.docker (no host data dir / db backend).
    docker_root = tmp_path / "checkout"
    docker_root.mkdir()
    _first_run(monkeypatch, docker_root, "--hosting", "docker")
    dstate = WizardState(root=docker_root)
    assert hydrate_state_from_disk(dstate) is True
    assert dstate.hosting is HostingOption.DOCKER
    # The single-container write carries no POSTGRES_PASSWORD, so it infers slim.
    from nymeria.onboarding import DockerStack

    assert dstate.docker_stack is DockerStack.SLIM
    # Docker picks hydrate from the NYMERIA_INIT_* carriers in .env.docker; a
    # no-pick first run wrote none, so nothing is filled.
    assert "web_search" not in dstate.extras

    # Local first run writes config.env and is inferred LOCAL.
    local_root = tmp_path / "local"
    _first_run(monkeypatch, local_root)
    lstate = WizardState(root=local_root)
    assert hydrate_state_from_disk(lstate) is True
    assert lstate.hosting is HostingOption.LOCAL


def test_hydrate_infers_full_vs_slim_docker_stack(tmp_path):
    from nymeria.onboarding import DockerStack, HostingOption
    from nymeria.setup.hydrate import hydrate_state_from_disk
    from nymeria.setup.state import WizardState

    # A full-stack .env.docker carries POSTGRES_PASSWORD; the slim shape never does.
    full_root = tmp_path / "full"
    full_root.mkdir()
    (full_root / ".env.docker").write_text(
        "LLM_PROVIDER=anthropic\nPOSTGRES_PASSWORD=secret\n", encoding="utf-8"
    )
    fstate = WizardState(root=full_root)
    assert hydrate_state_from_disk(fstate) is True
    assert fstate.hosting is HostingOption.DOCKER
    assert fstate.docker_stack is DockerStack.FULL

    slim_root = tmp_path / "slim"
    slim_root.mkdir()
    (slim_root / ".env.docker").write_text("LLM_PROVIDER=anthropic\n", encoding="utf-8")
    sstate = WizardState(root=slim_root)
    assert hydrate_state_from_disk(sstate) is True
    assert sstate.docker_stack is DockerStack.SLIM


# --- reconfigure mode: section filtering + runner ---------------------------


def test_build_section_steps_filters_to_section_plus_deps():
    from nymeria.setup.nav import Navigator
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps import build_section_steps, default_step_ids

    assert "provider" in default_step_ids() and "image_gen" in default_step_ids()

    # A family jump keeps the backend-keys step resolvable.
    state = WizardState(extras={"image_gen": ["image_gen_openai"]})
    steps = build_section_steps("image_gen")
    nav = Navigator(steps, state)
    assert [steps[i].id for i in nav.applicable_indices()] == [
        "welcome", "image_gen", "backend_keys", "review",
    ]

    # The LLM unit stays together (auth_method included so a jump can flip
    # between the API-key trio and the CLIProxy branch, llm_tuning because a
    # model switch changes what effort/sampling make sense); connection drops
    # out for a provider that needs no base URL, and the cliproxy_* steps drop
    # out on the API-key path.
    s2 = WizardState(provider="anthropic")
    nav2 = Navigator(build_section_steps("model"), s2)
    kept = [build_section_steps("model")[i].id for i in nav2.applicable_indices()]
    assert kept == ["welcome", "auth_method", "provider", "model", "llm_tuning", "review"]

    # A tuning-only jump re-runs just itself (hydrated provider satisfies its
    # applies predicate) without dragging the whole LLM unit back up.
    s3 = WizardState(provider="anthropic")
    nav3 = Navigator(build_section_steps("llm_tuning"), s3)
    kept3 = [build_section_steps("llm_tuning")[i].id for i in nav3.applicable_indices()]
    assert kept3 == ["welcome", "llm_tuning", "review"]


def test_run_init_rejects_unknown_section(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    with pytest.raises(SystemExit) as exc:
        setup_main(["definitely_not_a_section", "--root", str(tmp_path / "x")])
    assert "Unknown section" in str(exc.value)


# --- reconfigure mode: finalize merge-write + profile update -----------------


def test_merge_write_preserves_untouched_lines_and_secrets_key(monkeypatch, tmp_path):
    from nymeria.setup.finalize import finalize
    from nymeria.setup.hydrate import hydrate_state_from_disk
    from nymeria.setup.state import WizardState

    root = tmp_path / "runtime"
    _first_run(monkeypatch, root)
    config_path = root / "config.env"
    # A hand-added line the user put in config.env must survive a reconfigure.
    original = config_path.read_text(encoding="utf-8")
    config_path.write_text(original + "MY_CUSTOM_VAR=keepme\n", encoding="utf-8")
    key_before = _read_secrets_key(original)

    state = WizardState(root=root)
    assert hydrate_state_from_disk(state) is True
    state.model = "claude-new-model"  # the one thing we change
    console, _ = _capture_console()
    assert finalize(state, console=console, non_interactive=False,
                    overwrite_confirmed=True, merge=True) == 0

    after = config_path.read_text(encoding="utf-8")
    assert "MY_CUSTOM_VAR=keepme" in after          # untouched line preserved
    assert "LLM_MODEL=claude-new-model" in after     # changed value applied
    assert _read_secrets_key(after) == key_before    # key never rotated
    # The kept provider key line survives (blank field == keep existing).
    assert "ANTHROPIC_DIRECT_API_KEY=sk-ant-x" in after
    assert "LLM_PROVIDER=anthropic" in after          # not downgraded


def test_reconfigure_updates_profile_picks_without_clobbering_custom(monkeypatch, tmp_path):
    import json

    from nymeria.core.user_profile import UserProfileManager
    from nymeria.setup.finalize import finalize
    from nymeria.setup.hydrate import hydrate_state_from_disk
    from nymeria.setup.state import WizardState
    from nymeria.setup.tool_seed import core_seed_tool_names

    root = tmp_path / "runtime"
    data_dir = root / "data"
    _first_run(monkeypatch, root, "--web-search", "web_search_tavily", "--tavily-api-key", "k")
    manager = UserProfileManager(data_dir)
    profile = manager.get_profile("default")
    tools = list(profile.tool_preferences.default_thread_tools or [])
    tools.append("my_custom_tool")
    profile.tool_preferences.default_thread_tools = tools
    manager.save_profile(profile)

    state = WizardState(root=root)
    assert hydrate_state_from_disk(state) is True
    state.extras["web_search"] = ["web_search_brave"]  # swap the search backend
    console, _ = _capture_console()
    assert finalize(state, console=console, non_interactive=False,
                    overwrite_confirmed=True, merge=True,
                    scoped_section="web_search") == 0

    after = json.loads(
        (data_dir / "users" / "default" / "profile.json").read_text(encoding="utf-8")
    )
    result = after["tool_preferences"]["default_thread_tools"]
    assert "web_search_brave" in result
    assert "web_search_tavily" not in result          # the swapped-out pick is gone
    assert "my_custom_tool" in result                 # user-added tool preserved
    assert set(core_seed_tool_names()) <= set(result)  # core seed intact


def test_scoped_reconfigure_skips_token_and_start(monkeypatch, tmp_path):
    from nymeria.setup.finalize import finalize
    from nymeria.setup.hydrate import hydrate_state_from_disk
    from nymeria.setup.state import WizardState

    root = tmp_path / "runtime"
    _first_run(monkeypatch, root)
    state = WizardState(root=root)
    assert hydrate_state_from_disk(state) is True
    state.extras["image_gen"] = ["image_gen_gemini"]
    console, buf = _capture_console()
    rc = finalize(state, console=console, non_interactive=False,
                  overwrite_confirmed=True, merge=True, scoped_section="image_gen")
    out = buf.getvalue()
    assert rc == 0
    assert "Updated" in out and "image_gen" in out
    # A scoped edit must not re-print the one-time bootstrap token.
    assert "Bootstrap token" not in out


def test_reconfigure_docker_merges_env_docker_only(monkeypatch, tmp_path):
    from nymeria.setup.finalize import finalize
    from nymeria.setup.hydrate import hydrate_state_from_disk
    from nymeria.setup.state import WizardState

    root = tmp_path / "checkout"
    root.mkdir()
    _first_run(monkeypatch, root, "--hosting", "docker")
    state = WizardState(root=root)
    assert hydrate_state_from_disk(state) is True
    state.model = "claude-docker-new"
    console, _ = _capture_console()
    assert finalize(state, console=console, non_interactive=False,
                    overwrite_confirmed=True, merge=True) == 0

    content = (root / ".env.docker").read_text(encoding="utf-8")
    assert "LLM_MODEL=claude-docker-new" in content
    # No host-side profile is created for a Docker reconfigure.
    assert not (root / "data" / "users" / "default" / "profile.json").exists()


def test_docker_reconfigure_keeps_carrier_when_picks_untouched(monkeypatch, tmp_path):
    from nymeria.setup.finalize import finalize
    from nymeria.setup.hydrate import hydrate_state_from_disk
    from nymeria.setup.state import WizardState

    root = tmp_path / "checkout"
    root.mkdir()
    _first_run(
        monkeypatch, root, "--hosting", "docker",
        "--web-search", "web_search_tavily", "--tavily-api-key", "k",
    )
    before = (root / ".env.docker").read_text(encoding="utf-8")
    carrier = _env_line(before, "NYMERIA_INIT_DEFAULT_THREAD_TOOLS")
    assert carrier

    # A reconfigure that only changes the model: hydrate restores the picks, so
    # the carrier is re-produced with the same value, not retired.
    state = WizardState(root=root)
    assert hydrate_state_from_disk(state) is True
    state.model = "claude-docker-new"
    console, _ = _capture_console()
    assert finalize(state, console=console, non_interactive=False,
                    overwrite_confirmed=True, merge=True) == 0
    after = (root / ".env.docker").read_text(encoding="utf-8")
    assert _env_line(after, "NYMERIA_INIT_DEFAULT_THREAD_TOOLS") == carrier
    assert "LLM_MODEL=claude-docker-new" in after


def test_docker_reconfigure_revert_to_defaults_retires_carrier(monkeypatch, tmp_path):
    from nymeria.setup.finalize import finalize
    from nymeria.setup.hydrate import hydrate_state_from_disk
    from nymeria.setup.state import WizardState

    root = tmp_path / "checkout"
    root.mkdir()
    _first_run(
        monkeypatch, root, "--hosting", "docker",
        "--web-search", "web_search_tavily", "--tavily-api-key", "k",
    )
    assert "NYMERIA_INIT_DEFAULT_THREAD_TOOLS" in (
        (root / ".env.docker").read_text(encoding="utf-8")
    )

    # The user deselects the extra pick (back to backend defaults). The stale
    # carrier must be REMOVED, or a later fresh volume (down -v && up -d with the
    # same .env.docker) would re-seed the reverted pick.
    state = WizardState(root=root)
    assert hydrate_state_from_disk(state) is True
    assert state.extras.get("web_search") == ["web_search_tavily"]
    state.extras["web_search"] = []
    console, _ = _capture_console()
    assert finalize(state, console=console, non_interactive=False,
                    overwrite_confirmed=True, merge=True) == 0
    after = (root / ".env.docker").read_text(encoding="utf-8")
    assert "NYMERIA_INIT_" not in after


def test_review_summary_markup_surfaces_collected_choices():
    from nymeria.onboarding import (
        DockerStack,
        ExternalAccess,
        HostingOption,
        ProviderAuthMethod,
        SecurityProfile,
    )
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.review import _summary_markup

    state = WizardState(
        hosting=HostingOption.DOCKER,
        docker_stack=DockerStack.FULL,
        security_profile=SecurityProfile.SECURE,
        auth_method=ProviderAuthMethod.API_KEY,
        provider="anthropic",
        model="claude-opus-4-8",
        api_key="sk-ant-x",
        external_access=ExternalAccess.TAILSCALE,
        extras={
            "web_search": ["web_search_perplexity"],
            "context_strategy": "compact_tokens",
            "compact_threshold_tokens": "200000",
            "llm_effort": "medium",
            "rag_search": "__skip__",
        },
    )
    markup = _summary_markup(state)

    assert "Hosting" in markup
    assert "Stack" in markup  # docker host -> stack (slim/full) surfaces
    assert "Security" in markup
    assert "claude-opus-4-8" in markup
    assert "Default thread tools:" in markup  # core seed + picks, now written
    assert "web_search_perplexity" in markup  # picked family member
    assert "Context: Auto-compact at a token count" in markup
    assert "COMPACT_THRESHOLD_TOKENS=200000" in markup
    assert "Reasoning effort: Medium" in markup
    assert "RAG search" not in markup  # a skipped placeholder is not shown
    assert "Tailscale" in markup
    # The post-setup handoff is surfaced (default is print, not start).
    assert "Next" in markup
    assert "print the start command" in markup


def test_print_deployment_summary_suppresses_local_only_and_omits_docker_stack():
    from nymeria.onboarding import (
        DockerStack,
        ExternalAccess,
        HostingOption,
        SecurityProfile,
    )
    from nymeria.setup.finalize import print_deployment_summary
    from nymeria.setup.state import WizardState

    console, buf = _capture_console()
    state = WizardState(
        hosting=HostingOption.DOCKER,
        docker_stack=DockerStack.FULL,  # acted on, not a "recorded only" placeholder
        security_profile=SecurityProfile.UNLEASHED,  # the only selectable profile
        external_access=ExternalAccess.LOCAL_ONLY,  # the recommended default
    )
    print_deployment_summary(state, console)
    out = buf.getvalue()

    assert "Stack" not in out  # the docker stack is wired, so it is not echoed here
    assert "External access" not in out  # suppressed: local-only is the default
    assert "Security profile" in out and "Unleashed" in out
    assert "still being built" in out  # honest placeholder framing


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


# --- interactive Textual wizard (Pilot) -------------------------------------


async def _no_models(*_args, **_kwargs):
    """Stand-in for the live model fetch so Pilot tests never hit the network."""
    return []


# Step indices in the default flow (welcome, hosting, docker_stack, security,
# auth, the five cliproxy_* branch steps, provider, connection, model, ...).
# docker_stack (2) only applies to a Docker host and the cliproxy_* steps
# (5-9) only to the subscription branch, so on the default local API-key path
# the provider step is index 10.
_HOSTING_STEP = 1
_SECURITY_STEP = 3
_AUTH_STEP = 4
_PROVIDER_STEP = 10
_CONNECTION_STEP = 11


async def _advance_to_provider(pilot) -> None:
    """Walk welcome -> hosting -> security -> auth on the default (local) path.

    Accepts every default (local hosting, Unleashed security profile, Direct
    API key) and leaves the provider picker focused. docker_stack is skipped
    because the default hosting is not Docker.
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
            # docker_stack (index 2) is skipped for a non-Docker host.
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


def test_wizard_pilot_security_step_unleashed_only():
    """Secure and Standard render disabled (greyed out, unfocusable); the dot
    and focus land on Unleashed, arrows never reach a disabled row, and Enter
    stores UNLEASHED.
    """
    from nymeria.onboarding import SecurityProfile
    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.base import CircleRadioButton

    async def drive() -> WizardState:
        state = WizardState()
        app = SetupWizardApp(state)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")  # welcome -> hosting
            await pilot.pause()
            await pilot.press("enter")  # accept default hosting (local) -> security
            await pilot.pause()
            assert app.nav.current() == _SECURITY_STEP
            scr = app.screen
            buttons = list(scr.query(CircleRadioButton))
            assert [b.disabled for b in buttons] == [True, True, False]
            assert buttons[2].value is True  # dot pre-selected on Unleashed
            assert scr.focused is buttons[2]
            for key in ("up", "down"):
                await pilot.press(key)
                await pilot.pause()
                assert scr.focused is not buttons[0]
                assert scr.focused is not buttons[1]
            await pilot.press("enter")  # lock Unleashed, advance to auth method
            await pilot.pause()
            assert app.nav.current() == _AUTH_STEP
        return state

    state = asyncio.run(drive())
    assert state.security_profile is SecurityProfile.UNLEASHED


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


def test_wizard_pilot_voice_default_preserved_and_enter_locks_focus():
    """The TTS step's default is "Off" (first row) on a fresh run. Entering
    keeps that default selected and focused; Enter without moving records it.
    Arrowing moves focus (not selection); Enter then locks the focused option in.
    """
    from textual.widgets import Static

    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.base import CircleRadioButton
    from nymeria.setup.steps.placeholders import make_tts_step
    from nymeria.setup.voice_catalog import TTS_CHOICES

    # The catalog order drives the rows: "none" first, then the providers.
    assert TTS_CHOICES[0].value == "none"
    assert TTS_CHOICES[1].value == "kokoro"
    none_index = 0

    async def drive_enter_only() -> dict:
        app = SetupWizardApp(WizardState(), steps=[make_tts_step()])
        async with app.run_test() as pilot:
            await pilot.pause()
            scr = app.screen
            buttons = list(scr.query(CircleRadioButton))
            panel = scr.query_one("#choice-desc", Static)
            # Selection, focus, and description all sit on the stored default.
            assert buttons[none_index].value is True
            assert scr.focused is buttons[none_index]
            assert str(panel.render()) == TTS_CHOICES[0].description
            await pilot.press("enter")  # commit without moving
            await pilot.pause()
        return dict(app.state.extras)

    async def drive_down_then_enter() -> dict:
        app = SetupWizardApp(WizardState(), steps=[make_tts_step()])
        async with app.run_test() as pilot:
            await pilot.pause()
            scr = app.screen
            buttons = list(scr.query(CircleRadioButton))
            await pilot.press("down")  # focus moves to the next row (kokoro)
            await pilot.pause()
            assert scr.focused is buttons[1]  # focus moved...
            assert buttons[none_index].value is True  # ...but selection did not
            await pilot.press("enter")  # Enter locks the focused option in + advances
            await pilot.pause()
        return dict(app.state.extras)

    assert asyncio.run(drive_enter_only()) == {"tts": "none"}
    assert asyncio.run(drive_down_then_enter()) == {"tts": "kokoro"}


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
    # Hosting unset means a non-Docker host, so docker_stack is skipped and the
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


def test_wizard_pilot_skill_kits_multiselect_defaults_on_and_records_list():
    """The skill-kits step is a real multi-select: the curated default kit set is
    checked by default, the offered set is discovered live from bundled kits, and
    the chosen kit names are recorded as a list in extras.
    """
    from nymeria.setup import family_catalog
    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.placeholders import make_skill_kits_step, seeded_global_skills

    async def drive() -> WizardState:
        state = WizardState()
        app = SetupWizardApp(state, steps=[make_skill_kits_step()])
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")  # accept the default-checked set
            await pilot.pause()
        return state

    state = asyncio.run(drive())
    # The recorded picks are exactly the curated default-on kits (order follows the
    # discovered list, so compare order-independently).
    assert set(state.extras["skill_kits"]) == set(family_catalog.default_checked_skill_kits())
    # The offered set is discovered live, so it is a superset of the defaults.
    offered = {c.value for c in family_catalog.skill_kit_choices()}
    assert set(family_catalog.default_checked_skill_kits()) <= offered
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


def test_wizard_pilot_auth_step_enters_cliproxy_branch():
    """Selecting subscription OAuth advances into the CLIProxy branch (the
    disclaimer step), and the API-key provider trio drops out of the flow.
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
            await pilot.press("down")  # focus Subscription OAuth via CLIProxy
            await pilot.pause()
            await pilot.press("enter")  # accepted -> the disclaimer step
            await pilot.pause()
        return app

    app = asyncio.run(drive())
    assert app.state.auth_method is ProviderAuthMethod.CLIPROXY_OAUTH
    # Index 5 is cliproxy_disclaimer, the first step of the branch.
    assert app.nav.current() == 5


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
    assert start_now_applies(WizardState(hosting=HostingOption.SERVICE)) is True
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
    # The service install is detached like Docker, so it defaults to starting.
    service = start_now_choices(HostingOption.SERVICE)
    assert service[0].value is NextAction.START_API_OPEN_FRONTEND
    assert "service install" in service[1].description
    for choices in (docker, local, service):
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
    assert "start_now" in applicable(WizardState(hosting=HostingOption.SERVICE))


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


def test_finalize_installs_service_when_opted_in(monkeypatch, tmp_path, capsys):
    import nymeria.service_install as si

    _stub_llm(monkeypatch)
    root = tmp_path / "runtime"
    installs: list[tuple[list[str], object]] = []

    class _FakeManager:
        name = "systemd user service"

        def install(self, *, exec_argv, root):
            installs.append((list(exec_argv), root))
            return si.InstallReport(
                artifact=tmp_path / "nymeria.service",
                lines=("Installed systemd user unit: fake",),
                notes=("Logs: journalctl --user -u nymeria.service",),
            )

        def log_hint(self):
            return "journalctl --user -u nymeria.service"

    monkeypatch.setattr(si, "service_manager", lambda: _FakeManager())
    monkeypatch.setattr(si, "resolve_exec_argv", lambda: ["/usr/bin/python3", "slim"])
    health: list[dict] = []
    monkeypatch.setattr(
        finalize_mod, "wait_for_health", lambda **kw: bool(health.append(kw)) or True
    )

    rc = setup_main(
        ["--provider", "anthropic", "--model", "claude-test-model",
         "--api-key", "sk-ant-test-key", "--hosting", "service",
         "--root", str(root), "--start", "--non-interactive"]
    )
    out = capsys.readouterr().out

    assert rc == 0
    assert installs and installs[0][1] == root  # the unit points at the runtime root
    assert health  # verified the backend actually came up, not just "active"
    assert "Nymeria is up" in out
    assert "nymeria service status" in out


def test_finalize_service_unavailable_falls_back_to_foreground(
    monkeypatch, tmp_path, capsys
):
    import nymeria.service_install as si

    _stub_llm(monkeypatch)
    root = tmp_path / "runtime"

    def unavailable():
        raise si.ServiceUnavailableError("no user manager", hints=("try lingering",))

    monkeypatch.setattr(si, "service_manager", unavailable)
    monkeypatch.setattr(
        finalize_mod,
        "wait_for_health",
        lambda **kw: pytest.fail("must not health-poll when install is impossible"),
    )

    rc = setup_main(
        ["--provider", "anthropic", "--model", "claude-test-model",
         "--api-key", "sk-ant-test-key", "--hosting", "service",
         "--root", str(root), "--start", "--non-interactive"]
    )
    out = capsys.readouterr().out

    assert rc == 0  # config was written fine; the install is the optional part
    assert "no user manager" in out
    assert "try lingering" in out
    assert "nymeria slim" in out


def test_finalize_service_print_path_has_real_commands(monkeypatch, tmp_path, capsys):
    import nymeria.service_install as si

    _stub_llm(monkeypatch)
    # Keep the host's real unit (if any) out of the summary warning.
    monkeypatch.setattr(si, "installed_artifact_path", lambda: None)
    root = tmp_path / "runtime"

    rc = setup_main(
        ["--provider", "anthropic", "--model", "claude-test-model",
         "--api-key", "sk-ant-test-key", "--hosting", "service",
         "--root", str(root), "--non-interactive"]
    )
    out = capsys.readouterr().out

    assert rc == 0
    # The old placeholder apology is gone; the handoff is the real install
    # command (pinned to this config's root, since a bare invocation can
    # resolve a different one) plus the management verbs.
    assert "not wired up yet" not in out
    assert "nymeria service install" in out
    assert "--root" in out
    assert "nymeria service status" in out
    assert "nymeria service uninstall" in out
    # The hosting choice round-trips via the wizard-only marker.
    assert "NYMERIA_HOSTING=service" in (root / "config.env").read_text()


def test_finalize_warns_about_stale_service_artifact(monkeypatch, tmp_path, capsys):
    import nymeria.service_install as si

    _stub_llm(monkeypatch)
    monkeypatch.setattr(
        si, "installed_artifact_path", lambda: tmp_path / "nymeria.service"
    )
    root = tmp_path / "runtime"

    rc = setup_main(
        ["--provider", "anthropic", "--model", "claude-test-model",
         "--api-key", "sk-ant-test-key", "--hosting", "local",
         "--root", str(root), "--non-interactive"]
    )
    out = capsys.readouterr().out

    assert rc == 0
    # Switching away from SERVICE never tears the unit down silently; the
    # summary must say it is still installed and how to remove it.
    assert "nymeria service uninstall" in out


def test_hydrate_recovers_service_hosting(monkeypatch, tmp_path):
    import nymeria.service_install as si

    from nymeria.onboarding import HOSTING_MARKER_ENV
    from nymeria.setup.hydrate import hydrate_state_from_disk
    from nymeria.setup.state import WizardState

    _stub_llm(monkeypatch)
    root = tmp_path / "runtime"
    assert (
        setup_main(
            ["--provider", "anthropic", "--model", "claude-test-model",
             "--api-key", "sk-ant-test-key", "--hosting", "service",
             "--root", str(root), "--non-interactive"]
        )
        == 0
    )

    # The marker is authoritative: SERVICE round-trips even with no artifact
    # installed (and, the important direction, a switch away from SERVICE
    # sticks while the old unit still exists on disk).
    monkeypatch.setattr(si, "installed_artifact_path", lambda: None)
    state = WizardState(root=root)
    assert hydrate_state_from_disk(state)
    assert state.hosting is HostingOption.SERVICE

    # Marker-less configs (written before the marker existed) fall back to
    # the installed artifact to tell LOCAL from SERVICE.
    config = root / "config.env"
    config.write_text(
        "\n".join(
            line
            for line in config.read_text().splitlines()
            if not line.startswith(HOSTING_MARKER_ENV)
        )
        + "\n"
    )
    fake_unit = tmp_path / "nymeria.service"
    fake_unit.write_text("[Unit]\n")
    monkeypatch.setattr(si, "installed_artifact_path", lambda: fake_unit)
    legacy = WizardState(root=root)
    assert hydrate_state_from_disk(legacy)
    assert legacy.hosting is HostingOption.SERVICE

    monkeypatch.setattr(si, "installed_artifact_path", lambda: None)
    plain = WizardState(root=root)
    assert hydrate_state_from_disk(plain)
    assert plain.hosting is HostingOption.LOCAL


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


# --- full Docker stack (Postgres + Redis) -----------------------------------


def _env_line(text: str, key: str) -> str | None:
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith(f"{key}="):
            value = stripped.split("=", 1)[1].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            return value or None
    return None


def test_docker_stack_spec_selects_per_stack():
    from nymeria.onboarding import DockerStack
    from nymeria.setup.state import WizardState

    slim = finalize_mod._docker_stack_spec(WizardState(docker_stack=DockerStack.SLIM))
    assert slim.compose_args == ("-f", "docker-compose.single.yml")
    assert slim.service == "nymeria-single"
    assert slim.health_url.endswith("/health")

    full = finalize_mod._docker_stack_spec(WizardState(docker_stack=DockerStack.FULL))
    assert full.compose_args == ("--env-file", ".env.docker")
    assert full.service == "api"
    assert full.health_url.endswith("/ready")
    # No command-env sentinel: DISCORD_BOT_TOKEN is fully defaulted in the compose,
    # so the api self-mints with an empty (cleanly "not configured") token.
    assert full.command_env == ()
    # The full stack's first boot (build + deep Postgres/Redis check) gets a longer
    # readiness budget than the single container.
    assert full.health_timeout > slim.health_timeout

    # No explicit stack defaults to slim (back-compat with the single-container path).
    assert finalize_mod._docker_stack_spec(WizardState()).service == "nymeria-single"


def test_compose_command_str_per_stack():
    from nymeria.onboarding import DockerStack
    from nymeria.setup.state import WizardState

    slim = finalize_mod._docker_stack_spec(WizardState(docker_stack=DockerStack.SLIM))
    full = finalize_mod._docker_stack_spec(WizardState(docker_stack=DockerStack.FULL))

    assert (
        finalize_mod._compose_command_str(slim, "up", "-d")
        == "docker compose -f docker-compose.single.yml up -d"
    )
    # The full stack uses --env-file with no DISCORD sentinel prefix.
    assert (
        finalize_mod._compose_command_str(full, "up", "-d")
        == "docker compose --env-file .env.docker up -d"
    )


def test_finalize_full_stack_writes_minted_db_secrets(monkeypatch, tmp_path):
    _stub_llm(monkeypatch)
    monkeypatch.delenv("NYMERIA_SECRETS_KEY", raising=False)
    root = tmp_path / "checkout"
    root.mkdir()

    args = [
        "--provider", "anthropic", "--model", "claude-test-model",
        "--api-key", "sk-ant-x", "--hosting", "docker", "--docker-stack", "full",
        "--root", str(root), "--non-interactive",
    ]
    assert setup_main(args) == 0

    content = (root / ".env.docker").read_text(encoding="utf-8")
    # The compose requires POSTGRES_PASSWORD / REDIS_PASSWORD; both are minted.
    pg = _env_line(content, "POSTGRES_PASSWORD")
    rd = _env_line(content, "REDIS_PASSWORD")
    assert pg and rd and pg != rd
    assert '"' not in pg and len(pg) >= 16  # unquoted, non-trivial
    assert "POSTGRES_USER=nymeria" in content
    assert "POSTGRES_DB=nymeria" in content
    # The api self-mints the internal service token onto the shared volume, so the
    # installer writes no NYMERIA_SERVICE_TOKEN line of its own on a fresh install.
    assert "NYMERIA_SERVICE_TOKEN" not in content

    # A reconfigure (--force) must PRESERVE the same DB password: rotating it would
    # break auth against the already-initialized postgres volume.
    assert setup_main(args + ["--force"]) == 0
    content2 = (root / ".env.docker").read_text(encoding="utf-8")
    assert _env_line(content2, "POSTGRES_PASSWORD") == pg
    assert _env_line(content2, "REDIS_PASSWORD") == rd


def test_finalize_slim_docker_writes_no_db_secrets(monkeypatch, tmp_path):
    _stub_llm(monkeypatch)
    root = tmp_path / "checkout"
    root.mkdir()
    # Default (no --docker-stack) is slim; it must not write Postgres/Redis secrets.
    assert setup_main(
        ["--provider", "anthropic", "--model", "m", "--api-key", "sk-ant-x",
         "--hosting", "docker", "--root", str(root), "--non-interactive"]
    ) == 0
    content = (root / ".env.docker").read_text(encoding="utf-8")
    assert "POSTGRES_PASSWORD" not in content
    assert "REDIS_PASSWORD" not in content


def test_full_stack_and_slim_both_carry_init_picks(monkeypatch, tmp_path, capsys):
    _stub_llm(monkeypatch)
    picks = ["--web-search", "web_search_tavily"]

    # Full stack: the shared api/worker `environment:` anchor passes NYMERIA_INIT_*
    # through `--env-file` interpolation, so the picks ARE written and there is no
    # "set them in Settings" note anymore.
    full_root = tmp_path / "full"
    full_root.mkdir()
    assert setup_main(
        ["--provider", "anthropic", "--model", "m", "--api-key", "sk-ant-x",
         "--hosting", "docker", "--docker-stack", "full", "--root", str(full_root),
         "--non-interactive", *picks]
    ) == 0
    full_content = (full_root / ".env.docker").read_text(encoding="utf-8")
    full_out = capsys.readouterr().out
    assert "NYMERIA_INIT_DEFAULT_THREAD_TOOLS" in full_content
    assert "not yet carried into the full stack" not in full_out

    # Slim stack with the same picks carries them too (env_file injects the whole file).
    slim_root = tmp_path / "slim"
    slim_root.mkdir()
    assert setup_main(
        ["--provider", "anthropic", "--model", "m", "--api-key", "sk-ant-x",
         "--hosting", "docker", "--docker-stack", "slim", "--root", str(slim_root),
         "--non-interactive", *picks]
    ) == 0
    slim_content = (slim_root / ".env.docker").read_text(encoding="utf-8")
    assert "NYMERIA_INIT_DEFAULT_THREAD_TOOLS" in slim_content

    # Both shapes write the same carrier value: one writer, two delivery paths.
    assert (_env_line(full_content, "NYMERIA_INIT_DEFAULT_THREAD_TOOLS")
            == _env_line(slim_content, "NYMERIA_INIT_DEFAULT_THREAD_TOOLS"))


def test_finalize_full_stack_default_prints_single_up_and_token_read(monkeypatch, tmp_path, capsys):
    _stub_llm(monkeypatch)
    root = tmp_path / "checkout"
    root.mkdir()

    def boom(*_a, **_k):
        pytest.fail("the default handoff must not launch a process")

    monkeypatch.setattr(finalize_mod.subprocess, "run", boom)
    assert setup_main(
        ["--provider", "anthropic", "--model", "m", "--api-key", "sk-ant-x",
         "--hosting", "docker", "--docker-stack", "full",
         "--root", str(root), "--non-interactive"]
    ) == 0
    out = capsys.readouterr().out

    # The full-stack handoff is now a single `up -d` plus the in-container bootstrap
    # token read: no api-only first phase, no DISCORD sentinel, no host-side mint.
    assert "docker compose --env-file .env.docker up -d" in out
    assert "DISCORD_BOT_TOKEN=disabled" not in out
    assert "up -d api" not in out
    assert "exec api cat /data/BOOTSTRAP_TOKEN.txt" in out
    assert "users add bot-service" not in out


def test_finalize_starts_full_stack_single_up_and_surfaces_token(monkeypatch, tmp_path, capsys):
    _stub_llm(monkeypatch)
    root = tmp_path / "checkout"
    root.mkdir()
    boot_token = "nym_bootstrap_aaa111"
    calls: list[tuple[list[str], object, dict]] = []

    class _Result:
        def __init__(self, stdout=""):
            self.returncode = 0
            self.stdout = stdout
            self.stderr = ""

    def fake_run(cmd, *args, **kwargs):
        calls.append((cmd, kwargs.get("cwd"), kwargs.get("env") or {}))
        if "cat" in cmd:
            return _Result(stdout=f"Token: {boot_token}\n")
        return _Result()

    health: list[dict] = []
    monkeypatch.setattr(finalize_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(
        finalize_mod, "wait_for_health", lambda **kw: bool(health.append(kw)) or True
    )

    rc = setup_main(
        ["--provider", "anthropic", "--model", "claude-test-model",
         "--api-key", "sk-ant-x", "--hosting", "docker", "--docker-stack", "full",
         "--root", str(root), "--start", "--non-interactive"]
    )
    out = capsys.readouterr().out
    assert rc == 0

    cmds = [cmd for cmd, _cwd, _env in calls]
    # A single `up -d` brings up the whole stack (no api-only phase, no DISCORD
    # sentinel); the api self-mints the service token onto the shared volume and the
    # worker/mcp/watchdog read it from disk, so there is no host-side mint.
    assert cmds[0] == ["docker", "compose", "--env-file", ".env.docker", "up", "-d"]
    assert "DISCORD_BOT_TOKEN=disabled" not in out
    # Health polled on the deep /ready endpoint.
    assert health and str(health[0].get("url", "")).endswith("/ready")
    # Bootstrap token surfaced from the api container; NO `users add` mint exec.
    assert any("cat" in c for c in cmds)
    assert boot_token in out
    assert not any("users" in c for c in cmds)
    # The installer writes no NYMERIA_SERVICE_TOKEN line (the api self-mints it).
    assert _env_line((root / ".env.docker").read_text(encoding="utf-8"),
                     "NYMERIA_SERVICE_TOKEN") is None
    # Exactly one `up -d` total (no second recreate phase).
    up = ["docker", "compose", "--env-file", ".env.docker", "up", "-d"]
    assert cmds.count(up) == 1


def test_finalize_full_stack_start_carries_init_picks_without_note(monkeypatch, tmp_path, capsys):
    _stub_llm(monkeypatch)
    root = tmp_path / "checkout"
    root.mkdir()

    class _Result:
        returncode = 0
        stdout = "Token: nym_bootstrap_aaa111\n"
        stderr = ""

    monkeypatch.setattr(finalize_mod.subprocess, "run", lambda *a, **k: _Result())
    monkeypatch.setattr(finalize_mod, "wait_for_health", lambda **kw: True)

    rc = setup_main(
        ["--provider", "anthropic", "--model", "m", "--api-key", "sk-ant-x",
         "--hosting", "docker", "--docker-stack", "full", "--root", str(root),
         "--start", "--non-interactive", "--web-search", "web_search_tavily"]
    )
    out = capsys.readouterr().out
    assert rc == 0
    # The picks now reach the full stack via the compose env passthrough, so the
    # old "set them in Settings" note must be gone from both the config output and
    # the start handoff, and the carriers must be in the written env file.
    assert "not yet carried into the full stack" not in out
    content = (root / ".env.docker").read_text(encoding="utf-8")
    assert _env_line(content, "NYMERIA_INIT_DEFAULT_THREAD_TOOLS")


def test_finalize_full_stack_service_token_preserved_on_reconfigure(monkeypatch, tmp_path):
    _stub_llm(monkeypatch)
    root = tmp_path / "checkout"
    root.mkdir()
    (root / ".env.docker").write_text(
        "NYMERIA_SERVICE_TOKEN=nym_existing_token\nPOSTGRES_PASSWORD=keepme\n",
        encoding="utf-8",
    )

    def fake_run(cmd, *args, **kwargs):
        if "users" in cmd and "add" in cmd:
            pytest.fail("must not re-mint a service token that already exists")
        class _R:
            returncode = 0
            stdout = "Token: nym_bootstrap_xyz\n"
            stderr = ""
        return _R()

    monkeypatch.setattr(finalize_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(finalize_mod, "wait_for_health", lambda **kw: True)

    rc = setup_main(
        ["--provider", "anthropic", "--model", "m", "--api-key", "sk-ant-x",
         "--hosting", "docker", "--docker-stack", "full", "--root", str(root),
         "--start", "--force", "--non-interactive"]
    )
    assert rc == 0
    content = (root / ".env.docker").read_text(encoding="utf-8")
    assert _env_line(content, "NYMERIA_SERVICE_TOKEN") == "nym_existing_token"
    # The preserved POSTGRES_PASSWORD is also kept (not rotated).
    assert _env_line(content, "POSTGRES_PASSWORD") == "keepme"


def test_finalize_full_stack_health_timeout_prints_token_read(monkeypatch, tmp_path, capsys):
    _stub_llm(monkeypatch)
    root = tmp_path / "checkout"
    root.mkdir()
    calls: list[list[str]] = []

    class _R:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, *_a, **_k):
        calls.append(cmd)
        return _R()

    monkeypatch.setattr(finalize_mod.subprocess, "run", fake_run)
    # The API never becomes healthy within the timeout.
    monkeypatch.setattr(finalize_mod, "wait_for_health", lambda **_kw: False)

    rc = setup_main(
        ["--provider", "anthropic", "--model", "m", "--api-key", "sk-ant-x",
         "--hosting", "docker", "--docker-stack", "full", "--root", str(root),
         "--start", "--non-interactive"]
    )
    out = capsys.readouterr().out
    assert rc == 0  # a slow boot is non-fatal
    # The single `up -d` ran; on timeout we do NOT exec anything else (no token
    # read, no `users` mint, no second `up`).
    assert calls == [
        ["docker", "compose", "--env-file", ".env.docker", "up", "-d"]
    ]
    assert not any("users" in c for c in calls)
    # The bootstrap-token read is printed so the user can finish by hand; the api
    # still self-mints the service token, so no host-side mint command appears.
    assert "exec api cat /data/BOOTSTRAP_TOKEN.txt" in out
    assert "users add bot-service" not in out
    assert _env_line(
        (root / ".env.docker").read_text(encoding="utf-8"), "NYMERIA_SERVICE_TOKEN"
    ) is None


def test_finalize_full_stack_warns_on_shadowing_process_env(monkeypatch, tmp_path, capsys):
    _stub_llm(monkeypatch)
    root = tmp_path / "checkout"
    root.mkdir()
    # An exported POSTGRES_PASSWORD that differs from the generated one would win
    # over .env.docker in docker compose (shell env precedes --env-file); finalize
    # must flag it so the operator does not silently boot with stale credentials.
    monkeypatch.setenv("POSTGRES_PASSWORD", "shell-exported-value")
    assert setup_main(
        ["--provider", "anthropic", "--model", "m", "--api-key", "sk-ant-x",
         "--hosting", "docker", "--docker-stack", "full", "--root", str(root),
         "--non-interactive"]
    ) == 0
    out = capsys.readouterr().out
    assert "the environment already defines POSTGRES_PASSWORD" in out
    assert "unset POSTGRES_PASSWORD" in out


def test_finalize_full_stack_no_shadow_warning_when_env_clean(monkeypatch, tmp_path, capsys):
    _stub_llm(monkeypatch)
    root = tmp_path / "checkout"
    root.mkdir()
    # No conflicting values in the environment: the minted/written credentials are
    # the ones compose will use, so the shadow note must NOT fire (guards the
    # warning against false positives on a clean install).
    for key in ("POSTGRES_PASSWORD", "REDIS_PASSWORD", "NYMERIA_SECRETS_KEY",
                "POSTGRES_USER", "POSTGRES_DB"):
        monkeypatch.delenv(key, raising=False)
    assert setup_main(
        ["--provider", "anthropic", "--model", "m", "--api-key", "sk-ant-x",
         "--hosting", "docker", "--docker-stack", "full", "--root", str(root),
         "--non-interactive"]
    ) == 0
    out = capsys.readouterr().out
    assert "the environment already defines" not in out


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


# --- CLIProxy subscription branch ---------------------------------------------


def test_finalize_cliproxy_claude_local_writes_root_url_and_gatekeeper(
    monkeypatch, tmp_path
):
    """The Claude route: anthropic provider, proxy ROOT URL (no /v1), the cpx-
    gatekeeper in ANTHROPIC_API_KEY (never the DIRECT slot), and the management
    endpoint persisted for the backend's /cliproxy routes."""
    calls = _stub_llm(monkeypatch)
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
    assert "--cliproxy-gatekeeper-key" in str(exc.value)


def test_hydrate_infers_cliproxy_branch_from_base_url(monkeypatch, tmp_path):
    from nymeria.onboarding import ProviderAuthMethod
    from nymeria.setup.hydrate import hydrate_state_from_disk
    from nymeria.setup.state import WizardState

    _stub_llm(monkeypatch)
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
