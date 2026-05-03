"""Tests for ChatBindingsRepo: bind code claim, BYO bot registration, binding delete."""

from __future__ import annotations

from pathlib import Path

import pytest

from nymeria.core.accounts import AccountsRepo, UserNotFound
from nymeria.core.chat_bindings import (
    BindCodeInvalid,
    BindingAlreadyExists,
    BotAlreadyRegistered,
    ChatBindingsRepo,
)


@pytest.fixture()
def repos(tmp_path: Path):
    accounts_db = tmp_path / "accounts.db"
    accounts = AccountsRepo(accounts_db)
    bindings = ChatBindingsRepo(accounts_db)
    accounts.create_user("alice", "alice@test.com", "Alice")
    accounts.create_user("bob", "bob@test.com", "Bob")
    return accounts, bindings


# -- thread binding CRUD --------------------------------------------------


def test_create_and_lookup_binding(repos):
    _, bindings = repos
    b = bindings.create_thread_binding(
        thread_id="t1",
        provider="telegram",
        platform_chat_id="999",
        user_id="alice",
    )
    assert b.thread_id == "t1"
    assert b.provider == "telegram"
    assert b.platform_chat_id == "999"
    assert b.user_id == "alice"
    assert b.user_telegram_bot_id is None

    by_chat = bindings.lookup_thread_binding_by_chat("telegram", "999")
    assert by_chat is not None
    assert by_chat.id == b.id

    by_thread = bindings.lookup_thread_binding_by_thread("telegram", "t1")
    assert by_thread is not None
    assert by_thread.id == b.id


def test_create_binding_duplicate_raises(repos):
    _, bindings = repos
    bindings.create_thread_binding(
        thread_id="t1", provider="telegram", platform_chat_id="111", user_id="alice"
    )
    with pytest.raises(BindingAlreadyExists):
        bindings.create_thread_binding(
            thread_id="t2", provider="telegram", platform_chat_id="111", user_id="alice"
        )


def test_create_binding_unknown_user_raises(repos):
    _, bindings = repos
    with pytest.raises(UserNotFound):
        bindings.create_thread_binding(
            thread_id="t1", provider="telegram", platform_chat_id="111", user_id="ghost"
        )


def test_delete_binding_by_owner(repos):
    _, bindings = repos
    b = bindings.create_thread_binding(
        thread_id="t1", provider="telegram", platform_chat_id="111", user_id="alice"
    )
    assert bindings.delete_thread_binding(b.id, user_id="alice") is True
    assert bindings.lookup_thread_binding_by_thread("telegram", "t1") is None


def test_delete_binding_wrong_owner_returns_false(repos):
    _, bindings = repos
    b = bindings.create_thread_binding(
        thread_id="t1", provider="telegram", platform_chat_id="111", user_id="alice"
    )
    assert bindings.delete_thread_binding(b.id, user_id="bob") is False
    assert bindings.lookup_thread_binding_by_thread("telegram", "t1") is not None


def test_delete_bindings_for_thread(repos):
    _, bindings = repos
    bindings.create_thread_binding(
        thread_id="t1", provider="telegram", platform_chat_id="111", user_id="alice"
    )
    count = bindings.delete_thread_bindings_for_thread("t1")
    assert count == 1
    assert bindings.lookup_thread_binding_by_thread("telegram", "t1") is None


def test_list_thread_bindings(repos):
    _, bindings = repos
    bindings.create_thread_binding(
        thread_id="t1", provider="telegram", platform_chat_id="111", user_id="alice"
    )
    result = bindings.list_thread_bindings("t1")
    assert len(result) == 1
    assert result[0].thread_id == "t1"


def test_list_bindings_for_user(repos):
    _, bindings = repos
    bindings.create_thread_binding(
        thread_id="t1", provider="telegram", platform_chat_id="111", user_id="alice"
    )
    bindings.create_thread_binding(
        thread_id="t2", provider="telegram", platform_chat_id="222", user_id="bob"
    )
    alice_bindings = bindings.list_thread_bindings_for_user("alice")
    assert len(alice_bindings) == 1
    assert alice_bindings[0].thread_id == "t1"


