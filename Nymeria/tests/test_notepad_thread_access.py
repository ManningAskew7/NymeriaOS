"""A caller-supplied thread id cannot reach another user's notepad through the
command layer.

Found 2026-09-30 by the review of #101 entry 24: ``POST /commands/execute``
passes the client's ``thread_id`` through verbatim, and ``/notepad read``,
``write`` and ``clear`` called the ``thread_notes`` store directly with only a
presence check, so a non-admin read, rewrote and cleared another user's
notepad (the ``/memory limit`` readout leaked its size the same way). Every
command now goes through the access-checked client doors
(``get_thread_notepad`` / ``write_thread_notepad``), in-process and over
HTTP. Each refusal below is paired with the owner succeeding through the same
harness, so a refusal cannot pass for an incidental reason.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from cli_fixtures import run
from nymeria.core.command_service import (
    CommandBackendClient,
    CommandContext,
    CommandHttpClient,
    CommandService,
    _CommandBackendUser,
)
from nymeria.tools import thread_notes

THREAD = "alice-thread"
SECRET = "alice: the launch slips to May"


class _Accounts:
    """The thread_owners semantics the doors consult: claim is first-writer-
    wins, the owner read never inserts."""

    def __init__(self) -> None:
        self.owners = {THREAD: "alice"}

    def claim_thread(self, thread_id: str, user_id: str) -> str:
        return self.owners.setdefault(thread_id, user_id)

    def get_thread_owner(self, thread_id: str) -> str | None:
        return self.owners.get(thread_id)


@pytest.fixture
def store(monkeypatch, tmp_path):
    settings = SimpleNamespace(data_dir=tmp_path, memory_char_limit=100)
    monkeypatch.setattr("nymeria.config.get_settings", lambda: settings)
    monkeypatch.setattr(thread_notes, "_notes_dir", None)
    assert thread_notes.write_notepad(THREAD, SECRET, mode="replace").startswith("[Saved]")
    return settings


def _as(user_id: str, command: str, *, thread_id: str = THREAD, accounts=None):
    agent = SimpleNamespace(
        accounts_repo=accounts or _Accounts(),
        thread_config_manager=SimpleNamespace(get_config=lambda thread_id: None),
        profile_manager=SimpleNamespace(get_profile=lambda uid: SimpleNamespace(memories=[])),
    )
    client = CommandBackendClient(agent, user=_CommandBackendUser(id=user_id, role="user"))
    ctx = CommandContext(
        user_id=user_id, thread_id=thread_id, actor="user", surface="cli", is_admin=False
    )
    return run(CommandService().execute(ctx, command, api=client))


# -- in process (the API's own command path) --------------------------------------


def test_the_owner_reads_writes_and_clears_their_notepad(store):
    read = _as("alice", "/notepad read")
    assert read.success is True and SECRET in read.markdown
    assert f"({len(SECRET)} / 100 chars)" in read.markdown  # the door's size and limit

    assert _as("alice", "/notepad write more").success is True
    assert thread_notes.read_notepad(THREAD) == f"{SECRET}\n\nmore"

    assert _as("alice", "/notepad clear").success is True
    assert thread_notes.read_notepad(THREAD) is None


def test_another_user_cannot_read_the_notepad(store):
    result = _as("bob", "/notepad read")
    assert result.success is False
    assert SECRET not in result.markdown


@pytest.mark.parametrize(
    "command", ["/notepad write replace:pwned", "/notepad write pwned", "/notepad clear"]
)
def test_another_user_cannot_rewrite_or_clear_the_notepad(store, command):
    assert _as("bob", command).success is False
    assert thread_notes.read_notepad(THREAD) == SECRET


def test_status_shows_the_notepad_only_to_its_owner(store):
    size_line = f"notepad: {len(SECRET)} / 100 chars"
    assert size_line in _as("alice", "/status").markdown
    foreign = _as("bob", "/status")
    assert "notepad:" not in foreign.markdown


def test_the_memory_limit_readout_shows_the_thread_line_only_to_its_owner(store):
    owner = _as("alice", "/memory limit")
    assert f"thread: {len(SECRET)} / 100 chars (inherits global)" in owner.markdown
    foreign = _as("bob", "/memory limit")
    assert foreign.success is True and "global:" in foreign.markdown
    assert "thread:" not in foreign.markdown


def test_reading_an_ownerless_thread_claims_nothing_and_writing_claims_it(store):
    """The doors keep the platform's claim semantics: a read never registers
    a thread (a ghost "New Chat" per mistyped id), a write claims it for the
    writer on first touch (so the next user cannot claim-jack it)."""
    accounts = _Accounts()
    for command in ("/notepad read", "/status", "/memory limit"):
        assert _as("carol", command, thread_id="fresh-thread", accounts=accounts).success is True
    assert "fresh-thread" not in accounts.owners

    assert _as("carol", "/notepad write first", thread_id="fresh-thread", accounts=accounts).success
    assert accounts.owners["fresh-thread"] == "carol"
    refused = _as("dave", "/notepad read", thread_id="fresh-thread", accounts=accounts)
    assert refused.success is False and "first" not in refused.markdown


# -- over HTTP (CommandHttpClient against the real routes) --------------------------


@pytest.fixture
def http_as(store, tmp_path, api_client_builder):
    """``/notepad`` through CommandHttpClient over the real thread-config
    routes, as alice (the owner) or bob (another non-admin)."""
    from tests.test_api_thread_config_router import FakeAgent

    settings = api_client_builder.settings(tmp_path)
    agent = FakeAgent(tmp_path)
    client, alice_token = api_client_builder.authenticated_client(
        agent, settings, user_id="alice"
    )
    agent.accounts_repo.create_user("bob", "bob@example.com", "Bob", role="user")
    tokens = {"alice": alice_token, "bob": agent.accounts_repo.issue_token("bob")}
    agent.accounts_repo.claim_thread(THREAD, "alice")

    def execute(user_id: str, command: str):
        async def go():
            http = CommandHttpClient("http://api.test", tokens[user_id], use_act_as=False)
            http._client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=client.app), base_url="http://api.test"
            )
            try:
                ctx = CommandContext(
                    user_id=user_id, thread_id=THREAD, actor="user", surface="telegram",
                    is_admin=False,
                )
                return await CommandService().execute(ctx, command, api=http)
            finally:
                await http.aclose()

        return run(go())

    return execute


def test_over_http_the_owner_appends_and_a_refused_write_is_relayed(http_as):
    assert http_as("alice", "/notepad write more").success is True
    assert thread_notes.read_notepad(THREAD) == f"{SECRET}\n\nmore"  # appended, not replaced

    refused = http_as("alice", "/notepad write " + "x" * 120)
    assert refused.success is False
    assert "Thread memory is full" in refused.markdown and "`/memory limit" in refused.markdown
    assert thread_notes.read_notepad(THREAD) == f"{SECRET}\n\nmore"

    read = http_as("alice", "/notepad read")
    assert read.success is True and "more" in read.markdown

    assert http_as("alice", "/notepad clear").success is True
    assert http_as("alice", "/notepad clear").markdown.strip().endswith("Notepad was already empty.")


def test_over_http_another_user_is_refused(http_as):
    read = http_as("bob", "/notepad read")
    assert read.success is False and SECRET not in read.markdown
    assert http_as("bob", "/notepad write replace:pwned").success is False
    assert http_as("bob", "/notepad clear").success is False
    assert thread_notes.read_notepad(THREAD) == SECRET
