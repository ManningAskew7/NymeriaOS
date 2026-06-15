"""The compaction token estimate must size images by token cost, not base64 length."""

from __future__ import annotations

from langchain_core.messages import HumanMessage

from nymeria.config.model_capabilities import _DEFAULT_IMAGE_TOKENS
from nymeria.core.agent_compaction import _estimate_content_tokens


def _huge_data_url() -> str:
    # ~5 MB of base64 -> ~1.25M characters; str(content)//4 would be ~300k+ tokens.
    return "data:image/png;base64," + ("A" * 5_000_000)


def test_image_block_counted_by_token_cost_not_base64_length():
    content = [
        {"type": "text", "text": "what is in this picture?"},
        {"type": "image_url", "image_url": {"url": _huge_data_url()}},
    ]
    tokens = _estimate_content_tokens(content, model="claude-opus-4-8")
    # The image contributes the flat per-image estimate (dims unknown here), not
    # the length of its multi-megabyte base64 string.
    text_tokens = len("what is in this picture?") // 4
    assert tokens == text_tokens + _DEFAULT_IMAGE_TOKENS
    # Sanity: nowhere near the 1M+ tokens a base64-length count would produce.
    assert tokens < 5000


def test_plain_text_content_unchanged():
    msg = HumanMessage(content="just some text")
    assert _estimate_content_tokens(msg.content) == len("just some text") // 4