# -- bind codes ------------------------------------------------------------


def test_issue_and_claim_thread_bind_code(repos):
    _, bindings = repos
    raw = bindings.issue_bind_code(
        kind="thread_bind", provider="telegram", user_id="alice", thread_id="t1"
    )
    assert len(raw) == 8

    claim = bindings.claim_bind_code(raw, kind="thread_bind", provider="telegram")
    assert claim.kind == "thread_bind"
    assert claim.provider == "telegram"
    assert claim.user_id == "alice"
    assert claim.thread_id == "t1"


def test_issue_and_claim_platform_link_code(repos):
    _, bindings = repos
    raw = bindings.issue_bind_code(
        kind="platform_link", provider="telegram", user_id="alice"
    )
    claim = bindings.claim_bind_code(raw, kind="platform_link", provider="telegram")
    assert claim.kind == "platform_link"
    assert claim.thread_id is None


def test_claim_bind_code_already_used_raises(repos):
    _, bindings = repos
    raw = bindings.issue_bind_code(
        kind="thread_bind", provider="telegram", user_id="alice", thread_id="t1"
    )
    bindings.claim_bind_code(raw, kind="thread_bind", provider="telegram")
    with pytest.raises(BindCodeInvalid, match="already used"):
        bindings.claim_bind_code(raw, kind="thread_bind", provider="telegram")


def test_claim_bind_code_expired_raises(repos):
    _, bindings = repos
    raw = bindings.issue_bind_code(
        kind="thread_bind",
        provider="telegram",
        user_id="alice",
        thread_id="t1",
        ttl_seconds=-1,
    )
    with pytest.raises(BindCodeInvalid, match="expired"):
        bindings.claim_bind_code(raw, kind="thread_bind", provider="telegram")


def test_claim_bind_code_wrong_kind_raises(repos):
    _, bindings = repos
    raw = bindings.issue_bind_code(
        kind="thread_bind", provider="telegram", user_id="alice", thread_id="t1"
    )
    with pytest.raises(BindCodeInvalid):
        bindings.claim_bind_code(raw, kind="platform_link", provider="telegram")


def test_claim_bind_code_empty_raises(repos):
    _, bindings = repos
    with pytest.raises(BindCodeInvalid, match="empty"):
        bindings.claim_bind_code("", kind="thread_bind", provider="telegram")


def test_claim_bind_code_unknown_raises(repos):
    _, bindings = repos
    with pytest.raises(BindCodeInvalid, match="unknown"):
        bindings.claim_bind_code("ZZZZZZZZ", kind="thread_bind", provider="telegram")


def test_issue_bind_code_thread_required_for_thread_bind(repos):
    _, bindings = repos
    with pytest.raises(ValueError, match="thread_id"):
        bindings.issue_bind_code(
            kind="thread_bind", provider="telegram", user_id="alice"
        )


def test_issue_bind_code_unknown_user_raises(repos):
    _, bindings = repos
    with pytest.raises(UserNotFound):
        bindings.issue_bind_code(
            kind="thread_bind", provider="telegram", user_id="ghost", thread_id="t1"
        )


def test_delete_bind_codes_for_thread(repos):
    _, bindings = repos
    bindings.issue_bind_code(
        kind="thread_bind", provider="telegram", user_id="alice", thread_id="t1"
    )
    count = bindings.delete_bind_codes_for_thread("t1")
    assert count == 1


def test_purge_expired_bind_codes(repos):
    _, bindings = repos
    bindings.issue_bind_code(
        kind="thread_bind",
        provider="telegram",
        user_id="alice",
        thread_id="t1",
        ttl_seconds=-1,
    )
    purged = bindings.purge_expired_bind_codes()
    assert purged == 1


# -- BYO Telegram bots ----------------------------------------------------


