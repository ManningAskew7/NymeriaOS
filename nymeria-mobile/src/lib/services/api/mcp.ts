import type {
  MCPDiscoveredTool,
  MCPServer,
  MCPServerCreateRequest,
  MCPServerListResponse,
  MCPServerUpdateRequest
} from '$lib/types';
import { ToolsApi } from './tools';

export class MCPApi extends ToolsApi {
  private mcpServerFromResponse(item: Record<string, unknown>): MCPServer {
    return {
      id: item.id as string,
      name: item.name as string,
      description: (item.description as string) || '',
      serverCommand: item.server_command as string,
      serverArgs: (item.server_args as string[]) || [],
      envVars: (item.env_vars as Record<string, string>) || {},
      workingDirectory: item.working_directory as string | undefined,
      idleTimeoutSeconds: (item.idle_timeout_seconds as number) || 300,
      startupTimeoutSeconds: (item.startup_timeout_seconds as number) || 30,
      enabled: item.enabled as boolean,
      discoveredTools: ((item.discovered_tools as Array<Record<string, unknown>>) || []).map(t => ({
        name: t.name as string,
        description: (t.description as string) || '',
        inputSchema: (t.input_schema as Record<string, unknown>) || {},
      })),
      createdAt: item.created_at as string,
      updatedAt: item.updated_at as string,
    };
  }
  async listMCPServers(): Promise<MCPServerListResponse> {
    const response = await fetch(`${this.getBaseUrl()}/mcp-servers`, {
      headers: this.getHeaders()
    });
    if (!response.ok) throw new Error(`API error: ${response.status}`);
    const data = await response.json();
    return {
      servers: (data.servers || []).map((s: Record<string, unknown>) => this.mcpServerFromResponse(s)),
      total: data.total,
    };
  }
  async createMCPServer(request: MCPServerCreateRequest, threadId?: string): Promise<{ server: MCPServer; discoveredTools: number; discoveryError?: string }> {
    const url = threadId
      ? `${this.getBaseUrl()}/mcp-servers?thread_id=${encodeURIComponent(threadId)}`
      : `${this.getBaseUrl()}/mcp-servers`;
    const response = await fetch(url, {
      method: 'POST',
      headers: this.getHeaders(),
      body: JSON.stringify(request)
    });
    if (!response.ok) {
      const err = await response.json().catch(() => ({}));
      throw new Error(err.detail || `API error: ${response.status}`);
    }
    const data = await response.json();
    return {
      server: this.mcpServerFromResponse(data.server),
      discoveredTools: data.discovered_tools,
      discoveryError: data.discovery_error,
    };
  }
  async updateMCPServer(serverId: string, request: MCPServerUpdateRequest): Promise<{ server: MCPServer; discoveredTools: number; discoveryError?: string }> {
    const response = await fetch(`${this.getBaseUrl()}/mcp-servers/${serverId}`, {
      method: 'PUT',
      headers: this.getHeaders(),
      body: JSON.stringify(request)
    });
    if (!response.ok) throw new Error(`API error: ${response.status}`);
    const data = await response.json();
    return {
      server: this.mcpServerFromResponse(data.server),
      discoveredTools: data.discovered_tools,
      discoveryError: data.discovery_error,
    };
  }
  async deleteMCPServer(serverId: string): Promise<void> {
    const response = await fetch(`${this.getBaseUrl()}/mcp-servers/${serverId}`, {
      method: 'DELETE',
      headers: this.getHeaders()
    });
    if (!response.ok) throw new Error(`API error: ${response.status}`);
  }
  async discoverMCPServerTools(serverId: string): Promise<{ discoveredTools: MCPDiscoveredTool[]; count: number }> {
    const response = await fetch(`${this.getBaseUrl()}/mcp-servers/${serverId}/discover`, {
      method: 'POST',
      headers: this.getHeaders()
    });
    if (!response.ok) {
      const err = await response.json().catch(() => ({}));
      throw new Error(err.detail || `API error: ${response.status}`);
    }
    const data = await response.json();
    return {
      discoveredTools: (data.discovered_tools || []).map((t: Record<string, unknown>) => ({
        name: t.name as string,
        description: (t.description as string) || '',
        inputSchema: (t.input_schema as Record<string, unknown>) || {},
      })),
      count: data.count,
    };
  }
  async testMCPServer(serverId: string): Promise<{ status: string; toolsCount?: number; toolNames?: string[]; error?: string }> {
    const response = await fetch(`${this.getBaseUrl()}/mcp-servers/${serverId}/test`, {
      method: 'POST',
      headers: this.getHeaders()
    });
    if (!response.ok) throw new Error(`API error: ${response.status}`);
    const data = await response.json();
    return {
      status: data.status,
      toolsCount: data.tools_count,
      toolNames: data.tool_names,
      error: data.error,
    };
  }
}
