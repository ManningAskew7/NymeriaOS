"""Reconfigure mode (--non-interactive): RAG inverse-mappers, hydration, section filtering, merge-write.

Split out of the former monolithic test_setup_wizard.py (dev-todo #54).
"""


from __future__ import annotations

import pytest
from nymeria.onboarding import HostingOption
from nymeria.setup import finalize as finalize_mod
from nymeria.setup.providers import LLMConnectionError
from nymeria.setup.runner import main as setup_main

from _setup_wizard_helpers import (  # type: ignore[import-not-found]
    _capture_console,
    _env_line,
    _first_run,
    _read_secrets_key,
    _stub_llm,
)


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

    # The user swaps the extra pick back to the stock default (ddgs). BOTH
    # stale carriers must be REMOVED, or a later fresh volume (down -v &&
    # up -d with the same .env.docker) would re-seed the reverted pick: the
    # picks now equal what the container seeds on its own
    # (fresh_default_thread_tool_names / DEFAULT_GLOBAL_SKILLS), so there is
    # no difference to carry (see docker_init_seed_env). NB an explicit
    # EMPTY family ([] / `none`) is a deviation and keeps the carrier; that
    # case is pinned separately below.
    state = WizardState(root=root)
    assert hydrate_state_from_disk(state) is True
    assert state.extras.get("web_search") == ["web_search_tavily"]
    state.extras["web_search"] = ["web_search_ddgs"]
    console, _ = _capture_console()
    assert finalize(state, console=console, non_interactive=False,
                    overwrite_confirmed=True, merge=True) == 0
    after = (root / ".env.docker").read_text(encoding="utf-8")
    assert "NYMERIA_INIT_DEFAULT_THREAD_TOOLS" not in after
    assert "NYMERIA_INIT_ENABLED_GLOBAL_SKILLS" not in after


# --- scripted reconfigure (--non-interactive against an existing install) ----