def test_register_and_list_bot(repos):
    _, bindings = repos
    bot = bindings.register_user_telegram_bot(
        owner_user_id="alice",
        bot_username="AliceTestBot",
        bot_token_ciphertext="encrypted-token-data",
    )
    assert bot.owner_user_id == "alice"
    assert bot.bot_username == "AliceTestBot"
    assert bot.enabled is True
    assert bot.last_seen_at is None

    bots = bindings.list_user_telegram_bots("alice")
    assert len(bots) == 1
    assert bots[0].id == bot.id


def test_register_bot_duplicate_username_raises(repos):
    _, bindings = repos
    bindings.register_user_telegram_bot(
        owner_user_id="alice",
        bot_username="SharedBot",
        bot_token_ciphertext="ct1",
    )
    with pytest.raises(BotAlreadyRegistered):
        bindings.register_user_telegram_bot(
            owner_user_id="bob",
            bot_username="SharedBot",
            bot_token_ciphertext="ct2",
        )


def test_register_bot_unknown_user_raises(repos):
    _, bindings = repos
    with pytest.raises(UserNotFound):
        bindings.register_user_telegram_bot(
            owner_user_id="ghost",
            bot_username="GhostBot",
            bot_token_ciphertext="ct",
        )


def test_get_bot_by_id_and_owner(repos):
    _, bindings = repos
    bot = bindings.register_user_telegram_bot(
        owner_user_id="alice",
        bot_username="MyBot",
        bot_token_ciphertext="ct",
    )
    assert bindings.get_user_telegram_bot(bot.id) is not None
    assert bindings.get_user_telegram_bot(bot.id, owner_user_id="alice") is not None
    assert bindings.get_user_telegram_bot(bot.id, owner_user_id="bob") is None


def test_get_bot_by_username(repos):
    _, bindings = repos
    bindings.register_user_telegram_bot(
        owner_user_id="alice",
        bot_username="FindMe",
        bot_token_ciphertext="ct",
    )
    assert bindings.get_user_telegram_bot_by_username("FindMe") is not None
    assert bindings.get_user_telegram_bot_by_username("NoSuch") is None


def test_delete_bot_owner_scoped(repos):
    _, bindings = repos
    bot = bindings.register_user_telegram_bot(
        owner_user_id="alice",
        bot_username="DelBot",
        bot_token_ciphertext="ct",
    )
    assert bindings.delete_user_telegram_bot(bot.id, owner_user_id="bob") is False
    assert bindings.delete_user_telegram_bot(bot.id, owner_user_id="alice") is True
    assert bindings.get_user_telegram_bot(bot.id) is None


def test_bot_seen_heartbeat(repos):
    _, bindings = repos
    bot = bindings.register_user_telegram_bot(
        owner_user_id="alice",
        bot_username="HeartBot",
        bot_token_ciphertext="ct",
    )
    assert bot.last_seen_at is None
    bindings.update_user_telegram_bot_seen(bot.id)
    refreshed = bindings.get_user_telegram_bot(bot.id)
    assert refreshed.last_seen_at is not None


def test_list_bots_with_ciphertext(repos):
    _, bindings = repos
    bindings.register_user_telegram_bot(
        owner_user_id="alice",
        bot_username="CTBot",
        bot_token_ciphertext="secret-ct-value",
    )
    pairs = bindings.list_user_telegram_bots_with_ciphertext()
    assert len(pairs) == 1
    bot_meta, ct = pairs[0]
    assert bot_meta.bot_username == "CTBot"
    assert ct == "secret-ct-value"


def test_binding_with_byo_bot(repos):
    _, bindings = repos
    bot = bindings.register_user_telegram_bot(
        owner_user_id="alice",
        bot_username="BYOBot",
        bot_token_ciphertext="ct",
    )
    b = bindings.create_thread_binding(
        thread_id="t1",
        provider="telegram",
        platform_chat_id="555",
        user_id="alice",
        user_telegram_bot_id=bot.id,
    )
    assert b.user_telegram_bot_id == bot.id

    by_thread = bindings.lookup_thread_binding_by_thread("telegram", "t1")
    assert by_thread.user_telegram_bot_id == bot.id


