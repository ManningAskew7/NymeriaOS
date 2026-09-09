"""The wizard's server-browser slice: the pick, the finalize hook, doctor, hydrate.

The launcher itself is covered by ``test_server_browser.py``; this file covers
what setup DOES with it. Nothing here downloads a browser or starts a service:
``server_browser.provision`` / ``install`` are stubbed at the seam, so the
assertions are about the wiring setup owns (which token, which identity, which
preferences, which config key, and what the user is told when it fails).

Behaviour numbers (E1, E3, ...) index the server-browser plan's
expected-behaviours list; each test's docstring restates the behaviour it pins.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from nymeria import server_browser as sb
from nymeria.core.accounts import BOOTSTRAP_USER_ID
from nymeria.core.user_profile import UserProfileManager
from nymeria.onboarding import DockerStack, HostingOption, NextAction
from nymeria.setup import finalize as finalize_mod
from nymeria.setup import hydrate as hydrate_mod
from nymeria.setup.server_browser_catalog import (
    INSTALL,
    SERVER_BROWSER_STEP_ID,
    SKIP,
    server_browser_selected,
)
from nymeria.setup.state import WizardState


@pytest.fixture(autouse=True)
def _no_ambient_rig_home(monkeypatch):
    """`resolve_rig_home` consults the env first, and a dogfood box exports it."""
    monkeypatch.delenv(sb.HOME_ENV_KEY, raising=False)


class _FakeConsole:
    def __init__(self) -> None:
        self.printed: list[str] = []

    def print(self, *args, **kwargs):
        self.printed.append(" ".join(str(a) for a in args))

    @property
    def text(self) -> str:
        return "\n".join(self.printed)


class _FakeRepo:
    """The three accounts operations the hook uses, with a recorded history.

    A minted token JOINS `existing`, which is what makes ordering observable: a
    real repo's token list contains the token it just issued, so a mint-then-
    revoke implementation would revoke its own fresh token. A static list cannot
    see that and would pass either way.
    """

    def __init__(self, existing: list | None = None) -> None:
        self.existing = existing or []
        self.issued: list[str | None] = []
        self.revoked: list[str] = []

    def list_tokens_for_user(self, user_id):
        assert user_id == BOOTSTRAP_USER_ID
        return list(self.existing)

    def revoke_token(self, user_id, prefix):
        self.revoked.append(prefix)
        for record in self.existing:
            if record.hash_prefix == prefix:
                record.revoked_at = "2026-09-09T00:00:00Z"
        return True

    def issue_token(self, user_id, label=None):
        self.issued.append(label)
        raw = f"nym_serverbrowser_{len(self.issued)}"
        self.existing.append(_Token(label, f"fresh{len(self.issued)}"))
        return raw


class _Token:
    def __init__(self, label, prefix, revoked_at=None):
        self.label = label
        self.hash_prefix = prefix
        self.revoked_at = revoked_at


def _state(**kw) -> WizardState:
    state = WizardState()
    state.hosting = kw.pop("hosting", HostingOption.LOCAL)
    state.api_port = kw.pop("api_port", 8010)
    for key, value in kw.items():
        setattr(state, key, value)
    return state


def _stub_provision(monkeypatch, *, ok=True, warnings=(), manual=()):
    """Replace the launcher seam; returns the dict the call's kwargs land in."""
    captured: dict = {}

    def fake_provision(root, **kwargs):
        captured["root"] = root
        captured.update(kwargs)
        config = (
            sb.RigConfig(
                client_id=kwargs["client_id"],
                base_url=kwargs["base_url"],
                label=kwargs["label"],
            )
            if ok
            else None
        )
        return sb.ProvisionReport(
            ok=ok,
            config=config,
            lines=["Chrome for Testing 152.0.7977.64 installed."] if ok else [],
            warnings=list(warnings),
            manual_commands=list(manual),
            service_installed=ok,
        )

    monkeypatch.setattr(sb, "provision", fake_provision)
    return captured


def _seed_rig(root, client_id="nymeria-browser-existing", port=8010):
    rig = sb.resolve_rig_home(root, process_env=False)
    rig.path.mkdir(parents=True, exist_ok=True)
    sb.RigConfig(client_id=client_id, base_url=f"http://localhost:{port}").save(rig)
    return rig


def _seed_chrome(root):
    """A binary on disk is what tells Docker phase 2 that phase 1 succeeded."""
    rig = sb.resolve_rig_home(root, process_env=False)
    binary = rig.cft_dir / "152.0.7977.64" / sb.chrome_relative_binary(sb.cft_platform())
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\n")
    return rig


def _browser_prefs(data_dir):
    return UserProfileManager(data_dir).get_profile(BOOTSTRAP_USER_ID).get_browser_preferences()


# --- the pick ---------------------------------------------------------------


def test_install_is_the_default_and_only_a_skip_opts_out():
    """E4: an install that never saw the screen (a quick or non-interactive
    run) still gets the browser."""
    assert server_browser_selected(_state()) is True
    assert server_browser_selected(_state(extras={SERVER_BROWSER_STEP_ID: INSTALL})) is True
    assert server_browser_selected(_state(extras={SERVER_BROWSER_STEP_ID: SKIP})) is False
    # An unrecognised stored value is not a silent opt-in either.
    assert server_browser_selected(_state(extras={SERVER_BROWSER_STEP_ID: "maybe"})) is False


def test_step_sits_after_the_kits_and_always_applies():
    from nymeria.setup.steps import build_default_steps

    steps = build_default_steps()
    ids = [step.id for step in steps]
    assert ids.index("skill_kits") < ids.index(SERVER_BROWSER_STEP_ID)
    assert ids.index(SERVER_BROWSER_STEP_ID) < ids.index("review")
    step = steps[ids.index(SERVER_BROWSER_STEP_ID)]
    # No gating predicate: every hosting shape can drive a browser.
    assert step.applies(_state()) is True
    assert step.applies(_state(hosting=HostingOption.DOCKER)) is True


