"""Shared custom-tool build/update helper regressions (optimization slice 10 F5).

These helpers back both the ``/tools/custom`` and ``/tools/unified`` create and
update handlers, so this exercises the single source the two routers now share.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from nymeria.api.schemas.custom_tools import (
    MCP_CLIENT_FIELDS,
    CustomToolCreateRequest,
    CustomToolUpdateRequest,
    MCPToolConfigModel,
    apply_custom_tool_update,
    build_custom_tool_definition,
)
from nymeria.core.custom_tool_gate import custom_tool_execution_gate, stamp_custom_tool_approval
from nymeria.tools.definitions.mcp_schema import MCPToolConfig


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


def test_build_definition_stamps_the_actor_as_creator():
    definition = build_custom_tool_definition(_http_create(), actor_user_id="admin-1")
    assert definition.created_by == "admin-1"


def test_build_definition_without_an_actor_leaves_provenance_unknown():
    assert build_custom_tool_definition(_http_create()).created_by is None


# ---------------------------------------------------------------------------
# #124: the REST model expresses an http-transport MCP tool, and a PUT never
# silently converts one
# ---------------------------------------------------------------------------


def _mcp_create(**mcp) -> CustomToolCreateRequest:
    return CustomToolCreateRequest(
        id="mcp_read", name="MCP Read", description="d", implementation_type="mcp",
        mcp_config=mcp,
    )


def test_build_http_transport_mcp_definition():
    defn = build_custom_tool_definition(_mcp_create(
        transport="http", url="http://localhost:8811/mcp",
        headers={"Authorization": "${env:GATEWAY_TOKEN}"}, tool_name="read_file",
        server_id="gateway", call_timeout_seconds=120,
    ), actor_user_id="admin-1")

    cfg = defn.mcp_config
    assert cfg is not None
    assert cfg.transport == "http"
    assert cfg.url == "http://localhost:8811/mcp"
    assert cfg.headers == {"Authorization": "${env:GATEWAY_TOKEN}"}
    assert cfg.server_command == ""
    assert cfg.server_id == "gateway"
    assert cfg.call_timeout_seconds == 120


def test_build_stdio_mcp_without_a_command_is_refused():
    with pytest.raises(HTTPException) as excinfo:
        build_custom_tool_definition(_mcp_create(tool_name="read_file"))
    assert excinfo.value.status_code == 400
    assert "server_command is required" in excinfo.value.detail


def test_build_http_transport_mcp_without_a_url_is_refused():
    with pytest.raises(HTTPException) as excinfo:
        build_custom_tool_definition(_mcp_create(transport="http", tool_name="read_file"))
    assert excinfo.value.status_code == 400
    assert "url is required" in excinfo.value.detail


def test_client_field_list_covers_the_core_model_except_the_ciphertext():
    """The drift #124 was: a core field the REST model could not express. The
    pin: every core field is client-settable except the server-owned
    ciphertext, and the REST model declares exactly those."""
    assert set(MCPToolConfig.model_fields) - set(MCP_CLIENT_FIELDS) == {"encrypted_env_vars"}
    assert set(MCPToolConfigModel.model_fields) == set(MCP_CLIENT_FIELDS)


def _http_transport_definition():
    defn = build_custom_tool_definition(_mcp_create(
        transport="http", url="http://localhost:8811/mcp",
        headers={"X-Key": "${env:K}"}, tool_name="read_file",
        server_id="gateway", call_timeout_seconds=120,
    ), actor_user_id="admin-1")
    defn.mcp_config.encrypted_env_vars = {"TOKEN": "gAAAA-server-owned"}
    # The record an admin approved, ciphertext included; the merge is only
    # honoured over an approved record.
    return stamp_custom_tool_approval(defn, approved_by="admin-1")


def test_update_with_the_old_stdio_shaped_payload_keeps_the_http_transport():
    """The desktop form sends the stdio fields only. Before #124 that PUT
    rebuilt the config from those fields and the tool became stdio-shaped
    (with an empty command, so the validator refused it)."""
    defn = _http_transport_definition()

    apply_custom_tool_update(defn, CustomToolUpdateRequest(mcp_config={
        "server_command": "", "server_args": [], "tool_name": "read_file_v2",
        "env_vars": {"A": "1"}, "idle_timeout_seconds": 600,
    }), actor_user_id="admin-1")

    cfg = defn.mcp_config
    assert cfg.transport == "http"
    assert cfg.url == "http://localhost:8811/mcp"
    assert cfg.headers == {"X-Key": "${env:K}"}
    assert cfg.server_id == "gateway"
    assert cfg.encrypted_env_vars == {"TOKEN": "gAAAA-server-owned"}
    assert cfg.call_timeout_seconds == 120
    assert cfg.tool_name == "read_file_v2"
    assert cfg.env_vars == {"A": "1"}
    assert cfg.idle_timeout_seconds == 600


def test_update_can_switch_the_transport_explicitly():
    defn = _http_transport_definition()

    apply_custom_tool_update(defn, CustomToolUpdateRequest(mcp_config={
        "transport": "stdio", "server_command": "npx", "server_args": ["-y", "srv"],
        "tool_name": "read_file",
    }), actor_user_id="admin-1")

    cfg = defn.mcp_config
    assert cfg.transport == "stdio"
    assert cfg.server_command == "npx"
    assert cfg.url == "http://localhost:8811/mcp"  # unsent fields still carry over
    assert cfg.encrypted_env_vars == {"TOKEN": "gAAAA-server-owned"}


def test_update_never_takes_encrypted_env_vars_from_the_client():
    defn = _http_transport_definition()

    apply_custom_tool_update(defn, CustomToolUpdateRequest(mcp_config={
        "tool_name": "read_file", "encrypted_env_vars": {"TOKEN": "planted"},
    }), actor_user_id="admin-1")

    assert defn.mcp_config.encrypted_env_vars == {"TOKEN": "gAAAA-server-owned"}


def test_update_switching_to_http_without_a_url_is_refused_and_leaves_the_record():
    """The loader hands out its cached live object, so a refused update must
    not have applied the name or parameters first (the tool would keep
    running renamed and gate-failing, with nothing on disk to explain it)."""
    defn = stamp_custom_tool_approval(
        build_custom_tool_definition(_mcp_create(server_command="npx", tool_name="read_file")),
        approved_by="admin-1",
    )

    with pytest.raises(HTTPException) as excinfo:
        apply_custom_tool_update(defn, CustomToolUpdateRequest(
            name="Renamed",
            parameters={"p": {"type": "string", "description": "p"}},
            mcp_config={"transport": "http", "tool_name": "read_file"},
        ), actor_user_id="admin-1")
    assert excinfo.value.status_code == 400
    assert defn.name == "MCP Read"
    assert defn.parameters == {}
    assert defn.mcp_config.transport == "stdio"
    assert defn.mcp_config.server_command == "npx"
    assert custom_tool_execution_gate(defn) is None


def test_merged_update_leaves_the_tool_runnable():
    defn = _http_transport_definition()

    apply_custom_tool_update(defn, CustomToolUpdateRequest(mcp_config={
        "server_command": "", "tool_name": "read_file", "idle_timeout_seconds": 600,
    }), actor_user_id="admin-1")

    assert custom_tool_execution_gate(defn) is None
    assert defn.mcp_config.url == "http://localhost:8811/mcp"


def test_partial_update_over_an_unapproved_record_does_not_carry_its_launch_surface():
    """A record planted on disk (never approved) must not become approved
    because an admin nudged a timeout from the stdio-only form: nothing it
    did not send carries over, so the http fields are gone and the save
    fails the transport rule instead of blessing the planted url."""
    planted = _http_transport_definition()
    planted.approved_revision = ""
    assert custom_tool_execution_gate(planted) is not None

    with pytest.raises(HTTPException) as excinfo:
        apply_custom_tool_update(planted, CustomToolUpdateRequest(mcp_config={
            "server_command": "", "tool_name": "read_file", "idle_timeout_seconds": 600,
        }), actor_user_id="admin-1")
    assert excinfo.value.status_code == 400
    assert "not approved" in excinfo.value.detail
    assert "server_command is required" in excinfo.value.detail
    assert planted.mcp_config.url == "http://localhost:8811/mcp"  # untouched
    assert custom_tool_execution_gate(planted) is not None  # still unapproved


def test_full_update_over_an_unapproved_record_stamps_only_what_was_sent():
    planted = _http_transport_definition()
    planted.approved_revision = ""

    apply_custom_tool_update(planted, CustomToolUpdateRequest(mcp_config={
        "transport": "stdio", "server_command": "npx", "tool_name": "read_file",
    }), actor_user_id="admin-1")

    cfg = planted.mcp_config
    assert cfg.transport == "stdio"
    assert cfg.url == ""
    assert cfg.headers == {}
    assert cfg.server_id == ""
    assert cfg.encrypted_env_vars == {}
    assert custom_tool_execution_gate(planted) is None


def test_response_exposes_the_transport_fields_but_not_the_ciphertext():
    from nymeria.api.schemas.custom_tools import custom_tool_definition_to_response

    body = custom_tool_definition_to_response(_http_transport_definition()).model_dump()
    mcp = body["mcp_config"]
    assert mcp["transport"] == "http"
    assert mcp["url"] == "http://localhost:8811/mcp"
    assert mcp["headers"] == {"X-Key": "${env:K}"}
    assert mcp["server_id"] == "gateway"
    assert mcp["call_timeout_seconds"] == 120
    assert "encrypted_env_vars" not in mcp


def test_unified_response_carries_the_same_mcp_client_surface():
    """`/tools/unified` had its own hand-copied field list (transport/url/
    headers but not server_id or call_timeout_seconds); it now shares the
    pin, so the two surfaces cannot disagree about one tool."""
    from nymeria.api.schemas.unified_tools import custom_tool_definition_to_unified

    row = custom_tool_definition_to_unified(_http_transport_definition(), True, "default", {})
    mcp = row.mcp_config
    assert set(mcp) == set(MCP_CLIENT_FIELDS)
    assert mcp["server_id"] == "gateway"
    assert mcp["call_timeout_seconds"] == 120
    assert mcp["url"] == "http://localhost:8811/mcp"
