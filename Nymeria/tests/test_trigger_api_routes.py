"""REST-surface tests for the trigger routes: the #264 pause/resume verbs
and the #266 PATCH honesty.

``PATCH /triggers/{id}`` had no test at all before this file, which is part
of why #266 (a thread_id that vanished at the Pydantic parse) survived so
long.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException

from nymeria.core.accounts import AccountsRepo
from nymeria.core.trigger_manager import (
    TriggerAction,
    TriggerDefinition,
    TriggerManager,
)
from nymeria.triggers import trigger_api as trigger_api_module


class FakeAgent:
    def __init__(self, data_dir: Path):
        self.accounts_repo = AccountsRepo(data_dir / "accounts.db")
        self.thread_metadata_manager = SimpleNamespace(
            upsert_thread=lambda *args, **kwargs: None,
            delete_thread=lambda *args, **kwargs: None,
        )

    def sync_agent_tools(self):
        pass


def _client(tmp_path: Path, api_client_builder, monkeypatch):
    settings = api_client_builder.settings(tmp_path)
    settings.nymeria_service_token = None
    settings.api_port = 8000
    monkeypatch.setattr(trigger_api_module, "get_settings", lambda: settings)
    agent = FakeAgent(tmp_path)
    client = api_client_builder.client(agent, settings)
    agent.accounts_repo.create_user("owner", "owner@example.com", "Owner")
    token = agent.accounts_repo.issue_token("owner")
    return client, token, agent


def _insert_trigger(
    data_dir: Path,
    *,
    trigger_id: str = "trig1",
    user_id: str = "owner",
    thread_id: str = "thread-old",
    source_type: str = "webhook",
    source_config: dict | None = None,
    auto_paused_at: datetime | None = None,
    action_failures: int = 0,
    health_status: Any = "healthy",
    consecutive_errors: int = 0,
) -> None:
    manager = TriggerManager(data_dir)
    trigger = TriggerDefinition(
        id=trigger_id,
        name="Mail sweep",
        source_type=source_type,
        source_config=source_config if source_config is not None else {"secret": "s"},
        action=TriggerAction(
            type="agent_prompt", config={"prompt_template": "Handle {message}"}
        ),
        thread_id=thread_id,
        created_by="user",
        state={"trigger_id": trigger_id},
        auto_paused_at=auto_paused_at,
        action_failures=action_failures,
        health_status=health_status,
        consecutive_errors=consecutive_errors,
    )
    with manager.atomic_update(user_id) as store:
        store.triggers.append(trigger)


# --- #266: the PATCH capability and the PATCH honesty --------------------


def test_patch_repoints_a_trigger_at_a_different_thread(
    tmp_path: Path, api_client_builder, monkeypatch
):
    """#266 behavior 16. Re-pointing is the natural repair when a bound
    thread dies or is branched; before this the only routes were
    delete-and-recreate or hand-editing the store."""
    client, token, _agent = _client(tmp_path, api_client_builder, monkeypatch)
    _insert_trigger(tmp_path, thread_id="thread-old")

    response = client.patch(
        "/triggers/trig1",
        json={"thread_id": "thread-new"},
        headers=api_client_builder.auth(token),
    )

    assert response.status_code == 200, response.text
    assert response.json()["thread_id"] == "thread-new"
    stored = TriggerManager(tmp_path).get_trigger("owner", "trig1")
    assert stored is not None
    assert stored.thread_id == "thread-new"


def test_patch_rejects_an_unknown_key_instead_of_dropping_it(
    tmp_path: Path, api_client_builder, monkeypatch
):
    """#266 behavior 17. extra="ignore" dropped unknown keys at the parse,
    so the caller got a 200 and a full echo for a write that never
    happened."""
    client, token, _agent = _client(tmp_path, api_client_builder, monkeypatch)
    _insert_trigger(tmp_path)

    response = client.patch(
        "/triggers/trig1",
        json={"souce_type": "rss"},  # typo for source_type
        headers=api_client_builder.auth(token),
    )

    assert response.status_code == 422, response.text
    assert "souce_type" in response.text


def test_patch_applies_nothing_when_an_unknown_key_rides_along(
    tmp_path: Path, api_client_builder, monkeypatch
):
    """#266 behavior 17, the reported shape: the unknown key travelling with
    a recognized one is what produced a silent 200. The valid half must NOT
    land either, or the caller is told 'failed' about a partial write."""
    client, token, _agent = _client(tmp_path, api_client_builder, monkeypatch)
    _insert_trigger(tmp_path)

    response = client.patch(
        "/triggers/trig1",
        json={"name": "Renamed", "not_a_field": "x"},
        headers=api_client_builder.auth(token),
    )

    assert response.status_code == 422, response.text
    stored = TriggerManager(tmp_path).get_trigger("owner", "trig1")
    assert stored is not None
    assert stored.name == "Mail sweep"  # the valid half did not land


def test_patch_repoint_is_gated_by_the_same_thread_access_check_as_create(
    tmp_path: Path, api_client_builder, monkeypatch
):
    """Re-pointing must not be the create route's thread-access gate in two
    steps: create on a thread you own, then PATCH onto a guessed
    ``discord_<g>_<c>`` id and fire it through the unauthenticated webhook
    route. The gate's own comment on create_trigger describes that attack.
    """
    from nymeria.triggers import api as triggers_api_module

    checked: list[str] = []

    def _deny(user, thread_id: str, **kwargs) -> None:
        checked.append(thread_id)
        raise HTTPException(status_code=403, detail="Thread not yours")

    # Patch BEFORE the app is built: create_api_app passes this module-level
    # function into create_trigger_router, so the router closes over whatever
    # it resolves to at build time.
    monkeypatch.setattr(triggers_api_module, "_require_thread_access", _deny)

    client, token, _agent = _client(tmp_path, api_client_builder, monkeypatch)
    _insert_trigger(tmp_path, thread_id="thread-mine")

    response = client.patch(
        "/triggers/trig1",
        json={"thread_id": "discord_999_888"},
        headers=api_client_builder.auth(token),
    )

    assert response.status_code == 403, response.text
    assert checked == ["discord_999_888"]  # the route consulted the guard
    stored = TriggerManager(tmp_path).get_trigger("owner", "trig1")
    assert stored is not None
    assert stored.thread_id == "thread-mine"  # unmoved


def test_deleting_a_repointed_trigger_keeps_the_bound_thread_metadata(
    tmp_path: Path, api_client_builder, monkeypatch
):
    """#266 made re-pointing the RECOMMENDED repair, which walked into a
    pre-existing hazard: delete_trigger cleaned up "its" thread metadata
    unconditionally, so deleting a trigger you had re-pointed onto a real
    chat thread stripped that thread's title/platform/pin row.

    Cleanup now applies only to the throwaway `trigger-<uuid>` thread the
    trigger made for itself.
    """
    deleted: list[str] = []

    client, token, _agent = _client(tmp_path, api_client_builder, monkeypatch)
    # The app's agent is the FakeAgent built in _client; swap its metadata
    # manager for one that records deletions.
    _agent.thread_metadata_manager = SimpleNamespace(
        upsert_thread=lambda *a, **kw: None,
        delete_thread=lambda user_id, thread_id: deleted.append(thread_id),
    )

    _insert_trigger(tmp_path, trigger_id="bound", thread_id="telegram_12345")
    _insert_trigger(tmp_path, trigger_id="own", thread_id="trigger-abc-123")

    assert client.delete(
        "/triggers/bound", headers=api_client_builder.auth(token)
    ).status_code == 204
    assert deleted == []  # a thread the trigger did not own is left alone

    assert client.delete(
        "/triggers/own", headers=api_client_builder.auth(token)
    ).status_code == 204
    assert deleted == ["trigger-abc-123"]  # its own throwaway thread still goes


def test_patch_rejects_an_empty_thread_id(
    tmp_path: Path, api_client_builder, monkeypatch
):
    """An empty string skipped the access check and then got silently
    re-assigned a fresh `trigger-<uuid>` by the store's backfill, orphaning
    whatever the trigger was bound to."""
    client, token, _agent = _client(tmp_path, api_client_builder, monkeypatch)
    _insert_trigger(tmp_path, thread_id="thread-mine")

    response = client.patch(
        "/triggers/trig1",
        json={"thread_id": "   "},
        headers=api_client_builder.auth(token),
    )

    assert response.status_code == 400, response.text
    stored = TriggerManager(tmp_path).get_trigger("owner", "trig1")
    assert stored is not None
    assert stored.thread_id == "thread-mine"


# --- #264: pause enforcement and the resume verb -------------------------


def test_webhook_fire_refuses_an_auto_paused_trigger(
    tmp_path: Path, api_client_builder, monkeypatch
):
    """#264 behavior 4. A webhook fire is autonomous action on an external
    clock, so the pause governs it exactly as it governs the poll loop."""
    client, token, _agent = _client(tmp_path, api_client_builder, monkeypatch)
    _insert_trigger(
        tmp_path,
        source_config={"secret": "shared"},
        auto_paused_at=datetime(2026, 8, 31, 9, 0, tzinfo=timezone.utc),
        action_failures=5,
    )

    response = client.post(
        "/triggers/fire/trig1?user_id=owner",
        json={"message": "hello"},
        headers=api_client_builder.auth(token),
    )

    assert response.status_code == 409, response.text
    body = response.text
    assert "paused" in body
    assert "resume" in body.lower()  # the refusal says how to undo it


def test_test_route_still_works_on_a_paused_trigger(
    tmp_path: Path, api_client_builder, monkeypatch
):
    """#264 behavior 5. The test route is a dry run that executes no action
    and touches no health, and it is how an operator verifies a repair
    BEFORE resuming. Gating it would make the repair loop untestable."""
    client, token, _agent = _client(tmp_path, api_client_builder, monkeypatch)
    _insert_trigger(
        tmp_path,
        source_config={"secret": "shared"},
        auto_paused_at=datetime(2026, 8, 31, 9, 0, tzinfo=timezone.utc),
        action_failures=5,
    )

    response = client.post(
        "/triggers/trig1/test?user_id=owner",
        headers=api_client_builder.auth(token),
    )

    assert response.status_code == 200, response.text
    assert "rendered_output" in response.json()


def test_resume_route_clears_the_pause_and_the_counters(
    tmp_path: Path, api_client_builder, monkeypatch
):
    """#264 behavior 12."""
    client, token, _agent = _client(tmp_path, api_client_builder, monkeypatch)
    _insert_trigger(
        tmp_path,
        auto_paused_at=datetime(2026, 8, 31, 9, 0, tzinfo=timezone.utc),
        action_failures=5,
        consecutive_errors=7,
        health_status="failing",
    )

    response = client.post(
        "/triggers/trig1/resume",
        headers=api_client_builder.auth(token),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["auto_paused_at"] is None
    assert body["action_failures"] == 0
    assert body["health_status"] == "healthy"
    assert body["consecutive_errors"] == 0

    stored = TriggerManager(tmp_path).get_trigger("owner", "trig1")
    assert stored is not None
    assert stored.auto_paused_at is None
    assert stored.last_error is None


def test_resume_route_answers_404_for_an_unknown_trigger(
    tmp_path: Path, api_client_builder, monkeypatch
):
    """#264 behavior 14."""
    client, token, _agent = _client(tmp_path, api_client_builder, monkeypatch)

    response = client.post(
        "/triggers/nope/resume",
        headers=api_client_builder.auth(token),
    )

    assert response.status_code == 404, response.text


def test_trigger_listing_exposes_the_paused_state(
    tmp_path: Path, api_client_builder, monkeypatch
):
    """#264 behavior 19 on the REST surface. `enabled` alone no longer says
    whether a trigger runs, so a client rendering only the toggle would
    misreport a stopped trigger."""
    client, token, _agent = _client(tmp_path, api_client_builder, monkeypatch)
    _insert_trigger(
        tmp_path,
        auto_paused_at=datetime(2026, 8, 31, 9, 0, tzinfo=timezone.utc),
        action_failures=5,
    )

    response = client.get(
        "/triggers", headers=api_client_builder.auth(token)
    )

    assert response.status_code == 200, response.text
    row = response.json()[0]
    assert row["enabled"] is True  # the policy never touched the toggle
    assert row["auto_paused_at"] is not None
    assert row["action_failures"] == 5
