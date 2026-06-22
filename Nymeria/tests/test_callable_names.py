"""Tests for the shared callable-name sanitizer/deduper.

`core/callable_names.py` is the single source for the callable-thread naming
contract consumed by the branch path (`thread_branch`) and the import path
(`thread_share`). These tests pin both the generic helpers and the exact
per-call-site behavior the two paths relied on before the extraction.
"""

from __future__ import annotations

from nymeria.core.callable_names import (
    CALLABLE_NAME_RE,
    dedupe_callable_name,
    safe_callable_base,
)
from nymeria.core.thread_branch import _safe_callable_name
from nymeria.core.thread_share import _safe_callable_base, _dedupe_callable_name


def test_safe_callable_base_collapses_and_strips():
    assert safe_callable_base("My Cool Tool!!") == "My_Cool_Tool"
    assert safe_callable_base("  __weird__name__  ") == "weird_name"
    assert safe_callable_base("keep-dash_and_underscore") == "keep-dash_and_underscore"


def test_safe_callable_base_empty_falls_back():
    assert safe_callable_base("???", fallback="Branch") == "Branch"
    assert safe_callable_base("", fallback="ImportedThread") == "ImportedThread"


def test_safe_callable_base_digit_prefix():
    assert safe_callable_base("9lives", digit_prefix="Branch") == "Branch_9lives"
    assert safe_callable_base("42", digit_prefix="Imported") == "Imported_42"


def test_safe_callable_base_truncates_to_max_len():
    long = "a" * 200
    assert len(safe_callable_base(long, max_len=64)) == 64
    assert len(safe_callable_base(long, max_len=56)) == 56


def test_branch_site_behavior_matches_legacy():
    # Branch: fallback="Branch", digit_prefix="Branch", max_len=64.
    assert _safe_callable_name("9to5 plan") == "Branch_9to5_plan"
    assert _safe_callable_name("!!!") == "Branch"
    assert _safe_callable_name("a" * 100) == "a" * 64


def test_share_site_behavior_matches_legacy():
    # Share: fallback="ImportedThread", digit_prefix="Imported", max_len=56.
    assert _safe_callable_base("9to5 plan") == "Imported_9to5_plan"
    assert _safe_callable_base("!!!") == "ImportedThread"
    assert _safe_callable_base("a" * 100) == "a" * 56


def test_dedupe_returns_bare_base_when_available():
    assert dedupe_callable_name("Helper", set()) == "Helper"


def test_dedupe_appends_numeric_suffix_when_taken():
    assert dedupe_callable_name("Helper", {"Helper"}) == "Helper_2"
    assert dedupe_callable_name("Helper", {"Helper", "Helper_2"}) == "Helper_3"


def test_dedupe_truncates_base_to_fit_suffix():
    base = "x" * 64
    candidate = dedupe_callable_name(base, {base})
    assert candidate == "x" * 62 + "_2"
    assert len(candidate) == 64
    assert CALLABLE_NAME_RE.match(candidate)


def test_dedupe_returns_none_when_exhausted():
    base = "Helper"
    unavailable = {"Helper"} | {f"Helper_{i}" for i in range(2, 1000)}
    assert dedupe_callable_name(base, unavailable) is None


def test_share_dedupe_raises_typed_error_when_exhausted():
    import pytest

    from nymeria.core.thread_share import ThreadShareError

    base = _safe_callable_base("Helper")
    unavailable = {base[:64]} | {f"{base[:64 - len(f'_{i}')]}_{i}" for i in range(2, 1000)}
    with pytest.raises(ThreadShareError):
        _dedupe_callable_name("Helper", unavailable)
