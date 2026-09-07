"""The in-process command backend refuses non-canonical NEW thread ids.

``CommandBackendClient`` is the twin of the REST thread-access gate for the
CLI, bots and MCP command surfaces. Its write door (``_require_thread_access``
with ``claim=True``) and ``create_thread`` are creation points for a
client-chosen thread id, so both must refuse a non-canonical id with the
same 400 + detail the REST layer returns, leaving no owner row and no
metadata behind. Reads and grandfathered threads are never refused. Spec:
``tmp/keep/safe-path-segment-spec.md`` behaviors 2, 3 and 6.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from cli_fixtures import run
from nymeria.core.accounts import AccountsRepo
from nymeria.core.command_service import CommandBackendClient, _CommandBackendUser
from nymeria.core.thread_metadata import ThreadMetadataManager


def _backend(tmp_path: Path, *, role: str = "user"):
    repo = AccountsRepo(tmp_path / "accounts.db")
    repo.create_user("alice", "alice@example.com", "Alice", role=role)
    agent = SimpleNamespace(
        accounts_repo=repo,
        thread_metadata_manager=ThreadMetadataManager(tmp_path),
        thread_config_manager=None,
    )
    return CommandBackendClient(agent, user=_CommandBackendUser(id="alice", role=role)), agent


def _detail(excinfo: pytest.ExceptionInfo[httpx.HTTPStatusError]) -> str:
    return excinfo.value.response.json()["detail"]


@pytest.mark.parametrize("role", ["user", "admin"])
def test_write_door_refuses_non_canonical_new_thread(tmp_path: Path, role: str) -> None:
    client, agent = _backend(tmp_path, role=role)

    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        client._require_thread_access("qa-collide/1")

    assert excinfo.value.response.status_code == 400
    assert "stored as 'qa-collide1'" in _detail(excinfo)
    assert "letters, digits, '-' and '_'" in _detail(excinfo)
    assert agent.accounts_repo.get_thread_owner("qa-collide/1") is None

    # A read-only door is never refused, and a canonical id claims as before.
    client._require_thread_access("qa-collide/1", claim=False)
    client._require_thread_access("qa-collide1")
    assert agent.accounts_repo.get_thread_owner("qa-collide1") == "alice"


def test_create_thread_refuses_non_canonical_id_before_writing_metadata(
    tmp_path: Path,
) -> None:
    client, agent = _backend(tmp_path)

    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        run(client.create_thread("alice", thread_id="qa-collide/1", title="Draft"))

    assert excinfo.value.response.status_code == 400
    assert "stored as 'qa-collide1'" in _detail(excinfo)
    assert "qa-collide/1" not in agent.thread_metadata_manager.get_store("alice").threads
    assert agent.accounts_repo.get_thread_owner("qa-collide/1") is None

    created = run(client.create_thread("alice", thread_id="qa-collide1", title="Draft"))
    assert created["thread_id"] == "qa-collide1"
    assert created["title"] == "Draft"
    assert agent.accounts_repo.get_thread_owner("qa-collide1") == "alice"


def test_grandfathered_non_canonical_thread_passes_the_write_door(tmp_path: Path) -> None:
    client, agent = _backend(tmp_path)
    agent.accounts_repo.backfill_threads(["legacy.thread"], "alice")

    client._require_thread_access("legacy.thread")

    assert agent.accounts_repo.get_thread_owner("legacy.thread") == "alice"


def test_create_thread_with_someone_elses_thread_id_is_404_for_non_admin(
    tmp_path: Path,
) -> None:
    """The CLI startup ``--thread <id>`` path lands here; it must match the
    REST ``/claim`` contract (404, nothing written) instead of upserting the
    other user's thread into the caller's own metadata store."""
    client, agent = _backend(tmp_path)
    agent.accounts_repo.create_user("bob", "bob@example.com", "Bob")
    agent.accounts_repo.claim_thread("bobs-thread", "bob")

    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        run(client.create_thread("alice", thread_id="bobs-thread", title="Mine"))

    assert excinfo.value.response.status_code == 404
    assert _detail(excinfo) == "Not found"
    assert "bobs-thread" not in agent.thread_metadata_manager.get_store("alice").threads
    assert agent.accounts_repo.get_thread_owner("bobs-thread") == "bob"


def test_admin_create_thread_on_someone_elses_id_does_not_steal_ownership(
    tmp_path: Path,
) -> None:
    client, agent = _backend(tmp_path, role="admin")
    agent.accounts_repo.create_user("bob", "bob@example.com", "Bob")
    agent.accounts_repo.claim_thread("bobs-thread", "bob")

    created = run(client.create_thread("alice", thread_id="bobs-thread"))

    assert created["thread_id"] == "bobs-thread"
    assert agent.accounts_repo.get_thread_owner("bobs-thread") == "bob"
