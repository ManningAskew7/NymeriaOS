import { api } from '$lib/services/api.svelte';
import { humanizeErrorText } from '$lib/services/api/humanizeError';
import { configStore, registerIdentityReloadHook } from './config.svelte';
import type {
  MCPServer,
  MCPServerCreateRequest,
  MCPServerUpdateRequest,
  MCPDiscoveredTool,
  MCPInstallRequest,
  MCPInstallResponse,
  MCPInstallPreviewRequest,
  MCPInstallPreviewResponse,
} from '$lib/types';

// Exported for the store's tests only: each instance registers an identity
// reload hook for life, so the app uses the single `mcpServersStore`.
export function createMCPServersStore() {
  let servers = $state<MCPServer[]>([]);
  let loading = $state(false);
  // Also latched by a FAILED load (with `error` set), so the panel effects
  // that keep this store loaded stop re-running it: a failing GET
  // /mcp-servers used to be asked again on every settle, as fast as the
  // backend refused it (#445). refresh() is the way to ask again.
  let loaded = $state(false);
  let error = $state<string | null>(null);
  // A 403 latches silently, the serverSettings shape (#381): the route is
  // admin-only, so a refusal is not a failure to show.
  let latched403 = $state(false);
  // Bumped by the reload hook: a response from the previous backend lands nowhere.
  let identityGeneration = 0;

  // A KNOWN non-admin never asks (GET /mcp-servers is require_admin_user).
  // Derived, not latched, so a role promotion un-settles the store.
  function knownNonAdmin(): boolean {
    const role = configStore.identity?.role;
    return !!role && role !== 'admin';
  }

  // MCP servers are keyed by human-chosen ids (`github`), so a stale row
  // from the previous backend could act on the new backend's like-named
  // server. Drop the list on every connection switch (#242).
  registerIdentityReloadHook(() => {
    identityGeneration += 1;
    servers = [];
    loading = false;
    loaded = false;
    error = null;
    latched403 = false;
  });

  function current(generation: number): boolean {
    return generation === identityGeneration;
  }

  return {
    get servers() { return servers; },
    get loading() { return loading; },
    get loaded() { return loaded; },
    get error() { return error; },
    /** The account cannot manage MCP servers: a latched 403 or a known non-admin. */
    get forbidden() { return latched403 || knownNonAdmin(); },

    async load(): Promise<void> {
      if (loading || loaded || latched403 || knownNonAdmin()) return;
      const generation = identityGeneration;
      loading = true;
      error = null;
      try {
        const response = await api.listMCPServers();
        if (!current(generation)) return;
        servers = response.servers;
        loaded = true;
      } catch (e) {
        if (!current(generation)) return;
        if ((e as { status?: number } | null)?.status === 403) {
          latched403 = true;
          return;
        }
        error = humanizeErrorText(e, { action: 'load', resource: 'your MCP servers' });
        loaded = true;
        console.error('Failed to load MCP servers:', e);
      } finally {
        if (current(generation)) loading = false;
      }
    },

    async refresh(): Promise<void> {
      loaded = false;
      latched403 = false;
      // `error` clears when the load starts; an in-flight load lands on its
      // own and load() refuses to race it.
      await this.load();
    },

    async create(request: MCPServerCreateRequest, threadId?: string): Promise<{ server: MCPServer; discoveredTools: number; discoveryError?: string }> {
      const generation = identityGeneration;
      const result = await api.createMCPServer(request, threadId);
      if (current(generation)) servers = [...servers, result.server];
      return result;
    },

    async install(request: MCPInstallRequest): Promise<MCPInstallResponse> {
      const generation = identityGeneration;
      const result = await api.installMCPServer(request);
      if (!current(generation)) return result;
      const existing = servers.findIndex(s => s.id === result.server.id);
      if (existing >= 0) {
        servers = servers.map(s => s.id === result.server.id ? result.server : s);
      } else {
        servers = [...servers, result.server];
      }
      return result;
    },

    async preview(request: MCPInstallPreviewRequest): Promise<MCPInstallPreviewResponse> {
      return api.previewMCPServerInstall(request);
    },

    async previewUpload(file: File, name?: string): Promise<MCPInstallPreviewResponse> {
      return api.previewMCPServerUpload(file, name);
    },

    async retry(serverId: string, request: Pick<MCPInstallRequest, 'confirmed' | 'confirmed_risk_ids' | 'config_values' | 'credential_values' | 'credential_bindings'> = {}): Promise<MCPInstallResponse> {
      const generation = identityGeneration;
      const result = await api.retryMCPServerInstall(serverId, request);
      if (current(generation)) servers = servers.map(s => s.id === serverId ? result.server : s);
      return result;
    },

    async update(serverId: string, request: MCPServerUpdateRequest): Promise<{ server: MCPServer; discoveredTools: number; discoveryError?: string }> {
      const generation = identityGeneration;
      const result = await api.updateMCPServer(serverId, request);
      if (current(generation)) servers = servers.map(s => s.id === serverId ? result.server : s);
      return result;
    },

    async remove(serverId: string): Promise<void> {
      const generation = identityGeneration;
      await api.deleteMCPServer(serverId);
      if (current(generation)) servers = servers.filter(s => s.id !== serverId);
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
