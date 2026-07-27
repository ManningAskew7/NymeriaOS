import type {
  CLIProxyApplyRouteRequest,
  CLIProxyApplyRouteResponse,
  CLIProxyAuthFile,
  CLIProxyOAuthStart,
  CLIProxyProviderInfo,
  CLIProxyStatus
} from '$lib/types';
import { CommandsApi } from './commands';

/**
 * Admin /cliproxy routes: the backend drives the CLIProxy sidecar's
 * management API (OAuth logins, auth files, key config knobs) and owns the
 * route-shape math via apply-route, so this client never computes base URLs
 * or key slots itself. Works identically in thin-client mode.
 */
export class CLIProxyApi extends CommandsApi {
  async getCLIProxyCatalog(): Promise<CLIProxyProviderInfo[]> {
    try {
      const response = await fetch(`${this.getBaseUrl()}/cliproxy/catalog`, {
        headers: this.getHeaders()
      });
      if (!response.ok) return [];
      return response.json();
    } catch {
      return [];
    }
  }

  async getCLIProxyStatus(refresh = false): Promise<CLIProxyStatus | null> {
    try {
      const query = refresh ? '?refresh=true' : '';
      const response = await fetch(`${this.getBaseUrl()}/cliproxy/status${query}`, {
        headers: this.getHeaders()
      });
      if (!response.ok) return null;
      return response.json();
    } catch {
      return null;
    }
  }

  async startCLIProxyOAuth(provider: string, projectId?: string): Promise<CLIProxyOAuthStart> {
    const response = await fetch(`${this.getBaseUrl()}/cliproxy/oauth/start`, {
      method: 'POST',
      headers: this.getHeaders(),
      body: JSON.stringify({ provider, project_id: projectId || null })
    });
    if (!response.ok) {
      throw new Error(await this._toastAndExtractError(response, 'Failed to start the login'));
    }
    return response.json();
  }

  async getCLIProxyOAuthStatus(
    state: string,
    provider?: string
  ): Promise<{ status: 'wait' | 'ok' | 'error'; detail: string }> {
    const params = new URLSearchParams({ state });
    if (provider) params.set('provider', provider);
    const response = await fetch(
      `${this.getBaseUrl()}/cliproxy/oauth/status?${params.toString()}`,
      { headers: this.getHeaders() }
    );
    if (!response.ok) {
      throw new Error(await this._toastAndExtractError(response, 'Failed to check the login'));
    }
    const payload = await response.json();
    // detail carries the account label on a confirmed ok, and the backend's
    // explanation (e.g. the expired-session trap) on error.
    return { status: payload.status, detail: payload.detail ?? '' };
  }

  async deliverCLIProxyOAuthCallback(provider: string, redirectUrl: string): Promise<void> {
    const response = await fetch(`${this.getBaseUrl()}/cliproxy/oauth/callback`, {
      method: 'POST',
      headers: this.getHeaders(),
      body: JSON.stringify({ provider, redirect_url: redirectUrl })
    });
    if (!response.ok) {
      throw new Error(await this._toastAndExtractError(response, 'The proxy rejected the callback'));
    }
  }

  async listCLIProxyAuthFiles(provider?: string): Promise<CLIProxyAuthFile[]> {
    const query = provider ? `?provider=${encodeURIComponent(provider)}` : '';
    const response = await fetch(`${this.getBaseUrl()}/cliproxy/auth-files${query}`, {
      headers: this.getHeaders()
    });
    if (!response.ok) {
      throw new Error(await this._toastAndExtractError(response, 'Failed to list logins'));
    }
    return response.json();
  }

  async patchCLIProxyAuthFile(
    name: string,
    update: { disabled?: boolean; priority?: number }
  ): Promise<void> {
    const response = await fetch(
      `${this.getBaseUrl()}/cliproxy/auth-files/${encodeURIComponent(name)}`,
      {
        method: 'PATCH',
        headers: this.getHeaders(),
        body: JSON.stringify(update)
      }
    );
    if (!response.ok) {
      throw new Error(await this._toastAndExtractError(response, 'Failed to update the login'));
    }
  }

  async deleteCLIProxyAuthFile(name: string): Promise<void> {
    const response = await fetch(
      `${this.getBaseUrl()}/cliproxy/auth-files/${encodeURIComponent(name)}`,
      {
        method: 'DELETE',
        headers: this.getHeaders()
      }
    );
    if (!response.ok) {
      throw new Error(await this._toastAndExtractError(response, 'Failed to remove the login'));
    }
  }

  async getCLIProxyConfig(): Promise<Record<string, unknown>> {
    const response = await fetch(`${this.getBaseUrl()}/cliproxy/config`, {
      headers: this.getHeaders()
    });
    if (!response.ok) {
      throw new Error(await this._toastAndExtractError(response, 'Failed to load proxy settings'));
    }
    const payload = await response.json();
    return payload.knobs ?? {};
  }

  async patchCLIProxyConfig(knobs: Record<string, unknown>): Promise<Record<string, unknown>> {
    const response = await fetch(`${this.getBaseUrl()}/cliproxy/config`, {
      method: 'PATCH',
      headers: this.getHeaders(),
      body: JSON.stringify({ knobs })
    });
    if (!response.ok) {
      throw new Error(await this._toastAndExtractError(response, 'Failed to save proxy settings'));
    }
    const payload = await response.json();
    return payload.knobs ?? {};
  }

  async applyCLIProxyRoute(
    request: CLIProxyApplyRouteRequest
  ): Promise<CLIProxyApplyRouteResponse> {
    const response = await fetch(`${this.getBaseUrl()}/cliproxy/apply-route`, {
      method: 'POST',
      headers: this.getHeaders(),
      body: JSON.stringify(request)
    });
    if (!response.ok) {
      throw new Error(await this._toastAndExtractError(response, 'Failed to apply the route'));
    }
    return response.json();
  }
}
