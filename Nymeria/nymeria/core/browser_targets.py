"""Browser-target selection for the ``chrome_*`` extension tools.

More than one Nymeria browser extension can be connected on one account at
a time (a personal desktop Chrome, the headless rig, another household
machine). Commands are routed to exactly ONE of them, resolved here:

1. ``ThreadConfig.browser_target`` (per-thread override; the agent may set
   its own thread's target via ``chrome_target``, always narrated).
2. ``UserProfile.preferences['browser']['default_target']`` (account
   default; user-set only, ``/browser default``: the agent surface is
   blocked on that command).
3. Auto: when exactly one browser is connected, it is the target.
4. Otherwise unresolved: the dispatch path refuses with the roster rather
   than guessing (or broadcasting, which double-executed every command:
   backlog #282).

Targets are stored as the extension's persistent client_id
(``nymeria-browser-<uuid>``), never as a label: labels are display names
(``UserProfile.get_browser_preferences()['labels']``) and may be renamed
without moving the target.

In-process helpers only; durable state lives in the thread config and the
user profile. Callers outside an agent host (bare unit tests) degrade to
"no override configured" rather than erroring: target selection is routing
policy, not authentication, so failing open to the auto/refuse ladder is
the honest behavior.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from .chrome_subscribers import CHROME_CLIENT_ID_PREFIX, chrome_browser_roster

logger = logging.getLogger(__name__)

# Resolution sources, in precedence order.
SOURCE_THREAD = "thread"
SOURCE_ACCOUNT = "account"
SOURCE_AUTO = "auto"

# Unresolved reasons.
REASON_NONE_CONNECTED = "none_connected"
REASON_AMBIGUOUS = "ambiguous"

# Spellings /browser use and chrome_target accept for "unset the override".
CLEAR_WORDS = frozenset({"clear", "none", "off", "unset"})


@dataclass(frozen=True)
class TargetResolution:
    """Outcome of the resolution ladder for one (user, thread).

    ``client_id`` set: route there (``source`` says which rung). Unset:
    ``reason`` is ``none_connected`` (no browser online, no override) or
    ``ambiguous`` (several online, nothing selected: refuse, never guess).
    """

    client_id: Optional[str]
    source: str = ""
    reason: str = ""


def _agent():
    from .agent import get_current_agent

    return get_current_agent()


def _profile_manager():
    """The live agent's user-profile manager, or None outside an agent host."""
    agent = _agent()
    if agent is None:
        return None
    return getattr(agent, "profile_manager", None)


# Labels render in trusted-voice platform text (refusals, rosters, command
# output), so they are bounded: normalized at write (set_browser_label) and
# clamped again at read, because the profile store has other writers.
_LABEL_MAX_CHARS = 60


def _clean_label(label: str) -> str:
    """One line, printable, bounded: the shape a display name may take."""
    collapsed = " ".join(str(label).split())
    printable = "".join(ch for ch in collapsed if ch.isprintable())
    return printable[:_LABEL_MAX_CHARS]


def browser_labels(user_id: str) -> dict[str, str]:
    """The user's browser labels (client_id -> label). Empty on any failure:
    labels are display sugar and must never break a dispatch or a probe."""
    if not user_id:
        return {}
    try:
        manager = _profile_manager()
        if manager is None:
            return {}
        prefs = manager.get_profile(user_id).get_browser_preferences()
        labels = prefs.get("labels")
        if not isinstance(labels, dict):
            return {}
        cleaned = {str(k): _clean_label(v) for k, v in labels.items()}
        return {k: v for k, v in cleaned.items() if v}
    except Exception as exc:  # noqa: BLE001 - sugar, never load-bearing
        logger.debug("browser_labels failed for %s: %s", user_id, exc)
        return {}


def account_default_target(user_id: str) -> Optional[str]:
    """The user's account-level default browser target (client_id), if set."""
    if not user_id:
        return None
    try:
        manager = _profile_manager()
        if manager is None:
            return None
        prefs = manager.get_profile(user_id).get_browser_preferences()
        target = prefs.get("default_target")
        return str(target) if target else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("account_default_target failed for %s: %s", user_id, exc)
        return None