def test_noninteractive_reconfigure_merges_without_force(monkeypatch, tmp_path, capsys):
    root = tmp_path / "init"
    _first_run(monkeypatch, root)
    config = root / "config.env"
    config.write_text(
        config.read_text(encoding="utf-8") + "MY_CUSTOM=1\n", encoding="utf-8"
    )
    capsys.readouterr()

    # No --force needed: the run hydrates from disk and merge-writes only the
    # flags given, exactly like an interactive reconfigure.
    rc = setup_main(
        ["--model", "claude-new-model", "--root", str(root),
         "--non-interactive", "--skip-llm-test"]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "Reconfiguring" in out
    content = config.read_text(encoding="utf-8")
    assert _env_line(content, "LLM_MODEL") == "claude-new-model"
    # Untouched lines survive: the hand-added one and the original key.
    assert _env_line(content, "MY_CUSTOM") == "1"
    assert _env_line(content, "ANTHROPIC_DIRECT_API_KEY") == "sk-ant-x"


def test_noninteractive_reconfigure_keeps_key_and_skips_pre_write_test(
    monkeypatch, tmp_path, capsys
):
    root = tmp_path / "init"
    _first_run(monkeypatch, root)
    capsys.readouterr()

    # A fresh stub for the second run: no --api-key flag means "keep the key
    # on disk", which must also skip the pre-write provider test (there is no
    # key value in hand to test with).
    calls = _stub_llm(monkeypatch)
    rc = setup_main(
        ["--model", "claude-new-model", "--root", str(root), "--non-interactive"]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "Skipping LLM connection test" in out
    assert calls == []
    content = (root / "config.env").read_text(encoding="utf-8")
    assert _env_line(content, "ANTHROPIC_DIRECT_API_KEY") == "sk-ant-x"


def test_noninteractive_reconfigure_does_not_reprint_bootstrap_token(
    monkeypatch, tmp_path, capsys
):
    root = tmp_path / "init"
    _first_run(monkeypatch, root)
    assert "Bootstrap token" in capsys.readouterr().out

    rc = setup_main(
        ["--model", "claude-new-model", "--root", str(root),
         "--non-interactive", "--skip-llm-test"]
    )
    out = capsys.readouterr().out
    assert rc == 0
    # The one-time token handoff belongs to the first run only.
    assert "Bootstrap token" not in out


def test_noninteractive_force_skips_hydrate_and_overwrites(monkeypatch, tmp_path):
    root = tmp_path / "init"
    _first_run(monkeypatch, root)
    config = root / "config.env"
    config.write_text(
        config.read_text(encoding="utf-8") + "MY_CUSTOM=1\n", encoding="utf-8"
    )

    # --force keeps its destructive meaning: no hydrate, so the on-disk key
    # does not satisfy the requirement...
    with pytest.raises(SystemExit) as exc:
        setup_main(
            ["--provider", "anthropic", "--model", "m", "--root", str(root),
             "--non-interactive", "--skip-llm-test", "--force"]
        )
    assert "--api-key" in str(exc.value)

    # ...and a complete flag set rewrites the file from scratch.
    rc = setup_main(
        ["--provider", "anthropic", "--model", "m", "--api-key", "sk-ant-2",
         "--root", str(root), "--non-interactive", "--skip-llm-test", "--force"]
    )
    assert rc == 0
    content = config.read_text(encoding="utf-8")
    assert "MY_CUSTOM" not in content
    assert _env_line(content, "ANTHROPIC_DIRECT_API_KEY") == "sk-ant-2"


def test_noninteractive_section_jump_updates_scoped(monkeypatch, tmp_path, capsys):
    root = tmp_path / "init"
    _first_run(monkeypatch, root)
    capsys.readouterr()

    rc = setup_main(
        ["model", "--model", "claude-scoped-model", "--root", str(root),
         "--non-interactive", "--skip-llm-test"]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "Updated the model settings" in out
    # The scoped outcome is concise: no token handoff, no capability summary.
    assert "Bootstrap token" not in out
    assert "Capabilities" not in out
    content = (root / "config.env").read_text(encoding="utf-8")
    assert _env_line(content, "LLM_MODEL") == "claude-scoped-model"


def test_noninteractive_section_rejects_fresh_install(monkeypatch, tmp_path):
    _stub_llm(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        setup_main(
            ["model", "--provider", "anthropic", "--model", "m",
             "--api-key", "sk-ant-x", "--root", str(tmp_path / "fresh"),
             "--non-interactive", "--skip-llm-test"]
        )
    assert "existing install" in str(exc.value)


def test_noninteractive_rejects_unknown_section(tmp_path):
    with pytest.raises(SystemExit) as exc:
        setup_main(
            ["bogus", "--provider", "anthropic", "--model", "m",
             "--api-key", "k", "--root", str(tmp_path / "a"),
             "--non-interactive", "--skip-llm-test"]
        )
    assert "Unknown section 'bogus'" in str(exc.value)
    assert "model" in str(exc.value)


def test_family_flag_none_sentinel():
    from nymeria.setup.runner import _build_state, build_parser

    base = ["--provider", "anthropic", "--model", "m", "--api-key", "k"]

    state = _build_state(build_parser().parse_args(base + ["--web-search", "none"]))
    assert state.extras["web_search"] == []

    # The sentinel cannot be combined with real picks for the same flag.
    with pytest.raises(SystemExit) as exc:
        _build_state(
            build_parser().parse_args(
                base + ["--web-search", "none", "--web-search", "web_search_tavily"]
            )
        )
    assert "--web-search none" in str(exc.value)

    # Other families are independent.
    state = _build_state(
        build_parser().parse_args(
            base + ["--skill-kit", "none", "--web-search", "web_search_tavily"]
        )
    )
    assert state.extras["skill_kits"] == []
    assert state.extras["web_search"] == ["web_search_tavily"]


def test_noninteractive_web_search_none_keeps_docker_carrier(monkeypatch, tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    _first_run(
        monkeypatch, root, "--hosting", "docker",
        "--web-search", "web_search_tavily", "--tavily-api-key", "k",
    )
    assert "NYMERIA_INIT_DEFAULT_THREAD_TOOLS" in (
        (root / ".env.docker").read_text(encoding="utf-8")
    )

    # `none` is the scripted way to deselect the family, and since the
    # container's own no-carrier seeding includes web_search_ddgs
    # (2026-08-30), a deliberate no-search install is a DEVIATION the
    # carrier must keep carrying, or a fresh volume would resurrect search.
    rc = setup_main(
        ["--web-search", "none", "--root", str(root),
         "--non-interactive", "--skip-llm-test"]
    )
    assert rc == 0
    after = (root / ".env.docker").read_text(encoding="utf-8")
    carrier = _env_line(after, "NYMERIA_INIT_DEFAULT_THREAD_TOOLS")
    assert carrier is not None
    assert "web_search_" not in carrier
    assert "fetch_url_nymeria" in carrier


def test_noninteractive_quick_seeds_fetch_default_and_local_rag(monkeypatch, tmp_path):
    import json

    _stub_llm(monkeypatch)
    root = tmp_path / "init"
    rc = setup_main(
        ["--provider", "anthropic", "--model", "m", "--api-key", "sk-ant-x",
         "--quick", "--root", str(root), "--non-interactive", "--skip-llm-test"]
    )
    assert rc == 0
    content = (root / "config.env").read_text(encoding="utf-8")
    assert _env_line(content, "EMBEDDING_PROVIDER") == "local"
    profile = json.loads(
        (root / "data" / "users" / "default" / "profile.json").read_text("utf-8")
    )
    assert "fetch_url_nymeria" in profile["tool_preferences"]["default_thread_tools"]


def test_noninteractive_quick_docker_seeds_slim_without_carriers(monkeypatch, tmp_path):
    _stub_llm(monkeypatch)
    root = tmp_path / "checkout"
    root.mkdir()
    rc = setup_main(
        ["--provider", "anthropic", "--model", "m", "--api-key", "sk-ant-x",
         "--quick", "--hosting", "docker", "--root", str(root),
         "--non-interactive", "--skip-llm-test"]
    )
    assert rc == 0
    content = (root / ".env.docker").read_text(encoding="utf-8")
    # Quick defaults the Docker shape to slim: no full-stack passwords minted.
    assert "POSTGRES_PASSWORD" not in content
    # Quick's defaults (ddgs + nymeria fetch) equal the container's own
    # no-carrier seeding since 2026-08-30, so NO init-seed carriers ride
    # along, and no SearXNG sidecar plumbing is generated by default.
    assert _env_line(content, "NYMERIA_INIT_DEFAULT_THREAD_TOOLS") is None
    assert _env_line(content, "NYMERIA_INIT_ENABLED_GLOBAL_SKILLS") is None
    assert _env_line(content, "SEARXNG_BASE_URL") is None


def test_noninteractive_quick_local_seeds_keyless_search_and_voice(
    monkeypatch, tmp_path
):
    import json

    from nymeria.setup import family_catalog

    _stub_llm(monkeypatch)
    root = tmp_path / "init"
    # No --hosting: headless quick resolves to local, so the hosting-dependent
    # seeds still apply.
    rc = setup_main(
        ["--provider", "anthropic", "--model", "m", "--api-key", "sk-ant-x",
         "--quick", "--root", str(root), "--non-interactive", "--skip-llm-test"]
    )
    assert rc == 0
    content = (root / "config.env").read_text(encoding="utf-8")
    # Free local voice pair written; the install hint covers the extra.
    assert _env_line(content, "TTS_PROVIDER") == "kokoro"
    assert _env_line(content, "STT_PROVIDER") == "faster-whisper"
    # No SearXNG plumbing on a bare-metal install.
    assert _env_line(content, "SEARXNG_BASE_URL") is None
    profile = json.loads(
        (root / "data" / "users" / "default" / "profile.json").read_text("utf-8")
    )
    tools = profile["tool_preferences"]["default_thread_tools"]
    # ddgs (in-process keyless) is the bare-metal search default, not SearXNG.
    assert "web_search_ddgs" in tools
    assert "web_search_searxng" not in tools
    # Every bundled kit on, behind the always-on guidance skill.
    assert profile["enabled_global_skills"] == [
        "self-improve",
        "nymeria-resources",
        *family_catalog.default_checked_skill_kits(),
    ]


def test_noninteractive_docker_searxng_pick_provisions_sidecar(
    monkeypatch, tmp_path
):
    _stub_llm(monkeypatch)
    root = tmp_path / "checkout"
    root.mkdir()
    # SearXNG is no longer any shape's default (2026-08-30), but an explicit
    # pick must still wire the sidecar turnkey exactly as before.
    rc = setup_main(
        ["--provider", "anthropic", "--model", "m", "--api-key", "sk-ant-x",
         "--quick", "--hosting", "docker", "--web-search", "web_search_searxng",
         "--root", str(root), "--non-interactive", "--skip-llm-test"]
    )
    assert rc == 0
    content = (root / ".env.docker").read_text(encoding="utf-8")
    carrier = _env_line(content, "NYMERIA_INIT_DEFAULT_THREAD_TOOLS")
    assert carrier is not None and "web_search_searxng" in carrier
    assert "web_search_ddgs" not in carrier
    assert _env_line(content, "SEARXNG_BASE_URL") == "http://searxng:8080"
    secret = _env_line(content, "SEARXNG_SECRET")
    assert secret and len(secret) >= 32
    assert secret != "nymeria-searxng-internal-change-me"
    # Voice is NOT seeded on slim Docker (the image has no voice engines).
    assert _env_line(content, "TTS_PROVIDER") is None
    assert _env_line(content, "STT_PROVIDER") is None

    # A reconfigure keeps the generated secret (present_env_keys guards it
    # from rotating) and the custom-value path from clobbering.
    rc = setup_main(
        ["--model", "claude-new", "--root", str(root),
         "--non-interactive", "--skip-llm-test"]
    )
    assert rc == 0
    after = (root / ".env.docker").read_text(encoding="utf-8")
    assert _env_line(after, "SEARXNG_SECRET") == secret
    assert _env_line(after, "SEARXNG_BASE_URL") == "http://searxng:8080"


def test_docker_stack_spec_searxng_pick_adds_search_profile():
    from nymeria.onboarding import DockerStack, HostingOption
    from nymeria.setup.state import WizardState

    # Slim source checkout, default port: a SearXNG pick adds the profile AND
    # forces --env-file (the generated SEARXNG_SECRET only reaches the sidecar
    # via interpolation).
    state = WizardState(
        hosting=HostingOption.DOCKER,
        extras={"web_search": ["web_search_searxng"]},
    )
    spec = finalize_mod._docker_stack_spec(state)
    assert ("--profile", "search") == spec.compose_args[-2:]
    assert "--env-file" in spec.compose_args

    # No SearXNG pick: no profile, and the default-port slim command stays the
    # documented short form.
    plain = finalize_mod._docker_stack_spec(WizardState(hosting=HostingOption.DOCKER))
    assert "--profile" not in plain.compose_args

    # Full stack carries the profile too (its sidecar predates this wiring).
    full = WizardState(
        hosting=HostingOption.DOCKER,
        docker_stack=DockerStack.FULL,
        extras={"web_search": ["web_search_searxng"]},
    )
    full_spec = finalize_mod._docker_stack_spec(full)
    assert ("--profile", "search") == full_spec.compose_args[-2:]


def test_noninteractive_quick_still_requires_llm_flags(tmp_path):
    with pytest.raises(SystemExit) as exc:
        setup_main(
            ["--quick", "--root", str(tmp_path / "a"),
             "--non-interactive", "--skip-llm-test"]
        )
    assert "--provider" in str(exc.value)


def test_noninteractive_quick_respects_explicit_fetch_none(monkeypatch, tmp_path):
    import json

    _stub_llm(monkeypatch)
    root = tmp_path / "init"
    rc = setup_main(
        ["--provider", "anthropic", "--model", "m", "--api-key", "sk-ant-x",
         "--quick", "--fetch-url", "none", "--root", str(root),
         "--non-interactive", "--skip-llm-test"]
    )
    assert rc == 0
    profile = json.loads(
        (root / "data" / "users" / "default" / "profile.json").read_text("utf-8")
    )
    assert "fetch_url_nymeria" not in profile["tool_preferences"]["default_thread_tools"]


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


def test_chat_apps_hint_gives_a_start_command_that_works_on_this_shape(tmp_path):
    """#101 entry 11b: the closing output never mentioned chat-app bots. Each
    shape gets the command that actually starts one there."""
    from nymeria.onboarding import DockerStack
    from nymeria.setup.finalize import print_chat_apps_hint
    from nymeria.setup.state import WizardState

    native_config = tmp_path / "config.env"
    for hosting in (HostingOption.LOCAL, HostingOption.SERVICE, None):
        console, buf = _capture_console()
        print_chat_apps_hint(WizardState(hosting=hosting), console, config_path=native_config)
        out = " ".join(buf.getvalue().split())
        assert "nymeria telegram-bot" in out
        assert str(native_config) in out and "TELEGRAM_BOT_TOKEN" in out
        assert "docs/chat-apps" in out
        assert "--profile" not in out

    console, buf = _capture_console()
    print_chat_apps_hint(
        WizardState(hosting=HostingOption.DOCKER, docker_stack=DockerStack.FULL, api_port=8010),
        console,
        config_path=tmp_path / ".env.docker",
    )
    out = " ".join(buf.getvalue().split())
    assert "API_PORT=8010 docker compose" in out and "--profile telegram up -d" in out
    assert "TELEGRAM_BOT_TOKEN" in out
    assert "nymeria telegram-bot" not in out

    # #101 entry 15 of 2026-08-23: the single-container image now carries the
    # Discord, Telegram, and Slack SDKs and its compose runs them as profiles.
    # The bot's token reaches it only through compose interpolation, so the
    # command always carries --env-file, even on the default-port short form.
    console, buf = _capture_console()
    print_chat_apps_hint(
        WizardState(hosting=HostingOption.DOCKER, docker_stack=DockerStack.SLIM),
        console,
        config_path=tmp_path / ".env.docker",
    )
    out = " ".join(buf.getvalue().split())
    assert (
        "docker compose -f docker-compose.single.yml --env-file .env.docker "
        "--profile telegram up -d"
    ) in out
    assert "TELEGRAM_BOT_TOKEN" in out and str(tmp_path / ".env.docker") in out
    assert "discord, slack" in out
    assert "A Twitch bot needs the full Docker stack or a native install" in out
    assert "--build" not in out  # the suite's probe stub answers "no image"
    assert "does not run chat-app bots" not in out
    assert "nymeria telegram-bot" not in out

    # A non-default port keeps its prefix and does not double the flag.
    console, buf = _capture_console()
    print_chat_apps_hint(
        WizardState(hosting=HostingOption.DOCKER, docker_stack=DockerStack.SLIM, api_port=8020),
        console,
        config_path=tmp_path / ".env.docker",
    )
    out = " ".join(buf.getvalue().split())
    assert (
        "API_PORT=8020 docker compose -f docker-compose.single.yml --env-file "
        ".env.docker --profile telegram up -d"
    ) in out
    assert out.count("--env-file") == 1


def test_single_shape_bot_command_rebuilds_an_image_built_before_the_sdks(monkeypatch, tmp_path):
    # `up -d` builds only a MISSING image: an image from before the bot SDKs
    # would start a bot that crash-loops on "support is not installed".
    from nymeria.onboarding import DockerStack
    from nymeria.setup import finalize as finalize_mod
    from nymeria.setup.state import WizardState

    state = WizardState(hosting=HostingOption.DOCKER, docker_stack=DockerStack.SLIM)
    for answer, rebuilds in ((False, True), (True, False), (None, False)):
        monkeypatch.setattr(finalize_mod, "_single_image_has_bots", lambda a=answer: a)
        console, buf = _capture_console()
        finalize_mod.print_chat_apps_hint(state, console, config_path=tmp_path / ".env.docker")
        out = " ".join(buf.getvalue().split())
        assert ("--profile telegram up -d --build" in out) is rebuilds, answer
        assert ("predates the bot libraries" in out) is rebuilds, answer
        assert "--profile telegram up -d" in out


def test_clone_free_bot_command_names_its_root_and_never_builds(monkeypatch, tmp_path):
    # The published compose and .env.docker live in the runtime root, not a
    # checkout the user stands in; and there is nothing to build there.
    from nymeria.onboarding import DockerStack
    from nymeria.setup import environment as env_mod
    from nymeria.setup import finalize as finalize_mod
    from nymeria.setup.state import WizardState

    monkeypatch.setattr(env_mod, "source_checkout_root", lambda *a, **k: None)
    monkeypatch.setattr(finalize_mod, "_single_image_has_bots", lambda: False)
    state = WizardState(hosting=HostingOption.DOCKER, docker_stack=DockerStack.SLIM)
    root = finalize_mod.resolve_runtime_root(state, for_docker=True)
    console, buf = _capture_console()
    finalize_mod.print_chat_apps_hint(state, console, config_path=root / ".env.docker")
    out = " ".join(buf.getvalue().split())
    assert f"start the bot from {root}:" in out
    assert (
        "docker compose -f docker-compose.single.published.yml --env-file .env.docker "
        "--profile telegram up -d"
    ) in out
    assert "--build" not in out
    assert out.count("--env-file") == 1


def test_a_searxng_pick_keeps_its_profile_on_the_bot_command(tmp_path):
    from nymeria.onboarding import DockerStack
    from nymeria.setup.finalize import print_chat_apps_hint
    from nymeria.setup.state import WizardState

    state = WizardState(hosting=HostingOption.DOCKER, docker_stack=DockerStack.SLIM)
    state.extras["web_search"] = ["web_search_searxng"]
    console, buf = _capture_console()
    print_chat_apps_hint(state, console, config_path=tmp_path / ".env.docker")
    out = " ".join(buf.getvalue().split())
    assert "--env-file .env.docker --profile search --profile telegram up -d" in out
    assert out.count("--env-file") == 1


def test_a_finished_run_points_at_chat_apps(monkeypatch, tmp_path, capsys):
    root = tmp_path / "init"
    _first_run(monkeypatch, root, "--hosting", "local")
    out = " ".join(capsys.readouterr().out.split())
    # Root-pinned: the tmp root is not what a bare command resolves (#101 entry 6).
    assert "Chat apps" in out and f"nymeria --root {root} telegram-bot" in out
    # Part of the CLOSING instructions (review): after the start command and
    # the doctor line, not above them where a chatty doctor scrolls it away.
    assert out.index("Chat apps") > out.index("Start Nymeria with")
    assert out.index("Chat apps") > out.rindex(f"nymeria --root {root} doctor")


def test_chat_apps_block_closes_a_start_now_but_precedes_a_foreground_one(
    monkeypatch, tmp_path
):
    from nymeria.onboarding import NextAction
    from nymeria.setup.state import WizardState

    config_path = tmp_path / "config.env"

    # Foreground local start: the server's own output would bury the block,
    # so it prints BEFORE the start.
    console, buf = _capture_console()
    seen: dict[str, bool] = {}

    def fake_local(console, **_kwargs):
        seen["printed_before_start"] = "Chat apps" in buf.getvalue()
        return 0

    monkeypatch.setattr(finalize_mod, "_start_now_local", fake_local)
    state = WizardState(
        hosting=HostingOption.LOCAL, next_action=NextAction.START_API_OPEN_FRONTEND
    )
    finalize_mod.run_next_action(state, console, root=tmp_path, config_path=config_path)
    assert seen == {"printed_before_start": True}

    # Detached Docker start: the block comes after the start output.
    console, buf = _capture_console()
    monkeypatch.setattr(
        finalize_mod,
        "_start_now_docker",
        lambda console, **_kwargs: console.print("STACK STARTED") or 0,
    )
    state = WizardState(
        hosting=HostingOption.DOCKER, next_action=NextAction.START_API_OPEN_FRONTEND
    )
    finalize_mod.run_next_action(state, console, root=tmp_path, config_path=config_path)
    out = buf.getvalue()
    assert out.index("STACK STARTED") < out.index("Chat apps")


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


def test_hydrate_infers_local_model_branch_from_ollama_provider(tmp_path):
    """An on-disk Ollama config renders as the local-model branch (like the
    CLIProxy base-URL inference: auth_method is never written to disk)."""
    from nymeria.onboarding import ProviderAuthMethod
    from nymeria.setup.hydrate import hydrate_state_from_disk
    from nymeria.setup.state import WizardState

    root = tmp_path / "ollama"
    root.mkdir()
    (root / ".env.docker").write_text(
        "LLM_PROVIDER=ollama\nLLM_MODEL=qwen3:8b\n", encoding="utf-8"
    )
    state = WizardState(root=root)
    assert hydrate_state_from_disk(state) is True
    assert state.auth_method is ProviderAuthMethod.LOCAL_MODEL

    # Any other provider stays on the API-key branch.
    other = tmp_path / "anthropic"
    other.mkdir()
    (other / ".env.docker").write_text("LLM_PROVIDER=anthropic\n", encoding="utf-8")
    ostate = WizardState(root=other)
    assert hydrate_state_from_disk(ostate) is True
    assert ostate.auth_method is ProviderAuthMethod.API_KEY

    # An explicit --auth-method flag wins over the inference.
    explicit = WizardState(
        root=root,
        auth_method=ProviderAuthMethod.API_KEY,
        auth_method_explicit=True,
    )
    assert hydrate_state_from_disk(explicit) is True
    assert explicit.auth_method is ProviderAuthMethod.API_KEY


# --- #101 multi-root (entries 6, 10, 15a): one host, several installs ----------


def _checkout_with_docker_config(monkeypatch, tmp_path):
    """A source checkout that also drives a Docker install (its .env.docker)."""
    from nymeria.setup import environment as environment_mod

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".env.docker").write_text(
        "LLM_PROVIDER=openai\nLLM_MODEL=docker-model\n"
        "LLM_BASE_URL=http://cli-proxy-api-latest:8317/v1\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(environment_mod, "source_checkout_root", lambda: checkout)
    return checkout


def _launched_with_root(monkeypatch, root):
    """This process was LAUNCHED with NYMERIA_PROJECT_ROOT (None: without it)."""
    from nymeria import _runtime_paths

    monkeypatch.setattr(
        _runtime_paths, "_LAUNCH_PROJECT_ROOT_ENV", None if root is None else str(root)
    )
    if root is not None:
        monkeypatch.setenv("NYMERIA_PROJECT_ROOT", str(root))


def test_an_explicit_root_never_hydrates_from_the_checkouts_docker_config(
    monkeypatch, tmp_path
):
    # Entry 10: an exported root for a NEW install opened as "Reconfiguring the
    # existing install at Docker", carrying the Docker install's values over.
    from nymeria.setup.hydrate import hydrate_state_from_disk
    from nymeria.setup.state import WizardState

    _checkout_with_docker_config(monkeypatch, tmp_path)
    own = tmp_path / "own"
    own.mkdir()
    _launched_with_root(monkeypatch, own)

    state = WizardState()
    assert hydrate_state_from_disk(state) is False
    assert not state.model and state.hosting is None and not state.reconfigure

    # ...and its own config is what a reconfigure there reads.
    (own / "config.env").write_text(
        "LLM_PROVIDER=anthropic\nLLM_MODEL=own-model\n", encoding="utf-8"
    )
    state = WizardState()
    assert hydrate_state_from_disk(state) is True
    assert state.model == "own-model"
    assert state.hosting is not HostingOption.DOCKER


@pytest.mark.parametrize("launched_with", ["checkout", None])
def test_the_checkout_root_still_finds_its_docker_config(monkeypatch, tmp_path, launched_with):
    # An explicit root that IS the checkout, or no explicit root at all, keeps
    # the checkout's Docker config as the install being reconfigured.
    from nymeria.setup.hydrate import hydrate_state_from_disk
    from nymeria.setup.state import WizardState

    checkout = _checkout_with_docker_config(monkeypatch, tmp_path)
    _launched_with_root(monkeypatch, checkout if launched_with else None)

    state = WizardState()
    assert hydrate_state_from_disk(state) is True
    assert state.hosting is HostingOption.DOCKER
    assert state.model == "docker-model"


@pytest.mark.parametrize(
    "setup,source",
    [
        ("explicit", "NYMERIA_PROJECT_ROOT"),
        ("checkout", "source checkout"),
        ("flag", "--root"),
    ],
)
def test_the_reconfigure_line_names_the_file_and_where_the_root_came_from(
    monkeypatch, tmp_path, setup, source
):
    # Entry 15a: a lost export silently retargeted init at another install;
    # naming the file and the root's origin makes that visible at once.
    from nymeria.setup.hydrate import hydrate_state_from_disk
    from nymeria.setup.state import WizardState

    checkout = _checkout_with_docker_config(monkeypatch, tmp_path)
    own = tmp_path / "own"
    own.mkdir()
    (own / "config.env").write_text("LLM_PROVIDER=anthropic\n", encoding="utf-8")
    if setup == "explicit":
        _launched_with_root(monkeypatch, own)
        state, expected = WizardState(), own / "config.env"
    elif setup == "checkout":
        _launched_with_root(monkeypatch, None)
        state, expected = WizardState(), checkout / ".env.docker"
    else:
        _launched_with_root(monkeypatch, None)
        state, expected = WizardState(root=own), own / "config.env"
    console, buf = _capture_console()

    assert hydrate_state_from_disk(state, console=console) is True

    out = " ".join(buf.getvalue().split())
    assert str(expected) in out
    assert source in out


def test_a_fresh_install_says_which_root_it_will_use(monkeypatch, tmp_path, capsys):
    root = tmp_path / "fresh"
    _first_run(monkeypatch, root)

    out = " ".join(capsys.readouterr().out.split())
    assert f"New install at {root}" in out
    assert "--root" in out


def test_writing_a_local_config_beside_a_docker_one_warns(monkeypatch, tmp_path, capsys):
    # Entry 6: every process started from this root also loads .env.docker,
    # AFTER the local file, so the Docker install's values win and fill gaps.
    root = tmp_path / "shared"
    root.mkdir()
    (root / ".env.docker").write_text("LLM_MODEL=docker-model\n", encoding="utf-8")

    _first_run(monkeypatch, root, "--hosting", "local", "--force")

    out = " ".join(capsys.readouterr().out.split())
    assert (root / "config.env").exists() or (root / ".env").exists()
    assert "also holds .env.docker" in out
    assert "NYMERIA_PROJECT_ROOT" in out


@pytest.mark.parametrize("docker_config,hosting", [(False, "local"), (True, "docker")])
def test_no_shared_root_warning_otherwise(monkeypatch, tmp_path, capsys, docker_config, hosting):
    root = tmp_path / "root"
    root.mkdir()
    if docker_config:
        (root / ".env.docker").write_text("LLM_MODEL=docker-model\n", encoding="utf-8")

    _first_run(monkeypatch, root, "--hosting", hosting, "--force")

    assert "also holds .env.docker" not in " ".join(capsys.readouterr().out.split())


def test_docker_hosting_under_an_exported_root_reconfigures_the_docker_install(
    monkeypatch, tmp_path
):
    # The operator's own setup: slim's root exported in the shell profile, the
    # Docker stack at the checkout. A Docker run must hydrate the Docker config
    # it will merge-write, never the exported local install's.
    from nymeria.setup.hydrate import hydrate_state_from_disk
    from nymeria.setup.state import WizardState

    _checkout_with_docker_config(monkeypatch, tmp_path)
    own = tmp_path / "own"
    own.mkdir()
    (own / "config.env").write_text(
        "LLM_PROVIDER=anthropic\nLLM_MODEL=own-model\n", encoding="utf-8"
    )
    _launched_with_root(monkeypatch, own)

    state = WizardState(hosting=HostingOption.DOCKER)
    assert hydrate_state_from_disk(state) is True
    assert state.model == "docker-model"


def test_a_fresh_run_before_the_hosting_pick_names_both_roots(monkeypatch, tmp_path):
    from nymeria.setup.hydrate import announce_fresh_install
    from nymeria.setup.state import WizardState

    checkout = _checkout_with_docker_config(monkeypatch, tmp_path)
    own = tmp_path / "own"
    own.mkdir()
    _launched_with_root(monkeypatch, own)

    console, buf = _capture_console()
    announce_fresh_install(WizardState(), console=console)
    out = " ".join(buf.getvalue().split())
    assert f"A local install goes to {own} (root from NYMERIA_PROJECT_ROOT)" in out
    assert f"a Docker install to {checkout} (root from this source checkout)" in out

    # Once the hosting is known, only the root the run writes to.
    console, buf = _capture_console()
    announce_fresh_install(WizardState(hosting=HostingOption.LOCAL), console=console)
    out = " ".join(buf.getvalue().split())
    assert f"New install at {own}" in out
    assert str(checkout) not in out


# --- #101 entry 6: printed commands reach the install they describe ----------


def _closing_output(monkeypatch, capsys, root, *extra):
    _first_run(monkeypatch, root, *extra)
    return " ".join(capsys.readouterr().out.split())


@pytest.mark.parametrize("launched", ["other export", "nothing exported"])
@pytest.mark.parametrize(
    "extra,commands",
    [
        (("--hosting", "local"), ("slim", "cli", "init", "doctor", "telegram-bot")),
        (("--hosting", "local", "--next-action", "cli"), ("cli", "init", "doctor")),
        (
            ("--hosting", "service"),
            ("service status", "service uninstall", "cli", "init", "doctor"),
        ),
    ],
)
def test_closing_commands_name_the_root_a_bare_command_would_miss(
    monkeypatch, tmp_path, capsys, launched, extra, commands
):
    # After `init --root B` the closing `nymeria slim` started the DEFAULT
    # install: a bare command resolves the export or the checkout, not B.
    import shlex

    _launched_with_root(monkeypatch, tmp_path / "default" if launched == "other export" else None)
    root = tmp_path / "my install"  # a space: the root must arrive quoted

    out = _closing_output(monkeypatch, capsys, root, *extra)

    pinned = f"nymeria --root {shlex.quote(str(root))}"
    for command in commands:
        assert f"{pinned} {command}" in out, command
    for command in ("slim", "cli", "init", "doctor"):
        assert f"`nymeria {command}`" not in out
        assert f" nymeria {command} " not in f" {out} ".replace(pinned, "")


def test_closing_commands_stay_bare_when_a_bare_command_finds_the_root(
    monkeypatch, tmp_path, capsys
):
    root = tmp_path / "exported"
    _launched_with_root(monkeypatch, root)

    out = _closing_output(monkeypatch, capsys, root, "--hosting", "local")

    for command in ("nymeria slim", "nymeria cli", "`nymeria init`", "`nymeria doctor`"):
        assert command in out
    assert "nymeria --root" not in out


def test_printed_roots_are_quoted_for_the_shell_they_are_pasted_into():
    posix = finalize_mod._shell_arg("/home/a user/it's", windows=False)
    assert posix == "'/home/a user/it'\"'\"'s'"
    assert finalize_mod._shell_arg(r"C:\Users\A User\nymeria", windows=True) == (
        r'"C:\Users\A User\nymeria"'
    )
    assert finalize_mod._shell_arg(r"C:\plain", windows=True) == r"C:\plain"


def test_the_external_access_retry_names_the_root(monkeypatch, tmp_path):
    from nymeria.onboarding import ExternalAccess
    from nymeria.setup.state import WizardState

    _launched_with_root(monkeypatch, None)
    root = tmp_path / "b"
    state = WizardState(
        hosting=HostingOption.LOCAL, root=root, external_access=ExternalAccess.TAILSCALE
    )
    console, buf = _capture_console()

    finalize_mod.print_external_access_summary(state, console)

    out = " ".join(buf.getvalue().split())
    assert "Not set up in this run" in out
    assert f"`nymeria --root {root} init external_access`" in out


def test_a_port_change_behind_a_tunnel_names_the_external_access_command_for_the_root(
    monkeypatch, tmp_path
):
    from nymeria.onboarding import ExternalAccess
    from nymeria.setup.finalize import finalize
    from nymeria.setup.state import WizardState

    _launched_with_root(monkeypatch, None)
    _stub_llm(monkeypatch)
    monkeypatch.setattr(finalize_mod, "_maybe_install_local_rag", lambda *a, **k: None)
    monkeypatch.setattr(finalize_mod, "_finalize_server_browser_guarded", lambda *a, **k: None)
    root = tmp_path / "b"
    state = WizardState(
        hosting=HostingOption.LOCAL, root=root, provider="anthropic",
        model="claude-test-model", api_key="sk-ant-x", skip_llm_test=True,
        api_port=8297, external_access=ExternalAccess.TAILSCALE,
        public_url="https://box.example.ts.net",
    )
    state.extras["api_port_on_disk"] = 8296
    console, buf = _capture_console()

    finalize(state, console=console, non_interactive=True)

    out = " ".join(buf.getvalue().split())
    assert "still forwards to the old port" in out
    assert f"(`nymeria --root {root} init external_access`)" in out


# --- #101 entry 6: a local config never lands beside .env.docker by accident -


def _shared_root(tmp_path):
    root = tmp_path / "shared"
    root.mkdir()
    (root / ".env.docker").write_text(
        "LLM_MODEL=docker-model\nNYMERIA_SECRETS_KEY=docker-install-key\n", encoding="utf-8"
    )
    return root


@pytest.mark.parametrize("hosting", ["local", "service"])
@pytest.mark.parametrize("landed_by", ["export", "checkout default"])
def test_a_local_config_is_refused_beside_a_docker_one_unless_root_names_it(
    monkeypatch, tmp_path, capsys, hosting, landed_by
):
    import shlex

    from nymeria.setup import environment as environment_mod

    root = _shared_root(tmp_path)
    if landed_by == "export":
        _launched_with_root(monkeypatch, root)
    else:
        # An editable checkout with nothing exported: the checkout is the root.
        monkeypatch.setattr(environment_mod, "source_checkout_root", lambda: root)
        _launched_with_root(monkeypatch, None)
        monkeypatch.setenv("NYMERIA_PROJECT_ROOT", str(root))
    calls = _stub_llm(monkeypatch)
    before = (root / ".env.docker").read_bytes()

    rc = setup_main(
        ["--provider", "anthropic", "--model", "claude-test-model",
         "--api-key", "sk-ant-x", "--hosting", hosting, "--non-interactive"]
    )
    out = " ".join(capsys.readouterr().out.split())

    assert rc == 2
    assert calls == []  # stopped before the LLM test
    assert sorted(p.name for p in root.iterdir()) == [".env.docker"]  # nothing written
    assert (root / ".env.docker").read_bytes() == before
    assert "holds .env.docker" in out and "Nothing was written" in out
    assert "loads .env.docker after the local config" in out
    assert "nymeria --root <dir> init" in out  # remedy 1: its own root
    assert "docker compose down" in out  # remedy 2: retire the Docker config
    assert f"nymeria --root {shlex.quote(str(root))} init" in out  # write here anyway


def test_naming_the_shared_root_with_root_writes_it_with_the_warning(
    monkeypatch, tmp_path, capsys
):
    root = _shared_root(tmp_path)
    _launched_with_root(monkeypatch, None)

    _first_run(monkeypatch, root, "--hosting", "local")
    out = " ".join(capsys.readouterr().out.split())

    assert (root / "config.env").exists() or (root / ".env").exists()
    assert "also holds .env.docker" in out
    assert "Nothing was written" not in out


@pytest.mark.parametrize("answer,written", [("y", True), ("n", False), ("", False), (EOFError, False)])
def test_the_interactive_wizard_asks_before_writing_beside_a_docker_config(
    monkeypatch, tmp_path, answer, written
):
    import builtins

    from nymeria.setup.finalize import finalize
    from nymeria.setup.state import WizardState

    root = _shared_root(tmp_path)
    _launched_with_root(monkeypatch, root)
    _stub_llm(monkeypatch)
    asked: list[str] = []

    def fake_input(*_a):
        asked.append("asked")
        if answer is EOFError:
            raise EOFError
        return answer

    monkeypatch.setattr(builtins, "input", fake_input)
    # The other interactive prompts finalize can reach are not under test here.
    monkeypatch.setattr(finalize_mod, "_maybe_install_local_rag", lambda *a, **k: None)
    monkeypatch.setattr(finalize_mod, "_finalize_server_browser_guarded", lambda *a, **k: None)
    state = WizardState(
        hosting=HostingOption.LOCAL, provider="anthropic", model="claude-test-model",
        api_key="sk-ant-x", skip_llm_test=True,
    )
    console, buf = _capture_console()

    rc = finalize(state, console=console, non_interactive=False, overwrite_confirmed=True)

    out = " ".join(buf.getvalue().split())
    assert asked == ["asked"]
    assert "Write the local config here anyway?" in out
    local_files = [n for n in ("config.env", ".env") if (root / n).exists()]
    if written:
        assert rc == 0 and len(local_files) == 1
        assert "also holds .env.docker" in out
    else:
        assert rc == 2 and local_files == []
        assert "Nothing was written" in out


@pytest.mark.parametrize("hosting", ["local", "service"])
@pytest.mark.parametrize("local_file", ["config.env", ".env"])
def test_a_local_install_already_beside_a_docker_config_reconfigures_without_root(
    monkeypatch, tmp_path, capsys, hosting, local_file
):
    # F2: the guard is for landing beside .env.docker by ACCIDENT. A local
    # install already placed there (the Source-track layout, #477) is a
    # reconfigure: it proceeds, headless with no --root, and still gets the
    # post-write warning. A fresh write there stays refused (the test above).
    root = _shared_root(tmp_path)
    (root / local_file).write_text(
        "LLM_PROVIDER=anthropic\nLLM_MODEL=old-model\n", encoding="utf-8"
    )
    _launched_with_root(monkeypatch, root)
    _stub_llm(monkeypatch)

    rc = setup_main(
        ["--provider", "anthropic", "--model", "claude-test-model",
         "--api-key", "sk-ant-x", "--hosting", hosting, "--non-interactive",
         "--skip-llm-test"]
    )
    out = " ".join(capsys.readouterr().out.split())

    assert rc == 0
    assert "LLM_MODEL=claude-test-model" in (root / local_file).read_text(encoding="utf-8")
    assert "Nothing was written" not in out
    assert "also holds .env.docker" in out  # the post-write warning still fires


def test_the_interactive_wizard_never_asks_before_reconfiguring_an_install_already_there(
    monkeypatch, tmp_path
):
    import builtins

    from nymeria.setup.finalize import finalize
    from nymeria.setup.state import WizardState

    root = _shared_root(tmp_path)
    (root / "config.env").write_text("LLM_MODEL=old-model\n", encoding="utf-8")
    _launched_with_root(monkeypatch, root)
    _stub_llm(monkeypatch)
    monkeypatch.setattr(
        builtins, "input", lambda *_a: pytest.fail("a reconfigure must not be asked about")
    )
    monkeypatch.setattr(finalize_mod, "_maybe_install_local_rag", lambda *a, **k: None)
    monkeypatch.setattr(finalize_mod, "_finalize_server_browser_guarded", lambda *a, **k: None)
    state = WizardState(
        hosting=HostingOption.LOCAL, provider="anthropic", model="claude-test-model",
        api_key="sk-ant-x", skip_llm_test=True,
    )
    console, buf = _capture_console()

    rc = finalize(state, console=console, non_interactive=False, overwrite_confirmed=True)

    out = " ".join(buf.getvalue().split())
    assert rc == 0
    assert "Write the local config here anyway?" not in out
    assert "LLM_MODEL=claude-test-model" in (root / "config.env").read_text(encoding="utf-8")


def test_docker_hosting_at_a_checkout_is_not_refused(monkeypatch, tmp_path):
    # E12: the Docker install's own config lives beside compose; a Docker run
    # with no --root reconfigures it exactly as before.
    checkout = _checkout_with_docker_config(monkeypatch, tmp_path)
    _launched_with_root(monkeypatch, None)
    monkeypatch.setenv("NYMERIA_PROJECT_ROOT", str(checkout))
    _stub_llm(monkeypatch)

    rc = setup_main(
        ["--provider", "anthropic", "--model", "claude-test-model", "--api-key",
         "sk-ant-x", "--hosting", "docker", "--non-interactive", "--skip-llm-test"]
    )

    assert rc == 0
    assert "LLM_MODEL=claude-test-model" in (checkout / ".env.docker").read_text()
    assert not (checkout / "config.env").exists() and not (checkout / ".env").exists()


@pytest.mark.parametrize("case", ["fresh", "named", "placed"])
def test_the_review_screen_names_the_root_and_the_docker_config_beside_it(
    monkeypatch, tmp_path, case
):
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.review import _summary_markup

    root = _shared_root(tmp_path)
    if case == "placed":  # a local install already lives there (F2)
        (root / "config.env").write_text("LLM_MODEL=old-model\n", encoding="utf-8")
    _launched_with_root(monkeypatch, root)
    state = WizardState(hosting=HostingOption.LOCAL, root=root if case == "named" else None)

    markup = " ".join(_summary_markup(state).split())

    assert str(root) in markup
    assert "also holds .env.docker" in markup
    if case == "named":
        assert "it is written anyway" in markup
    elif case == "placed":
        assert "this updates the local install already here" in markup
        assert "finishing asks" not in markup
    else:
        assert "finishing asks before writing here" in markup
        assert "nymeria --root <dir> init" in markup

    # Docker hosting there, or a local root without the file: no heads-up.
    for clean in (
        WizardState(hosting=HostingOption.DOCKER, root=root),
        WizardState(hosting=HostingOption.LOCAL, root=tmp_path / "own"),
    ):
        assert ".env.docker" not in _summary_markup(clean)
