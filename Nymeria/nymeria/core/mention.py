"""Thread @mention parsing and resolution."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class MentionCandidate:
    """Thread candidate surfaced when a mention is ambiguous."""

    thread_id: str
    title: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MentionTarget:
    """Resolved target thread and stripped prompt."""

    thread_id: str
    title: str
    reference: str
    message: str


@dataclass(frozen=True, slots=True)
class MentionAmbiguity:
    """Multiple threads matched the same mention reference."""

    reference: str
    candidates: tuple[MentionCandidate, ...]


MentionResolution = MentionTarget | MentionAmbiguity | None


def resolve_thread_mention(
    message: str,
    *,
    user_id: str,
    thread_metadata_manager: Any,
    accounts_repo: Any,
    thread_config_manager: Any | None = None,
) -> MentionResolution:
    """Resolve a leading ``@thread`` prefix to a user-visible thread.

    Resolution order follows the CLI gap task:
    exact title match, unique title substring match, then unique ID prefix.
    If no thread matches, return ``None`` so natural text beginning with ``@``
    remains a normal message.
    """

    parsed = _parse_mention_prefix(message)
    if parsed is None:
        return None

    reference, prompt = parsed
    candidates = _visible_threads(
        user_id=user_id,
        thread_metadata_manager=thread_metadata_manager,
        accounts_repo=accounts_repo,
        thread_config_manager=thread_config_manager,
    )
    if not candidates:
        return None

    resolved = _resolve_against_candidates(reference, prompt, candidates)
    return resolved


def _parse_mention_prefix(message: str) -> tuple[str, str] | None:
    stripped = message.lstrip()
    if not stripped.startswith("@"):
        return None

    remainder = stripped[1:]
    if not remainder:
        return None

    if remainder.startswith('"'):
        closing = remainder.find('"', 1)
        if closing <= 1:
            return None
        reference = remainder[1:closing].strip()
        prompt = remainder[closing + 1 :].lstrip()
    else:
        parts = remainder.split(None, 1)
        reference = parts[0].strip()
        prompt = parts[1].lstrip() if len(parts) > 1 else ""

    if not reference or not prompt:
        return None
    return reference, prompt


def _visible_threads(
    *,
    user_id: str,
    thread_metadata_manager: Any,
    accounts_repo: Any,
    thread_config_manager: Any | None,
) -> tuple[MentionCandidate, ...]:
    try:
        owned_ids = set(accounts_repo.list_threads_for_user(user_id))
    except Exception:  # noqa: BLE001 - mention resolution must be best effort.
        owned_ids = set()

    try:
        store = thread_metadata_manager.get_store(user_id)
        metadata = dict(getattr(store, "threads", {}) or {})
    except Exception:  # noqa: BLE001 - fall back to owned IDs if metadata is bad.
        metadata = {}

    thread_ids = set(owned_ids)
    if not owned_ids:
        thread_ids.update(metadata)

    candidates: list[MentionCandidate] = []
    for thread_id in sorted(thread_ids):
        meta = metadata.get(thread_id)
        metadata_title = str(getattr(meta, "title", "") or "").strip()
        callable_name = _callable_name(thread_config_manager, thread_id)
        title = callable_name or metadata_title or thread_id
        aliases = _aliases(title, metadata_title, callable_name)
        candidates.append(
            MentionCandidate(thread_id=thread_id, title=title, aliases=aliases)
        )
    return tuple(candidates)


def _callable_name(thread_config_manager: Any | None, thread_id: str) -> str:
    if thread_config_manager is None:
        return ""
    try:
        config = thread_config_manager.get_config(thread_id)
    except Exception:  # noqa: BLE001 - mention resolution must be best effort.
        return ""
    if not config or not getattr(config, "callable", False):
        return ""
    return str(getattr(config, "callable_name", "") or "").strip()


def _aliases(*values: str) -> tuple[str, ...]:
    aliases: list[str] = []
    seen: set[str] = set()
    for value in values:
        alias = str(value or "").strip()
        key = alias.casefold()
        if not alias or key in seen:
            continue
        aliases.append(alias)
        seen.add(key)
    return tuple(aliases)


def _resolve_against_candidates(
    reference: str,
    prompt: str,
    candidates: Iterable[MentionCandidate],
) -> MentionResolution:
    candidate_tuple = tuple(candidates)
    reference_key = reference.casefold()

    exact_title = _matching_candidates(
        candidate_tuple,
        lambda alias: alias.casefold() == reference_key,
    )
    if exact_title:
        return _target_or_ambiguity(reference, prompt, exact_title)

    title_substring = _matching_candidates(
        candidate_tuple,
        lambda alias: reference_key in alias.casefold(),
    )
    if title_substring:
        return _target_or_ambiguity(reference, prompt, title_substring)

    id_prefix = [
        candidate
        for candidate in candidate_tuple
        if candidate.thread_id.casefold().startswith(reference_key)
    ]
    if id_prefix:
        return _target_or_ambiguity(reference, prompt, id_prefix)

    return None


def _matching_candidates(
    candidates: Iterable[MentionCandidate],
    matches: Callable[[str], bool],
) -> list[MentionCandidate]:
    selected: list[MentionCandidate] = []
    seen: set[str] = set()
    for candidate in candidates:
        aliases = candidate.aliases or (candidate.title,)
        if not any(matches(alias) for alias in aliases):
            continue
        if candidate.thread_id in seen:
            continue
        selected.append(candidate)
        seen.add(candidate.thread_id)
    return selected


def _target_or_ambiguity(
    reference: str,
    prompt: str,
    candidates: list[MentionCandidate],
) -> MentionTarget | MentionAmbiguity:
    ordered = tuple(
        sorted(candidates, key=lambda item: (item.title.casefold(), item.thread_id))
    )
    if len(ordered) == 1:
        candidate = ordered[0]
        return MentionTarget(
            thread_id=candidate.thread_id,
            title=candidate.title,
            reference=reference,
            message=prompt,
        )
    return MentionAmbiguity(reference=reference, candidates=ordered)


__all__ = [
    "MentionAmbiguity",
    "MentionCandidate",
    "MentionResolution",
    "MentionTarget",
    "resolve_thread_mention",
]
