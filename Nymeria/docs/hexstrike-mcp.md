# HexStrike MCP Sidecar

HexStrike AI is installed as an optional Docker sidecar, not inside the main
Nymeria API image. This keeps the broad pentesting toolchain isolated while
still letting both Codex and Nymeria connect through native MCP.

## Architecture

- `hexstrike-api`: Kali Rolling container running upstream HexStrike's Flask API
  on port `8888`.
- `hexstrike-mcp`: same image, running HexStrike's stdio MCP wrapper through
  Supergateway as Streamable HTTP at `/mcp` on port `8889`.
- Host ports bind to `127.0.0.1` only.
- Nymeria containers connect to `http://hexstrike-mcp:8889/mcp`.
- Codex connects to `http://localhost:8889/mcp`.

The image pins upstream `0x4m4/hexstrike-ai` to commit
`9b8c780f324ce5145a322bfa23c98886f8424ba3` and uses `supergateway@3.4.3`.
It also adds compatibility aliases for Kali package names: `httpx-toolkit` is
available as `httpx`, and `getallurls` is available as `gau`.

## Start / Stop

From `Nymeria/`:

```bash
docker compose --env-file .env.docker -f docker-compose.yml -f docker-compose.hexstrike.yml up -d --build hexstrike-api hexstrike-mcp
docker compose --env-file .env.docker -f docker-compose.yml -f docker-compose.hexstrike.yml logs -f hexstrike-api hexstrike-mcp
docker compose --env-file .env.docker -f docker-compose.yml -f docker-compose.hexstrike.yml down
```

## Curated Tool Surface

The upstream MCP exposes a very large tool list. The sidecar defaults to
`docker/hexstrike/allowed-tools.txt`, which keeps reconnaissance, web testing,
API testing, target intelligence, and reporting tools with Kali packages
available in the image. It omits password cracking, payload generation, raw
command execution, file mutation, binary exploitation, cloud exploitation,
process-control tools, and bulky overlapping scanners such as Feroxbuster.

Override the list with either:

```bash
HEXSTRIKE_ALLOWED_TOOLS=nmap_scan,nuclei_scan,server_health
```

or by mounting a different file and setting:

```bash
HEXSTRIKE_ALLOWED_TOOLS_FILE=/path/in/container/allowed-tools.txt
```

## Nymeria Registration

Register the sidecar as an HTTP MCP server:

```json
{
  "id": "hexstrike",
  "name": "HexStrike",
  "description": "Curated HexStrike AI MCP sidecar for authorized security testing",
  "transport": "http",
  "url": "http://hexstrike-mcp:8889/mcp",
  "idle_timeout_seconds": 900,
  "startup_timeout_seconds": 60,
  "enabled": true
}
```

After discovery, enable individual `mcp__hexstrike__...` tools per thread. Keep
this opt-in; do not add HexStrike tools to the global default tool set.

## Safety Notes

Use HexStrike only for systems you own or are explicitly authorized to test.
The container is not privileged and is attached only to the Nymeria Docker
network plus its localhost-bound host ports, but the exposed tools can still run
network scans from the host's network environment.
