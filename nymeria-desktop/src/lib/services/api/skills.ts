import { ThreadConfigApi } from './thread-config';

export class SkillsApi extends ThreadConfigApi {
  // =========================================================================
  // Agent Skills API
  // =========================================================================

  async listSkills(
    userId?: string,
    scope?: import('$lib/types').SkillScope,
  ): Promise<import('$lib/types').SkillMetadata[]> {
    const params = new URLSearchParams({ user_id: this.resolveUserId(userId) });
    if (scope) params.set('scope', scope);
    const response = await fetch(`${this.getBaseUrl()}/skills?${params.toString()}`, {
      headers: this.getHeaders()
    });
    if (!response.ok) throw new Error(`API error: ${response.status}`);
    const data = await response.json();
    return (data.skills ?? []) as import('$lib/types').SkillMetadata[];
  }

  async getSkill(
    name: string,
    userId?: string,
  ): Promise<import('$lib/types').SkillDetail> {
    const params = new URLSearchParams({ user_id: this.resolveUserId(userId) });
    const response = await fetch(
      `${this.getBaseUrl()}/skills/${encodeURIComponent(name)}?${params.toString()}`,
      { headers: this.getHeaders() },
    );
    if (!response.ok) throw new Error(`API error: ${response.status}`);
    return (await response.json()) as import('$lib/types').SkillDetail;
  }

  async installSkill(
    request: import('$lib/types').SkillInstallRequest,
    userId?: string,
  ): Promise<import('$lib/types').SkillMetadata> {
    const params = new URLSearchParams({ user_id: this.resolveUserId(userId) });
    const response = await fetch(
      `${this.getBaseUrl()}/skills/install?${params.toString()}`,
      {
        method: 'POST',
        headers: this.getHeaders(),
        body: JSON.stringify(request),
      },
    );
    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(`Install failed: ${response.status} - ${errorText}`);
    }
    const data = await response.json();
    return data.skill as import('$lib/types').SkillMetadata;
  }

  async uninstallSkill(
    name: string,
    scope: 'user' | 'global' = 'user',
    userId?: string,
  ): Promise<void> {
    const params = new URLSearchParams({ scope, user_id: this.resolveUserId(userId) });
    const response = await fetch(
      `${this.getBaseUrl()}/skills/${encodeURIComponent(name)}?${params.toString()}`,
      { method: 'DELETE', headers: this.getHeaders() },
    );
    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(`Uninstall failed: ${response.status} - ${errorText}`);
    }
  }

  async searchSkillsMarketplace(
    source: import('$lib/types').SkillMarketplaceSource = 'anthropic',
    query?: string,
  ): Promise<import('$lib/types').MarketplaceSkillEntry[]> {
    const params = new URLSearchParams({ source });
    if (query) params.set('q', query);
    const response = await fetch(
      `${this.getBaseUrl()}/skills/marketplace/search?${params.toString()}`,
      { headers: this.getHeaders() },
    );
    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(`Marketplace search failed: ${response.status} - ${errorText}`);
    }
    const data = await response.json();
    return (data.results ?? []) as import('$lib/types').MarketplaceSkillEntry[];
  }

  async getThreadActiveSkills(
    threadId: string,
    userId?: string,
  ): Promise<import('$lib/types').ThreadActiveSkillsResponse> {
    const params = new URLSearchParams({ user_id: this.resolveUserId(userId) });
    const response = await fetch(
      `${this.getBaseUrl()}/threads/${encodeURIComponent(threadId)}/skills?${params.toString()}`,
      { headers: this.getHeaders() },
    );
    if (!response.ok) throw new Error(`API error: ${response.status}`);
    return (await response.json()) as import('$lib/types').ThreadActiveSkillsResponse;
  }

  async getThreadCallableTools(
    threadId: string,
    userId?: string,
  ): Promise<import('$lib/types').ThreadCallableToolsResponse> {
    const params = new URLSearchParams({ user_id: this.resolveUserId(userId) });
    const response = await fetch(
      `${this.getBaseUrl()}/threads/${encodeURIComponent(threadId)}/callable-tools?${params.toString()}`,
      { headers: this.getHeaders() },
    );
    if (!response.ok) throw new Error(`API error: ${response.status}`);
    return (await response.json()) as import('$lib/types').ThreadCallableToolsResponse;
  }

  async getGlobalSkills(userId?: string): Promise<string[]> {
    const params = new URLSearchParams({ user_id: this.resolveUserId(userId) });
    const response = await fetch(
      `${this.getBaseUrl()}/settings/global-skills?${params.toString()}`,
      { headers: this.getHeaders() },
    );
    if (!response.ok) throw new Error(`API error: ${response.status}`);
    const data = await response.json();
    return (data.enabled_global_skills ?? []) as string[];
  }

  async setGlobalSkills(
    skillNames: string[],
    userId?: string,
  ): Promise<string[]> {
    const params = new URLSearchParams({ user_id: this.resolveUserId(userId) });
    const response = await fetch(
      `${this.getBaseUrl()}/settings/global-skills?${params.toString()}`,
      {
        method: 'PUT',
        headers: this.getHeaders(),
        body: JSON.stringify({ skill_names: skillNames }),
      },
    );
    if (!response.ok) throw new Error(`API error: ${response.status}`);
    const data = await response.json();
    return (data.enabled_global_skills ?? []) as string[];
  }
}
