"""The Docker wizard's check of the settings the app saved for itself (#435).

On both container shapes every in-app settings write lands in
``/data/settings.env`` (``NYMERIA_SETTINGS_FILE``, #254), which loads LAST and
beats the compose environment ``.env.docker`` feeds. #434 made a shadow visible
and gave ``/settings clear``, but only the wizard knows what a reconfigure
changed: a key it DROPPED reaches the container as compose's empty string, so a
stale app copy reads as "saved only in the app" there (a GUI-onboarded install
holds its whole route that way, legitimately), never as a shadow.

So after finalize writes ``.env.docker`` (and before the stack is recreated),
this module diffs the file against what it held before the write, asks the
RUNNING container which of the changed route or credential keys the app's file
also sets (one ``compose exec -T <api|nymeria-single> python3 -c <program>``,
the channel finalize already uses), offers to remove those copies (route keys
as one group: a route is a tuple, and a mixed one sends a key to the wrong
host; credentials one question each), and removes the accepted ones IN the
container with the config layer's primitive
(``config.settings.remove_runtime_settings_keys``). The normal ``up -d``
recreate then applies both files. A real OpenAI or Gemini key whose slot this
run hands to a gateway moves to its direct slot in the app's file instead of
being discarded (the #431 class).

Names, classes and booleans cross the exec boundary; values never do, in either
direction: the programs take key names as argv and print a JSON marker, and the
wizard never echoes their stderr. Both programs contain no double quote and no
backslash, so Windows ``list2cmdline`` and the Go argv parser in ``docker.exe``
round-trip them unchanged.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Mapping, Sequence

from rich.console import Console
from rich.markup import escape

from ..config.env_file import is_env_key_name
from ..config.secret_keys import classify_setting_key, direct_key_slot

# The file inside the container (both shapes' compose files name it).
APP_SETTINGS_PATH = "/data/settings.env"
# Docker Desktop execs go through the WSL2 VM: slower, never this slow.
EXEC_TIMEOUT_SECONDS = 30

REPORT_MARKER = "NYMERIA_SETTINGS_FILE_REPORT="
APPLY_MARKER = "NYMERIA_SETTINGS_FILE_APPLY="
ERROR_MARKER = "NYMERIA_SETTINGS_FILE_ERROR="
# What an image from before this report printed (#254): names only.
LEGACY_OVERRIDES_MARKER = "NYMERIA_SETTINGS_OVERRIDES="
UNSUPPORTED = "unsupported"

# Read-only. The image's own report (`config.settings.runtime_settings_file_
# report`), judged after the server's own env load. An image older than the
# report (the module imports, the function is missing) says `unsupported` and,
# when it has the #254 override list, prints that too, so the post-start note
# keeps working on an older published image. A module that does not import at
# all prints no marker: that is a broken stack, not an old one.
REPORT_PROGRAM = f"""import json
import nymeria.config.settings as conf
report = getattr(conf, 'runtime_settings_file_report', None)
if report is None:
    print({REPORT_MARKER + UNSUPPORTED!r})
    overrides = getattr(conf, 'runtime_settings_overrides', None)
    if overrides is not None:
        conf.load_env_files_into_environ()
        print({LEGACY_OVERRIDES_MARKER!r} + ','.join(overrides()))
else:
    try:
        conf.load_env_files_into_environ()
        print({REPORT_MARKER!r} + json.dumps(report()))
    except Exception as exc:
        print({ERROR_MARKER!r} + type(exc).__name__)
"""

# Writes. Argv: KEY names to remove, and SLOT:DIRECT pairs to move a shared
# slot's real vendor key to its direct twin (the container re-checks both).
APPLY_PROGRAM = f"""import json, sys
import nymeria.config.settings as conf
remove = getattr(conf, 'remove_runtime_settings_keys', None)
if remove is None:
    print({APPLY_MARKER + UNSUPPORTED!r})
