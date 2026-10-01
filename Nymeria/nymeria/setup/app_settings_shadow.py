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
direction: the programs take key names (and ``route:`` facts, two booleans per
shared slot) as argv and print a JSON marker, and the wizard never echoes their
stderr. Both programs contain no double quote and no backslash, so Windows
``list2cmdline`` and the Go argv parser in ``docker.exe`` round-trip them
unchanged.

When the stack was down before the start and the wizard starts it, the same
questions run once it is healthy (the catch-up). The container then already
runs THIS run's route, which is not the route the app saved its copy under, so
the wizard passes the OLD route as ``vendor_keys.RouteFacts`` and both programs
judge a shared slot's shape under it: a real vendor key moves exactly as it
would have before the start, and a key whose route cannot be judged is
``unknown`` (never moved, never removed without an explicit yes).
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
from ..config.vendor_keys import (
    ROUTE_FACTS_PREFIX,
    UNKNOWN_ROUTE,
    VENDOR_KEY_SLOTS,
    RouteFacts,
    route_facts,
    route_facts_tokens,
)

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
# all prints no marker: that is a broken stack, not an old one. Argv: optional
# `route:` facts (the catch-up's old route; none before the start).
REPORT_PROGRAM = f"""import json, sys
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
        from nymeria.config.vendor_keys import parse_route_facts
        conf.load_env_files_into_environ()
        prior = parse_route_facts(sys.argv[1:])
        print({REPORT_MARKER!r} + json.dumps(report(prior_route=prior)))
    except Exception as exc:
        print({ERROR_MARKER!r} + type(exc).__name__)
"""

# Writes. Argv: KEY names to remove, SLOT:DIRECT pairs to move a shared slot's
# real vendor key to its direct twin (the container re-checks both), and the
# report's `route:` facts, so the re-check judges under the same route.
APPLY_PROGRAM = f"""import json, sys
import nymeria.config.settings as conf
remove = getattr(conf, 'remove_runtime_settings_keys', None)
if remove is None:
    print({APPLY_MARKER + UNSUPPORTED!r})
else:
    args = [arg for arg in sys.argv[1:] if not arg.startswith({ROUTE_FACTS_PREFIX!r})]
    names = [arg for arg in args if ':' not in arg]
    moves = dict(arg.split(':', 1) for arg in args if ':' in arg)
    try:
        from nymeria.config.vendor_keys import parse_route_facts
        conf.load_env_files_into_environ()
        prior = parse_route_facts(sys.argv[1:])
        print({APPLY_MARKER!r} + json.dumps(remove(names, relocate=moves, prior_route=prior)))
    except Exception as exc:
        print({ERROR_MARKER!r} + type(exc).__name__)
"""

# The LLM route: the keys a route change moves together, asked as ONE group
# (DP3: a mixed route sends a key to the wrong host). The background base URL
# reuses the main route's key, so it travels with it. Every one is classed
# `route`; the other route-class keys (EMBEDDING_, TTS_, STT_BASE_URL) are
# separate services, asked about one by one and only when this run changes
# them (review F2: an embedder change once offered to wipe the LLM route).
LLM_ROUTE_KEYS: frozenset[str] = frozenset({
    "LLM_PROVIDER", "LLM_BASE_URL", "LLM_PROVIDER_ROUTE", "OPENAI_API_MODE",
    "CLIPROXY_MANAGEMENT_URL", "LLM_BACKGROUND_BASE_URL",
})

# DP4: the models ride the route group. A model is a plain setting by #434's
# classifier (a wrong model is an error, not a leak), but an app-saved gpt-*
# model under this run's new anthropic route fails every turn.
ROUTE_MODEL_KEYS: frozenset[str] = frozenset(
    {"LLM_MODEL", "LLM_FAST_MODEL", "LLM_SMART_MODEL", "LLM_BACKGROUND_MODEL"}
)

