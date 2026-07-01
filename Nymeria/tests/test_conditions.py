"""Unit tests for the shared condition model + evaluator (core/conditions.py)."""

from __future__ import annotations

from nymeria.core.conditions import HookCondition, evaluate_conditions
from nymeria.core.trigger_manager import TriggerCondition, TriggerManager


def _c(field, operator, value, case_sensitive=False):
    return HookCondition(field=field, operator=operator, value=value, case_sensitive=case_sensitive)


# --- operators --------------------------------------------------------------

def test_equals():
    assert evaluate_conditions({"x": "hi"}, [_c("x", "equals", "hi")])
    assert not evaluate_conditions({"x": "hi"}, [_c("x", "equals", "bye")])


def test_not_equals():
    assert evaluate_conditions({"x": "hi"}, [_c("x", "not_equals", "bye")])
    assert not evaluate_conditions({"x": "hi"}, [_c("x", "not_equals", "hi")])


def test_contains():
    assert evaluate_conditions({"cmd": "rm -rf /"}, [_c("cmd", "contains", "rm -rf")])
    assert not evaluate_conditions({"cmd": "ls"}, [_c("cmd", "contains", "rm -rf")])


def test_starts_with():
    assert evaluate_conditions({"cmd": "sudo rm"}, [_c("cmd", "starts_with", "sudo")])
    assert not evaluate_conditions({"cmd": "rm sudo"}, [_c("cmd", "starts_with", "sudo")])


def test_matches_regex():
    assert evaluate_conditions({"cmd": "rm -rf /etc"}, [_c("cmd", "matches_regex", r"rm\s+-rf")])
    assert not evaluate_conditions({"cmd": "remove"}, [_c("cmd", "matches_regex", r"rm\s+-rf")])


def test_bad_regex_is_non_match_not_raise():
    # An invalid pattern must degrade to a non-match, never raise.
    assert not evaluate_conditions({"cmd": "anything"}, [_c("cmd", "matches_regex", "(")])


# --- AND logic + empty ------------------------------------------------------

def test_and_logic():
    conds = [_c("a", "equals", "1"), _c("b", "contains", "x")]
    assert evaluate_conditions({"a": "1", "b": "xyz"}, conds)
    assert not evaluate_conditions({"a": "1", "b": "yz"}, conds)


def test_empty_conditions_always_true():
    assert evaluate_conditions({}, [])


# --- case sensitivity -------------------------------------------------------

def test_case_insensitive_by_default():
    assert evaluate_conditions({"x": "HELLO"}, [_c("x", "equals", "hello")])


def test_case_sensitive_honored():
    assert not evaluate_conditions({"x": "HELLO"}, [_c("x", "equals", "hello", case_sensitive=True)])
    assert evaluate_conditions({"x": "HELLO"}, [_c("x", "equals", "HELLO", case_sensitive=True)])


def test_case_sensitive_regex():
    assert not evaluate_conditions(
        {"x": "ABC"}, [_c("x", "matches_regex", "abc", case_sensitive=True)]
    )
    assert evaluate_conditions(
        {"x": "ABC"}, [_c("x", "matches_regex", "abc", case_sensitive=False)]
    )


# --- field resolution semantics ---------------------------------------------

def test_missing_key_renders_empty():
    # Absent field coerces to "" (so "contains x" fails, "equals ''" passes).
    assert not evaluate_conditions({}, [_c("x", "contains", "a")])
    assert evaluate_conditions({}, [_c("x", "equals", "")])


def test_present_none_renders_str_none():
    # A present-but-None value coerces to str(None) == "None" (trigger parity).
    assert evaluate_conditions({"x": None}, [_c("x", "equals", "none")])
    assert evaluate_conditions({"x": None}, [_c("x", "contains", "non")])


def test_dotted_path_resolves_nested():
    data = {"input": {"command": "rm -rf /"}}
    assert evaluate_conditions(data, [_c("input.command", "contains", "rm -rf")])
    assert not evaluate_conditions(data, [_c("input.command", "contains", "ls")])


def test_dotted_path_missing_branch_is_empty():
    assert not evaluate_conditions({"input": {}}, [_c("input.command", "contains", "rm")])
    assert not evaluate_conditions({}, [_c("a.b.c", "contains", "x")])


def test_literal_dotted_key_wins_over_path():
    # A top-level key that literally contains a dot must resolve before the path
    # walk (preserves trigger behavior for flat events with dotted field names).
    data = {"a.b": "flat", "a": {"b": "nested"}}
    assert evaluate_conditions(data, [_c("a.b", "equals", "flat")])


# --- trigger parity (regression) --------------------------------------------

def test_trigger_evaluator_delegates_unchanged():
    # TriggerManager._evaluate_conditions now delegates to the shared evaluator;
    # assert representative trigger semantics are unchanged.
    conds = [TriggerCondition(field="type", operator="equals", value="push")]
    assert TriggerManager._evaluate_conditions({"type": "push"}, conds) is True
    assert TriggerManager._evaluate_conditions({"type": "pull"}, conds) is False
    # Missing field -> "" (contains fails), matching the historical default.
    miss = [TriggerCondition(field="label", operator="contains", value="bug")]
    assert TriggerManager._evaluate_conditions({}, miss) is False


def test_trigger_condition_is_shared_model():
    # The trigger stack re-exports HookCondition, so both are the same class.
    assert TriggerCondition is HookCondition
