import { WorkflowsApi } from './workflows';

export class LlmFallbackApi extends WorkflowsApi {
  // =========================================================================
  // LLM fallback consent API (llm-fallback-consent Phase 2)
  //
  // Resolve surface for parked model switches: when the fallback switch mode
  // (or refusal swap mode) is `ask`, a consent-capable interactive turn parks
  // on a `fallback_prompt` bus event and waits for this call (or the
  // timeout, which auto-swaps). Identity comes from the bearer token
  // (owner-or-admin; the backend 404s another user's record), so there is no
  // user_id parameter. A 409 means the prompt is no longer pending: it timed
  // out and auto-swapped, was resolved elsewhere, or its turn ended.
  // =========================================================================

  async resolveLlmFallbackPrompt(
    recordId: string,
    approved: boolean,
    options: { holdSeconds?: number | null; holdPermanent?: boolean } = {}
  ): Promise<{ record_id: string; outcome: string }> {
    const body: Record<string, unknown> = { approved };
    if (options.holdSeconds != null) body.hold_seconds = options.holdSeconds;
    if (options.holdPermanent) body.hold_permanent = true;

    const response = await fetch(
      `${this.getBaseUrl()}/llm/fallback-approvals/${encodeURIComponent(recordId)}/resolve`,
      {
        method: 'POST',
        headers: this.getHeaders(),
        body: JSON.stringify(body)
      }
    );

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(`API error: ${response.status} - ${errorText}`);
    }

    return await response.json() as { record_id: string; outcome: string };
  }
}
