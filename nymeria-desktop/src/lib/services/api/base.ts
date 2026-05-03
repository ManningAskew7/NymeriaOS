import { clientId } from '$lib/stores/clientId.svelte';
import { configStore } from '$lib/stores/config.svelte';
import { errorsStore } from '$lib/stores/errors.svelte';
import type { AccountIdentity, MessageStep, ToolCall, WorkspaceArtifact } from '$lib/types';

export type ConnectionProbeResult =
  | { ok: true; identity: AccountIdentity; provider?: string }
  | {
      ok: false;
      reason: 'unreachable' | 'unauthorized' | 'identity_failed' | 'error';
      status?: number;
      message: string;
    };

/**
 * Stateless connection probe used by SetupWizard and AddAccountSheet to
 * validate a backend URL + token combination *before* committing them to the
 * configStore. Module-level on purpose: NymeriaAPI methods all read from
 * configStore, and routing this through the class would invite a future
 * maintainer to "helpfully" use this.getHeaders() and reintroduce the bug
 * where the wizard's render gate flips mid-test.
 *
 * No configStore reads. No errorsStore writes (a wrong-token-while-typing
 * must not trip pushAuthInvalid and sign the user out).
 */
export async function probeConnection(
  url: string,
  key: string
): Promise<ConnectionProbeResult> {
  const base = url.trim().replace(/\/$/, '');
  if (!base) {
    return { ok: false, reason: 'error', message: 'Server URL is required.' };
  }
  if (!key) {
    return { ok: false, reason: 'error', message: 'Account token is required.' };
  }

  const authHeaders = {
    'Content-Type': 'application/json',
    Authorization: `Bearer ${key}`,
  };

  let health: Response | null = null;
  try {
    health = await fetch(`${base}/health`);
  } catch {
    return {
      ok: false,
      reason: 'unreachable',
      message: 'Cannot reach this server. Check the URL and that the backend is running.',
    };
  }
  if (!health.ok) {
    return {
      ok: false,
      reason: 'unreachable',
      status: health.status,
      message: `Server is reachable but /health returned ${health.status}.`,
    };
  }

  let me: Response;
  try {
    me = await fetch(`${base}/me`, { headers: authHeaders });
  } catch (e) {
    return {
      ok: false,
      reason: 'error',
      message: e instanceof Error ? e.message : 'Network error contacting /me.',
    };
  }
  if (me.status === 401 || me.status === 403) {
    return {
      ok: false,
      reason: 'unauthorized',
      status: me.status,
      message: 'Token rejected. Check it matches one issued by this backend.',
    };
  }
  if (!me.ok) {
    return {
      ok: false,
      reason: 'identity_failed',
      status: me.status,
      message: `Identity check failed (${me.status}).`,
    };
  }
  let identity: AccountIdentity;
  try {
    identity = (await me.json()) as AccountIdentity;
  } catch {
    return {
      ok: false,
      reason: 'identity_failed',
      status: me.status,
      message: 'Identity response was not valid JSON.',
    };
  }

  // Best-effort: surface the LLM provider for the wizard's backend-info line.
  // Failures here are silent so a backend that gates /settings doesn't block
  // an otherwise-successful probe.
  let provider: string | undefined;
  try {
    const settings = await fetch(`${base}/settings`, { headers: authHeaders });
    if (settings.ok) {
      const data = (await settings.json()) as { llm_provider?: string };
      provider = data.llm_provider;
    }
  } catch {
    // ignore
  }

  return { ok: true, identity, provider };
}

