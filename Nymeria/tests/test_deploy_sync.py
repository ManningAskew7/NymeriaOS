"""Behavior tests for the deploy-sync tick (`scripts/deploy_sync.py`).

Plan behaviors B4-B13 in ``tmp/deploy-sync-plan.md``. The script restarts
LIVE deployments, so these tests assert observable outcomes: which commands
ran (pull, restart argv), what the marker files say afterwards, and what
the summary reports; never internal call counts for their own sake. The
git/HTTP/sleep seams are injected fakes; nothing here touches a real repo,
socket, or service.
"""
from __future__ import annotations

import importlib.util
import json
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "deploy_sync.py"

spec = importlib.util.spec_from_file_location("deploy_sync", SCRIPT)
assert spec is not None and spec.loader is not None
deploy_sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy_sync)

SHA_OLD = "a" * 40
SHA_NEW = "b" * 40
SHA_LOCAL = "c" * 40


def refused() -> urllib.error.URLError:
    """What production urllib actually raises for a refused connection."""
    return urllib.error.URLError(ConnectionRefusedError(111, "refused"))


class FakeWorld:
    def __init__(self):
        self.branch: str | None = "main"  # None = detached HEAD
        self.head = SHA_OLD
        self.head_rc = 0
        self.origin = SHA_OLD
        self.merge_base: str | None = SHA_OLD
        self.status_out = ""
        # Range -> changed file names; a value of None makes `git diff`
        # FAIL for that range (unknown/rewritten commit).
        self.diff_names: dict[tuple[str, str], list[str] | None] = {}
        self.fetch_fails = False
        self.pull_ok = True
        self.running_containers: list[str] = []
        self.restart_rc = 0


class FakeRunner:
    def __init__(self, world: FakeWorld):
        self.world = world
        self.calls: list[tuple] = []

    def __call__(self, argv, cwd=None, env_extra=None):
        self.calls.append((tuple(argv), cwd, tuple(sorted((env_extra or {}).items()))))
        w = self.world
        if argv[:2] == ["git", "-C"]:
            sub = argv[3:]
            if sub[0] == "symbolic-ref":
                if w.branch is None:
                    return 1, ""  # detached HEAD
                return 0, w.branch
            if sub[0] == "fetch":
                return (1, "network down") if w.fetch_fails else (0, "")
            if sub[:2] == ["rev-parse", "HEAD"]:
                return w.head_rc, w.head
            if sub[:2] == ["rev-parse", "origin/main"]:
                return 0, w.origin
            if sub[0] == "merge-base":
                if w.merge_base is None:
                    return 1, "no merge base"
                return 0, w.merge_base
            if sub[0] == "status":
                return 0, w.status_out
            if sub[0] == "diff":
                names = w.diff_names.get((sub[2], sub[3]), [])
                if names is None:
                    return 1, "fatal: bad object"
                return 0, "\n".join(names)
            if sub[0] == "pull":
                if w.pull_ok:
                    w.head = w.origin
                    return 0, "Fast-forward"
                return 1, "cannot fast-forward"
            raise AssertionError(f"unexpected git call: {sub}")
        if argv[:2] == ["docker", "ps"]:
            return 0, "\n".join(w.running_containers)
        if argv[:2] == ["docker", "restart"]:
            return 0, ""
        if argv[0].startswith("restart-"):
            return w.restart_rc, "" if w.restart_rc == 0 else "boom"
        raise AssertionError(f"unexpected command: {argv}")

    def commands(self):
        return [c[0] for c in self.calls]


class FakeHttp:
    def __init__(self):
        self.routes: dict[str, object] = {}
        self.headers_seen: dict[str, list[dict]] = {}

    def __call__(self, url, headers):
        self.headers_seen.setdefault(url, []).append(dict(headers))
        entry = self.routes[url]
        if isinstance(entry, Exception):
            raise entry
        return entry


