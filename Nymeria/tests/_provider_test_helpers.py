"""Shared helpers for the LLM-provider wiring test cluster.

Centralizes the ``_xxx_config(**overrides) -> LLMConfig`` factory that three
provider test files previously copied as 22 near-identical 10-line clones
(optimization slice 34 F4):

- ``test_openai_responses_config.py`` (16 factories),
- ``test_partner_provider_creation.py`` (3 factories),
- ``test_reasoning_effort_scale.py`` (3 factories).

Each file keeps its own thin named wrappers (so call sites stay byte-identical)
that delegate to :func:`llm_config`, passing their per-provider ``defaults``::

    from _provider_test_helpers import llm_config  # type: ignore[import-not-found]

    def _openai_config(**overrides) -> LLMConfig:
        return llm_config(
            {"provider": "openai", "model": "gpt-5.5", "base_url": "http://example.test/v1"},
            **overrides,
        )

``defaults`` then ``overrides`` reproduces the old ``values.update(overrides)``
semantics exactly: ANY key (including ``model``/``provider``/``api_key``) that a
test passes wins, since ``overrides`` is merged last.
"""

from __future__ import annotations

from typing import Any

from nymeria.vendor.react_agent.config import LLMConfig


def llm_config(defaults: dict[str, Any], /, **overrides) -> LLMConfig:
    """Build an :class:`LLMConfig` from per-provider ``defaults`` plus ``overrides``.

    Layers, in increasing precedence: a shared base of ``api_key="test-key"`` /
    ``temperature=None``, then the per-provider ``defaults``
    (provider/model/base_url and any ``api_key``/``provider_route`` the provider
    needs), then the caller's ``overrides``. The two ``update`` calls are
    identical to the ``values.update(overrides)`` each duplicated factory
    performed, so a test can override any field (e.g. ``model=...``,
    ``api_key=None``).
    """
    values: dict[str, Any] = {"api_key": "test-key", "temperature": None}
    values.update(defaults)
    values.update(overrides)
    return LLMConfig(**values)
