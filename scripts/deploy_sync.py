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
                    count (held turn locks, interactive admission slots,
                    background bash jobs, or detached Claude Code / /code
                    runs) defers that target to the next
                    tick, so in-flight work is never severed. A target
                    without a turns endpoint (the Claude Code runner) is
                    gated on its /health "active_jobs" instead. When a PULL is
                    pending, every target sharing the checkout must be idle
                    first: imports are lazy in this codebase, so swapping
                    files under a live turn creates mixed-version state even
                    before any restart. Connection refused means the target
                    is already down, and a restart only brings it up
                    current, so it proceeds. Any OTHER failure (bad token,
                    HTTP error, malformed payload) fails safe: cannot verify
                    idleness, do not restart.
  verify            After a restart the target's /health must answer within
                    the deadline AND the target must report the code it was
                    restarted onto (#423): the commit it booted from must be
                    the desired commit, and the digest of its boot-time file
                    fingerprint must equal the same digest computed over the
                    host checkout just before the restart. A bare "it
                    answers" proves nothing: a stale editable install, a
                    stray process on the port, or a container that lost its
                    bind mount (the image bakes a full copy) all answer.
                    The API reports both on /status/turns to an admin token
                    (Docker reports no commit: its mounts carry no .git, so
                    its files decide); the runner's /health reports the
                    commit. A field a target does not report is not checked,
                    and one reporting neither (an older API) is accepted on
                    /health alone, said so in its line. Failure to the
                    deadline does NOT advance the marker, and a failure stamp
                    blocks further retries FOR THAT COMMIT (no 5-minute
                    restart storm into a broken boot or a wrong install; the
                    run exits non-zero for the journal). A newer commit, or
                    --mark-deployed after manual recovery, re-arms it. A
                    mismatch the checkout itself explains (HEAD moved,
                    tracked files dirtied, or package files changed during
                    the restart) is "unverified" instead: no marker, no
                    stamp, and the next clean tick restarts and verifies.

State: one marker file per target (last deployed commit) plus a once-per-commit
escalation stamp, under --state-dir. A missing marker initializes to the
current desired commit WITHOUT restarting (fresh install is presumed current),
unless the target reports the commit it booted from: then the marker starts
there, so a stale target restarts on the same tick.
One flock serializes overlapping runs; a busy peer exits 0 quietly.

Config (JSON, see --config): {"repo": path, "clean_paths": [...],
"escalation_globs": [...], "targets": [{"name", "health_url", "turns_url",
"token" ({"file": path} or {"env_file": path, "key": VAR}), "restart" (argv),
optional "restart_cwd", "restart_env", "extra_restart_containers",
"package_dir" (the package the target imports; default
<repo>/Nymeria/nymeria, its run.py beside it), "verify_files" (default
true; false skips the file check, for a mount that does not preserve
stat)}]}.
A target with no "turns_url" (the Claude Code runner unit) is idle-probed
through its unauthenticated "health_url" instead, on the "active_jobs"
count it reports (#335); it needs no "token", and its identity is the
"code_version" on that /health.
Tokens are read at call time and never logged. stdlib only: this runs from a
system python3 on a host timer, not from the project venv.

Usage:
    python3 scripts/deploy_sync.py --config ~/.config/nymeria-deploy-sync/config.json
    python3 scripts/deploy_sync.py --config ... --dry-run
    python3 scripts/deploy_sync.py --config ... --check          # read-only
    python3 scripts/deploy_sync.py --config ... --mark-deployed [TARGET ...]

--check restarts, fetches and writes nothing: per target it prints the
reported identity against the checkout and the marker, and exits 1 when any
target runs other code or cannot be probed.

Nothing runs this by default; each host schedules it (the reference dev host
uses a 5-minute systemd user timer; pause with
`systemctl --user stop nymeria-deploy-sync.timer`).
"""
from __future__ import annotations

import argparse
import fcntl
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, NamedTuple, Optional

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

COMMIT_RE = re.compile(r"[0-9a-f]{40}")
FILES_DIGEST_RE = re.compile(r"[0-9a-f]{16}")


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
# Seams. Tests inject fakes for these (and for checkout_fingerprint below);
# production uses them.


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
    # --no-write-fetch-head: this fetch runs every 5 minutes on a checkout
    # where interactive agent sessions also run `git pull --ff-only`. A plain
    # fetch rewrites FETCH_HEAD, and two uncoordinated writers can leave it
    # with multiple merge-candidate lines, failing the agent's pull with
    # "Cannot fast-forward to multiple branches" (backlog #221). This fetch
    # only needs origin/main updated; skipping FETCH_HEAD removes the
    # collision at its source (verified: the tracking ref still updates).
    fetch_rc, fetch_out = git(
        repo, "fetch", "--no-write-fetch-head", "origin", "main", runner=runner
    )
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
    if not target.get("turns_url"):
        return health_idle_verdict(target, http_get=http_get)
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
    # Absent on an API predating #339: an older backend still gates on the
    # three counters it does report.
    claude_code = payload.get("claude_code_jobs", 0)
    if not all(
        isinstance(v, int) for v in (active, interactive, background, claude_code)
    ):
        return False, "activity payload malformed"
    # interactive_active covers the admission-to-lock gap (a turn admitted
    # but not yet holding its thread lock); background_jobs covers detached
    # bash jobs, which hold no lock by design; claude_code_jobs covers
    # detached Claude Code / /code runs, which a restart would kill
    # mid-repair (#339).
    if active or interactive or background or claude_code:
        return False, (
            f"busy (turns={active} interactive={interactive} "
            f"background={background} claude_code={claude_code})"
        )
    return True, "idle"


def health_idle_verdict(
    target: dict[str, Any], http_get: Callable = http_get_json
) -> tuple[bool, str]:
    """The idle probe for a target with no turn-activity endpoint: the Claude
    Code runner (#335) reports its in-flight runs as ``active_jobs`` on its
    unauthenticated ``/health``. Same fail-safe shape as ``idle_verdict``:
    refused means down (restart brings it up current), any other failure
    or a payload without the count means "cannot verify", no restart.
    """
    try:
        status, payload = http_get(target["health_url"], {})
    except OSError as exc:
        if is_connection_refused(exc):
            return True, "target down"
        return False, f"probe failed: {type(exc).__name__}"
    if status != 200 or not isinstance(payload, dict):
        return False, f"health probe answered {status}"
    active = payload.get("active_jobs")
    if not isinstance(active, int) or isinstance(active, bool):
        # A runner predating #335 reports no count: never restart blind.
        return False, "health payload has no active_jobs count"
    if active:
        return False, f"busy (active_jobs={active})"
    return True, "idle"


# ---------------------------------------------------------------------------
# Code identity (#423): which code a target booted from, against the checkout.


def checkout_fingerprint(package_dir: Path) -> Optional[str]:
    """Digest of the package's files exactly as ``nymeria/_provenance.py``
    computes it for a process's boot record, or None (no files found).

    A COPY of ``_provenance._stat_paths`` + ``source_fingerprint`` +
    ``fingerprint_digest``: this script runs from a system python3 and cannot
    import the package. ``tests/test_runtime_provenance.py`` pins the two to
    the same answer; change both or neither. Stat only (count, summed
    mtime_ns, summed sizes) over every file but ``__pycache__``, plus the
    ``run.py`` beside the package; a bind mount preserves all three, so the
    host and a container see the same digest.
    """
    package_dir = Path(package_dir)

    def paths():
        for root, dirs, names in os.walk(package_dir):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in names:
                yield os.path.join(root, name)
        yield str(package_dir.parent / "run.py")

    files = mtime_sum = size_sum = 0
    try:
        for path in paths():
            try:
                st = os.stat(path)
            except OSError:
                continue
            files += 1
            mtime_sum += st.st_mtime_ns
            size_sum += st.st_size
    except OSError:
        return None
    if not files:
        return None
    raw = f"{files}:{mtime_sum}:{size_sum}"
    return hashlib.sha256(raw.encode("ascii")).hexdigest()[:16]


def target_package_dir(config: dict[str, Any], target: dict[str, Any]) -> Path:
    """The package directory this target imports (its run.py beside it)."""
    if target.get("package_dir"):
        return Path(target["package_dir"])
    return Path(config["repo"]) / "Nymeria" / "nymeria"


def host_files(
    config: dict[str, Any], target: dict[str, Any], fingerprint: Callable
) -> Optional[str]:
    """The checkout-side files digest for a target, None when it opts out."""
    if target.get("verify_files", True) is False:
        return None
    return fingerprint(target_package_dir(config, target))


class Identity(NamedTuple):
    """What a target says it booted from.

    ``reported`` is False when the payload carries neither identity key (an
    API predating #423, or a token that is not admin: the API shows the
    fields to admins only). ``error`` is set when nothing could be read.
    (A NamedTuple, not a dataclass: the tests load this file without
    registering it in ``sys.modules``, which a dataclass requires.)
    """

    commit: Optional[str] = None
    files: Optional[str] = None
    reported: bool = False
    error: Optional[str] = None


def reported_identity(payload: Any) -> Identity:
    if not isinstance(payload, dict):
        return Identity(error="identity payload malformed")
    commit = payload.get("code_version")
    files = payload.get("code_fingerprint")
    return Identity(
        commit=commit if isinstance(commit, str) and COMMIT_RE.fullmatch(commit) else None,
        files=files if isinstance(files, str) and FILES_DIGEST_RE.fullmatch(files) else None,
        reported="code_version" in payload or "code_fingerprint" in payload,
    )


def probe_identity(
    target: dict[str, Any], http_get: Callable = http_get_json
) -> Identity:
    """Ask the target what it booted from: ``/status/turns`` with its bearer
    when it has a turns endpoint (the API), else its unauthenticated
    ``/health`` (the Claude Code runner, #335)."""
    url = target.get("turns_url")
    headers: dict[str, str] = {}
    if url:
        token = read_token(target.get("token", {}))
        if token is None:
            return Identity(error="token unavailable")
        headers = {"Authorization": f"Bearer {token}"}
    else:
        url = target["health_url"]
    try:
        status, payload = http_get(url, headers)
    except OSError as exc:
        if is_connection_refused(exc):
            return Identity(error="target down")
        return Identity(error=f"probe failed: {type(exc).__name__}")
    if status != 200:
        return Identity(error=f"identity probe answered {status}")
    return reported_identity(payload)


def compare_identity(
    identity: Identity, want_commit: Optional[str], want_files: Optional[str]
) -> tuple[list[str], list[str]]:
    """(proven, mismatched) field names. The one predicate every target
    shares: a field the target does not report, or that the host has no
    value for, is neither proven nor held against it."""
    proven: list[str] = []
    mismatched: list[str] = []
    for field, got, want in (
        ("commit", identity.commit, want_commit),
        ("files", identity.files, want_files),
    ):
        if got is None or want is None:
            continue
        (proven if got == want else mismatched).append(field)
    return proven, mismatched


def describe_proof(identity: Identity, proven: list[str]) -> str:
    """What a matching answer proved, for the journal line."""
    if proven:
        return "verified " + "+".join(proven)
    if identity.commit is None and identity.files is None:
        if not identity.reported:
            return "unverified (old API or non-admin token: no code identity reported)"
        return "unverified (target reports no code identity)"
    return "unverified (no checkout value to compare: verify_files off or package_dir unreadable)"


def describe_mismatch(
    identity: Identity,
    want_commit: Optional[str],
    want_files: Optional[str],
    previous: Optional[str] = None,
    after_restart: bool = True,
) -> str:
    """The cause, in operator terms (callers append the remedy).

    ``after_restart`` picks the likely causes: right after a restart the
    files were digested moments before it (a change during it is classified
    transient separately), so a difference means wiring; a read-only check
    of a long-running target more often meets a checkout that moved or was
    edited since it booted.
    """
    parts = []
    if (
        identity.commit is not None
        and want_commit is not None
        and identity.commit != want_commit
    ):
        if previous is not None and identity.commit == previous:
            parts.append(
                f"code_version {identity.commit[:12]} is still the previous "
                f"commit, not {want_commit[:12]} (the restart did not take?)"
            )
        elif after_restart:
            parts.append(
                f"booted code_version {identity.commit[:12]} != desired "
                f"{want_commit[:12]} (install or unit runs another tree?)"
            )
        else:
            parts.append(
                f"booted code_version {identity.commit[:12]} != checkout "
                f"{want_commit[:12]} (not restarted since HEAD moved, or "
                "runs another tree?)"
            )
    if (
        identity.files is not None
        and want_files is not None
        and identity.files != want_files
    ):
        hint = (
            "stale image, missing bind mount, or an install from another tree?"
            if after_restart
            else "checkout files changed since it booted, uncommitted edits "
            "included, or a stale image, missing bind mount, another tree?"
        )
        parts.append(
            f"booted files {identity.files} differ from the checkout's "
            f"{want_files} ({hint})"
        )
    return "; ".join(parts)


MISMATCH_REMEDY = "fix it, restart, then --mark-deployed"


def restart_target(
    target: dict[str, Any],
    runner: Callable = run_command,
    http_get: Callable = http_get_json,
    sleep: Callable = sleep_seconds,
    expected_version: Optional[str] = None,
    expected_files: Optional[str] = None,
    previous_version: Optional[str] = None,
) -> tuple[str, str]:
    """Run the target's restart argv, then wait until it answers /health
    AND reports the code it was restarted onto.

    Returns (outcome, detail): "ok" (detail says what was proven),
    "mismatch" (it kept answering on other code to the deadline; detail is
    the cause), or "failed". Every answer before the deadline is polled
    through rather than judged: a slow boot answers 503 first, and the old
    process can answer during its shutdown. Accepting the first 200 would
    advance the marker over a stale install, a stray process on the port,
    or a container that lost its bind mount (#335, #423).
    """
    rc, out = runner(
        list(target["restart"]),
        cwd=target.get("restart_cwd"),
        env_extra=target.get("restart_env"),
    )
    if rc != 0:
        return "failed", f"restart command failed rc={rc}: {out.splitlines()[-1] if out else ''}"

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
    # The LATEST observation decides the verdict at the deadline.
    last_mismatch: Optional[Identity] = None
    last_error: Optional[str] = None
    while waited <= HEALTH_DEADLINE_SECONDS:
        try:
            status, payload = http_get(target["health_url"], {})
        except OSError:
            status, payload = None, None
        if status == 200 and isinstance(payload, dict):
            identity = (
                probe_identity(target, http_get=http_get)
                if target.get("turns_url")
                else reported_identity(payload)
            )
            if identity.error:
                last_mismatch, last_error = None, identity.error
            else:
                proven, mismatched = compare_identity(
                    identity, expected_version, expected_files
                )
                if not mismatched:
                    return "ok", describe_proof(identity, proven)
                last_mismatch, last_error = identity, None
        else:
            last_mismatch = last_error = None
        sleep(HEALTH_POLL_SECONDS)
        waited += HEALTH_POLL_SECONDS
    if last_mismatch is not None:
        return "mismatch", (
            describe_mismatch(
                last_mismatch, expected_version, expected_files, previous_version
            )
            + f", for {HEALTH_DEADLINE_SECONDS}s"
        )
    if last_error is not None:
        return "failed", (
            f"health answered but the code identity probe did not "
            f"({last_error}) within {HEALTH_DEADLINE_SECONDS}s"
        )
    return "failed", f"health check did not pass within {HEALTH_DEADLINE_SECONDS}s"


def transient_cause(
    config: dict[str, Any],
    target: dict[str, Any],
    desired: str,
    files_before: Optional[str],
    runner: Callable = run_command,
    fingerprint: Callable = checkout_fingerprint,
) -> Optional[str]:
    """Why a post-restart mismatch may be the checkout's doing, or None.

    The host digest and the desired commit were taken BEFORE the restart; a
    parallel session committing, editing a tracked file or adding one to the
    package while the target booted makes the target load newer files than
    that, a real but self-healing mismatch. Those are retried by the next
    clean tick, never stamped: a held target needs a human. When in doubt
    this says transient (the cost is one extra restart next tick, which
    stamps if the mismatch was real after all).
    """
    repo = config["repo"]
    rc, head = git(repo, "rev-parse", "HEAD", runner=runner)
    if rc != 0:
        return "HEAD became unreadable"
    if head != desired:
        return f"HEAD moved to {head[:12]}"
    clean_paths = config.get("clean_paths", DEFAULT_CLEAN_PATHS)
    if dirty_tracked_paths(repo, clean_paths, runner=runner):
        return "tracked files were modified"
    if files_before is not None and host_files(config, target, fingerprint) != files_before:
        return "files under the package changed"
    return None


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
    fingerprint: Callable = checkout_fingerprint,
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
            # Fresh install is presumed current: initialize without a
            # restart. A target that REPORTS the commit it booted from is
            # not presumed anything: its marker starts there, so a stale
            # one is restarted on this same tick (#335, #423). A files
            # digest alone is not a commit and cannot seed a marker; a probe
            # that fails (down, bad token) falls back to the presumption.
            reported = probe_identity(target, http_get=http_get).commit
            initial = reported or desired
            if not dry_run:
                state_dir.mkdir(parents=True, exist_ok=True)
                write_state_file(marker_file, initial)
            if initial == desired:
                summary["targets"][name] = {
                    "action": "initialized",
                    "detail": f"marker set to {desired[:12]} without restart",
                }
                continue
            marker = initial
            summary["targets"][name] = {
                "action": "initialized",
                "detail": f"marker set to reported {initial[:12]}; stale, restarting",
            }

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

        # The checkout's side of the identity check, taken just before the
        # restart so it describes the tree the target is about to load.
        files_before = host_files(config, target, fingerprint)
        outcome, detail = restart_target(
            target,
            runner=runner,
            http_get=http_get,
            sleep=sleep,
            expected_version=desired,
            expected_files=files_before,
            previous_version=marker,
        )
        state_dir.mkdir(parents=True, exist_ok=True)
        if outcome == "ok":
            write_state_file(marker_file, desired)
            escalation_stamp_path(state_dir, name).unlink(missing_ok=True)
            failure_stamp.unlink(missing_ok=True)
            summary["targets"][name] = {
                "action": "restarted",
                "detail": f"{marker[:12]} -> {desired[:12]}, {detail}",
            }
            continue
        if outcome == "mismatch":
            cause = transient_cause(
                config, target, desired, files_before,
                runner=runner, fingerprint=fingerprint,
            )
            if cause:
                summary["targets"][name] = {
                    "action": "unverified",
                    "detail": (
                        f"{marker[:12]} -> {desired[:12]} not confirmed: "
                        f"{detail}; {cause} during the restart, so not "
                        "held: the next clean tick restarts and verifies"
                    ),
                }
                continue
            detail = f"{detail}; {MISMATCH_REMEDY}"
        write_state_file(failure_stamp, desired)
        summary["targets"][name] = {"action": "failed", "detail": detail}
    return summary


def verify_running(
    config: dict[str, Any],
    target: dict[str, Any],
    head: str,
    http_get: Callable = http_get_json,
    fingerprint: Callable = checkout_fingerprint,
) -> dict[str, Any]:
    """Does the target run the checkout's code right now? Read-only.

    {"verdict": verified|unverified|mismatch|unreachable, "detail",
    "identity", "checkout_files"}. Serves --check and the --mark-deployed
    warning; the restart verify uses the same predicate.
    """
    identity = probe_identity(target, http_get=http_get)
    files = host_files(config, target, fingerprint)
    result: dict[str, Any] = {"identity": identity, "checkout_files": files}
    if identity.error:
        result.update(verdict="unreachable", detail=f"could not verify ({identity.error})")
        return result
    proven, mismatched = compare_identity(identity, head, files)
    if mismatched:
        result.update(
            verdict="mismatch",
            detail="MISMATCH: "
            + describe_mismatch(identity, head, files, after_restart=False),
        )
    else:
        result.update(
            verdict="verified" if proven else "unverified",
            detail=describe_proof(identity, proven),
        )
    return result


def resolve_head(config: dict[str, Any], runner: Callable = run_command) -> str:
    """The checkout's HEAD as it is (no fetch), or SystemExit."""
    rc, head = git(config["repo"], "rev-parse", "HEAD", runner=runner)
    if rc != 0 or not COMMIT_RE.fullmatch(head):
        raise SystemExit(
            "deploy_sync: cannot resolve HEAD "
            f"({head.splitlines()[0] if head else 'rev-parse failed'})"
        )
    return head


def check(
    config: dict[str, Any],
    state_dir: Path,
    runner: Callable = run_command,
    http_get: Callable = http_get_json,
    fingerprint: Callable = checkout_fingerprint,
) -> dict[str, dict[str, Any]]:
    """--check: per target, the code it reports against the checkout and the
    marker. No fetch, no pull, no restart, no state written."""
    head = resolve_head(config, runner=runner)
    results = {}
    for target in config["targets"]:
        result = verify_running(
            config, target, head, http_get=http_get, fingerprint=fingerprint
        )
        result["head"] = head
        result["marker"] = read_state_file(marker_path(state_dir, target["name"]))
        results[target["name"]] = result
    return results


def _short(value: Optional[str]) -> str:
    return value[:12] if value else "none"


def format_check_line(name: str, result: dict[str, Any]) -> str:
    identity: Identity = result["identity"]
    facts = (
        f"reported commit {_short(identity.commit)} files {_short(identity.files)}; "
        f"checkout {_short(result['head'])} files {_short(result['checkout_files'])}; "
        f"marker {_short(result['marker'])}"
    )
    if identity.error:
        facts = (
            f"checkout {_short(result['head'])} files "
            f"{_short(result['checkout_files'])}; marker {_short(result['marker'])}"
        )
    return f"deploy_sync: check: {name}: {result['detail']} [{facts}]"


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


def _print_check(results: dict[str, dict[str, Any]]) -> int:
    failed = False
    for name, result in results.items():
        line = format_check_line(name, result)
        if result["verdict"] in ("mismatch", "unreachable"):
            print(line, file=sys.stderr)
            failed = True
        else:
            print(line)
    return 1 if failed else 0


def main(
    argv: Optional[list[str]] = None,
    *,
    runner: Callable = run_command,
    http_get: Callable = http_get_json,
    sleep: Callable = sleep_seconds,
    fingerprint: Callable = checkout_fingerprint,
) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "deploy_sync").splitlines()[0])
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--dry-run", action="store_true", help="report, change nothing")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--mark-deployed",
        nargs="*",
        metavar="TARGET",
        default=None,
        help=(
            "acknowledge a manual deploy: advance the named targets' markers "
            "(all targets when none named) to the current HEAD and clear "
            "their escalation/failure stamps, restarting nothing; warns for "
            "each target whose running code differs from HEAD"
        ),
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help=(
            "read-only: report whether each target runs the checkout's code "
            "(commit and files) against its marker; no fetch, restart or "
            "state write; exit 1 on a mismatch or an unreachable target"
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
            if args.check:
                return _print_check(
                    check(
                        config, args.state_dir,
                        runner=runner, http_get=http_get, fingerprint=fingerprint,
                    )
                )
            if args.mark_deployed is not None:
                marked = mark_deployed(
                    config, args.state_dir, args.mark_deployed, runner=runner
                )
                by_name = {t["name"]: t for t in config["targets"]}
                for name, sha in marked.items():
                    print(f"deploy_sync: {name}: marked deployed at {sha[:12]}")
                    # The ack stays unconditional (it is the step run after
                    # a manual recovery), but a recovery that left the
                    # target on other code must not be blessed silently:
                    # the next tick is a noop and never probes it again.
                    result = verify_running(
                        config, by_name[name], sha,
                        http_get=http_get, fingerprint=fingerprint,
                    )
                    if result["verdict"] in ("mismatch", "unreachable"):
                        print(
                            f"deploy_sync: WARNING: {name}: marked at {sha[:12]} "
                            f"but {result['detail']}",
                            file=sys.stderr,
                        )
                return 0
            summary = sync(
                config, args.state_dir, dry_run=args.dry_run,
                runner=runner, http_get=http_get, sleep=sleep,
                fingerprint=fingerprint,
            )
    except SyncBusy:
        if args.check:
            print(
                "deploy_sync: another run is active (it may be restarting "
                "targets); --check cannot answer now, try again shortly",
                file=sys.stderr,
            )
            return 1
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
