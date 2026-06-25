"""Characterization tests for ``tools/google_docs.py::_execute_write_segments``.

This is the silent-index-math write path (slice 16 F10): it partitions parsed
markdown blocks into contiguous text segments and individual table segments, then
drives the Google Docs API (text via one ``batchUpdate``; tables via
``insertTable`` -> re-read -> reverse-order cell population -> re-read). It had no
direct coverage before this file.

These tests mock the ``_docs_request`` seam and pin the EXACT ordered sequence of
API request bodies and the returned ``(success, message)`` for every path, so the
F10 decomposition into ``_write_text_segment`` / ``_write_table_segment`` is provably
behavior-preserving. They are written to pass against the pre-refactor code and must
stay green, byte-for-byte, across the extraction.

``_docs_request(user_id, operation, account_id)`` receives ``operation`` as an
UNRESOLVED ``lambda s: s.documents()...execute()``. The fake resolves it against a
spy ``documents()`` service that records the request body (or a ``get``) and returns
the canned ``.execute()`` value, so payloads are inspectable.
"""

from __future__ import annotations

from typing import Any, Callable

import nymeria.tools.google_docs as gdocs
from nymeria.tools.google_docs import Block, _execute_write_segments, _text_blocks_to_requests


# --- spy / fake _docs_request harness --------------------------------------


class _Exec:
    def __init__(self, value: Any) -> None:
        self._value = value

    def execute(self) -> Any:
        return self._value


class _SpyDocs:
    """A stand-in Google Docs service that records the one call made against it.

    ``_docs_request`` invokes ``operation(service)`` exactly once per call, so each
    spy captures a single ``batchUpdate`` body or ``get`` and returns *exec_value*
    from ``.execute()`` (the canned document for a ``get``; ignored for batchUpdate).
    """

    def __init__(self, exec_value: Any, sink: list[tuple[str, Any]]) -> None:
        self._exec_value = exec_value
        self._sink = sink

    def documents(self) -> "_SpyDocs":
        return self

    def batchUpdate(self, *, documentId: str, body: dict) -> _Exec:  # noqa: N802 - mirrors google client
        self._sink.append(("batchUpdate", body))
        return _Exec(self._exec_value)

    def get(self, *, documentId: str) -> _Exec:
        self._sink.append(("get", None))
        return _Exec(self._exec_value)


def _make_fake(
    script: list[tuple[bool, Any]],
) -> tuple[Callable[..., tuple[bool, Any]], list[tuple[str, Any]]]:
    """Build a fake ``_docs_request`` driven by *script* and a shared ``recorded`` log.

    Each entry is ``(success, exec_value)``: ``exec_value`` is what ``.execute()``
    returns for that call (the canned doc for a ``get``; harmless ``{}`` for a
    batchUpdate; the error string for a failing call). The fake resolves the
    operation against a spy, appends the recorded request to ``recorded``, and
    returns ``(success, exec_value)`` to mirror the real return contract.
    """
    pending = list(script)
    recorded: list[tuple[str, Any]] = []

    def fake(user_id: str, operation: Callable[[Any], Any], account_id: Any = None) -> tuple[bool, Any]:
        assert pending, "unexpected extra _docs_request call"
        success, exec_value = pending.pop(0)
        spy = _SpyDocs(exec_value, recorded)
        operation(spy)
        return (success, exec_value)

    return fake, recorded


# --- canned-document builders ----------------------------------------------


def _table_doc(start_index: int, cell_indices: list[list[int]], end_index: int = 0) -> dict:
    """A re-read document holding one table at *start_index* with the given cell
    paragraph startIndexes (consumed by ``_find_table_at_index`` + ``_get_cell_indices``)."""
    rows = []
    for row in cell_indices:
        rows.append(
            {
                "tableCells": [
                    {"content": [{"paragraph": {"elements": [{"startIndex": idx}]}}]}
                    for idx in row
                ]
            }
        )
    return {
        "body": {
            "content": [
                {
                    "startIndex": start_index,
                    "endIndex": end_index or (start_index + 100),
                    "table": {"tableRows": rows},
                }
            ]
        }
    }