else:
    names = [arg for arg in sys.argv[1:] if ':' not in arg]
    moves = dict(arg.split(':', 1) for arg in sys.argv[1:] if ':' in arg)
    try:
        conf.load_env_files_into_environ()
        print({APPLY_MARKER!r} + json.dumps(remove(names, relocate=moves)))
    except Exception as exc:
        print({ERROR_MARKER!r} + type(exc).__name__)
"""

# DP4: the models ride the route group. A model is a plain setting by #434's
# classifier (a wrong model is an error, not a leak), but an app-saved gpt-*
# model under this run's new anthropic route fails every turn.
ROUTE_MODEL_KEYS: frozenset[str] = frozenset(
    {"LLM_MODEL", "LLM_FAST_MODEL", "LLM_SMART_MODEL", "LLM_BACKGROUND_MODEL"}
)

_VENDOR_LABELS = {"openai": "OpenAI", "google": "Google AI (Gemini)"}
_CLASSES = frozenset({"credential", "route", "setting"})
_KINDS = frozenset({"override", "blanked", "app_only", "same"})
_SHAPES = frozenset({"vendor", "gateway", "gatekeeper"})
_STATUSES = frozenset({
    "cleared", "relocated", "not_saved", "pinned_line_removed", "unparsable",
    "relocation_refused", "malformed",
})

ProbeStatus = Literal["report", "unsupported", "unreachable"]


# --- the container channel ----------------------------------------------------


@dataclass(frozen=True)
class ComposeChannel:
    """How to reach the running stack: finalize's compose exec, plus copy.

    ``exec_argv`` is ``docker compose <args> exec -T <service>`` (the api
    service on the full stack, never the worker; ``nymeria-single`` on both
    single-container composes) and ``env`` is finalize's ``_compose_env``.
    ``start_command`` is the ``up -d`` line that recreates the stack (a
    callable: deciding it may probe the image); ``exec_hint`` the
    user-facing ``docker compose ... exec <service>`` prefix for a recipe.
    """

    exec_argv: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str]
    start_command: Callable[[], str]
    exec_hint: str


def _run_program(
    channel: ComposeChannel, program: str, args: Sequence[str] = ()
) -> subprocess.CompletedProcess[str] | None:
    """Run one program in the container; None when it could not be run at all."""
    try:
        # env-gate: full-copy - finalize's `_compose_env`, for the reason its
        # own token-file exec gives: compose resolves the project from
        # `${...}` in the process environment on the no-`--env-file` path.
        # The program runs inside the already-running container with ITS
        # environment; only key names travel in argv.
        return subprocess.run(
            [*channel.exec_argv, "python3", "-c", program, *args],
            cwd=str(channel.cwd),
            env=dict(channel.env),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=EXEC_TIMEOUT_SECONDS,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def _marker_payload(stdout: str, marker: str) -> str | None:
    for line in (stdout or "").splitlines():
        if line.startswith(marker):
            return line[len(marker):].strip()
    return None


# --- the report -----------------------------------------------------------------


@dataclass(frozen=True)
class FileKey:
    """One key the app's file sets: names, classes and booleans, never a value."""

    key: str
    key_class: str
    kind: str
    empty: bool = False
    shape: str | None = None
    direct_set: bool = False

    @property
    def shadows(self) -> bool:
        return self.kind in ("override", "blanked")


@dataclass(frozen=True)
class FileReport:
    """What the container said. ``unsupported``: an image older than the report
    (``legacy_overrides`` then carries its #254 override names, when it had
    them); ``unreachable``: no answer (stack down, docker down, timeout)."""

    status: ProbeStatus
    keys: tuple[FileKey, ...] = ()
    legacy_overrides: tuple[str, ...] = ()

    def entry(self, key: str) -> FileKey | None:
        return next((item for item in self.keys if item.key == key), None)


def _parse_entry(raw: Any) -> FileKey | None:
    if not isinstance(raw, dict):
        return None
    key = raw.get("key")
    key_class = raw.get("class")
    kind = raw.get("kind")
    if not isinstance(key, str) or not is_env_key_name(key):
        return None
    if key_class not in _CLASSES or kind not in _KINDS:
        return None
    shape = raw.get("shape")
    return FileKey(
        key=key,
        key_class=str(key_class),
        kind=str(kind),
        empty=raw.get("empty") is True,
        shape=shape if shape in _SHAPES else None,
        direct_set=raw.get("direct_set") is True,
    )


