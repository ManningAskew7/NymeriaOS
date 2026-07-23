"""Callable-team entity store: team identity, description, and shared memory.

Backlog #100 phase 1. A ``Team`` is a per-user entity (id, name, description,
and, from phase 3, a key-value memory registry) persisted as one JSON file per
user under ``data_dir/teams/``, mirroring the ``HookManager`` store pattern
(per-user lock, atomic write, quarantine on corrupt, fingerprint audit,
fingerprint-keyed read cache).

MEMBERSHIP IS NOT STORED HERE. ``ThreadConfig.callable_team_id`` remains the
single source of truth for which threads belong to a team, so there is no
dual-write drift; this store holds identity and meta. The one shared
membership scan (``members``/``all_members``) collapses the three duplicate
O(N) config scans that previously lived in the REST router, the command
service, and the CLI in-process transport, and ``serialize_thread_teams`` is
the one serializer they all call.

``ThreadConfig.callable_team_name`` is DEPRECATED by this store: still parsed
from old config files (the lazy migration consumes it, and display fallbacks
may read it for dangling ids), never written by any code path. Names resolve
through ``resolve_team_name``/``team_names``; renames are O(1) store writes.

Migration is LAZY and idempotent: the first store access for a user with no
store file synthesizes entities from the distinct ``(callable_team_id,
callable_team_name)`` pairs found across their thread configs (first-seen
name wins on drift, logged) and writes the store once. The written file,
empty or not, is the migrated marker.

The graph never depends on team NAME or memory (only on membership, which is
config-side), so an external edit to a store file needs no graph
invalidation; ``poll_external_changes`` lets the hot-load chokepoint mark the
tool search index dirty (its callable tags carry the team name).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field, field_validator

from .keyed_locks import KeyedRLockMap
from .storage_paths import (
    quarantine_corrupt_file,
    read_store_fingerprint,
    record_store_fingerprint,
    safe_path_segment,
    write_text_atomic,
)
from .time_utils import ensure_aware_utc, utc_now

logger = logging.getLogger(__name__)

# Thread-safe locks keyed by user_id.
_team_locks = KeyedRLockMap()

# Debounce for the hot-load chokepoint's external-edit poll (seconds).
_POLL_MIN_INTERVAL = 2.0

MAX_TEAM_NAME_LENGTH = 120
MAX_TEAM_DESCRIPTION_LENGTH = 2000

_TEAM_SLUG_RE = re.compile(r"[^a-z0-9_-]+")


def make_team_id(name: str) -> str:
    """Build a stable-enough team id with a readable slug plus random suffix.

    The id-format authority (moved here from the retired REST helper
    ``make_thread_team_id``); existing ids in the wild follow it, so it must
    stay stable.
    """
    slug = _TEAM_SLUG_RE.sub("-", name.strip().lower()).strip("-_")
    if not slug:
        slug = "team"
    return f"team-{slug[:48]}-{uuid.uuid4().hex[:8]}"


class TeamMemory(BaseModel):
    """One shared key-value team fact (phase 3 populates these)."""

    key: str = Field(..., min_length=1, max_length=200)
    value: str = Field(default="")
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("created_at", "updated_at")
    @classmethod
    def _datetimes_as_utc(cls, value: datetime) -> datetime:
        return ensure_aware_utc(value)


class Team(BaseModel):
    """A callable-team entity: identity and meta, never membership."""

    id: str = Field(..., min_length=1, max_length=MAX_TEAM_NAME_LENGTH)
    name: str = Field(..., min_length=1, max_length=MAX_TEAM_NAME_LENGTH)
    description: Optional[str] = Field(
        default=None, max_length=MAX_TEAM_DESCRIPTION_LENGTH
    )
    memories: List[TeamMemory] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("created_at", "updated_at")
    @classmethod
    def _datetimes_as_utc(cls, value: datetime) -> datetime:
        return ensure_aware_utc(value)


class TeamStore(BaseModel):
    """All of one user's team entities (one JSON file per user)."""

    user_id: str = "default"
    teams: List[Team] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("updated_at")
    @classmethod
    def _datetimes_as_utc(cls, value: datetime) -> datetime:
        return ensure_aware_utc(value)


