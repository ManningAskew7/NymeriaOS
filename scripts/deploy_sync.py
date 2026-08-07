#!/usr/bin/env python3
"""Keep checkout-backed deployments current with main, idle-gated.

The deployments this serves run code straight from a git checkout (bind-mounted
containers, an editable install), so "deploy" is literally restart. What makes
a bare restart-on-commit hook unsafe is everything around that restart, and
each gate here exists for one concrete hazard:

  fetch + ff-only   Commits arrive two ways: pushed to origin from another
                    machine (checkout must PULL first) and made directly in
                    this checkout by agent sessions (HEAD already moved). Both
                    count as stale. Diverged history is never resolved here.
  clean gate        The checkout is a shared workspace: a parallel agent
                    session mid-task means the tree holds half-finished edits,
                    and a restart would boot them. Any MODIFIED tracked file
                    under the configured paths skips the whole tick; untracked
                    files never block.
  escalation gate   A commit range touching dependency or image inputs
                    (requirements*, pyproject, Dockerfile*, docker-compose*)
                    must not be half-deployed: restarting bind-mounted code
                    against un-updated deps is worse than staying stale. The
                    sync refuses (and skips the pull too, so a crash-looping
                    container cannot boot the new code either), and journals
                    an escalation notice once per offending commit. A human
                    or agent runs the heavy path (build / reinstall +
                    restart) and then acknowledges it with --mark-deployed,
                    which advances the markers and re-arms the sync; without
                    that ack every later commit inherits the escalating
                    range and auto-sync stays off.
  idle gate         GET /status/turns on each target; any nonzero activity
                    count (held turn locks, interactive admission slots, or
                    background bash jobs) defers that target to the next
                    tick, so in-flight work is never severed. When a PULL is
                    pending, every target sharing the checkout must be idle
                    first: imports are lazy in this codebase, so swapping
                    files under a live turn creates mixed-version state even
                    before any restart. Connection refused means the target
                    is already down, and a restart only brings it up
                    current, so it proceeds. Any OTHER failure (bad token,
                    HTTP error, malformed payload) fails safe: cannot verify
                    idleness, do not restart.
  health verify     After a restart the target's /health must answer within
                    the deadline or the marker is NOT advanced and a failure
                    stamp blocks further retries FOR THAT COMMIT (no
                    5-minute restart storm into a broken boot; the run exits
                    non-zero for the journal). A newer commit, or
                    --mark-deployed after manual recovery, re-arms it.

State: one marker file per target (last deployed commit) plus a once-per-commit
escalation stamp, under --state-dir. A missing marker initializes to the
current desired commit WITHOUT restarting (fresh install is presumed current).
One flock serializes overlapping runs; a busy peer exits 0 quietly.

Config (JSON, see --config): {"repo": path, "clean_paths": [...],
"escalation_globs": [...], "targets": [{"name", "health_url", "turns_url",
"token" ({"file": path} or {"env_file": path, "key": VAR}), "restart" (argv),
optional "restart_cwd", "restart_env", "extra_restart_containers"}]}.
Tokens are read at call time and never logged. stdlib only: this runs from a
system python3 on a host timer, not from the project venv.

Usage:
    python3 scripts/deploy_sync.py --config ~/.config/nymeria-deploy-sync/config.json
    python3 scripts/deploy_sync.py --config ... --dry-run

Nothing runs this by default; each host schedules it (the reference dev host
uses a 5-minute systemd user timer; pause with
`systemctl --user stop nymeria-deploy-sync.timer`).
"""
from __future__ import annotations

import argparse
import fcntl
import fnmatch
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Optional

DEFAULT_CONFIG = Path.home() / ".config" / "nymeria-deploy-sync" / "config.json"
DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "nymeria-deploy-sync"
LOCK_NAME = ".sync.lock"

DEFAULT_CLEAN_PATHS = ["Nymeria", "scripts"]
DEFAULT_ESCALATION_GLOBS = [
    "Nymeria/requirements*.txt",
    "Nymeria/pyproject.toml",
    "Nymeria/Dockerfile*",
    "Nymeria/docker-compose*.yml",
]