_VENDOR_LABELS = {"openai": "OpenAI", "google": "Google AI (Gemini)"}
_CLASSES = frozenset({"credential", "route", "setting"})
_KINDS = frozenset({"override", "blanked", "app_only", "same"})
_SHAPES = frozenset({"vendor", "gateway", "gatekeeper", "unknown"})
_STATUSES = frozenset({
    "cleared", "relocated", "not_saved", "pinned_line_removed", "unparsable",
    "relocation_refused", "malformed",
})

ProbeStatus = Literal["report", "unsupported", "unreachable"]
# What a removal that got no per-key answer did: `not_applied` (nothing ran in
# the container, or an image without the step), `unconfirmed` (it ran, or may
# have, then no answer: the one atomic write may or may not be in place).
NOT_APPLIED = "not_applied"
UNCONFIRMED = "unconfirmed"
ApplyOutcome = Literal["not_applied", "unconfirmed"]


# --- the container channel ----------------------------------------------------


@dataclass(frozen=True)
class ComposeChannel:
    """How to reach the running stack: finalize's compose exec, plus copy.

    ``exec_argv`` is ``docker compose <args> exec -T <service>`` (the api
    service on the full stack, never the worker; ``nymeria-single`` on both
    single-container composes) and ``env`` is finalize's ``_compose_env``.
    ``start_command`` is the ``up -d`` line that recreates the stack (a
    callable: deciding it may probe the image, so it runs only when a message
    needs it); ``restart_command`` restarts the services that load the app's
    file; ``exec_hint`` the user-facing ``docker compose ... exec <service>``
    prefix for a recipe.
    """

    exec_argv: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str]
    start_command: Callable[[], str]
    restart_command: str
    exec_hint: str


def _run_program(
    channel: ComposeChannel, program: str, args: Sequence[str] = ()
) -> subprocess.CompletedProcess[str] | None:
    """Run one program in the container; None when docker could not be started.

    A timeout raises ``subprocess.TimeoutExpired``: the program may have run.
    """
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
    except (OSError, ValueError):
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


def probe(
    channel: ComposeChannel, *, prior_route: Mapping[str, RouteFacts] | None = None
) -> FileReport:
    """Ask the running container what the app's file sets (one read-only exec).

    ``prior_route``: the route the app's copies were saved under, when the
    container no longer runs it (the post-start catch-up).
    """
    args = route_facts_tokens(prior_route) if prior_route is not None else []
    try:
        result = _run_program(channel, REPORT_PROGRAM, args)
    except subprocess.TimeoutExpired:
        return FileReport("unreachable")
    if result is None:
        return FileReport("unreachable")
    return parse_report(result.stdout)


def apply(
    channel: ComposeChannel,
    keys: Sequence[str],
    moves: Mapping[str, str],
    *,
    prior_route: Mapping[str, RouteFacts] | None = None,
) -> dict[str, str] | ApplyOutcome:
    """Remove ``keys`` (and move ``moves``' slots) in the container.

    Per-key status; ``NOT_APPLIED`` when nothing ran there (docker could not
    start, or an image without the step); ``UNCONFIRMED`` when it ran, or may
    have, without an answer (a timeout, no marker, an error marker): the
    write is one atomic replace, so it is either fully in place or not at
    all, and the caller cannot tell which. ``prior_route`` as for ``probe``.
    """
    args = [key for key in keys if key not in moves and is_env_key_name(key)]
    args += [
        f"{slot}:{direct}"
        for slot, direct in moves.items()
        if is_env_key_name(slot) and is_env_key_name(direct)
    ]
    if not args:
        return {}
    if prior_route is not None:
        args += route_facts_tokens(prior_route)
    try:
        result = _run_program(channel, APPLY_PROGRAM, args)
    except subprocess.TimeoutExpired:
        return UNCONFIRMED
    if result is None:
        return NOT_APPLIED
    payload = _marker_payload(result.stdout, APPLY_MARKER)
    if payload == UNSUPPORTED:
        return NOT_APPLIED
    if payload is None:
        return UNCONFIRMED
    try:
        data = json.loads(payload)
    except ValueError:
        return UNCONFIRMED
    if not isinstance(data, dict):
        return UNCONFIRMED
    return {
        str(key): str(status)
        for key, status in data.items()
        if isinstance(key, str) and is_env_key_name(key) and status in _STATUSES
    }


