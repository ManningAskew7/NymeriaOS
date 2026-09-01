"""Trigger source_config secrets are fingerprinted on every read surface (#307).

A webhook trigger's `secret` IS the credential authenticating its public
`POST /triggers/fire/{id}` route, and every read surface used to print it in
full: the agent's `trigger_info` detail view (so into the transcript,
checkpoints, compaction summaries and the RAG index), the REST read model, and
both GUIs. Filed after a live agent probe read one back and flagged it
unprompted.

Two properties matter and are tested separately here:

1. NOTHING renders a secret in full, and the rule is driven by each source's
   own `secret: true` schema flag rather than a key-name guess.
2. Masking the read model must not corrupt the stored value. Both GUI wizards
   prefill an edit form from the read model and POST the whole dict back, so
   the fingerprint has to survive a round trip. The end-to-end test here fires
   the real webhook after a mask-echoing PATCH, because that is the only proof
   that the credential still works.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from nymeria.core.accounts import AccountsRepo
from nymeria.core.trigger_manager import (
    TriggerAction,
    TriggerDefinition,
    TriggerManager,
)
from nymeria.triggers import trigger_api as trigger_api_module
from nymeria.triggers.sources import (
    redact_source_config,
    redacted_secret_keys,
    restore_unchanged_secrets,
    secret_field_names,
)

SECRET = "QA-CANARY-8f31d0"


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
    return client, token


def _add_trigger(
    data_dir: Path,
    *,
    source_type: str = "webhook",
    source_config: dict | None = None,
    trigger_id: str = "webhook1",
    user_id: str = "owner",
) -> TriggerDefinition:
    manager = TriggerManager(data_dir)
    trigger = TriggerDefinition(
        id=trigger_id,
        name="Probe",
        source_type=source_type,
        source_config=source_config if source_config is not None else {"secret": SECRET},
        action=TriggerAction(
            type="agent_prompt",
            config={"prompt_template": "Payload: {message}"},
        ),
        thread_id=f"trigger-{trigger_id}",
        created_by="user",
        state={"trigger_id": trigger_id},
    )
    with manager.atomic_update(user_id) as store:
        store.triggers.append(trigger)
    return trigger


# --- the declaration itself -------------------------------------------------


def test_secret_fields_come_from_each_source_schema():
    """The knowledge lives on the source, not in a key list somewhere else.
    `secret: true` has been part of the documented config-schema contract all
    along; until #307 only the GUI wizards read it."""
    assert secret_field_names("webhook") == {"secret"}
    assert secret_field_names("slack") == {"bot_token"}
    assert secret_field_names("rss") == set()
    # Unknown is NOT the same answer as "declares nothing": the caller has to
    # fail safe on a source whose plugin is missing.
    assert secret_field_names("no_such_source") is None


def test_redaction_masks_declared_secrets_and_leaves_the_rest_alone():
    masked = redact_source_config("webhook", {"secret": SECRET})
    assert masked["secret"] != SECRET
    assert SECRET not in str(masked)
    # Enough to compare two values, useless to replay.
    assert masked["secret"].startswith("QA-C")
    assert masked["secret"].endswith("1d0")

    untouched = redact_source_config("rss", {"url": "https://example.com/feed"})
    assert untouched == {"url": "https://example.com/feed"}


def test_an_unregistered_source_fails_safe_by_name():
    """A source plugin removed from the build cannot be asked what is secret,
    so the fallback guesses by name. Masking a harmless field is cosmetic;
    printing a live credential is not."""
    masked = redact_source_config(
        "gone", {"api_key": "abcdefghijklmnop", "url": "https://x"}
    )
    assert "abcdefghijklmnop" not in str(masked)
    assert masked["url"] == "https://x"


def test_http_poll_headers_mask_values_but_keep_header_names():
    """`headers` is a free-form dict whose VALUES are arbitrary Authorization
    credentials, which a boolean per-field flag cannot express. Names stay
    visible because they are what a reader debugging a poll needs."""
    config = {
        "url": "https://api.example.com/status",
        "headers": {"Authorization": "Bearer sk-live-abcdef123456", "Accept": "application/json"},
    }
    masked = redact_source_config("http_poll", config)
    assert "sk-live-abcdef123456" not in str(masked)
    assert "Authorization" in masked["headers"]
    assert masked["url"] == "https://api.example.com/status"