def parse_report(stdout: str) -> FileReport:
    """The report marker, validated name by name; ``unreachable`` without one."""
    payload = _marker_payload(stdout, REPORT_MARKER)
    if payload == UNSUPPORTED:
        legacy = _marker_payload(stdout, LEGACY_OVERRIDES_MARKER) or ""
        names = (name.strip() for name in legacy.split(","))
        return FileReport(
            "unsupported", legacy_overrides=tuple(n for n in names if is_env_key_name(n))
        )
    if payload is None:
        return FileReport("unreachable")
    try:
        data = json.loads(payload)
    except ValueError:
        return FileReport("unreachable")
    raw_keys = data.get("keys") if isinstance(data, dict) else None
    entries = [_parse_entry(raw) for raw in (raw_keys if isinstance(raw_keys, list) else [])]
    return FileReport("report", tuple(entry for entry in entries if entry is not None))


def probe(channel: ComposeChannel) -> FileReport:
    """Ask the running container what the app's file sets (one read-only exec)."""
    result = _run_program(channel, REPORT_PROGRAM)
    if result is None:
        return FileReport("unreachable")
    return parse_report(result.stdout)


def apply(
    channel: ComposeChannel, keys: Sequence[str], moves: Mapping[str, str]
) -> dict[str, str] | None:
    """Remove ``keys`` (and move ``moves``' slots) in the container; per-key
    status, or None when the container could not do it (no answer, an older
    image, a read failure)."""
    args = [key for key in keys if key not in moves and is_env_key_name(key)]
    args += [
        f"{slot}:{direct}"
        for slot, direct in moves.items()
        if is_env_key_name(slot) and is_env_key_name(direct)
    ]
    if not args:
        return {}
    result = _run_program(channel, APPLY_PROGRAM, args)
    payload = _marker_payload(result.stdout if result else "", APPLY_MARKER)
    if payload is None or payload == UNSUPPORTED:
        return None
    try:
        data = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    return {
        str(key): str(status)
        for key, status in data.items()
        if isinstance(key, str) and is_env_key_name(key) and status in _STATUSES
    }


# --- what this run changed ------------------------------------------------------


def snapshot_env_file(path: Path) -> dict[str, str]:
    """``path``'s non-empty values, raw (no ``${VAR}`` expansion). In memory only.

    Empty counts as unset, like compose's ``${VAR:-}``: dropping a blank line
    changes nothing a container sees.
    """
    if not path.is_file():
        return {}
    from dotenv import dotenv_values

    try:
        values = dotenv_values(path, interpolate=False)
    except (OSError, UnicodeDecodeError, ValueError):
        return {}
    return {key: value for key, value in values.items() if value}


@dataclass(frozen=True)
class ConfigDiff:
    """Key NAMES this run's write changed, added or dropped (values compared
    in memory and discarded), plus the names the new file sets."""

    changed: frozenset[str] = frozenset()
    added: frozenset[str] = frozenset()
    dropped: frozenset[str] = frozenset()
    after_keys: frozenset[str] = frozenset()

    @property
    def keys(self) -> frozenset[str]:
        return self.changed | self.added | self.dropped

    @property
    def route_changed(self) -> bool:
        return any(classify_setting_key(key) == "route" for key in self.keys)

    def candidate_names(self) -> tuple[str, ...]:
        """The route and credential keys this run changed, routes first."""
        from ..config.settings import CONTAINER_PINNED_KEYS

        names = [
            key
            for key in self.keys
            if key not in CONTAINER_PINNED_KEYS
            and classify_setting_key(key) in ("credential", "route")
        ]
        return tuple(sorted(names, key=lambda k: (classify_setting_key(k) != "route", k)))


def config_diff(before: Mapping[str, str], after: Mapping[str, str]) -> ConfigDiff:
    """Compare two ``snapshot_env_file`` results by name."""
    return ConfigDiff(
        changed=frozenset(k for k in before.keys() & after.keys() if before[k] != after[k]),
        added=frozenset(after.keys() - before.keys()),
        dropped=frozenset(before.keys() - after.keys()),
        after_keys=frozenset(after),
    )


