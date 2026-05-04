"""Tests for provider-agnostic token usage extraction.

Covers every known provider metadata shape (Anthropic, OpenAI, OpenRouter,
CLIProxy, LangChain standardised) plus edge cases like empty messages,
mixed providers, and missing metadata.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage

from nymeria.core.token_usage import extract_from_message, extract_last_from_messages


# ---------------------------------------------------------------------------
# Fixtures — one per provider metadata shape
# ---------------------------------------------------------------------------

def _ai(*, usage_metadata=None, response_metadata=None, content="ok"):
    msg = AIMessage(content=content)
    if usage_metadata is not None:
        msg.usage_metadata = usage_metadata
    if response_metadata is not None:
        msg.response_metadata = response_metadata
    return msg


LANGCHAIN_STANDARD = _ai(usage_metadata={"input_tokens": 100, "output_tokens": 50})
ANTHROPIC_NATIVE = _ai(response_metadata={"usage": {"input_tokens": 200, "output_tokens": 80}})
OPENAI_NATIVE = _ai(response_metadata={"token_usage": {"prompt_tokens": 300, "completion_tokens": 120}})
OPENROUTER = _ai(response_metadata={"token_usage": {"prompt_tokens": 400, "completion_tokens": 160}})
CLIPROXY_ANTHROPIC = _ai(
    usage_metadata={"input_tokens": 500, "output_tokens": 200},
    response_metadata={"usage": {"input_tokens": 500, "output_tokens": 200}},
)
NO_METADATA = _ai()
ZERO_TOKENS = _ai(usage_metadata={"input_tokens": 0, "output_tokens": 0})


# ---------------------------------------------------------------------------
# extract_from_message — single message
# ---------------------------------------------------------------------------

class TestExtractFromMessage:

    def test_langchain_standard(self):
        assert extract_from_message(LANGCHAIN_STANDARD) == (100, 50)

    def test_anthropic_native(self):
        assert extract_from_message(ANTHROPIC_NATIVE) == (200, 80)

    def test_openai_native(self):
        assert extract_from_message(OPENAI_NATIVE) == (300, 120)

    def test_openrouter(self):
        assert extract_from_message(OPENROUTER) == (400, 160)

    def test_cliproxy_with_both_metadata(self):
        """CLIProxy populates both usage_metadata and response_metadata;
        usage_metadata should win."""
        assert extract_from_message(CLIPROXY_ANTHROPIC) == (500, 200)

    def test_no_metadata_returns_zero(self):
        assert extract_from_message(NO_METADATA) == (0, 0)

    def test_zero_tokens_returns_zero(self):
        assert extract_from_message(ZERO_TOKENS) == (0, 0)

    def test_input_only(self):
        msg = _ai(usage_metadata={"input_tokens": 42, "output_tokens": 0})
        assert extract_from_message(msg) == (42, 0)

    def test_output_only(self):
        msg = _ai(usage_metadata={"input_tokens": 0, "output_tokens": 17})
        assert extract_from_message(msg) == (0, 17)

    def test_usage_metadata_preferred_over_response_metadata(self):
        """When both are present but disagree, usage_metadata wins."""
        msg = _ai(
            usage_metadata={"input_tokens": 1, "output_tokens": 2},
            response_metadata={"usage": {"input_tokens": 99, "output_tokens": 99}},
        )
        assert extract_from_message(msg) == (1, 2)

    def test_anthropic_fallback_when_usage_metadata_zero(self):
        """Falls through to response_metadata when usage_metadata is all zeros."""
        msg = _ai(
            usage_metadata={"input_tokens": 0, "output_tokens": 0},
            response_metadata={"usage": {"input_tokens": 10, "output_tokens": 5}},
        )
        assert extract_from_message(msg) == (10, 5)

    def test_openai_fallback_when_anthropic_empty(self):
        msg = _ai(response_metadata={
            "usage": {},
            "token_usage": {"prompt_tokens": 77, "completion_tokens": 33},
        })
        assert extract_from_message(msg) == (77, 33)

    def test_empty_response_metadata(self):
        msg = _ai(response_metadata={})
        assert extract_from_message(msg) == (0, 0)


# ---------------------------------------------------------------------------
# extract_last_from_messages — message list
# ---------------------------------------------------------------------------

class TestExtractLastFromMessages:

    def test_finds_last_ai_message(self):
        msgs = [
            HumanMessage(content="hi"),
            _ai(usage_metadata={"input_tokens": 10, "output_tokens": 5}),
            HumanMessage(content="again"),
            _ai(usage_metadata={"input_tokens": 20, "output_tokens": 10}),
        ]
        assert extract_last_from_messages(msgs) == (20, 10)

    def test_skips_ai_without_tokens(self):
        msgs = [
            _ai(usage_metadata={"input_tokens": 10, "output_tokens": 5}),
            _ai(),  # no metadata
        ]
        assert extract_last_from_messages(msgs) == (10, 5)

    def test_empty_list(self):
        assert extract_last_from_messages([]) == (0, 0)

    def test_only_human_messages(self):
        msgs = [HumanMessage(content="hello"), HumanMessage(content="world")]
        assert extract_last_from_messages(msgs) == (0, 0)

    def test_only_zero_token_ai(self):
        msgs = [ZERO_TOKENS, ZERO_TOKENS]
        assert extract_last_from_messages(msgs) == (0, 0)

    def test_mixed_providers(self):
        """Anthropic followed by OpenAI — picks the later OpenAI message."""
        msgs = [
            ANTHROPIC_NATIVE,
            OPENAI_NATIVE,
        ]
        assert extract_last_from_messages(msgs) == (300, 120)

    def test_single_ai_message(self):
        assert extract_last_from_messages([LANGCHAIN_STANDARD]) == (100, 50)

    def test_trailing_human_after_ai(self):
        msgs = [
            _ai(usage_metadata={"input_tokens": 15, "output_tokens": 7}),
            HumanMessage(content="thanks"),
        ]
        assert extract_last_from_messages(msgs) == (15, 7)