def normalize_team_name(name: str) -> str:
    """Whitespace-normalize a team display name; raise ValueError when invalid.

    Transport-free twin of the REST layer's ``normalize_thread_team_name``
    (which raises HTTPException); agent surfaces wrap this one.
    """
    normalized = " ".join((name or "").strip().split())
    if not normalized:
        raise ValueError("Team name is required")
    if len(normalized) > MAX_TEAM_NAME_LENGTH:
        raise ValueError(
            f"Team name must be {MAX_TEAM_NAME_LENGTH} characters or fewer"
        )
    return normalized


class TeamManager:
    """CRUD + persistence for callable-team entities (one JSON file per user)."""

    def __init__(
        self,
        data_dir: Path,
        *,
        thread_config_manager: Any = None,
        accounts_repo: Any = None,
    ):
        self.teams_dir = Path(data_dir) / "teams"
        self.teams_dir.mkdir(parents=True, exist_ok=True)
        # Membership scans and lazy migration read through these; both are
        # optional so the store stays constructible in isolation (tests,
        # tooling), degrading to empty scans.
        self.thread_config_manager = thread_config_manager
        self.accounts_repo = accounts_repo
        # Fingerprint-keyed read cache: ((mtime_ns, size) or None, store).
        # Size is part of the key because file-timestamp clocks are coarse.
        self._read_cache: Dict[str, Tuple[Optional[Tuple[int, int]], TeamStore]] = {}
        # Per-session migrated marker (file existence is the durable one).
        self._migrated: set[str] = set()
        # Hot-load chokepoint poll state (dir-wide (name -> fingerprint) map).
        self._poll_lock = threading.Lock()
        self._poll_snapshot: Optional[Dict[str, Tuple[int, int]]] = None
        self._last_poll = 0.0
        logger.info("TeamManager initialized: %s", self.teams_dir)

    # -- locking -----------------------------------------------------------

    def _get_lock(self, user_id: str) -> threading.RLock:
        return _team_locks.get(user_id)

    @staticmethod
    def _snapshot(store: TeamStore) -> str:
        return json.dumps(store.model_dump(mode="json"), sort_keys=True, default=str)

    @contextmanager
    def atomic_update(self, user_id: str = "default"):
        """Atomic store update: persist only when the block changed the store.

        A block that raises persists NOTHING, even if it mutated the store
        first: partial mutations from a failed block must not reach disk
        (current CRUD helpers all raise before mutating; this guard keeps
        that safe under future edits).
        """
        self.ensure_migrated(user_id)
        lock = self._get_lock(user_id)
        with lock:
            store = self._load(user_id)
            before = self._snapshot(store)
            yield store
            if self._snapshot(store) != before and not self._save(store):
                raise RuntimeError(f"Failed to persist teams for user {user_id}")

    # -- persistence -------------------------------------------------------

    def _path_for(self, user_id: str) -> Path:
        return self.teams_dir / f"{safe_path_segment(user_id)}.json"

    def _load(self, user_id: str) -> TeamStore:
        path = self._path_for(user_id)
        # Read + parse + quarantine run under the per-user lock so unlocked
        # readers cannot race a concurrent locked write into quarantining a
        # freshly written valid store (same invariant as HookManager._load).
        with self._get_lock(user_id):
            if not path.exists():
                return TeamStore(user_id=user_id)
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                return TeamStore.model_validate(data)
            except Exception as e:  # noqa: BLE001 - never let a bad file break a turn
                quarantine = quarantine_corrupt_file(path)
                logger.error(
                    "Failed to load teams for %s: %s (%s)",
                    user_id,
                    e,
                    f"corrupt file preserved at {quarantine}"
                    if quarantine
                    else "quarantine rename failed; file left in place",
                )
                try:
                    from .activity_log import log_external_edit

                    log_external_edit(
                        "teams",
                        "corrupt team store "
                        + (
                            f"quarantined as quarantine/{quarantine.name}"
                            if quarantine
                            else "could not be quarantined"
                        ),
                        user_id=user_id,
                    )
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "Failed to record teams quarantine audit", exc_info=True
                    )
                return TeamStore(user_id=user_id)

    def _save(self, store: TeamStore) -> bool:
        path = self._path_for(store.user_id)
        try:
            store.updated_at = utc_now()
            write_text_atomic(
                path,
                json.dumps(store.model_dump(mode="json"), indent=2, default=str),
            )
            fingerprint = record_store_fingerprint(path)
            # Keep the chokepoint poll snapshot current so manager writes are
            # not misread as external edits on the next poll.
            with self._poll_lock:
                if self._poll_snapshot is not None and fingerprint is not None:
                    self._poll_snapshot[path.name] = fingerprint
            return True
        except Exception as e:  # noqa: BLE001
            logger.error("Failed to save teams for %s: %s", store.user_id, e)
            return False

    # -- lazy migration ----------------------------------------------------

    def ensure_migrated(self, user_id: str) -> None:
        """Synthesize the user's store from legacy config fields, once.

        First access for a user with no store file scans their thread configs
        for the deprecated ``(callable_team_id, callable_team_name)`` pairs and
        writes the store (first-seen name wins on drift, logged). The written
        file, even when empty, is the durable migrated marker; a session-local
        set makes the steady-state check a set lookup, not a stat.
        """
        if user_id in self._migrated:
            return
        with self._get_lock(user_id):
            if user_id in self._migrated:
                return
            path = self._path_for(user_id)
            if path.exists():
                self._migrated.add(user_id)
                return
            store = TeamStore(user_id=user_id)
            try:
                pairs, drift = self._scan_legacy_team_names(user_id)
            except Exception:  # noqa: BLE001 - migration must never break a turn
                # Do NOT write the store or mark migrated: writing an empty
                # store here would make a transient scan failure permanent
                # (the file is the migrated marker). Callers degrade to an
                # empty in-memory store and the next access retries.
                logger.exception("Team-store migration scan failed for %s", user_id)
                return
            for team_id, name in pairs.items():
                store.teams.append(Team(id=team_id, name=name or team_id))
            if self._save(store):
                self._migrated.add(user_id)
                if pairs:
                    logger.info(
                        "Migrated %d callable team(s) into the team store for %s",
                        len(pairs),
                        user_id,
                    )
                for team_id, ignored in drift.items():
                    logger.warning(
                        "Team name drift for %s (user %s): kept '%s', ignored %s",
                        team_id,
                        user_id,
                        pairs.get(team_id),
                        sorted(ignored),
                    )

    def _owned_thread_ids(self, user_id: str) -> List[str]:
        repo = self.accounts_repo
        if repo is None:
            return []
        try:
            return list(repo.list_threads_for_user(user_id))
        except Exception:  # noqa: BLE001 - scans degrade to empty, never raise
            logger.exception("Team membership scan failed listing threads for %s", user_id)
            return []

    def _scan_legacy_team_names(
        self, user_id: str
    ) -> Tuple[Dict[str, Optional[str]], Dict[str, set]]:
        """Collect team_id -> first-seen legacy name across the user's configs.

        Deterministic: thread ids are scanned sorted, so "first seen" is the
        lexicographically first teamed thread. Returns the kept names and the
        drift set of ignored alternates per team.
        """
        manager = self.thread_config_manager
        pairs: Dict[str, Optional[str]] = {}
        drift: Dict[str, set] = {}
        if manager is None:
            return pairs, drift
        for thread_id in sorted(self._owned_thread_ids(user_id)):
            tc = manager.get_config(thread_id)
            team_id = (getattr(tc, "callable_team_id", None) or None) if tc else None
            if not team_id:
                continue
            name = (getattr(tc, "callable_team_name", None) or "").strip() or None
            if team_id not in pairs:
                pairs[team_id] = name
            elif name and name != pairs[team_id]:
                if pairs[team_id] is None:
                    pairs[team_id] = name
                else:
                    drift.setdefault(team_id, set()).add(name)
        return pairs, drift

    # -- cached reads ------------------------------------------------------

    def get_store_cached(self, user_id: str) -> TeamStore:
        """The user's store, fingerprint-cached for hot read paths.

        Reparses only when the backing file's (mtime_ns, size) changed; a
        fingerprint differing from the recorded manager write is a raw
        on-disk edit and is audited once (mirrors ``get_hooks_cached``).
        Callers must treat the returned store as read-only; mutations go
        through ``atomic_update`` or the CRUD helpers.
        """
        self.ensure_migrated(user_id)
        path = self._path_for(user_id)
        try:
            st = path.stat() if path.exists() else None
        except OSError:
            st = None
        sig = (st.st_mtime_ns, st.st_size) if st is not None else None
        with self._get_lock(user_id):
            cached = self._read_cache.get(user_id)
            if cached is not None and cached[0] == sig:
                return cached[1]
            expected = read_store_fingerprint(path)
            if expected is not None and sig is not None and sig != expected:
                try:
                    from .activity_log import log_external_edit

                    log_external_edit(
                        "teams", "team store file edited on disk", user_id=user_id
                    )
                except Exception:  # noqa: BLE001 - audit must never break a turn
                    logger.debug(
                        "Failed to record teams external-edit audit", exc_info=True
                    )
                record_store_fingerprint(path)
            store = self._load(user_id)
            self._read_cache[user_id] = (sig, store)
            return store

    def get_team(self, user_id: str, team_id: str) -> Optional[Team]:
        for team in self.get_store_cached(user_id).teams:
            if team.id == team_id:
                return team
        return None

    def team_names(self, user_id: str) -> Dict[str, str]:
        """team_id -> display name for every entity in the user's store."""
        return {team.id: team.name for team in self.get_store_cached(user_id).teams}

    def resolve_team_name(
        self, user_id: str, team_id: Optional[str]
    ) -> Optional[str]:
        """The store name for ``team_id``, or None (callers fall back to the id)."""
        if not team_id:
            return None
        team = self.get_team(user_id, team_id)
        return team.name if team else None

    # -- CRUD --------------------------------------------------------------

    def create_team(
        self,
        user_id: str,
        *,
        name: str,
        description: Optional[str] = None,
        team_id: Optional[str] = None,
    ) -> Team:
        """Create a team entity (members join via config writes elsewhere).

        Raises ValueError on a name collision or invalid name. Empty teams
        are legal: an entity may exist with no member threads.
        """
        clean_name = normalize_team_name(name)
        new_id = team_id or make_team_id(clean_name)
        with self.atomic_update(user_id) as store:
            for team in store.teams:
                if team.id == new_id:
                    raise ValueError(f"Team id '{new_id}' already exists")
                if team.name.strip().casefold() == clean_name.casefold():
                    raise ValueError(f"Thread team '{clean_name}' already exists")
            team = Team(
                id=new_id,
                name=clean_name,
                description=(description or "").strip()[:MAX_TEAM_DESCRIPTION_LENGTH]
                or None,
            )
            store.teams.append(team)
        return team

    def rename_team(self, user_id: str, team_id: str, name: str) -> Optional[Team]:
        """Rename a team: an O(1) store write, no member config touched.

        Raises ValueError on collision; returns None for an unknown team.
        """
        clean_name = normalize_team_name(name)
        with self.atomic_update(user_id) as store:
            target = None
            for team in store.teams:
                if team.id == team_id:
                    target = team
                elif team.name.strip().casefold() == clean_name.casefold():
                    raise ValueError(f"Thread team '{clean_name}' already exists")
            if target is None:
                return None
            target.name = clean_name
            target.updated_at = utc_now()
            return target

    def describe_team(
        self, user_id: str, team_id: str, description: Optional[str]
    ) -> Optional[Team]:
        """Set (or clear, with None/blank) a team's description."""
        with self.atomic_update(user_id) as store:
            for team in store.teams:
                if team.id == team_id:
                    team.description = (
                        (description or "").strip()[:MAX_TEAM_DESCRIPTION_LENGTH]
                        or None
                    )
                    team.updated_at = utc_now()
                    return team
        return None

    def delete_team(self, user_id: str, team_id: str) -> bool:
        """Remove the entity. Membership clearing is the caller's job."""
        with self.atomic_update(user_id) as store:
            before = len(store.teams)
            store.teams = [team for team in store.teams if team.id != team_id]
            return len(store.teams) != before

    def ensure_team_exists(
        self,
        user_id: str,
        team_id: str,
        *,
        fallback_name: Optional[str] = None,
    ) -> Team:
        """Adopt a membership-referenced team id into the store if missing.

        Membership writes (config PATCH with an arbitrary ``callable_team_id``,
        raw config edits) can reference ids the store has never seen; adopting
        them keeps the entity store coherent without blocking the write. The
        display name falls back to the id when no name is offered.
        """
        with self.atomic_update(user_id) as store:
            for team in store.teams:
                if team.id == team_id:
                    return team
            name = (fallback_name or "").strip() or team_id
            team = Team(id=team_id, name=name[:MAX_TEAM_NAME_LENGTH])
            store.teams.append(team)
            logger.info(
                "Adopted dangling callable team %s into the store for %s",
                team_id,
                user_id,
            )
            return team

    # -- membership (config-side truth, one shared scan) -------------------

    def members(
        self,
        user_id: str,
        team_id: str,
        *,
        thread_ids: Optional[List[str]] = None,
    ) -> List[str]:
        return self.all_members(user_id, thread_ids=thread_ids).get(team_id, [])

    def all_members(
        self,
        user_id: str,
        *,
        thread_ids: Optional[List[str]] = None,
    ) -> Dict[str, List[str]]:
        """team_id -> member thread ids, from one pass over the configs.

        ``thread_ids`` overrides the owned-thread listing for callers whose
        thread universe is not the accounts repo (the CLI local transport).
        """
        memberships, _ = self._membership_scan(user_id, thread_ids=thread_ids)
        return memberships

    def _membership_scan(
        self,
        user_id: str,
        *,
        thread_ids: Optional[List[str]] = None,
    ) -> Tuple[Dict[str, List[str]], Dict[str, str]]:
        """One config pass: memberships plus surviving legacy names.

        The legacy names back the display fallback for dangling team ids
        (membership referencing an id absent from the store).
        """
        manager = self.thread_config_manager
        memberships: Dict[str, List[str]] = {}
        legacy_names: Dict[str, str] = {}
        if manager is None:
            return memberships, legacy_names
        scan_ids = thread_ids if thread_ids is not None else self._owned_thread_ids(user_id)
        for thread_id in scan_ids:
            tc = manager.get_config(thread_id)
            team_id = (getattr(tc, "callable_team_id", None) or None) if tc else None
            if not team_id:
                continue
            memberships.setdefault(team_id, []).append(thread_id)
            legacy = (getattr(tc, "callable_team_name", None) or "").strip()
            if legacy and team_id not in legacy_names:
                legacy_names[team_id] = legacy
        return memberships, legacy_names

    # -- hot-load chokepoint -----------------------------------------------

    def poll_external_changes(self) -> bool:
        """Debounced dir scan: report whether any store file changed on disk.

        Called from the external-edit sync chokepoint. Manager writes update
        the snapshot in ``_save`` so only genuinely external edits report
        True; the first poll primes the snapshot and reports False.
        """
        now = time.monotonic()
        with self._poll_lock:
            if now - self._last_poll < _POLL_MIN_INTERVAL:
                return False
            self._last_poll = now
            snapshot: Dict[str, Tuple[int, int]] = {}
            try:
                for path in self.teams_dir.glob("*.json"):
                    try:
                        st = path.stat()
                    except OSError:
                        continue
                    snapshot[path.name] = (st.st_mtime_ns, st.st_size)
            except OSError:
                return False
            first = self._poll_snapshot is None
            changed = not first and snapshot != self._poll_snapshot
            self._poll_snapshot = snapshot
            return changed