# --- which app copies to ask about -----------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """An app-saved copy this run's change makes stale."""

    entry: FileKey
    group: Literal["route", "credential"]
    # This run removed the key from .env.docker; the app's copy brings it back.
    dropped: bool = False
    # The direct slot the app's real vendor key moves to, when offered.
    relocate_to: str | None = None

    @property
    def key(self) -> str:
        return self.entry.key

    def label(self) -> str:
        cls = "model" if self.key in ROUTE_MODEL_KEYS else self.entry.key_class
        if self.dropped:
            return f"{self.key} ({cls}; this setup removed it, the app's copy would bring it back)"
        return f"{self.key} ({cls})"


def select_candidates(
    report: FileReport,
    diff: ConfigDiff,
    *,
    precise: bool,
    gateway_slot: Callable[[str], str | None],
) -> list[Candidate]:
    """The app copies a reconfigure should ask about, route group first.

    ``precise``: the report was judged against THIS run's config (after the
    recreate). Before it, the container still runs the OLD config, so a copy
    of a key this run changed is asked about even when it equals the old
    value (it would still beat the new one); a copy of a key this run did not
    touch that equals what the container runs is skipped either way.

    - A route or credential key this run changed, added or dropped, that the
      file sets.
    - Route tuple: once this run changes a route key, every route key the
      file sets (and, DP4, every model), changed by this run or not: a GUI
      route's LLM_BASE_URL left under the wizard's new provider would send
      the new key to the old gateway.
    - Also on a route change: a shared slot holding a GATEWAY's key (judged
      under the app's own route), which only works with the gateway route
      being replaced; kept, the media tools would send it to the vendor (#433).

    ``gateway_slot(slot)`` is this run's ``tool_keys.gateway_direct_slot``:
    a vendor-shaped copy moves there when the container says the direct slot
    is empty and the new ``.env.docker`` does not set it either.
    """
    if report.status != "report":
        return []
    route_changed = diff.route_changed
    picked: list[Candidate] = []
    for entry in report.keys:
        in_diff = entry.key in diff.keys
        route_member = entry.key_class == "route" or (
            route_changed and entry.key in ROUTE_MODEL_KEYS
        )
        if in_diff and (entry.key_class == "credential" or route_member):
            pass
        elif route_changed and route_member:
            pass
        elif route_changed and entry.key_class == "credential" and entry.shape == "gateway":
            pass
        else:
            continue
        if entry.kind == "same" and (precise or not in_diff):
            continue
        if entry.empty and entry.key not in diff.after_keys:
            continue  # a blank line over a key the new config leaves unset
        relocate_to = None
        if entry.shape == "vendor" and not entry.direct_set:
            direct = gateway_slot(entry.key)
            if direct and direct not in diff.after_keys:
                relocate_to = direct
        picked.append(
            Candidate(
                entry,
                "route" if route_member else "credential",
                dropped=entry.key in diff.dropped,
                relocate_to=relocate_to,
            )
        )
    return sorted(picked, key=lambda c: (c.group != "route", c.key))


# --- the conversation ---------------------------------------------------------


@dataclass
class ShadowRun:
    """This run's record, read by the post-start note (and catch-up)."""

    diff: ConfigDiff
    # The pre-start check got a report from the running container, or had
    # nothing to check. False sends a wizard-run start to check again (DP2).
    checked: bool = False
    # Keys gone from the app's file because of this run (cleared, moved, or
    # already gone when the removal ran): never named as a shadow afterwards.
    cleared: set[str] = field(default_factory=set)


def _names(keys: Iterable[str]) -> str:
    return ", ".join(keys)


def _clear_commands(keys: Sequence[str]) -> str:
    commands = [f"/settings clear {key}" for key in keys]
    if len(commands) <= 1:
        return "".join(commands)
    return ", ".join(commands[:-1]) + " and " + commands[-1]