def thread_target(thread_id: str) -> Optional[str]:
    """The thread's ``browser_target`` override, if any."""
    if not thread_id:
        return None
    try:
        agent = _agent()
        if agent is None:
            return None
        config = agent.thread_config_manager.get_config(thread_id)
        if config is None:
            return None
        target = getattr(config, "browser_target", None)
        return str(target) if target else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("thread_target failed for %s: %s", thread_id, exc)
        return None


def resolve_target(user_id: str, thread_id: str) -> TargetResolution:
    """Run the resolution ladder. Never waits and never checks whether an
    EXPLICIT target is connected: connectivity (including the recycle-grace
    hold) is the dispatch path's job, so a configured-but-offline browser
    resolves here and fails there with an error that names it."""
    override = thread_target(thread_id)
    if override:
        return TargetResolution(client_id=override, source=SOURCE_THREAD)
    default = account_default_target(user_id)
    if default:
        return TargetResolution(client_id=default, source=SOURCE_ACCOUNT)
    connected = [r for r in chrome_browser_roster(user_id) if r.connected]
    if len(connected) == 1:
        return TargetResolution(client_id=connected[0].client_id, source=SOURCE_AUTO)
    if not connected:
        return TargetResolution(client_id=None, reason=REASON_NONE_CONNECTED)
    return TargetResolution(client_id=None, reason=REASON_AMBIGUOUS)


def short_browser_id(client_id: str) -> str:
    """The first 8 characters of the uuid half: enough to tell browsers
    apart in prose without quoting the whole 52-character id."""
    tail = client_id[len(CHROME_CLIENT_ID_PREFIX):] if client_id.startswith(
        CHROME_CLIENT_ID_PREFIX
    ) else client_id
    return tail[:8] if tail else client_id[:8]


def describe_browser(
    user_id: str, client_id: str, labels: Optional[dict[str, str]] = None
) -> str:
    """Human handle for one browser: its label when named, else its short id."""
    if labels is None:
        labels = browser_labels(user_id)
    label = labels.get(client_id)
    if label:
        return f"{label} ({short_browser_id(client_id)})"
    return short_browser_id(client_id)


def roster_lines(user_id: str) -> list[str]:
    """One display line per known browser, for refusals and listings."""
    labels = browser_labels(user_id)
    lines = []
    for record in chrome_browser_roster(user_id):
        state = "connected" if record.connected else "disconnected"
        if not record.connected and record.last_disconnect_age_s is not None:
            state = f"disconnected {int(record.last_disconnect_age_s)}s ago"
        version = f", v{record.version}" if record.version else ""
        lines.append(
            f"{describe_browser(user_id, record.client_id, labels)}: {state}{version}"
        )
    return lines


def resolve_browser_ref(user_id: str, ref: str) -> tuple[Optional[str], Optional[str]]:
    """Resolve a user- or agent-typed browser reference to a client_id.

    Accepts a label (case-insensitive), a full client_id, or a unique
    fragment of either. Returns ``(client_id, None)`` or ``(None, error)``.
    Candidates are the union of the connection roster (everything seen this
    process) and the labeled browsers in the profile, so a labeled browser
    can be selected even before it connects (after a backend restart, say).
    """
    wanted = (ref or "").strip()
    if not wanted:
        return None, "No browser named. Say which browser, by label or id."
    labels = browser_labels(user_id)
    candidates: dict[str, Optional[str]] = dict(labels)
    for record in chrome_browser_roster(user_id):
        candidates.setdefault(record.client_id, None)
    if not candidates:
        return None, (
            "No browsers are known for this account yet. One appears the "
            "first time its extension connects."
        )
    folded = wanted.casefold()
    # Exact client_id, then exact label.
    if wanted in candidates:
        return wanted, None
    exact_label = [cid for cid, label in candidates.items() if label and label.casefold() == folded]
    if len(exact_label) == 1:
        return exact_label[0], None
    if len(exact_label) > 1:
        return None, (
            f"The label '{wanted}' names {len(exact_label)} browsers; rename "
            "one with /browser rename, or use an id."
        )
    # Unique fragment of id or label.
    partial = [
        cid
        for cid, label in candidates.items()
        if folded in cid.casefold() or (label and folded in label.casefold())
    ]
    if len(partial) == 1:
        return partial[0], None
    if not partial:
        known = "; ".join(roster_lines(user_id)) or "none known"
        return None, f"No browser matches '{wanted}'. Known browsers: {known}."
    matches = ", ".join(describe_browser(user_id, cid, labels) for cid in partial)
    return None, f"'{wanted}' is ambiguous: it matches {matches}."


