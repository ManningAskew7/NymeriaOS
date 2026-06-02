import type {
  CustomTool,
  CustomToolCreateRequest,
  CustomToolListResponse,
  CustomToolTestResponse,
  CustomToolUpdateRequest,
  HTTPToolConfig,
  Tool,
  ToolSearchResponse,
  UnifiedTool,
  UnifiedToolListResponse
} from '$lib/types';
import { NotificationsApi } from './notifications';

export class ToolsApi extends NotificationsApi {
  async getTools(): Promise<Tool[]> {
    const response = await fetch(`${this.getBaseUrl()}/tools`, {
      headers: this.getHeaders()
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }

    return response.json();
  }

  private toolSearchFromResponse(data: Record<string, unknown>): ToolSearchResponse {
    return {
      query: (data.query as string) || '',
      mode: (data.mode as ToolSearchResponse['mode']) || 'substring',
      warning: data.warning as string | null | undefined,
      results: ((data.results as Record<string, unknown>[]) || []).map((item) => ({
        name: item.name as string,
        description: (item.description as string) || '',
        category: (item.category as string) || 'unknown',
        securityLevel: (item.security_level as string) || 'moderate',
        toolType: (item.tool_type as string) || 'builtin',
        isDefault: Boolean(item.is_default),
        status: (item.status as string | null | undefined) ?? null,
        score: Number(item.score ?? 0),
        enableHint: (item.enable_hint as string) || '',
        group: (item.group as string | null | undefined) ?? null,
        groupLabel: (item.group_label as string | null | undefined) ?? null,
        service: (item.service as string | null | undefined) ?? null,
        serviceLabel: (item.service_label as string | null | undefined) ?? null
      }))
    };
  }

  async searchTools(options: {
    query?: string;
    category?: string;
    threadId?: string;
    topK?: number;
    includeStatus?: boolean;
    userId?: string;
  } = {}): Promise<ToolSearchResponse> {
    const id = this.resolveUserId(options.userId);
    const params = new URLSearchParams();
    if (options.query !== undefined) params.set('query', options.query);
    if (options.category) params.set('category', options.category);
    if (options.threadId) params.set('thread_id', options.threadId);
    if (options.topK !== undefined) params.set('top_k', String(options.topK));
    if (options.includeStatus !== undefined) {
      params.set('include_status', String(options.includeStatus));
    }

    const response = await fetch(
      `${this.getBaseUrl()}/users/${id}/tools/search?${params}`,
      { headers: this.getHeaders() }
    );

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }

    return this.toolSearchFromResponse(await response.json());
  }

  // =========================================================================
  // Custom Tools API
  // =========================================================================

  private customToolFromResponse(item: Record<string, unknown>): CustomTool {
    return {
      id: item.id as string,
      name: item.name as string,
      description: item.description as string,
      parameters: item.parameters as Record<string, unknown> as CustomTool['parameters'],
      implementationType: item.implementation_type as CustomTool['implementationType'],
      httpConfig: item.http_config
        ? {
            method: (item.http_config as Record<string, unknown>).method as HTTPToolConfig['method'],
            url: (item.http_config as Record<string, unknown>).url as string,
            headers: ((item.http_config as Record<string, unknown>).headers || {}) as Record<string, string>,
            bodyTemplate: (item.http_config as Record<string, unknown>).body_template as string | undefined,
            queryParams: ((item.http_config as Record<string, unknown>).query_params || {}) as Record<string, string>,
            timeoutSeconds: ((item.http_config as Record<string, unknown>).timeout_seconds || 30) as number,
            responsePath: (item.http_config as Record<string, unknown>).response_path as string | undefined,
            responseFormat: ((item.http_config as Record<string, unknown>).response_format || 'auto') as HTTPToolConfig['responseFormat']
          }
        : undefined,
      mcpConfig: item.mcp_config
        ? {
            serverCommand: (item.mcp_config as Record<string, unknown>).server_command as string,
            serverArgs: ((item.mcp_config as Record<string, unknown>).server_args || []) as string[],
            toolName: (item.mcp_config as Record<string, unknown>).tool_name as string,
            envVars: ((item.mcp_config as Record<string, unknown>).env_vars || {}) as Record<string, string>,
            workingDirectory: (item.mcp_config as Record<string, unknown>).working_directory as string | undefined,
            idleTimeoutSeconds: ((item.mcp_config as Record<string, unknown>).idle_timeout_seconds || 300) as number,
            startupTimeoutSeconds: ((item.mcp_config as Record<string, unknown>).startup_timeout_seconds || 30) as number
          }
        : undefined,
      enabled: (item.enabled ?? true) as boolean,
      tags: (item.tags || []) as string[],
      createdAt: this.parseUtcTimestamp(item.created_at as string),
      updatedAt: this.parseUtcTimestamp(item.updated_at as string)
    };
  }

