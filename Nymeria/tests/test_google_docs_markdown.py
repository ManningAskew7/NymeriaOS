"""Unit tests for the inline markdown parser in ``tools/google_docs.py``.

These cover ``_parse_inline`` after F4 (slice 16): the bold/italic-only parser
``_parse_inline_simple`` was merged into ``_parse_inline`` via the
``allow_links`` keyword. The cases below pin the observable behavior (plain
text + span offsets) so the merge is provably behavior-preserving and future
edits to the asterisk-scanning state machine stay covered.
"""

from nymeria.tools.google_docs import (
    InlineSpan,
    _extract_markdown,
    _parse_inline,
    _parse_markdown,
)


def _docs_table(rows):
    """Build a minimal Google Docs API document holding one table of ``rows``."""
    return {
        "body": {
            "content": [
                {
                    "table": {
                        "tableRows": [
                            {
                                "tableCells": [
                                    {
                                        "content": [
                                            {
                                                "paragraph": {
                                                    "elements": [
                                                        {"textRun": {"content": cell}}
                                                    ]
                                                }
                                            }
                                        ]
                                    }
                                    for cell in row
                                ]
                            }
                            for row in rows
                        ]
                    }
                }
            ]
        }
    }


def _spans_as_tuples(spans):
    return [(s.start, s.end, s.bold, s.italic, s.link_url) for s in spans]


# --- bold / italic / bold-italic ------------------------------------------


def test_bold_only():
    plain, spans = _parse_inline("**bold**")
    assert plain == "bold"
    assert _spans_as_tuples(spans) == [(0, 4, True, False, None)]


def test_italic_only():
    plain, spans = _parse_inline("*it*")
    assert plain == "it"
    assert _spans_as_tuples(spans) == [(0, 2, False, True, None)]


def test_bold_italic_combined():
    plain, spans = _parse_inline("***bi***")
    assert plain == "bi"
    assert _spans_as_tuples(spans) == [(0, 2, True, True, None)]


def test_plain_text_has_no_spans():
    plain, spans = _parse_inline("just words")
    assert plain == "just words"
    assert spans == []


def test_span_offsets_account_for_surrounding_text():
    # The marker characters are stripped, so the bold span must point at the
    # offsets within the *plain* output, not the raw markdown.
    plain, spans = _parse_inline("a **b** c")
    assert plain == "a b c"
    assert _spans_as_tuples(spans) == [(2, 3, True, False, None)]


def test_multiple_spans_in_one_line():
    plain, spans = _parse_inline("**x** and *y*")
    assert plain == "x and y"
    assert _spans_as_tuples(spans) == [
        (0, 1, True, False, None),
        (6, 7, False, True, None),
    ]


# --- links -----------------------------------------------------------------


def test_plain_link():
    plain, spans = _parse_inline("[text](http://x)")
    assert plain == "text"
    assert _spans_as_tuples(spans) == [(0, 4, False, False, "http://x")]


def test_bold_inside_link_offsets_correctly():
    # Regression case called out in the F4 finding: a link whose text contains
    # bold formatting must emit both a link span and a correctly-offset bold
    # span over the same plain-text range.
    plain, spans = _parse_inline("[**bold link**](http://x)")
    assert plain == "bold link"
    assert _spans_as_tuples(spans) == [
        (0, 9, False, False, "http://x"),
        (0, 9, True, False, None),
    ]


def test_italic_inside_link_offsets_correctly():
    plain, spans = _parse_inline("before [*hi*](u) after")
    assert plain == "before hi after"
    assert _spans_as_tuples(spans) == [
        (7, 9, False, False, "u"),
        (7, 9, False, True, None),
    ]


def test_bracket_inside_link_text_stays_literal():
    # The inner parse runs with allow_links=False, so a stray '[' inside the
    # link text is emitted literally and produces no nested link span.
    plain, spans = _parse_inline("[a [ b](u)")
    assert plain == "a [ b"
    assert _spans_as_tuples(spans) == [(0, 5, False, False, "u")]


def test_bold_italic_inside_link_offsets_correctly():
    # The most offset-sensitive combination: bold+italic inside link text.
    plain, spans = _parse_inline("[***bi***](u)")
    assert plain == "bi"
    assert _spans_as_tuples(spans) == [
        (0, 2, False, False, "u"),
        (0, 2, True, True, None),
    ]


def test_top_level_nested_link_does_not_recurse():
    # Anti-recursion contract through the DEFAULT (allow_links=True) path: a
    # link whose text itself looks like markdown is parsed exactly once. The
    # inner recursive parse runs with allow_links=False, so no second link span
    # is produced and parsing terminates without error.
    plain, spans = _parse_inline("[outer [inner](u2)](u1)")
    assert plain == "outer [inner](u1)"
    assert _spans_as_tuples(spans) == [(0, 12, False, False, "u2")]


# --- allow_links=False (former _parse_inline_simple behavior) ---------------


def test_allow_links_false_skips_link_branch():
    plain, spans = _parse_inline("[x](y)", allow_links=False)
    assert plain == "[x](y)"
    assert spans == []


def test_allow_links_false_still_parses_bold_and_italic():
    plain, spans = _parse_inline("**b** *i*", allow_links=False)
    assert plain == "b i"
    assert _spans_as_tuples(spans) == [
        (0, 1, True, False, None),
        (2, 3, False, True, None),
    ]


# --- integration through _parse_markdown -----------------------------------


def test_parse_markdown_paragraph_carries_inline_spans():
    blocks = _parse_markdown("Hello **world**")
    assert len(blocks) == 1
    block = blocks[0]
    assert block.kind == "paragraph"
    assert block.text == "Hello world"
    assert _spans_as_tuples(block.spans) == [(6, 11, True, False, None)]


def test_parse_markdown_heading_with_link():
    blocks = _parse_markdown("# Title [docs](http://d)")
    assert len(blocks) == 1
    block = blocks[0]
    assert block.kind == "heading"
    assert block.level == 1
    assert block.text == "Title docs"
    assert InlineSpan(start=6, end=10, link_url="http://d") in block.spans


# --- table extraction via the shared helper (slice 16 F8) ------------------


def test_extract_markdown_renders_table_as_gfm():
    out = _extract_markdown(_docs_table([["Name", "Role"], ["Alice", "Dev"]]))

    # Exact output pins the trailing-newline glue around the table block: the
    # helper result + "\n" then the inter-element "\n", matching the old emitter.
    assert out == "| Name | Role |\n| --- | --- |\n| Alice | Dev |\n\n"


def test_extract_markdown_escapes_pipe_in_table_cell():
    # The shared rows_to_markdown_table helper now escapes pipes that previously
    # corrupted the rendered table (F8 latent-correctness fix).
    out = _extract_markdown(_docs_table([["Cmd"], ["a | b"]]))

    assert "| a \\| b |" in out
    assert "| a | b |" not in out
