from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
MOBILE_LIB = REPO_ROOT / "nymeria-mobile" / "src" / "lib"


def read_mobile(path: str) -> str:
    return (MOBILE_LIB / path).read_text()


def test_mobile_chat_store_does_not_materialize_legacy_message_fields() -> None:
    source = read_mobile("stores/chat.svelte.ts")

    forbidden_store_writes = [
        "intermediateContent:",
        "toolCalls:",
        "setIntermediateContent",
        "_computeIntermediateContent",
        "_computeToolCalls",
    ]

    for forbidden in forbidden_store_writes:
        assert forbidden not in source


def test_mobile_history_keeps_legacy_fields_only_for_stepless_records() -> None:
    source = read_mobile("services/api/threads.ts")

    assert "const hasSteps = !!steps?.length;" in source
    assert "...(!hasSteps ? {" in source
    assert "intermediateContent: m.intermediate_content as string | undefined" in source
    assert "toolCalls: legacyToolCalls" in source


def test_mobile_message_bubble_derives_legacy_fallbacks_at_render_time() -> None:
    source = read_mobile("components/chat/MessageBubble.svelte")

    assert "function thinkingContentFromSteps" in source
    assert "function toolCallsFromSteps" in source
    assert "message.intermediateContent || thinkingContentFromSteps(message.steps)" in source
    assert "message.toolCalls && message.toolCalls.length > 0" in source
    assert "toolCallsFromSteps(message.steps)" in source
