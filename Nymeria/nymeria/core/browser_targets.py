"""Browser-target selection for the ``chrome_*`` extension tools.

More than one Nymeria browser extension can be connected on one account at
a time (the server browser beside the backend, a personal desktop Chrome,
another household machine). Commands are routed to exactly ONE of them,
resolved here:

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
without moving the target. Labels carry a PROVENANCE beside them
(``label_origins``): a name the user gave renders as Nymeria's own, a name
the browser announced on its connect is attributed and quoted wherever it
reaches unfenced text, and a user's decision (a rename, or a removal) is
never overwritten by a later connect. A browser's KIND (``server``: the headless Chrome
``nymeria browser`` runs beside the backend; ``desktop``: the user's own
Chrome) rides the connection roster and is display information only; the
ladder never reads it. :func:`has_server_browser` is the one install-aware
read here, so refusals can stop telling a headless browser to click a popup;
it is account-scoped, because the rig belongs to one account (its docstring
says why, and the install-wide reading is a bug this already had).

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

from ..config import get_settings
from .accounts import BOOTSTRAP_USER_ID
from .chrome_subscribers import (
    CHROME_CLIENT_ID_PREFIX,
    chrome_browser_kind,
    chrome_browser_roster,
    known_server_browser,
)

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

# Where a stored label came from, kept beside the labels themselves so it
# survives a restart. ``user`` is a decision a human (or the agent acting on
# their word) made through /browser rename or chrome_browsers rename, INCLUDING
# the decision to remove a name; ``extension`` is text a browser announced on
# its connect. A client_id with no entry reads as trusted: that is every label
# written before this key existed, plus the one `nymeria init` writes when it
# provisions the server browser.
LABEL_ORIGINS_KEY = "label_origins"
LABEL_ORIGIN_USER = "user"
LABEL_ORIGIN_EXTENSION = "extension"

# How many named browsers one account may accumulate FROM CONNECTS. A person
# with more than a handful of browsers is already unusual; this only bounds
# what the wire can add on its own (see _seed_refused).
_MAX_SEEDED_LABELS = 32

# The delimiters Nymeria's own voice uses in these lines: square brackets open
# every platform note ("[Error]: ...", "[System: ...]"), parentheses and
# quotes group a handle, and a backtick marks a command. Extension-chosen
# text keeps NONE of them, dropped rather than substituted: a substitute is
# still a delimiter somewhere else in the same line, and the point is that no
# announced text can open a note of its own or close the quoting it sits
# inside. A legitimate name loses at most some punctuation.
_VOICE_SIGILS = str.maketrans(
    {"[": None, "]": None, "(": None, ")": None, '"': None, "`": None}
)


def _strip_voice_sigils(text: str) -> str:
    """Drop the delimiters that let text read as Nymeria's own voice."""
    return " ".join(str(text).translate(_VOICE_SIGILS).split())


def _clean_label(label: str) -> str:
    """One line, printable, bounded: the shape a display name may take."""
    collapsed = " ".join(str(label).split())
    printable = "".join(ch for ch in collapsed if ch.isprintable())
    return printable[:_LABEL_MAX_CHARS]


def _browser_prefs(user_id: str) -> dict:
    """The user's raw browser preference block, or ``{}`` on any failure."""
    if not user_id:
        return {}
    manager = _profile_manager()
    if manager is None:
        return {}
    prefs = manager.get_profile(user_id).get_browser_preferences()
    return prefs if isinstance(prefs, dict) else {}


def _labels_from(prefs: dict) -> dict[str, str]:
    labels = prefs.get("labels")
    if not isinstance(labels, dict):
        return {}
    cleaned = {str(k): _clean_label(v) for k, v in labels.items()}
    return {k: v for k, v in cleaned.items() if v}


def _origins_from(prefs: dict) -> dict[str, str]:
    origins = prefs.get(LABEL_ORIGINS_KEY)
    if not isinstance(origins, dict):
        return {}
    return {str(k): str(v) for k, v in origins.items()}


def browser_labels(user_id: str) -> dict[str, str]:
    """The user's browser labels (client_id -> label). Empty on any failure:
    labels are display sugar and must never break a dispatch or a probe."""
    try:
        return _labels_from(_browser_prefs(user_id))
    except Exception as exc:  # noqa: BLE001 - sugar, never load-bearing
        logger.debug("browser_labels failed for %s: %s", user_id, exc)
        return {}