# ---------------------------------------------------------------------------
# Shared mutation services (backlog #100 phase 2)
#
# Every team mutation surface (REST routes, the team_manage tool, the
# nym.threads.configure verb, spawn_thread) goes through these so the
# surfaces cannot drift: one membership applier with legacy-name banking,
# one fan-out invalidation, one post-mutation chokepoint that re-tags the
# search index and nudges GUIs via the thread_teams_changed sync event.
# ---------------------------------------------------------------------------

def resolve_team_ref(
    agent: Any, user_id: str, ref: str, *, adopt: bool = True
) -> Optional[Team]:
    """Resolve a team reference (id or display name) to a store entity.

    Exact id match wins, then a case-insensitive display-name match over the
    store, then a dangling membership-referenced id. Returns None when
    nothing matches. Dangling ids are adopted into the store by default so
    mutation surfaces leave the store coherent; read-only callers pass
    ``adopt=False`` to get an UNSAVED synthetic entity instead (no store
    write on a read path).
    """
    manager = getattr(agent, "team_manager", None)
    wanted = (ref or "").strip()
    if manager is None or not wanted:
        return None
    team = manager.get_team(user_id, wanted)
    if team is not None:
        return team
    folded = wanted.casefold()
    for candidate in manager.get_store_cached(user_id).teams:
        if candidate.name.strip().casefold() == folded:
            return candidate
    memberships, legacy_names = manager._membership_scan(user_id)
    if wanted in memberships:
        if adopt:
            return manager.ensure_team_exists(
                user_id, wanted, fallback_name=legacy_names.get(wanted)
            )
        name = (legacy_names.get(wanted) or "").strip() or wanted
        return Team(id=wanted, name=name[:MAX_TEAM_NAME_LENGTH])
    return None