export class ApiBase {
  protected getHeaders(): HeadersInit {
    return {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${configStore.apiKey}`,
      'X-Nymeria-Client-Id': clientId
    };
  }

  protected getBaseUrl(): string {
    return configStore.apiUrl.replace(/\/$/, '');
  }

  /**
   * Resolve the user-scoped path segment for /users/{user_id}/... routes.
   * Falls back to the currently signed-in identity if no caller-provided id.
   * Throws if neither is available — better to surface a clear error once
   * than to send a literal "default" or "" and let the backend 404.
   */
  protected resolveUserId(userId?: string): string {
    const id = userId ?? configStore.identity?.id ?? null;
    if (!id) throw new Error('Not signed in');
    return id;
  }

  /**
   * Surface a structured toast for known account/admin failure modes and then
   * extract a human-readable error message to throw. Called from the new
   * /me/* and /admin/* wrappers — keeps the toast UI in sync with backend
   * gating without forcing every component to know the status code semantics.
   *
   * - 401 → auth_invalid (also signs out via errorsStore.pushAuthInvalid)
   * - 403 → forbidden_admin
   * - 409 with last-admin / owns-resources hints → last_admin / resource_owned
   * - any other non-2xx → generic toast
   */
  protected async _toastAndExtractError(response: Response, fallback: string): Promise<string> {
    const detail = await response.json().catch(() => ({}));
    const detailMessage = typeof detail?.detail === 'string' ? detail.detail : null;
    const message = detailMessage || `${fallback} (${response.status})`;

    if (response.status === 401) {
      errorsStore.pushAuthInvalid(detailMessage || 'Your token is no longer valid. Sign in again to continue.');
    } else if (response.status === 403) {
      errorsStore.push({
        kind: 'forbidden_admin',
        message: detailMessage || 'This action requires the admin role.',
      });
    } else if (response.status === 409) {
      const lower = (detailMessage || '').toLowerCase();
      if (lower.includes('last admin') || lower.includes('only admin') || lower.includes('only enabled admin')) {
        errorsStore.push({ kind: 'last_admin', message });
      } else if (lower.includes('thread') || lower.includes('todo') || lower.includes('owns')) {
        errorsStore.push({ kind: 'resource_owned', message });
      } else {
        errorsStore.push({ kind: 'generic', message });
      }
    } else if (!response.ok) {
      errorsStore.push({ kind: 'generic', message });
    }
    return message;
  }

  protected normalizeWorkspaceArtifact(raw: unknown): WorkspaceArtifact | null {
    if (!raw || typeof raw !== 'object') return null;

    const data = raw as Record<string, unknown>;
    const path = data.path as string | undefined;
    const name = data.name as string | undefined;
    if (!path || !name) return null;

    return {
      path,
      name,
      mimeType: (data.mimeType as string) || (data.mime_type as string) || 'application/octet-stream',
      sizeBytes: Number((data.sizeBytes as number | string | undefined) ?? data.size_bytes ?? 0) || 0
    };
  }

  protected normalizeMessageStep(raw: unknown): MessageStep | null {
    if (!raw || typeof raw !== 'object') return null;

    const step = raw as Record<string, unknown>;
    const type = step.type as MessageStep['type'] | undefined;
    if (!type) return null;

    const artifacts = Array.isArray(step.artifacts)
      ? step.artifacts
          .map((artifact) => this.normalizeWorkspaceArtifact(artifact))
          .filter((artifact): artifact is WorkspaceArtifact => artifact !== null)
      : undefined;

    return {
      type,
      content: step.content as string | undefined,
      id: step.id as string | undefined,
      name: step.name as string | undefined,
      arguments: step.arguments as Record<string, unknown> | undefined,
      result: step.result as string | undefined,
      artifacts,
      status: step.status as MessageStep['status'],
      startTime: step.startTime ? new Date(step.startTime as string) : undefined,
      endTime: step.endTime ? new Date(step.endTime as string) : undefined
    };
  }

  protected normalizeToolCall(raw: unknown): ToolCall | null {
    if (!raw || typeof raw !== 'object') return null;

    const toolCall = raw as Record<string, unknown>;
    const artifacts = Array.isArray(toolCall.artifacts)
      ? toolCall.artifacts
          .map((artifact) => this.normalizeWorkspaceArtifact(artifact))
          .filter((artifact): artifact is WorkspaceArtifact => artifact !== null)
      : undefined;

    return {
      id: (toolCall.id as string) || crypto.randomUUID(),
      name: (toolCall.name as string) || 'unknown',
      arguments: (toolCall.arguments as Record<string, unknown>) || {},
      result: toolCall.result as string | undefined,
      artifacts,
      status: (toolCall.status as ToolCall['status']) || 'success',
      startTime: toolCall.startTime ? new Date(toolCall.startTime as string) : undefined,
      endTime: toolCall.endTime ? new Date(toolCall.endTime as string) : undefined
    };
  }


  // Parse timestamp as UTC (backend sends timestamps without timezone suffix)
  protected parseUtcTimestamp(timestamp: string): Date {
    // If timestamp doesn't have timezone info, treat it as UTC
    if (!timestamp.endsWith('Z') && !timestamp.includes('+') && !timestamp.includes('-', 10)) {
      return new Date(timestamp + 'Z');
    }
    return new Date(timestamp);
  }
}
