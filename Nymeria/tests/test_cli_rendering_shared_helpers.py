from __future__ import annotations

from nymeria.triggers.cli.rendering.shared_helpers import (
    _assistant_response_lengths,
    _dispatch_reference_text,
)
from nymeria.triggers.cli.state import AssistantMessage, CLIUIState, ResponseStep


class TestDispatchReferenceText:
    def test_prefers_stripped_content(self) -> None:
        msg = AssistantMessage(
            id="m1",
            dispatch_info={"content": "  hello  ", "title": "T", "thread_id": "x"},
        )
        assert _dispatch_reference_text(msg) == "hello"

    def test_falls_back_to_title(self) -> None:
        msg = AssistantMessage(id="m1", dispatch_info={"title": " Daily ", "thread_id": "x"})
        assert _dispatch_reference_text(msg) == "Response from Daily"

    def test_falls_back_to_thread_id(self) -> None:
        msg = AssistantMessage(id="m1", dispatch_info={"thread_id": " t-9 "})
        assert _dispatch_reference_text(msg) == "Response from t-9"

    def test_empty_when_no_dispatch_fields(self) -> None:
        assert _dispatch_reference_text(AssistantMessage(id="m1", dispatch_info={})) == ""


class TestAssistantResponseLengths:
    def test_maps_message_id_to_total_response_length(self) -> None:
        m1 = AssistantMessage(
            id="m1", steps=(ResponseStep(content="abc"), ResponseStep(content="de"))
        )
        m2 = AssistantMessage(id="m2", steps=(ResponseStep(content=""),))
        state = CLIUIState(messages=(m1, m2))
        assert _assistant_response_lengths(state) == {"m1": 5, "m2": 0}

    def test_empty_state_yields_empty_map(self) -> None:
        assert _assistant_response_lengths(CLIUIState()) == {}
