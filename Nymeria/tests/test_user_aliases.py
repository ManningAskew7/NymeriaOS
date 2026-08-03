"""Per-user command alias store (backlog #133).

Persistence, the per-user cap, and the authoring-stamp control
(control-store-matrix rule 3): rows are stamped by the authoring path and
verified at dispatch resolution, so an out-of-band edit yields an inert
alias, not a rewritten dispatch.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nymeria.core.user_aliases import (
    AliasAlreadyExists,
    AliasLimitReached,
    UserAliasesRepo,
)


@pytest.fixture()
def repo(tmp_path: Path) -> UserAliasesRepo:
    return UserAliasesRepo(tmp_path / "accounts.db")


def _create(
    repo: UserAliasesRepo,
    name: str = "gpt5",
    tokens: tuple[str, ...] = ("model", "openai/gpt-5.5"),
    user: str = "alice",
    actor: str = "user",
):
    return repo.create_alias(
        user_id=user,
        name=name,
        command_id="model",
        tokens=tokens,
        author_actor=actor,
    )


def test_create_and_resolve_round_trip(repo: UserAliasesRepo) -> None:
    created = _create(repo)
    assert created.stamp_valid is True

    resolved = repo.resolve_for_dispatch("alice", "gpt5")
    assert resolved is not None
    assert resolved.tokens == ("model", "openai/gpt-5.5")
    assert resolved.command_id == "model"
    assert resolved.author_actor == "user"

    # Another user's table is a different namespace.
    assert repo.resolve_for_dispatch("bob", "gpt5") is None


def test_duplicate_name_rejected_per_user_not_globally(repo: UserAliasesRepo) -> None:
    _create(repo)
    with pytest.raises(AliasAlreadyExists):
        _create(repo, tokens=("thread", "list"))
    # Same spelling for a different user is fine.
    other = _create(repo, user="bob")
    assert other.name == "gpt5"


def test_delete_removes_exactly_one_row(repo: UserAliasesRepo) -> None:
    _create(repo)
    _create(repo, user="bob")

    assert repo.delete_alias(user_id="alice", name="gpt5") is True
    assert repo.resolve_for_dispatch("alice", "gpt5") is None
    assert repo.delete_alias(user_id="alice", name="gpt5") is False
    # Bob's identically-named alias survives.
    assert repo.resolve_for_dispatch("bob", "gpt5") is not None


def test_list_is_name_ordered_and_stamped(repo: UserAliasesRepo) -> None:
    _create(repo, name="zz", tokens=("todos", "list"))
    _create(repo, name="aa", tokens=("thread", "list"))

    listed = repo.list_aliases("alice")
    assert [alias.name for alias in listed] == ["aa", "zz"]
    assert all(alias.stamp_valid for alias in listed)


def _tamper(repo: UserAliasesRepo, column: str, value: str) -> None:
    """Simulate the out-of-band writer the control exists for."""
    with sqlite3.connect(str(repo.db_path)) as conn:
        conn.execute(
            f"UPDATE user_command_aliases SET {column} = ? WHERE name = 'gpt5'",
            (value,),
        )
        conn.commit()


def test_control_a_tampered_expansion_goes_inert(repo: UserAliasesRepo) -> None:
    """The rule-3 gate: an edit that skipped the authoring path leaves the
    stamp stale, and the dispatch read refuses the row instead of running
    the planted expansion. The row stays VISIBLE in the listing, flagged,
    so the user learns to re-create it.
    """
    _create(repo)
    _tamper(repo, "tokens_json", '["env", "set", "llm_model", "evil"]')

    assert repo.resolve_for_dispatch("alice", "gpt5") is None
    listed = repo.list_aliases("alice")
    assert len(listed) == 1
    assert listed[0].stamp_valid is False


def test_control_covers_the_authoring_actor(repo: UserAliasesRepo) -> None:
    """Retagging WHO authored an alias is a forge attempt the stamp must
    catch: authorship is a security-relevant field (it is what /alias list
    reports to the human)."""
    _create(repo, actor="agent")
    _tamper(repo, "author_actor", "user")

    assert repo.resolve_for_dispatch("alice", "gpt5") is None


def test_control_covers_the_recorded_target(repo: UserAliasesRepo) -> None:
    """command_id drives the LISTING's dormancy verdict, not dispatch, but a
    field the listing trusts and the stamp ignores is a field an
    out-of-band writer can lie through."""
    _create(repo)
    _tamper(repo, "command_id", "somewhere.else")

    assert repo.resolve_for_dispatch("alice", "gpt5") is None
    assert repo.list_aliases("alice")[0].stamp_valid is False


def test_corrupt_tokens_json_is_inert_not_crashing(repo: UserAliasesRepo) -> None:
    _create(repo)
    _tamper(repo, "tokens_json", "not json")

    assert repo.resolve_for_dispatch("alice", "gpt5") is None
    assert repo.list_aliases("alice")[0].stamp_valid is False


def test_per_user_cap(repo: UserAliasesRepo, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nymeria.core.user_aliases.MAX_ALIASES_PER_USER", 2)
    _create(repo, name="one", tokens=("thread", "list"))
    _create(repo, name="two", tokens=("todos", "list"))
    with pytest.raises(AliasLimitReached):
        _create(repo, name="three", tokens=("memory", "list"))
    # The cap is per user, not global.
    other = _create(repo, name="one", user="bob", tokens=("thread", "list"))
    assert other.stamp_valid is True