def apply_team_membership_to_config(
    agent: Any,
    user_id: str,
    tc: Any,
    team_id: Optional[str],
    *,
    offered_name: Optional[str] = None,
) -> bool:
    """Apply a membership change to an in-memory config (no save).

    Banks a surviving legacy name for the PREVIOUS team into the store before
    the deprecated config field is cleared, and adopts a new id the store has
    never seen (display name from ``offered_name``, else the id). Returns
    True when ``callable_team_id`` actually changed; the caller saves the
    config and, on a change, runs :func:`after_team_change`.
    """
    new_team_id = (team_id or "").strip() or None
    old_team_id = getattr(tc, "callable_team_id", None) or None
    old_team_name = getattr(tc, "callable_team_name", None)
    tc.callable_team_id = new_team_id
    tc.callable_team_name = None
    manager = getattr(agent, "team_manager", None)
    if manager is not None:
        if old_team_id and old_team_name:
            manager.ensure_team_exists(
                user_id, old_team_id, fallback_name=old_team_name
            )
        if new_team_id:
            manager.ensure_team_exists(
                user_id, new_team_id, fallback_name=offered_name
            )
    return new_team_id != old_team_id


def set_thread_team(
    agent: Any,
    user_id: str,
    thread_id: str,
    team_id: Optional[str],
    *,
    offered_name: Optional[str] = None,
) -> bool:
    """Load, apply, and save one thread's team membership.

    Returns True when the membership changed. Raises RuntimeError when the
    config save fails (transport layers map that to their own error shape).
    """
    from .thread_config import ThreadConfig

    manager = agent.thread_config_manager
    tc = manager.get_config(thread_id)
    if tc is None:
        tc = ThreadConfig(thread_id=thread_id)
    changed = apply_team_membership_to_config(
        agent, user_id, tc, team_id, offered_name=offered_name
    )
    if not manager.save_config(tc):
        raise RuntimeError(f"Failed to save team membership for thread {thread_id}")
    return changed


