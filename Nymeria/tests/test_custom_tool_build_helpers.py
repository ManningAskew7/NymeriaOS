"""Shared custom-tool build/update helper regressions (optimization slice 10 F5).

These helpers back both the ``/tools/custom`` and ``/tools/unified`` create and
update handlers, so this exercises the single source the two routers now share.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from nymeria.api.schemas.custom_tools import (
    CustomToolCreateRequest,
    CustomToolUpdateRequest,
    apply_custom_tool_update,
    build_custom_tool_definition,
)


def _http_create(**overrides) -> CustomToolCreateRequest:
    payload = {
        "id": "price_lookup",
        "name": "Price Lookup",
        "description": "Look up a price",
        "implementation_type": "http",
        "http_config": {"url": "https://api.example.com/prices/${symbol}"},
    }
    payload.update(overrides)
    return CustomToolCreateRequest(**payload)


def test_build_http_definition_populates_only_http_config():
    defn = build_custom_tool_definition(_http_create())
    assert defn.implementation_type == "http"
    assert defn.http_config is not None
    assert defn.http_config.url == "https://api.example.com/prices/${symbol}"
    assert defn.mcp_config is None
    assert defn.python_config is None
    # parameters defaults to {}, so the prior `parameters or {}` and bare
    # `parameters` call sites collapse to the same value.
    assert defn.parameters == {}


def test_build_python_definition():
    req = CustomToolCreateRequest(
        id="python_echo",
        name="Python Echo",
        description="Echo text",
        implementation_type="python",
        python_config={"source_code": "def run(value):\n    return value\n"},
    )
    defn = build_custom_tool_definition(req, actor_user_id="admin-1")
    assert defn.implementation_type == "python"
    assert defn.python_config is not None
    assert defn.python_config.entrypoint == "run"
    assert defn.http_config is None
    # The admin actor's create self-approves the revision so the execution gate
    # admits it (mirrors the workflow branch).
    from nymeria.core.python_custom_tools import (
        config_python_revision_hash,
        python_execution_gate,
    )

    assert defn.python_config.approved_by == "admin-1"
    assert defn.python_config.approved_revision == config_python_revision_hash(
        defn.python_config, defn.parameters
    )
    assert python_execution_gate(defn.python_config, defn.parameters) is None


@pytest.mark.parametrize(
    "impl_type,detail",
    [
        ("http", "http_config is required for HTTP tools"),
        ("mcp", "mcp_config is required for MCP tools"),
        ("python", "python_config is required for Python tools"),
    ],
)
def test_build_missing_config_raises_http_400(impl_type: str, detail: str):
    req = CustomToolCreateRequest(
        id="needs_config",
        name="Needs Config",
        description="d",
        implementation_type=impl_type,
    )
    with pytest.raises(HTTPException) as exc:
        build_custom_tool_definition(req)
    assert exc.value.status_code == 400
    assert exc.value.detail == detail


def test_apply_update_replaces_matching_config_and_ignores_mismatched():
    defn = build_custom_tool_definition(_http_create())

    # An mcp_config patch on an http tool is ignored (implementation_type guard).
    apply_custom_tool_update(
        defn,
        CustomToolUpdateRequest(
            mcp_config={"server_command": "npx", "tool_name": "x"}
        ),
    )
    assert defn.mcp_config is None

    # Matching http_config plus scalar fields apply.
    apply_custom_tool_update(
        defn,
        CustomToolUpdateRequest(
            description="Look up a public market price",
            enabled=False,
            tags=["finance"],
            http_config={"url": "https://api.example.com/v2/${symbol}"},
        ),
    )
    assert defn.description == "Look up a public market price"
    assert defn.enabled is False
    assert defn.tags == ["finance"]
    assert defn.http_config is not None
    assert defn.http_config.url == "https://api.example.com/v2/${symbol}"


def test_apply_update_noop_request_leaves_definition_unchanged():
    defn = build_custom_tool_definition(_http_create(enabled=True, tags=["a"]))
    before = defn.model_dump()
    apply_custom_tool_update(defn, CustomToolUpdateRequest())
    # Only None fields in the update request -> nothing changes.
    assert defn.model_dump() == before