def _discard_note(candidate: Candidate) -> str:
    """What removing this credential copy does to it (never its value)."""
    key = candidate.key
    if candidate.relocate_to:
        from ..config.vendor_keys import VENDOR_KEY_SLOTS

        slot = VENDOR_KEY_SLOTS.get(key)
        vendor = _VENDOR_LABELS.get(slot.vendor, "vendor") if slot else "vendor"
        return (
            f"The app's copy of {key} looks like a real {vendor} key, and this "
            f"setup's route sends {key} to a gateway, so it moves to "
            f"{candidate.relocate_to} in the app's settings (the slot the media "
            "tools read) instead of being discarded."
        )
    if candidate.entry.shape == "gateway":
        return (
            f"The app's copy of {key} is a gateway's key: it only works with the "
            "app's own gateway route, so removing it discards nothing this setup "
            "uses."
        )
    direct = direct_key_slot(key)
    if candidate.entry.shape == "vendor" and candidate.entry.direct_set and direct:
        return (
            f"Removing it discards the app's copy ({direct} is already set, so it "
            "is not moved there)."
        )
    if direct:
        return (
            f"Removing it discards the app's copy: if it is a real vendor key you "
            f"still need, save it as {direct} first."
        )
    return "Removing it discards the app's copy."


def _clear_hint(candidate: Candidate) -> str:
    """What a later ``/settings clear`` does to this copy (it never moves one)."""
    key = candidate.key
    if candidate.entry.shape == "gateway":
        return (
            f" The app's copy of {key} is a gateway's key: it only works with the "
            "app's own gateway route."
        )
    direct = direct_key_slot(key)
    if not direct:
        return ""
    hint = (
        f" Clearing {key} discards the app's copy: if it is a real vendor key you "
        f"still need, save it as {direct} first."
    )
    if candidate.relocate_to:
        hint += (
            f" (--clear-app-overrides moves it to {candidate.relocate_to} "
            "instead.)"
        )
    return hint


