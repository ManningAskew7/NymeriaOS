"""Shared query-batch helpers for the web_search_* and web fetch tools.

Both the per-provider search tools (``web.py``, ``web_search_integrations.py``)
and the page-fetch tool (``web_fetch.py``) accept either a single item or a
pipe-separated batch, and render batch results under
``"=== <label> N/M: <item> ==="`` section headers. This module owns the two
byte-identical fragments those tools used to copy per provider so a change to
batch semantics (the parse rules or the header shape) lives in one place. The
search tools use both helpers; ``web_fetch`` keeps its own URL-specific parse
(it also splits on commas before an ``http(s)`` URL) and shares only
``run_batched``.

``MAX_BATCH_QUERIES`` here is only the default cap for ``parse_batch_queries``;
each tool keeps its own ``_MAX_BATCH_QUERIES`` / ``_MAX_BATCH_URLS`` constant as
the authoritative per-tool knob and passes it in via ``max_n``.
"""

from typing import Callable, Optional

MAX_BATCH_QUERIES = 10


def parse_batch_queries(
    query: str, queries: str, *, max_n: int = MAX_BATCH_QUERIES
) -> tuple[list[str], Optional[str]]:
    """Parse the single/batch query args (batch takes precedence over single).

    ``queries`` is split on ``" | "``, each part stripped, empties dropped, and
    the list capped at ``max_n``. A non-empty ``query`` is used only when
    ``queries`` is blank.

    Returns ``(query_list, None)`` on success, or ``([], error_str)`` when
    neither arg is provided (the caller returns ``error_str`` as the tool
    result).
    """
    if queries.strip():
        query_list = [q.strip() for q in queries.split(" | ")]
        query_list = [q for q in query_list if q]
        if len(query_list) > max_n:
            query_list = query_list[:max_n]
    elif query.strip():
        query_list = [query.strip()]
    else:
        return [], "[Error]: Provide a query or pipe-separated queries."
    return query_list, None


def run_batched(
    items: list[str], runner: Callable[[str], str], *, label: str = "Query"
) -> str:
    """Run ``runner`` over one or many items, joining batch results with headers.

    A single item returns ``runner(item)`` directly (no header). Multiple items
    are rendered as ``"=== <label> N/M: <item> ===\\n<result>"`` sections joined
    by a blank line. ``runner`` receives the raw (header) item; a provider that
    rewrites the item before issuing the request (for example appending a
    ``site:`` filter) does so inside ``runner`` so the header still shows the
    raw item.
    """
    if len(items) == 1:
        return runner(items[0])
    total = len(items)
    sections: list[str] = []
    for i, item in enumerate(items, 1):
        result = runner(item)
        sections.append(f"=== {label} {i}/{total}: {item} ===\n{result}")
    return "\n\n".join(sections)