# -- switch binding --------------------------------------------------------


def test_switch_thread_binding_for_chat(repos):
    _, bindings = repos
    bindings.create_thread_binding(
        thread_id="old-thread",
        provider="telegram",
        platform_chat_id="999",
        user_id="alice",
    )
    new_binding, previous = bindings.switch_thread_binding_for_chat(
        thread_id="new-thread",
        provider="telegram",
        platform_chat_id="999",
        user_id="alice",
    )
    assert new_binding.thread_id == "new-thread"
    assert previous == "old-thread"
    assert bindings.lookup_thread_binding_by_thread("telegram", "old-thread") is None
    assert bindings.lookup_thread_binding_by_chat("telegram", "999").thread_id == "new-thread"


# -- cross-repo FK cascade ------------------------------------------------


def test_user_cascade_deletes_bindings(repos):
    accounts, bindings = repos
    bindings.create_thread_binding(
        thread_id="t1", provider="telegram", platform_chat_id="111", user_id="alice"
    )
    bindings.issue_bind_code(
        kind="thread_bind", provider="telegram", user_id="alice", thread_id="t1"
    )
    accounts.delete_user_cascade("alice")
    assert bindings.lookup_thread_binding_by_thread("telegram", "t1") is None
    assert bindings.list_thread_bindings_for_user("alice") == []


# -- inspect_bind_code (CHATAPP-001 regression) ----------------------------


def test_inspect_does_not_consume_code(repos):
    """inspect_bind_code returns claim info without consuming the code."""
    _, bindings = repos
    raw = bindings.issue_bind_code(
        kind="thread_bind", provider="telegram", user_id="alice", thread_id="t1"
    )
    claim = bindings.inspect_bind_code(raw, kind="thread_bind", provider="telegram")
    assert claim.user_id == "alice"
    assert claim.thread_id == "t1"
    # Code is still usable — claim should succeed.
    claim2 = bindings.claim_bind_code(raw, kind="thread_bind", provider="telegram")
    assert claim2.user_id == "alice"


def test_inspect_rejects_expired(repos):
    _, bindings = repos
    raw = bindings.issue_bind_code(
        kind="thread_bind",
        provider="telegram",
        user_id="alice",
        thread_id="t1",
        ttl_seconds=-1,
    )
    with pytest.raises(BindCodeInvalid, match="expired"):
        bindings.inspect_bind_code(raw, kind="thread_bind", provider="telegram")


def test_inspect_rejects_already_consumed(repos):
    _, bindings = repos
    raw = bindings.issue_bind_code(
        kind="thread_bind", provider="telegram", user_id="alice", thread_id="t1"
    )
    bindings.claim_bind_code(raw, kind="thread_bind", provider="telegram")
    with pytest.raises(BindCodeInvalid, match="already used"):
        bindings.inspect_bind_code(raw, kind="thread_bind", provider="telegram")


def test_inspect_rejects_wrong_kind(repos):
    _, bindings = repos
    raw = bindings.issue_bind_code(
        kind="thread_bind", provider="telegram", user_id="alice", thread_id="t1"
    )
    with pytest.raises(BindCodeInvalid):
        bindings.inspect_bind_code(raw, kind="platform_link", provider="telegram")


def test_inspect_rejects_unknown(repos):
    _, bindings = repos
    with pytest.raises(BindCodeInvalid, match="unknown"):
        bindings.inspect_bind_code("ZZZZZZZZ", kind="thread_bind", provider="telegram")


def test_inspect_rejects_empty(repos):
    _, bindings = repos
    with pytest.raises(BindCodeInvalid, match="empty"):
        bindings.inspect_bind_code("", kind="thread_bind", provider="telegram")


