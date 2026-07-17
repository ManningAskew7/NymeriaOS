"""Onboarding data model + declarative family catalog drift guards.

Split out of the former monolithic test_setup_wizard.py (dev-todo #54).
"""


from __future__ import annotations

import pytest
from nymeria.onboarding import (
    HOSTING_CHOICES,
    HOSTING_ORDER,
    HostingOption,
    choice_values,
    parse_choice,
)


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
        flagged = [opt for opt in order if table[opt].recommended]  # type: ignore[bad-index]
        expected = [recommended] if recommended is not None else []
        assert flagged == expected  # at most one, the expected default

    coming_soon = {opt for opt in SecurityProfile if SECURITY_PROFILE_CHOICES[opt].coming_soon}
    assert coming_soon == {SecurityProfile.SECURE, SecurityProfile.STANDARD}
    for table in (DOCKER_STACK_CHOICES, EXTERNAL_ACCESS_CHOICES):
        assert not any(meta.coming_soon for meta in table.values())


def test_security_profile_choices_disable_coming_soon_rows():
    """Secure and Standard render greyed out with a "(to come)" suffix until
    the approval gate ships (docs/private/security/security-profiles.md); Unleashed is
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
    # No picks recorded (step skipped): default to self-improve plus every
    # offered bundled kit (widened 2026-06-12 from the curated four).
    from nymeria.setup import family_catalog

    assert selected_global_skills_for_state(WizardState()) == [
        "self-improve",
        *family_catalog.default_checked_skill_kits(),
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
    # The curated default-on set: the six kits existing as of 2026-06-12,
    # deliberately a literal decoupled from discovery, so a newly bundled kit
    # is offered but NOT auto-checked (user decision; default-on stays a
    # per-kit call).
    assert default_checked == [
        "tool-management",
        "skill-management",
        "mcp-management",
        "credential-management",
        "callable-thread-builder",
        "trigger-management",
    ]
    # Every default-checked kit is actually offered (discovered live).
    assert set(default_checked) <= offered
    # The narrower backend fallback stays inside the curated set.
    assert {s for s in DEFAULT_GLOBAL_SKILLS if s != "self-improve"} <= set(
        default_checked
    )
    # self-improve is a guidance skill, not a selectable kit.
    assert "self-improve" not in offered
    # Discovery is drift-proof: it surfaces bundled kits beyond the legacy four.
    assert offered  # non-empty even if the scan path changes


def test_every_keyed_catalog_backend_has_a_key_spec():
    """Drift guard: a new web_search/image_gen backend needs a BACKEND_KEY_SPECS entry."""
    from nymeria.setup import family_catalog
    from nymeria.setup.tool_keys import BACKEND_KEY_SPECS

    # fetch_url_nymeria is intentionally keyless (uses the configured primary
    # LLM); web_search_ddgs is keyless by design (in-process metasearch, no
    # credential of any kind, not even a base URL).
    keyless = {"fetch_url_nymeria", "web_search_ddgs"}
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

# --- flattened auth step (beta-readiness 03) ---------------------------------


def test_auth_method_choices_are_flat_and_ordered(monkeypatch):
    """Three equal paths, no "(recommended)"/"(advanced)" labels, and no
    capability warnings when Docker and Ollama are both present."""
    import shutil

    from nymeria.onboarding import PROVIDER_AUTH_METHOD_ORDER, ProviderAuthMethod
    from nymeria.setup.steps.auth import _auth_choices
    from nymeria.setup.state import WizardState

    assert PROVIDER_AUTH_METHOD_ORDER == (
        ProviderAuthMethod.API_KEY,
        ProviderAuthMethod.CLIPROXY_OAUTH,
        ProviderAuthMethod.LOCAL_MODEL,
    )

    monkeypatch.setattr("nymeria.setup.environment.docker_available", lambda: True)
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/ollama")
    choices = _auth_choices(WizardState())

    assert [c.value for c in choices] == list(PROVIDER_AUTH_METHOD_ORDER)
    for choice in choices:
        assert "(recommended)" not in choice.label
        assert "(advanced)" not in choice.label
        assert "Warning:" not in choice.description
        assert not choice.disabled  # capability gaps warn, never disable
    # The ToS disclaimer pointer stays impossible to miss on the CLIProxy row.
    assert "disclaimer" in choices[1].description


def test_auth_choices_append_capability_warnings(monkeypatch):
    import shutil

    from nymeria.onboarding import ProviderAuthMethod
    from nymeria.setup.steps.auth import _auth_choices
    from nymeria.setup.state import WizardState

    monkeypatch.setattr("nymeria.setup.environment.docker_available", lambda: False)
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    by_value = {c.value: c for c in _auth_choices(WizardState())}

    cliproxy = by_value[ProviderAuthMethod.CLIPROXY_OAUTH]
    assert "existing CLIProxy endpoint" in cliproxy.description
    assert not cliproxy.disabled  # selectable: the endpoint step takes a URL
    local = by_value[ProviderAuthMethod.LOCAL_MODEL]
    assert "ollama.com" in local.description
    assert not local.disabled

    # A known management endpoint (hydrated reconfigure / flag) means no
    # local deploy is needed, so the Docker warning drops out.
    state = WizardState(cliproxy_management_url="http://localhost:8318")
    by_value = {c.value: c for c in _auth_choices(state)}
    assert "Warning:" not in by_value[ProviderAuthMethod.CLIPROXY_OAUTH].description


def test_store_auth_method_local_pins_ollama_and_clears_stale_details():
    from nymeria.onboarding import ProviderAuthMethod
    from nymeria.setup.steps.auth import store_auth_method
    from nymeria.setup.state import WizardState

    # Switching from a keyed provider: the key and connection details belong
    # to the old provider and must not leak into the Ollama config.
    state = WizardState(
        provider="anthropic",
        api_key="sk-ant-stale",
        api_mode="responses",
        base_url="https://api.anthropic.com",
    )
    store_auth_method(state, ProviderAuthMethod.LOCAL_MODEL)
    assert state.auth_method is ProviderAuthMethod.LOCAL_MODEL
    assert state.provider == "ollama"
    assert state.api_key == "" and state.api_mode == "" and state.base_url == ""

    # Re-selecting the local branch keeps a customized Ollama base URL.
    state.base_url = "http://box:11434"
    store_auth_method(state, ProviderAuthMethod.LOCAL_MODEL)
    assert state.base_url == "http://box:11434"

    # No provider chosen yet: flag-provided values are intent for THIS
    # branch (e.g. `--base-url` naming a remote Ollama) and must survive.
    fresh = WizardState(base_url="http://box:11434", api_key="proxy-token")
    store_auth_method(fresh, ProviderAuthMethod.LOCAL_MODEL)
    assert fresh.provider == "ollama"
    assert fresh.base_url == "http://box:11434"
    assert fresh.api_key == "proxy-token"

    # The other branches store the method and touch nothing else.
    store_auth_method(state, ProviderAuthMethod.API_KEY)
    assert state.auth_method is ProviderAuthMethod.API_KEY
    assert state.provider == "ollama" and state.base_url == "http://box:11434"


def test_runner_local_model_pins_ollama_unless_provider_given():
    from nymeria.onboarding import ProviderAuthMethod
    from nymeria.setup.runner import _build_state, build_parser

    args = build_parser().parse_args(["--auth-method", "local_model"])
    state = _build_state(args)
    assert state.auth_method is ProviderAuthMethod.LOCAL_MODEL
    assert state.provider == "ollama"

    # An explicit --provider wins: LM Studio and friends are local too.
    args = build_parser().parse_args(
        ["--auth-method", "local_model", "--provider", "lmstudio"]
    )
    state = _build_state(args)
    assert state.provider == "lmstudio"


def test_provider_signup_guidance_renders_from_registry():
    """The "get a key" panel is registry-sourced (one place for TUI + GUI)."""
    from nymeria.setup.steps.provider import _provider_signup

    text = _provider_signup("google")
    assert "https://aistudio.google.com/apikey" in text
    assert "gemini-3.5-flash" in text  # free-tier steering rides as copy

    # Paid keys carry the spend-real-money line.
    assert "spend" in _provider_signup("openai")
    # Uncurated providers render nothing (the panel hides itself).
    assert _provider_signup("groq") == ""
    assert _provider_signup(None) == ""
