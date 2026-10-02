import { afterEach, describe, expect, it, vi } from 'vitest';

// #445: the MCP servers store latches a 403 as a silent "admin-managed"
// state, so the status must ride the error listMCPServers throws (the
// getServerSettings shape, #381).
vi.mock('./tools', () => ({
  ToolsApi: class {
    getBaseUrl() { return 'http://test'; }
    getHeaders() { return {}; }
  },
}));

import { MCPApi } from './mcp';

afterEach(() => vi.unstubAllGlobals());

describe('MCPApi.listMCPServers', () => {
  it.each([403, 500])('a %s rejects with the HTTP status on the error', async (status) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{"detail":"no"}', { status })));
    await expect(new MCPApi().listMCPServers()).rejects.toMatchObject({ status });
  });

  it('a 200 maps the servers', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      servers: [{ id: 'github', name: 'GitHub', discovered_tools: [] }],
      total: 1,
    })));
    vi.stubGlobal('fetch', fetchMock);

    const result = await new MCPApi().listMCPServers();

    expect(fetchMock).toHaveBeenCalledWith('http://test/mcp-servers', expect.anything());
    expect(result.total).toBe(1);
    expect(result.servers.map((s) => s.id)).toEqual(['github']);
  });
});