def browser_label_origins(user_id: str) -> dict[str, str]:
    """Where each stored label came from (see :data:`LABEL_ORIGINS_KEY`).

    Empty on any failure, and a missing entry reads as trusted, so a store
    this build cannot parse degrades to the pre-seeding behavior rather than
    to a roster full of quoted attributions.
    """
    try:
        return _origins_from(_browser_prefs(user_id))
    except Exception as exc:  # noqa: BLE001 - display metadata, never load-bearing
        logger.debug("browser_label_origins failed for %s: %s", user_id, exc)
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
    user_id: str,
    client_id: str,
    labels: Optional[dict[str, str]] = None,
    origins: Optional[dict[str, str]] = None,
) -> str:
    """Human handle for one browser: its label when named, else its short id,
    plus its kind (``server`` or ``desktop``) when this process has seen it
    connect. A browser known only from the profile (after a backend restart,
    say) carries no kind rather than a guessed one.

    Every handle here lands in TRUSTED-VOICE text (refusals, roster lines,
    command output) with no fence around it, so a name the BROWSER chose is
    never rendered as the browser's name: it is attributed and quoted
    (``a1b2c3d4 (desktop, announced "shop laptop")``), with the sigils our
    own voice uses stripped out of it. A name a human gave keeps the plain
    ``shop laptop (a1b2c3d4, desktop)`` form, because the user's own words
    carry the user's authority. The raw text stays available in the fenced
    payload of chrome_health / chrome_target / chrome_browsers, which is
    where extension-supplied strings belong.
    """
    if labels is None:
        labels = browser_labels(user_id)
    if origins is None:
        origins = browser_label_origins(user_id)
    label = labels.get(client_id)
    short = short_browser_id(client_id)
    kind = chrome_browser_kind(user_id, client_id)
    tail = f"{short} ({kind})" if kind else short
    if not label:
        return tail
    if origins.get(client_id) == LABEL_ORIGIN_EXTENSION:
        # Double quotes delimit the announced text, and no quote survives
        # inside it, so the name cannot close its own quoting and continue
        # as narration. A name that was nothing but delimiters leaves
        # nothing to show, so the browser reads as unnamed here.
        spoken = _strip_voice_sigils(label)
        if not spoken:
            return tail
        announced = f'announced "{spoken}"'
        inner = f"{kind}, {announced}" if kind else announced
        return f"{short} ({inner})"
    return f"{label} ({short}, {kind})" if kind else f"{label} ({short})"


