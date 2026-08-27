"""Reconfigure mode (--non-interactive): RAG inverse-mappers, hydration, section filtering, merge-write.

Split out of the former monolithic test_setup_wizard.py (dev-todo #54).
"""


from __future__ import annotations

import pytest
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

    # The user deselects the extra pick (back to backend defaults). BOTH stale
    # carriers must be REMOVED, or a later fresh volume (down -v && up -d
    # with the same .env.docker) would re-seed the reverted pick. The skills
    # carrier retires too since 2026-08-27: the wizard's default-checked kit
    # set now matches the backend fallback exactly, so with no skill picks
    # there is no difference to carry (see docker_init_seed_env).
    state = WizardState(root=root)
    assert hydrate_state_from_disk(state) is True
    assert state.extras.get("web_search") == ["web_search_tavily"]
    state.extras["web_search"] = []
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


def test_noninteractive_web_search_none_retires_docker_carrier(monkeypatch, tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    _first_run(
        monkeypatch, root, "--hosting", "docker",
        "--web-search", "web_search_tavily", "--tavily-api-key", "k",
    )
    assert "NYMERIA_INIT_DEFAULT_THREAD_TOOLS" in (
        (root / ".env.docker").read_text(encoding="utf-8")
    )

    # The CLI-level twin of the revert-to-defaults carrier test: `none` is the
    # scripted way to deselect the family, and the stale tools carrier must go
    # (the skills carrier stays; see docker_init_seed_env).
    rc = setup_main(
        ["--web-search", "none", "--root", str(root),
         "--non-interactive", "--skip-llm-test"]
    )
    assert rc == 0
    after = (root / ".env.docker").read_text(encoding="utf-8")
    assert "NYMERIA_INIT_DEFAULT_THREAD_TOOLS" not in after


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


def test_noninteractive_quick_docker_seeds_slim_and_carrier(monkeypatch, tmp_path):
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
    carrier = _env_line(content, "NYMERIA_INIT_DEFAULT_THREAD_TOOLS")
    assert carrier is not None and "fetch_url_nymeria" in carrier


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
        *family_catalog.default_checked_skill_kits(),
    ]


def test_noninteractive_quick_docker_provisions_searxng_sidecar(
    monkeypatch, tmp_path
):
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
    # SearXNG (sidecar-backed) is the Docker search default, wired turnkey.
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