def _end_doc(end_index: int) -> dict:
    """A re-read document whose last content element has *end_index* (for the
    post-table cursor recompute ``_get_doc_end_index(doc) - 1``)."""
    return {"body": {"content": [{"endIndex": end_index}]}}


def _para(text: str) -> Block:
    return Block(kind="paragraph", text=text)


def _run(monkeypatch, script: list[tuple[bool, Any]], *args, **kwargs):
    fake, recorded = _make_fake(script)
    monkeypatch.setattr(gdocs, "_docs_request", fake)
    success, message = _execute_write_segments(*args, **kwargs)
    return success, message, recorded


# --- fast path (no tables) -------------------------------------------------


def test_fast_path_text_only(monkeypatch):
    blocks = [_para("Hello"), _para("World")]
    expected_reqs, _ = _text_blocks_to_requests(blocks, 1)

    success, message, recorded = _run(
        monkeypatch, [(True, {})], "u", "doc", blocks, 1, account_id="acct"
    )

    assert (success, message) == (True, "ok")
    assert recorded == [("batchUpdate", {"requests": expected_reqs})]


def test_fast_path_empty_blocks(monkeypatch):
    success, message, recorded = _run(monkeypatch, [], "u", "doc", [], 1)
    assert (success, message) == (True, "No content to write.")
    assert recorded == []


def test_fast_path_with_prefix_prepended(monkeypatch):
    blocks = [_para("Body")]
    prefix = [{"deleteContentRange": {"range": {"startIndex": 1, "endIndex": 5}}}]
    text_reqs, _ = _text_blocks_to_requests(blocks, 1)

    success, message, recorded = _run(
        monkeypatch, [(True, {})], "u", "doc", blocks, 1, prefix_requests=prefix
    )

    assert (success, message) == (True, "ok")
    assert recorded == [("batchUpdate", {"requests": prefix + text_reqs})]


def test_fast_path_text_batch_failure_propagates(monkeypatch):
    blocks = [_para("Hello")]
    success, message, recorded = _run(monkeypatch, [(False, "boom")], "u", "doc", blocks, 1)
    assert (success, message) == (False, "boom")
    assert len(recorded) == 1 and recorded[0][0] == "batchUpdate"


# --- table-only path -------------------------------------------------------


def test_table_only_full_choreography(monkeypatch):
    block = Block(kind="table", rows=[["A", "B"], ["C", "D"]])
    table_doc = _table_doc(start_index=1, cell_indices=[[5, 8], [12, 15]])
    script = [
        (True, {}),            # insertTable batchUpdate
        (True, table_doc),     # get -> locate table + cell indices
        (True, {}),            # populate batchUpdate
        (True, _end_doc(30)),  # get -> end index
    ]

    success, message, recorded = _run(monkeypatch, script, "u", "doc", [block], 1)

    assert (success, message) == (True, "ok")
    kinds = [k for k, _ in recorded]
    assert kinds == ["batchUpdate", "get", "batchUpdate", "get"]

    insert_body = recorded[0][1]
    assert insert_body == {
        "requests": [
            {"insertTable": {"rows": 2, "columns": 2, "location": {"index": 1}}}
        ]
    }
    # Cells populated in reverse row/col order, at the canned cell start indices.
    populate_body = recorded[2][1]
    assert populate_body == {
        "requests": [
            {"insertText": {"location": {"index": 15}, "text": "D"}},
            {"insertText": {"location": {"index": 12}, "text": "C"}},
            {"insertText": {"location": {"index": 8}, "text": "B"}},
            {"insertText": {"location": {"index": 5}, "text": "A"}},
        ]
    }