@pytest.fixture
def world(tmp_path):
    w = FakeWorld()
    w.runner = FakeRunner(w)
    w.http = FakeHttp()
    w.state_dir = tmp_path / "state"
    w.state_dir.mkdir()

    slim_token = tmp_path / "slim-token.txt"
    slim_token.write_text("tok-slim\n", encoding="utf-8")
    env_file = tmp_path / "env.docker"
    env_file.write_text(
        'IRRELEVANT=1\nNYMERIA_SERVICE_TOKEN="tok-docker"\n', encoding="utf-8"
    )

    w.config = {
        "repo": "/repo",
        "targets": [
            {
                "name": "slim",
                "health_url": "http://slim/health",
                "turns_url": "http://slim/status/turns",
                "token": {"file": str(slim_token)},
                "restart": ["restart-slim"],
            },
            {
                "name": "docker",
                "health_url": "http://docker/health",
                "turns_url": "http://docker/status/turns",
                "token": {"env_file": str(env_file), "key": "NYMERIA_SERVICE_TOKEN"},
                "restart": ["restart-docker"],
                "restart_cwd": "/repo/Nymeria",
                "restart_env": {"DISCORD_BOT_TOKEN": "disabled"},
                "extra_restart_containers": ["nymeria-telegram-bot"],
            },
        ],
    }
    for name in ("slim", "docker"):
        (w.state_dir / f"{name}.commit").write_text(SHA_OLD + "\n", encoding="utf-8")
        w.http.routes[f"http://{name}/status/turns"] = (
            200,
            {"active_turns": 0, "interactive_active": 0, "busy_threads": []},
        )
        w.http.routes[f"http://{name}/health"] = (200, {"status": "ok"})
    return w


def run_sync(w, dry_run: bool = False):
    return deploy_sync.sync(
        w.config,
        w.state_dir,
        dry_run=dry_run,
        runner=w.runner,
        http_get=w.http,
        sleep=lambda seconds: None,
    )


def marker(w, name: str) -> str:
    return (w.state_dir / f"{name}.commit").read_text(encoding="utf-8").strip()


def test_everything_current_is_a_quiet_noop(world):
    summary = run_sync(world)
    assert summary["targets"]["slim"]["action"] == "noop"
    assert summary["targets"]["docker"]["action"] == "noop"
    assert ("restart-slim",) not in world.runner.commands()
    assert ("restart-docker",) not in world.runner.commands()
    assert not any("pull" in c for c in world.runner.commands())


def test_repo_state_fetch_never_writes_fetch_head(world):
    """#221: the timer's 5-minute fetch races interactive `git pull --ff-only`
    sessions through FETCH_HEAD ("Cannot fast-forward to multiple branches").
    The fix is fetching with --no-write-fetch-head, so the flag on every fetch
    IS the contract this pins."""
    run_sync(world)
    fetches = [c for c in world.runner.calls if len(c[0]) > 3 and c[0][3] == "fetch"]
    assert fetches, "sync ran no fetch"
    for call in fetches:
        assert "--no-write-fetch-head" in call[0]


def test_origin_ahead_pulls_restarts_both_and_advances_markers(world):
    world.origin = SHA_NEW
    world.merge_base = SHA_OLD  # behind

    summary = run_sync(world)

    commands = world.runner.commands()
    assert ("git", "-C", "/repo", "pull", "--ff-only") in commands
    assert ("restart-slim",) in commands
    assert ("restart-docker",) in commands
    assert summary["targets"]["slim"]["action"] == "restarted"
    assert summary["targets"]["docker"]["action"] == "restarted"
    assert marker(world, "slim") == SHA_NEW
    assert marker(world, "docker") == SHA_NEW
    # The docker restart carried its compose env quirk.
    docker_call = next(c for c in world.runner.calls if c[0] == ("restart-docker",))
    assert ("DISCORD_BOT_TOKEN", "disabled") in docker_call[2]
    # compose resolves its file relative to cwd; dropping it breaks the
    # restart in production while looking identical in a cwd-blind fake.
    assert docker_call[1] == "/repo/Nymeria"


def test_local_commit_restarts_without_pull(world):
    world.head = SHA_LOCAL
    world.origin = SHA_OLD
    world.merge_base = SHA_OLD  # ahead
    summary = run_sync(world)
    commands = world.runner.commands()
    assert not any("pull" in c for c in commands)
    assert ("restart-slim",) in commands
    assert summary["targets"]["slim"]["action"] == "restarted"
    assert marker(world, "slim") == SHA_LOCAL


