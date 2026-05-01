#!/usr/bin/env python3
"""Run HexStrike's upstream MCP wrapper with a curated tool allowlist.

The upstream project exposes a very broad MCP surface. For Nymeria we keep the
container installed with useful Kali tooling but only expose the small set in
allowed-tools.txt by default. Set HEXSTRIKE_ALLOWED_TOOLS to a comma/newline
separated list, or mount a different HEXSTRIKE_ALLOWED_TOOLS_FILE, to change it.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Iterable, Set

from hexstrike_mcp import (
    DEFAULT_HEXSTRIKE_SERVER,
    DEFAULT_REQUEST_TIMEOUT,
    HexStrikeClient,
    setup_mcp_server,
)


logging.basicConfig(
    level=logging.INFO,
    format="[HexStrike MCP Limited] %(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)


DEFAULT_ALLOWED_TOOLS = {
    "server_health",
    "get_cache_stats",
    "get_telemetry",
    "display_system_metrics",
    "nmap_scan",
    "nmap_advanced_scan",
    "gobuster_scan",
    "ffuf_scan",
    "dirsearch_scan",
    "dirb_scan",
    "nikto_scan",
    "nuclei_scan",
    "sqlmap_scan",
    "wafw00f_scan",
    "subfinder_scan",
    "amass_scan",
    "dnsenum_scan",
    "fierce_scan",
    "httpx_probe",
    "hakrawler_crawl",
    "gau_discovery",
    "arjun_scan",
    "arjun_parameter_discovery",
    "paramspider_discovery",
    "paramspider_mining",
    "jwt_analyzer",
    "api_schema_analyzer",
    "graphql_scanner",
    "analyze_target_intelligence",
    "select_optimal_tools_ai",
    "optimize_tool_parameters_ai",
    "detect_technologies_ai",
    "intelligent_smart_scan",
    "ai_reconnaissance_workflow",
    "ai_vulnerability_assessment",
    "create_vulnerability_report",
    "create_scan_summary",
}


def _split_tool_names(raw: str) -> Set[str]:
    names: Set[str] = set()
    for line in raw.splitlines():
        content = line.split("#", 1)[0]
        for part in content.replace(",", "\n").splitlines():
            item = part.strip()
            if item:
                names.add(item)
    return names


def _read_allowed_tools() -> Set[str]:
    env_value = os.environ.get("HEXSTRIKE_ALLOWED_TOOLS", "")
    if env_value.strip():
        return _split_tool_names(env_value)

    file_path = Path(
        os.environ.get(
            "HEXSTRIKE_ALLOWED_TOOLS_FILE",
            "/opt/hexstrike-ai/allowed-tools.txt",
        )
    )
    if file_path.exists():
        names = _split_tool_names(file_path.read_text(encoding="utf-8"))
        if names:
            return names

    return set(DEFAULT_ALLOWED_TOOLS)


def _registered_tool_names(mcp) -> Set[str]:
    manager = getattr(mcp, "_tool_manager", None)
    tools = getattr(manager, "_tools", {})
    return set(tools.keys())


def _filter_tools(mcp, allowed: Iterable[str]) -> None:
    allowed_set = set(allowed)
    before = _registered_tool_names(mcp)
    missing = sorted(allowed_set - before)

    for tool_name in sorted(before - allowed_set):
        mcp.remove_tool(tool_name)

    after = _registered_tool_names(mcp)
    logger.info("Filtered HexStrike MCP tools: %d exposed, %d removed", len(after), len(before - after))
    if missing:
        logger.warning("Allowed tool names not present upstream: %s", ", ".join(missing))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run limited HexStrike AI MCP")
    parser.add_argument(
        "--server",
        type=str,
        default=DEFAULT_HEXSTRIKE_SERVER,
        help=f"HexStrike AI API server URL (default: {DEFAULT_HEXSTRIKE_SERVER})",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_REQUEST_TIMEOUT,
        help=f"Request timeout in seconds (default: {DEFAULT_REQUEST_TIMEOUT})",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.debug:
        logger.setLevel(logging.DEBUG)

    client = HexStrikeClient(args.server, args.timeout)
    health = client.check_health()
    if "error" in health:
        logger.warning("HexStrike API health check failed: %s", health["error"])
    else:
        logger.info("Connected to HexStrike API: status=%s version=%s", health.get("status"), health.get("version"))

    mcp = setup_mcp_server(client)
    _filter_tools(mcp, _read_allowed_tools())
    mcp.run()


if __name__ == "__main__":
    main()