# --- the agent surface ------------------------------------------------------


def test_agent_detail_view_never_prints_the_secret(tmp_path):
    """The leak lands on the DEBUGGING path: "why is my webhook rejecting me"
    is exactly when this view gets called."""
    from nymeria.tools.triggers import _inspect_detail

    _add_trigger(tmp_path)
    manager = TriggerManager(tmp_path)
    rendered = _inspect_detail(manager, "owner", "webhook1")

    assert SECRET not in rendered
    assert "Source config:" in rendered
    assert "QA-C" in rendered  # the fingerprint is still there to compare
    # A fingerprint looks like a value, so the view must say it is not one,
    # or a reader will try to authenticate with it.
    assert "fingerprint" in rendered
    # The note must NAME the field it withheld, not just mention masking.
    assert "Note: secret shown as a fingerprint" in rendered


def test_agent_detail_view_masks_a_slack_token_too(tmp_path):
    """Schema-driven, so a second source needs no new code."""
    from nymeria.tools.triggers import _inspect_detail

    _add_trigger(
        tmp_path,
        source_type="slack",
        source_config={"bot_token": "xoxb-9999-secret-token", "channel_id": "C123"},
        trigger_id="slack1",
    )
    manager = TriggerManager(tmp_path)
    rendered = _inspect_detail(manager, "owner", "slack1")

    assert "xoxb-9999-secret-token" not in rendered
    assert "C123" in rendered  # the non-secret field is untouched


def test_agent_detail_view_says_nothing_about_masking_when_nothing_is_secret(
    tmp_path,
):
    from nymeria.tools.triggers import _inspect_detail

    _add_trigger(
        tmp_path,
        source_type="rss",
        source_config={"url": "https://example.com/feed.xml"},
        trigger_id="rss1",
    )
    manager = TriggerManager(tmp_path)
    rendered = _inspect_detail(manager, "owner", "rss1")

    assert "https://example.com/feed.xml" in rendered
    assert "fingerprint" not in rendered


# --- the REST wire ----------------------------------------------------------