def test_inspect_then_failed_auth_leaves_code_usable(repos):
    """Core regression: failed authorization after inspect must not burn the code."""
    accounts, bindings = repos
    raw = bindings.issue_bind_code(
        kind="thread_bind", provider="telegram", user_id="alice", thread_id="t1"
    )
    # Simulate the endpoint pattern: inspect → auth fails → code survives.
    claim = bindings.inspect_bind_code(raw, kind="thread_bind", provider="telegram")
    assert claim.user_id == "alice"
    # Auth check fails (wrong user) — endpoint would raise 403 here.
    # The code must NOT be consumed.

    # Alice retries with correct credentials — should still work.
    claim2 = bindings.claim_bind_code(raw, kind="thread_bind", provider="telegram")
    assert claim2.user_id == "alice"
    assert claim2.thread_id == "t1"


def test_inspect_platform_link_code(repos):
    _, bindings = repos
    raw = bindings.issue_bind_code(
        kind="platform_link", provider="telegram", user_id="bob"
    )
    claim = bindings.inspect_bind_code(raw, kind="platform_link", provider="telegram")
    assert claim.kind == "platform_link"
    assert claim.user_id == "bob"
    assert claim.thread_id is None
    # Still claimable.
    claim2 = bindings.claim_bind_code(raw, kind="platform_link", provider="telegram")
    assert claim2.user_id == "bob"


def test_concurrent_inspect_then_claim_race(repos):
    """If two callers inspect the same code, only one claim succeeds."""
    _, bindings = repos
    raw = bindings.issue_bind_code(
        kind="thread_bind", provider="telegram", user_id="alice", thread_id="t1"
    )
    # Both inspect succeed (read-only).
    c1 = bindings.inspect_bind_code(raw, kind="thread_bind", provider="telegram")
    c2 = bindings.inspect_bind_code(raw, kind="thread_bind", provider="telegram")
    assert c1.user_id == c2.user_id == "alice"
    # First claim wins.
    bindings.claim_bind_code(raw, kind="thread_bind", provider="telegram")
    # Second claim fails.
    with pytest.raises(BindCodeInvalid, match="already used"):
        bindings.claim_bind_code(raw, kind="thread_bind", provider="telegram")


# -- CHATAPP-001 endpoint-pattern regression: binding failure preserves code --


def test_binding_already_exists_leaves_code_usable(repos):
    """If create_thread_binding raises BindingAlreadyExists, the code must
    survive so the user can retry after resolving the conflict."""
    _, bindings = repos
    raw = bindings.issue_bind_code(
        kind="thread_bind", provider="telegram", user_id="alice", thread_id="t1"
    )
    bindings.create_thread_binding(
        thread_id="t1", provider="telegram", platform_chat_id="111", user_id="alice"
    )
    # Simulate the fixed endpoint pattern:
    # inspect → authz passes → create binding → BindingAlreadyExists → skip claim.
    claim = bindings.inspect_bind_code(raw, kind="thread_bind", provider="telegram")
    assert claim.thread_id == "t1"
    with pytest.raises(BindingAlreadyExists):
        bindings.create_thread_binding(
            thread_id="t1",
            provider="telegram",
            platform_chat_id="222",
            user_id="alice",
        )
    # Code was NOT consumed — it's still valid.
    claim2 = bindings.claim_bind_code(raw, kind="thread_bind", provider="telegram")
    assert claim2.user_id == "alice"


def test_wrong_platform_user_leaves_code_usable(repos):
    """Authorization failure (wrong platform user) must not burn the code.
    Simulates: alice issues code, bob (different Nymeria user) tries to claim."""
    accounts, bindings = repos
    accounts.link_platform("telegram", "alice_tg_id", "alice")
    accounts.link_platform("telegram", "bob_tg_id", "bob")
    raw = bindings.issue_bind_code(
        kind="thread_bind", provider="telegram", user_id="alice", thread_id="t1"
    )
    claim = bindings.inspect_bind_code(raw, kind="thread_bind", provider="telegram")
    assert claim.user_id == "alice"
    # Bob's platform id resolves to "bob", not "alice" — authz fails.
    resolved = accounts.resolve_platform("telegram", "bob_tg_id")
    assert resolved != claim.user_id
    # Endpoint would return 403 here without consuming the code.
    # Code is still valid for alice's correct attempt.
    claim2 = bindings.claim_bind_code(raw, kind="thread_bind", provider="telegram")
    assert claim2.user_id == "alice"


