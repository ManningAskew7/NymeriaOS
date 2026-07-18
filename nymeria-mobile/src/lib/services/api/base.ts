import { clientId } from '$lib/stores/clientId.svelte';
import { configStore } from '$lib/stores/config.svelte';
import { errorsStore } from '$lib/stores/errors.svelte';
import type { AccountIdentity, MessageStep, ToolCall, WorkspaceArtifact } from '$lib/types';

export type ConnectionProbeResult =
  | { ok: true; identity: AccountIdentity; provider?: string; backendVersion?: string }
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
    return { ok: false, reason: 'error', message: 'Enter the server URL, like http://localhost:8000.' };
  }
  if (!key) {
    return { ok: false, reason: 'error', message: 'Enter your account token (nym_...).' };
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
      message: 'Cannot reach this server or the browser blocked the response. Check the URL, confirm the backend is running, and make sure CORS allows this app origin.',
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
  // Best-effort: /health carries the backend version (tag-identical to the
  // desktop app's by construction), which feeds the version-skew nudge.
  let backendVersion: string | undefined;
  try {
    const healthBody = (await health.json()) as { version?: string };
    if (typeof healthBody.version === 'string') backendVersion = healthBody.version;
  } catch {
    // ignore: a body-less /health still proves reachability
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

  return { ok: true, identity, provider, backendVersion };
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
  protected resolveUserId(userId?: string): string {
    const id = userId ?? configStore.identity?.id ?? null;
    if (!id) throw new Error('Not signed in');
    return id;
  }

  /**
   * Surface a structured toast for known account/admin failure modes and then
   * extract a human-readable error message to throw. See desktop counterpart.
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
      providerStatus: (step.providerStatus as MessageStep['providerStatus'])
        ?? (step.provider_status as MessageStep['providerStatus']),
      provider: step.provider as string | undefined,
      model: step.model as string | undefined,
      fromProvider: (step.fromProvider as string | undefined) ?? (step.from_provider as string | undefined),
      fromModel: (step.fromModel as string | undefined) ?? (step.from_model as string | undefined),
      toProvider: (step.toProvider as string | undefined) ?? (step.to_provider as string | undefined),
      toModel: (step.toModel as string | undefined) ?? (step.to_model as string | undefined),
      attempt: step.attempt as number | undefined,
      maxRetries: (step.maxRetries as number | undefined) ?? (step.max_retries as number | undefined),
      delaySeconds: (step.delaySeconds as number | undefined) ?? (step.delay_seconds as number | undefined),
      holdSeconds: (step.holdSeconds as number | undefined) ?? (step.hold_seconds as number | undefined),
      expiresAt: (step.expiresAt as string | null | undefined) ?? (step.expires_at as string | null | undefined),
      reason: step.reason as string | undefined,
      httpStatus: (step.httpStatus as number | null | undefined) ?? (step.http_status as number | null | undefined),
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
  protected parseUtcTimestamp(timestamp: string): Date {
    // If timestamp doesn't have timezone info, treat it as UTC
    if (!timestamp.endsWith('Z') && !timestamp.includes('+') && !timestamp.includes('-', 10)) {
      return new Date(timestamp + 'Z');
    }
    return new Date(timestamp);
  }

  /**
   * Parse the backend's JSON error `detail` string, if present. No side
   * effects. Returns null when the body is missing, non-JSON, or `detail` is
   * not a string (FastAPI validation errors arrive as a list, which we
   * deliberately do not surface verbatim). Mirrors desktop base.ts.
   */
  protected async _parseDetail(response: Response): Promise<string | null> {
    const detail = await response.json().catch(() => ({}));
    return typeof detail?.detail === 'string' ? detail.detail : null;
  }

  /**
   * Non-toasting error extractor for endpoints whose callers render an inline
   * error. Surfaces the backend `detail` when present, else a
   * "<fallback> (status)" string, so the downstream humanizer has a real
   * message to work with. Mirrors desktop base.ts (ported for the mirrored
   * ui-prompts API, which referenced it without the base ever gaining it).
   */
  protected async _extractError(response: Response, fallback: string): Promise<string> {
    const detailMessage = await this._parseDetail(response);
    return detailMessage || `${fallback} (${response.status})`;
  }

  // =========================================================================
  // Custom Tools API
  // =========================================================================
}
