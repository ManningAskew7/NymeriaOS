import type {
  MCPDiscoveredTool,
  MCPInstallPreviewRequest,
  MCPInstallPreviewResponse,
  MCPInstallRequest,
  MCPInstallResponse,
  MCPServer,
  MCPServerCreateRequest,
  MCPServerListResponse,
  MCPServerUpdateRequest
} from '$lib/types';
import { ToolsApi } from './tools';

export class MCPApi extends ToolsApi {
  private async mcpErrorMessage(response: Response, fallback: string): Promise<string> {
    const err = await response.json().catch(() => ({}));
    const detail = (err as Record<string, unknown>).detail;
    if (typeof detail === 'string') return detail;
    if (detail && typeof detail === 'object') {
      const message = (detail as Record<string, unknown>).message;
      if (typeof message === 'string') return message;
    }
    return `${fallback} (${response.status})`;
  }

  private mcpServerFromResponse(item: Record<string, unknown>): MCPServer {
    return {
      id: item.id as string,
      name: item.name as string,
      description: (item.description as string) || '',
      transport: (item.transport as 'stdio' | 'http') || 'stdio',
      serverCommand: item.server_command as string,
      serverArgs: (item.server_args as string[]) || [],
      url: (item.url as string) || '',
      headers: (item.headers as Record<string, string>) || {},
      envVars: (item.env_vars as Record<string, string>) || {},
      workingDirectory: item.working_directory as string | undefined,
      idleTimeoutSeconds: (item.idle_timeout_seconds as number) || 300,
      startupTimeoutSeconds: (item.startup_timeout_seconds as number) || 30,
      enabled: item.enabled !== false,
      discoveredTools: ((item.discovered_tools as Array<Record<string, unknown>>) || []).map(t => ({
        name: t.name as string,
        description: (t.description as string) || '',
        inputSchema: (t.input_schema as Record<string, unknown>) || {},
      })),
      installStatus: (item.install_status as MCPServer['installStatus']) || 'ready',
      sourceType: (item.source_type as string) || '',
      runtimeType: (item.runtime_type as string) || '',
      originalSource: (item.original_source as string) || '',
      parsedSummary: (item.parsed_summary as string) || '',
      installPlan: (item.install_plan as Record<string, unknown>) || {},
      installLogs: (item.install_logs as string[]) || [],
      lastError: item.last_error as string | undefined,
      missingConfig: (item.missing_config as MCPInstallResponse['missingConfig']) || [],
      credentialRequirements: (item.credential_requirements as MCPInstallResponse['credentialRequirements']) || [],
      riskSignals: (item.risk_signals as MCPServer['riskSignals']) || [],
      registeredToolNames: (item.registered_tool_names as string[]) || [],
      riskLevel: (item.risk_level as string) || 'low',
      confirmationRequired: item.confirmation_required === true,
      createdAt: item.created_at as string,
      updatedAt: item.updated_at as string,
    };
  }

  private mcpInstallResponseFromData(data: Record<string, unknown>): MCPInstallResponse {
    return {
      status: (data.status as MCPInstallResponse['status']) || 'ok',
      server: this.mcpServerFromResponse(data.server as Record<string, unknown>),
      parsedSummary: (data.parsed_summary as string) || '',
      discoveredTools: (data.discovered_tools as number) ?? 0,
      toolNames: (data.tool_names as string[]) || [],
      toolDisplayNames: (data.tool_display_names as string[]) || [],
      threadId: (data.thread_id as string) ?? undefined,
      discoveryError: (data.discovery_error as string) ?? undefined,
      installLogs: (data.install_logs as string[]) || [],
      missingConfig: (data.missing_config as MCPInstallResponse['missingConfig']) || [],
      credentialRequirements: (data.credential_requirements as MCPInstallResponse['credentialRequirements']) || [],
      registeredToolNames: (data.registered_tool_names as string[]) || [],
      requiresConfirmation: data.requires_confirmation === true,
    };
  }

  private mcpCandidateFromResponse(item: Record<string, unknown>) {
    return {
      id: item.id as string,
      title: (item.title as string) || (item.id as string),
      source: (item.source as string) || '',
      server: this.mcpServerFromResponse(item.server as Record<string, unknown>),
      plan: item.plan as import('$lib/types').MCPInstallPlan,
      credential_requirements: (item.credential_requirements as import('$lib/types').MCPCredentialRequirement[]) || [],
      risk_signals: (item.risk_signals as import('$lib/types').MCPRiskSignal[]) || [],
      install_steps: (item.install_steps as import('$lib/types').MCPInstallStep[]) || [],
      can_install: item.can_install !== false,
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

  async previewMCPServerInstall(request: MCPInstallPreviewRequest): Promise<MCPInstallPreviewResponse> {
    const response = await fetch(`${this.getBaseUrl()}/mcp-servers/install/preview`, {
      method: 'POST',
      headers: this.getHeaders(),
      body: JSON.stringify(request)
    });
    if (!response.ok) {
      throw new Error(await this.mcpErrorMessage(response, 'Preview failed'));
    }
    const data = await response.json() as Record<string, unknown>;
    return {
      previewToken: data.preview_token as string,
      selectedCandidateId: (data.selected_candidate_id as string) ?? null,
      candidateId: (data.candidate_id as string) ?? null,
      server: this.mcpServerFromResponse(data.server as Record<string, unknown>),
      plan: data.plan as MCPInstallPreviewResponse['plan'],
      candidates: ((data.candidates as Array<Record<string, unknown>>) || []).map((item) => this.mcpCandidateFromResponse(item)),
      credentialRequirements: (data.credential_requirements as MCPInstallPreviewResponse['credentialRequirements']) || [],
      riskSignals: (data.risk_signals as MCPInstallPreviewResponse['riskSignals']) || [],
      installSteps: (data.install_steps as MCPInstallPreviewResponse['installSteps']) || [],
      canInstall: data.can_install !== false,
    };
  }

  async installMCPServer(request: MCPInstallRequest): Promise<MCPInstallResponse> {
    const response = await fetch(`${this.getBaseUrl()}/mcp-servers/install`, {
      method: 'POST',
      headers: this.getHeaders(),
      body: JSON.stringify(request)
    });
    if (!response.ok) {
      throw new Error(await this.mcpErrorMessage(response, 'Installation failed'));
    }
    const data = await response.json() as Record<string, unknown>;
    return this.mcpInstallResponseFromData(data);
  }

  async retryMCPServerInstall(serverId: string, request: Pick<MCPInstallRequest, 'confirmed' | 'confirmed_risk_ids' | 'config_values' | 'credential_values' | 'credential_bindings'> = {}): Promise<MCPInstallResponse> {
    const response = await fetch(`${this.getBaseUrl()}/mcp-servers/${serverId}/retry`, {
      method: 'POST',
      headers: this.getHeaders(),
      body: JSON.stringify(request)
    });
    if (!response.ok) {
      throw new Error(await this.mcpErrorMessage(response, 'Retry failed'));
    }
    const data = await response.json() as Record<string, unknown>;
    return this.mcpInstallResponseFromData(data);
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