def test_wrong_bot_owner_leaves_code_usable(repos):
    """BYO bot path: bot owned by bob cannot claim alice's code."""
    _, bindings = repos
    bob_bot = bindings.register_user_telegram_bot(
        owner_user_id="bob",
        bot_username="bobs_bot",
        bot_token_ciphertext="encrypted_token",
    )
    raw = bindings.issue_bind_code(
        kind="thread_bind", provider="telegram", user_id="alice", thread_id="t1"
    )
    claim = bindings.inspect_bind_code(raw, kind="thread_bind", provider="telegram")
    assert claim.user_id == "alice"
    # Bob's bot doesn't match alice's code — authz fails.
    assert bob_bot.owner_user_id != claim.user_id
    # Code is still valid.
    claim2 = bindings.claim_bind_code(raw, kind="thread_bind", provider="telegram")
    assert claim2.user_id == "alice"


def test_platform_link_hijack_leaves_code_usable(repos):
    """If a platform user is already linked to a different account, the
    409 must not burn the link code."""
    accounts, bindings = repos
    accounts.link_platform("telegram", "tg_user_1", "bob")
    raw = bindings.issue_bind_code(
        kind="platform_link", provider="telegram", user_id="alice"
    )
    claim = bindings.inspect_bind_code(raw, kind="platform_link", provider="telegram")
    assert claim.user_id == "alice"
    # tg_user_1 is already linked to bob, not alice — hijack check fails.
    existing = accounts.resolve_platform("telegram", "tg_user_1")
    assert existing is not None and existing != claim.user_id
    # Code is still valid for a legitimate claim.
    claim2 = bindings.claim_bind_code(raw, kind="platform_link", provider="telegram")
    assert claim2.user_id == "alice"


def test_full_successful_bind_flow(repos):
    """Happy path: inspect → authz → bind → claim all succeed."""
    accounts, bindings = repos
    accounts.link_platform("telegram", "alice_tg", "alice")
    raw = bindings.issue_bind_code(
        kind="thread_bind", provider="telegram", user_id="alice", thread_id="t1"
    )
    claim = bindings.inspect_bind_code(raw, kind="thread_bind", provider="telegram")
    resolved = accounts.resolve_platform("telegram", "alice_tg")
    assert resolved == claim.user_id
    binding = bindings.create_thread_binding(
        thread_id="t1",
        provider="telegram",
        platform_chat_id="999",
        user_id=claim.user_id,
    )
    assert binding.thread_id == "t1"
    bindings.claim_bind_code(raw, kind="thread_bind", provider="telegram")
    # Code is now consumed.
    with pytest.raises(BindCodeInvalid, match="already used"):
        bindings.inspect_bind_code(raw, kind="thread_bind", provider="telegram")


def test_full_successful_link_flow(repos):
    """Happy path: inspect → authz → link → claim all succeed."""
    accounts, bindings = repos
    raw = bindings.issue_bind_code(
        kind="platform_link", provider="telegram", user_id="alice"
    )
    claim = bindings.inspect_bind_code(raw, kind="platform_link", provider="telegram")
    existing = accounts.resolve_platform("telegram", "new_tg_user")
    assert existing is None
    accounts.link_platform("telegram", "new_tg_user", claim.user_id)
    bindings.claim_bind_code(raw, kind="platform_link", provider="telegram")
    # Code is consumed, link exists.
    with pytest.raises(BindCodeInvalid, match="already used"):
        bindings.claim_bind_code(raw, kind="platform_link", provider="telegram")
    assert accounts.resolve_platform("telegram", "new_tg_user") == "alice"
