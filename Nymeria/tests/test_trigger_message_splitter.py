"""Regression tests for shared trigger message splitting."""

from __future__ import annotations

import pytest

from nymeria.triggers.discord_bot import split_message as split_discord_alias
from nymeria.triggers.message_splitter import (
    split_discord_message,
    split_googlechat_message,
    split_line_message,
    split_markdown_message,
    split_matrix_message,
    split_messenger_message,
    split_mattermost_message,
    split_plain_message,
    split_rocketchat_message,
    split_signal_message,
    split_slack_message,
    split_telegram_message,
    split_teams_message,
    split_twitch_message,
    split_webex_message,
    split_whatsapp_message,
    split_zulip_message,
)
from nymeria.triggers.google_chat_bot import split_message as split_googlechat_alias
from nymeria.triggers.line_bot import split_message as split_line_alias
from nymeria.triggers.messenger_bot import split_message as split_messenger_alias
from nymeria.triggers.rocketchat_bot import split_message as split_rocketchat_alias
from nymeria.triggers.signal_bot import split_message as split_signal_alias
from nymeria.triggers.slack_bot import split_message as split_slack_alias
from nymeria.triggers.teams_bot import split_message as split_teams_alias
from nymeria.triggers.matrix_bot import split_message as split_matrix_alias
from nymeria.triggers.mattermost_bot import split_message as split_mattermost_alias
from nymeria.triggers.telegram_bot import split_message as split_telegram_alias
from nymeria.triggers.webex_bot import split_message as split_webex_alias
from nymeria.triggers.whatsapp_bot import split_message as split_whatsapp_alias
from nymeria.triggers.zulip_bot import split_message as split_zulip_alias


def test_markdown_splitter_preserves_under_limit_and_empty_text():
    assert split_markdown_message("", 10) == [""]
    assert split_markdown_message("short", 10) == ["short"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("AAAAAA\n\nBBBBBB", ["AAAAAA", "BBBBBB"]),
        ("AAAAAAA\nBBBBBB", ["AAAAAAA", "BBBBBB"]),
        ("One two. Three four.", ["One two.", "Three four."]),
    ],
)
def test_markdown_splitter_prefers_readable_boundaries(text: str, expected: list[str]):
    assert split_markdown_message(text, 12) == expected


def test_markdown_splitter_hard_splits_when_no_boundary_exists():
    assert split_markdown_message("ABCDEFGHIJ", 4) == ["ABCD", "EFGH", "IJ"]


def test_markdown_splitter_keeps_complete_code_block_together_when_possible():
    text = "Intro\n\n```python\nprint('hello')\n```\n\nTail"

    chunks = split_markdown_message(text, len(text) - 2)

    assert "```python\nprint('hello')\n```" in chunks[0]
    assert all(chunk.count("```") % 2 == 0 for chunk in chunks)


def test_plain_splitter_prefers_sentence_then_word_boundaries():
    assert split_plain_message("One two. Three four.", 12) == ["One two.", "Three four."]
    assert split_plain_message("hello world again", 12) == ["hello world", "again"]


def test_plain_splitter_hard_splits_when_no_boundary_exists():
    assert split_plain_message("ABCDEFGHIJ", 4) == ["ABCD", "EFGH", "IJ"]


def test_platform_aliases_keep_existing_defaults_and_import_surfaces():
    assert split_discord_alias is split_discord_message
    assert split_telegram_alias is split_telegram_message
    assert split_slack_alias is split_slack_message
    assert split_matrix_alias is split_matrix_message
    assert split_mattermost_alias is split_mattermost_message
    assert split_whatsapp_alias is split_whatsapp_message
    assert split_messenger_alias is split_messenger_message
    assert split_webex_alias is split_webex_message
    assert split_zulip_alias is split_zulip_message
    assert split_rocketchat_alias is split_rocketchat_message
    assert split_teams_alias is split_teams_message
    assert split_googlechat_alias is split_googlechat_message
    assert split_line_alias is split_line_message
    assert split_signal_alias is split_signal_message

    assert max(map(len, split_discord_message("x" * 2001))) <= 2000
    assert max(map(len, split_telegram_message("x" * 4097))) <= 4096
    assert max(map(len, split_twitch_message("x" * 491))) <= 490
    assert max(map(len, split_slack_message("x" * 3501))) <= 3500
    assert max(map(len, split_matrix_message("x" * 3501))) <= 3500
    assert max(map(len, split_mattermost_message("x" * 4001))) <= 4000
    assert max(map(len, split_whatsapp_message("x" * 4097))) <= 4096
    assert max(map(len, split_messenger_message("x" * 2001))) <= 2000
    assert max(map(len, split_webex_message("x" * 3501))) <= 3500
    assert max(map(len, split_zulip_message("x" * 4001))) <= 4000
    assert max(map(len, split_rocketchat_message("x" * 4001))) <= 4000
    assert max(map(len, split_teams_message("x" * 4001))) <= 4000
    assert max(map(len, split_googlechat_message("x" * 4001))) <= 4000
    assert max(map(len, split_line_message("x" * 5001))) <= 5000
    assert max(map(len, split_signal_message("x" * 8001))) <= 8000


def test_splitters_reject_invalid_limits():
    with pytest.raises(ValueError, match="max_length"):
        split_markdown_message("text", 0)
    with pytest.raises(ValueError, match="max_length"):
        split_plain_message("text", 0)
