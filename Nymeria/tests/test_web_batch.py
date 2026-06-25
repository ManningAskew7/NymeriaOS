"""Tests for the shared query-batch helpers in tools/web_batch.py.

These lock the byte-for-byte contract the web_search_* and web fetch tools rely
on: the single/batch parse rules, the cap, the empty-input error string, and the
"=== <label> N/M: <item> ===" batch header shape.
"""

from nymeria.tools.web_batch import (
    MAX_BATCH_QUERIES,
    parse_batch_queries,
    run_batched,
)


# --- parse_batch_queries ------------------------------------------------------


def test_parse_single_query():
    assert parse_batch_queries("hello", "") == (["hello"], None)


def test_parse_single_query_is_stripped():
    assert parse_batch_queries("  hello  ", "") == (["hello"], None)


def test_parse_batch_splits_trims_and_drops_empties():
    # Pipe-separated; each part trimmed; empty segments dropped.
    assert parse_batch_queries("", "a | b |  | c") == (["a", "b", "c"], None)


def test_parse_batch_takes_precedence_over_single():
    assert parse_batch_queries("ignored", "a | b") == (["a", "b"], None)


def test_parse_caps_at_max_n():
    items = " | ".join(str(i) for i in range(15))
    query_list, error = parse_batch_queries("", items, max_n=10)
    assert error is None
    assert query_list == [str(i) for i in range(10)]


def test_parse_default_cap_is_max_batch_queries():
    items = " | ".join(str(i) for i in range(MAX_BATCH_QUERIES + 5))
    query_list, error = parse_batch_queries("", items)
    assert error is None
    assert len(query_list) == MAX_BATCH_QUERIES


def test_parse_empties_do_not_count_toward_cap():
    # Empties are dropped BEFORE the cap, so interleaved blanks never crowd out
    # real queries: 4 real + 4 blanks with max_n=3 keeps the first 3 real ones.
    query_list, error = parse_batch_queries(
        "", "a |  | b |  | c |  | d |  ", max_n=3
    )
    assert error is None
    assert query_list == ["a", "b", "c"]


def test_parse_empty_returns_error_string():
    assert parse_batch_queries("", "") == (
        [],
        "[Error]: Provide a query or pipe-separated queries.",
    )


def test_parse_whitespace_only_is_treated_as_empty():
    assert parse_batch_queries("   ", "   ") == (
        [],
        "[Error]: Provide a query or pipe-separated queries.",
    )


# --- run_batched --------------------------------------------------------------


def test_run_single_returns_runner_output_with_no_header():
    assert run_batched(["x"], lambda q: f"R({q})") == "R(x)"


def test_run_batch_emits_query_headers_joined_by_blank_line():
    out = run_batched(["a", "b"], lambda q: f"R({q})")
    assert out == "=== Query 1/2: a ===\nR(a)\n\n=== Query 2/2: b ===\nR(b)"


def test_run_batch_custom_label():
    out = run_batched(["u1", "u2"], lambda q: "res", label="URL")
    assert out == "=== URL 1/2: u1 ===\nres\n\n=== URL 2/2: u2 ===\nres"


def test_run_passes_raw_item_to_both_header_and_runner():
    # Providers that rewrite the item (e.g. add a site: filter) do so inside the
    # runner; the header must still show the raw item.
    seen: list[str] = []

    def runner(item: str) -> str:
        seen.append(item)
        return f"rewritten:{item}+filter"

    out = run_batched(["alpha", "beta"], runner)
    assert seen == ["alpha", "beta"]
    assert "=== Query 1/2: alpha ===" in out
    assert "rewritten:alpha+filter" in out


def test_run_single_does_not_emit_any_header():
    out = run_batched(["only"], lambda q: "body", label="URL")
    assert out == "body"
    assert "===" not in out
