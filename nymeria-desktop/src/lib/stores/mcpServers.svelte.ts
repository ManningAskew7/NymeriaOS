import { api } from '$lib/services/api.svelte';
import type {
  MCPServer,
  MCPServerCreateRequest,
  MCPServerUpdateRequest,
  MCPDiscoveredTool,
  MCPInstallRequest,
  MCPInstallResponse,
} from '$lib/types';

function createMCPServersStore() {
  let servers = $state<MCPServer[]>([]);
  let loading = $state(false);
  let loaded = $state(false);
  let error = $state<string | null>(null);

  return {
    get servers() { return servers; },
    get loading() { return loading; },
    get loaded() { return loaded; },
    get error() { return error; },

    async load(): Promise<void> {
      if (loading) return;
      loading = true;
      error = null;
      try {
        const response = await api.listMCPServers();
        servers = response.servers;
        loaded = true;
      } catch (e) {
        error = e instanceof Error ? e.message : 'Failed to load MCP servers';
        console.error('Failed to load MCP servers:', e);
      } finally {
        loading = false;
      }
    },

    async refresh(): Promise<void> {
      loaded = false;
      await this.load();
    },

    async create(request: MCPServerCreateRequest, threadId?: string): Promise<{ server: MCPServer; discoveredTools: number; discoveryError?: string }> {
      const result = await api.createMCPServer(request, threadId);
      servers = [...servers, result.server];
      return result;
    },

    async install(request: MCPInstallRequest): Promise<MCPInstallResponse> {
      const result = await api.installMCPServer(request);
      const existing = servers.findIndex(s => s.id === result.server.id);
      if (existing >= 0) {
        servers = servers.map(s => s.id === result.server.id ? result.server : s);
      } else {
        servers = [...servers, result.server];
      }
      return result;
    },

    async update(serverId: string, request: MCPServerUpdateRequest): Promise<{ server: MCPServer; discoveredTools: number; discoveryError?: string }> {
      const result = await api.updateMCPServer(serverId, request);
      servers = servers.map(s => s.id === serverId ? result.server : s);
      return result;
    },

    async remove(serverId: string): Promise<void> {
      await api.deleteMCPServer(serverId);
      servers = servers.filter(s => s.id !== serverId);
    },

    async discover(serverId: string): Promise<{ discoveredTools: MCPDiscoveredTool[]; count: number }> {
      const result = await api.discoverMCPServerTools(serverId);
      // Refresh the server to get updated discovered_tools
      await this.refresh();
      return result;
    },

    async test(serverId: string): Promise<{ status: string; toolsCount?: number; toolNames?: string[]; error?: string }> {
      return api.testMCPServer(serverId);
    },

    getServer(serverId: string): MCPServer | undefined {
      return servers.find(s => s.id === serverId);
    },
  };
}

export const mcpServersStore = createMCPServersStore();