HEALTH_DEADLINE_SECONDS = 90
HEALTH_POLL_SECONDS = 3
HTTP_TIMEOUT_SECONDS = 10


class SyncBusy(RuntimeError):
    """Another sync run already holds the lock."""


@contextmanager
def exclusive_lock(lock_path: Path):
    with open(lock_path, "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SyncBusy(f"another sync holds {lock_path}") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# Seams. Tests inject fakes for all three; production uses these.


def run_command(
    argv: list[str],
    cwd: Optional[str] = None,
    env_extra: Optional[dict[str, str]] = None,
) -> tuple[int, str]:
    import os

    env = None
    if env_extra:
        env = {**os.environ, **env_extra}
    proc = subprocess.run(
        argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=600
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """The probe targets never legitimately redirect, and following one
    would forward the bearer token to whatever host the redirect names."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        return None


_OPENER = urllib.request.build_opener(_RefuseRedirects)


def http_get_json(url: str, headers: dict[str, str]) -> tuple[int, Any]:
    """(status, parsed JSON or None). Raises OSError family on no connection.

    Redirects are refused (they surface as HTTPError -> a non-200 return,
    which the caller fails safe on).
    """
    request = urllib.request.Request(url, headers=headers)
    try:
        with _OPENER.open(request, timeout=HTTP_TIMEOUT_SECONDS) as resp:
            body = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        return exc.code, None
    try:
        return status, json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return status, None


def is_connection_refused(exc: OSError) -> bool:
    """Only a REFUSED connection may be read as "target down".

    urllib wraps it as URLError(reason=ConnectionRefusedError). A timeout
    (TimeoutError), DNS failure, or reset must NOT count: a pegged API that
    cannot answer the probe in time is the busy state the idle gate exists
    to protect, and "down" is the one verdict that proceeds to restart.
    """
    if isinstance(exc, ConnectionRefusedError):
        return True
    return isinstance(getattr(exc, "reason", None), ConnectionRefusedError)


def sleep_seconds(seconds: float) -> None:
    import time

    time.sleep(seconds)


# ---------------------------------------------------------------------------
# Git.


def git(repo: str, *args: str, runner: Callable = run_command) -> tuple[int, str]:
    return runner(["git", "-C", repo, *args])


def repo_state(repo: str, runner: Callable = run_command) -> dict[str, Any]:
    """HEAD/origin relation after a fetch. relation: current|behind|ahead|diverged."""
    fetch_rc, fetch_out = git(repo, "fetch", "origin", "main", runner=runner)
    head_rc, head = git(repo, "rev-parse", "HEAD", runner=runner)
    if head_rc != 0:
        # Never let git's error text flow onward as if it were a commit id
        # (it would be written into markers and reported as deployed).
        return {"head_error": head.splitlines()[0] if head else "rev-parse failed"}
    origin_rc, origin = git(repo, "rev-parse", "origin/main", runner=runner)
    state: dict[str, Any] = {
        "head": head,
        "origin": origin if origin_rc == 0 else None,
        "fetch_failed": fetch_rc != 0,
        "fetch_error": fetch_out if fetch_rc != 0 else "",
    }
    if origin_rc != 0 or head == origin:
        state["relation"] = "current"
        return state
    base_rc, base = git(repo, "merge-base", head, origin, runner=runner)
    if base_rc != 0:
        state["relation"] = "diverged"
    elif base == head:
        state["relation"] = "behind"
    elif base == origin:
        state["relation"] = "ahead"
    else:
        state["relation"] = "diverged"
    return state


def dirty_tracked_paths(
    repo: str, clean_paths: list[str], runner: Callable = run_command
) -> list[str]:
    """Tracked files with index or worktree changes under the guarded paths."""
    _, out = git(repo, "status", "--porcelain", "--", *clean_paths, runner=runner)
    dirty = []
    for line in out.splitlines():
        if not line.strip():
            continue
        status = line[:2]
        if status == "??":
            continue  # untracked never blocks
        dirty.append(line[3:].strip())
    return dirty


def escalation_hits(
    repo: str,
    old: str,
    new: str,
    globs: list[str],
    runner: Callable = run_command,
) -> list[str]:
    rc, out = git(repo, "diff", "--name-only", old, new, runner=runner)
    if rc != 0:
        # An unknown marker commit (e.g. history rewritten) cannot prove the
        # range is safe: treat it as escalation, never as a green light.
        return [f"<diff failed: {out.splitlines()[0] if out else 'unknown'}>"]
    hits = []
    for name in out.splitlines():
        name = name.strip()
        if name and any(fnmatch.fnmatch(name, glob) for glob in globs):
            hits.append(name)
    return hits


# ---------------------------------------------------------------------------
# Targets.


def read_token(token_spec: dict[str, str]) -> Optional[str]:
    """Resolve a target's bearer token. Never log the return value."""
    if "file" in token_spec:
        try:
            value = Path(token_spec["file"]).read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return value or None
    if "env_file" in token_spec:
        key = token_spec.get("key", "")
        try:
            lines = Path(token_spec["env_file"]).read_text(encoding="utf-8")
        except OSError:
            return None
        # LAST match wins (dotenv semantics: this token ROTATES, and an
        # appended rotation must beat the stale line above it).
        value: Optional[str] = None
        for line in lines.splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            if not line.startswith(f"{key}="):
                continue
            candidate = line[len(key) + 1 :].strip()
            if " #" in candidate:
                candidate = candidate.split(" #", 1)[0].rstrip()
            value = candidate.strip("'\"") or None
        return value
    return None


def idle_verdict(
    target: dict[str, Any], http_get: Callable = http_get_json
) -> tuple[bool, str]:
    """(safe_to_restart, reason)."""
    token = read_token(target.get("token", {}))
    if token is None:
        return False, "token unavailable"
    try:
        status, payload = http_get(
            target["turns_url"], {"Authorization": f"Bearer {token}"}
        )
    except OSError as exc:
        if is_connection_refused(exc):
            # Down already: a restart only brings it back current.
            return True, "target down"
        # Timeout, DNS, reset, TLS: cannot verify idleness, fail SAFE. A
        # pegged API that cannot answer in time is busy, not dead.
        return False, f"probe failed: {type(exc).__name__}"
    if status != 200 or not isinstance(payload, dict):
        return False, f"activity probe answered {status}"
    active = payload.get("active_turns")
    interactive = payload.get("interactive_active")
    background = payload.get("background_jobs", 0)
    if not all(isinstance(v, int) for v in (active, interactive, background)):
        return False, "activity payload malformed"
    # interactive_active covers the admission-to-lock gap (a turn admitted
    # but not yet holding its thread lock); background_jobs covers detached
    # bash jobs, which hold no lock by design.
    if active or interactive or background:
        return False, (
            f"busy (turns={active} interactive={interactive} "
            f"background={background})"
        )
    return True, "idle"


def restart_target(
    target: dict[str, Any],
    runner: Callable = run_command,
    http_get: Callable = http_get_json,
    sleep: Callable = sleep_seconds,
) -> tuple[bool, str]:
    rc, out = runner(
        list(target["restart"]),
        cwd=target.get("restart_cwd"),
        env_extra=target.get("restart_env"),
    )
    if rc != 0:
        return False, f"restart command failed rc={rc}: {out.splitlines()[-1] if out else ''}"

    # Profile-gated sidecar containers (bots) run the same bind-mounted code
    # but are not in the base restart line; restart the ones actually running.
    extra = target.get("extra_restart_containers") or []
    if extra:
        rc, names = runner(["docker", "ps", "--format", "{{.Names}}"])
        running = set(names.splitlines()) if rc == 0 else set()
        for container in extra:
            if container in running:
                runner(["docker", "restart", container])

    waited = 0.0
    while waited <= HEALTH_DEADLINE_SECONDS:
        try:
            status, payload = http_get(target["health_url"], {})
            if status == 200 and isinstance(payload, dict):
                return True, "healthy"
        except OSError:
            pass
        sleep(HEALTH_POLL_SECONDS)
        waited += HEALTH_POLL_SECONDS
    return False, f"health check did not pass within {HEALTH_DEADLINE_SECONDS}s"


# ---------------------------------------------------------------------------
# Markers.


def marker_path(state_dir: Path, name: str) -> Path:
    return state_dir / f"{name}.commit"


def escalation_stamp_path(state_dir: Path, name: str) -> Path:
    return state_dir / f"{name}.escalated"


def failure_stamp_path(state_dir: Path, name: str) -> Path:
    return state_dir / f"{name}.failed"


def read_state_file(path: Path) -> Optional[str]:
    try:
        value = path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None
    # State files hold commit ids and nothing else. A truncated or corrupt
    # value must read as absent (marker: re-initialize; stamp: re-arm), not
    # flow into `git diff` where it wedges every future tick on the
    # diff-failed escalation sentinel.
    if value is not None and not re.fullmatch(r"[0-9a-f]{40}", value):
        return None
    return value


def write_state_file(path: Path, value: str) -> None:
    """Atomic write: a crash mid-write must not corrupt a marker."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(value + "\n", encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# The tick.


def sync(
    config: dict[str, Any],
    state_dir: Path,
    dry_run: bool = False,
    runner: Callable = run_command,
    http_get: Callable = http_get_json,
    sleep: Callable = sleep_seconds,
) -> dict[str, Any]:
    """One tick. Returns {"repo": {...}, "targets": {name: {action, detail}}}."""
    repo = config["repo"]
    clean_paths = config.get("clean_paths", DEFAULT_CLEAN_PATHS)
    globs = config.get("escalation_globs", DEFAULT_ESCALATION_GLOBS)
    branch = config.get("branch", "main")
    summary: dict[str, Any] = {"repo": {}, "targets": {}}

    # Branch gate: `git pull` pulls the CURRENT branch's upstream, so on a
    # feature branch the sync would "deploy" origin/main's sha while pulling
    # something else entirely, and a detached HEAD (an agent inspecting
    # history in this shared checkout) would redeploy old code. Wrong
    # checkout state blocks the whole tick, loudly.
    branch_rc, current_branch = git(
        repo, "symbolic-ref", "--quiet", "--short", "HEAD", runner=runner
    )
    if branch_rc != 0 or current_branch != branch:
        where = f"'{current_branch}'" if branch_rc == 0 else "a detached HEAD"
        summary["repo"]["blocked"] = (
            f"checkout is on {where}, expected '{branch}'; resolve by hand"
        )
        for target in config["targets"]:
            summary["targets"][target["name"]] = {
                "action": "skipped",
                "detail": summary["repo"]["blocked"],
            }
        return summary

    state = repo_state(repo, runner=runner)
    summary["repo"] = state
    if state.get("head_error"):
        for target in config["targets"]:
            summary["targets"][target["name"]] = {
                "action": "skipped",
                "detail": f"cannot resolve HEAD: {state['head_error']}",
            }
        return summary

    if state["relation"] == "diverged":
        for target in config["targets"]:
            summary["targets"][target["name"]] = {
                "action": "skipped",
                "detail": "local and origin history diverged; resolve by hand",
            }
        return summary

    dirty = dirty_tracked_paths(repo, clean_paths, runner=runner)
    if dirty:
        for target in config["targets"]:
            summary["targets"][target["name"]] = {
                "action": "skipped",
                "detail": f"working tree busy ({len(dirty)} modified tracked file(s))",
            }
        return summary

    desired = state["head"]
    verdicts: dict[str, tuple[bool, str]] = {}
    if state["relation"] == "behind":
        pull_hits = escalation_hits(
            repo, state["head"], state["origin"], globs, runner=runner
        )
        if pull_hits:
            summary["repo"]["pull_escalation"] = pull_hits
            # No pull: a crash-looping container must not boot code whose
            # dependency change was never applied. Targets may still sync to
            # the current HEAD below.
        else:
            # A pull rewrites the SHARED checkout under every target at
            # once, and imports are lazy in this codebase, so a live turn
            # would import post-pull modules into a pre-pull process. All
            # consumers must be idle (or down) before the tree moves.
            verdicts = {
                target["name"]: idle_verdict(target, http_get=http_get)
                for target in config["targets"]
            }
            blockers = [name for name, (safe, _) in verdicts.items() if not safe]
            if blockers:
                for target in config["targets"]:
                    name = target["name"]
                    safe, reason = verdicts[name]
                    summary["targets"][name] = {
                        "action": "deferred",
                        "detail": (
                            reason
                            if not safe
                            else "shared checkout: waiting for "
                            + ", ".join(blockers)
                        ),
                    }
                summary["repo"]["desired"] = desired
                return summary
            if dry_run:
                desired = state["origin"]
            else:
                rc, out = git(repo, "pull", "--ff-only", runner=runner)
                if rc != 0:
                    summary["repo"]["pull_failed"] = (
                        out.splitlines()[-1] if out else "?"
                    )
                else:
                    desired = state["origin"]
                    # The pull took time; a turn may have started during
                    # it. Restart decisions below must re-probe, not reuse
                    # the pre-pull verdicts.
                    verdicts = {}
    summary["repo"]["desired"] = desired

    for target in config["targets"]:
        name = target["name"]
        marker_file = marker_path(state_dir, name)
        marker = read_state_file(marker_file)

        if marker is None:
            # Fresh install is presumed current: initialize without a restart.
            if not dry_run:
                state_dir.mkdir(parents=True, exist_ok=True)
                write_state_file(marker_file, desired)
            summary["targets"][name] = {
                "action": "initialized",
                "detail": f"marker set to {desired[:12]} without restart",
            }
            continue

        if marker == desired:
            summary["targets"][name] = {"action": "noop", "detail": "current"}
            continue

        hits = escalation_hits(repo, marker, desired, globs, runner=runner)
        if hits:
            stamp = escalation_stamp_path(state_dir, name)
            already = read_state_file(stamp) == desired
            if not dry_run and not already:
                state_dir.mkdir(parents=True, exist_ok=True)
                write_state_file(stamp, desired)
            summary["targets"][name] = {
                "action": "escalated",
                "detail": (
                    f"{marker[:12]}..{desired[:12]} touches {', '.join(hits[:4])}"
                    f"{'' if len(hits) <= 4 else ' (+ more)'}; run the heavy "
                    "deploy path by hand, then --mark-deployed"
                ),
                "already_stamped": already,
            }
            continue

        failure_stamp = failure_stamp_path(state_dir, name)
        if read_state_file(failure_stamp) == desired:
            # The restart already failed for this exact commit: retrying
            # every tick would storm a broken boot. A newer commit or
            # --mark-deployed (after manual recovery) re-arms the target.
            summary["targets"][name] = {
                "action": "held",
                "detail": (
                    f"restart already failed for {desired[:12]}; push a fix "
                    "or run --mark-deployed after manual recovery"
                ),
            }
            continue

        safe, reason = verdicts.get(name) or idle_verdict(target, http_get=http_get)
        if not safe:
            summary["targets"][name] = {"action": "deferred", "detail": reason}
            continue

        if dry_run:
            summary["targets"][name] = {
                "action": "would-restart",
                "detail": f"{marker[:12]} -> {desired[:12]} ({reason})",
            }
            continue

        ok, detail = restart_target(
            target, runner=runner, http_get=http_get, sleep=sleep
        )
        state_dir.mkdir(parents=True, exist_ok=True)
        if ok:
            write_state_file(marker_file, desired)
            escalation_stamp_path(state_dir, name).unlink(missing_ok=True)
            failure_stamp.unlink(missing_ok=True)
            summary["targets"][name] = {
                "action": "restarted",
                "detail": f"{marker[:12]} -> {desired[:12]}",
            }
        else:
            write_state_file(failure_stamp, desired)
            summary["targets"][name] = {"action": "failed", "detail": detail}
    return summary


def mark_deployed(
    config: dict[str, Any],
    state_dir: Path,
    names: Optional[list[str]],
    runner: Callable = run_command,
) -> dict[str, str]:
    """Acknowledge a manual deploy: markers advance to HEAD, stamps clear.

    The documented final step of the escalation runbook. Uses the CURRENT
    checkout HEAD (no fetch, no pull): the operator deployed what is on
    disk, and saying otherwise here would mark commits as deployed that
    never were.
    """
    rc, head = git(config["repo"], "rev-parse", "HEAD", runner=runner)
    if rc != 0 or not re.fullmatch(r"[0-9a-f]{40}", head):
        raise SystemExit(
            "deploy_sync: cannot resolve HEAD; nothing marked "
            f"({head.splitlines()[0] if head else 'rev-parse failed'})"
        )
    selected = {t["name"] for t in config["targets"]}
    if names:
        unknown = set(names) - selected
        if unknown:
            raise SystemExit(
                f"deploy_sync: unknown target(s): {', '.join(sorted(unknown))}"
            )
        selected = set(names)
    state_dir.mkdir(parents=True, exist_ok=True)
    result = {}
    for name in sorted(selected):
        write_state_file(marker_path(state_dir, name), head)
        escalation_stamp_path(state_dir, name).unlink(missing_ok=True)
        failure_stamp_path(state_dir, name).unlink(missing_ok=True)
        result[name] = head
    return result


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--dry-run", action="store_true", help="report, change nothing")
    parser.add_argument(
        "--mark-deployed",
        nargs="*",
        metavar="TARGET",
        default=None,
        help=(
            "acknowledge a manual deploy: advance the named targets' markers "
            "(all targets when none named) to the current HEAD and clear "
            "their escalation/failure stamps, restarting nothing"
        ),
    )
    args = parser.parse_args(argv)

    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"deploy_sync: cannot read config {args.config}: {exc}", file=sys.stderr)
        return 1

    args.state_dir.mkdir(parents=True, exist_ok=True)
    try:
        with exclusive_lock(args.state_dir / LOCK_NAME):
            if args.mark_deployed is not None:
                marked = mark_deployed(config, args.state_dir, args.mark_deployed)
                for name, sha in marked.items():
                    print(f"deploy_sync: {name}: marked deployed at {sha[:12]}")
                return 0
            summary = sync(config, args.state_dir, dry_run=args.dry_run)
    except SyncBusy:
        print("deploy_sync: another run is active, nothing to do")
        return 0

    repo_info = summary["repo"]
    failed = False
    if repo_info.get("blocked"):
        print(f"deploy_sync: BLOCKED: {repo_info['blocked']}", file=sys.stderr)
        failed = True
    if repo_info.get("head_error"):
        print(
            f"deploy_sync: cannot resolve HEAD: {repo_info['head_error']}",
            file=sys.stderr,
        )
        failed = True
    if repo_info.get("fetch_failed"):
        print(f"deploy_sync: fetch failed ({repo_info.get('fetch_error', '?')})")
    if repo_info.get("pull_failed"):
        print(f"deploy_sync: pull failed ({repo_info['pull_failed']})", file=sys.stderr)
        failed = True
    if repo_info.get("pull_escalation"):
        print(
            "deploy_sync: ESCALATION: incoming commits touch "
            + ", ".join(repo_info["pull_escalation"][:4])
            + "; pull skipped, run the heavy deploy path by hand, "
            "then --mark-deployed"
        )
    for name, result in summary["targets"].items():
        line = f"deploy_sync: {name}: {result['action']} ({result['detail']})"
        if result["action"] in ("failed", "held"):
            # held stays loud on purpose: a deployment that cannot boot
            # must not be indistinguishable from a healthy idle system,
            # even though it is (correctly) not being restarted again.
            print(line, file=sys.stderr)
            failed = True
        elif result["action"] == "noop":
            pass  # a 5-minute timer must not journal 288 no-op lines a day
        elif result["action"] == "escalated" and result.get("already_stamped"):
            pass  # journaled once already; stay quiet on repeat ticks
        else:
            print(line)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
