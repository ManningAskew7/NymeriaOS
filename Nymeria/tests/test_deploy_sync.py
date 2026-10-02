"""Behavior tests for the deploy-sync tick (`scripts/deploy_sync.py`).

Plan behaviors B4-B13 in ``tmp/deploy-sync-plan.md``. The script restarts
LIVE deployments, so these tests assert observable outcomes: which commands
ran (pull, restart argv), what the marker files say afterwards, and what
the summary reports; never internal call counts for their own sake. The
git/HTTP/sleep seams are injected fakes; nothing here touches a real repo,
socket, or service (one test drives the production runner over a throwaway
repository it builds under tmp_path).
"""
from __future__ import annotations

import importlib.util
import json
import os
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
SHA_THIRD = "d" * 40
FILES_HOST = "f" * 16
FILES_OTHER = "e" * 16


def refused() -> urllib.error.URLError:
    """What production urllib actually raises for a refused connection."""
    return urllib.error.URLError(ConnectionRefusedError(111, "refused"))


class FakeWorld:
    # Wired by the ``world`` fixture.
    runner: "FakeRunner"
    http: "FakeHttp"
    fingerprint: "FakeFingerprint"
    state_dir: Path
    config: dict

    def __init__(self):
        self.branch: str | None = "main"  # None = detached HEAD
        self.head = SHA_OLD
        self.head_rc = 0
        self.origin = SHA_OLD
        self.merge_base: str | None = SHA_OLD
        self.status_out = ""
        self.status_rc = 0
        # Range -> changed file names; a value of None makes `git diff`
        # FAIL for that range (unknown/rewritten commit).
        self.diff_names: dict[tuple[str, str], list[str] | None] = {}
        self.fetch_fails = False
        self.pull_ok = True
        self.running_containers: list[str] = []
        self.restart_rc = 0
        # restart argv[0] -> side effect run when that restart command runs
        # (a commit or an edit landing while the target boots).
        self.on_restart: dict[str, object] = {}


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
                return w.status_rc, w.status_out
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
            hook = w.on_restart.get(argv[0])
            if callable(hook):
                hook()
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
        if callable(entry):
            entry = entry()
        if isinstance(entry, Exception):
            raise entry
        return entry


