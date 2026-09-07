"""Identity ids are canonical storage segments, enforced at creation.

Every per-identity store keys its file on ``safe_path_segment(id)``, so two
distinct ids that fold to one segment share one file (``alice.smith`` and
``alicesmith``; any all-punctuation id and the owner's ``default``). The fix
refuses a NON-canonical id at the two creation INSERTs in ``AccountsRepo``
(``create_user`` and the first-touch ``claim_thread``), grandfathers every
pre-existing row untouched, and reports collision groups for the startup
audit. Behaviors 1 to 6 of the #349 identity-id boundary pass spec.
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

import pytest

from nymeria.core.accounts import (
    AccountsRepo,
    InvalidIdentityId,
    UserAlreadyExists,
    find_identity_collisions,
)
from nymeria.core.storage_paths import canonical_segment_error

FIX_COPY = "letters, digits, '-' and '_'"

NON_CANONICAL_IDS = [
    ("alice.smith", "alicesmith"),
    (" alice", "alice"),
    ("a/b", "ab"),
    ("..", "default"),
    ("!!!", "default"),
    ("  ", "default"),
    ("", "default"),
]


def _repo(tmp_path: Path) -> AccountsRepo:
    return AccountsRepo(tmp_path / "accounts.db")


def _insert_legacy_user(repo: AccountsRepo, user_id: str) -> None:
    """A row exactly as the pre-fix code wrote it (no id validation)."""
    with sqlite3.connect(repo.db_path) as conn:
        conn.execute(
            "INSERT INTO users (id, email, display_name, role, disabled, created_at, updated_at) "
            "VALUES (?, ?, ?, 'user', 0, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
            (user_id, f"{uuid.uuid4().hex[:8]}@legacy.example", user_id),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# accounts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("user_id", "segment"), NON_CANONICAL_IDS)
def test_create_user_refuses_non_canonical_id_before_writing(
    tmp_path: Path, user_id: str, segment: str
) -> None:
    repo = _repo(tmp_path)

    with pytest.raises(InvalidIdentityId) as excinfo:
        repo.create_user(user_id, "alice@example.com", "Alice")

    message = str(excinfo.value)
    assert message.startswith(f"User id {user_id!r}")
    assert f"stored as {segment!r}" in message
    assert FIX_COPY in message
    assert repo.get_user_by_id(user_id) is None
    assert repo.get_user_by_email("alice@example.com") is None
    # The email is still free: no row landed under any id.
    assert repo.create_user("alice", "alice@example.com", "Alice").id == "alice"


def test_create_user_accepts_canonical_ids(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    for user_id in ["alice-smith", "alice_smith", "Alice42", "déjà"]:
        created = repo.create_user(user_id, f"{user_id}@example.com", user_id)
        assert created.id == user_id
        assert repo.get_user_by_id(user_id) is not None


def test_create_user_refuses_case_variant_of_existing_id(tmp_path: Path) -> None:
    """Case-insensitive filesystems (Windows, macOS) make ``Alice`` and
    ``alice`` one store file, so a NEW id that differs from an existing row
    only by case is refused; an exact duplicate stays the conflict error."""
    repo = _repo(tmp_path)
    repo.create_user("alice", "alice@example.com", "Alice")

    for variant in ["Alice", "ALICE"]:
        with pytest.raises(InvalidIdentityId) as excinfo:
            repo.create_user(variant, f"{variant}@example.com", variant)
        assert "'alice'" in str(excinfo.value)
        assert "case" in str(excinfo.value)
        assert repo.get_user_by_id(variant) is None
        assert repo.get_user_by_email(f"{variant}@example.com") is None
    with pytest.raises(UserAlreadyExists):
        repo.create_user("alice", "other@example.com", "Alice")


def test_grandfathered_case_variant_users_both_keep_working(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _insert_legacy_user(repo, "Alice")
    _insert_legacy_user(repo, "alice")

    for user_id in ["Alice", "alice"]:
        token = repo.issue_token(user_id, label="laptop")
        assert repo.verify_token(token).id == user_id


def test_grandfathered_non_canonical_user_keeps_working(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _insert_legacy_user(repo, "alice.smith")

    token = repo.issue_token("alice.smith", label="laptop")
    repo.update_user("alice.smith", display_name="Alice S")

    authenticated = repo.verify_token(token)
    assert authenticated is not None
    assert authenticated.id == "alice.smith"
    assert repo.get_user_by_id("alice.smith").display_name == "Alice S"
    assert repo.list_tokens_for_user("alice.smith")[0].label == "laptop"


# ---------------------------------------------------------------------------
# threads
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("thread_id", ["qa-collide/1", "thread:1", "..", "!!!", ""])
def test_claim_thread_refuses_new_non_canonical_thread(
    tmp_path: Path, thread_id: str
) -> None:
    repo = _repo(tmp_path)
    repo.create_user("alice", "alice@example.com", "Alice")

    with pytest.raises(InvalidIdentityId) as excinfo:
        repo.claim_thread(thread_id, "alice")

    message = str(excinfo.value)
    assert message.startswith(f"Thread id {thread_id!r}")
    assert FIX_COPY in message
    assert repo.get_thread_owner(thread_id) is None
    assert repo.list_threads_for_user("alice") == []


def test_claim_thread_accepts_every_system_generated_id_shape(tmp_path: Path) -> None:
    """Built from the real id builders, not typed literals, so a builder that
    starts emitting a character ``safe_path_segment`` drops (a dot, a colon)
    turns this red. The shapes without a helper are the inline f-strings at
    ``tools/spawn_thread.py`` (``spawned-``), ``api/routers/chat.py``
    (``spawned-quick-``), ``api/routers/agent_threads.py`` (``agent-``, name
    schema ``^[a-zA-Z0-9_-]+$``), ``core/thread_branch.py``,
    ``api/routers/thread_operations.py`` (``imported-``), and
    ``core/dreaming/invoke.py``; their slug helpers are called for real."""
    import importlib

    from nymeria.core.dreaming.invoke import DREAM_THREAD_ID_PREFIX, _slug_from_thread_id
    from nymeria.triggers import discord_bot, slack_bot, teams_bot, telegram_bot, whatsapp_bot

    spawn = importlib.import_module("nymeria.tools.spawn_thread")
    repo = _repo(tmp_path)
    repo.create_user("alice", "alice@example.com", "Alice")
    hex8 = uuid.uuid4().hex[:8]
    thread_ids = [
        discord_bot.make_thread_id(None, 123),
        discord_bot.make_thread_id(1, 2),
        telegram_bot.make_thread_id(42),
        telegram_bot.make_thread_id(-100123),
        slack_bot.make_thread_id("T1", "D1", user_id="U1", is_dm=True),
        slack_bot.make_thread_id("T1", "C1", thread_ts="1712345678.123456"),
        whatsapp_bot.make_thread_id("+61 400.000.000"),
        teams_bot.make_thread_id(
            "tenant-guid", "a:1abc", conversation_type="personal", user_id="29:1abc-def"
        ),
        f"spawned-{spawn._slug_from_title('Q&A: v2.0 / notes')}-{hex8}",
        f"spawned-quick-{hex8}",
        f"agent-{'Helper_Bot-2'.lower()}-{hex8}",
        f"branch-{uuid.uuid4().hex[:12]}",
        f"imported-{uuid.uuid4().hex[:12]}",
        f"{DREAM_THREAD_ID_PREFIX}-{_slug_from_thread_id('a.b/c d')}-20260907T000000-{hex8[:6]}",
        f"trigger-{uuid.uuid4()}",
        f"todo-{str(uuid.uuid4())[:8]}",
        str(uuid.uuid4()),
        uuid.uuid4().hex[:8],
        "default",
    ]

    for thread_id in thread_ids:
        assert repo.claim_thread(thread_id, "alice") == "alice", thread_id

    assert set(repo.list_threads_for_user("alice")) == set(thread_ids)


def test_teams_channel_ids_are_the_one_non_canonical_system_shape_and_never_claimed() -> None:
    """Pins the known residual. Bot Framework conversation ids end in a fixed
    dotted suffix (``@thread.tacv2``) that ``bot_helpers.safe_id`` keeps, so a
    Teams CHANNEL thread id is not canonical. It is a shared channel, which no
    access gate ever claims, so it never reaches the creation rule; a Teams DM
    id is canonical. Dropping ``.`` from ``safe_id`` would rename every
    existing Teams channel thread, which is why the charset stays."""
    from nymeria.core.thread_classification import is_shared_channel
    from nymeria.triggers import teams_bot

    channel = teams_bot.make_thread_id("tenant-guid", "19:abc@thread.tacv2;messageid=1")
    dm = teams_bot.make_thread_id(
        "tenant-guid", "a:1abc", conversation_type="personal", user_id="29:1abc"
    )

    assert canonical_segment_error(channel) is not None
    assert is_shared_channel(channel)
    assert canonical_segment_error(dm) is None
    assert not is_shared_channel(dm)


def test_grandfathered_non_canonical_thread_keeps_working(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    repo.create_user("alice", "alice@example.com", "Alice")
    repo.create_user("bob", "bob@example.com", "Bob")
    # The boot backfill is the path that registers pre-existing threads.
    assert repo.backfill_threads(["qa-collide/1"], "alice") == 1

    assert repo.claim_thread("qa-collide/1", "alice") == "alice"
    assert repo.claim_thread("qa-collide/1", "bob") == "alice"
    assert repo.get_thread_owner("qa-collide/1") == "alice"

    # Once the row is gone the same id is a NEW thread again, and refused.
    assert repo.delete_thread_owner("qa-collide/1") is True
    with pytest.raises(InvalidIdentityId):
        repo.claim_thread("qa-collide/1", "alice")


def test_claim_thread_refuses_case_variant_of_existing_thread(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    repo.create_user("alice", "alice@example.com", "Alice")
    repo.create_user("bob", "bob@example.com", "Bob")
    repo.claim_thread("qa-thread", "alice")

    with pytest.raises(InvalidIdentityId) as excinfo:
        repo.claim_thread("QA-Thread", "bob")

    assert "'qa-thread'" in str(excinfo.value)
    assert "case" in str(excinfo.value)
    assert repo.get_thread_owner("QA-Thread") is None
    # A grandfathered pair keeps working for both spellings (exact rows exist).
    repo.backfill_threads(["Legacy", "legacy"], "alice")
    assert repo.claim_thread("Legacy", "bob") == "alice"
    assert repo.claim_thread("legacy", "bob") == "alice"


def test_backfill_threads_never_refuses(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    repo.create_user("alice", "alice@example.com", "Alice")

    assert repo.backfill_threads(["a/b", "!!!", "ok"], "alice") == 3
    assert sorted(repo.list_thread_ids()) == ["!!!", "a/b", "ok"]


def test_list_thread_ids_spans_every_owner(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    repo.create_user("alice", "alice@example.com", "Alice")
    repo.create_user("bob", "bob@example.com", "Bob")
    repo.claim_thread("t-alice", "alice")
    repo.claim_thread("t-bob", "bob")

    assert sorted(repo.list_thread_ids()) == ["t-alice", "t-bob"]
    assert repo.list_thread_ids() != repo.list_threads_for_user("alice")


# ---------------------------------------------------------------------------
# startup audit source
# ---------------------------------------------------------------------------


def test_find_identity_collisions_groups_per_namespace(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    repo.create_user("default", "owner@localhost", "Owner", role="admin")
    repo.create_user("bob", "bob@example.com", "Bob")
    for legacy in ["alice.smith", "alicesmith", "!!!", "lone.wolf"]:
        _insert_legacy_user(repo, legacy)
    # Thread "alicesmith" must NOT group with user "alicesmith": different stores.
    repo.backfill_threads(
        ["qa-collide/1", "qa-collide1", "qa-collide.1", "solo.thread", "alicesmith"],
        "default",
    )

    collisions = find_identity_collisions(repo)

    assert [tuple(c) for c in collisions] == [
        ("user", "alicesmith", ["alice.smith", "alicesmith"]),
        ("user", "default", ["!!!", "default"]),
        ("thread", "qa-collide1", ["qa-collide.1", "qa-collide/1", "qa-collide1"]),
    ]


def test_find_identity_collisions_groups_case_variants(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    repo.create_user("default", "owner@localhost", "Owner", role="admin")
    _insert_legacy_user(repo, "Alice")
    _insert_legacy_user(repo, "alice")
    repo.backfill_threads(["QA", "qa"], "default")

    assert [tuple(c) for c in find_identity_collisions(repo)] == [
        ("user", "alice", ["Alice", "alice"]),
        ("thread", "qa", ["QA", "qa"]),
    ]


def test_find_identity_collisions_is_empty_on_a_clean_deployment(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    repo.create_user("default", "owner@localhost", "Owner", role="admin")
    repo.create_user("alice", "alice@example.com", "Alice")
    repo.claim_thread("qa-collide1", "alice")
    repo.claim_thread("telegram_-100123", "default")

    assert find_identity_collisions(repo) == []
