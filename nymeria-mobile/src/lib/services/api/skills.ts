import { ThreadConfigApi } from './thread-config';

export class SkillsApi extends ThreadConfigApi {
  async listSkills(
    userId?: string,
    scope?: import('$lib/types').SkillScope,
  ): Promise<import('$lib/types').SkillMetadata[]> {
    const params = new URLSearchParams({ user_id: this.resolveUserId(userId) });
    if (scope) params.set('scope', scope);
    const response = await fetch(`${this.getBaseUrl()}/skills?${params.toString()}`, {
      headers: this.getHeaders(),
    });
    if (!response.ok) throw new Error(`API error: ${response.status}`);
    const data = await response.json();
    return (data.skills ?? []) as import('$lib/types').SkillMetadata[];
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
}
