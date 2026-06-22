"""Unit tests for AuthPromptCoordinator: the auth-specific behavior layered
on top of FutureRendezvous (attempt enrichment, token verification, sweep
payload). The shared registry/sweep machinery is covered in
test_future_rendezvous.py."""

from __future__ import annotations

import asyncio

import pytest

from nymeria.core.auth_prompt_coordinator import (
    AuthPromptCoordinator,
    hash_prompt_token,
    new_prompt_id,
    new_prompt_token,
    prompt_expires_at,
)


@pytest.fixture(autouse=True)
def reset_coordinator(monkeypatch):
    import nymeria.core.auth_prompt_coordinator as mod
    monkeypatch.setattr(mod, "_coordinator", None)
    yield


def _register(coord: AuthPromptCoordinator, prompt_id: str, **overrides) -> asyncio.Future:
    kwargs = dict(
        prompt_id=prompt_id,
        credential_id="cred_1",
        user_id="u1",
        thread_id="t1",
        provider="github",
    )
    kwargs.update(overrides)
    return coord.register(**kwargs)


def test_new_prompt_id_and_token_formats() -> None:
    pid = new_prompt_id()
    assert pid.startswith("prompt_")
    token = new_prompt_token()
    assert len(token) > 20
    assert hash_prompt_token(token) == hash_prompt_token(token)


def test_resolve_enriches_with_attempt_totals() -> None:
    async def run() -> None:
        coord = AuthPromptCoordinator()
        future = _register(coord, "prompt_1")
        coord.record_attempt("prompt_1", error="bad secret")
        coord.record_attempt("prompt_1", error="still bad")

        async def waiter() -> dict:
            return await asyncio.wait_for(future, timeout=2)

        task = asyncio.create_task(waiter())
        await asyncio.sleep(0)
        assert coord.resolve("prompt_1", {"ok": True, "status": "success"}) is True
        result = await task
        assert result["ok"] is True
        assert result["attempts"] == 2
        assert result["last_test_error"] == "still bad"
        assert coord.get("prompt_1") is None

    asyncio.run(run())


def test_resolve_payload_can_override_enriched_keys() -> None:
    async def run() -> None:
        coord = AuthPromptCoordinator()
        future = _register(coord, "prompt_ov")
        coord.record_attempt("prompt_ov", error="x")
        coord.resolve("prompt_ov", {"attempts": 99, "ok": False})
        await asyncio.sleep(0)  # let the threadsafe result callback run
        # The explicit result payload wins over the running totals.
        assert future.result()["attempts"] == 99

    asyncio.run(run())


def test_record_attempt_unknown_returns_zero() -> None:
    coord = AuthPromptCoordinator()
    assert coord.record_attempt("nope", error="boom") == 0


def test_get_for_user_thread_returns_oldest() -> None:
    async def run() -> None:
        coord = AuthPromptCoordinator()
        _register(coord, "prompt_old")
        await asyncio.sleep(0.01)
        _register(coord, "prompt_new")
        _register(coord, "prompt_other", thread_id="t2")
        match = coord.get_for_user_thread(user_id="u1", thread_id="t1")
        assert match is not None and match.prompt_id == "prompt_old"
        assert coord.get_for_user_thread(user_id="u1", thread_id="absent") is None

    asyncio.run(run())


def test_verify_prompt_token_happy_expired_and_mismatch() -> None:
    async def run() -> None:
        coord = AuthPromptCoordinator()
        token = new_prompt_token()
        expires_at, expires_iso = prompt_expires_at(300)
        _register(
            coord,
            "prompt_tok",
            token_hash=hash_prompt_token(token),
            token_expires_at=expires_at,
            token_expires_at_iso=expires_iso,
        )
        assert coord.verify_prompt_token("prompt_tok", token) is not None
        assert coord.verify_prompt_token("prompt_tok", "wrong") is None
        assert coord.verify_prompt_token("prompt_tok", "") is None
        # No token hash registered -> never verifies.
        _register(coord, "prompt_notok")
        assert coord.verify_prompt_token("prompt_notok", token) is None

    asyncio.run(run())


def test_prompts_alias_reflects_live_registry() -> None:
    async def run() -> None:
        coord = AuthPromptCoordinator()
        _register(coord, "prompt_a")
        # The legacy `_prompts` accessor is the same live dict the base owns.
        assert "prompt_a" in coord._prompts
        coord.discard("prompt_a")
        assert "prompt_a" not in coord._prompts

    asyncio.run(run())


def test_sweep_resolves_orphan_with_attempt_totals(monkeypatch) -> None:
    import nymeria.core.auth_prompt_coordinator as mod
    monkeypatch.setattr(mod, "_ORPHAN_TTL_SECONDS", 0.05)
    monkeypatch.setattr(mod, "_SWEEP_INTERVAL_SECONDS", 0.02)

    async def run() -> None:
        coord = AuthPromptCoordinator()
        future = _register(coord, "prompt_orphan")
        coord.record_attempt("prompt_orphan", error="left hanging")
        try:
            result = await asyncio.wait_for(future, timeout=2)
        except asyncio.TimeoutError:
            pytest.fail("sweep should have resolved the orphaned prompt")
        assert result["ok"] is False
        assert result["status"] == "swept"
        assert result["attempts"] == 1
        assert result["last_test_error"] == "left hanging"

    asyncio.run(run())