def test_rest_read_masks_the_secret_and_declares_which_keys(
    tmp_path, api_client_builder, monkeypatch
):
    client, token = _client(tmp_path, api_client_builder, monkeypatch)
    _add_trigger(tmp_path)

    response = client.get(
        "/triggers/webhook1", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    body = response.json()
    assert SECRET not in response.text
    assert body["source_config"]["secret"] != SECRET
    # Clients are TOLD which keys are masked rather than guessing by name.
    assert body["source_config_secret_fields"] == ["secret"]


def test_rest_list_masks_the_secret(tmp_path, api_client_builder, monkeypatch):
    client, token = _client(tmp_path, api_client_builder, monkeypatch)
    _add_trigger(tmp_path)

    response = client.get(
        "/triggers", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    assert SECRET not in response.text


# --- the round trip, end to end --------------------------------------------


def test_saving_back_the_mask_keeps_the_real_secret_working(
    tmp_path, api_client_builder, monkeypatch
):
    """The property that makes masking the wire safe at all.

    Both GUI wizards prefill from the read model and POST the whole
    source_config back, so a plain rename would otherwise write the
    fingerprint over the live credential. Asserting the stored value is not
    enough: this fires the real public webhook afterwards, which is the only
    thing that proves the credential still authenticates.
    """
    client, token = _client(tmp_path, api_client_builder, monkeypatch)
    _add_trigger(tmp_path)
    auth = {"Authorization": f"Bearer {token}"}

    masked = client.get("/triggers/webhook1", headers=auth).json()["source_config"]
    assert masked["secret"] != SECRET

    # Exactly what a wizard save sends: the whole dict, mask included.
    patched = client.patch(
        "/triggers/webhook1",
        headers=auth,
        json={"name": "Renamed", "source_config": masked},
    )
    assert patched.status_code == 200
    assert patched.json()["name"] == "Renamed"

    stored = TriggerManager(tmp_path).get_trigger("owner", "webhook1")
    assert stored is not None
    assert stored.source_config["secret"] == SECRET

    fired = client.post(
        f"/triggers/fire/webhook1?user_id=owner&secret={SECRET}",
        json={"message": "hello"},
    )
    assert fired.status_code == 200
    assert fired.json()["status"] == "fired"

    # And the fingerprint itself is not a working credential.
    replayed = client.post(
        f"/triggers/fire/webhook1?user_id=owner&secret={masked['secret']}",
        json={"message": "hello"},
    )
    assert replayed.status_code == 403


def test_a_genuinely_new_secret_still_changes(
    tmp_path, api_client_builder, monkeypatch
):
    """The restore must not swallow a real edit."""
    client, token = _client(tmp_path, api_client_builder, monkeypatch)
    _add_trigger(tmp_path)
    auth = {"Authorization": f"Bearer {token}"}

    response = client.patch(
        "/triggers/webhook1",
        headers=auth,
        json={"source_config": {"secret": "a-brand-new-secret"}},
    )
    assert response.status_code == 200

    stored = TriggerManager(tmp_path).get_trigger("owner", "webhook1")
    assert stored is not None
    assert stored.source_config["secret"] == "a-brand-new-secret"

    assert (
        client.post(
            f"/triggers/fire/webhook1?user_id=owner&secret={SECRET}",
            json={"message": "hello"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/triggers/fire/webhook1?user_id=owner&secret=a-brand-new-secret",
            json={"message": "hello"},
        ).status_code
        == 200
    )


def test_restore_leaves_a_dict_valued_secret_field_per_value(tmp_path):
    """http_poll headers round-trip per value: an untouched Authorization is
    kept while an edited Accept lands."""
    stored = {"Authorization": "Bearer sk-live-abcdef123456", "Accept": "text/plain"}
    masked = redact_source_config("http_poll", {"headers": stored})["headers"]
    restored = restore_unchanged_secrets(
        "http_poll",
        {"headers": {**masked, "Accept": "application/json"}},
        {"headers": stored},
    )
    assert restored["headers"]["Authorization"] == "Bearer sk-live-abcdef123456"
    assert restored["headers"]["Accept"] == "application/json"


def test_redacted_secret_keys_reports_only_what_was_actually_masked():
    """Not "which keys would qualify": an empty secret is left alone by the
    redactor, and reporting it as masked puts a "masked" chip beside a
    visibly empty value on a field whose selling point is authority."""
    assert redacted_secret_keys("webhook", {"secret": SECRET}) == ["secret"]
    assert redacted_secret_keys("webhook", {"secret": ""}) == []
    assert redacted_secret_keys("webhook", {}) == []
    assert redacted_secret_keys("rss", {"url": "https://x"}) == []


def test_the_agent_write_path_also_keeps_an_echoed_fingerprint(tmp_path):
    """The agent reads a fingerprint too, so it can write one back (review
    finding on the first cut, which wired the restore into the REST route
    only).

    `source_config` is a whole-dict replace, so an agent changing one
    NON-secret key has to resend the rest, and the only value it holds for
    the secret key is the mask it was shown. Slack is exactly this shape:
    "point this trigger at another channel" would have written
    `xoxb...ken` over a live bot token. The restore therefore lives in
    `TriggerManager.update_trigger`, the one seam both writers pass through.
    """
    from nymeria.tools.triggers import _inspect_detail

    _add_trigger(
        tmp_path,
        source_type="slack",
        source_config={"bot_token": "xoxb-9999-secret-token", "channel_id": "C123"},
        trigger_id="slack1",
    )
    manager = TriggerManager(tmp_path)

    # What the agent can actually see.
    rendered = _inspect_detail(manager, "owner", "slack1")
    assert "xoxb-9999-secret-token" not in rendered
    masked_token = json.loads(
        rendered.split("Source config: ", 1)[1].split("\n", 1)[0]
    )["bot_token"]

    # It resends the whole dict to change only the channel.
    manager.update_trigger(
        "owner",
        "slack1",
        source_config={"bot_token": masked_token, "channel_id": "C999"},
    )

    stored = manager.get_trigger("owner", "slack1")
    assert stored is not None
    assert stored.source_config["bot_token"] == "xoxb-9999-secret-token"
    assert stored.source_config["channel_id"] == "C999"


def test_creating_a_trigger_from_a_fingerprint_is_refused(tmp_path):
    """The failure mode masking itself creates (review finding).

    An agent reads a trigger's details, sees the fingerprint, is asked for
    "another one like that", and passes the fingerprint into a CREATE. Nothing
    can compare it against a stored value, so without this guard an
    11-character string already sitting in the transcript, the checkpoints and
    the RAG index would become a LIVE credential on the anonymous fire route,
    while the user pointed their external service at the real secret and got
    403. Update repairs silently; create can only refuse.
    """
    from nymeria.tools.triggers import _inspect_detail
    from nymeria.triggers.sources import MaskedSecretRejected

    _add_trigger(tmp_path)
    manager = TriggerManager(tmp_path)
    rendered = _inspect_detail(manager, "owner", "webhook1")
    fingerprint = json.loads(
        rendered.split("Source config: ", 1)[1].split("\n", 1)[0]
    )["secret"]

    with pytest.raises(MaskedSecretRejected) as exc:
        manager.add_trigger(
            user_id="owner",
            name="Copy of the first one",
            source_type="webhook",
            source_config={"secret": fingerprint},
            action=TriggerAction(type="notify", config={"message_template": "x"}),
        )
    assert "secret" in str(exc.value)

    # Nothing was stored, so the fingerprint never became a usable credential.
    assert len(TriggerManager(tmp_path).get_triggers("owner")) == 1

    # A real value is still accepted.
    assert (
        manager.add_trigger(
            user_id="owner",
            name="A genuine second trigger",
            source_type="webhook",
            source_config={"secret": "a-different-real-secret"},
            action=TriggerAction(type="notify", config={"message_template": "x"}),
        )
        is not None
    )


def test_the_create_surface_explains_the_refusal(
    tmp_path, api_client_builder, monkeypatch
):
    """A bare rejection would surface as "check source_type and config",
    which sends the reader looking in the wrong place entirely."""
    client, token = _client(tmp_path, api_client_builder, monkeypatch)
    _add_trigger(tmp_path)
    masked = redact_source_config("webhook", {"secret": SECRET})["secret"]

    response = client.post(
        "/triggers",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "name": "Copy",
            "source_type": "webhook",
            "source_config": {"secret": masked},
            "action_type": "notify",
            "action_config": {"message_template": "x"},
        },
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "fingerprint" in detail
    assert "Check source_type" not in detail


def test_an_update_says_when_it_kept_a_secret_you_echoed(tmp_path, monkeypatch):
    """Create refuses an echoed fingerprint loudly, so update must not
    discard one silently (live verification finding).

    Before this, a rename that resent the masked config answered a bare
    "Trigger x updated." The caller could not tell "my new secret was saved"
    from "the fingerprint I sent was ignored" without firing the webhook to
    find out, which is exactly what the verifying agent had to do.
    """
    from nymeria.tools import triggers as trigger_tools

    _add_trigger(tmp_path)
    monkeypatch.setattr(trigger_tools, "_trigger_manager", TriggerManager(tmp_path))
    cfg = {"configurable": {"user_id": "owner"}}
    masked = redact_source_config("webhook", {"secret": SECRET})

    kept = trigger_tools._trigger_update(
        "webhook1", name="Renamed", source_config=masked, config=cfg
    )
    assert kept.startswith("[Success]")
    assert "secret unchanged" in kept
    assert "fingerprint" in kept
    assert (
        TriggerManager(tmp_path).get_trigger("owner", "webhook1").source_config["secret"]
        == SECRET
    )

    changed = trigger_tools._trigger_update(
        "webhook1", source_config={"secret": "a-real-new-one"}, config=cfg
    )
    assert changed.startswith("[Success]")
    assert "unchanged" not in changed