def test_dirty_tracked_backend_file_skips_the_whole_tick(world):
    world.origin = SHA_NEW
    world.merge_base = SHA_OLD
    world.status_out = " M Nymeria/nymeria/core/agent.py"
    summary = run_sync(world)
    commands = world.runner.commands()
    assert not any("pull" in c for c in commands)
    assert ("restart-slim",) not in commands
    assert summary["targets"]["slim"]["action"] == "skipped"
    assert "working tree busy" in summary["targets"]["slim"]["detail"]
    assert marker(world, "slim") == SHA_OLD


def test_untracked_files_never_block(world):
    world.head = SHA_LOCAL
    world.merge_base = SHA_OLD
    world.status_out = "?? Nymeria/docs/scratch-note.md"
    summary = run_sync(world)
    assert summary["targets"]["slim"]["action"] == "restarted"


def test_busy_target_defers_while_the_other_proceeds(world):
    world.head = SHA_LOCAL
    world.merge_base = SHA_OLD
    world.http.routes["http://slim/status/turns"] = (
        200,
        {"active_turns": 2, "interactive_active": 1, "busy_threads": []},
    )
    summary = run_sync(world)
    assert summary["targets"]["slim"]["action"] == "deferred"
    assert "busy" in summary["targets"]["slim"]["detail"]
    assert marker(world, "slim") == SHA_OLD
    assert summary["targets"]["docker"]["action"] == "restarted"
    assert marker(world, "docker") == SHA_LOCAL


def test_dependency_change_escalates_instead_of_restarting(world):
    world.head = SHA_LOCAL
    world.merge_base = SHA_OLD
    world.diff_names[(SHA_OLD, SHA_LOCAL)] = [
        "Nymeria/nymeria/core/agent.py",
        "Nymeria/requirements-docker.txt",
    ]
    summary = run_sync(world)
    assert summary["targets"]["slim"]["action"] == "escalated"
    assert "requirements-docker.txt" in summary["targets"]["slim"]["detail"]
    assert ("restart-slim",) not in world.runner.commands()
    assert marker(world, "slim") == SHA_OLD
    # Journaled once: the second tick knows it already stamped this commit.
    assert summary["targets"]["slim"]["already_stamped"] is False
    again = run_sync(world)
    assert again["targets"]["slim"]["already_stamped"] is True


def test_incoming_dependency_change_skips_the_pull_itself(world):
    world.origin = SHA_NEW
    world.merge_base = SHA_OLD
    world.diff_names[(SHA_OLD, SHA_NEW)] = ["Nymeria/pyproject.toml"]
    summary = run_sync(world)
    commands = world.runner.commands()
    assert not any("pull" in c for c in commands)
    assert summary["repo"]["pull_escalation"] == ["Nymeria/pyproject.toml"]
    # Nothing to restart either: desired stays at the current HEAD.
    assert summary["targets"]["slim"]["action"] == "noop"


def test_down_target_is_restarted_anyway(world):
    world.head = SHA_LOCAL
    world.merge_base = SHA_OLD
    world.http.routes["http://slim/status/turns"] = refused()
    summary = run_sync(world)
    assert summary["targets"]["slim"]["action"] == "restarted"
    assert marker(world, "slim") == SHA_LOCAL


def test_unverifiable_idleness_fails_safe(world):
    world.head = SHA_LOCAL
    world.merge_base = SHA_OLD
    world.http.routes["http://slim/status/turns"] = (401, None)
    summary = run_sync(world)
    assert summary["targets"]["slim"]["action"] == "deferred"
    assert ("restart-slim",) not in world.runner.commands()
    assert marker(world, "slim") == SHA_OLD


def test_failed_health_leaves_marker_for_retry(world):
    world.head = SHA_LOCAL
    world.merge_base = SHA_OLD
    world.http.routes["http://slim/health"] = refused()
    summary = run_sync(world)
    assert summary["targets"]["slim"]["action"] == "failed"
    assert marker(world, "slim") == SHA_OLD  # next tick retries


