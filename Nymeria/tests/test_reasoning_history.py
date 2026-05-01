"""Tests for reasoning/thinking rehydration in thread history."""

from __future__ import annotations

from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from nymeria.core.agent import NymeriaAgent


class FakeGraph:
    def __init__(self, messages):
        self._messages = messages

    def get_state(self, _config):
        return SimpleNamespace(values={"messages": self._messages})

    def get_state_history(self, _config, limit=None):
        return iter(())


def _history_for(messages):
    agent = object.__new__(NymeriaAgent)
    agent._default_graph = FakeGraph(messages)
    agent._clean_tool_result_for_display = lambda value: value
    agent._extract_workspace_artifacts = lambda _value: []
    return NymeriaAgent.get_conversation_history(agent, "thread-1")


def test_history_rehydrates_reasoning_content_metadata():
    history = _history_for([
        HumanMessage(content="Hello"),
        AIMessage(
            content="Final answer",
            additional_kwargs={"reasoning_content": "Saved reasoning"},
        ),
    ])

    assistant = history[1]
    assert assistant["content"] == "Final answer"
    assert assistant["steps"] == [
        {"type": "thinking", "content": "Saved reasoning"},
        {"type": "response", "content": "Final answer"},
    ]
    assert assistant["intermediate_content"] == "Saved reasoning"


def test_history_rehydrates_legacy_reasoning_metadata_key():
    history = _history_for([
        HumanMessage(content="Hello"),
        AIMessage(
            content="Final answer",
            additional_kwargs={"reasoning": "Alternate reasoning"},
        ),
    ])

    assert history[1]["steps"][0] == {
        "type": "thinking",
        "content": "Alternate reasoning",
    }


def test_history_rehydrates_openrouter_reasoning_details_metadata():
    history = _history_for([
        HumanMessage(content="Hello"),
        AIMessage(
            content="Final answer",
            additional_kwargs={
                "reasoning_details": [
                    {
                        "type": "reasoning.text",
                        "text": "Saved ",
                        "format": "unknown",
                        "index": 0,
                    },
                    {
                        "type": "reasoning.text",
                        "text": "details",
                        "format": "unknown",
                        "index": 0,
                    },
                ],
            },
        ),
    ])

    assert history[1]["steps"] == [
        {"type": "thinking", "content": "Saved details"},
        {"type": "response", "content": "Final answer"},
    ]


def test_history_deduplicates_openrouter_reasoning_details_and_string():
    history = _history_for([
        HumanMessage(content="Hello"),
        AIMessage(
            content="Final answer",
            additional_kwargs={
                "reasoning_details": [
                    {
                        "type": "reasoning.text",
                        "text": "Same thought",
                        "format": "unknown",
                        "index": 0,
                    },
                ],
                "reasoning_content": "Same thought",
            },
        ),
    ])

    thinking_steps = [
        step for step in history[1]["steps"] if step["type"] == "thinking"
    ]
    assert thinking_steps == [{"type": "thinking", "content": "Same thought"}]


def test_history_keeps_anthropic_typed_thinking_without_duplication():
    history = _history_for([
        HumanMessage(content="Hello"),
        AIMessage(content=[
            {"type": "thinking", "thinking": "Claude thought"},
            {"type": "text", "text": "Claude answer"},
        ]),
    ])

    assistant = history[1]
    thinking_steps = [
        step for step in assistant["steps"]
        if step["type"] == "thinking"
    ]
    assert thinking_steps == [{"type": "thinking", "content": "Claude thought"}]
    assert assistant["steps"][-1] == {
        "type": "response",
        "content": "Claude answer",
    }


def test_history_rehydrates_responses_reasoning_summary_block():
    history = _history_for([
        HumanMessage(content="Hello"),
        AIMessage(content=[
            {
                "type": "reasoning",
                "summary": [
                    {"type": "summary_text", "text": "Responses thought"},
                ],
            },
            {"type": "text", "text": "Responses answer"},
        ]),
    ])

    assistant = history[1]
    assert assistant["steps"] == [
        {"type": "thinking", "content": "Responses thought"},
        {"type": "response", "content": "Responses answer"},
    ]


def test_history_keeps_one_responses_reasoning_block_as_one_thinking_step():
    history = _history_for([
        HumanMessage(content="Hello"),
        AIMessage(
            content=[
                {
                    "type": "reasoning",
                    "summary": [
                        {"type": "summary_text", "text": "First section"},
                        {"type": "summary_text", "text": "Second section"},
                        {"type": "summary_text", "text": "Third section"},
                    ],
                },
                {
                    "type": "function_call",
                    "name": "tool_create",
                    "call_id": "call-1",
                    "arguments": '{"action": "draft"}',
                },
            ],
            tool_calls=[{
                "id": "call-1",
                "name": "tool_create",
                "args": {"action": "draft"},
            }],
        ),
        ToolMessage(content="drafted", tool_call_id="call-1"),
        AIMessage(content="Done"),
    ])

    assistant = history[1]
    assert [step["type"] for step in assistant["steps"]] == [
        "thinking",
        "tool_call",
        "response",
    ]
    assert assistant["steps"][0] == {
        "type": "thinking",
        "content": "First section\n\nSecond section\n\nThird section",
    }
    assert assistant["steps"][1]["name"] == "tool_create"