class FakeFingerprint:
    """The host-checkout digest seam (``checkout_fingerprint``). An
    exception as the value is raised, the way the real walk raises
    ``PackageUnreadable``."""

    def __init__(self, value):
        self.value = value
        self.paths: list[str] = []

    def __call__(self, package_dir):
        self.paths.append(str(package_dir))
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class FakeClock:
    """A monotonic clock that only moves when told: ``sleep`` advances it,
    and a test can make each probe cost time too."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def world(tmp_path):
    w = FakeWorld()
    w.runner = FakeRunner(w)
    w.http = FakeHttp()
    w.fingerprint = FakeFingerprint(FILES_HOST)
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
        fingerprint=w.fingerprint,
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

    def turns():
        # Down until the restart brings it up (the post-restart verify
        # reads this endpoint too, #423).
        if ("restart-slim",) in world.runner.commands():
            return (200, {"active_turns": 0, "interactive_active": 0})
        return refused()

    world.http.routes["http://slim/status/turns"] = turns
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
        (world.state_dir / f"{name}.unverified").write_text(SHA_OLD, encoding="utf-8")

    marked = deploy_sync.mark_deployed(
        world.config, world.state_dir, None, runner=world.runner
    )

    assert marked == {"slim": SHA_LOCAL, "docker": SHA_LOCAL}
    for name in ("slim", "docker"):
        assert marker(world, name) == SHA_LOCAL
        assert not (world.state_dir / f"{name}.escalated").exists()
        assert not (world.state_dir / f"{name}.failed").exists()
        assert not (world.state_dir / f"{name}.unverified").exists()
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


def test_claude_code_jobs_defer(world):
    """#339: a detached Claude Code run counts as busy; an older API without
    the key still gates on the three counters."""
    world.head = SHA_LOCAL
    world.merge_base = SHA_OLD
    world.http.routes["http://slim/status/turns"] = (
        200,
        {"active_turns": 0, "interactive_active": 0, "background_jobs": 0,
         "claude_code_jobs": 1},
    )
    summary = run_sync(world)
    assert summary["targets"]["slim"]["action"] == "deferred"
    assert "claude_code=1" in summary["targets"]["slim"]["detail"]
    assert ("restart-slim",) not in world.runner.commands()

    world.http.routes["http://slim/status/turns"] = (
        200,
        {"active_turns": 0, "interactive_active": 0, "background_jobs": 0,
         "claude_code_jobs": "one"},
    )
    summary = run_sync(world)
    assert summary["targets"]["slim"]["action"] == "deferred"
    assert "malformed" in summary["targets"]["slim"]["detail"]


def _add_runner_target(world):
    """A turns-less target (the Claude Code runner unit, #335): idle-probed
    on its unauthenticated /health, no token."""
    world.config["targets"].append(
        {
            "name": "runner",
            "health_url": "http://runner/health",
            "restart": ["restart-runner"],
        }
    )
    (world.state_dir / "runner.commit").write_text(SHA_OLD + "\n", encoding="utf-8")
    world.http.routes["http://runner/health"] = (
        200, {"status": "ok", "claude": True, "active_jobs": 0, "code_version": "abc"},
    )
    world.head = SHA_LOCAL
    world.merge_base = SHA_OLD


def test_runner_target_restarts_when_its_health_reports_no_jobs(world):
    _add_runner_target(world)
    summary = run_sync(world)
    assert summary["targets"]["runner"]["action"] == "restarted"
    assert ("restart-runner",) in world.runner.commands()
    assert marker(world, "runner") == SHA_LOCAL
    # No bearer token was sent to the unauthenticated health probe.
    assert all("Authorization" not in h for h in world.http.headers_seen["http://runner/health"])


def test_runner_target_defers_while_a_run_is_in_flight(world):
    _add_runner_target(world)
    world.http.routes["http://runner/health"] = (200, {"status": "ok", "active_jobs": 2})
    summary = run_sync(world)
    assert summary["targets"]["runner"]["action"] == "deferred"
    assert "active_jobs=2" in summary["targets"]["runner"]["detail"]
    assert ("restart-runner",) not in world.runner.commands()
    assert marker(world, "runner") == SHA_OLD
    # The other targets are unaffected by the runner's state.
    assert summary["targets"]["slim"]["action"] == "restarted"


def test_runner_without_a_job_count_is_never_restarted_blind(world):
    _add_runner_target(world)
    world.http.routes["http://runner/health"] = (200, {"status": "ok", "claude": True})
    summary = run_sync(world)
    assert summary["targets"]["runner"]["action"] == "deferred"
    assert "active_jobs" in summary["targets"]["runner"]["detail"]
    assert ("restart-runner",) not in world.runner.commands()


def test_down_runner_is_restarted_anyway(world):
    """Refused before the restart means down (restart brings it up
    current); the verify then sees it healthy on the desired commit."""
    _add_runner_target(world)

    def health():
        if ("restart-runner",) in world.runner.commands():
            return (200, {"status": "ok", "active_jobs": 0, "code_version": SHA_LOCAL})
        return refused()

    world.http.routes["http://runner/health"] = health
    summary = run_sync(world)
    assert summary["targets"]["runner"]["action"] == "restarted"
    assert ("restart-runner",) in world.runner.commands()
    assert marker(world, "runner") == SHA_LOCAL


def test_a_restart_that_comes_up_on_other_code_is_not_marked_deployed(world):
    """The old process answers /health during its shutdown, and a restart
    on the wrong checkout answers forever: neither is 'healthy' for a
    target that reports code_version."""
    _add_runner_target(world)
    world.http.routes["http://runner/health"] = (
        200, {"status": "ok", "active_jobs": 0, "code_version": SHA_OLD},
    )
    summary = run_sync(world)
    assert summary["targets"]["runner"]["action"] == "failed"
    assert "code_version" in summary["targets"]["runner"]["detail"]
    assert marker(world, "runner") == SHA_OLD
    # A target that reports no version (the API) is verified as before.
    assert summary["targets"]["slim"]["action"] == "restarted"


def test_a_new_runner_target_initializes_from_its_reported_version(world):
    """A fresh marker is not 'presumed current' for a target that says
    what it booted from: a stale runner is restarted on the same tick."""
    _add_runner_target(world)
    (world.state_dir / "runner.commit").unlink()
    booted = "d" * 40

    def health():
        if ("restart-runner",) in world.runner.commands():
            return (200, {"status": "ok", "active_jobs": 0, "code_version": SHA_LOCAL})
        return (200, {"status": "ok", "active_jobs": 0, "code_version": booted})

    world.http.routes["http://runner/health"] = health
    summary = run_sync(world)
    assert summary["targets"]["runner"]["action"] == "restarted"
    assert ("restart-runner",) in world.runner.commands()
    assert marker(world, "runner") == SHA_LOCAL
    # A target reporting no version keeps the old presumption.
    (world.state_dir / "slim.commit").unlink()
    summary = run_sync(world)
    assert summary["targets"]["slim"]["action"] == "initialized"


def test_pull_waits_for_a_busy_runner_too(world):
    _add_runner_target(world)
    world.head = SHA_OLD
    world.origin = SHA_NEW
    world.merge_base = SHA_OLD
    world.diff_names[(SHA_OLD, SHA_NEW)] = ["Nymeria/nymeria/tools/claude_code_bridge.py"]
    world.http.routes["http://runner/health"] = (200, {"status": "ok", "active_jobs": 1})
    summary = run_sync(world)
    assert ("git", "-C", "/repo", "pull", "--ff-only") not in world.runner.commands()
    assert summary["targets"]["runner"]["action"] == "deferred"
    assert "active_jobs=1" in summary["targets"]["runner"]["detail"]
    assert "runner" in summary["targets"]["slim"]["detail"]


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
    pull = ("git", "-C", "/repo", "pull", "--ff-only")

    def turns():
        if pull in world.runner.commands():
            return (200, {"active_turns": 1, "interactive_active": 0})
        return (200, {"active_turns": 0, "interactive_active": 0})

    world.http.routes["http://slim/status/turns"] = turns
    summary = run_sync(world)
    assert pull in world.runner.commands()  # idle at the pull gate
    assert summary["targets"]["slim"]["action"] == "deferred"
    assert "busy (turns=1" in summary["targets"]["slim"]["detail"]
    assert ("restart-slim",) not in world.runner.commands()
    assert marker(world, "slim") == SHA_OLD


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
    with pytest.raises(SystemExit, match="cannot resolve HEAD; nothing marked"):
        deploy_sync.mark_deployed(
            world.config, world.state_dir, None, runner=world.runner
        )
    assert marker(world, "slim") == SHA_OLD


# ---------------------------------------------------------------------------
# #423: a restart counts only once the target reports the code it was
# restarted onto (behaviors V5-V15 of the it34 plan). The API reports its
# booted commit and a digest of its boot-time file fingerprint on
# /status/turns (admin token); the host digest is the ``fingerprint`` seam
# (FILES_HOST unless a test changes it). Edges skipped on purpose: a sha256
# repository (nothing deploys there at all, a pre-existing gap filed as a
# follow-up) and a mount that does not preserve stat (no target has one).


def _idle(**identity):
    """An idle /status/turns payload, plus whatever identity fields given."""
    return (
        200,
        {"active_turns": 0, "interactive_active": 0, "background_jobs": 0,
         "claude_code_jobs": 0, "busy_threads": [], **identity},
    )


def _phased(world, name, before, *after):
    """A route answering ``before`` until ``restart-<name>`` ran, then each
    of ``after`` in turn (the last one repeats)."""
    queue = list(after)

    def route():
        if (f"restart-{name}",) not in world.runner.commands():
            return before
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return route


def _local_commit(world):
    """HEAD is a local commit ahead of every marker: desired = SHA_LOCAL."""
    world.head = SHA_LOCAL
    world.merge_base = SHA_OLD


def _stamp(world, name):
    path = world.state_dir / f"{name}.failed"
    return path.read_text(encoding="utf-8").strip() if path.exists() else None


def _run_main(world, tmp_path, capsys, *args):
    config_file = _write_config(world, tmp_path)
    rc = deploy_sync.main(
        ["--config", str(config_file), "--state-dir", str(world.state_dir), *args],
        runner=world.runner,
        http_get=world.http,
        sleep=lambda seconds: None,
        fingerprint=world.fingerprint,
    )
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


@pytest.mark.parametrize(
    "booted, cause",
    [
        (SHA_OLD, "still the previous commit"),  # the restart did not take
        (SHA_THIRD, "!= desired cccccccccccc (install or unit runs another tree?)"),
    ],
)
def test_a_backend_on_other_code_after_restart_is_failed_and_held(world, booted, cause):
    """V5: answering /health is not enough; the booted commit on the
    authenticated /status/turns must be the desired one."""
    _local_commit(world)
    world.http.routes["http://slim/status/turns"] = _phased(
        world, "slim",
        _idle(code_version=SHA_OLD, code_fingerprint=FILES_HOST),
        _idle(code_version=booted, code_fingerprint=FILES_HOST),
    )

    summary = run_sync(world)

    slim = summary["targets"]["slim"]
    assert ("restart-slim",) in world.runner.commands()
    assert slim["action"] == "failed"
    assert cause in slim["detail"]
    assert booted[:12] in slim["detail"] and SHA_LOCAL[:12] in slim["detail"]
    assert "--mark-deployed" in slim["detail"]  # the remedy rides along
    assert marker(world, "slim") == SHA_OLD
    assert _stamp(world, "slim") == SHA_LOCAL
    # The identity was read with the target's bearer; /health got none.
    assert all(
        h == {"Authorization": "Bearer tok-slim"}
        for h in world.http.headers_seen["http://slim/status/turns"]
    )
    assert all(h == {} for h in world.http.headers_seen["http://slim/health"])
    # No restart storm: the next tick holds the target, loudly.
    restarts = world.runner.commands().count(("restart-slim",))
    assert run_sync(world)["targets"]["slim"]["action"] == "held"
    assert world.runner.commands().count(("restart-slim",)) == restarts


def test_booted_files_are_compared_with_the_checkout(world):
    """V6: Docker reports no commit (its mounts carry no .git), so its file
    digest decides: a container that lost its bind mount boots the image's
    baked copy, which answers /health just the same."""
    _local_commit(world)
    world.http.routes["http://docker/status/turns"] = _phased(
        world, "docker",
        _idle(code_version=None, code_fingerprint=FILES_HOST),
        _idle(code_version=None, code_fingerprint=FILES_OTHER),
    )
    world.http.routes["http://slim/status/turns"] = _phased(
        world, "slim",
        _idle(code_version=SHA_OLD, code_fingerprint=FILES_HOST),
        _idle(code_version=SHA_LOCAL, code_fingerprint=FILES_HOST),
    )

    summary = run_sync(world)

    docker = summary["targets"]["docker"]
    assert docker["action"] == "failed"
    assert docker["detail"].startswith(
        f"booted files {FILES_OTHER} differ from the checkout's {FILES_HOST} "
        "(stale image, missing bind mount, or an install from another tree?)"
    )
    assert marker(world, "docker") == SHA_OLD
    assert _stamp(world, "docker") == SHA_LOCAL
    # Both identity fields matched for slim: the line says what was proven.
    slim = summary["targets"]["slim"]
    assert slim["action"] == "restarted"
    assert slim["detail"] == f"{SHA_OLD[:12]} -> {SHA_LOCAL[:12]}, verified commit+files"
    # The host digest describes the checkout's package by default.
    assert world.fingerprint.paths
    assert set(world.fingerprint.paths) == {"/repo/Nymeria/nymeria"}


def test_matching_files_verify_a_target_that_reports_no_commit(world):
    _local_commit(world)
    world.http.routes["http://docker/status/turns"] = _phased(
        world, "docker",
        _idle(code_version=None, code_fingerprint=FILES_OTHER),
        _idle(code_version=None, code_fingerprint=FILES_HOST),
    )
    summary = run_sync(world)
    docker = summary["targets"]["docker"]
    assert docker["action"] == "restarted"
    assert docker["detail"].endswith(", verified files")
    assert marker(world, "docker") == SHA_LOCAL
    assert _stamp(world, "docker") is None


UNREADABLE = "/repo/Nymeria/nymeria/core/locked"
UNREADABLE_NOTE = (
    f"files not checked: cannot read {UNREADABLE} in the checkout as this "
    "user; fix its permissions"
)


def test_an_unreadable_checkout_directory_is_unverified_not_a_mismatch(
    world, tmp_path, capsys
):
    """S-LOW-3: deploy-sync runs as the host user and a container may not;
    a package directory only one of them can read used to shorten one
    count, which read as "booted files differ (stale image, missing bind
    mount ...)" and held the target. Now the walk refuses to digest and the
    line says which path to fix; the commit still verifies slim."""
    _local_commit(world)
    world.fingerprint.value = deploy_sync.PackageUnreadable(UNREADABLE)
    world.http.routes["http://docker/status/turns"] = _idle(
        code_version=None, code_fingerprint=FILES_OTHER
    )
    world.http.routes["http://slim/status/turns"] = _phased(
        world, "slim",
        _idle(code_version=SHA_OLD, code_fingerprint=FILES_OTHER),
        _idle(code_version=SHA_LOCAL, code_fingerprint=FILES_OTHER),
    )

    summary = run_sync(world)

    docker, slim = summary["targets"]["docker"], summary["targets"]["slim"]
    assert docker["action"] == "restarted"
    assert docker["detail"] == (
        f"{SHA_OLD[:12]} -> {SHA_LOCAL[:12]}, unverified ({UNREADABLE_NOTE})"
    )
    assert slim["action"] == "restarted"
    assert slim["detail"] == (
        f"{SHA_OLD[:12]} -> {SHA_LOCAL[:12]}, verified commit ({UNREADABLE_NOTE})"
    )
    assert _stamp(world, "docker") is None and _stamp(world, "slim") is None
    # The host digest describes the checkout's own package.
    assert set(world.fingerprint.paths) == {"/repo/Nymeria/nymeria"}

    # --check says the same, read-only, and passes (nothing is wrong with
    # the code, only with what this user can read).
    rc, out, err = _run_main(world, tmp_path, capsys, "--check")
    assert rc == 0, err
    assert f"deploy_sync: check: docker: unverified ({UNREADABLE_NOTE}) [" in out


def _move_head(world):
    world.head = SHA_NEW


def _dirty_tree(world):
    world.status_out = " M Nymeria/nymeria/core/agent.py"


def _add_package_file(world):
    world.fingerprint.value = FILES_OTHER  # an untracked file appeared


def _lose_head(world):
    world.head_rc = 1  # a ref being rewritten, a lock file mid-update


GIT_STATUS_FATAL = "fatal: index file corrupt"


def _fail_git_status(world):
    world.status_rc, world.status_out = 128, GIT_STATUS_FATAL


def _unverified_stamp(world, name):
    path = world.state_dir / f"{name}.unverified"
    return path.read_text(encoding="utf-8").strip() if path.exists() else None


@pytest.mark.parametrize(
    "during_restart, booted, cause, counted",
    [
        (_move_head, {"code_version": SHA_NEW, "code_fingerprint": FILES_OTHER},
         f"HEAD moved to {SHA_NEW[:12]}", False),
        (_dirty_tree, {"code_version": SHA_LOCAL, "code_fingerprint": FILES_OTHER},
         "tracked files were modified", True),
        (_add_package_file, {"code_version": SHA_LOCAL, "code_fingerprint": FILES_OTHER},
         "files under the package changed", True),
        (_lose_head, {"code_version": SHA_THIRD, "code_fingerprint": FILES_HOST},
         "HEAD became unreadable", False),
        (_fail_git_status, {"code_version": SHA_LOCAL, "code_fingerprint": FILES_HOST},
         f"git status failed ({GIT_STATUS_FATAL})", False),
    ],
    ids=["head-moved", "tree-dirtied", "package-file-added", "head-unreadable",
         "git-status-failed"],
)
def test_a_mismatch_the_checkout_explains_is_unverified_and_retried(
    world, tmp_path, capsys, during_restart, booted, cause, counted
):
    """V7: a parallel session committing or editing while the target boots
    makes it load newer files than the pre-restart digest: a real but
    self-healing mismatch. No marker, no failure stamp, exit 0; the next
    clean tick restarts and verifies. The line LEADS with what the checkout
    did, then the facts, never the after-restart wiring guesses (a lost
    bind mount, another tree) that would send a reader the wrong way. Only
    changed files arm the cap (the ``.unverified`` count): a commit landing
    or git failing mid-update never does (delta review D1, D2)."""
    _local_commit(world)
    world.on_restart["restart-slim"] = lambda: during_restart(world)
    world.http.routes["http://slim/status/turns"] = _phased(
        world, "slim",
        _idle(code_version=SHA_OLD, code_fingerprint=FILES_HOST),
        _idle(**booted),
    )

    rc, out, err = _run_main(world, tmp_path, capsys)

    assert rc == 0
    line = next(x for x in out.splitlines() if x.startswith("deploy_sync: slim: "))
    assert line.startswith(
        f"deploy_sync: slim: unverified ({SHA_OLD[:12]} -> {SHA_LOCAL[:12]} "
        f"not confirmed: {cause} during the restart (it reported commit "
        f"{booted['code_version'][:12]} files {booted['code_fingerprint'][:12]}, "
        f"the checkout held {SHA_LOCAL[:12]} files {FILES_HOST[:12]} before it)"
    )
    assert "another tree" not in line and "bind mount" not in line
    assert "slim" not in err
    assert marker(world, "slim") == SHA_OLD
    assert _stamp(world, "slim") is None
    assert _unverified_stamp(world, "slim") == (SHA_LOCAL if counted else None)

    # Next tick: the tree is clean again and the target reports exactly
    # what the checkout holds now.
    world.on_restart.clear()
    world.status_rc, world.status_out = 0, ""
    world.head_rc = 0
    world.http.routes["http://slim/status/turns"] = _idle(
        code_version=world.head, code_fingerprint=world.fingerprint.value
    )
    restarts = world.runner.commands().count(("restart-slim",))
    summary = run_sync(world)
    assert summary["targets"]["slim"]["action"] == "restarted"
    assert summary["targets"]["slim"]["detail"].endswith("verified commit+files")
    assert world.runner.commands().count(("restart-slim",)) == restarts + 1
    assert marker(world, "slim") == world.head
    assert _unverified_stamp(world, "slim") is None  # success clears the count


def test_a_checkout_that_changes_during_every_boot_is_held_on_the_second_try(
    world, tmp_path, capsys
):
    """S-MED-1: something writing into the package during every boot (an
    agent's scratch modules, say) made every tick an exit-0 "unverified"
    restart of the target, every 5 minutes, forever. The second consecutive
    unverified restart for the SAME commit is failed then held, naming the
    likely cause and the remedy."""
    _local_commit(world)
    churn = iter(f"{i:016x}" for i in range(1, 100))

    def write_into_the_package():
        world.fingerprint.value = next(churn)

    def turns():
        if ("restart-slim",) not in world.runner.commands():
            return _idle(code_version=SHA_OLD, code_fingerprint=FILES_HOST)
        # It boots whatever the churn left on disk.
        return _idle(code_version=SHA_LOCAL, code_fingerprint=world.fingerprint.value)

    world.on_restart["restart-slim"] = write_into_the_package
    world.http.routes["http://slim/status/turns"] = turns

    rc, out, _ = _run_main(world, tmp_path, capsys)
    assert rc == 0
    assert "deploy_sync: slim: unverified (" in out
    assert _stamp(world, "slim") is None

    rc, out, err = _run_main(world, tmp_path, capsys)
    assert rc == 1
    line = next(x for x in err.splitlines() if x.startswith("deploy_sync: slim: "))
    assert line.startswith(
        "deploy_sync: slim: failed (files under the package changed during a "
        f"second restart for {SHA_LOCAL[:12]}: something keeps changing the "
        "checkout while the target boots (files written into the package?); "
        "stop it, restart, then --mark-deployed"
    )
    assert marker(world, "slim") == SHA_OLD
    assert _stamp(world, "slim") == SHA_LOCAL
    assert _unverified_stamp(world, "slim") is None

    # Held: no third restart into the churn.
    restarts = world.runner.commands().count(("restart-slim",))
    assert run_sync(world)["targets"]["slim"]["action"] == "held"
    assert world.runner.commands().count(("restart-slim",)) == restarts


def test_an_unverified_count_is_per_commit(world, tmp_path, capsys):
    """The cap counts restarts of one commit: a new commit landing during
    a restart is the ordinary case (every commit moves HEAD), so a stale
    count from the previous commit never turns the next one into a held
    target."""
    _local_commit(world)
    (world.state_dir / "slim.unverified").write_text(SHA_OLD, encoding="utf-8")
    world.on_restart["restart-slim"] = lambda: _dirty_tree(world)
    world.http.routes["http://slim/status/turns"] = _phased(
        world, "slim",
        _idle(code_version=SHA_OLD, code_fingerprint=FILES_HOST),
        _idle(code_version=SHA_LOCAL, code_fingerprint=FILES_OTHER),
    )
    summary = run_sync(world)
    assert summary["targets"]["slim"]["action"] == "unverified"
    assert _unverified_stamp(world, "slim") == SHA_LOCAL
    assert _stamp(world, "slim") is None


@pytest.mark.parametrize(
    "second_cause, cause",
    [
        (_move_head, f"HEAD moved to {SHA_NEW[:12]}"),
        (_lose_head, "HEAD became unreadable"),
        (_fail_git_status, f"git status failed ({GIT_STATUS_FATAL})"),
    ],
    ids=["head-moved", "head-unreadable", "git-status-failed"],
)
def test_a_commit_landing_during_the_second_restart_is_never_held(
    world, tmp_path, capsys, second_cause, cause
):
    """Delta review D1: the cap is for something rewriting the package at
    every boot. A commit landing during the second restart of a commit
    (or git failing mid-update) is not that: it stays `unverified`, exit
    0, and the next tick deploys whatever HEAD now is. Holding it named
    the wrong remedy for a target that needed nothing."""
    _local_commit(world)

    def turns():
        if ("restart-slim",) not in world.runner.commands():
            return _idle(code_version=SHA_OLD, code_fingerprint=FILES_HOST)
        # It boots whatever the checkout holds when it starts.
        return _idle(code_version=world.head, code_fingerprint=world.fingerprint.value)

    world.http.routes["http://slim/status/turns"] = turns
    # Tick 1: files under the package change mid-boot, which counts.
    world.on_restart["restart-slim"] = lambda: _add_package_file(world)
    rc, _, _ = _run_main(world, tmp_path, capsys)
    assert rc == 0
    assert _unverified_stamp(world, "slim") == SHA_LOCAL

    # Tick 2, the same commit again: this time a commit lands mid-boot.
    world.on_restart["restart-slim"] = lambda: second_cause(world)
    rc, out, err = _run_main(world, tmp_path, capsys)

    assert rc == 0, err
    line = next(x for x in out.splitlines() if x.startswith("deploy_sync: slim: "))
    assert line.startswith(
        f"deploy_sync: slim: unverified ({SHA_OLD[:12]} -> {SHA_LOCAL[:12]} "
        f"not confirmed: {cause} during the restart ("
    )
    assert "not held" in line
    assert "slim" not in err
    assert _stamp(world, "slim") is None
    assert marker(world, "slim") == SHA_OLD

    # Tick 3: the checkout settled; the target restarts and verifies
    # whatever HEAD is now.
    world.on_restart.clear()
    world.status_rc, world.status_out, world.head_rc = 0, "", 0
    summary = run_sync(world)
    assert summary["targets"]["slim"]["action"] == "restarted"
    assert summary["targets"]["slim"]["detail"].endswith("verified commit+files")
    assert marker(world, "slim") == world.head
    assert _unverified_stamp(world, "slim") is None


GIT_WARNING = (
    "warning: could not open directory 'Nymeria/nymeria/locked/': "
    "Permission denied"
)


def test_git_noise_is_never_a_modified_tracked_file(world):
    """Delta review D2: the runner merges stderr into the output, and git
    prints this warning with exit 0 for an untracked directory it cannot
    read. Read as a path, it held every tick as "working tree busy" and,
    in the post-restart re-check, turned a verified restart unverified."""
    _local_commit(world)
    world.status_out = f"?? Nymeria/nymeria/scratch.py\n{GIT_WARNING}"
    world.http.routes["http://slim/status/turns"] = _phased(
        world, "slim",
        _idle(code_version=SHA_OLD, code_fingerprint=FILES_HOST),
        _idle(code_version=SHA_LOCAL, code_fingerprint=FILES_HOST),
    )

    summary = run_sync(world)

    slim = summary["targets"]["slim"]
    assert slim["action"] == "restarted"
    assert slim["detail"] == f"{SHA_OLD[:12]} -> {SHA_LOCAL[:12]}, verified commit+files"
    assert marker(world, "slim") == SHA_LOCAL
    assert _unverified_stamp(world, "slim") is None


def test_a_failing_git_status_skips_the_tick_loudly(world, tmp_path, capsys):
    """Delta review D2: a non-zero `git status` proves nothing clean. Its
    fatal line is neither a dirty path (the old reading: "1 modified tracked
    file") nor, once noise is filtered, an empty and therefore clean tree:
    the tick skips, says why, and fails the run like an unresolvable HEAD."""
    _local_commit(world)
    world.status_rc = 128
    world.status_out = "fatal: detected dubious ownership in repository at '/repo'"

    rc, out, err = _run_main(world, tmp_path, capsys)

    assert rc == 1
    assert (
        "deploy_sync: git status failed: fatal: detected dubious ownership "
        "in repository at '/repo'"
    ) in err
    for name in ("slim", "docker"):
        assert (
            f"deploy_sync: {name}: skipped (git status failed: fatal: detected "
            "dubious ownership"
        ) in out
        assert marker(world, name) == SHA_OLD
    assert not any(c[0].startswith("restart-") for c in world.runner.commands())


def test_the_real_runner_reads_porcelain_not_git_noise(tmp_path, monkeypatch):
    """Delta review D2 through the production runner and a real git: an
    unstaged edit on the FIRST status line keeps its status column (the
    runner once stripped the whole output, turning " M path" into "M path"),
    the warning git writes for an unreadable untracked directory is not a
    path, and a failing git raises rather than reading clean."""
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))  # no global config or hooks
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    repo = tmp_path / "repo"
    pkg = repo / "Nymeria" / "nymeria"
    pkg.mkdir(parents=True)
    (pkg / "a.py").write_text("a = 1\n", encoding="utf-8")
    (pkg / "b.py").write_text("b = 1\n", encoding="utf-8")

    def git(*args):
        rc, out = deploy_sync.run_command(["git", "-C", str(repo), *args])
        assert rc == 0, out

    git("init", "-q")
    git("add", ".")
    git("-c", "user.name=t", "-c", "user.email=t@example.invalid",
        "commit", "-q", "--no-verify", "-m", "init")
    (pkg / "a.py").write_text("a = 2\n", encoding="utf-8")  # " M", first line
    (pkg / "c.py").write_text("c = 1\n", encoding="utf-8")  # untracked
    locked = pkg / "locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        rc, raw = deploy_sync.git(str(repo), "status", "--porcelain", "--", "Nymeria")
        assert rc == 0
        if os.geteuid() != 0:  # root reads it, so git has nothing to warn about
            assert "warning: could not open directory" in raw
        assert deploy_sync.dirty_tracked_paths(str(repo), ["Nymeria"]) == [
            "Nymeria/nymeria/a.py"
        ]
    finally:
        locked.chmod(0o755)

    with pytest.raises(deploy_sync.GitStatusFailed, match=r"^fatal: "):
        deploy_sync.dirty_tracked_paths(str(tmp_path / "missing"), ["Nymeria"])


@pytest.mark.parametrize(
    "during_restart, cause",
    [
        (_dirty_tree, "tracked files were modified"),
        (_move_head, f"HEAD moved to {SHA_NEW[:12]}"),
    ],
    ids=["tree-dirtied", "head-moved"],
)
def test_a_match_while_the_checkout_moved_is_never_called_verified(
    world, during_restart, cause
):
    """C-LOW-1: "verified" claims the target runs the desired commit's
    code. An edit or a commit landing while slim boots is booted by slim
    AND by docker after it, and each then reports exactly the files the
    host digested just before ITS restart, so the digests agree. With the
    checkout no longer at the clean desired commit when the answer comes
    in, neither line may say verified, advance its marker, or stamp it."""
    _local_commit(world)
    world.on_restart["restart-slim"] = lambda: during_restart(world)
    world.http.routes["http://slim/status/turns"] = _phased(
        world, "slim",
        _idle(code_version=SHA_OLD, code_fingerprint=FILES_HOST),
        _idle(code_version=SHA_LOCAL, code_fingerprint=FILES_HOST),
    )
    world.http.routes["http://docker/status/turns"] = _phased(
        world, "docker",
        _idle(code_version=None, code_fingerprint=FILES_HOST),
        _idle(code_version=None, code_fingerprint=FILES_HOST),
    )

    summary = run_sync(world)

    for name in ("slim", "docker"):
        result = summary["targets"][name]
        assert result["action"] == "unverified"
        assert result["detail"].startswith(
            f"{SHA_OLD[:12]} -> {SHA_LOCAL[:12]} not confirmed: {cause} "
            "during the restart"
        )
        assert "verified" not in result["detail"].replace("unverified", "")
        assert marker(world, name) == SHA_OLD
        assert _stamp(world, name) is None


def test_a_slow_boot_is_polled_through_not_judged_early(world):
    """V8: refused, then /status/turns 503 (agent not built yet), then the
    OLD identity (a shutdown straggler), then the desired one, all inside
    the deadline: restarted, no failure."""
    _local_commit(world)
    world.http.routes["http://slim/health"] = _phased(
        world, "slim", (200, {"status": "ok"}), refused(), (200, {"status": "ok"})
    )
    world.http.routes["http://slim/status/turns"] = _phased(
        world, "slim",
        _idle(code_version=SHA_OLD, code_fingerprint=FILES_HOST),
        (503, None),
        _idle(code_version=SHA_OLD, code_fingerprint=FILES_OTHER),
        _idle(code_version=SHA_LOCAL, code_fingerprint=FILES_HOST),
    )
    summary = run_sync(world)
    slim = summary["targets"]["slim"]
    assert slim["action"] == "restarted"
    assert slim["detail"].endswith("verified commit+files")
    assert marker(world, "slim") == SHA_LOCAL
    assert _stamp(world, "slim") is None


@pytest.mark.parametrize(
    "before, status, remedy",
    [
        # The agent never finished building: the API's own fault.
        (_idle(code_version=SHA_OLD), 503,
         "; check the target's log, fix it, restart, then --mark-deployed"),
        # Down at the idle gate (a reboot), so the token was never tested
        # before the restart: an expired or non-admin token shows up here.
        (refused(), 401,
         "; the service token is expired or not admin: rotate it, then "
         "--mark-deployed"),
        (refused(), 403,
         "; the service token is expired or not admin: rotate it, then "
         "--mark-deployed"),
    ],
    ids=["503", "401", "403"],
)
def test_an_identity_probe_that_never_answers_fails_the_restart(
    world, before, status, remedy
):
    """/health answers but /status/turns never does to the deadline:
    nothing proved the new code is serving, so it is not marked. That is
    `failed`, not `unverified`, ON PURPOSE: the stamp holds the target
    (loud, exit 1 every tick) instead of a restart every 5 minutes, and
    the line names the remedy for the likely cause."""
    _local_commit(world)
    world.http.routes["http://slim/status/turns"] = _phased(
        world, "slim", before, (status, None)
    )
    summary = run_sync(world)
    slim = summary["targets"]["slim"]
    assert slim["action"] == "failed"
    assert slim["detail"] == (
        "health answered but the code identity probe did not (identity probe "
        f"answered {status}) within {deploy_sync.HEALTH_DEADLINE_SECONDS}s{remedy}"
    )
    assert marker(world, "slim") == SHA_OLD
    assert _stamp(world, "slim") == SHA_LOCAL
    # Held from here on: no 5-minute restart storm while it stays broken.
    restarts = world.runner.commands().count(("restart-slim",))
    assert run_sync(world)["targets"]["slim"]["action"] == "held"
    assert world.runner.commands().count(("restart-slim",)) == restarts


def test_the_verify_deadline_is_wall_clock_time(world):
    """C-NIT: each poll makes two probes that may each wait out their 10 s
    timeout, so counting only the 3 s sleeps stretched "within 90s" to
    over ten minutes on a hanging target, all of it holding the sync
    lock. The deadline is wall-clock time now: the target gives up at 90 s
    plus at most the poll in flight."""
    _local_commit(world)
    clock = FakeClock()

    def hangs_to_its_timeout(answer):
        def route():
            clock.now += deploy_sync.HTTP_TIMEOUT_SECONDS
            return answer
        return route

    world.http.routes["http://slim/health"] = hangs_to_its_timeout((200, {"status": "ok"}))
    world.http.routes["http://slim/status/turns"] = _phased(
        world, "slim", _idle(code_version=SHA_OLD),
        TimeoutError("timed out"),
    )
    plain_turns = world.http.routes["http://slim/status/turns"]

    def turns():
        if ("restart-slim",) in world.runner.commands():
            clock.now += deploy_sync.HTTP_TIMEOUT_SECONDS
        return plain_turns()

    world.http.routes["http://slim/status/turns"] = turns

    summary = deploy_sync.sync(
        world.config, world.state_dir, runner=world.runner,
        http_get=world.http, sleep=clock.sleep, fingerprint=world.fingerprint,
        clock=clock,
    )

    slim = summary["targets"]["slim"]
    assert slim["action"] == "failed"
    assert "(probe failed: TimeoutError) within 90s" in slim["detail"]
    one_poll = 2 * deploy_sync.HTTP_TIMEOUT_SECONDS + deploy_sync.HEALTH_POLL_SECONDS
    # Wall time spent on slim: from its first probe after the restart
    # (the idle probe before it costs nothing here) to giving up.
    assert clock.now <= deploy_sync.HEALTH_DEADLINE_SECONDS + one_poll


def test_the_latest_observation_decides_at_the_deadline(world):
    """A mismatch seen early, then a target that stops answering through
    the deadline (it crashed after booting the wrong code): the verdict is
    what is true at the end, "health check did not pass", not the stale
    mismatch, so the line never blames wiring for a crash."""
    _local_commit(world)
    answers = iter([(200, {"status": "ok"})] * 3)

    def health():
        if ("restart-slim",) not in world.runner.commands():
            return (200, {"status": "ok"})
        return next(answers, refused())

    world.http.routes["http://slim/health"] = health
    world.http.routes["http://slim/status/turns"] = _phased(
        world, "slim",
        _idle(code_version=SHA_OLD),
        _idle(code_version=SHA_THIRD, code_fingerprint=FILES_HOST),
    )
    summary = run_sync(world)
    slim = summary["targets"]["slim"]
    assert slim["action"] == "failed"
    assert slim["detail"] == (
        f"health check did not pass within {deploy_sync.HEALTH_DEADLINE_SECONDS}s"
    )
    assert _stamp(world, "slim") == SHA_LOCAL


NO_IDENTITY = (
    "unverified (target reports no code identity: no git metadata, and no "
    "boot file fingerprint, e.g. a package directory it could not read)"
)


@pytest.mark.parametrize(
    "payload, note",
    [
        # An API predating #423 (or a non-admin token): no identity keys.
        ({}, "unverified (old API or non-admin token: no code identity reported)"),
        # A current API with no git metadata whose boot walk failed (a
        # package directory it could not read): keys, both null.
        ({"code_version": None, "code_fingerprint": None}, NO_IDENTITY),
        # Malformed values are no identity, never a mismatch.
        ({"code_version": "abc", "code_fingerprint": "XYZ"}, NO_IDENTITY),
    ],
    ids=["old-api", "nulls", "malformed"],
)
def test_a_target_reporting_no_identity_keeps_todays_check(world, payload, note):
    """V9: degrade to the reachability check, said so in the line, never a
    failure."""
    _local_commit(world)
    world.http.routes["http://slim/status/turns"] = _idle(**payload)
    summary = run_sync(world)
    slim = summary["targets"]["slim"]
    assert slim["action"] == "restarted"
    assert slim["detail"] == f"{SHA_OLD[:12]} -> {SHA_LOCAL[:12]}, {note}"
    assert marker(world, "slim") == SHA_LOCAL


def test_a_new_api_target_initializes_from_its_reported_commit(world):
    """V10: a fresh marker for a target that reports its booted commit
    starts there, so a stale one restarts on the same tick (like the
    runner, #335)."""
    _local_commit(world)
    (world.state_dir / "slim.commit").unlink()
    world.http.routes["http://slim/status/turns"] = _phased(
        world, "slim",
        _idle(code_version=SHA_THIRD, code_fingerprint=FILES_OTHER),
        _idle(code_version=SHA_LOCAL, code_fingerprint=FILES_HOST),
    )
    summary = run_sync(world)
    assert ("restart-slim",) in world.runner.commands()
    assert summary["targets"]["slim"]["action"] == "restarted"
    assert summary["targets"]["slim"]["detail"].startswith(f"{SHA_THIRD[:12]} -> ")
    assert marker(world, "slim") == SHA_LOCAL
    # The init probe authenticated like the idle gate does.
    assert world.http.headers_seen["http://slim/status/turns"][0] == {
        "Authorization": "Bearer tok-slim"
    }


@pytest.mark.parametrize(
    "route, token",
    [
        (_idle(code_version=None, code_fingerprint=FILES_OTHER), None),  # files only
        ((401, None), None),  # cannot authenticate
        (_idle(code_version=SHA_THIRD), {"file": "/nonexistent/token"}),
    ],
    ids=["fingerprint-only", "probe-401", "no-token"],
)
def test_an_init_without_a_reported_commit_keeps_the_presumption(world, route, token):
    """V10: a digest is not a commit, and an init probe that cannot read an
    identity falls back to "presumed current" rather than wedging."""
    _local_commit(world)
    (world.state_dir / "docker.commit").unlink()
    world.http.routes["http://docker/status/turns"] = route
    if token is not None:
        world.config["targets"][1]["token"] = token
    summary = run_sync(world)
    assert summary["targets"]["docker"]["action"] == "initialized"
    assert ("restart-docker",) not in world.runner.commands()
    assert marker(world, "docker") == SHA_LOCAL


def _all_current(world):
    """Every target (runner included) runs SHA_LOCAL with the host files,
    and every marker says so."""
    _add_runner_target(world)
    world.head = SHA_LOCAL
    for name in ("slim", "docker", "runner"):
        (world.state_dir / f"{name}.commit").write_text(SHA_LOCAL + "\n", encoding="utf-8")
    world.http.routes["http://slim/status/turns"] = _idle(
        code_version=SHA_LOCAL, code_fingerprint=FILES_HOST
    )
    world.http.routes["http://docker/status/turns"] = _idle(
        code_version=None, code_fingerprint=FILES_HOST
    )
    world.http.routes["http://runner/health"] = (
        200, {"status": "ok", "active_jobs": 0, "code_version": SHA_LOCAL},
    )


def _state_snapshot(world):
    # The lock file included: read-only means --check creates nothing.
    return {
        p.name: (p.read_bytes(), p.stat().st_mtime_ns)
        for p in world.state_dir.iterdir()
    }


def test_check_reports_each_target_and_changes_nothing(world, tmp_path, capsys):
    """V12: read-only. No fetch, pull or restart; no state written, not
    even the lock file; one line per target naming what it reports against
    the checkout and the marker; exit 0 when everything matches."""
    _all_current(world)
    before = _state_snapshot(world)

    rc, out, err = _run_main(world, tmp_path, capsys, "--check")

    assert rc == 0, err
    lines = out.splitlines()
    assert lines[0].startswith("deploy_sync: check: slim: verified commit+files [")
    assert lines[1].startswith("deploy_sync: check: docker: verified files [")
    assert lines[2].startswith("deploy_sync: check: runner: verified commit [")
    assert (
        f"reported commit {SHA_LOCAL[:12]} files {FILES_HOST[:12]}; "
        f"checkout {SHA_LOCAL[:12]} files {FILES_HOST[:12]}; marker {SHA_LOCAL[:12]}"
    ) in lines[0]
    assert "reported commit none" in lines[1]
    # Only HEAD was read: nothing fetched, pulled, restarted or written.
    assert world.runner.commands() == [("git", "-C", "/repo", "rev-parse", "HEAD")]
    assert _state_snapshot(world) == before


def test_check_exits_nonzero_on_a_mismatch_or_an_unreachable_target(
    world, tmp_path, capsys
):
    _all_current(world)
    world.http.routes["http://docker/status/turns"] = _idle(
        code_version=None, code_fingerprint=FILES_OTHER
    )
    (world.state_dir / "docker.commit").write_text(SHA_OLD + "\n", encoding="utf-8")
    world.http.routes["http://runner/health"] = refused()

    rc, out, err = _run_main(world, tmp_path, capsys, "--check")

    assert rc == 1
    assert "deploy_sync: check: slim: verified commit+files" in out
    assert f"deploy_sync: check: docker: MISMATCH: booted files {FILES_OTHER}" in err
    assert f"marker {SHA_OLD[:12]}" in err
    assert "deploy_sync: check: runner: could not verify (target down)" in err


def test_check_names_the_causes_of_a_long_running_target(world, tmp_path, capsys):
    """A read-only check meets a checkout that moved or was edited since the
    target booted far more often than a broken restart, and says so."""
    _all_current(world)
    world.http.routes["http://slim/status/turns"] = _idle(
        code_version=SHA_OLD, code_fingerprint=FILES_HOST
    )
    world.http.routes["http://docker/status/turns"] = _idle(
        code_version=None, code_fingerprint=FILES_OTHER
    )
    rc, _, err = _run_main(world, tmp_path, capsys, "--check")
    assert rc == 1
    assert (
        f"slim: MISMATCH: booted code_version {SHA_OLD[:12]} != checkout "
        f"{SHA_LOCAL[:12]} (not restarted since HEAD moved, or runs another tree?)"
    ) in err
    assert (
        f"docker: MISMATCH: booted files {FILES_OTHER} differ from the checkout's "
        f"{FILES_HOST} (checkout files changed since it booted, uncommitted edits "
        "included, or a stale image, missing bind mount, another tree?)"
    ) in err


def test_check_on_a_target_reporting_nothing_says_so_and_passes(
    world, tmp_path, capsys
):
    _all_current(world)
    world.http.routes["http://docker/status/turns"] = _idle()  # old API
    rc, out, _ = _run_main(world, tmp_path, capsys, "--check")
    assert rc == 0
    assert (
        "deploy_sync: check: docker: unverified (old API or non-admin token: "
        "no code identity reported)"
    ) in out


def test_check_on_a_host_that_never_ran_a_tick_creates_no_state(
    world, tmp_path, capsys
):
    """No state dir yet (a fresh host, or a mistyped --state-dir): --check
    still answers, and leaves nothing behind (S-NIT)."""
    _all_current(world)
    world.state_dir = tmp_path / "never-created"
    rc, out, err = _run_main(world, tmp_path, capsys, "--check")
    assert rc == 0, err
    assert "deploy_sync: check: slim: verified commit+files [" in out
    assert "marker none" in out
    assert not world.state_dir.exists()


def test_check_never_answers_mid_tick(world, tmp_path, capsys):
    """--check takes the sync lock: during a tick it would read targets
    mid-restart, so it refuses (non-zero) instead of guessing."""
    _all_current(world)
    with deploy_sync.exclusive_lock(world.state_dir / deploy_sync.LOCK_NAME):
        rc, _, err = _run_main(world, tmp_path, capsys, "--check")
    assert rc == 1
    assert "another run is active" in err
    assert world.http.headers_seen == {}


def test_mark_deployed_marks_all_but_warns_per_target_on_other_code(
    world, tmp_path, capsys
):
    """V13: the ack stays unconditional (it is the post-recovery step), but
    a recovery that left a target on other code is not blessed silently."""
    _all_current(world)
    for name in ("slim", "docker", "runner"):
        (world.state_dir / f"{name}.commit").write_text(SHA_OLD + "\n", encoding="utf-8")
        (world.state_dir / f"{name}.failed").write_text(SHA_LOCAL, encoding="utf-8")
    world.http.routes["http://docker/status/turns"] = _idle(
        code_version=None, code_fingerprint=FILES_OTHER
    )
    world.http.routes["http://runner/health"] = refused()

    rc, out, err = _run_main(world, tmp_path, capsys, "--mark-deployed")

    assert rc == 0
    for name in ("slim", "docker", "runner"):
        assert marker(world, name) == SHA_LOCAL
        assert _stamp(world, name) is None
        assert f"deploy_sync: {name}: marked deployed at {SHA_LOCAL[:12]}" in out
    warnings = [line for line in err.splitlines() if "WARNING" in line]
    assert len(warnings) == 2
    assert warnings[0].startswith("deploy_sync: WARNING: docker: marked at ")
    assert f"booted files {FILES_OTHER} differ" in warnings[0]
    assert warnings[1].startswith("deploy_sync: WARNING: runner: marked at ")
    assert "could not verify (target down)" in warnings[1]


def test_mark_deployed_probes_only_the_targets_it_marks(world, tmp_path, capsys):
    _all_current(world)
    world.http.routes["http://docker/status/turns"] = _idle(code_fingerprint=FILES_OTHER)
    rc, _, err = _run_main(world, tmp_path, capsys, "--mark-deployed", "slim")
    assert rc == 0
    assert "WARNING" not in err
    assert "http://docker/status/turns" not in world.http.headers_seen


def test_no_token_value_reaches_any_line_or_detail(world, tmp_path, capsys):
    """V15: tokens are read at call time and never logged, on every new
    path: a failed identity probe, a mismatch, --check, --mark-deployed."""
    _local_commit(world)
    world.http.routes["http://slim/status/turns"] = _phased(
        world, "slim", _idle(code_version=SHA_OLD), (401, None)
    )
    world.http.routes["http://docker/status/turns"] = _phased(
        world, "docker",
        _idle(code_fingerprint=FILES_HOST),
        _idle(code_fingerprint=FILES_OTHER),
    )
    texts = []
    summary = run_sync(world)
    texts += [t["detail"] for t in summary["targets"].values()]
    for args in ((), ("--check",), ("--mark-deployed",)):
        world.http.routes["http://slim/status/turns"] = TimeoutError("timed out")
        _, out, err = _run_main(world, tmp_path, capsys, *args)
        texts += [out, err]
    blob = "\n".join(texts)
    assert "identity probe answered 401" in blob  # the failure path ran
    assert "MISMATCH" in blob and "WARNING" in blob
    assert "tok-slim" not in blob
    assert "tok-docker" not in blob
