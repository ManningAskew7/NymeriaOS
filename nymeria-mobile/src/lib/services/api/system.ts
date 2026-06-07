import type {
  AvailableModel,
  LLMProviderSpec,
  LLMProviderTestRequest,
  LLMProviderTestResponse,
  LLMProviderTestSuiteRequest,
  LLMProviderTestSuiteResponse,
  ModelMetadata,
  RagUserSettings,
  RagUserSettingsUpdate,
  ServerSettings,
  ServerSettingsUpdate
} from '$lib/types';
import { ApiBase } from './base';

export class SystemApi extends ApiBase {
  async healthCheck(): Promise<boolean> {
    try {
      const response = await fetch(`${this.getBaseUrl()}/health`, {
        headers: this.getHeaders()
      });
      return response.ok;
    } catch {
      return false;
    }
  }
  async verifyAuth(): Promise<Response> {
    return fetch(`${this.getBaseUrl()}/me`, {
      headers: this.getHeaders()
    });
  }
  async restartServer(): Promise<boolean> {
    try {
      const response = await fetch(`${this.getBaseUrl()}/restart`, {
        method: 'POST',
        headers: this.getHeaders()
      });
      return response.ok;
    } catch {
      // Connection may drop before response — that's expected during restart
      return true;
    }
  }
  async getServerSettings(): Promise<ServerSettings> {
    const response = await fetch(`${this.getBaseUrl()}/settings`, {
      headers: this.getHeaders()
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }

    return response.json();
  }
  async updateServerSettings(
    updates: ServerSettingsUpdate
  ): Promise<{ message: string; updated: string[]; restart_required: boolean }> {
    const response = await fetch(`${this.getBaseUrl()}/settings`, {
      method: 'PATCH',
      headers: this.getHeaders(),
      body: JSON.stringify(updates)
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }

    return response.json();
  }

  async getRagSettings(userId: string): Promise<RagUserSettings> {
    const response = await fetch(
      `${this.getBaseUrl()}/users/${encodeURIComponent(userId)}/rag/settings`,
      { headers: this.getHeaders() }
    );
    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }
    return response.json();
  }

  async updateRagSettings(
    userId: string,
    updates: RagUserSettingsUpdate
  ): Promise<RagUserSettings & { status: string }> {
    const response = await fetch(
      `${this.getBaseUrl()}/users/${encodeURIComponent(userId)}/rag/settings`,
      {
        method: 'PUT',
        headers: this.getHeaders(),
        body: JSON.stringify(updates)
      }
    );
    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }
    return response.json();
  }

  async testLLMProviderConfig(
    request: LLMProviderTestRequest
  ): Promise<LLMProviderTestResponse> {
    const response = await fetch(`${this.getBaseUrl()}/settings/llm/test`, {
      method: 'POST',
      headers: this.getHeaders(),
      body: JSON.stringify(request)
    });

    if (!response.ok) {
      throw new Error(await this._toastAndExtractError(response, 'Failed to test provider'));
    }

    return response.json();
  }

  async runLLMProviderTestSuite(
    request: LLMProviderTestSuiteRequest
  ): Promise<LLMProviderTestSuiteResponse> {
    const response = await fetch(`${this.getBaseUrl()}/settings/llm/test-suite`, {
      method: 'POST',
      headers: this.getHeaders(),
      body: JSON.stringify(request)
    });

    if (!response.ok) {
      throw new Error(await this._toastAndExtractError(response, 'Failed to run provider test suite'));
    }

    return response.json();
  }

  async getLLMProviderCatalog(): Promise<LLMProviderSpec[]> {
    try {
      const response = await fetch(`${this.getBaseUrl()}/settings/llm/providers`, {
        headers: this.getHeaders()
      });

      if (!response.ok) return [];
      return response.json();
    } catch {
      return [];
    }
  }

  // Model Metadata
  async getOpenRouterModels(): Promise<ModelMetadata[]> {
    try {
      const response = await fetch(`${this.getBaseUrl()}/models`, {
        headers: this.getHeaders()
      });

      if (!response.ok) return [];
      return response.json();
    } catch {
      return [];
    }
  }
  async getAvailableModels(provider?: string, baseUrl?: string): Promise<AvailableModel[]> {
    try {
      const params = new URLSearchParams();
      if (provider) params.set('provider', provider);
      if (baseUrl) params.set('base_url', baseUrl);
      const query = params.toString() ? `?${params.toString()}` : '';
      const response = await fetch(`${this.getBaseUrl()}/models/available${query}`, {
        headers: this.getHeaders()
      });

      if (!response.ok) return [];
      return response.json();
    } catch {
      return [];
    }
  }

  // Dashboard API Methods
}
