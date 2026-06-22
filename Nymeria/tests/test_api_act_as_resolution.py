"""Tests for the shared act-as resolution helper and its two call sites.

Slice 23 F5 extracted the duplicated ``X-Nymeria-Act-As`` target-resolution
block out of ``resolve_authenticated_user`` and ``require_admin_caller`` into
one ``_resolve_act_as_target`` helper. These tests lock in the helper's status
codes and each dependency's distinct role-gate placement (which is the only
thing that legitimately differed between the two copies).
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from fastapi import HTTPException

from nymeria.core.accounts import AuthenticatedUser, UserRecord
from nymeria.triggers import api as api_app


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _user_record(uid: str = "u1", role: str = "user", disabled: bool = False) -> UserRecord:
    return UserRecord(
        id=uid,
        email=f"{uid}@example.com",
        display_name=uid.title(),
        role=cast(Any, role),
        disabled=disabled,
        created_at="t",
        updated_at="t",
    )


class _FakeRepo:
    def __init__(self, users: dict[str, UserRecord]) -> None:
        self._users = users

    def get_user_by_id(self, uid: str):
        return self._users.get(uid)


class _FakeAgent:
    def __init__(self, users: dict[str, UserRecord]) -> None:
        self.accounts_repo = _FakeRepo(users)


def _agent(users: dict[str, UserRecord]) -> Any:
    """A duck-typed stand-in for NymeriaAgent (only accounts_repo is used)."""
    return _FakeAgent(users)


def _patch_caller(monkeypatch, caller: AuthenticatedUser, agent: Any) -> None:
    monkeypatch.setattr(
        api_app,
        "_resolve_caller_from_bearer",
        lambda *, request, authorization: (caller, agent),
    )


def _run_dep(dep, *, act_as):
    """Invoke an auth dependency directly; request/settings are unused by the
    body once ``_resolve_caller_from_bearer`` is patched, so pass dummies."""
    return asyncio.run(
        dep(
            request=cast(Any, None),
            authorization=None,
            x_nymeria_act_as=act_as,
            settings=cast(Any, None),
        )
    )


# ---------------------------------------------------------------------------
# _resolve_act_as_target (the extracted helper)
# ---------------------------------------------------------------------------


def test_resolve_act_as_target_503_when_agent_uninitialized():
    with pytest.raises(HTTPException) as exc:
        api_app._resolve_act_as_target(None, "u1")
    assert exc.value.status_code == 503


def test_resolve_act_as_target_404_when_target_missing():
    with pytest.raises(HTTPException) as exc:
        api_app._resolve_act_as_target(_agent({}), "u1")
    assert exc.value.status_code == 404


def test_resolve_act_as_target_404_when_target_disabled():
    with pytest.raises(HTTPException) as exc:
        api_app._resolve_act_as_target(_agent({"u1": _user_record(disabled=True)}), "u1")
    assert exc.value.status_code == 404


def test_resolve_act_as_target_success_sets_via_act_as():
    result = api_app._resolve_act_as_target(_agent({"u1": _user_record(uid="u1", role="user")}), "u1")
    assert isinstance(result, AuthenticatedUser)
    assert result.id == "u1"
    assert result.email == "u1@example.com"
    assert result.role == "user"
    assert result.via_act_as is True


# ---------------------------------------------------------------------------
# resolve_authenticated_user: act-as is reachable only by an admin *caller*
# (non-admin -> 403 before any target lookup).
# ---------------------------------------------------------------------------


def test_resolve_authenticated_user_act_as_rejects_non_admin(monkeypatch):
    caller = AuthenticatedUser(id="c", email="c@x", display_name="C", role="user")
    _patch_caller(monkeypatch, caller, _agent({"u1": _user_record("u1")}))
    with pytest.raises(HTTPException) as exc:
        _run_dep(api_app.resolve_authenticated_user, act_as="u1")
    assert exc.value.status_code == 403
    assert exc.value.detail == "Act-As requires admin"


def test_resolve_authenticated_user_admin_act_as_returns_target(monkeypatch):
    caller = AuthenticatedUser(id="admin", email="a@x", display_name="A", role="admin")
    _patch_caller(monkeypatch, caller, _agent({"u1": _user_record("u1", role="user")}))
    result = _run_dep(api_app.resolve_authenticated_user, act_as="u1")
    assert result.id == "u1"
    assert result.via_act_as is True


def test_resolve_authenticated_user_no_act_as_returns_caller(monkeypatch):
    caller = AuthenticatedUser(id="c", email="c@x", display_name="C", role="user")
    _patch_caller(monkeypatch, caller, _agent({}))
    result = _run_dep(api_app.resolve_authenticated_user, act_as=None)
    assert result is caller


# ---------------------------------------------------------------------------
# require_admin_caller: admin *caller* gate runs first (pre-act-as), then the
# caller may impersonate any user (admin OR non-admin) via act-as.
# ---------------------------------------------------------------------------


def test_require_admin_caller_rejects_non_admin(monkeypatch):
    caller = AuthenticatedUser(id="c", email="c@x", display_name="C", role="user")
    _patch_caller(monkeypatch, caller, _agent({}))
    with pytest.raises(HTTPException) as exc:
        _run_dep(api_app.require_admin_caller, act_as=None)
    assert exc.value.status_code == 403
    assert exc.value.detail == "Admin only"


def test_require_admin_caller_rejects_non_admin_even_with_act_as(monkeypatch):
    # The admin-*caller* gate runs before act-as resolution, so a non-admin
    # caller is rejected with "Admin only" even when an act-as header is set
    # (the target user is never looked up). Locks the gate ordering that
    # distinguishes this dependency from resolve_authenticated_user.
    caller = AuthenticatedUser(id="c", email="c@x", display_name="C", role="user")
    _patch_caller(monkeypatch, caller, _agent({"u1": _user_record("u1")}))
    with pytest.raises(HTTPException) as exc:
        _run_dep(api_app.require_admin_caller, act_as="u1")
    assert exc.value.status_code == 403
    assert exc.value.detail == "Admin only"


def test_require_admin_caller_admin_act_as_returns_target(monkeypatch):
    caller = AuthenticatedUser(id="admin", email="a@x", display_name="A", role="admin")
    _patch_caller(monkeypatch, caller, _agent({"u1": _user_record("u1", role="user")}))
    result = _run_dep(api_app.require_admin_caller, act_as="u1")
    assert result.id == "u1"
    assert result.via_act_as is True


def test_require_admin_caller_admin_no_act_as_returns_caller(monkeypatch):
    caller = AuthenticatedUser(id="admin", email="a@x", display_name="A", role="admin")
    _patch_caller(monkeypatch, caller, _agent({}))
    result = _run_dep(api_app.require_admin_caller, act_as=None)
    assert result is caller