def test_diverged_history_does_nothing_destructive(world):
    world.origin = SHA_NEW
    world.merge_base = None
    summary = run_sync(world)
    commands = world.runner.commands()
    assert not any("pull" in c for c in commands)
    assert ("restart-slim",) not in commands
    assert summary["targets"]["slim"]["action"] == "skipped"
    assert "diverged" in summary["targets"]["slim"]["detail"]


def test_missing_marker_initializes_without_restart(world):
    (world.state_dir / "slim.commit").unlink()
    world.head = SHA_LOCAL
    world.merge_base = SHA_OLD
    summary = run_sync(world)
    assert summary["targets"]["slim"]["action"] == "initialized"
    assert ("restart-slim",) not in world.runner.commands()
    assert marker(world, "slim") == SHA_LOCAL  # future ticks diff from here


def test_running_extra_container_is_restarted_with_the_stack(world):
    world.head = SHA_LOCAL
    world.merge_base = SHA_OLD
    world.running_containers = ["nymeria-telegram-bot", "nymeria-api"]
    run_sync(world)
    assert ("docker", "restart", "nymeria-telegram-bot") in world.runner.commands()


def test_absent_extra_container_is_left_alone(world):
    world.head = SHA_LOCAL
    world.merge_base = SHA_OLD
    world.running_containers = ["nymeria-api"]
    run_sync(world)
    assert ("docker", "restart", "nymeria-telegram-bot") not in world.runner.commands()


def test_dry_run_reports_but_changes_nothing(world):
    world.origin = SHA_NEW
    world.merge_base = SHA_OLD
    summary = run_sync(world, dry_run=True)
    commands = world.runner.commands()
    assert not any("pull" in c for c in commands)
    assert ("restart-slim",) not in commands
    assert summary["targets"]["slim"]["action"] == "would-restart"
    assert marker(world, "slim") == SHA_OLD


def test_bearer_token_reaches_the_idle_probe_from_both_sources(world):
    world.head = SHA_LOCAL
    world.merge_base = SHA_OLD
    run_sync(world)
    slim_headers = world.http.headers_seen["http://slim/status/turns"][0]
    docker_headers = world.http.headers_seen["http://docker/status/turns"][0]
    assert slim_headers["Authorization"] == "Bearer tok-slim"
    # env_file source: value unquoted, other lines ignored.
    assert docker_headers["Authorization"] == "Bearer tok-docker"


def test_missing_token_fails_safe(world):
    world.head = SHA_LOCAL
    world.merge_base = SHA_OLD
    world.config["targets"][0]["token"] = {"file": "/nonexistent/token"}
    summary = run_sync(world)
    assert summary["targets"]["slim"]["action"] == "deferred"
    assert summary["targets"]["slim"]["detail"] == "token unavailable"


def test_overlapping_runs_serialize(world, tmp_path):
    lock = tmp_path / "lock"
    with deploy_sync.exclusive_lock(lock):
        with pytest.raises(deploy_sync.SyncBusy):
            with deploy_sync.exclusive_lock(lock):
                pass


def test_mark_deployed_advances_markers_and_clears_stamps(world):
    world.head = SHA_LOCAL
    for name in ("slim", "docker"):
        (world.state_dir / f"{name}.escalated").write_text(SHA_OLD, encoding="utf-8")
        (world.state_dir / f"{name}.failed").write_text(SHA_OLD, encoding="utf-8")

    marked = deploy_sync.mark_deployed(
        world.config, world.state_dir, None, runner=world.runner
    )

    assert marked == {"slim": SHA_LOCAL, "docker": SHA_LOCAL}
    for name in ("slim", "docker"):
        assert marker(world, name) == SHA_LOCAL
        assert not (world.state_dir / f"{name}.escalated").exists()
        assert not (world.state_dir / f"{name}.failed").exists()
    assert ("restart-slim",) not in world.runner.commands()
    # Escalation dead-end regression: after the ack, the next tick is a
    # plain noop instead of re-escalating forever.
    world.diff_names[(SHA_OLD, SHA_LOCAL)] = ["Nymeria/requirements-docker.txt"]
    summary = run_sync(world)
    assert summary["targets"]["slim"]["action"] == "noop"