def test_no_server_browser_flag_stores_the_skip():
    """E3/E4: the flag is the only opt-out; there is no --server-browser
    because installing is already the default."""
    from nymeria.setup.runner import _build_state, build_parser

    parser = build_parser()
    default = _build_state(parser.parse_args(["--non-interactive"]))
    assert server_browser_selected(default) is True

    skipped = _build_state(parser.parse_args(["--non-interactive", "--no-server-browser"]))
    assert skipped.extras[SERVER_BROWSER_STEP_ID] == SKIP
    assert server_browser_selected(skipped) is False


def test_a_namespace_without_the_flag_still_defaults_to_install():
    """`_build_state` is fed namespaces built elsewhere (the desktop flow,
    tests), so a missing attribute must not crash or flip the default."""
    from nymeria.setup.runner import _build_state

    assert server_browser_selected(_build_state(argparse.Namespace())) is True


def test_the_step_is_a_jumpable_section():
    """`nymeria init server_browser` re-provisions the browser alone. The
    section list is a TUI-free mirror of the step list, so a step added to one
    and not the other makes its own jump an unknown-section error."""
    from nymeria.setup.quick import validate_section_id

    validate_section_id(SERVER_BROWSER_STEP_ID)
    with pytest.raises(SystemExit) as exc:
        validate_section_id("server-browser")
    assert SERVER_BROWSER_STEP_ID in str(exc.value)


def test_review_summary_names_the_pick():
    from nymeria.setup.steps.review import _summary_markup

    assert "Server browser: install" in _summary_markup(_state())
    assert "Server browser: skipped" in _summary_markup(
        _state(extras={SERVER_BROWSER_STEP_ID: SKIP})
    )


# --- SERVER_BROWSER_HOME in the config --------------------------------------


def test_home_is_recorded_when_selected_and_not_when_skipped(tmp_path):
    """E1/E3: this key is what tells the backend's refusals and doctor that
    the install HAS a server browser, so a skip must not write it."""
    root = tmp_path / "root"
    root.mkdir()
    assert (
        finalize_mod._server_browser_home_for_config(_state(), root=root)
        == (root / "data" / "server-browser").resolve()
    )
    assert (
        finalize_mod._server_browser_home_for_config(
            _state(extras={SERVER_BROWSER_STEP_ID: SKIP}), root=root
        )
        is None
    )


def test_home_stays_recorded_when_a_skip_leaves_an_existing_rig(tmp_path):
    """A reconfigure that skips must not orphan a rig that exists: dropping
    the key would make every refusal forget the server browser."""
    root = tmp_path / "root"
    rig = _seed_rig(root)
    assert (
        finalize_mod._server_browser_home_for_config(
            _state(extras={SERVER_BROWSER_STEP_ID: SKIP}), root=root
        )
        == rig.path
    )


# --- the finalize hook, native shapes ---------------------------------------


def _run_hook(state, console, *, root, data_dir, repo, for_docker=False):
    finalize_mod._maybe_install_server_browser(
        state,
        console,
        root=root,
        data_dir=data_dir,
        repo=repo,
        for_docker=for_docker,
    )


def _finish_docker(state, console, *, root):
    """Docker phase 2, with the stack descriptor finalize would have built."""
    finalize_mod._finish_docker_server_browser(
        state, console, root=root, spec=finalize_mod._docker_stack_spec(state)
    )


def test_hook_mints_a_labelled_token_and_provisions_against_the_local_api(
    tmp_path, monkeypatch
):
    """E1: the rig is connected to THIS install as the admin, with its own
    token, a fresh identity, and the default label."""
    root = tmp_path / "root"
    data_dir = root / "data"
    data_dir.mkdir(parents=True)
    captured = _stub_provision(monkeypatch)
    repo = _FakeRepo()

    _run_hook(_state(api_port=8010), _FakeConsole(), root=root, data_dir=data_dir, repo=repo)

    assert repo.issued == [sb.TOKEN_LABEL]
    assert captured["token"] == "nym_serverbrowser_1"
    assert captured["base_url"] == "http://localhost:8010"
    assert captured["client_id"].startswith(sb.CLIENT_ID_PREFIX)
    assert captured["label"] == sb.DEFAULT_LABEL
    assert captured["root"] == root
    # The rig lands in this install's own home, not a user-global one.
    assert captured["home"].path == sb.resolve_rig_home(root).path


def test_hook_makes_the_rig_the_accounts_labelled_default_browser(tmp_path, monkeypatch):
    """E1/E13: the browser is named and made the account default, so a fresh
    thread routes to it with no target step and the roster reads in English."""
    root = tmp_path / "root"
    data_dir = root / "data"
    data_dir.mkdir(parents=True)
    captured = _stub_provision(monkeypatch)
    console = _FakeConsole()

    _run_hook(_state(), console, root=root, data_dir=data_dir, repo=_FakeRepo())

    prefs = _browser_prefs(data_dir)
    assert prefs["default_target"] == captured["client_id"]
    assert prefs["labels"] == {captured["client_id"]: sb.DEFAULT_LABEL}
    assert sb.DEFAULT_LABEL in console.text


def test_hook_keeps_the_rigs_identity_across_a_rerun(tmp_path, monkeypatch):
    """E6: a reconfigure re-bakes the SAME browser. A fresh client_id would
    register a second browser on the account and make the roster lie."""
    root = tmp_path / "root"
    data_dir = root / "data"
    data_dir.mkdir(parents=True)
    _seed_rig(root)
    captured = _stub_provision(monkeypatch)

    _run_hook(_state(), _FakeConsole(), root=root, data_dir=data_dir, repo=_FakeRepo())

    assert captured["client_id"] == "nymeria-browser-existing"