# --- what this run changed ------------------------------------------------------


def snapshot_env_file(path: Path) -> dict[str, str] | None:
    """``path``'s non-empty values, raw (no ``${VAR}`` expansion). In memory only.

    Empty counts as unset, like compose's ``${VAR:-}``: dropping a blank line
    changes nothing a container sees. ``{}`` for a missing file, None for one
    that cannot be read (its contents are unknown, not empty).
    """
    if not path.is_file():
        return {}
    from dotenv import dotenv_values

    try:
        values = dotenv_values(path, interpolate=False)
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    return {key: value for key, value in values.items() if value}


def _all_unknown() -> dict[str, RouteFacts]:
    return dict.fromkeys(VENDOR_KEY_SLOTS, UNKNOWN_ROUTE)


@dataclass(frozen=True)
class ConfigDiff:
    """Key NAMES this run's write changed, added or dropped (values compared
    in memory and discarded), plus the names the new file sets.

    ``recreates``: the file's values changed, so compose recreates the stack
    on the next ``up -d`` (a ``--force`` rewrite of the same values does not).
    ``prior_route``: the OLD file's LLM route as booleans per shared slot
    (``RouteFacts``), for the post-start check, whose container already runs
    the new one.
    """

    changed: frozenset[str] = frozenset()
    added: frozenset[str] = frozenset()
    dropped: frozenset[str] = frozenset()
    after_keys: frozenset[str] = frozenset()
    recreates: bool = True
    prior_route: Mapping[str, RouteFacts] = field(default_factory=_all_unknown)

    @property
    def keys(self) -> frozenset[str]:
        return self.changed | self.added | self.dropped

    @property
    def llm_route_changed(self) -> bool:
        return bool(self.keys & LLM_ROUTE_KEYS)

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


def _known(value: str) -> str | None:
    """A raw `.env.docker` value, or None when compose resolves it from
    elsewhere (a ``$VAR`` reference), so the host cannot know it."""
    return None if "$" in value else value


def _prior_route(before: Mapping[str, str] | None) -> dict[str, RouteFacts]:
    if before is None:
        return _all_unknown()
    provider = _known(before.get("LLM_PROVIDER", ""))
    base_url = _known(before.get("LLM_BASE_URL", ""))
    return {
        slot: route_facts(slot, provider=provider, base_url=base_url)
        for slot in VENDOR_KEY_SLOTS
    }


def config_diff(
    before: Mapping[str, str] | None, after: Mapping[str, str], *, fresh: bool = False
) -> ConfigDiff:
    """Compare two ``snapshot_env_file`` results by name.

    ``fresh`` (``--force``) counts every key the new file writes as added,
    and so does an unreadable ``before`` (None); the old values still decide
    ``recreates`` and ``prior_route`` when they are known.
    """
    base = {} if fresh or before is None else before
    return ConfigDiff(
        changed=frozenset(k for k in base.keys() & after.keys() if base[k] != after[k]),
        added=frozenset(after.keys() - base.keys()),
        dropped=frozenset(base.keys() - after.keys()),
        after_keys=frozenset(after),
        recreates=before is None or dict(before) != dict(after),
        prior_route=_prior_route(before),
    )


# --- which app copies to ask about -----------------------------------------------


CandidateGroup = Literal["route", "single", "credential"]
_GROUP_ORDER = {"route": 0, "single": 1, "credential": 2}


