"""Tests for managed MCP install planning."""

from __future__ import annotations

import json
import zipfile

from nymeria.core.mcp_runtime import plan_bundle_file, plan_text_source


def test_plan_npm_package_url_uses_npx_runtime():
    defn, plan = plan_text_source("https://www.npmjs.com/package/@modelcontextprotocol/server-filesystem")

    assert plan.source_type == "npm"
    assert plan.runtime_type == "npx"
    assert plan.confirmation_required is False
    assert defn.server_command == "npx"
    assert defn.server_args[:2] == ["-y", "@modelcontextprotocol/server-filesystem"]


def test_plan_git_url_requires_confirmation_without_ready_command():
    defn, plan = plan_text_source("https://github.com/example/example-mcp-server")

    assert plan.source_type == "git"
    assert plan.runtime_type == "git"
    assert plan.confirmation_required is True
    assert plan.risk_level == "medium"
    assert defn.install_status == "draft"
    assert defn.server_command == ""


def test_plan_claude_json_detects_missing_secret_env():
    source = {
        "mcpServers": {
            "example": {
                "command": "uvx",
                "args": ["example-mcp"],
                "env": {"EXAMPLE_API_KEY": ""},
            }
        }
    }

    _defn, plan = plan_text_source(json.dumps(source))

    assert plan.source_type == "json"
    assert plan.runtime_type == "uvx"
    assert plan.required_config[0]["name"] == "EXAMPLE_API_KEY"
    assert plan.required_config[0]["sensitive"] is True


def test_plan_extracts_command_from_markdown_fence():
    source = """
Install with:

```bash
npx -y @example/mcp-server
```
"""

    defn, plan = plan_text_source(source)

    assert plan.source_type == "stdio"
    assert plan.runtime_type == "npx"
    assert defn.server_command == "npx"
    assert defn.server_args == ["-y", "@example/mcp-server"]


def test_plan_bundle_upload_reads_manifest_user_config(tmp_path):
    bundle = tmp_path / "example.mcpb"
    manifest = {
        "name": "example-bundle",
        "display_name": "Example Bundle",
        "description": "Example MCP bundle",
        "user_config": {
            "token": {
                "title": "API token",
                "type": "string",
                "required": True,
                "sensitive": True,
            }
        },
        "server": {
            "type": "node",
            "entry_point": "server.js",
        },
    }
    with zipfile.ZipFile(bundle, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("server.js", "console.error('noop');")

    defn, plan = plan_bundle_file(bundle, original_name="example.mcpb")

    assert defn.name == "Example Bundle"
    assert plan.source_type == "bundle_upload"
    assert plan.runtime_type == "bundle"
    assert plan.confirmation_required is True
    assert plan.required_config[0]["name"] == "token"
    assert plan.required_config[0]["sensitive"] is True