def test_table_empty_cells_skipped_in_populate(monkeypatch):
    # A blank cell emits no insertText (the `if cell_text` guard).
    block = Block(kind="table", rows=[["A", ""]])
    table_doc = _table_doc(start_index=1, cell_indices=[[5, 8]])
    script = [(True, {}), (True, table_doc), (True, {}), (True, _end_doc(20))]

    success, _, recorded = _run(monkeypatch, script, "u", "doc", [block], 1)

    assert success is True
    assert recorded[2][1] == {
        "requests": [{"insertText": {"location": {"index": 5}, "text": "A"}}]
    }


def test_table_ragged_rows_respect_bounds_guards(monkeypatch):
    # row 1 is shorter than num_cols (=2): its missing cell emits no insertText.
    block = Block(kind="table", rows=[["A", "B"], ["C"]])
    table_doc = _table_doc(start_index=1, cell_indices=[[5, 8], [12, 15]])
    script = [(True, {}), (True, table_doc), (True, {}), (True, _end_doc(25))]

    success, _, recorded = _run(monkeypatch, script, "u", "doc", [block], 1)

    assert success is True
    # insertTable still 2 columns (max row width).
    assert recorded[0][1]["requests"][0]["insertTable"]["columns"] == 2
    # reverse order: r1c1 skipped (no data), r1c0 "C"@12, r0c1 "B"@8, r0c0 "A"@5.
    assert recorded[2][1] == {
        "requests": [
            {"insertText": {"location": {"index": 12}, "text": "C"}},
            {"insertText": {"location": {"index": 8}, "text": "B"}},
            {"insertText": {"location": {"index": 5}, "text": "A"}},
        ]
    }


def test_table_not_found_returns_error(monkeypatch):
    block = Block(kind="table", rows=[["A"]])
    no_table_doc = {"body": {"content": [{"endIndex": 5}]}}  # no "table" key
    script = [(True, {}), (True, no_table_doc)]

    success, message, recorded = _run(monkeypatch, script, "u", "doc", [block], 1)

    assert success is False
    assert message == "Could not locate inserted table in document."
    assert [k for k, _ in recorded] == ["batchUpdate", "get"]


def test_table_insert_failure_propagates(monkeypatch):
    block = Block(kind="table", rows=[["A"]])
    success, message, recorded = _run(monkeypatch, [(False, "boom")], "u", "doc", [block], 1)
    assert (success, message) == (False, "boom")
    assert [k for k, _ in recorded] == ["batchUpdate"]


def test_table_first_get_failure_propagates(monkeypatch):
    block = Block(kind="table", rows=[["A"]])
    success, message, recorded = _run(
        monkeypatch, [(True, {}), (False, "boom")], "u", "doc", [block], 1
    )
    assert (success, message) == (False, "boom")
    assert [k for k, _ in recorded] == ["batchUpdate", "get"]


def test_table_populate_failure_propagates(monkeypatch):
    block = Block(kind="table", rows=[["A"]])
    table_doc = _table_doc(start_index=1, cell_indices=[[5]])
    script = [(True, {}), (True, table_doc), (False, "boom")]
    success, message, recorded = _run(monkeypatch, script, "u", "doc", [block], 1)
    assert (success, message) == (False, "boom")
    assert [k for k, _ in recorded] == ["batchUpdate", "get", "batchUpdate"]


def test_table_final_get_failure_propagates(monkeypatch):
    block = Block(kind="table", rows=[["A"]])
    table_doc = _table_doc(start_index=1, cell_indices=[[5]])
    script = [(True, {}), (True, table_doc), (True, {}), (False, "boom")]
    success, message, recorded = _run(monkeypatch, script, "u", "doc", [block], 1)
    assert (success, message) == (False, "boom")
    assert [k for k, _ in recorded] == ["batchUpdate", "get", "batchUpdate", "get"]


# --- mixed text + table (cursor threading) ---------------------------------


