import type { UserMemory } from '$lib/types';
import { ApiBase } from './base';

/**
 * Global (per-user) memory entries: the structured key/value records the agent
 * reads as its persistent global memory. Backed by /users/{id}/memories. The
 * user id defaults to the signed-in identity via resolveUserId().
 */
export class MemoryApi extends ApiBase {
  async getMemories(userId?: string): Promise<UserMemory[]> {
    const id = this.resolveUserId(userId);
    const response = await fetch(
      `${this.getBaseUrl()}/users/${encodeURIComponent(id)}/memories`,
      { headers: this.getHeaders() }
    );
    if (!response.ok) {
      throw new Error(await this._toastAndExtractError(response, 'Failed to load memories'));
    }
    const data = await response.json();
    const rows = Array.isArray(data?.memories) ? data.memories : [];
    return rows.map((m: Record<string, unknown>) => ({
      key: m.key as string,
      value: m.value as string,
      createdAt: (m.created_at as string) ?? null,
      accessedAt: (m.accessed_at as string) ?? null,
      accessCount: (m.access_count as number) ?? 0
    }));
  }

  /** Upsert a memory (same key overwrites). */
  async saveMemory(key: string, value: string, userId?: string): Promise<void> {
    const id = this.resolveUserId(userId);
    const response = await fetch(
      `${this.getBaseUrl()}/users/${encodeURIComponent(id)}/memories`,
      {
        method: 'POST',
        headers: this.getHeaders(),
        body: JSON.stringify({ key, value })
      }
    );
    if (!response.ok) {
      throw new Error(await this._toastAndExtractError(response, 'Failed to save memory'));
    }
  }

  async forgetMemory(key: string, userId?: string): Promise<void> {
    const id = this.resolveUserId(userId);
    const response = await fetch(
      `${this.getBaseUrl()}/users/${encodeURIComponent(id)}/memories/${encodeURIComponent(key)}`,
      {
        method: 'DELETE',
        headers: this.getHeaders()
      }
    );
    if (!response.ok) {
      throw new Error(await this._toastAndExtractError(response, 'Failed to delete memory'));
    }
  }
}