  async getCustomTools(): Promise<CustomToolListResponse> {
    const response = await fetch(`${this.getBaseUrl()}/tools/custom`, {
      headers: this.getHeaders()
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }

    const data = await response.json();
    return {
      tools: (data.tools || []).map((item: Record<string, unknown>) =>
        this.customToolFromResponse(item)
      ),
      total: data.total
    };
  }

  async createCustomTool(request: CustomToolCreateRequest): Promise<CustomTool> {
    const body: Record<string, unknown> = {
      id: request.id,
      name: request.name,
      description: request.description,
      parameters: request.parameters || {},
      implementation_type: request.implementationType,
      enabled: request.enabled ?? true,
      tags: request.tags || []
    };

    if (request.httpConfig) {
      body.http_config = {
        method: request.httpConfig.method,
        url: request.httpConfig.url,
        headers: request.httpConfig.headers || {},
        body_template: request.httpConfig.body_template,
        query_params: request.httpConfig.query_params || {},
        timeout_seconds: request.httpConfig.timeout_seconds || 30,
        response_path: request.httpConfig.response_path,
        response_format: request.httpConfig.response_format || 'auto'
      };
    }

    if (request.mcpConfig) {
      body.mcp_config = {
        server_command: request.mcpConfig.server_command,
        server_args: request.mcpConfig.server_args || [],
        tool_name: request.mcpConfig.tool_name,
        env_vars: request.mcpConfig.env_vars || {},
        working_directory: request.mcpConfig.working_directory,
        idle_timeout_seconds: request.mcpConfig.idle_timeout_seconds || 300,
        startup_timeout_seconds: request.mcpConfig.startup_timeout_seconds || 30
      };
    }

    const response = await fetch(`${this.getBaseUrl()}/tools/custom`, {
      method: 'POST',
      headers: this.getHeaders(),
      body: JSON.stringify(body)
    });

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(`API error: ${response.status} - ${errorText}`);
    }

    const data = await response.json();
    return this.customToolFromResponse(data);
  }

  async getCustomTool(toolId: string): Promise<CustomTool> {
    const response = await fetch(`${this.getBaseUrl()}/tools/custom/${toolId}`, {
      headers: this.getHeaders()
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }

    const data = await response.json();
    return this.customToolFromResponse(data);
  }

  async updateCustomTool(toolId: string, request: CustomToolUpdateRequest): Promise<CustomTool> {
    const body: Record<string, unknown> = {};

    if (request.name !== undefined) body.name = request.name;
    if (request.description !== undefined) body.description = request.description;
    if (request.parameters !== undefined) body.parameters = request.parameters;
    if (request.enabled !== undefined) body.enabled = request.enabled;
    if (request.tags !== undefined) body.tags = request.tags;

    if (request.httpConfig) {
      body.http_config = {
        method: request.httpConfig.method,
        url: request.httpConfig.url,
        headers: request.httpConfig.headers || {},
        body_template: request.httpConfig.body_template,
        query_params: request.httpConfig.query_params || {},
        timeout_seconds: request.httpConfig.timeout_seconds || 30,
        response_path: request.httpConfig.response_path,
        response_format: request.httpConfig.response_format || 'auto'
      };
    }

    if (request.mcpConfig) {
      body.mcp_config = {
        server_command: request.mcpConfig.server_command,
        server_args: request.mcpConfig.server_args || [],
        tool_name: request.mcpConfig.tool_name,
        env_vars: request.mcpConfig.env_vars || {},
        working_directory: request.mcpConfig.working_directory,
        idle_timeout_seconds: request.mcpConfig.idle_timeout_seconds || 300,
        startup_timeout_seconds: request.mcpConfig.startup_timeout_seconds || 30
      };
    }

    const response = await fetch(`${this.getBaseUrl()}/tools/custom/${toolId}`, {
      method: 'PUT',
      headers: this.getHeaders(),
      body: JSON.stringify(body)
    });

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(`API error: ${response.status} - ${errorText}`);
    }

    const data = await response.json();
    return this.customToolFromResponse(data);
  }

