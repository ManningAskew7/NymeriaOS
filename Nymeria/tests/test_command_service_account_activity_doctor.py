"""Tests for /account, /activity, and /doctor backend commands (Phase 2a.4)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from cli_fixtures import run
import nymeria.core.agent as agent_module
from nymeria.core.command_service import CommandContext, CommandService


class _FakeUser(SimpleNamespace):
    pass


class _FakeAccountsRepo:
    def __init__(self) -> None:
        self.users: dict[str, _FakeUser] = {}
        self.tokens: dict[str, list[Any]] = {}
        self.platforms: dict[str, list[Any]] = {}
        self.issued_tokens: list[tuple[str, str | None]] = []
        self.revoked_tokens: list[tuple[str, str]] = []
        self.next_token = "tok_secret_value"

    def get_user_by_id(self, user_id: str):
        return self.users.get(user_id)

    def list_tokens_for_user(self, user_id: str) -> list[Any]:
        return list(self.tokens.get(user_id, []))

    def issue_token(self, user_id: str, label: str | None = None) -> str:
        self.issued_tokens.append((user_id, label))
        return self.next_token

    def revoke_token(self, user_id: str, prefix: str) -> bool:
        existing = self.tokens.get(user_id, [])
        before = len(existing)
        self.tokens[user_id] = [
            t for t in existing if getattr(t, "token_hash_prefix", "") != prefix
        ]
        revoked = len(self.tokens[user_id]) < before
        if revoked:
            self.revoked_tokens.append((user_id, prefix))
        return revoked

    def list_platforms_for_user(self, user_id: str) -> list[Any]:
        return list(self.platforms.get(user_id, []))


class _SettingsApi:
    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        self._settings = settings or {}

    async def close(self) -> None:
        return None

    async def get_settings(self, user_id: str | None = None) -> dict[str, Any]:
        return dict(self._settings)


def _ctx(thread_id: str | None = "thread-1") -> CommandContext:
    return CommandContext(
        user_id="alice",
        thread_id=thread_id or "",
        actor="user",
        surface="cli",
        is_admin=True,
    )


@pytest.fixture(autouse=True)
def patched_agent(monkeypatch: pytest.MonkeyPatch):
    def install(agent) -> Any:
        monkeypatch.setattr(agent_module, "get_current_agent", lambda: agent)
        return agent

    install(SimpleNamespace(accounts_repo=_FakeAccountsRepo()))
    return install


# ── Account commands ──────────────────────────────────────────────────────


def test_account_current_renders_user_metadata(patched_agent) -> None:
    repo = _FakeAccountsRepo()
    repo.users["alice"] = _FakeUser(
        id="alice",
        email="alice@example.test",
        display_name="Alice",
        role="user",
    )
    patched_agent(SimpleNamespace(accounts_repo=repo))

    result = run(CommandService().execute(_ctx(), "/account current"))
    assert result.success is True, result.markdown
    assert "alice@example.test" in result.markdown
    assert "Alice" in result.markdown


def test_account_current_reports_missing_user(patched_agent) -> None:
    patched_agent(SimpleNamespace(accounts_repo=_FakeAccountsRepo()))

    result = run(CommandService().execute(_ctx(), "/account current"))
    assert result.success is False
    assert "not found" in result.markdown


def test_account_tokens_lists(patched_agent) -> None:
    repo = _FakeAccountsRepo()
    repo.tokens["alice"] = [
        SimpleNamespace(
            token_hash_prefix="abcd1234",
            label="laptop",
            created_at="2026-05-01",
            last_used_at="2026-05-17",
            revoked_at="",
        )
    ]
    patched_agent(SimpleNamespace(accounts_repo=repo))

    result = run(CommandService().execute(_ctx(), "/account tokens"))
    assert result.success is True, result.markdown
    assert "abcd1234" in result.markdown
    assert "laptop" in result.markdown


def test_account_tokens_issue_returns_raw_token_once(patched_agent) -> None:
    repo = _FakeAccountsRepo()
    repo.next_token = "tok_brand_new_secret"
    patched_agent(SimpleNamespace(accounts_repo=repo))

    result = run(CommandService().execute(_ctx(), "/account tokens issue cli-laptop"))
    assert result.success is True, result.markdown
    assert "tok_brand_new_secret" in result.markdown
    assert repo.issued_tokens == [("alice", "cli-laptop")]


def test_account_tokens_revoke_calls_repo(patched_agent) -> None:
    repo = _FakeAccountsRepo()
    repo.tokens["alice"] = [
        SimpleNamespace(token_hash_prefix="abcd1234"),
    ]
    patched_agent(SimpleNamespace(accounts_repo=repo))

    result = run(CommandService().execute(_ctx(), "/account tokens revoke abcd1234"))
    assert result.success is True, result.markdown
    assert repo.revoked_tokens == [("alice", "abcd1234")]


def test_account_tokens_revoke_reports_unknown(patched_agent) -> None:
    patched_agent(SimpleNamespace(accounts_repo=_FakeAccountsRepo()))

    result = run(CommandService().execute(_ctx(), "/account tokens revoke missing"))
    assert result.success is False
    assert "No matching token" in result.markdown


def test_account_platforms_lists_linked_chat_identities(patched_agent) -> None:
    repo = _FakeAccountsRepo()
    repo.platforms["alice"] = [
        SimpleNamespace(provider="telegram", provider_user_id="123", created_at="2026-05-01"),
        SimpleNamespace(provider="discord", provider_user_id="abc", created_at="2026-05-02"),
    ]
    patched_agent(SimpleNamespace(accounts_repo=repo))

    result = run(CommandService().execute(_ctx(), "/account platforms"))
    assert result.success is True, result.markdown
    assert "telegram" in result.markdown
    assert "discord" in result.markdown


# ── Activity commands ─────────────────────────────────────────────────────


def test_activity_list_renders_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    from nymeria.core.activity_log import ActivityType

    class _FakeEntry:
        def __init__(self, message: str) -> None:
            self.id = "entry-1"
            self.timestamp = "2026-05-18T10:00:00"
            self.type = ActivityType.USER_MESSAGE
            self.message = message
            self.thread_id = "thread-1"
            self.metadata = {}

    class _FakeLog:
        def get_entries(self, user_id, limit, activity_type, thread_id):
            return [_FakeEntry("said hi"), _FakeEntry("said bye")]

    monkeypatch.setattr(
        "nymeria.core.activity_log.get_activity_log",
        lambda: _FakeLog(),
    )

    result = run(CommandService().execute(_ctx(), "/activity list"))
    assert result.success is True, result.markdown
    assert "said hi" in result.markdown
    assert "said bye" in result.markdown
    assert "| Time | Type | Thread | Message |" in result.markdown


def test_activity_list_rejects_unknown_type(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeLog:
        def get_entries(self, *a, **k):
            return []

    monkeypatch.setattr(
        "nymeria.core.activity_log.get_activity_log",
        lambda: _FakeLog(),
    )

    result = run(
        CommandService().execute(_ctx(), "/activity list --type bogus_type")
    )
    assert result.success is False
    assert "Invalid activity type" in result.markdown


def test_activity_notifications_renders_unread_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeNotification:
        def __init__(self, nid: str, summary: str, read: bool) -> None:
            self.id = nid
            self.summary = summary
            self.read = read

    class _FakeStore:
        def get_all(self, user_id, limit):
            return [
                _FakeNotification("n1", "Build complete", read=False),
                _FakeNotification("n2", "Reminder ping", read=True),
            ]

        def get_unread_count(self, user_id):
            return 1

    monkeypatch.setattr(
        "nymeria.core.notifications.get_notification_store",
        lambda: _FakeStore(),
    )

    result = run(CommandService().execute(_ctx(), "/activity notifications"))
    assert result.success is True, result.markdown
    assert "Build complete" in result.markdown
    assert "(1 unread)" in result.markdown


# ── Doctor commands ───────────────────────────────────────────────────────


def test_doctor_auth_shows_identity(patched_agent) -> None:
    repo = _FakeAccountsRepo()
    repo.users["alice"] = _FakeUser(
        id="alice", display_name="Alice", role="admin", email=""
    )
    patched_agent(SimpleNamespace(accounts_repo=repo))

    result = run(CommandService().execute(_ctx(), "/doctor auth"))
    assert result.success is True, result.markdown
    assert "Alice" in result.markdown
    assert "admin" in result.markdown


def test_doctor_model_renders_provider_and_model() -> None:
    api = _SettingsApi({
        "llm_provider": "anthropic",
        "llm_model": "claude-opus-4-7",
        "llm_base_url": "http://cli-proxy-api:8317",
    })

    result = run(CommandService().execute(_ctx(), "/doctor model", api=api))
    assert result.success is True, result.markdown
    assert "anthropic" in result.markdown
    assert "claude-opus-4-7" in result.markdown


def test_doctor_root_combines_auth_and_model(patched_agent) -> None:
    repo = _FakeAccountsRepo()
    repo.users["alice"] = _FakeUser(id="alice", display_name="Alice", role="user", email="")
    patched_agent(SimpleNamespace(accounts_repo=repo))
    api = _SettingsApi({"llm_provider": "openai", "llm_model": "gpt-5.5", "llm_base_url": ""})

    result = run(CommandService().execute(_ctx(), "/doctor", api=api))
    assert result.success is True, result.markdown
    assert "Auth" in result.markdown
    assert "Model" in result.markdown
    assert "openai" in result.markdown
    # The composed root must not leak a raw sentinel mid-body: composing
    # two prefixed halves used to print a literal "[Info]:" before the
    # Model section (#132 survey, the one unprefixed-return bug).
    assert "[Info]:" not in result.markdown


# ── #129 wave 2b: declared params ─────────────────────────────────────────


def test_account_root_rejects_an_unknown_verb_with_family_guidance(
    patched_agent,
) -> None:
    patched_agent(SimpleNamespace(accounts_repo=_FakeAccountsRepo()))

    # The root declares no arguments, so a stray verb is a binder rejection
    # and the dispatcher layers the family guidance on top.
    unknown = run(CommandService().execute(_ctx(), "/account bogus"))
    assert unknown.success is False
    assert "Unexpected argument `bogus`" in unknown.markdown
    assert "Valid subcommands: current, platforms, tokens." in unknown.markdown
    assert "Usage: `/account`." in unknown.markdown

    # A second token stops at the same rejection, on the first extra word.
    extra = run(CommandService().execute(_ctx(), "/account bogus more"))
    assert extra.success is False
    assert "Unexpected argument `bogus`" in extra.markdown
    assert "Valid subcommands: current, platforms, tokens." in extra.markdown


def test_account_tokens_revoke_without_a_prefix_revokes_nothing(
    patched_agent,
) -> None:
    repo = _FakeAccountsRepo()
    repo.tokens["alice"] = [SimpleNamespace(token_hash_prefix="abcd1234")]
    patched_agent(SimpleNamespace(accounts_repo=repo))

    result = run(CommandService().execute(_ctx(), "/account tokens revoke"))

    assert result.success is False
    assert "Missing required argument: hash-prefix" in result.markdown
    assert "Usage: `/account tokens revoke <hash-prefix>`" in result.markdown
    assert repo.revoked_tokens == []


def test_account_tokens_issue_joins_a_multi_word_label(patched_agent) -> None:
    repo = _FakeAccountsRepo()
    patched_agent(SimpleNamespace(accounts_repo=repo))

    result = run(CommandService().execute(_ctx(), "/account tokens issue work laptop"))

    assert result.success is True, result.markdown
    assert repo.issued_tokens == [("alice", "work laptop")]


def test_account_current_rejects_stray_arguments(patched_agent) -> None:
    repo = _FakeAccountsRepo()
    repo.users["alice"] = _FakeUser(
        id="alice", email="", display_name="Alice", role="user"
    )
    patched_agent(SimpleNamespace(accounts_repo=repo))

    result = run(CommandService().execute(_ctx(), "/account current verbose"))

    assert result.success is False
    assert "Unexpected argument `verbose`" in result.markdown
    assert "Alice" not in result.markdown


class _RecordingActivityLog:
    def __init__(self) -> None:
        self.queries: list[dict[str, Any]] = []

    def get_entries(self, user_id, limit, activity_type, thread_id):
        self.queries.append(
            {
                "user_id": user_id,
                "limit": limit,
                "activity_type": activity_type,
                "thread_id": thread_id,
            }
        )
        return []


def test_activity_list_binds_limit_and_both_option_spellings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nymeria.core.activity_log import ActivityType

    log = _RecordingActivityLog()
    monkeypatch.setattr("nymeria.core.activity_log.get_activity_log", lambda: log)

    result = run(
        CommandService().execute(
            _ctx(),
            "/activity list 5 --type=user_message --thread current",
        )
    )

    assert result.success is True, result.markdown
    assert log.queries == [
        {
            "user_id": "alice",
            "limit": 5,
            "activity_type": ActivityType.USER_MESSAGE,
            "thread_id": "thread-1",
        }
    ]


def test_activity_recent_alias_survives_the_root_flip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``recent`` was a handler-side synonym; it is a whole-path alias now.

    The root takes no arguments any more, so the alias is what keeps
    ``/activity recent`` working, and it has to bind ``activity list``'s own
    schema (limit and options), not just resolve to the bare listing.
    """
    from nymeria.core.activity_log import ActivityType

    service = CommandService()
    resolved = service.find_command("activity recent")
    assert resolved is not None
    assert resolved.name == "activity list"

    log = _RecordingActivityLog()
    monkeypatch.setattr("nymeria.core.activity_log.get_activity_log", lambda: log)

    result = run(
        service.execute(_ctx(), "/activity recent 3 --type user_message")
    )

    assert result.success is True, result.markdown
    assert log.queries == [
        {
            "user_id": "alice",
            "limit": 3,
            "activity_type": ActivityType.USER_MESSAGE,
            "thread_id": None,
        }
    ]