def _ask(console: Console, prompt: str) -> bool:
    """Default yes; "n...", EOF (Ctrl+D) and Ctrl+C decline (the local-rag prompt)."""
    try:
        answer = console.input(prompt + r" \[Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return not answer.startswith("n")


def _when(started: bool) -> str:
    """Where the app's copy wins: the stack about to be recreated, or the one
    the wizard just started (the post-start catch-up)."""
    return "in the stack that just started" if started else "once the stack is recreated"


def _kept_warning(
    console: Console, candidates: Sequence[Candidate], *, started: bool = False
) -> None:
    keys = [c.key for c in candidates]
    hints = "".join(_clear_hint(c) for c in candidates if c.group == "credential")
    console.print(
        f"[yellow]Kept the app's copy of {_names(keys)}: it overrides this setup "
        f"{_when(started)}. To use this setup's value later, an admin runs "
        f"{_clear_commands(keys)} in the app.{escape(hints)}[/yellow]"
    )


def _warn_only(
    console: Console, candidates: Sequence[Candidate], *, started: bool = False
) -> None:
    keys = [c.key for c in candidates]
    hints = "".join(_clear_hint(c) for c in candidates if c.group == "credential")
    console.print(
        f"\n[yellow]Settings saved in the app override this setup {_when(started)}: "
        f"the app's copy ({APP_SETTINGS_PATH} on the data volume) loads last and "
        f"sets {_names(c.label() for c in candidates)}. To use this setup's "
        f"values, an admin runs {_clear_commands(keys)} in the app"
        + ("" if started else " after the start")
        + ", or re-run setup with --clear-app-overrides to remove them now."
        + f"{escape(hints)}[/yellow]"
    )


def _report_outcomes(
    console: Console,
    channel: ComposeChannel,
    attempted: Sequence[Candidate],
    statuses: Mapping[str, str] | None,
    run: ShadowRun,
    *,
    applies_when: str,
) -> list[str]:
    """Print each key's outcome; return the keys whose copy left the file."""
    keys = [c.key for c in attempted]
    if statuses is None:
        console.print(
            "[yellow]Could not remove the app's copies: the container did not "
            f"answer. An admin runs {_clear_commands(keys)} in the app once the "
            "stack is up.[/yellow]"
        )
        return []
    removed = [k for k in keys if statuses.get(k) in ("cleared", "pinned_line_removed")]
    moved = [c for c in attempted if statuses.get(c.key) == "relocated"]
    gone = [k for k in keys if statuses.get(k) == "not_saved"]
    run.cleared.update(removed, (c.key for c in moved), gone)
    if removed:
        console.print(f"[green]Removed the app's copy of {_names(removed)}.[/green]")
    for candidate in moved:
        console.print(
            f"[green]Moved the app's copy of {candidate.key} to "
            f"{candidate.relocate_to} (in the app's settings).[/green]"
        )
    if gone:
        console.print(f"{_names(gone)}: already gone from the app's settings.")
    for key in keys:
        status = statuses.get(key)
        if status == "unparsable":
            recipe = (
                f"{channel.exec_hint} sed -i -E "
                f"'/^[[:space:]]*(export[[:space:]]+)?{key}[[:space:]]*=/d' "
                f"{APP_SETTINGS_PATH}"
            )
            console.print(
                f"[yellow]Could not remove {key}: its line in {APP_SETTINGS_PATH} "
                "is not in KEY=value form (an `export` line?). Remove that line "
                f"inside the container, for example `{escape(recipe)}`, then "
                "recreate the stack.[/yellow]"
            )
        elif status == "relocation_refused":
            console.print(
                f"[yellow]Kept {key}: the app's copy no longer qualifies for the "
                "move to its direct slot (it changed since setup checked, or that "
                "slot is set now), so nothing was discarded. An admin can run "
                f"/settings clear {key} in the app once the key is safe "
                "elsewhere.[/yellow]"
            )
        elif status not in ("cleared", "pinned_line_removed", "relocated", "not_saved"):
            console.print(
                f"[yellow]Could not remove {key}. An admin can run /settings "
                f"clear {key} in the app.[/yellow]"
            )
    if removed or moved:
        console.print(applies_when)
    return [*removed, *(c.key for c in moved)]


def resolve_candidates(
    console: Console,
    channel: ComposeChannel,
    candidates: Sequence[Candidate],
    run: ShadowRun,
    *,
    mode: bool | None,
    interactive: bool,
    applies_when: str,
    started: bool = False,
) -> list[str]:
    """Ask (or follow the flag), then remove the accepted copies in one exec.

    ``mode``: True clears every candidate without asking, False never clears
    (warn only), None asks when ``interactive`` and warns otherwise.
    ``started``: the stack is already running this run's config (the
    post-start catch-up), which only changes the copy. Returns the keys whose
    copy left the app's file.
    """
    if not candidates:
        return []
    if mode is False or (mode is None and not interactive):
        _warn_only(console, candidates, started=started)
        return []
    accepted: list[Candidate] = []
    if mode is True:
        accepted = list(candidates)
    else:
        console.print(
            "\n[bold]Settings saved in the app[/bold]\nThe app keeps its own copy "
            f"of some settings this setup changed ({APP_SETTINGS_PATH} on the data "
            "volume). That copy loads last, so it "
            + ("overrides this setup in the stack that just started." if started
               else "would override this setup once the stack is recreated.")
        )
        route = [c for c in candidates if c.group == "route"]
        if route:
            console.print(f"Route settings the app saved: {_names(c.label() for c in route)}.")
            if _ask(
                console,
                f"Remove the app-saved route settings ({_names(c.key for c in route)}) "
                "so this choice takes effect?",
            ):
                accepted.extend(route)
            else:
                _kept_warning(console, route, started=started)
        for candidate in (c for c in candidates if c.group == "credential"):
            notes = []
            if candidate.dropped:
                notes.append(
                    f"This setup removed {candidate.key}; the app's copy would bring it back."
                )
            notes.append(_discard_note(candidate))
            console.print(escape(" ".join(notes)))
            if _ask(
                console,
                f"Remove the app-saved {candidate.key} so this choice takes effect?",
            ):
                accepted.append(candidate)
            else:
                _kept_warning(console, [candidate], started=started)
    if not accepted:
        return []
    moves = {c.key: c.relocate_to for c in accepted if c.relocate_to}
    statuses = apply(channel, [c.key for c in accepted], moves)
    return _report_outcomes(
        console, channel, accepted, statuses, run, applies_when=applies_when
    )


def _warn_unchecked(console: Console, report: FileReport, diff: ConfigDiff) -> None:
    names = diff.candidate_names()
    why = (
        "the running image predates this check"
        if report.status == "unsupported"
        else "the stack is not running (or did not answer)"
    )
    labels = _names(f"{key} ({classify_setting_key(key)})" for key in names)
    console.print(
        f"\n[yellow]Setup could not check the settings saved in the app: {why}. "
        f"If the app saved its own copy ({APP_SETTINGS_PATH} on the data volume) "
        "of a key this setup changed, that copy loads last and overrides this "
        f"setup once the stack is up: {labels}. After the start an admin can run "
        "/settings clear <KEY> in the app for each (it says there is nothing to "
        "clear when the app saved none).[/yellow]"
    )


def check_before_start(
    console: Console,
    channel: ComposeChannel,
    diff: ConfigDiff,
    *,
    mode: bool | None,
    interactive: bool,
    gateway_slot: Callable[[str], str | None],
) -> ShadowRun:
    """The pre-start step: no exec at all unless this run changed a route or
    credential key; then one probe, the questions, and one apply."""
    run = ShadowRun(diff)
    if not diff.candidate_names():
        run.checked = True  # nothing this run changed can be shadowed
        return run
    report = probe(channel)
    if report.status != "report":
        _warn_unchecked(console, report, diff)
        return run
    run.checked = True
    candidates = select_candidates(
        report, diff, precise=False, gateway_slot=gateway_slot
    )
    resolve_candidates(
        console,
        channel,
        candidates,
        run,
        mode=mode,
        interactive=interactive,
        applies_when=(
            "This takes effect when the stack is recreated: the start command "
            f"(`{escape(channel.start_command())}`) does that."
        ),
    )
    return run


def check_after_start(
    console: Console,
    channel: ComposeChannel,
    report: FileReport,
    run: ShadowRun,
    *,
    mode: bool | None,
    interactive: bool,
    gateway_slot: Callable[[str], str | None],
) -> tuple[frozenset[str], list[str]]:
    """DP2: the catch-up when the pre-start check could not reach the stack and
    the wizard then started it (Docker Desktop stopped between uses, say).

    ``report`` is the post-start probe, judged against THIS run's config, so
    the selection is precise: a copy equal to the new value is not asked
    about. Same questions and removal as before the start; the caller then
    restarts the services that load the file. Returns the keys it raised
    (asked or warned about, so the closing note does not repeat them) and the
    keys whose copy left the file (empty: no restart needed).
    """
    if run.checked or report.status != "report":
        return frozenset(), []
    run.checked = True
    candidates = select_candidates(report, run.diff, precise=True, gateway_slot=gateway_slot)
    changed = resolve_candidates(
        console,
        channel,
        candidates,
        run,
        mode=mode,
        interactive=interactive,
        applies_when="The running stack loaded them at boot, so setup restarts it:",
        started=True,
    )
    return frozenset(c.key for c in candidates), changed


def is_interactive(non_interactive: bool) -> bool:
    """A prompt needs a terminal a human answers (the local-rag rule)."""
    return not non_interactive and sys.stdin.isatty()


__all__ = [
    "APPLY_PROGRAM",
    "APP_SETTINGS_PATH",
    "Candidate",
    "ComposeChannel",
    "ConfigDiff",
    "FileKey",
    "FileReport",
    "REPORT_PROGRAM",
    "ROUTE_MODEL_KEYS",
    "ShadowRun",
    "apply",
    "check_after_start",
    "check_before_start",
    "config_diff",
    "is_interactive",
    "parse_report",
    "probe",
    "resolve_candidates",
    "select_candidates",
    "snapshot_env_file",
]
