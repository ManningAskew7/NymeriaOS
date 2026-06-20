"""Pure navigation model, quick path, and the tier chooser / quick defaults.

Split out of the former monolithic test_setup_wizard.py (dev-todo #54).
"""


from __future__ import annotations

from pathlib import Path
from nymeria.onboarding import HostingOption

from _setup_wizard_helpers import (
    _capture_console,
    _conditional_steps,
    _env_line,
)


# --- pure navigation model --------------------------------------------------


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
        "docker_stack", "security_profile", "core_tools",
        "web_search", "fetch_url", "embedder", "reranker", "image_gen",
        "backend_keys", "skill_kits", "tts", "stt", "llm_tuning",
        "context", "agent_limits",
    )

    # Full (default) path keeps the optional/placeholder steps. (reranker and
    # backend_keys are conditional on other picks, so they are excluded here.)
    full = applicable_ids(WizardState(hosting=HostingOption.LOCAL))
    for sid in ("security_profile", "auth_method", "core_tools", "web_search",
                "fetch_url", "embedder", "image_gen", "skill_kits", "external_access"):
        assert sid in full

    # Quick path keeps only the essentials; every skippable step is gated off.
    # A provider is set so the provider-gated model step is applicable (the
    # Navigator re-evaluates this as the real run advances). auth_method,
    # timezone, and external_access stay visible in quick mode (the LLM auth
    # choice, the detection confirm, and remote access are all irreducible).
    quick = applicable_ids(
        WizardState(hosting=HostingOption.LOCAL, provider="anthropic", quick=True)
    )
    assert set(quick) <= QUICK_KEEP_STEP_IDS
    for essential in ("welcome", "hosting", "auth_method", "provider", "model",
                      "timezone", "external_access", "start_now", "review"):
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
    # Keyless web fetch seeded; nothing that needs a search/image key. The
    # hosting-dependent web-search seed waits for a hosting pick.
    assert state.extras["fetch_url"] == ["fetch_url_nymeria"]
    assert "web_search" not in state.extras
    assert "image_gen" not in state.extras
    # The seeded default tools carry the keyless fetcher on top of the core seed.
    tools = default_thread_tools_for_state(state)
    assert "fetch_url_nymeria" in tools
    assert "bash_execute" in tools
    # Skill kits fall back to all-on (self-improve plus every bundled kit).
    from nymeria.setup import family_catalog

    assert selected_global_skills_for_state(state) == [
        "self-improve",
        *family_catalog.default_checked_skill_kits(),
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
    # Either flag locks the tier (the chooser screen is skipped); no flag
    # leaves it unlocked so the chooser shows.
    assert _build_state(build_parser().parse_args(base + ["--quick"])).tier_locked is True
    assert _build_state(build_parser().parse_args(base + ["--custom"])).tier_locked is True
    assert _build_state(build_parser().parse_args(base)).tier_locked is False


# --- tier chooser and hosting-dependent quick defaults -----------------------


def test_store_tier_choice_quickstart_applies_and_full_unwinds():
    from nymeria.onboarding import SetupTier
    from nymeria.setup.rag_catalog import QUICKSTART_EMBEDDER
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.tier import store_tier_choice

    # Hosting already known (a hydrated reconfigure), so the hosting-dependent
    # seeds apply at pick time too.
    state = WizardState(hosting=HostingOption.LOCAL)
    store_tier_choice(state, SetupTier.QUICKSTART)
    assert state.quick is True
    assert state.embedder == QUICKSTART_EMBEDDER
    assert state.extras["fetch_url"] == ["fetch_url_nymeria"]
    assert state.extras["web_search"] == ["web_search_ddgs"]
    assert state.extras["tts"] == "kokoro"
    assert state.extras["stt"] == "faster-whisper"

    # Switching to Full unwinds exactly the seeded values, so the full walk
    # starts from the normal step defaults (and the reranker step is not
    # gated off by a stale rag_quickstarted).
    store_tier_choice(state, SetupTier.FULL)
    assert state.quick is False
    assert state.embedder is None
    assert state.rag_quickstarted is False
    for key in ("fetch_url", "web_search", "tts", "stt"):
        assert key not in state.extras

    # And back again: the chooser round-trip re-seeds cleanly.
    store_tier_choice(state, SetupTier.QUICKSTART)
    assert state.quick is True
    assert state.extras["web_search"] == ["web_search_ddgs"]


def test_store_tier_choice_full_keeps_user_values():
    from nymeria.onboarding import SetupTier
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.tier import store_tier_choice

    # Values that predate the quickstart pick (flags, hydrated reconfigure)
    # are not ours to unwind.
    state = WizardState(
        hosting=HostingOption.LOCAL,
        extras={"web_search": ["web_search_tavily"], "fetch_url": []},
    )
    store_tier_choice(state, SetupTier.QUICKSTART)
    assert state.extras["web_search"] == ["web_search_tavily"]
    assert state.extras["fetch_url"] == []
    store_tier_choice(state, SetupTier.FULL)
    assert state.extras["web_search"] == ["web_search_tavily"]
    assert state.extras["fetch_url"] == []


def test_tier_step_skipped_when_flag_locked():
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.tier import make_setup_tier_step

    step = make_setup_tier_step()
    assert step.applies(WizardState()) is True
    assert step.applies(WizardState(tier_locked=True)) is False


def test_tier_initial_quickstart_fresh_full_on_reconfigure():
    from nymeria.onboarding import SetupTier
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.tier import initial_tier

    # Fresh install: Quickstart is the recommendation.
    assert initial_tier(WizardState()) is SetupTier.QUICKSTART
    # Reconfigure: Full, so Enter-through never quick-seeds capabilities the
    # user deliberately left unconfigured (absences hydrate as nothing).
    assert initial_tier(WizardState(reconfigure=True)) is SetupTier.FULL
    # A tier already picked this run wins either way.
    state = WizardState(reconfigure=True, extras={"tier": "quickstart"})
    assert initial_tier(state) is SetupTier.QUICKSTART


def test_searxng_seed_fires_on_fresh_write_despite_stale_presence(tmp_path):
    from nymeria.setup.finalize import finalize
    from nymeria.setup.state import WizardState

    # A fresh write (merge=False, e.g. a hosting-shape switch where the merge
    # fell back) carries nothing over from the old file, so presence recorded
    # from it must not suppress the turnkey SearXNG seeding.
    root = tmp_path / "checkout"
    root.mkdir()
    state = WizardState(
        hosting=HostingOption.DOCKER,
        provider="anthropic",
        api_key="sk-ant-x",
        model="m",
        skip_llm_test=True,
        root=root,
        extras={"web_search": ["web_search_searxng"]},
        present_env_keys={"SEARXNG_BASE_URL", "SEARXNG_SECRET"},
    )
    console, _ = _capture_console()
    assert finalize(state, console=console, non_interactive=True,
                    overwrite_confirmed=True, merge=False) == 0
    content = (root / ".env.docker").read_text(encoding="utf-8")
    assert _env_line(content, "SEARXNG_BASE_URL") == "http://searxng:8080"
    secret = _env_line(content, "SEARXNG_SECRET")
    assert secret and secret != "nymeria-searxng-internal-change-me"


def test_capability_summary_recognizes_keyless_ddgs():
    from nymeria.setup.finalize import print_capability_summary

    console, output = _capture_console()
    print_capability_summary(None, {}, console, keyless_search_selected=True)
    assert "add a search backend key" not in output.getvalue()

    console, output = _capture_console()
    print_capability_summary(None, {}, console, keyless_search_selected=False)
    assert "add a search backend key" in output.getvalue()


def test_quick_hosting_defaults_reseed_on_hosting_change():
    from nymeria.setup.quick import apply_quick_hosting_defaults
    from nymeria.setup.state import WizardState

    state = WizardState(quick=True, hosting=HostingOption.LOCAL)
    apply_quick_hosting_defaults(state)
    assert state.extras["web_search"] == ["web_search_ddgs"]
    assert state.extras["tts"] == "kokoro"
    assert state.extras["stt"] == "faster-whisper"

    # Going back and picking Docker re-resolves: the sidecar-backed SearXNG
    # replaces ddgs, and the in-process voice seeds are retired (the slim
    # image has no voice engines).
    state.hosting = HostingOption.DOCKER
    apply_quick_hosting_defaults(state)
    assert state.extras["web_search"] == ["web_search_searxng"]
    assert "tts" not in state.extras
    assert "stt" not in state.extras

    # And back to local restores the in-process pair.
    state.hosting = HostingOption.LOCAL
    apply_quick_hosting_defaults(state)
    assert state.extras["web_search"] == ["web_search_ddgs"]
    assert state.extras["tts"] == "kokoro"


def test_quick_hosting_defaults_respect_user_picks_and_disk():
    from nymeria.setup.quick import apply_quick_hosting_defaults
    from nymeria.setup.state import WizardState

    # A flag pick is never replaced, even across hosting changes.
    state = WizardState(
        quick=True,
        hosting=HostingOption.LOCAL,
        extras={"web_search": ["web_search_tavily"]},
    )
    apply_quick_hosting_defaults(state)
    assert state.extras["web_search"] == ["web_search_tavily"]
    state.hosting = HostingOption.DOCKER
    apply_quick_hosting_defaults(state)
    assert state.extras["web_search"] == ["web_search_tavily"]

    # An on-disk voice provider (reconfigure) is never overridden; the other
    # slot still seeds.
    state = WizardState(
        quick=True,
        hosting=HostingOption.LOCAL,
        extras={"tts_on_disk": "openai"},
    )
    apply_quick_hosting_defaults(state)
    assert "tts" not in state.extras
    assert state.extras["stt"] == "faster-whisper"

    # Outside quick mode the function is a no-op.
    state = WizardState(hosting=HostingOption.LOCAL)
    apply_quick_hosting_defaults(state)
    assert "web_search" not in state.extras


def test_quick_defaults_respect_explicit_empty_family():
    from nymeria.setup.quick import apply_quick_defaults
    from nymeria.setup.state import WizardState

    # An explicit "no fetch tools" pick (`--fetch-url none` -> empty list) must
    # survive quick mode: the fill check is key-presence, not truthiness.
    state = WizardState(quick=True, extras={"fetch_url": []})
    apply_quick_defaults(state)
    assert state.extras["fetch_url"] == []


def test_default_step_ids_matches_tui_free_list():
    from nymeria.setup.quick import DEFAULT_STEP_IDS
    from nymeria.setup.steps import default_step_ids

    # quick.DEFAULT_STEP_IDS is the TUI-free copy the non-interactive path
    # validates section jumps against; it must mirror the real step list.
    assert list(DEFAULT_STEP_IDS) == default_step_ids()


def test_steps_cliproxy_reexports_pure_helpers():
    from nymeria.setup import cliproxy_login
    from nymeria.setup.steps import cliproxy as cliproxy_steps

    # The step module re-imports the moved TUI-free helpers, not copies.
    assert cliproxy_steps.management_credentials is cliproxy_login.management_credentials
    assert cliproxy_steps.make_management_client is cliproxy_login.make_management_client
    assert cliproxy_steps.ensure_gatekeeper_key is cliproxy_login.ensure_gatekeeper_key


def test_cliproxy_login_module_is_tui_free():
    from nymeria.setup import cliproxy_login

    # The headless path imports this module; it must never pull in Textual.
    assert "textual" not in Path(cliproxy_login.__file__).read_text()