def test_activity_list_rejects_a_non_integer_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = _RecordingActivityLog()
    monkeypatch.setattr("nymeria.core.activity_log.get_activity_log", lambda: log)

    result = run(CommandService().execute(_ctx(), "/activity list lots"))

    assert result.success is False
    assert "limit must be an integer, got `lots`" in result.markdown
    assert log.queries == []


def test_activity_list_rejects_an_unknown_option(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = _RecordingActivityLog()
    monkeypatch.setattr("nymeria.core.activity_log.get_activity_log", lambda: log)

    result = run(CommandService().execute(_ctx(), "/activity list --kind chat"))

    assert result.success is False
    assert "Unknown option `--kind`" in result.markdown
    assert "Usage: `/activity list [limit] [--type TYPE] [--thread ID]`" in result.markdown
    assert log.queries == []


def test_doctor_rejects_an_unknown_section(patched_agent) -> None:
    patched_agent(SimpleNamespace(accounts_repo=_FakeAccountsRepo()))

    result = run(CommandService().execute(_ctx(), "/doctor network"))

    assert result.success is False
    assert "Unexpected argument `network`" in result.markdown
    assert "Valid subcommands: auth, model." in result.markdown
    assert "Usage: `/doctor`." in result.markdown
    # The combined report never ran.
    assert "Auth" not in result.markdown