@dataclass(frozen=True)
class Candidate:
    """An app-saved copy this run's change makes stale.

    ``group``: ``route`` (the LLM route group, one question), ``single`` (a
    separate service's base URL, its own question) or ``credential`` (its own
    question).
    """

    entry: FileKey
    group: CandidateGroup
    # This run removed the key from .env.docker; the app's copy brings it back.
    dropped: bool = False
    # The direct slot the app's real vendor key moves to, when offered.
    relocate_to: str | None = None
    # Route group only, when this run did not change the LLM route itself: the
    # changed keys the app's OWN saved route sends to its gateway.
    pulled_by: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return self.entry.key

    @property
    def unknown(self) -> bool:
        """A vendor-shaped copy whose route could not be judged: maybe the user's
        only copy of a real key, so it is never moved or removed unasked."""
        return self.entry.shape == "unknown"

    @property
    def default_yes(self) -> bool:
        """Enter means yes, except for a credential whose copy would be
        discarded (review S3): that takes an explicit yes."""
        return self.group != "credential" or bool(self.relocate_to)

    def label(self) -> str:
        cls = "model" if self.key in ROUTE_MODEL_KEYS else self.entry.key_class
        if self.dropped:
            return f"{self.key} ({cls}; this setup removed it, the app's copy would bring it back)"
        return f"{self.key} ({cls})"