def _login_session_guard(
    user_id: str, *, thread_id: Optional[str], account_level: bool
) -> Optional[str]:
    """Refuse a target change that would pull the rug from a live login.

    A live login handoff is pinned to the browser it started on; retargeting
    the session's own thread (or the account default, which can retarget
    every thread at once) mid-login strands the human typing their password.
    Thread-level changes on OTHER threads pass: they cannot move the session.
    """
    from .browser_login_sessions import get_browser_login_registry

    sessions = get_browser_login_registry().active_for_user(user_id)
    for session in sessions:
        if account_level or (thread_id and session.thread_id == thread_id):
            return (
                "A human login handoff is live (session "
                f"{session.session_id}, tab {session.tab_id}). Finish or "
                "cancel it before switching browsers: retargeting mid-login "
                "would strand the person typing their password."
            )
    return None


def set_thread_target(
    user_id: str, thread_id: str, client_id: Optional[str]
) -> Optional[str]:
    """Set (or clear, with ``None``) the thread's browser target.

    Returns an error string, or None on success.
    """
    if not thread_id:
        return "This surface has no thread; use /browser default instead."
    guard = _login_session_guard(user_id, thread_id=thread_id, account_level=False)
    if guard:
        return guard
    agent = _agent()
    if agent is None:
        return "No agent host is live; try again from a running Nymeria."
    try:
        from .thread_config import ThreadConfig

        manager = agent.thread_config_manager
        config = manager.get_config(thread_id)
        if config is None:
            config = ThreadConfig(thread_id=thread_id)
        config.browser_target = client_id
        if not manager.save_config(config):
            return "Could not save the thread's browser target."
        return None
    except Exception as exc:  # noqa: BLE001 - surfaced, not swallowed
        logger.warning("set_thread_target failed for %s: %s", thread_id, exc)
        return f"Could not save the thread's browser target: {exc}"


def set_account_default(user_id: str, client_id: Optional[str]) -> Optional[str]:
    """Set (or clear, with ``None``) the account default browser target."""
    guard = _login_session_guard(user_id, thread_id=None, account_level=True)
    if guard:
        return guard
    manager = _profile_manager()
    if manager is None:
        return "No agent host is live; try again from a running Nymeria."
    try:
        with manager.atomic_update(user_id) as profile:
            profile.set_browser_preference("default_target", client_id)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("set_account_default failed for %s: %s", user_id, exc)
        return f"Could not save the account browser default: {exc}"


def set_browser_label(
    user_id: str, client_id: str, label: Optional[str]
) -> Optional[str]:
    """Name (or, with ``None``, unname) a browser. Returns error or None.

    Labels are bounded (one line, printable, ``_LABEL_MAX_CHARS``) because
    they render in trusted-voice platform text on every later refusal and
    roster line; an over-long or control-charactered name is refused with
    the rule rather than silently mangled.
    """
    if label is not None:
        cleaned = _clean_label(label)
        if not cleaned:
            return "That name is empty once normalized; pick a readable one."
        if cleaned != " ".join(str(label).split()):
            return (
                f"Browser names are plain text up to {_LABEL_MAX_CHARS} "
                "characters; pick a shorter or simpler one."
            )
        label = cleaned
    manager = _profile_manager()
    if manager is None:
        return "No agent host is live; try again from a running Nymeria."
    try:
        with manager.atomic_update(user_id) as profile:
            prefs = profile.get_browser_preferences()
            labels = dict(prefs.get("labels") or {})
            if label:
                labels[client_id] = label
            else:
                labels.pop(client_id, None)
            profile.set_browser_preference("labels", labels)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("set_browser_label failed for %s: %s", user_id, exc)
        return f"Could not save the browser label: {exc}"


__all__ = [
    "SOURCE_THREAD",
    "SOURCE_ACCOUNT",
    "SOURCE_AUTO",
    "REASON_NONE_CONNECTED",
    "REASON_AMBIGUOUS",
    "CLEAR_WORDS",
    "TargetResolution",
    "browser_labels",
    "account_default_target",
    "thread_target",
    "resolve_target",
    "short_browser_id",
    "describe_browser",
    "roster_lines",
    "resolve_browser_ref",
    "set_thread_target",
    "set_account_default",
    "set_browser_label",
]