def test_mixed_text_then_table_threads_cursor(monkeypatch):
    text_block = _para("Intro")
    table_block = Block(kind="table", rows=[["X"]])
    text_reqs, text_cursor = _text_blocks_to_requests([text_block], 1)
    # The table inserts at the cursor returned by the text segment.
    assert text_cursor == 1 + len("Intro\n")  # == 7

    table_doc = _table_doc(start_index=text_cursor, cell_indices=[[text_cursor + 3]])
    script = [
        (True, {}),            # text batchUpdate
        (True, {}),            # insertTable batchUpdate
        (True, table_doc),     # get
        (True, {}),            # populate batchUpdate
        (True, _end_doc(40)),  # get
    ]

    success, message, recorded = _run(
        monkeypatch, script, "u", "doc", [text_block, table_block], 1
    )

    assert (success, message) == (True, "ok")
    assert [k for k, _ in recorded] == ["batchUpdate", "batchUpdate", "get", "batchUpdate", "get"]
    # text body byte-identical to the pure helper output (no prefix).
    assert recorded[0][1] == {"requests": text_reqs}
    # insertTable located at the threaded cursor, not start_index.
    assert recorded[1][1]["requests"][0]["insertTable"]["location"]["index"] == text_cursor
    # the canned table startIndex is within the `approx_index - 2` find slack.
    assert abs(table_doc["body"]["content"][0]["startIndex"] - text_cursor) <= 2


def test_two_consecutive_tables_rethread_cursor(monkeypatch):
    t1 = Block(kind="table", rows=[["A"]])
    t2 = Block(kind="table", rows=[["B"]])
    doc1 = _table_doc(start_index=1, cell_indices=[[5]])
    doc2_cursor = 15 - 1  # _get_doc_end_index(end_doc(15)) - 1
    doc2 = _table_doc(start_index=doc2_cursor, cell_indices=[[doc2_cursor + 5]])
    script = [
        (True, {}),             # table1 insertTable
        (True, doc1),           # table1 get
        (True, {}),             # table1 populate
        (True, _end_doc(15)),   # table1 end -> cursor 14
        (True, {}),             # table2 insertTable
        (True, doc2),           # table2 get
        (True, {}),             # table2 populate
        (True, _end_doc(30)),   # table2 end
    ]

    success, message, recorded = _run(monkeypatch, script, "u", "doc", [t1, t2], 1)

    assert (success, message) == (True, "ok")
    insert_indices = [
        body["requests"][0]["insertTable"]["location"]["index"]
        for kind, body in recorded
        if kind == "batchUpdate" and "insertTable" in body["requests"][0]
    ]
    assert insert_indices == [1, doc2_cursor]


def test_table_text_table_threads_cursor(monkeypatch):
    t1 = Block(kind="table", rows=[["A"]])
    mid = _para("Mid")
    t2 = Block(kind="table", rows=[["B"]])
    doc1 = _table_doc(start_index=1, cell_indices=[[5]])
    text_start = 12  # _get_doc_end_index(end_doc(13)) - 1
    text_reqs, after_text = _text_blocks_to_requests([mid], text_start)
    doc2 = _table_doc(start_index=after_text, cell_indices=[[after_text + 4]])
    script = [
        (True, {}),            # t1 insertTable
        (True, doc1),          # t1 get
        (True, {}),            # t1 populate
        (True, _end_doc(13)),  # t1 end -> cursor 12
        (True, {}),            # text batchUpdate at cursor 12
        (True, {}),            # t2 insertTable at after_text
        (True, doc2),          # t2 get
        (True, {}),            # t2 populate
        (True, _end_doc(40)),  # t2 end
    ]

    success, message, recorded = _run(monkeypatch, script, "u", "doc", [t1, mid, t2], 1)

    assert (success, message) == (True, "ok")
    # the text segment writes at the cursor recomputed from t1's end doc.
    assert recorded[4][1] == {"requests": text_reqs}
    # t2 inserts at the cursor advanced by the text segment.
    assert recorded[5][1]["requests"][0]["insertTable"]["location"]["index"] == after_text


# --- prefix-to-first-batch-only across mixed content -----------------------


