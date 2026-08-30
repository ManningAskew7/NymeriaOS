"""The suite's process-environment hermeticity (conftest layer 3, #294).

Locks two properties: environment written during COLLECTION never reaches a
test, and the restore is a true two-way sync (drops additions, puts back
deletions). Why layer 3 exists, what it does and does not promise, and why it
is a hook rather than an autouse fixture: the comment on ``_PRISTINE_ENV`` in
``conftest.py``.
"""

from __future__ import annotations

import os

from conftest import _PRISTINE_ENV, _restore_pristine_env  # type: ignore[import-not-found]

# Module-level, so this runs during COLLECTION: the exact shape of the #294
# bug, and the half a per-test snapshot taken after collection would miss.
os.environ["NYMERIA_294_COLLECTION_PROBE"] = "leaked-at-import"


def test_env_written_during_collection_never_reaches_a_test():
    assert "NYMERIA_294_COLLECTION_PROBE" not in os.environ
    assert "NYMERIA_294_COLLECTION_PROBE" not in _PRISTINE_ENV


def test_restore_drops_a_variable_a_test_added():
    os.environ["NYMERIA_294_RUNTIME_PROBE"] = "leaked-during-a-test"
    try:
        _restore_pristine_env()
        assert "NYMERIA_294_RUNTIME_PROBE" not in os.environ
    finally:
        os.environ.pop("NYMERIA_294_RUNTIME_PROBE", None)


def test_restore_puts_back_a_variable_a_test_deleted():
    # NYMERIA_PROJECT_ROOT is in the snapshot (conftest sets it above its own
    # first `nymeria` import) and losing it would mis-point the whole suite,
    # so the restore is also what keeps a deleting test from poisoning others.
    key = "NYMERIA_PROJECT_ROOT"
    original = os.environ[key]
    try:
        del os.environ[key]
        _restore_pristine_env()
        assert os.environ[key] == original
    finally:
        os.environ[key] = original