def test_hook_revokes_a_previous_server_browser_token_before_minting(tmp_path, monkeypatch):
    """Re-runs must not pile tokens up against the account's cap, and the old
    bake's token must stop working the moment it is replaced."""
    root = tmp_path / "root"
    data_dir = root / "data"
    data_dir.mkdir(parents=True)
    _stub_provision(monkeypatch)
    repo = _FakeRepo(
        existing=[
            _Token(sb.TOKEN_LABEL, "aaaa1111"),
            _Token("chrome-extension", "bbbb2222"),
            _Token(sb.TOKEN_LABEL, "cccc3333", revoked_at="2026-01-01T00:00:00Z"),
        ]
    )

    _run_hook(_state(), _FakeConsole(), root=root, data_dir=data_dir, repo=repo)

    # Only the LIVE token of our own label: another label's token survives, and
    # an already-revoked one is not revoked twice.
    assert repo.revoked == ["aaaa1111"]
    assert repo.issued == [sb.TOKEN_LABEL]
    # And the order was revoke-then-mint, not the reverse: the fake appends the
    # token it issues, so a mint-first implementation would revoke it here and
    # hand the browser a token that is already dead.
    assert "fresh1" not in repo.revoked


def test_hook_does_nothing_when_the_step_was_skipped(tmp_path, monkeypatch):
    """E3: no download, no token, no service, and nothing claimed."""
    root = tmp_path / "root"
    data_dir = root / "data"
    data_dir.mkdir(parents=True)
    captured = _stub_provision(monkeypatch)
    monkeypatch.setattr(
        sb, "install", lambda *a, **k: pytest.fail("install must not run on a skip")
    )
    repo = _FakeRepo()
    console = _FakeConsole()

    _run_hook(
        _state(extras={SERVER_BROWSER_STEP_ID: SKIP}),
        console,
        root=root,
        data_dir=data_dir,
        repo=repo,
    )

    assert captured == {}
    assert repo.issued == []
    assert console.printed == []


def test_hook_says_an_existing_rig_is_kept_when_the_step_is_skipped(tmp_path, monkeypatch):
    root = tmp_path / "root"
    data_dir = root / "data"
    data_dir.mkdir(parents=True)
    _seed_rig(root)
    _stub_provision(monkeypatch)
    console = _FakeConsole()

    _run_hook(
        _state(extras={SERVER_BROWSER_STEP_ID: SKIP}),
        console,
        root=root,
        data_dir=data_dir,
        repo=_FakeRepo(),
    )

    assert "kept as-is" in console.text
    assert "nymeria browser service uninstall" in console.text


def test_hook_prints_the_manual_commands_when_provisioning_fails(tmp_path, monkeypatch):
    """Setup finishes whatever happens here, and the user is left with the
    commands that would complete the job."""
    root = tmp_path / "root"
    data_dir = root / "data"
    data_dir.mkdir(parents=True)
    _stub_provision(
        monkeypatch,
        ok=False,
        warnings=["Could not install Chrome for Testing: offline"],
        manual=["nymeria browser install --root /r"],
    )
    console = _FakeConsole()

    _run_hook(_state(), console, root=root, data_dir=data_dir, repo=_FakeRepo())

    assert "Could not install Chrome for Testing" in console.text
    assert "nymeria browser install --root /r" in console.text
    # No default claimed for a browser that does not exist.
    assert not _browser_prefs(data_dir).get("default_target")


def test_hook_survives_a_token_mint_failure_with_a_recovery_hint(tmp_path, monkeypatch):
    root = tmp_path / "root"
    data_dir = root / "data"
    data_dir.mkdir(parents=True)
    captured = _stub_provision(monkeypatch)

    class _Broken(_FakeRepo):
        def issue_token(self, user_id, label=None):
            raise RuntimeError("token limit reached")

    console = _FakeConsole()
    _run_hook(_state(), console, root=root, data_dir=data_dir, repo=_Broken())

    assert captured == {}
    assert "token limit reached" in console.text
    assert "nymeria users issue-token" in console.text


def test_hook_without_an_accounts_repo_hands_over_the_configure_command(tmp_path, monkeypatch):
    root = tmp_path / "root"
    data_dir = root / "data"
    data_dir.mkdir(parents=True)
    captured = _stub_provision(monkeypatch)
    console = _FakeConsole()

    _run_hook(_state(), console, root=root, data_dir=data_dir, repo=None)

    assert captured == {}
    # Named as the missing-database case, not misreported as a mint failure:
    # the two have different fixes.
    assert "No accounts database" in console.text
    assert "nymeria browser configure" in console.text
    assert "--token-file" in console.text


def test_hook_reports_a_failed_default_write_without_failing_setup(tmp_path, monkeypatch):
    """The profile write is best-effort: a rig that provisioned is still
    reported as provisioned, with the command that finishes the naming."""
    root = tmp_path / "root"
    data_dir = root / "data"
    data_dir.mkdir(parents=True)
    captured = _stub_provision(monkeypatch)

    def boom(self, user_id=BOOTSTRAP_USER_ID):
        raise OSError("read-only file system")

    monkeypatch.setattr(UserProfileManager, "atomic_update", boom)
    console = _FakeConsole()

    _run_hook(_state(), console, root=root, data_dir=data_dir, repo=_FakeRepo())

    assert captured["label"] == sb.DEFAULT_LABEL
    assert "read-only file system" in console.text
    assert f"/browser default {captured['client_id']}" in console.text