def _settled(entry: FileKey, diff: ConfigDiff, *, precise: bool) -> bool:
    """The app's copy changes nothing this run cares about."""
    if entry.kind == "same" and (precise or entry.key not in diff.keys):
        return True
    # A blank line over a key the new config leaves unset.
    return entry.empty and entry.key not in diff.after_keys


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

    - A credential this run changed, added or dropped that the file sets.
    - The LLM route group (``LLM_ROUTE_KEYS`` plus, DP4, the models): once the
      route is in play, every member the file sets, changed by this run or
      not: a GUI route's LLM_BASE_URL left under the wizard's new provider
      would send the new key to the old gateway. The route is in play when
      this run changes an LLM route key, or when it changes a credential the
      app's OWN saved route (a route key the file sets) sends to its gateway
      (review MED: a key rotation would otherwise feed the new vendor key to
      the app's gateway).
    - A separate service's route key (embedding, TTS, STT base URL) only when
      this run changed it, asked alone (review F2).
    - With the route in play, a shared slot holding a GATEWAY's key (judged
      under the route the app saved it beside), which only works with the
      gateway route being replaced; kept, the media tools would send it to
      the vendor (#433).

    ``gateway_slot(slot)`` is this run's ``tool_keys.gateway_direct_slot``:
    a vendor-shaped copy moves there when the container says the direct slot
    is empty and the new ``.env.docker`` does not set it either. An
    ``unknown`` shape never moves.
    """
    if report.status != "report":
        return []
    live = [entry for entry in report.keys if not _settled(entry, diff, precise=precise)]
    app_route = [entry.key for entry in live if entry.key in LLM_ROUTE_KEYS]
    gateway_fed = tuple(
        entry.key
        for entry in live
        if entry.key_class == "credential"
        and entry.key in diff.keys
        and entry.shape == "gateway"
    )
    pulled_by = gateway_fed if app_route and not diff.llm_route_changed else ()
    route_in_play = diff.llm_route_changed or bool(pulled_by)
    picked: list[Candidate] = []
    for entry in live:
        in_diff = entry.key in diff.keys
        group: CandidateGroup
        if entry.key in LLM_ROUTE_KEYS or entry.key in ROUTE_MODEL_KEYS:
            if not route_in_play:
                continue
            group = "route"
        elif entry.key_class == "credential":
            if not (in_diff or (route_in_play and entry.shape == "gateway")):
                continue
            group = "credential"
        elif entry.key_class == "route" and in_diff:
            group = "single"
        else:
            continue
        relocate_to = None
        if entry.shape == "vendor" and not entry.direct_set:
            direct = gateway_slot(entry.key)
            if direct and direct not in diff.after_keys:
                relocate_to = direct
        picked.append(
            Candidate(
                entry,
                group,
                dropped=entry.key in diff.dropped,
                relocate_to=relocate_to,
                pulled_by=pulled_by if group == "route" else (),
            )
        )
    return sorted(picked, key=lambda c: (_GROUP_ORDER[c.group], c.key))


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
    # Keys the user answered no for: the closing note does not repeat them.
    declined: set[str] = field(default_factory=set)
    # Keys whose copy the pre-start step removed or moved (or may have):
    # with an unchanged config (`--force`) the start must restart to apply it.
    removed_before_start: list[str] = field(default_factory=list)


def _names(keys: Iterable[str]) -> str:
    return ", ".join(keys)


def _clear_commands(keys: Sequence[str]) -> str:
    commands = [f"/settings clear {key}" for key in keys]
    if len(commands) <= 1:
        return "".join(commands)
    return ", ".join(commands[:-1]) + " and " + commands[-1]


def _vendor_label(key: str) -> str:
    slot = VENDOR_KEY_SLOTS.get(key)
    return _VENDOR_LABELS.get(slot.vendor, "vendor") if slot else "vendor"


def _key_note(candidate: Candidate, *, later: bool) -> str:
    """What removing this copy does to it, never its value: ``later=False``
    as the question is asked, ``later=True`` for a later ``/settings clear``
    (which never moves a key)."""
    key = candidate.key
    direct = direct_key_slot(key)
    shape = candidate.entry.shape
    if candidate.group != "credential":
        return ""
    if shape == "gateway":
        return (
            f"The app's copy of {key} is a gateway's key: it only works with the "
            "app's own gateway route"
            + ("." if later else ", so removing it discards nothing this setup uses.")
        )
    if candidate.unknown:
        return (
            f"Setup cannot tell whether the app's copy of {key} is a real "
            f"{_vendor_label(key)} key or a gateway's (it could not read the route "
            "the app saved it under), so it is not moved"
            + (": " if later else " and removing it discards it: ")
            + f"if it is your own key, save it as {direct} first."
        )
    if candidate.relocate_to and not later:
        return (
            f"The app's copy of {key} looks like a real {_vendor_label(key)} key, and "
            f"this setup's route sends {key} to a gateway, so it moves to "
            f"{candidate.relocate_to} in the app's settings (the slot the media "
            "tools read) instead of being discarded."
        )
    if not direct:
        return "" if later else "Removing it discards the app's copy."
    if shape == "vendor" and candidate.entry.direct_set and not later:
        return (
            f"Removing it discards the app's copy ({direct} is already set, so it "
            "is not moved there)."
        )
    note = (
        f"{f'Clearing {key}' if later else 'Removing it'} discards the app's copy: "
        f"if it is a real vendor key you still need, save it as {direct} first."
    )
    if later and candidate.relocate_to:
        note += f" (--clear-app-overrides moves it to {candidate.relocate_to} instead.)"
    return note


def _later_notes(candidates: Sequence[Candidate]) -> str:
    notes = (_key_note(c, later=True) for c in candidates)
    return "".join(f" {note}" for note in notes if note)


def _route_note(route: Sequence[Candidate]) -> str:
    """Why the app's route group is asked about on a credential-only change."""
    pulled = next((c.pulled_by for c in route if c.pulled_by), ())
    if not pulled:
        return ""
    return (
        f" The app's own saved route sends {_names(pulled)} to its gateway, so "
        f"while it stays, this setup's new {_names(pulled)} goes there too."
    )


def _ask(console: Console, prompt: str, *, default: bool) -> bool:
    """Enter takes ``default``; "y..." and "n..." decide; EOF (Ctrl+D) and
    Ctrl+C decline (the local-rag prompt)."""
    suffix = r" \[Y/n] " if default else r" \[y/N] "
    try:
        answer = console.input(prompt + suffix).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    if answer.startswith("y"):
        return True
    if answer.startswith("n"):
        return False
    return default


def _when(started: bool) -> str:
    """Where the app's copy wins: the stack about to be recreated, or the one
    the wizard just started (the post-start catch-up)."""
    return "in the stack that just started" if started else "once the stack is recreated"


def _kept_warning(
    console: Console, candidates: Sequence[Candidate], *, started: bool = False
) -> None:
    keys = [c.key for c in candidates]
    console.print(
        f"[yellow]Kept the app's copy of {_names(keys)}: it overrides this setup "
        f"{_when(started)}. To use this setup's value later, an admin runs "
        f"{_clear_commands(keys)} in the app.{escape(_later_notes(candidates))}[/yellow]"
    )


def _held_warning(console: Console, candidates: Sequence[Candidate], *, started: bool) -> None:
    """--clear-app-overrides never removes a copy setup cannot judge (#435 review)."""
    keys = [c.key for c in candidates]
    console.print(
        f"[yellow]Kept the app's copy of {_names(keys)}: --clear-app-overrides "
        f"never removes a key setup cannot judge, and it overrides this setup "
        f"{_when(started)}.{escape(_later_notes(candidates))} Then an admin runs "
        f"{_clear_commands(keys)} in the app.[/yellow]"
    )


def _warn_only(
    console: Console, candidates: Sequence[Candidate], *, started: bool = False
) -> None:
    keys = [c.key for c in candidates]
    route = [c for c in candidates if c.group == "route"]
    console.print(
        f"\n[yellow]Settings saved in the app override this setup {_when(started)}: "
        f"the app's copy ({APP_SETTINGS_PATH} on the data volume) loads last and "
        f"sets {_names(c.label() for c in candidates)}.{escape(_route_note(route))} "
        f"To use this setup's values, an admin runs {_clear_commands(keys)} in the "
        "app" + ("" if started else " after the start")
        # A re-run with the same values changes nothing, so it checks nothing:
        # the flag only helps on the run that makes the change.
        + ". To have setup remove them instead, pass --clear-app-overrides on the "
        "run that makes the change (a re-run that changes nothing checks nothing)."
        + f"{escape(_later_notes(candidates))}[/yellow]"
    )


def _report_outcomes(
    console: Console,
    channel: ComposeChannel,
    attempted: Sequence[Candidate],
    statuses: Mapping[str, str] | ApplyOutcome,
    run: ShadowRun,
    *,
    applies_when: Callable[[], str],
) -> list[str]:
    """Print each key's outcome; return the keys whose copy left the file (or
    may have: an unconfirmed removal still needs the restart or recreate)."""
    keys = [c.key for c in attempted]
    if statuses == UNCONFIRMED:
        recipe = f"{channel.exec_hint} cut -d= -f1 {APP_SETTINGS_PATH}"
        console.print(
            f"[yellow]Setup could not confirm the removal of {_names(keys)}: the "
            "container did not answer, so the change may or may not have been "
            "made. To see which keys the app's settings still set (names only), "
            f"run `{escape(recipe)}`; an admin can clear any still listed with "
            f"{_clear_commands(keys)} in the app.[/yellow]"
        )
        console.print(applies_when())
        return keys
    if isinstance(statuses, str):  # NOT_APPLIED
        console.print(
            "[yellow]Could not remove the app's copies: setup could not run the "
            f"removal in the container, so nothing changed. An admin runs "
            f"{_clear_commands(keys)} in the app once the stack is up.[/yellow]"
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
        console.print(applies_when())
    return [*removed, *(c.key for c in moved)]


def resolve_candidates(
    console: Console,
    channel: ComposeChannel,
    candidates: Sequence[Candidate],
    run: ShadowRun,
    *,
    mode: bool | None,
    interactive: bool,
    applies_when: Callable[[], str],
    started: bool = False,
    prior_route: Mapping[str, RouteFacts] | None = None,
) -> list[str]:
    """Ask (or follow the flag), then remove the accepted copies in one exec.

    ``mode``: True clears every candidate without asking (except an
    ``unknown`` one, which it keeps with a warning), False never clears
    (warn only), None asks when ``interactive`` and warns otherwise.
    ``started``: the stack is already running this run's config (the
    post-start catch-up), which only changes the copy; ``prior_route`` then
    goes to the removal's re-check. ``applies_when`` is built only when
    something was removed (deciding the start command may probe the image).
    Returns the keys whose copy left the app's file (or may have).
    """
    if not candidates:
        return []
    if mode is False or (mode is None and not interactive):
        _warn_only(console, candidates, started=started)
        return []
    accepted: list[Candidate] = []
    if mode is True:
        held = [c for c in candidates if c.unknown]
        if held:
            _held_warning(console, held, started=started)
        accepted = [c for c in candidates if not c.unknown]
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
            console.print(
                f"Route settings the app saved: {_names(c.label() for c in route)}."
                + escape(_route_note(route))
            )
            if _ask(
                console,
                f"Remove the app-saved route settings ({_names(c.key for c in route)}) "
                "so this choice takes effect?",
                default=True,
            ):
                accepted.extend(route)
            else:
                run.declined.update(c.key for c in route)
                _kept_warning(console, route, started=started)
        for candidate in (c for c in candidates if c.group != "route"):
            notes = []
            if candidate.dropped:
                notes.append(
                    f"This setup removed {candidate.key}; the app's copy would bring it back."
                )
            if note := _key_note(candidate, later=False):
                notes.append(note)
            if notes:
                console.print(escape(" ".join(notes)))
            if _ask(
                console,
                f"Remove the app-saved {candidate.key} so this choice takes effect?",
                default=candidate.default_yes,
            ):
                accepted.append(candidate)
            else:
                run.declined.add(candidate.key)
                _kept_warning(console, [candidate], started=started)
    if not accepted:
        return []
    moves = {c.key: c.relocate_to for c in accepted if c.relocate_to}
    statuses = apply(channel, [c.key for c in accepted], moves, prior_route=prior_route)
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
        f"{_clear_commands(names)} in the app (each says there is nothing to "
        "clear when the app saved none).[/yellow]"
    )


def _applies_before_start(channel: ComposeChannel, diff: ConfigDiff, *, starts: bool) -> str:
    """When a pre-start removal takes effect, honestly (review S1, F7)."""
    if not diff.recreates:
        # A --force rewrite of the same values: compose recreates nothing.
        if starts:
            return (
                "The stack's configuration did not change, so the start does not "
                "recreate it: setup restarts it after the start so this takes effect."
            )
        return (
            "The stack's configuration did not change, so `up -d` does not "
            f"recreate it: run `{escape(channel.restart_command)}` now so this "
            "takes effect."
        )
    start = escape(channel.start_command())
    if starts:
        return (
            "This takes effect when the stack is recreated: the start command "
            f"(`{start}`) does that."
        )
    # The removal is already in the file, the recreate is the user's: a
    # restart in between (a reboot, say) boots the OLD .env.docker without
    # the app's copy, so say so plainly.
    return (
        f"The app's settings already changed, so recreate the stack now with "
        f"`{start}`. Until then it keeps its old configuration, and a restart "
        "before the recreate (a reboot, say) would start the old configuration "
        "without the removed copies."
    )


def check_before_start(
    console: Console,
    channel: ComposeChannel,
    diff: ConfigDiff,
    *,
    mode: bool | None,
    interactive: bool,
    gateway_slot: Callable[[str], str | None],
    starts: bool = True,
) -> ShadowRun:
    """The pre-start step: no exec at all unless this run changed a route or
    credential key; then one probe, the questions, and one apply.

    ``starts``: this run itself runs the start command next (a wizard-run
    start); False on the print and scoped paths, where the recreate is the
    user's to run.
    """
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
    run.removed_before_start = resolve_candidates(
        console,
        channel,
        candidates,
        run,
        mode=mode,
        interactive=interactive,
        applies_when=lambda: _applies_before_start(channel, diff, starts=starts),
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
    about. It must be probed with ``run.diff.prior_route``: the container now
    runs the new route, and a shared slot's shape is judged under the route
    the app saved it beside (the review's HIGH: judged under the new one, a
    real vendor key read as the new gateway's and was discarded). Same
    questions and removal as before the start; the caller then restarts the
    services that load the file. Returns the keys it raised (asked or warned
    about, so the closing note does not repeat them) and the keys whose copy
    left the file (empty: no restart needed).
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
        applies_when=lambda: "The running stack loaded them at boot, so setup restarts it:",
        started=True,
        prior_route=run.diff.prior_route,
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
    "LLM_ROUTE_KEYS",
    "NOT_APPLIED",
    "REPORT_PROGRAM",
    "ROUTE_MODEL_KEYS",
    "ShadowRun",
    "UNCONFIRMED",
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