def invalidate_team_graphs(agent: Any, user_id: str) -> None:
    """Drop every owned thread's cached graph plus the per-user "" sentinel.

    Team visibility is cross-thread (a callable's visibility depends on its
    peers' team ids), so a membership change invalidates the whole owned set,
    not just the edited thread.
    """
    for owned_thread_id in agent.accounts_repo.list_threads_for_user(user_id):
        agent.invalidate_thread_config_cache(owned_thread_id)
    agent.invalidate_thread_config_cache("")


def publish_teams_changed(
    user_id: str, *, team_id: str = "", reason: str = ""
) -> None:
    """Best-effort ``thread_teams_changed`` sync event (GUI sidebar freshness).

    Consumers refetch the /thread-teams list; the payload is a hint, not
    state. Never raises: freshness is advisory.
    """
    try:
        from .event_bus import publish_sync_event

        data: Dict[str, Any] = {}
        if reason:
            data["reason"] = reason
        if team_id:
            data["team_id"] = team_id
        publish_sync_event(
            event_type="thread_teams_changed",
            thread_id="",
            user_id=user_id,
            data=data,
        )
    except Exception:  # noqa: BLE001 - freshness must never break a mutation
        logger.debug("thread_teams_changed publish failed", exc_info=True)


def after_team_change(
    agent: Any,
    user_id: str,
    *,
    membership_changed: bool,
    renamed: bool = False,
    team_id: str = "",
    reason: str = "",
) -> None:
    """The one post-mutation chokepoint shared by every team surface.

    Membership changes rebuild every owned graph (fan-out); a rename only
    re-tags the search index (callable tags carry the team name, no graph
    content does); every mutation nudges GUIs via the sync event.
    """
    if membership_changed:
        invalidate_team_graphs(agent, user_id)
    if membership_changed or renamed:
        try:
            from .tool_search_index import mark_tool_search_dirty

            mark_tool_search_dirty()
        except Exception:  # noqa: BLE001 - re-tag is advisory, next sweep catches up
            logger.debug(
                "tool-search dirty mark failed after team change", exc_info=True
            )
    publish_teams_changed(user_id, team_id=team_id, reason=reason)


