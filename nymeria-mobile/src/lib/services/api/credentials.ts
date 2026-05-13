import type {
  Credential,
  CredentialBinding,
  CredentialBindingRequest,
  CredentialCreateRequest,
  CredentialListResponse,
  CredentialSetupSessionRequest,
  CredentialUpdateRequest
} from '$lib/types';
import { AccountsApi } from './accounts';

export class CredentialsApi extends AccountsApi {
  private credentialFromResponse(item: Record<string, unknown>): Credential {
    return {
      id: item.id as string,
      ownerType: item.owner_type as Credential['ownerType'],
      ownerUserId: item.owner_user_id as string | null,
      name: item.name as string,
      provider: item.provider as string,
      kind: item.kind as string,
      accountLabel: item.account_label as string | null,
      status: item.status as Credential['status'],
      metadata: (item.metadata as Record<string, unknown>) || {},
      scopes: (item.scopes as string[]) || [],
      allowedTargets: (item.allowed_targets as string[]) || [],
      expiresAt: item.expires_at as string | null,
      lastUsedAt: item.last_used_at as string | null,
      lastTestedAt: item.last_tested_at as string | null,
      disabledAt: item.disabled_at as string | null,
      createdByUserId: item.created_by_user_id as string | null,
      createdAt: item.created_at as string,
      updatedAt: item.updated_at as string,
      secretFields: (item.secret_fields as string[]) || [],
      hasSecret: item.has_secret === true,
    };
  }

  private bindingFromResponse(item: Record<string, unknown>): CredentialBinding {
    return {
      id: item.id as string,
      credentialId: item.credential_id as string,
      targetType: item.target_type as string,
      targetId: item.target_id as string,
      bindingName: item.binding_name as string | null,
      createdByUserId: item.created_by_user_id as string | null,
      createdAt: item.created_at as string,
    };
  }

  async listCredentials(scope: 'visible' | 'mine' | 'system' | 'all' = 'visible', includeDisabled = false): Promise<CredentialListResponse> {
    const params = new URLSearchParams({ scope });
    if (includeDisabled) params.set('include_disabled', 'true');
    const response = await fetch(`${this.getBaseUrl()}/credentials?${params.toString()}`, {
      headers: this.getHeaders()
    });
    if (!response.ok) {
      throw new Error(await this._toastAndExtractError(response, 'Failed to load credentials'));
    }
    const data = await response.json();
    return {
      credentials: ((data.credentials as Array<Record<string, unknown>>) || []).map((item) => this.credentialFromResponse(item)),
      total: (data.total as number) || 0,
    };
  }

  async createCredential(request: CredentialCreateRequest): Promise<Credential> {
    const response = await fetch(`${this.getBaseUrl()}/credentials`, {
      method: 'POST',
      headers: this.getHeaders(),
      body: JSON.stringify(request)
    });
    if (!response.ok) {
      throw new Error(await this._toastAndExtractError(response, 'Failed to create credential'));
    }
    return this.credentialFromResponse(await response.json());
  }

  async createCredentialSetupSession(request: CredentialSetupSessionRequest): Promise<Credential> {
    const response = await fetch(`${this.getBaseUrl()}/credential-setup-sessions`, {
      method: 'POST',
      headers: this.getHeaders(),
      body: JSON.stringify(request)
    });
    if (!response.ok) {
      throw new Error(await this._toastAndExtractError(response, 'Failed to create setup session'));
    }
    return this.credentialFromResponse(await response.json());
  }

  async updateCredential(credentialId: string, request: CredentialUpdateRequest): Promise<Credential> {
    const response = await fetch(`${this.getBaseUrl()}/credentials/${encodeURIComponent(credentialId)}`, {
      method: 'PATCH',
      headers: this.getHeaders(),
      body: JSON.stringify(request)
    });
    if (!response.ok) {
      throw new Error(await this._toastAndExtractError(response, 'Failed to update credential'));
    }
    return this.credentialFromResponse(await response.json());
  }

  async deleteCredential(credentialId: string, hard = false): Promise<void> {
    const response = await fetch(`${this.getBaseUrl()}/credentials/${encodeURIComponent(credentialId)}?hard=${hard ? 'true' : 'false'}`, {
      method: 'DELETE',
      headers: this.getHeaders()
    });
    if (!response.ok) {
      throw new Error(await this._toastAndExtractError(response, 'Failed to delete credential'));
    }
  }

  async testCredential(credentialId: string): Promise<Credential> {
    const response = await fetch(`${this.getBaseUrl()}/credentials/${encodeURIComponent(credentialId)}/test`, {
      method: 'POST',
      headers: this.getHeaders()
    });
    if (!response.ok) {
      throw new Error(await this._toastAndExtractError(response, 'Failed to test credential'));
    }
    return this.credentialFromResponse(await response.json());
  }

  async listCredentialBindings(credentialId: string): Promise<CredentialBinding[]> {
    const response = await fetch(`${this.getBaseUrl()}/credentials/${encodeURIComponent(credentialId)}/bindings`, {
      headers: this.getHeaders()
    });
    if (!response.ok) {
      throw new Error(await this._toastAndExtractError(response, 'Failed to load credential bindings'));
    }
    const data = await response.json();
    return ((data as Array<Record<string, unknown>>) || []).map((item) => this.bindingFromResponse(item));
  }

  async bindCredential(credentialId: string, request: CredentialBindingRequest): Promise<CredentialBinding> {
    const response = await fetch(`${this.getBaseUrl()}/credentials/${encodeURIComponent(credentialId)}/bindings`, {
      method: 'POST',
      headers: this.getHeaders(),
      body: JSON.stringify(request)
    });
    if (!response.ok) {
      throw new Error(await this._toastAndExtractError(response, 'Failed to bind credential'));
    }
    return this.bindingFromResponse(await response.json());
  }

  async deleteCredentialBinding(bindingId: string): Promise<void> {
    const response = await fetch(`${this.getBaseUrl()}/credential-bindings/${encodeURIComponent(bindingId)}`, {
      method: 'DELETE',
      headers: this.getHeaders()
    });
    if (!response.ok) {
      throw new Error(await this._toastAndExtractError(response, 'Failed to remove credential binding'));
    }
  }
}
