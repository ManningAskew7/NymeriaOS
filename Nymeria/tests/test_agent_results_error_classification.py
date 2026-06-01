"""Tests for classify_stream_exception in nymeria.core.agent_results.

Covers the provider-error -> frontend payload mapping, including the Anthropic
subscription "third-party / extra usage" 400 that is really a tool-name
collision (e.g. a tool named exactly like a known MCP/Exa integration tool).
"""

from nymeria.core.agent_results import classify_stream_exception


# --- Anthropic third-party / extra-usage 400 (tool-name collision) ------------

# The exact provider text seen when a bound tool name collides with a known
# Anthropic integration tool (e.g. the old `web_search_exa`).
_THIRD_PARTY_400 = (
    "Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', "
    "'message': 'Third-party apps now draw from your extra usage, not your plan "
    "limits. Add more at claude.ai/settings/usage and keep going.'}, "
    "'request_id': 'req_011CbbpNTiH5e9oC782gjNww'}"
)


def test_third_party_extra_usage_maps_to_tool_name_hint():
    result = classify_stream_exception(Exception(_THIRD_PARTY_400))

    assert result["type"] == "error"
    assert result["code"] == "anthropic_third_party_tool_name"
    # Actionable, not the raw provider text.
    assert "tool-name collision" in result["content"]
    assert "rename" in result["content"].lower()
    assert "An error occurred" not in result["content"]
    assert result["details"]["http_status"] == 400
    assert result["details"]["hint"] == "tool_name_collision"


def test_third_party_detection_is_phrase_based_not_status_only():
    # A plain 400 without the third-party/extra-usage wording must NOT be
    # misclassified as a tool-name collision.
    result = classify_stream_exception(
        Exception("Error code: 400 - {'message': 'temperature must be <= 1'}")
    )
    assert result["code"] == "agent_runtime_error"


def test_extra_usage_phrase_alone_matches():
    # Robust to minor wording changes that keep the distinctive phrase.
    result = classify_stream_exception(
        Exception("400 - you must draw from your extra usage to continue")
    )
    assert result["code"] == "anthropic_third_party_tool_name"


# --- Regression guards for the other branches ---------------------------------


def test_openrouter_402_still_classified():
    err = Exception(
        "Error code: 402 - This request requires more credits. Requested up to "
        "8192 tokens, but can only afford 1200."
    )
    result = classify_stream_exception(err)
    assert result["code"] == "openrouter_insufficient_credits"
    assert result["details"]["http_status"] == 402
    assert result["details"]["requested_max_tokens"] == 8192
    assert result["details"]["affordable_max_tokens"] == 1200


def test_generic_error_falls_back_to_runtime_error():
    result = classify_stream_exception(Exception("kaboom"))
    assert result["code"] == "agent_runtime_error"
    assert result["content"] == "An error occurred: kaboom"
