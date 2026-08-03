"""Tests for the chained /provider setup flow (backlog #110).

Server-side coverage of the step chain rendered as one tabbed form (the
step rail): start, key step (masked entry and Keep/Replace/Clear),
connection steps, the model step with pending credentials and its cache,
review, back-navigation, test-first atomic apply, cancel, and the TTL
store. The CLI renderer/adapter halves are covered by
test_cli_form_panel.py and test_cli_form_contract.py.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from cli_fixtures import run
from nymeria.core import provider_setup
from nymeria.core.command_service import CommandContext, CommandService
from nymeria.core.provider_setup import clean_pasted_secret


@pytest.fixture(autouse=True)
def _clear_pending_store():
    provider_setup._pending.clear()
    yield
    provider_setup._pending.clear()


class FakeSetupApi:
    """Minimal API facade for the /provider setup handlers."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.settings: dict[str, Any] = {
            "llm_provider": "openai",
            "llm_model": "gpt-test",
            "llm_base_url": "",
            "openai_api_mode": "responses",
        }
        self.env_entries: list[dict[str, Any]] = []
        self.models: list[dict[str, Any]] = []
        self.provider_test_result: dict[str, Any] = {"ok": True, "message": ""}

    async def get_settings(self, user_id: str | None = None) -> dict[str, Any]:
        return dict(self.settings)

    async def get_env_vars(self, *, user_id: str | None = None) -> dict[str, Any]:
        return {"entries": [dict(entry) for entry in self.env_entries]}

    async def list_available_models(
        self,
        provider: str | None = None,
        user_id: str | None = None,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> list[dict[str, Any]]:
        self.calls.append(
            (
                "list_available_models",
                {"provider": provider, "api_key": api_key, "base_url": base_url},
            )
        )
        return [dict(entry) for entry in self.models]

    async def test_llm_provider_config(
        self, request: dict[str, Any], *, user_id: str | None = None
    ) -> dict[str, Any]:
        self.calls.append(("test_llm_provider_config", dict(request)))
        return dict(self.provider_test_result)

    async def update_settings(
        self, *, user_id: str | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        self.calls.append(("update_settings", dict(kwargs)))
        return {"updated": list(kwargs), "restart_required": False}


def _run(api: FakeSetupApi, command: str, *, is_admin: bool = True):
    return run(
        CommandService().execute(
            CommandContext(
                user_id="alice",
                thread_id="thread-1",
                actor="user",
                surface="cli",
                is_admin=is_admin,
                supports_forms=True,
            ),
            command,
            api=api,
        )
    )


def _form(result) -> dict[str, Any]:
    form = (result.data or {}).get("form")
    assert form is not None, f"expected a form on: {result.markdown}"
    return form


def _active_tab(form: dict[str, Any]) -> dict[str, Any]:
    """The step-rail tab flagged active; exactly one per chain response."""
    flagged = [tab for tab in form["tabs"] if tab.get("active")]
    assert len(flagged) == 1, f"expected one active tab: {form['tabs']}"
    return flagged[0]


def _updates(api: FakeSetupApi) -> list[dict[str, Any]]:
    return [payload for name, payload in api.calls if name == "update_settings"]


def _listings(api: FakeSetupApi) -> list[dict[str, Any]]:
    return [
        payload for name, payload in api.calls if name == "list_available_models"
    ]


# ── start + key step ────────────────────────────────────────────────────────


def test_setup_start_without_server_key_shows_masked_entry() -> None:
    api = FakeSetupApi()

    result = _run(api, "/provider setup anthropic")

    assert result.success is True
    assert "Configure Anthropic" in result.markdown
    assert "console.anthropic.com" in result.markdown  # signup guidance
    # Form-less surfaces get the typed path for the active step.
    assert "Type: /provider setup key" in result.markdown
    form = _form(result)
    tab = _active_tab(form)
    assert tab["label"] == "API key"
    field = tab["fields"][0]
    assert field["kind"] == "text"
    assert field["secret"] is True
    assert tab["submit"] == {"command": "provider setup key {api_key}"}
    # A text-entry tab words the Enter action as submit, not apply.
    assert form["footer_hint"] == "Enter submit · Esc close"


def test_setup_start_with_server_key_offers_keep_replace_clear() -> None:
    api = FakeSetupApi()
    api.env_entries = [
        {"name": "openai_api_key", "env_var": "OPENAI_API_KEY", "is_set": True}
    ]

    result = _run(api, "/provider setup openai")

    assert result.success is True
    assert "OPENAI_API_KEY" in result.markdown
    tab = _active_tab(_form(result))
    field = tab["fields"][0]
    assert field["kind"] == "radio"
    assert [option["id"] for option in field["options"]] == [
        "keep",
        "replace",
        "clear",
    ]
    assert tab["submit"] == {"command": "provider setup {key_choice}"}


def test_setup_unknown_provider_is_refused() -> None:
    result = _run(FakeSetupApi(), "/provider setup bogus-llm")
    assert result.success is False
    assert "Unknown provider" in result.markdown


def test_setup_requires_admin() -> None:
    result = _run(FakeSetupApi(), "/provider setup openai", is_admin=False)
    assert result.success is False


# ── the full chain: growing tab rail, atomic apply ──────────────────────────


def test_setup_full_chain_applies_one_atomic_patch() -> None:
    """keep -> mode -> baseurl (current set forces the step) -> model ->
    review -> apply notest writes exactly ONE update_settings patch. The
    chain form's tab rail grows one step at a time with the new step
    active."""
    api = FakeSetupApi()
    api.env_entries = [
        {"name": "openai_api_key", "env_var": "OPENAI_API_KEY", "is_set": True}
    ]
    api.settings["llm_base_url"] = "http://localhost:8318"
    api.models = [{"id": "gpt-5.5", "context_length": 400000}]

    _run(api, "/provider setup openai")
    step = _run(api, "/provider setup keep")
    # openai supports_responses -> the API mode step comes next; the key
    # step stays behind it as a revisitable tab.
    form = _form(step)
    assert [tab["label"] for tab in form["tabs"]] == ["API key", "API mode"]
    tab = _active_tab(form)
    assert tab["label"] == "API mode"
    assert tab["submit"] == {"command": "provider setup mode {api_mode}"}

    step = _run(api, "/provider setup mode responses")
    # A configured base URL forces an explicit keep/clear decision.
    tab = _active_tab(_form(step))
    assert tab["label"] == "Base URL"
    assert tab["submit"] == {"command": "provider setup baseurl {base_url_choice}"}
    ids = [option["id"] for option in tab["fields"][0]["options"]]
    assert ids == ["http://localhost:8318", "default", "custom"]
    current = [o for o in tab["fields"][0]["options"] if o["current"]]
    assert current[0]["id"] == "http://localhost:8318"

    step = _run(api, "/provider setup baseurl default")
    assert "1 models listed" in step.markdown
    tab = _active_tab(_form(step))
    assert tab["label"] == "Model"
    assert tab["submit"] == {"command": "provider setup model {model}"}
    option_ids = [option["id"] for option in tab["fields"][1]["options"]]
    # The active provider's current model surfaces even when the live list
    # does not contain it, so the tab renders the standing decision.
    assert option_ids == ["gpt-test", "gpt-5.5", "custom"]

    step = _run(api, "/provider setup model gpt-5.5")
    form = _form(step)
    assert "Review the pending provider change" in step.markdown
    assert "Choose: /provider setup apply | notest | cancel" in step.markdown
    assert form["footer_hint"] == "←→ step · Enter apply · Esc close"
    assert [t["label"] for t in form["tabs"]] == [
        "API key",
        "API mode",
        "Base URL",
        "Model",
        "Review",
    ]
    tab = _active_tab(form)
    assert tab["label"] == "Review"
    actions = [option["id"] for option in tab["fields"][0]["options"]]
    assert actions == ["apply", "notest", "cancel"]

    final = _run(api, "/provider setup apply notest")
    assert final.success is True
    assert _updates(api) == [
        {
            "llm_provider": "openai",
            "llm_model": "gpt-5.5",
            "llm_base_url": "",
            "openai_api_mode": "responses",
        }
    ]
    # The store is cleared: a bare resume reports nothing in progress.
    assert _run(api, "/provider setup").success is False


def test_setup_revisiting_a_decided_step_returns_to_review() -> None:
    """Back navigation: once everything is decided, re-submitting an earlier
    step's tab lands back on Review with the revision applied, and the
    cached model list is NOT refetched for unchanged credentials."""
    api = FakeSetupApi()
    api.env_entries = [
        {"name": "openai_api_key", "env_var": "OPENAI_API_KEY", "is_set": True}
    ]
    api.models = [{"id": "gpt-5.5"}]

    _run(api, "/provider setup openai")
    _run(api, "/provider setup keep")
    _run(api, "/provider setup mode responses")
    _run(api, "/provider setup model gpt-5.5")
    assert len(_listings(api)) == 1

    step = _run(api, "/provider setup mode chat_completions")
    tab = _active_tab(_form(step))
    assert tab["label"] == "Review"
    assert "chat_completions" in step.markdown  # the revision shows in review
    assert len(_listings(api)) == 1  # cache hit: no refetch

    # The form path submits the single-token id.
    _run(api, "/provider setup notest")
    patch = _updates(api)[0]
    assert patch["openai_api_mode"] == "chat_completions"


def test_setup_pasted_key_feeds_model_list_and_apply_but_never_leaks() -> None:
    api = FakeSetupApi()
    api.models = [{"id": "claude-fable-5", "context_length": 500000}]

    _run(api, "/provider setup anthropic")
    # Paste with bracketed-paste markers and a trailing newline: hygiene
    # strips them before the key is stored.
    step = _run(api, "/provider setup key \x1b[200~sk-ant-secret-1\x1b[201~\n")
    assert step.success is True
    listing = _listings(api)[0]
    assert listing["provider"] == "anthropic"
    assert listing["api_key"] == "sk-ant-secret-1"
    # The secret never rides the markdown or the form payload.
    assert "sk-ant-secret-1" not in step.markdown
    assert "sk-ant-secret-1" not in str(step.data)
    # The model-list cache fingerprint carries a digest, never the raw key.
    record = provider_setup.get_setup("alice")
    assert record is not None
    assert record.models_fingerprint
    assert "sk-ant-secret-1" not in record.models_fingerprint

    _run(api, "/provider setup model claude-fable-5")
    final = _run(api, "/provider setup apply")
    assert final.success is True
    test_request = [p for n, p in api.calls if n == "test_llm_provider_config"][0]
    assert test_request["api_key"] == "sk-ant-secret-1"
    assert _updates(api) == [
        {
            "llm_provider": "anthropic",
            "llm_model": "claude-fable-5",
            "llm_api_key": "sk-ant-secret-1",
        }
    ]
    assert "sk-ant-secret-1" not in final.markdown


def test_setup_pasted_key_tab_becomes_keep_replace_clear() -> None:
    """After a paste, revisiting the key tab offers keeping the PASTED key
    (not the masked entry field), so back-navigation cannot lose it."""
    api = FakeSetupApi()
    api.models = [{"id": "claude-fable-5"}]

    _run(api, "/provider setup anthropic")
    step = _run(api, "/provider setup key sk-ant-secret-1")
    key_tab = _form(step)["tabs"][0]
    assert key_tab["label"] == "API key"
    field = key_tab["fields"][0]
    assert field["kind"] == "radio"
    labels = {option["id"]: option["label"] for option in field["options"]}
    assert labels["keep"] == "Keep the pasted key"
    # No clearable stored key on the server -> the clear option (whose
    # ""-patch would silently drop) is not offered at all.
    assert list(labels) == ["keep", "replace"]
    # Re-submitting keep from that tab preserves the pasted key.
    _run(api, "/provider setup keep")
    _run(api, "/provider setup model claude-fable-5")
    _run(api, "/provider setup apply notest")
    assert _updates(api)[0]["llm_api_key"] == "sk-ant-secret-1"


def test_setup_apply_test_failure_writes_nothing_and_offers_retry() -> None:
    api = FakeSetupApi()
    api.provider_test_result = {"ok": False, "message": "401 unauthorized"}

    _run(api, "/provider setup anthropic")
    _run(api, "/provider setup key sk-ant-bad")
    _run(api, "/provider setup model claude-fable-5")
    result = _run(api, "/provider setup apply")

    assert result.success is True  # info-level so the retry form survives
    assert "FAILED" in result.markdown
    assert "401 unauthorized" in result.markdown
    assert "Nothing was saved" in result.markdown
    assert _updates(api) == []
    actions = [
        o["id"] for o in _form(result)["tabs"][0]["fields"][0]["options"]
    ]
    assert actions == ["apply", "notest", "replace", "cancel"]
    # The pending setup survives the failure: apply-anyway still works.
    final = _run(api, "/provider setup apply notest")
    assert final.success is True
    assert len(_updates(api)) == 1


def test_setup_clear_key_patches_concrete_settings_fields() -> None:
    api = FakeSetupApi()
    api.env_entries = [
        {"name": "openai_api_key", "env_var": "OPENAI_API_KEY", "is_set": True}
    ]
    api.models = [{"id": "gpt-5.5"}]

    _run(api, "/provider setup openai")
    step = _run(api, "/provider setup clear")
    assert "stop working after apply" in step.markdown  # requires-key warning
    _run(api, "/provider setup mode responses")
    _run(api, "/provider setup model gpt-5.5")
    _run(api, "/provider setup apply notest")

    assert _updates(api) == [
        {
            "llm_provider": "openai",
            "llm_model": "gpt-5.5",
            "openai_api_mode": "responses",
            "openai_api_key": "",
        }
    ]


def test_setup_clear_without_clearable_field_refuses_honestly() -> None:
    """A typed clear with no patchable key field must refuse at apply
    (the ""-patch would silently drop on ServerSettingsUpdate), never
    pretend the key was removed."""
    api = FakeSetupApi()

    _run(api, "/provider setup anthropic")
    _run(api, "/provider setup clear")
    _run(api, "/provider setup model claude-fable-5")
    result = _run(api, "/provider setup notest")

    assert result.success is False
    assert "No clearable stored key" in result.markdown
    assert _updates(api) == []


def test_setup_empty_model_list_is_not_cached() -> None:
    """A failed/empty listing renders the degrade note but is NOT cached:
    the next render retries, so a transient outage or a key fixed on the
    key tab recovers without restarting the chain."""
    api = FakeSetupApi()  # api.models is empty

    _run(api, "/provider setup anthropic")
    step = _run(api, "/provider setup key sk-ant-x")
    assert "Model list unavailable" in step.markdown
    assert len(_listings(api)) == 1

    api.models = [{"id": "claude-fable-5"}]
    step = _run(api, "/provider setup")  # bare resume re-renders the chain
    assert "1 models listed" in step.markdown
    assert len(_listings(api)) == 2


def test_setup_quoted_value_is_unquoted() -> None:
    """The form client shell-quotes values containing whitespace; the step
    reader unquotes a single quoted token back to the raw value."""
    api = FakeSetupApi()
    _run(api, "/provider setup anthropic")
    _run(api, "/provider setup key sk-ant-x")
    _run(api, "/provider setup model 'my custom model'")

    record = provider_setup.get_setup("alice")
    assert record is not None
    assert record.model == "my custom model"


def test_setup_cancel_discards_state() -> None:
    api = FakeSetupApi()
    _run(api, "/provider setup anthropic")
    result = _run(api, "/provider setup cancel")
    assert result.success is True
    assert "cancelled" in result.markdown.lower()
    assert provider_setup.get_setup("alice") is None


def test_setup_model_list_failure_degrades_to_defaults() -> None:
    class _BrokenListApi(FakeSetupApi):
        async def list_available_models(self, *args: Any, **kwargs: Any):
            raise RuntimeError("provider offline")

    api = _BrokenListApi()
    _run(api, "/provider setup anthropic")
    step = _run(api, "/provider setup key sk-ant-x")

    assert step.success is True
    assert "Model list unavailable" in step.markdown
    tab = _active_tab(_form(step))
    option_ids = [option["id"] for option in tab["fields"][1]["options"]]
    # Spec default plus the custom escape hatch; never an empty dead end.
    assert option_ids == ["claude-sonnet-4-6", "custom"]


# ── store + hygiene units ───────────────────────────────────────────────────


def test_pending_setup_expires_after_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    provider_setup.start_setup("alice", "openai")
    assert provider_setup.get_setup("alice") is not None
    real_monotonic = time.monotonic
    monkeypatch.setattr(
        provider_setup.time,
        "monotonic",
        lambda: real_monotonic() + provider_setup.PENDING_SETUP_TTL_SECONDS + 1,
    )
    assert provider_setup.get_setup("alice") is None


def test_pending_setup_ttl_slides_on_activity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reads and updates refresh the expiry, so an in-flight chain (the
    user off fetching a key from a signup page) stays alive while idle
    abandoned records still expire."""
    record = provider_setup.start_setup("alice", "openai")
    first_expiry = record.expires_at
    real_monotonic = time.monotonic
    half_ttl = provider_setup.PENDING_SETUP_TTL_SECONDS * 0.5
    monkeypatch.setattr(
        provider_setup.time, "monotonic", lambda: real_monotonic() + half_ttl
    )
    assert provider_setup.get_setup("alice") is record
    assert record.expires_at > first_expiry
    # Past the ORIGINAL expiry, the touched record is still alive.
    monkeypatch.setattr(
        provider_setup.time,
        "monotonic",
        lambda: real_monotonic() + half_ttl * 2.5,
    )
    assert provider_setup.get_setup("alice") is record


def test_pending_setup_repr_redacts_the_key() -> None:
    record = provider_setup.start_setup("alice", "openai")
    provider_setup.update_setup("alice", api_key="sk-super-secret")
    assert "sk-super-secret" not in repr(record)


def test_clean_pasted_secret_strips_markers_and_reports_lookalikes() -> None:
    value, warnings = clean_pasted_secret("\x1b[200~sk-abc\r\n\x1b[201~")
    assert value == "sk-abc"
    assert warnings == []

    value, warnings = clean_pasted_secret("sk-–abc")  # en dash lookalike
    assert value == "sk-–abc"
    assert len(warnings) == 1
    assert "U+2013" in warnings[0]
    assert "position 4" in warnings[0]

    # A copied "Bearer <key>" or wrapped paste: internal whitespace warned.
    value, warnings = clean_pasted_secret("Bearer sk-abc")
    assert value == "Bearer sk-abc"
    assert any("internal whitespace" in warning for warning in warnings)