def roster_lines(user_id: str) -> list[str]:
    """One display line per known browser, for refusals and listings."""
    labels = browser_labels(user_id)
    origins = browser_label_origins(user_id)
    lines = []
    for record in chrome_browser_roster(user_id):
        state = "connected" if record.connected else "disconnected"
        if not record.connected and record.last_disconnect_age_s is not None:
            state = f"disconnected {int(record.last_disconnect_age_s)}s ago"
        # The version is extension-chosen too (bounded at ingest), and these
        # lines are unfenced: neutralise its sigils for the same reason a
        # label's are neutralised.
        version = f", v{_strip_voice_sigils(record.version)}" if record.version else ""
        lines.append(
            f"{describe_browser(user_id, record.client_id, labels, origins)}: "
            f"{state}{version}"
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
    origins = browser_label_origins(user_id)
    matches = ", ".join(
        describe_browser(user_id, cid, labels, origins) for cid in partial
    )
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

    Either way (naming or unnaming) the client_id is stamped
    ``LABEL_ORIGIN_USER``, which is durable: a REMOVED name is a decision
    too, and the stamp is what stops the next connect re-seeding the name
    the user just took off.
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
            origins = dict(_origins_from(prefs))
            if label:
                labels[client_id] = label
            else:
                labels.pop(client_id, None)
            origins[client_id] = LABEL_ORIGIN_USER
            profile.set_browser_preference("labels", labels)
            profile.set_browser_preference(LABEL_ORIGINS_KEY, origins)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("set_browser_label failed for %s: %s", user_id, exc)
        return f"Could not save the browser label: {exc}"


# Why a seed was refused. Returned rather than a bare bool because two of
# these leave a browser UNNAMED for a reason nobody chose and nothing else
# surfaces: on the Docker path the connect is now the only way the server
# browser is ever named, so a silent refusal there is an operator staring at
# a raw id with no explanation. Those two are logged; the other two are the
# ordinary steady state of a browser reconnecting every minute.
SEED_ALREADY_NAMED = "the browser already has a stored name"
SEED_USER_DECIDED = "the user has already decided this browser's name"
SEED_NAME_TAKEN = "another browser on this account already uses that name"
SEED_AT_CEILING = "the account is at its ceiling for names from connects"
_NOTEWORTHY_SEED_REFUSALS = (SEED_NAME_TAKEN, SEED_AT_CEILING)

# One log line per (user, browser, reason) per process: the announcing
# browser reconnects about once a minute forever, and a warning that repeats
# every minute is a warning nobody reads. Bounded, because the key contains
# a client-chosen id.
_REPORTED_SEED_REFUSALS: set[tuple[str, str, str]] = set()
_MAX_REPORTED_SEED_REFUSALS = 128


def _report_seed_refusal(
    user_id: str, client_id: str, cleaned: str, reason: str
) -> None:
    if reason not in _NOTEWORTHY_SEED_REFUSALS:
        logger.debug(
            "browser label seed declined for %s (%s): %s", client_id, user_id, reason
        )
        return
    key = (user_id, client_id, reason)
    if key in _REPORTED_SEED_REFUSALS:
        return
    if len(_REPORTED_SEED_REFUSALS) < _MAX_REPORTED_SEED_REFUSALS:
        _REPORTED_SEED_REFUSALS.add(key)
    logger.warning(
        "Browser %s (user %s) announced the name %r and it was NOT applied: %s. "
        "It stays reachable by its id; name it with /browser rename.",
        client_id,
        user_id,
        cleaned,
        reason,
    )


def _seed_refusal(prefs: dict, client_id: str, cleaned: str) -> Optional[str]:
    """Why seeding ``cleaned`` for ``client_id`` would be refused, or None to
    allow it. The whole policy in one place, so the cheap pre-check and the
    authoritative write cannot drift.

    Four reasons, and the asymmetry with clamping is deliberate:

    * the browser already has a stored name (including the one provisioning
      wrote): a name that is there wins over one announced later;
    * the user has DECIDED about this browser's name (``LABEL_ORIGIN_USER``),
      which covers a rename and, just as much, a removal: an un-name must
      survive the reconnect a minute later, or the user cannot un-name a
      browser that keeps announcing itself;
    * the name is already in use by a DIFFERENT browser on this account.
      Unlike an unusable name, which is clamped because nobody is on the
      other end of a connect to re-type it, a colliding name cannot be
      repaired by normalising it. Labels resolve to client_ids
      (``chrome_target``, ``/browser switch``, ``/browser default``), so
      letting a second browser take a name already in use either hands it
      the first browser's routing selector or makes that selector ambiguous
      and breaks the documented recovery path. Refusing keeps every name
      pointing at exactly one browser; the browser is still reachable by id;
    * the account already holds ``_MAX_SEEDED_LABELS`` names. A connect
      writes a durable entry keyed by an id the client chooses, so without a
      ceiling the profile grows one entry per distinct announced id, forever.
      A human renaming browsers by hand is never near the ceiling; a stream
      of fresh ids is exactly what it is for.
    """
    labels = _labels_from(prefs)
    if labels.get(client_id):
        return SEED_ALREADY_NAMED
    if _origins_from(prefs).get(client_id) == LABEL_ORIGIN_USER:
        return SEED_USER_DECIDED
    folded = cleaned.casefold()
    if any(
        other != client_id and existing.casefold() == folded
        for other, existing in labels.items()
    ):
        return SEED_NAME_TAKEN
    if client_id not in labels and len(labels) >= _MAX_SEEDED_LABELS:
        return SEED_AT_CEILING
    return None


def seed_browser_label(user_id: str, client_id: str, label: str) -> bool:
    """Name a browser from the label its extension announced on connect, but
    only when nothing and nobody has decided otherwise: the refusal policy is
    :func:`_seed_refusal`, and a refusal that leaves a browser unnamed for a
    reason nobody chose is logged once per process (see
    :func:`_report_seed_refusal`).

    Read first, write only if the read says there is something to do. The
    extension's MV3 worker recycles about once a minute, so the steady state
    of a named browser is a reconnect every minute forever; opening
    ``atomic_update`` for it rewrote the whole profile.json (mkstemp, write,
    rename, ``updated_at`` bumped) each time, on the same per-user lock
    memories and preferences take, and widened the cross-process clobber
    window the api and worker containers share. The write path re-checks
    under the lock, so a rename racing the connect still cannot be clobbered:
    the pre-check only decides whether to take the lock at all.

    The announced text is normalised with the same bound as a user rename
    (one printable line, ``_LABEL_MAX_CHARS``) and clamped rather than
    refused, since there is nobody to refuse to. Best-effort by contract:
    returns True when it wrote a label, False for every other outcome, and
    never raises, because it runs off the stream's critical path where an
    exception would only be lost.
    """
    cleaned = _clean_label(label) if label else ""
    if not cleaned or not client_id:
        return False
    try:
        manager = _profile_manager()
        if manager is None:
            return False
        refusal = _seed_refusal(
            manager.get_profile(user_id).get_browser_preferences(), client_id, cleaned
        )
        if refusal:
            _report_seed_refusal(user_id, client_id, cleaned, refusal)
            return False
        with manager.atomic_update(user_id) as profile:
            prefs = profile.get_browser_preferences()
            refusal = _seed_refusal(prefs, client_id, cleaned)
            if refusal:
                _report_seed_refusal(user_id, client_id, cleaned, refusal)
                return False
            labels = dict(prefs.get("labels") or {})
            origins = dict(_origins_from(prefs))
            labels[client_id] = cleaned
            origins[client_id] = LABEL_ORIGIN_EXTENSION
            profile.set_browser_preference("labels", labels)
            profile.set_browser_preference(LABEL_ORIGINS_KEY, origins)
        return True
    except Exception as exc:  # noqa: BLE001 - display sugar, off the stream path
        logger.debug("seed_browser_label failed for %s: %s", user_id, exc)
        return False


# ---------------------------------------------------------------------------
# Server-browser awareness
# ---------------------------------------------------------------------------
#
# Facts the refusals and the /browser overview share, so the agent-facing and
# the human-facing copy cannot name different commands. The sentences around
# them are composed at each surface, which own their voice.

# Which account the server browser actually belongs to. The rig connects with
# its own token for the bootstrap admin and is set as THAT account's default
# browser target, so it is the only account that can drive it or manage it.
SERVER_BROWSER_ACCOUNT = BOOTSTRAP_USER_ID

SERVER_BROWSER_STATUS_COMMAND = "nymeria browser status"
SERVER_BROWSER_RESTART_COMMAND = "nymeria browser service restart"
SERVER_BROWSER_INSTALL_COMMAND = "nymeria browser install"
EXTENSION_RELEASES_URL = "https://github.com/ManningAskew7/nymeria-browser/releases"
# How the user's own Chrome gets the extension, in one breath.
OWN_CHROME_INSTALL_STEPS = (
    f"download the release zip from {EXTENSION_RELEASES_URL}, unzip it, then "
    "Chrome > Extensions > Developer mode > Load unpacked, click the extension "
    "icon and paste the backend URL and an account token (Nymeria Desktop > "
    "Account tab > Tokens, or `nymeria users issue-token`)"
)


def has_server_browser(user_id: str) -> bool:
    """Does THIS ACCOUNT have a server browser?

    True when the roster has seen one connect for this user this process
    (kind ``server``, connected or not), or, for the account the rig belongs
    to, when the install's ``SERVER_BROWSER_HOME`` is set: that covers the
    window after a backend restart when the roster is empty. A refusal that
    reads True must name the server browser and its commands, never a popup
    it does not have.

    The settings branch is ACCOUNT-SCOPED even though the setting is
    install-wide, and that is not an oversight to simplify away. The rig
    connects as the bootstrap admin and is that account's default browser
    target; no other account can route a command to it, rename it, or run
    the host commands the copy names. Answering True for everyone told a
    shared-account user (a household member on Telegram, a group on Discord)
    to go run `nymeria browser status` on a machine they have no shell on,
    AND suppressed the "install the extension in your own Chrome" branch,
    which is the only route they actually have. Their own roster still
    answers for them: if a server browser really does connect on their
    account, the first branch says so.

    Settings are read defensively: every caller is composing an error message,
    and a refusal that raises while explaining a problem is strictly worse
    than one that falls back to the roster's own answer.
    """
    if known_server_browser(user_id):
        return True
    if user_id != SERVER_BROWSER_ACCOUNT:
        return False
    try:
        return bool(get_settings().server_browser_home)
    except Exception as exc:  # noqa: BLE001 - never break a refusal
        logger.debug("has_server_browser could not read settings: %s", exc)
        return False


__all__ = [
    "SOURCE_THREAD",
    "SOURCE_ACCOUNT",
    "SOURCE_AUTO",
    "REASON_NONE_CONNECTED",
    "REASON_AMBIGUOUS",
    "CLEAR_WORDS",
    "TargetResolution",
    "SEED_ALREADY_NAMED",
    "SEED_USER_DECIDED",
    "SEED_NAME_TAKEN",
    "SEED_AT_CEILING",
    "LABEL_ORIGINS_KEY",
    "LABEL_ORIGIN_USER",
    "LABEL_ORIGIN_EXTENSION",
    "browser_labels",
    "browser_label_origins",
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
    "seed_browser_label",
    "SERVER_BROWSER_ACCOUNT",
    "SERVER_BROWSER_STATUS_COMMAND",
    "SERVER_BROWSER_RESTART_COMMAND",
    "SERVER_BROWSER_INSTALL_COMMAND",
    "EXTENSION_RELEASES_URL",
    "OWN_CHROME_INSTALL_STEPS",
    "has_server_browser",
]
