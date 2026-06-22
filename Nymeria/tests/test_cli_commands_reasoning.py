"""CLI ``/reasoning`` command tests.

Relocated from test_reasoning_effort_scale.py (optimization slice 33 F5):
these exercise the CLI command layer
(``nymeria.triggers.cli.commands.reasoning``), not the provider
effort-translation wire-format that fills the rest of that file. The
backend ``/think`` equivalent lives in test_command_service.py; the two real
code paths (CLI command module vs CommandService) are deliberately covered
separately.
"""

from __future__ import annotations


def test_cli_reasoning_command_valid_efforts():
    from nymeria.triggers.cli.commands.reasoning import VALID_EFFORTS

    assert VALID_EFFORTS == {"off", "low", "medium", "high", "xhigh", "max"}


class _CliReasoningFakeClient:
    """Minimal CLI transport double for the /reasoning command."""

    def __init__(
        self,
        *,
        settings: dict | None = None,
        thread_config: dict | None = None,
    ) -> None:
        self.settings = settings or {}
        self.thread_config = thread_config or {}
        self.calls: list[tuple[str, dict]] = []

    async def get_settings(self, user_id: str | None = None) -> dict:
        self.calls.append(("get_settings", {"user_id": user_id}))
        return dict(self.settings)

    async def update_settings(self, *, user_id: str | None = None, **kwargs) -> dict:
        self.calls.append(("update_settings", {"user_id": user_id, **kwargs}))
        return {"updated": list(kwargs)}

    async def get_thread_config(
        self, thread_id: str, user_id: str | None = None
    ) -> dict:
        self.calls.append(
            ("get_thread_config", {"thread_id": thread_id, "user_id": user_id})
        )
        return dict(self.thread_config)

    async def update_thread_config(
        self, thread_id: str, *, user_id: str | None = None, **kwargs
    ) -> dict:
        self.calls.append(
            ("update_thread_config", {"thread_id": thread_id, "user_id": user_id, **kwargs})
        )
        return dict(kwargs)


def _run_cli_reasoning(client, args, *, thread_id=None):
    import asyncio

    from nymeria.triggers.cli.commands import CommandContext as CliCommandContext
    from nymeria.triggers.cli.commands.reasoning import _handle_reasoning

    context = CliCommandContext(client=client, thread_id=thread_id, user_id="alice")
    return asyncio.run(_handle_reasoning(context, args))


def test_cli_reasoning_on_global_clears_persisted_off_effort():
    client = _CliReasoningFakeClient(settings={"llm_reasoning_effort": "off"})

    result = _run_cli_reasoning(client, ["on"])

    assert result.ok is True
    update = next(call for call in client.calls if call[0] == "update_settings")[1]
    assert update["llm_extended_thinking"] is True
    assert "llm_reasoning_effort" in update
    assert update["llm_reasoning_effort"] is None
    assert "effort reset to default" in result.messages[0].content
    assert result.payload["effort_cleared"] is True


def test_cli_reasoning_on_global_without_off_leaves_effort_untouched():
    client = _CliReasoningFakeClient(settings={"llm_reasoning_effort": "high"})

    result = _run_cli_reasoning(client, ["on"])

    assert result.ok is True
    update = next(call for call in client.calls if call[0] == "update_settings")[1]
    assert update["llm_extended_thinking"] is True
    assert "llm_reasoning_effort" not in update
    assert "effort_cleared" not in (result.payload or {})


def test_cli_reasoning_on_thread_clears_persisted_off_to_inherit():
    client = _CliReasoningFakeClient(
        thread_config={"llm_config": {"reasoning_effort": "off"}},
    )

    result = _run_cli_reasoning(client, ["on"], thread_id="thread-1")

    assert result.ok is True
    update = next(
        call for call in client.calls if call[0] == "update_thread_config"
    )[1]
    assert update["llm_config"] == {
        "extended_thinking": True,
        "reasoning_effort": "",
    }
    assert "effort reset to default" in result.messages[0].content


def test_cli_reasoning_on_thread_without_off_leaves_effort_untouched():
    client = _CliReasoningFakeClient(
        thread_config={"llm_config": {"reasoning_effort": "high"}},
    )

    result = _run_cli_reasoning(client, ["on"], thread_id="thread-1")

    assert result.ok is True
    update = next(
        call for call in client.calls if call[0] == "update_thread_config"
    )[1]
    assert update["llm_config"] == {"extended_thinking": True}


def test_cli_reasoning_off_still_persists_explicit_off():
    client = _CliReasoningFakeClient(settings={"llm_reasoning_effort": None})

    result = _run_cli_reasoning(client, ["off"])

    assert result.ok is True
    update = next(call for call in client.calls if call[0] == "update_settings")[1]
    assert update["llm_extended_thinking"] is False
    assert update["llm_reasoning_effort"] == "off"


def test_cli_reasoning_over_ask_reports_clamped_level():
    client = _CliReasoningFakeClient(
        settings={"llm_provider": "openai", "llm_model": "gpt-5.1"},
    )

    result = _run_cli_reasoning(client, ["xhigh"])

    assert result.ok is True
    assert "this model runs at high" in result.messages[0].content
    assert result.payload["effort_effective"] == "high"


def test_cli_reasoning_supported_level_has_no_clamp_note():
    client = _CliReasoningFakeClient(
        settings={"llm_provider": "anthropic", "llm_model": "claude-opus-4-8"},
    )

    result = _run_cli_reasoning(client, ["max"])

    assert result.ok is True
    assert "runs at" not in result.messages[0].content
    assert result.payload["effort_effective"] == "max"


def test_cli_reasoning_off_on_undisableable_model_reports_floor():
    client = _CliReasoningFakeClient(
        settings={"llm_provider": "anthropic", "llm_model": "claude-fable-5"},
    )

    result = _run_cli_reasoning(client, ["off"])

    assert result.ok is True
    assert "cannot disable thinking" in result.messages[0].content
    assert result.payload["effort_effective"] == "low"


def test_cli_reasoning_thread_clamp_uses_thread_model_override():
    client = _CliReasoningFakeClient(
        settings={"llm_provider": "openai", "llm_model": "gpt-5.5"},
        thread_config={"llm_config": {"model": "gpt-5.1"}},
    )

    result = _run_cli_reasoning(client, ["xhigh"], thread_id="thread-1")

    assert result.ok is True
    assert "this model runs at high" in result.messages[0].content


def test_cli_reasoning_show_state_lists_supported_levels():
    client = _CliReasoningFakeClient(
        settings={
            "llm_provider": "anthropic",
            "llm_model": "claude-fable-5",
            "llm_extended_thinking": True,
            "llm_reasoning_effort": "high",
        },
    )

    result = _run_cli_reasoning(client, [])

    assert result.ok is True
    content = result.messages[0].content
    assert "Supported  low, medium, high, xhigh, max (claude-fable-5)" in content
    assert result.payload["supported_efforts"] == [
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    ]
