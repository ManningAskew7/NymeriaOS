"""Direct unit tests for the callable-thread tool guard helpers.

These cover the auth/visibility guards extracted from the
``callable_thread_tool_func`` closure (optimization slice 27 F4). Before the
extraction the only seam was a full ``tool.invoke(...)`` through LangChain
machinery (see ``test_callable_runtime_ownership.py``), which left the
fail-closed ownership-exception path uncovered. Each guard returns an error
string to surface to the model, or ``None`` when it allows / does not apply.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from nymeria.agents.tool_factory import (
    _check_callable_ownership,
    _check_team_visibility,
    _resolve_parent_name_and_trigger,
)

LOGGER_NAME = "nymeria.agents.tool_factory"


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _OwnershipRepo:
    def __init__(self, owner, roles=None, *, raise_on=None):
        self.owner = owner
        self.roles = roles or {}
        self.raise_on = raise_on  # "owner" | "user" | None

    def get_thread_owner(self, thread_id):
        if self.raise_on == "owner":
            raise RuntimeError("owner lookup boom")
        return self.owner

    def get_user_by_id(self, user_id):
        if self.raise_on == "user":
            raise RuntimeError("user lookup boom")
        return SimpleNamespace(role=self.roles.get(user_id, "user"))


def _ownership_agent(owner, roles=None, *, raise_on=None):
    return SimpleNamespace(accounts_repo=_OwnershipRepo(owner, roles, raise_on=raise_on))


class _ExplodingRepo:
    """accounts_repo whose attribute access proves the gate was skipped."""

    def get_thread_owner(self, thread_id):  # pragma: no cover - must not run
        raise AssertionError("ownership gate should have been skipped")

    def get_user_by_id(self, user_id):  # pragma: no cover - must not run
        raise AssertionError("ownership gate should have been skipped")


# --------------------------------------------------------------------------- #
# _check_callable_ownership
# --------------------------------------------------------------------------- #
def test_ownership_no_agent_skips():
    assert _check_callable_ownership(None, "u", name="Helper", thread_id="t") is None


def test_ownership_falsy_user_id_skips():
    # The original guard is `if _agent and user_id:`; a falsy user_id must skip
    # the gate entirely (and never touch accounts_repo).
    agent = SimpleNamespace(accounts_repo=_ExplodingRepo())
    assert _check_callable_ownership(agent, "", name="Helper", thread_id="t") is None


def test_ownership_owner_matches_allows():
    agent = _ownership_agent("owner")
    assert _check_callable_ownership(agent, "owner", name="Helper", thread_id="t") is None


def test_ownership_mismatch_regular_user_blocked():
    agent = _ownership_agent("owner", {"other": "user"})
    result = _check_callable_ownership(agent, "other", name="Helper", thread_id="t")
    assert result is not None
    assert result.startswith("[Error]:")
    assert "not available to this user" in result


def test_ownership_mismatch_admin_as_self_still_blocked():
    # Admin invoking as itself (not act-as) against another user's callable is
    # blocked; admin act-as works only because the API rewrites user_id upstream.
    agent = _ownership_agent("owner", {"admin": "admin"})
    result = _check_callable_ownership(agent, "admin", name="Helper", thread_id="t")
    assert result is not None
    assert "not available to this user" in result


def test_ownership_legacy_unowned_regular_blocked(caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    agent = _ownership_agent(None, {"other": "user"})
    result = _check_callable_ownership(agent, "other", name="Helper", thread_id="t")
    assert result is not None
    assert "not available to this user" in result
    assert "can only be invoked by an admin" in caplog.text


def test_ownership_legacy_unowned_admin_allowed_with_warning(caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    agent = _ownership_agent(None, {"admin": "admin"})
    result = _check_callable_ownership(agent, "admin", name="Helper", thread_id="t")
    assert result is None  # admin may invoke a legacy unowned callable
    assert "admin user_id=admin invoking legacy unowned callable thread t" in caplog.text


def test_ownership_owner_lookup_raises_fails_closed(caplog):
    # The headline coverage gap: a verification error must refuse, not allow.
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    agent = _ownership_agent("owner", raise_on="owner")
    result = _check_callable_ownership(agent, "owner", name="Helper", thread_id="t")
    assert result is not None
    assert "ownership could not be verified" in result
    assert "Refusing invocation" in result
    assert "ownership check failed:" in caplog.text


def test_ownership_user_lookup_raises_fails_closed(caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    agent = _ownership_agent("owner", raise_on="user")
    result = _check_callable_ownership(agent, "owner", name="Helper", thread_id="t")
    assert result is not None
    assert "ownership could not be verified" in result
    assert "ownership check failed:" in caplog.text


def test_ownership_unknown_caller_treated_as_non_admin():
    # get_user_by_id can return None for an unknown user: caller_is_admin is then
    # False, so an unowned callable is admin-only and the call is blocked.
    class _NoCallerRepo:
        def get_thread_owner(self, thread_id):
            return None

        def get_user_by_id(self, user_id):
            return None

    agent = SimpleNamespace(accounts_repo=_NoCallerRepo())
    result = _check_callable_ownership(agent, "ghost", name="Helper", thread_id="t")
    assert result is not None
    assert "not available to this user" in result


# --------------------------------------------------------------------------- #
# _check_team_visibility
# --------------------------------------------------------------------------- #
def test_team_no_parent_skips():
    agent = SimpleNamespace(is_callable_visible_to_thread=lambda p, t: False)
    assert _check_team_visibility(agent, None, name="Helper", thread_id="t") is None


def test_team_agent_without_method_skips():
    # hasattr(agent, "is_callable_visible_to_thread") is False -> skip.
    agent = SimpleNamespace()
    assert _check_team_visibility(agent, "parent", name="Helper", thread_id="t") is None


def test_team_visible_allows():
    agent = SimpleNamespace(is_callable_visible_to_thread=lambda p, t: True)
    assert _check_team_visibility(agent, "parent", name="Helper", thread_id="t") is None


def test_team_not_visible_blocked():
    agent = SimpleNamespace(is_callable_visible_to_thread=lambda p, t: False)
    result = _check_team_visibility(agent, "parent", name="Helper", thread_id="t")
    assert result is not None
    assert "not visible from this thread" in result
    assert "Callable teams are isolated" in result


def test_team_no_agent_skips():
    assert _check_team_visibility(None, "parent", name="Helper", thread_id="t") is None


# --------------------------------------------------------------------------- #
# _resolve_parent_name_and_trigger
# --------------------------------------------------------------------------- #
class _MetaMgr:
    def __init__(self, title=None, *, raise_=False):
        self._title = title
        self._raise = raise_

    def get_thread(self, user_id, thread_id):
        if self._raise:
            raise RuntimeError("meta boom")
        return SimpleNamespace(title=self._title)


class _ConfigMgr:
    def __init__(self, callable_name=None, *, raise_=False):
        self._callable_name = callable_name
        self._raise = raise_

    def get_config(self, thread_id):
        if self._raise:
            raise RuntimeError("config boom")
        return SimpleNamespace(callable_name=self._callable_name)


def _resolve_agent(meta, config):
    return SimpleNamespace(thread_metadata_manager=meta, thread_config_manager=config)


def test_resolve_no_parent_returns_none_pair():
    agent = _resolve_agent(_MetaMgr("Title"), _ConfigMgr("Cfg"))
    assert _resolve_parent_name_and_trigger(agent, "u", None) == (None, None)


def test_resolve_no_agent_returns_none_pair():
    assert _resolve_parent_name_and_trigger(None, "u", "parent") == (None, None)


def test_resolve_uses_thread_title():
    agent = _resolve_agent(_MetaMgr("My Title"), _ConfigMgr("Cfg"))
    name, trigger = _resolve_parent_name_and_trigger(agent, "u", "pid")
    assert name == "My Title"
    assert trigger == 'Thread("pid", "My Title")'


def test_resolve_falls_back_to_callable_name():
    agent = _resolve_agent(_MetaMgr(None), _ConfigMgr("CallableName"))
    name, trigger = _resolve_parent_name_and_trigger(agent, "u", "pid")
    assert name == "CallableName"
    assert trigger == 'Thread("pid", "CallableName")'


def test_resolve_falls_back_to_raw_thread_id():
    agent = _resolve_agent(_MetaMgr(None), _ConfigMgr(None))
    name, trigger = _resolve_parent_name_and_trigger(agent, "u", "pid")
    assert name == "pid"
    assert trigger == 'Thread("pid", "pid")'


def test_resolve_metadata_raises_falls_through(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
    agent = _resolve_agent(_MetaMgr(raise_=True), _ConfigMgr("CallableName"))
    name, trigger = _resolve_parent_name_and_trigger(agent, "u", "pid")
    assert name == "CallableName"
    assert "Failed to resolve parent thread title" in caplog.text


def test_resolve_config_raises_falls_through_to_raw_id(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
    agent = _resolve_agent(_MetaMgr(None), _ConfigMgr(raise_=True))
    name, trigger = _resolve_parent_name_and_trigger(agent, "u", "pid")
    assert name == "pid"
    assert "Failed to resolve parent callable name" in caplog.text


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