  async deleteCustomTool(toolId: string): Promise<void> {
    const response = await fetch(`${this.getBaseUrl()}/tools/custom/${toolId}`, {
      method: 'DELETE',
      headers: this.getHeaders()
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }
  }

  async testCustomTool(toolId: string, params: Record<string, unknown>): Promise<CustomToolTestResponse> {
    const startTime = performance.now();
    const response = await fetch(`${this.getBaseUrl()}/tools/custom/${toolId}/test`, {
      method: 'POST',
      headers: this.getHeaders(),
      body: JSON.stringify({ params })
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }

    const data = await response.json();
    const executionTimeMs = Math.round(performance.now() - startTime);
    return {
      status: data.status,
      toolId: data.tool_id,
      result: data.result,
      error: data.error,
      success: data.status === 'ok',
      executionTimeMs
    };
  }

  async exportCustomTools(): Promise<{ tools: CustomTool[]; total: number; exportedAt: string }> {
    const response = await fetch(`${this.getBaseUrl()}/tools/custom/export`, {
      headers: this.getHeaders()
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }

    const data = await response.json();
    return {
      tools: (data.tools || []).map((item: Record<string, unknown>) =>
        this.customToolFromResponse(item)
      ),
      total: data.total,
      exportedAt: data.exported_at
    };
  }

  async importCustomTools(
    tools: CustomToolCreateRequest[]
  ): Promise<{ imported: number; errors: string[] }> {
    const response = await fetch(`${this.getBaseUrl()}/tools/custom/import`, {
      method: 'POST',
      headers: this.getHeaders(),
      body: JSON.stringify({ tools })
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }

    return response.json();
  }

  // =========================================================================
  // Unified Tools API
  // =========================================================================

  private unifiedToolFromResponse(item: Record<string, unknown>): UnifiedTool {
    return {
      id: item.id as string,
      name: item.name as string,
      description: item.description as string,
      defaultDescription: item.default_description as string,
      customDescription: item.custom_description as string | null | undefined,
      category: item.category as string,
      securityLevel: item.security_level as UnifiedTool['securityLevel'],
      enabled: item.enabled as boolean,
      enabledReason: item.enabled_reason as UnifiedTool['enabledReason'],
      toolType: item.tool_type as UnifiedTool['toolType'],
      implementationType: (item.implementation_type as UnifiedTool['implementationType']) || null,
      configSchema: item.config_schema as Record<string, unknown> | undefined,
      userConfig: (item.user_config as Record<string, unknown>) || {},
      configurable: item.configurable as boolean,
      parameters: item.parameters as Record<string, unknown> | undefined,
      httpConfig: item.http_config as UnifiedTool['httpConfig'],
      mcpConfig: item.mcp_config as UnifiedTool['mcpConfig'],
      tags: (item.tags as string[]) || [],
      editable: item.editable as boolean,
      group: (item.group as string | null | undefined) ?? null,
      groupLabel: (item.group_label as string | null | undefined) ?? null,
      service: (item.service as string | null | undefined) ?? null,
      serviceLabel: (item.service_label as string | null | undefined) ?? null,
      createdAt: item.created_at as string | undefined,
      updatedAt: item.updated_at as string | undefined
    };
  }

  async getUnifiedTools(userId?: string): Promise<UnifiedToolListResponse> {
    const id = this.resolveUserId(userId);
    const response = await fetch(`${this.getBaseUrl()}/users/${id}/tools/unified`, {
      headers: this.getHeaders()
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }

    const data = await response.json();
    return {
      tools: (data.tools || []).map((item: Record<string, unknown>) =>
        this.unifiedToolFromResponse(item)
      ),
      total: data.total,
      builtinCount: data.builtin_count,
      customCount: data.custom_count
    };
  }

  async setUnifiedToolEnabled(
    toolId: string,
    enabled: boolean,
    userId?: string
  ): Promise<{ status: string; toolId: string; enabled: boolean; toolType: string }> {
    const id = this.resolveUserId(userId);
    const response = await fetch(
      `${this.getBaseUrl()}/users/${id}/tools/unified/${toolId}/enable`,
      {
        method: 'PUT',
        headers: this.getHeaders(),
        body: JSON.stringify({ enabled })
      }
    );

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(`API error: ${response.status} - ${errorText}`);
    }

    const data = await response.json();
    return {
      status: data.status,
      toolId: data.tool_id,
      enabled: data.enabled,
      toolType: data.tool_type
    };
  }

  async setUnifiedToolDescription(
    toolId: string,
    description: string | null,
    userId?: string
  ): Promise<{ status: string; toolId: string; action: string; description: string | null }> {
    const id = this.resolveUserId(userId);
    const response = await fetch(
      `${this.getBaseUrl()}/users/${id}/tools/unified/${toolId}/description`,
      {
        method: 'PUT',
        headers: this.getHeaders(),
        body: JSON.stringify({ description })
      }
    );

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(`API error: ${response.status} - ${errorText}`);
    }

    const data = await response.json();
    return {
      status: data.status,
      toolId: data.tool_id,
      action: data.action,
      description: data.description
    };
  }

  async setUnifiedToolConfig(
    toolId: string,
    config: Record<string, unknown>,
    userId?: string
  ): Promise<{ status: string; toolId: string; action: string; config: Record<string, unknown> }> {
    const id = this.resolveUserId(userId);
    const response = await fetch(
      `${this.getBaseUrl()}/users/${id}/tools/unified/${toolId}/config`,
      {
        method: 'PUT',
        headers: this.getHeaders(),
        body: JSON.stringify({ config })
      }
    );

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(`API error: ${response.status} - ${errorText}`);
    }

    const data = await response.json();
    return {
      status: data.status,
      toolId: data.tool_id,
      action: data.action,
      config: data.config
    };
  }

  async getOptionalTools(): Promise<import('$lib/types').OptionalTool[]> {
    const response = await fetch(`${this.getBaseUrl()}/tools/optional`, {
      headers: this.getHeaders()
    });
    if (!response.ok) return [];
    const data = await response.json();
    return data.tools ?? [];
  }

  async getDefaultTools(userId?: string): Promise<import('$lib/types').DefaultToolsResponse> {
    const params = new URLSearchParams({ user_id: this.resolveUserId(userId) });
    const response = await fetch(`${this.getBaseUrl()}/tools/defaults?${params}`, {
      headers: this.getHeaders()
    });
    if (!response.ok) throw new Error(`Failed to load default tools: ${response.status}`);
    return response.json();
  }

  async setDefaultTools(toolNames: string[], userId?: string): Promise<void> {
    const params = new URLSearchParams({ user_id: this.resolveUserId(userId) });
    const response = await fetch(`${this.getBaseUrl()}/tools/defaults?${params}`, {
      method: 'PUT',
      headers: this.getHeaders(),
      body: JSON.stringify({ tool_names: toolNames })
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.detail || `Failed to save default tools: ${response.status}`);
    }
  }

  async resetDefaultTools(userId?: string): Promise<void> {
    const params = new URLSearchParams({ user_id: this.resolveUserId(userId) });
    const response = await fetch(`${this.getBaseUrl()}/tools/defaults?${params}`, {
      method: 'DELETE',
      headers: this.getHeaders()
    });
    if (!response.ok) throw new Error(`Failed to reset default tools: ${response.status}`);
  }
}