def test_prefix_only_on_first_batch_text_first(monkeypatch):
    text_block = _para("Intro")
    table_block = Block(kind="table", rows=[["X"]])
    prefix = [{"deleteContentRange": {"range": {"startIndex": 1, "endIndex": 9}}}]
    text_reqs, text_cursor = _text_blocks_to_requests([text_block], 1)
    table_doc = _table_doc(start_index=text_cursor, cell_indices=[[text_cursor + 3]])
    script = [(True, {}), (True, {}), (True, table_doc), (True, {}), (True, _end_doc(40))]

    success, _, recorded = _run(
        monkeypatch, script, "u", "doc", [text_block, table_block], 1, prefix_requests=prefix
    )

    assert success is True
    # prefix prepended to the FIRST (text) batch only.
    assert recorded[0][1] == {"requests": prefix + text_reqs}
    # insertTable batch carries no prefix.
    assert recorded[1][1] == {
        "requests": [{"insertTable": {"rows": 1, "columns": 1, "location": {"index": text_cursor}}}]
    }


def test_prefix_only_on_first_batch_table_first(monkeypatch):
    table_block = Block(kind="table", rows=[["X"]])
    text_block = _para("Outro")
    prefix = [{"deleteContentRange": {"range": {"startIndex": 1, "endIndex": 9}}}]
    table_doc = _table_doc(start_index=1, cell_indices=[[5]])
    text_reqs, _ = _text_blocks_to_requests([text_block], 19)  # cursor after end_doc(20)
    script = [
        (True, {}),            # insertTable (with prefix)
        (True, table_doc),     # get
        (True, {}),            # populate
        (True, _end_doc(20)),  # end -> cursor 19
        (True, {}),            # text batchUpdate (no prefix)
    ]

    success, _, recorded = _run(
        monkeypatch, script, "u", "doc", [table_block, text_block], 1, prefix_requests=prefix
    )

    assert success is True
    # prefix prepended to the FIRST (insertTable) batch.
    assert recorded[0][1] == {
        "requests": prefix + [{"insertTable": {"rows": 1, "columns": 1, "location": {"index": 1}}}]
    }
    # trailing text batch carries no prefix.
    assert recorded[4][1] == {"requests": text_reqs}


# --- first-batch slot semantics & trailing prefix --------------------------


def test_empty_row_table_skipped_keeps_prefix_for_text(monkeypatch):
    # An empty-row table is skipped WITHOUT consuming the first-batch slot, so the
    # prefix still lands on the following text segment.
    empty_table = Block(kind="table", rows=[])
    text_block = _para("Body")
    prefix = [{"deleteContentRange": {"range": {"startIndex": 1, "endIndex": 5}}}]
    text_reqs, _ = _text_blocks_to_requests([text_block], 1)

    success, message, recorded = _run(
        monkeypatch, [(True, {})], "u", "doc", [empty_table, text_block], 1, prefix_requests=prefix
    )

    assert (success, message) == (True, "ok")
    assert recorded == [("batchUpdate", {"requests": prefix + text_reqs})]


def test_trailing_prefix_sent_when_no_segment_fires(monkeypatch):
    # Only an empty-row table -> nothing sent in the loop -> the prefix is flushed
    # once after the loop.
    empty_table = Block(kind="table", rows=[])
    prefix = [{"deleteContentRange": {"range": {"startIndex": 1, "endIndex": 5}}}]

    success, message, recorded = _run(
        monkeypatch, [(True, {})], "u", "doc", [empty_table], 1, prefix_requests=prefix
    )

    assert (success, message) == (True, "ok")
    assert recorded == [("batchUpdate", {"requests": prefix})]


def test_trailing_prefix_failure_propagates(monkeypatch):
    empty_table = Block(kind="table", rows=[])
    prefix = [{"deleteContentRange": {"range": {"startIndex": 1, "endIndex": 5}}}]
    success, message, recorded = _run(
        monkeypatch, [(False, "boom")], "u", "doc", [empty_table], 1, prefix_requests=prefix
    )
    assert (success, message) == (False, "boom")
    assert recorded == [("batchUpdate", {"requests": prefix})]