# ---------------------------------------------------------------------------
# Shared team-list serializer (the three-scan collapse)
# ---------------------------------------------------------------------------

def serialize_thread_teams(
    agent: Any,
    user_id: str,
    *,
    thread_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """One serializer behind GET /thread-teams, the command service, and the CLI.

    Store entities carry name/description; membership comes from the shared
    config scan. Teams present in the store with no members still list (empty
    teams are legal); memberships referencing an id absent from the store
    (raw edits) list with a surviving legacy config name or the id itself.
    """
    manager = getattr(agent, "team_manager", None)
    teams: Dict[str, Dict[str, Any]] = {}
    if manager is not None:
        store = manager.get_store_cached(user_id)
        memberships, legacy_names = manager._membership_scan(
            user_id, thread_ids=thread_ids
        )
        for team in store.teams:
            teams[team.id] = {
                "id": team.id,
                "name": team.name,
                "description": team.description,
                "thread_ids": memberships.get(team.id, []),
            }
        for team_id, members in memberships.items():
            if team_id not in teams:
                teams[team_id] = {
                    "id": team_id,
                    "name": legacy_names.get(team_id) or team_id,
                    "description": None,
                    "thread_ids": members,
                }
    else:
        # Degraded path for agents without a team manager (test fakes): the
        # legacy pure config grouping, deprecated-name fallback included.
        tcm = getattr(agent, "thread_config_manager", None)
        repo = getattr(agent, "accounts_repo", None)
        scan_ids = thread_ids
        if scan_ids is None:
            scan_ids = list(repo.list_threads_for_user(user_id)) if repo else []
        for thread_id in scan_ids:
            tc = tcm.get_config(thread_id) if tcm is not None else None
            team_id = (getattr(tc, "callable_team_id", None) or None) if tc else None
            if not team_id:
                continue
            team = teams.setdefault(
                team_id,
                {
                    "id": team_id,
                    "name": (getattr(tc, "callable_team_name", None) or team_id),
                    "description": None,
                    "thread_ids": [],
                },
            )
            team["thread_ids"].append(thread_id)

    result = sorted(teams.values(), key=lambda t: str(t["name"]).casefold())
    return {"teams": result, "total": len(result)}