def test_mark_deployed_scopes_to_named_targets(world):
    world.head = SHA_LOCAL
    deploy_sync.mark_deployed(
        world.config, world.state_dir, ["slim"], runner=world.runner
    )
    assert marker(world, "slim") == SHA_LOCAL
    assert marker(world, "docker") == SHA_OLD


def test_failed_restart_is_not_retried_for_the_same_commit(world):
    world.head = SHA_LOCAL
    world.merge_base = SHA_OLD
    world.http.routes["http://slim/health"] = refused()

    first = run_sync(world)
    assert first["targets"]["slim"]["action"] == "failed"
    restarts_after_first = world.runner.commands().count(("restart-slim",))

    second = run_sync(world)
    # No 5-minute restart storm into a broken boot.
    assert second["targets"]["slim"]["action"] == "held"
    assert world.runner.commands().count(("restart-slim",)) == restarts_after_first

    # A newer commit re-arms the target.
    world.head = SHA_NEW
    world.http.routes["http://slim/health"] = (200, {"status": "ok"})
    third = run_sync(world)
    assert third["targets"]["slim"]["action"] == "restarted"
    assert marker(world, "slim") == SHA_NEW
    # Success cleared the failure stamp.
    assert not (world.state_dir / "slim.failed").exists()


def test_pull_waits_for_every_checkout_consumer(world):
    world.origin = SHA_NEW
    world.merge_base = SHA_OLD
    world.http.routes["http://slim/status/turns"] = (
        200,
        {"active_turns": 1, "interactive_active": 0, "busy_threads": []},
    )
    summary = run_sync(world)
    commands = world.runner.commands()
    # Imports are lazy and the checkout is shared: no pull under a live turn,
    # and the idle docker target waits too rather than restarting onto a
    # tree that is about to move.
    assert not any("pull" in c for c in commands)
    assert ("restart-docker",) not in commands
    assert summary["targets"]["slim"]["action"] == "deferred"
    assert summary["targets"]["docker"]["action"] == "deferred"
    assert "shared checkout" in summary["targets"]["docker"]["detail"]


def test_interactive_occupancy_alone_defers(world):
    # Admission happens before the thread lock is taken, so this gap is a
    # real in-flight turn.
    world.head = SHA_LOCAL
    world.merge_base = SHA_OLD
    world.http.routes["http://slim/status/turns"] = (
        200,
        {"active_turns": 0, "interactive_active": 1, "background_jobs": 0},
    )
    summary = run_sync(world)
    assert summary["targets"]["slim"]["action"] == "deferred"
    assert ("restart-slim",) not in world.runner.commands()


def test_background_jobs_defer(world):
    world.head = SHA_LOCAL
    world.merge_base = SHA_OLD
    world.http.routes["http://slim/status/turns"] = (
        200,
        {"active_turns": 0, "interactive_active": 0, "background_jobs": 2},
    )
    summary = run_sync(world)
    assert summary["targets"]["slim"]["action"] == "deferred"


def test_malformed_activity_payload_fails_safe(world):
    world.head = SHA_LOCAL
    world.merge_base = SHA_OLD
    world.http.routes["http://slim/status/turns"] = (200, {"active_turns": "lots"})
    summary = run_sync(world)
    assert summary["targets"]["slim"]["action"] == "deferred"
    assert "malformed" in summary["targets"]["slim"]["detail"]


def test_fetch_failure_still_syncs_local_commits(world):
    world.fetch_fails = True
    world.head = SHA_LOCAL
    world.origin = SHA_LOCAL  # what the (stale) origin ref resolves to
    summary = run_sync(world)
    assert summary["repo"]["fetch_failed"] is True
    assert summary["targets"]["slim"]["action"] == "restarted"


def _write_config(world, tmp_path) -> Path:
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(world.config), encoding="utf-8")
    return config_file