def test_history_rehydrates_responses_reasoning_content_block():
    history = _history_for([
        HumanMessage(content="Hello"),
        AIMessage(content=[
            {
                "type": "reasoning",
                "content": [
                    {
                        "type": "reasoning_text",
                        "text": "Claude Responses thought",
                    },
                ],
                "summary": [],
            },
            {"type": "text", "text": "Responses answer"},
        ]),
    ])

    assistant = history[1]
    assert assistant["steps"] == [
        {"type": "thinking", "content": "Claude Responses thought"},
        {"type": "response", "content": "Responses answer"},
    ]


def test_history_strips_inline_thinking_leaked_as_text():
    history = _history_for([
        HumanMessage(content="Hello"),
        AIMessage(content=[
            {
                "type": "reasoning",
                "summary": [],
            },
            {
                "type": "text",
                "text": "private chain</think>\n\nVisible answer",
            },
        ]),
    ])

    assistant = history[1]
    assert assistant["content"] == "Visible answer"
    assert "private chain" not in assistant["content"]
    assert "steps" not in assistant


def test_history_without_reasoning_metadata_stays_legacy_shaped():
    history = _history_for([
        HumanMessage(content="Hello"),
        AIMessage(content="Plain answer"),
    ])

    assistant = history[1]
    assert assistant["content"] == "Plain answer"
    assert "steps" not in assistant
    assert "intermediate_content" not in assistant


def test_history_rehydrates_reasoning_before_tool_call_steps():
    history = _history_for([
        HumanMessage(content="Hello"),
        AIMessage(
            content="",
            additional_kwargs={"reasoning_content": "Need a tool"},
            tool_calls=[{
                "id": "call-1",
                "name": "search",
                "args": {"q": "nymeria"},
            }],
        ),
        ToolMessage(content="tool result", tool_call_id="call-1"),
        AIMessage(content="Done"),
    ])

    assistant = history[1]
    assert [step["type"] for step in assistant["steps"]] == [
        "thinking",
        "tool_call",
        "response",
    ]
    assert assistant["steps"][0]["content"] == "Need a tool"
    assert assistant["steps"][1]["name"] == "search"
    assert assistant["steps"][2]["content"] == "Done"


def test_history_rehydrates_responses_function_call_steps():
    history = _history_for([
        HumanMessage(content="Hello"),
        AIMessage(
            content=[
                {
                    "type": "reasoning",
                    "summary": [
                        {"type": "summary_text", "text": "Need a tool"},
                    ],
                },
                {
                    "type": "function_call",
                    "name": "search",
                    "call_id": "call-1",
                    "arguments": '{"q": "nymeria"}',
                },
            ],
            tool_calls=[{
                "id": "call-1",
                "name": "search",
                "args": {"q": "nymeria"},
            }],
        ),
        ToolMessage(content="tool result", tool_call_id="call-1"),
        AIMessage(content="Done"),
    ])

    assistant = history[1]
    assert [step["type"] for step in assistant["steps"]] == [
        "thinking",
        "tool_call",
        "response",
    ]
    assert assistant["steps"][0]["content"] == "Need a tool"
    assert assistant["steps"][1]["id"] == "call-1"
    assert assistant["steps"][1]["arguments"] == {"q": "nymeria"}
    assert assistant["steps"][2]["content"] == "Done"


def test_history_treats_string_blocks_before_tool_calls_as_response_text():
    history = _history_for([
        HumanMessage(content="Hello"),
        AIMessage(
            content=[
                "I'll check that now.",
                {
                    "type": "function_call",
                    "name": "search",
                    "call_id": "call-1",
                    "arguments": '{"q": "nymeria"}',
                },
            ],
            tool_calls=[{
                "id": "call-1",
                "name": "search",
                "args": {"q": "nymeria"},
            }],
        ),
        ToolMessage(content="tool result", tool_call_id="call-1"),
        AIMessage(content="Done"),
    ])

    assistant = history[1]
    assert [step["type"] for step in assistant["steps"]] == [
        "response",
        "tool_call",
        "response",
    ]
    assert assistant["steps"][0]["content"] == "I'll check that now."
    assert assistant["intermediate_content"] is None


def test_history_shows_compaction_marker_as_notice():
    history = _history_for([
        HumanMessage(
            content="Context compacted",
            additional_kwargs={
                "internal": True,
                "internal_type": "compaction_marker",
                "summary": "Important prior context.",
                "messages_removed": 12,
                "auto_resumed": True,
                "timestamp": "2026-04-30T00:00:00Z",
            },
        ),
    ])

    assert history == [
        {
            "id": "thread-1-1",
            "role": "system",
            "kind": "compaction_notice",
            "content": "Context compacted",
            "context_summary": "Important prior context.",
            "messages_removed": 12,
            "auto_resumed": True,
            "timestamp": "2026-04-30T00:00:00Z",
        }
    ]


def test_history_hides_auto_resume_prompt_but_keeps_resumed_output():
    history = _history_for([
        HumanMessage(
            content="Context compacted",
            additional_kwargs={
                "internal": True,
                "internal_type": "compaction_marker",
                "summary": "Important prior context.",
                "messages_removed": 12,
                "auto_resumed": True,
            },
        ),
        HumanMessage(
            content="[Auto-compact resume prompt with summary]",
            additional_kwargs={"internal": True, "internal_type": "auto_resume"},
        ),
        AIMessage(content="Continuing after compaction."),
    ])

    assert [item["kind"] for item in history if item.get("kind")] == [
        "compaction_notice"
    ]
    assert history[1]["role"] == "assistant"
    assert history[1]["content"] == "Continuing after compaction."