def test_the_hook_is_contained_so_setup_finishes(tmp_path, monkeypatch):
    """The one invariant this whole feature rests on. `provision` promises never
    to raise, but it drives a 200 MB download, an extract, a service install and
    four chmods; setup has already written config.env and minted the admin by
    then, so an escape would cost the user the bootstrap-token handoff."""
    root = tmp_path / "root"
    data_dir = root / "data"
    data_dir.mkdir(parents=True)

    def boom(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(sb, "provision", boom)
    console = _FakeConsole()

    # Called the way finalize calls it, through the real guard.
    finalize_mod._finalize_server_browser_guarded(
        _state(), console, root=root, data_dir=data_dir, repo=_FakeRepo(), for_docker=False
    )

    assert "No space left on device" in console.text
    assert "nymeria browser install" in console.text


def test_the_hook_runs_from_finalize_and_only_for_its_own_scoped_jump(monkeypatch):
    """Guards the call site and its gate: the hook only helps if finalize runs
    it, and a scoped `init <other-section>` must not re-provision a browser."""
    import inspect

    source = inspect.getsource(finalize_mod.finalize)
    assert "_finalize_server_browser_guarded(" in source
    assert 'scoped_section in (None, "server_browser")' in source


def test_the_step_id_and_the_scoped_section_name_are_the_same_string():
    """finalize gates the hook on the literal section name; a step id that
    drifted from it would silently stop the scoped jump provisioning."""
    from nymeria.setup.quick import DEFAULT_STEP_IDS

    assert SERVER_BROWSER_STEP_ID in DEFAULT_STEP_IDS


def test_the_home_key_reaches_the_written_config(tmp_path, monkeypatch):
    """The helper's return value is not the behaviour; the line in config.env
    is. Everything downstream (the refusals, doctor) reads the file."""
    from nymeria.setup.finalize import write_config

    root = tmp_path / "root"
    data_dir = root / "data"
    data_dir.mkdir(parents=True)
    config = root / "config.env"
    home = finalize_mod._server_browser_home_for_config(_state(), root=root)
    assert home is not None
    write_config(config, data_dir=data_dir, extra_env={sb.HOME_ENV_KEY: str(home)})
    assert f"{sb.HOME_ENV_KEY}={home}" in config.read_text()


def test_the_home_key_is_retired_when_the_rig_is_gone(tmp_path):
    """E3's other half: after a `service uninstall` and an rm, a reconfigure
    that skips must stop the refusals naming a browser that no longer exists.

    Driven from the same value finalize computes, not from a hand-built drop
    list, so a change that stops deriving the drop from the home is caught."""
    from nymeria.setup.finalize import write_config

    root = tmp_path / "root"
    data_dir = root / "data"
    data_dir.mkdir(parents=True)
    config = root / "config.env"
    config.write_text(f"LLM_PROVIDER=anthropic\n{sb.HOME_ENV_KEY}=/gone/rig\n")

    state = _state(extras={SERVER_BROWSER_STEP_ID: SKIP})
    home = finalize_mod._server_browser_home_for_config(state, root=root)
    assert home is None
    write_config(
        config,
        data_dir=data_dir,
        merge=True,
        drop_stale_server_browser=finalize_mod._server_browser_drop_env(home),
    )
    assert sb.HOME_ENV_KEY not in config.read_text()


def test_a_run_that_has_a_rig_retires_nothing(tmp_path):
    """The inverse, so the drop cannot become unconditional: a reconfigure of an
    install that HAS a rig must not strip its own key on the way past."""
    root = tmp_path / "root"
    root.mkdir()
    home = finalize_mod._server_browser_home_for_config(_state(), root=root)
    assert home is not None
    assert finalize_mod._server_browser_drop_env(home) == ()


def test_the_drop_is_wired_to_the_computed_home():
    """Guards the call site: the helper only helps if write_config is handed
    what it returns."""
    import inspect

    source = inspect.getsource(finalize_mod.finalize)
    assert "_server_browser_drop_env(rig_home)" in source
    assert "drop_stale_server_browser=drop_stale_server_browser" in source


def test_the_target_roots_rig_home_wins_over_the_launch_environment(tmp_path, monkeypatch):
    """A two-install host is the normal shape here (a Docker stack and a slim
    instance). `nymeria init --root <other>` must configure the OTHER install's
    rig, never re-bake the running one it was launched from."""
    launch = tmp_path / "launch"
    other = tmp_path / "other"
    other.mkdir(parents=True)
    (other / "config.env").write_text(f"{sb.HOME_ENV_KEY}={other / 'rig'}\n")
    monkeypatch.setenv(sb.HOME_ENV_KEY, str(launch / "rig"))

    assert finalize_mod._rig_home_for_root(other).path == (other / "rig").resolve()


def test_a_root_with_no_setting_falls_back_to_its_own_default_home(tmp_path, monkeypatch):
    """The fresh-root half of the same bug: `nymeria init --root /new` launched
    from an install that exports the key must still target /new."""
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv(sb.HOME_ENV_KEY, str(tmp_path / "elsewhere"))
    assert (
        finalize_mod._rig_home_for_root(root).path
        == (root / "data" / "server-browser").resolve()
    )


def test_setup_honours_the_launchers_pointer_file(tmp_path, monkeypatch):
    """`nymeria browser configure --home X` writes no env key, only the pointer
    file under the root's data dir. Setup once re-spelled the resolver's chain
    without that source and would have provisioned a second rig at the default
    home beside the configured one; delegating to the resolver closes it."""
    monkeypatch.delenv(sb.HOME_ENV_KEY, raising=False)
    root = tmp_path / "root"
    root.mkdir()
    custom = sb.RigHome((tmp_path / "custom-rig").resolve())
    sb.write_rig_home_pointer(root, custom, log=lambda _: None)

    assert finalize_mod._rig_home_for_root(root).path == custom.path
    assert finalize_mod._rig_home_for_root(root).path == sb.resolve_rig_home(root).path


# --- the finalize hook, Docker ----------------------------------------------


def test_docker_phase_one_only_installs_chrome(tmp_path, monkeypatch):
    """E5: the token is minted inside the container, which does not exist
    yet, so the first phase downloads the browser and stops."""
    root = tmp_path / "root"
    data_dir = root / "data"
    data_dir.mkdir(parents=True)
    installs: list = []
    monkeypatch.setattr(sb, "install", lambda rig, **kw: installs.append(rig.path))
    captured = _stub_provision(monkeypatch)
    console = _FakeConsole()

    _run_hook(
        _state(
            hosting=HostingOption.DOCKER,
            docker_stack=DockerStack.SLIM,
            next_action=NextAction.START_API_OPEN_FRONTEND,
        ),
        console,
        root=root,
        data_dir=data_dir,
        repo=_FakeRepo(),
        for_docker=True,
    )

    assert installs == [sb.resolve_rig_home(root).path]
    assert captured == {}
    assert "once the stack is up" in console.text.lower()


def test_docker_without_start_now_prints_the_three_manual_commands(tmp_path, monkeypatch):
    root = tmp_path / "root"
    data_dir = root / "data"
    data_dir.mkdir(parents=True)
    monkeypatch.setattr(sb, "install", lambda rig, **kw: None)
    console = _FakeConsole()

    _run_hook(
        _state(hosting=HostingOption.DOCKER, docker_stack=DockerStack.SLIM),
        console,
        root=root,
        data_dir=data_dir,
        repo=None,
        for_docker=True,
    )

    text = console.text
    assert "issue-token" in text and sb.TOKEN_LABEL in text
    assert "nymeria browser configure" in text
    assert "nymeria browser service install" in text


def test_docker_phase_one_reports_a_download_failure_and_finishes(tmp_path, monkeypatch):
    root = tmp_path / "root"
    data_dir = root / "data"
    data_dir.mkdir(parents=True)

    def boom(rig, **kw):
        raise sb.ServerBrowserError("offline", hints=["retry with nymeria browser install"])

    monkeypatch.setattr(sb, "install", boom)
    console = _FakeConsole()

    _run_hook(
        _state(hosting=HostingOption.DOCKER, docker_stack=DockerStack.SLIM),
        console,
        root=root,
        data_dir=data_dir,
        repo=None,
        for_docker=True,
    )

    assert "offline" in console.text
    assert "retry with nymeria browser install" in console.text


def test_docker_phase_two_mints_in_container_then_provisions(tmp_path, monkeypatch):
    """E5's second half: once the stack answers its health check the token
    comes from inside the container, and the account default goes through the
    API because the profile lives in the container's volume."""
    root = tmp_path / "root"
    root.mkdir()
    _seed_chrome(root)
    captured = _stub_provision(monkeypatch)
    monkeypatch.setattr(
        finalize_mod, "_mint_docker_server_browser_token", lambda **kw: "nym_in_container"
    )
    applied: dict = {}
    monkeypatch.setattr(
        finalize_mod,
        "_apply_server_browser_default_via_api",
        lambda **kw: applied.update(kw),
    )
    state = _state(hosting=HostingOption.DOCKER, docker_stack=DockerStack.SLIM, api_port=8000)

    _finish_docker(state, _FakeConsole(), root=root)

    assert captured["token"] == "nym_in_container"
    assert captured["base_url"] == "http://localhost:8000"
    assert applied["client_id"] == captured["client_id"]
    assert applied["label"] == sb.DEFAULT_LABEL
    assert applied["token"] == "nym_in_container"


def test_docker_phase_two_keeps_an_existing_rigs_identity(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    _seed_chrome(root)
    _seed_rig(root, port=8000)
    captured = _stub_provision(monkeypatch)
    monkeypatch.setattr(finalize_mod, "_mint_docker_server_browser_token", lambda **kw: "t")
    monkeypatch.setattr(
        finalize_mod, "_apply_server_browser_default_via_api", lambda **kw: None
    )
    state = _state(hosting=HostingOption.DOCKER, docker_stack=DockerStack.SLIM, api_port=8000)

    _finish_docker(state, _FakeConsole(), root=root)

    assert captured["client_id"] == "nymeria-browser-existing"


def test_docker_phase_two_falls_back_to_the_manual_commands_when_the_mint_fails(
    tmp_path, monkeypatch
):
    root = tmp_path / "root"
    root.mkdir()
    _seed_chrome(root)
    captured = _stub_provision(monkeypatch)
    monkeypatch.setattr(finalize_mod, "_mint_docker_server_browser_token", lambda **kw: None)
    state = _state(hosting=HostingOption.DOCKER, docker_stack=DockerStack.SLIM)
    console = _FakeConsole()

    _finish_docker(state, console, root=root)

    assert captured == {}
    assert "nymeria browser configure" in console.text


def test_docker_phase_two_is_silent_when_phase_one_never_installed(tmp_path, monkeypatch):
    """No browser on disk means phase 1 already reported its failure; phase 2
    must not repeat it or claim a rig that does not exist."""
    root = tmp_path / "root"
    root.mkdir()
    captured = _stub_provision(monkeypatch)
    state = _state(hosting=HostingOption.DOCKER, docker_stack=DockerStack.SLIM)
    console = _FakeConsole()

    _finish_docker(state, console, root=root)

    assert captured == {}
    assert console.printed == []


def test_docker_phase_two_is_silent_when_the_step_was_skipped(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    _seed_chrome(root)
    captured = _stub_provision(monkeypatch)
    state = _state(
        hosting=HostingOption.DOCKER,
        docker_stack=DockerStack.SLIM,
        extras={SERVER_BROWSER_STEP_ID: SKIP},
    )
    console = _FakeConsole()

    _finish_docker(state, console, root=root)

    assert captured == {}
    assert console.printed == []


def test_the_docker_mint_revokes_the_label_before_issuing(tmp_path, monkeypatch):
    """Without --replace every Docker re-run leaves another live admin token
    behind, baked into a config.json since overwritten so nothing will ever
    revoke it, until the account's active-token cap trips and minting starts
    failing with no explanation."""
    calls: list = []

    class _Result:
        returncode = 0
        stdout = "Issued new token for default.\n  Token: nym_" + "a" * 40

    def fake_run(command, **kwargs):
        calls.append(command)
        return _Result()

    monkeypatch.setattr(finalize_mod.subprocess, "run", fake_run)
    state = _state(hosting=HostingOption.DOCKER, docker_stack=DockerStack.SLIM)
    token = finalize_mod._mint_docker_server_browser_token(
        spec=finalize_mod._docker_stack_spec(state), root=tmp_path
    )

    assert token is not None and token.startswith("nym_")
    argv = calls[0]
    assert "issue-token" in argv and "--replace" in argv
    assert argv[argv.index("--label") + 1] == sb.TOKEN_LABEL


def test_the_docker_mint_reports_nothing_rather_than_a_partial_token(tmp_path, monkeypatch):
    class _Failed:
        returncode = 2
        stdout = ""

    monkeypatch.setattr(finalize_mod.subprocess, "run", lambda c, **k: _Failed())
    state = _state(hosting=HostingOption.DOCKER, docker_stack=DockerStack.SLIM)
    assert (
        finalize_mod._mint_docker_server_browser_token(
            spec=finalize_mod._docker_stack_spec(state), root=tmp_path
        )
        is None
    )


def test_the_docker_default_is_set_over_the_api_and_never_renames(tmp_path, monkeypatch):
    """The profile lives in the container's volume, so the default goes through
    the API. The NAME does not: `/browser rename`'s label is a single plain
    positional, so the two-word default label parsed as an extra argument and
    the command failed at level=error inside an HTTP 200, which raised nothing
    and printed success anyway. The extension's own announce carries the name."""
    sent: list = []
    monkeypatch.setattr(
        sb, "status", lambda home, **kw: type("R", (), {"connected_per_backend": True})()
    )
    monkeypatch.setattr(
        sb, "http_json", lambda url, **kw: sent.append((url, kw.get("body", {}).get("command")))
    )
    console = _FakeConsole()
    finalize_mod._apply_server_browser_default_via_api(
        base_url="http://localhost:8000",
        token="t",
        client_id="nymeria-browser-abc",
        label=sb.DEFAULT_LABEL,
        home=sb.resolve_rig_home(tmp_path),
        console=console,
    )

    commands = [c for _, c in sent]
    assert commands == ["/browser default nymeria-browser-abc"]
    assert not any("rename" in (c or "") for c in commands)
    assert sb.DEFAULT_LABEL in console.text


def test_the_docker_default_gives_up_with_the_command_when_it_never_connects(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        sb, "status", lambda home, **kw: type("R", (), {"connected_per_backend": False})()
    )
    monkeypatch.setattr(sb, "http_json", lambda *a, **k: pytest.fail("must not send"))
    console = _FakeConsole()
    finalize_mod._apply_server_browser_default_via_api(
        base_url="http://localhost:8000",
        token="t",
        client_id="nymeria-browser-abc",
        label=sb.DEFAULT_LABEL,
        home=sb.resolve_rig_home(tmp_path),
        console=console,
        wait_seconds=0.0,
    )
    assert "/browser default nymeria-browser-abc" in console.text


def test_a_docker_start_that_fails_still_hands_over_the_browser_commands():
    """Phase 1 withholds the manual steps on the promise that phase 2 runs, so
    every early return from the start must print them or the user is left with a
    downloaded Chrome, a written config key, no rig, and no instructions."""
    import inspect

    source = inspect.getsource(finalize_mod._start_now_docker)
    # One call per early return plus the success path's phase 2.
    assert source.count("_print_docker_server_browser_steps(") == 3
    assert "_finish_docker_server_browser(" in source


def test_the_manual_docker_steps_stay_silent_for_an_install_that_declined(tmp_path):
    console = _FakeConsole()
    finalize_mod._print_docker_server_browser_steps(
        console,
        _state(
            hosting=HostingOption.DOCKER,
            docker_stack=DockerStack.SLIM,
            extras={SERVER_BROWSER_STEP_ID: SKIP},
        ),
        root=tmp_path,
    )
    assert console.printed == []


# --- the rig's token lifetime -----------------------------------------------


def test_the_rigs_token_outlives_the_ordinary_account_lifetime(tmp_path):
    """Nothing renews the baked token and no human is in the loop, so the
    ordinary 90-day account lifetime would 401 every install's browser tools a
    quarter after setup, with no warning and no recovery but a manual
    re-configure."""
    from nymeria.core.accounts import AccountsRepo

    repo = AccountsRepo(tmp_path / "accounts.db")
    repo.create_user("default", "owner@localhost", "Owner", role="admin")
    repo.issue_token("default", label=sb.TOKEN_LABEL)
    repo.issue_token("default", label="cli")

    by_label = {t.label: t for t in repo.list_tokens_for_user("default")}
    assert by_label[sb.TOKEN_LABEL].expires_at > by_label["cli"].expires_at


def test_the_token_label_is_the_one_the_lifetime_rule_keys_on():
    """Two modules spell the same label; a drift would silently put the rig
    back on the 90-day fuse."""
    from nymeria.core.accounts import SERVER_BROWSER_TOKEN_LABEL

    assert sb.TOKEN_LABEL == SERVER_BROWSER_TOKEN_LABEL


# --- doctor -----------------------------------------------------------------


@pytest.fixture()
def rig_dir(tmp_path):
    """A rig home that EXISTS on disk.

    Doctor distinguishes a rig it can inspect from "the key is set but there is
    no rig at that path on this machine": on the Docker stack the api and
    worker containers carry the key as a PRESENCE signal while the rig runs on
    the host, so a fixture whose home never existed would exercise that branch
    instead of the one it means to.
    """
    path = tmp_path / "rig"
    path.mkdir()
    return path



class _Settings:
    def __init__(self, home=None):
        self.server_browser_home = home


def _report(**kw) -> sb.RigStatus:
    report = sb.RigStatus(home=kw.pop("home", Path("/rig")))
    for key, value in kw.items():
        setattr(report, key, value)
    return report


def test_doctor_stays_silent_on_an_install_with_no_server_browser(tmp_path):
    """A popup-only install must not grow a row about a feature it never
    configured."""
    from nymeria import doctor

    assert doctor._check_server_browser(_Settings(), tmp_path) is None


def test_doctor_finds_a_rig_named_only_by_the_environment(tmp_path, rig_dir, monkeypatch):
    """The settings object is not the only source: a process that was launched
    with the key exported has it in the environment and nowhere else."""
    from nymeria import doctor

    monkeypatch.setenv(sb.HOME_ENV_KEY, str(rig_dir))
    monkeypatch.setattr(
        sb, "status", lambda home, **kw: _report(installed=True, problems=["Not running."])
    )
    result = doctor._check_server_browser(_Settings(), tmp_path)
    assert result is not None
    assert result.status == "warn"


def test_doctor_does_not_call_a_host_rig_missing_from_inside_a_container(
    tmp_path, monkeypatch
):
    """The Docker stack sets the key in the api and worker containers on
    purpose: it is the presence signal the refusals read after a restart, and
    the path it names belongs to the HOST. Running doctor in there must not
    accuse a rig that is running fine one filesystem away."""
    from nymeria import doctor

    monkeypatch.setattr(
        sb, "status", lambda home, **kw: pytest.fail("status must not be probed")
    )
    result = doctor._check_server_browser(_Settings(str(tmp_path / "host-rig")), tmp_path)
    assert result is not None
    assert result.status == "warn"
    assert "on this machine" in result.detail
    assert "the rig runs on the host" in result.detail


def test_doctor_does_not_blame_the_browser_when_the_backend_is_down(tmp_path, rig_dir, monkeypatch):
    """`nymeria init --doctor` runs BEFORE start-now launches the backend, so a
    perfectly healthy fresh install would otherwise end on a red-looking row
    about a browser that is running exactly as installed."""
    from nymeria import doctor

    monkeypatch.setattr(
        sb,
        "status",
        lambda home, **kw: _report(
            installed=True,
            configured=True,
            running=True,
            backend_reachable=False,
            problems=["Backend not reachable at http://localhost:8010."],
        ),
    )
    result = doctor._check_server_browser(_Settings(str(rig_dir)), tmp_path)
    assert result is not None
    assert result.status == "warn"
    assert "installed and running" in result.detail
    assert "the backend is not up" in result.detail
    assert "nymeria browser status" in result.detail


def test_doctor_passes_a_healthy_rig_and_names_it(tmp_path, rig_dir, monkeypatch):
    from nymeria import doctor

    monkeypatch.setattr(
        sb,
        "status",
        lambda home, **kw: _report(
            installed=True,
            configured=True,
            running=True,
            chrome_version="Chrome 152.0.7977.64",
            identity="me@example.com",
            label="server browser",
            debug_port=9222,
            connected_per_backend=True,
            extension_worker_present=True,
            token_valid=True,
        ),
    )
    result = doctor._check_server_browser(_Settings(str(rig_dir)), tmp_path)
    assert result is not None
    assert result.status == "pass"
    assert "152.0.7977.64" in result.detail
    assert "me@example.com" in result.detail
    assert "9222" in result.detail
    # The refusals and the roster both speak in kinds, so the row does too.
    assert "server browser" in result.detail


def test_doctor_warns_for_as_long_as_the_sandbox_is_off(tmp_path, rig_dir, monkeypatch):
    """The opt-out was measured, not chosen, so even a healthy rig says so and
    names the durable fix."""
    from nymeria import doctor

    monkeypatch.setattr(
        sb,
        "status",
        lambda home, **kw: _report(
            installed=True,
            configured=True,
            running=True,
            chrome_version="Chrome 152",
            connected_per_backend=True,
            no_sandbox=True,
        ),
    )
    result = doctor._check_server_browser(_Settings(str(rig_dir)), tmp_path)
    assert result is not None
    assert result.status == "warn"
    assert "--no-sandbox" in result.detail
    assert "AppArmor" in result.detail


def test_doctor_warns_with_the_status_command_when_the_rig_is_down(tmp_path, rig_dir, monkeypatch):
    """E23: never a hard failure (the backend runs fine without a browser),
    always the command that says more."""
    from nymeria import doctor

    monkeypatch.setattr(
        sb,
        "status",
        lambda home, **kw: _report(
            installed=True,
            configured=True,
            problems=["Not running (nymeria browser run)."],
        ),
    )
    result = doctor._check_server_browser(_Settings(str(rig_dir)), tmp_path)
    assert result is not None
    assert result.status == "warn"
    assert "Not running" in result.detail
    assert "nymeria browser status" in result.detail


def test_doctor_reports_rather_than_raises_when_status_explodes(
    tmp_path, rig_dir, monkeypatch
):
    from nymeria import doctor

    def boom(home, **kw):
        raise OSError("permission denied")

    monkeypatch.setattr(sb, "status", boom)
    result = doctor._check_server_browser(_Settings(str(rig_dir)), tmp_path)
    assert result is not None
    assert result.status == "warn"
    assert "permission denied" in result.detail


def test_doctor_finds_a_hand_configured_rig_with_no_config_key(tmp_path, monkeypatch):
    """`nymeria browser configure` writes no env key, so the rig.json at the
    default home is the other signal doctor must honour."""
    from nymeria import doctor

    _seed_rig(tmp_path)
    monkeypatch.setattr(
        sb, "status", lambda home, **kw: _report(installed=True, problems=["Not configured."])
    )
    result = doctor._check_server_browser(_Settings(), tmp_path)
    assert result is not None
    assert result.status == "warn"


# --- hydrate ----------------------------------------------------------------


def test_hydrate_recognises_a_rig_from_the_config_key(tmp_path):
    """A rig this install owns: the re-run re-bakes it, keeping its identity."""
    state = _state(extras={})
    hydrate_mod._hydrate_server_browser(
        state, {sb.HOME_ENV_KEY: str(tmp_path / "elsewhere")}, config_root=tmp_path
    )
    assert state.extras[SERVER_BROWSER_STEP_ID] == INSTALL


def test_hydrate_recognises_a_rig_configured_by_hand(tmp_path):
    """`nymeria browser configure` writes no env key, so the rig.json at the
    default home is the other signal that a rig exists."""
    _seed_rig(tmp_path)
    state = _state(extras={})
    hydrate_mod._hydrate_server_browser(state, {}, config_root=tmp_path)
    assert state.extras[SERVER_BROWSER_STEP_ID] == INSTALL


def test_a_reconfigure_of_a_rigless_install_defaults_to_skip(tmp_path):
    """The declined-install case, and the one that bites. Hydration only runs on
    a RECONFIGURE, so no rig here means the operator either said no or predates
    the feature. Defaulting to install would have any later `nymeria init
    --non-interactive` (a model change, say) download 200 MB, mint an admin
    token and install a service on a host that never asked, and would make
    `--no-server-browser` something they must remember forever."""
    state = _state(extras={})
    hydrate_mod._hydrate_server_browser(state, {}, config_root=tmp_path)
    assert state.extras[SERVER_BROWSER_STEP_ID] == SKIP
    assert server_browser_selected(state) is False


def test_a_fresh_install_still_defaults_to_install(tmp_path):
    """The sticky-skip rule must not reach a first run: hydration returns before
    it when there is no config at all, so the wizard's own default applies."""
    state = _state(root=tmp_path, extras={})
    assert hydrate_mod.hydrate_state_from_disk(state) is False
    assert server_browser_selected(state) is True


def test_hydrate_does_not_overwrite_this_runs_pick(tmp_path):
    """A reconfigure that passed --no-server-browser keeps its skip even though
    a rig exists on disk, and the reverse: an explicit install survives the
    sticky-skip default."""
    state = _state(extras={SERVER_BROWSER_STEP_ID: SKIP})
    hydrate_mod._hydrate_server_browser(
        state, {sb.HOME_ENV_KEY: str(tmp_path / "rig")}, config_root=tmp_path
    )
    assert state.extras[SERVER_BROWSER_STEP_ID] == SKIP

    chosen = _state(extras={SERVER_BROWSER_STEP_ID: INSTALL})
    hydrate_mod._hydrate_server_browser(chosen, {}, config_root=tmp_path)
    assert chosen.extras[SERVER_BROWSER_STEP_ID] == INSTALL


def test_hydrate_is_wired_into_the_disk_hydration(monkeypatch, tmp_path):
    """Guards the call site: the helper only helps if hydration runs it, and
    it must be handed the config's own directory."""
    seen: list = []
    monkeypatch.setattr(
        hydrate_mod,
        "_hydrate_server_browser",
        lambda state, values, *, config_root: seen.append(config_root),
    )
    root = tmp_path / "root"
    root.mkdir()
    (root / "config.env").write_text("LLM_PROVIDER=anthropic\nLLM_MODEL=claude-opus-5\n")

    assert hydrate_mod.hydrate_state_from_disk(WizardState(root=root)) is True
    assert seen == [root]


# --- every setup-side lookup answers for the ROOT, never the launch env -------


def test_the_native_hook_provisions_the_target_roots_rig(tmp_path, monkeypatch):
    """The provisioning half of the two-install bug: with the launch shell
    exporting another install's rig home, the hook must still build THIS
    root's rig. The old bare `resolve_rig_home(root)` re-baked the other one."""
    root = tmp_path / "root"
    data_dir = root / "data"
    data_dir.mkdir(parents=True)
    monkeypatch.setenv(sb.HOME_ENV_KEY, str(tmp_path / "elsewhere"))
    captured = _stub_provision(monkeypatch)

    _run_hook(_state(), _FakeConsole(), root=root, data_dir=data_dir, repo=_FakeRepo())

    assert captured["home"].path == (root / "data" / "server-browser").resolve()


def test_docker_phase_two_provisions_the_target_roots_rig(tmp_path, monkeypatch):
    """Same rule for the Docker phase: the Chrome that phase 1 put under the
    target root is the one phase 2 connects, even when the shell names a rig
    (with no Chrome in it) somewhere else."""
    root = tmp_path / "root"
    root.mkdir()
    _seed_chrome(root)
    monkeypatch.setenv(sb.HOME_ENV_KEY, str(tmp_path / "elsewhere"))
    captured = _stub_provision(monkeypatch)
    monkeypatch.setattr(finalize_mod, "_mint_docker_server_browser_token", lambda **kw: "t")
    monkeypatch.setattr(
        finalize_mod, "_apply_server_browser_default_via_api", lambda **kw: None
    )
    state = _state(hosting=HostingOption.DOCKER, docker_stack=DockerStack.SLIM, api_port=8000)

    _finish_docker(state, _FakeConsole(), root=root)

    assert captured["home"].path == (root / "data" / "server-browser").resolve()


def test_hydrate_does_not_mistake_the_launch_environments_rig_for_this_roots(
    tmp_path, monkeypatch
):
    """A rig.json under the shell's exported home belongs to the install that
    exported it. This root has no key and no rig, so the sticky-skip default
    applies; the old env-first lookup would have re-selected install."""
    other_rig = _seed_rig(tmp_path / "elsewhere")
    monkeypatch.setenv(sb.HOME_ENV_KEY, str(other_rig.path))
    root = tmp_path / "root"
    root.mkdir()
    state = _state(extras={})

    hydrate_mod._hydrate_server_browser(state, {}, config_root=root)

    assert state.extras[SERVER_BROWSER_STEP_ID] == SKIP