def test_main_is_silent_on_an_all_noop_tick(world, tmp_path, monkeypatch, capsys):
    config_file = _write_config(world, tmp_path)
    monkeypatch.setattr(
        deploy_sync,
        "sync",
        lambda *a, **k: {
            "repo": {},
            "targets": {
                "slim": {"action": "noop", "detail": "current"},
                "docker": {"action": "noop", "detail": "current"},
            },
        },
    )
    rc = deploy_sync.main(
        ["--config", str(config_file), "--state-dir", str(world.state_dir)]
    )
    captured = capsys.readouterr()
    # systemd runs this every 5 minutes; a quiet tick must not journal.
    assert rc == 0
    assert captured.out == ""
    assert captured.err == ""


def test_main_exits_nonzero_when_a_target_failed(world, tmp_path, monkeypatch, capsys):
    config_file = _write_config(world, tmp_path)
    monkeypatch.setattr(
        deploy_sync,
        "sync",
        lambda *a, **k: {
            "repo": {},
            "targets": {"slim": {"action": "failed", "detail": "health timeout"}},
        },
    )
    rc = deploy_sync.main(
        ["--config", str(config_file), "--state-dir", str(world.state_dir)]
    )
    assert rc == 1
    assert "failed" in capsys.readouterr().err


def test_main_yields_quietly_to_a_running_peer(world, tmp_path, capsys):
    config_file = _write_config(world, tmp_path)
    with deploy_sync.exclusive_lock(world.state_dir / deploy_sync.LOCK_NAME):
        rc = deploy_sync.main(
            ["--config", str(config_file), "--state-dir", str(world.state_dir)]
        )
    assert rc == 0
    assert "another run is active" in capsys.readouterr().out


def test_held_target_is_loud_and_fails_the_run(world, tmp_path, monkeypatch, capsys):
    # A deployment that cannot boot must not look like a healthy idle
    # system: held targets go to stderr and fail the unit for systemd.
    config_file = _write_config(world, tmp_path)
    monkeypatch.setattr(
        deploy_sync,
        "sync",
        lambda *a, **k: {
            "repo": {},
            "targets": {"slim": {"action": "held", "detail": "failed earlier"}},
        },
    )
    rc = deploy_sync.main(
        ["--config", str(config_file), "--state-dir", str(world.state_dir)]
    )
    assert rc == 1
    assert "held" in capsys.readouterr().err


def test_probe_timeout_fails_safe(world):
    # A pegged API that cannot answer in 10s is BUSY, not down: restarting
    # here severs exactly the in-flight work the gate exists to protect.
    world.head = SHA_LOCAL
    world.merge_base = SHA_OLD
    world.http.routes["http://slim/status/turns"] = TimeoutError("timed out")
    summary = run_sync(world)
    assert summary["targets"]["slim"]["action"] == "deferred"
    assert "TimeoutError" in summary["targets"]["slim"]["detail"]
    assert ("restart-slim",) not in world.runner.commands()


def test_feature_branch_blocks_the_whole_tick(world):
    # `git pull` pulls the CURRENT branch's upstream: on a feature branch
    # the sync would mark origin/main deployed while pulling something else.
    world.branch = "feature"
    world.origin = SHA_NEW
    world.merge_base = SHA_OLD
    summary = run_sync(world)
    commands = world.runner.commands()
    assert not any("pull" in c for c in commands)
    assert ("restart-slim",) not in commands
    assert "feature" in summary["repo"]["blocked"]
    assert summary["targets"]["slim"]["action"] == "skipped"
    assert marker(world, "slim") == SHA_OLD


def test_detached_head_blocks_the_whole_tick(world):
    # An agent running `git checkout <sha>` in this shared checkout must
    # not cause production to be redeployed onto old code.
    world.branch = None
    world.head = SHA_LOCAL
    world.merge_base = SHA_OLD
    summary = run_sync(world)
    assert ("restart-slim",) not in world.runner.commands()
    assert "detached" in summary["repo"]["blocked"]
    assert marker(world, "slim") == SHA_OLD


def test_unresolvable_head_aborts_without_touching_markers(world):
    world.head_rc = 1
    world.head = "fatal: not a git repository"
    summary = run_sync(world)
    assert summary["repo"]["head_error"]
    assert summary["targets"]["slim"]["action"] == "skipped"
    # git's error text must never be written into a marker as a "commit".
    assert marker(world, "slim") == SHA_OLD


