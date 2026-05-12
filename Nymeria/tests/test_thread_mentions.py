from __future__ import annotations

from nymeria.core.accounts import AccountsRepo
from nymeria.core.mention import MentionAmbiguity, MentionTarget, resolve_thread_mention
from nymeria.core.thread_metadata import ThreadMetadataManager


def _repo(tmp_path):
    metadata = ThreadMetadataManager(tmp_path)
    accounts = AccountsRepo(tmp_path / "accounts.db")
    accounts.create_user("user-1", "user@example.com", "User")
    return metadata, accounts


def test_resolves_quoted_title_and_strips_prompt(tmp_path) -> None:
    metadata, accounts = _repo(tmp_path)
    accounts.claim_thread("thread-research", "user-1")
    metadata.upsert_thread("user-1", "thread-research", title="Research Notes")

    result = resolve_thread_mention(
        '@"Research Notes" summarize this',
        user_id="user-1",
        thread_metadata_manager=metadata,
        accounts_repo=accounts,
    )

    assert isinstance(result, MentionTarget)
    assert result.thread_id == "thread-research"
    assert result.title == "Research Notes"
    assert result.message == "summarize this"


def test_unmatched_at_prefix_falls_through_to_normal_chat(tmp_path) -> None:
    metadata, accounts = _repo(tmp_path)
    accounts.claim_thread("thread-research", "user-1")
    metadata.upsert_thread("user-1", "thread-research", title="Research")

    result = resolve_thread_mention(
        "@someone hello there",
        user_id="user-1",
        thread_metadata_manager=metadata,
        accounts_repo=accounts,
    )

    assert result is None


def test_ambiguous_substring_returns_candidates(tmp_path) -> None:
    metadata, accounts = _repo(tmp_path)
    accounts.claim_thread("thread-alpha", "user-1")
    accounts.claim_thread("thread-beta", "user-1")
    metadata.upsert_thread("user-1", "thread-alpha", title="Research Alpha")
    metadata.upsert_thread("user-1", "thread-beta", title="Research Beta")

    result = resolve_thread_mention(
        "@Research compare notes",
        user_id="user-1",
        thread_metadata_manager=metadata,
        accounts_repo=accounts,
    )

    assert isinstance(result, MentionAmbiguity)
    assert [candidate.thread_id for candidate in result.candidates] == [
        "thread-alpha",
        "thread-beta",
    ]