def test_corrupt_marker_reinitializes_instead_of_wedging(world):
    # A truncated marker (crash mid-write, disk full) must not flow into
    # `git diff`, where it would trip the diff-failed escalation sentinel
    # on every tick forever.
    (world.state_dir / "slim.commit").write_text("garba", encoding="utf-8")
    world.head = SHA_LOCAL
    world.merge_base = SHA_OLD
    summary = run_sync(world)
    assert summary["targets"]["slim"]["action"] == "initialized"
    assert marker(world, "slim") == SHA_LOCAL


def test_unknown_marker_commit_escalates_not_restarts(world):
    # A rewritten/garbage-collected marker commit makes the range
    # unverifiable; restarting on "cannot prove it is safe" is the wrong
    # default.
    stale = "d" * 40
    (world.state_dir / "slim.commit").write_text(stale + "\n", encoding="utf-8")
    world.head = SHA_LOCAL
    world.merge_base = SHA_OLD
    world.diff_names[(stale, SHA_LOCAL)] = None  # git diff fails
    summary = run_sync(world)
    assert summary["targets"]["slim"]["action"] == "escalated"
    assert ("restart-slim",) not in world.runner.commands()


def test_idle_is_reprobed_after_the_pull(world):
    # The pre-pull verdict is stale by the time the tree has moved; a turn
    # that started during the pull must defer the restart.
    world.origin = SHA_NEW
    world.merge_base = SHA_OLD
    run_sync(world)
    # One probe for the pull gate, a second for the restart decision.
    assert len(world.http.headers_seen["http://slim/status/turns"]) == 2


def test_pull_failure_is_reported_and_nothing_restarts(world):
    world.origin = SHA_NEW
    world.merge_base = SHA_OLD
    world.pull_ok = False
    summary = run_sync(world)
    assert summary["repo"]["pull_failed"]
    assert ("restart-slim",) not in world.runner.commands()
    assert marker(world, "slim") == SHA_OLD


def test_restart_command_failure_stamps_and_reports(world):
    world.head = SHA_LOCAL
    world.merge_base = SHA_OLD
    world.restart_rc = 1
    summary = run_sync(world)
    assert summary["targets"]["slim"]["action"] == "failed"
    assert "restart command failed" in summary["targets"]["slim"]["detail"]
    assert marker(world, "slim") == SHA_OLD
    # The failure stamp holds the target on the next tick too.
    assert run_sync(world)["targets"]["slim"]["action"] == "held"


def test_env_token_is_last_wins_with_export_and_comments(world, tmp_path):
    # dotenv semantics: the token ROTATES, and an appended rotation must
    # beat the stale line above it.
    env_file = tmp_path / "env.docker"
    env_file.write_text(
        "NYMERIA_SERVICE_TOKEN=stale-token\n"
        "OTHER=x\n"
        'export NYMERIA_SERVICE_TOKEN="fresh-token" # rotated 2027\n',
        encoding="utf-8",
    )
    world.config["targets"][1]["token"] = {
        "env_file": str(env_file),
        "key": "NYMERIA_SERVICE_TOKEN",
    }
    world.head = SHA_LOCAL
    world.merge_base = SHA_OLD
    run_sync(world)
    headers = world.http.headers_seen["http://docker/status/turns"][0]
    assert headers["Authorization"] == "Bearer fresh-token"


def test_empty_token_file_fails_safe(world, tmp_path):
    empty = tmp_path / "empty-token.txt"
    empty.write_text("\n", encoding="utf-8")
    world.config["targets"][0]["token"] = {"file": str(empty)}
    world.head = SHA_LOCAL
    world.merge_base = SHA_OLD
    summary = run_sync(world)
    # "" would send "Bearer " and 401; absent is the honest verdict.
    assert summary["targets"]["slim"]["action"] == "deferred"
    assert summary["targets"]["slim"]["detail"] == "token unavailable"


def test_mark_deployed_refuses_an_unresolvable_head(world):
    world.head_rc = 1
    world.head = "fatal: ambiguous argument 'HEAD'"
    with pytest.raises(SystemExit):
        deploy_sync.mark_deployed(
            world.config, world.state_dir, None, runner=world.runner
        )
    assert marker(world, "slim") == SHA_OLD
