import type { UiPromptAck, UiPromptResultRequest } from '$lib/types';
import { ReportingApi } from './reporting';

export class UiPromptsApi extends ReportingApi {
  /**
   * Resolve an in-flight ui_prompt: the awaiting agent tool wakes with this
   * result. `delivered: false` means the prompt already resolved server-side
   * (timeout, abort, or another client answered first).
   */
  async submitUiPromptResult(promptId: string, request: UiPromptResultRequest): Promise<UiPromptAck> {
    const response = await fetch(`${this.getBaseUrl()}/ui-prompts/${encodeURIComponent(promptId)}/result`, {
      method: 'POST',
      headers: this.getHeaders(),
      body: JSON.stringify(request)
    });
    if (!response.ok) {
      // Non-toasting: the modal renders failures inline (and a failed
      // dismissal closes anyway), so the account/admin toast helper would
      // double-surface every error and sign the user out on a stray 401.
      throw new Error(await this._extractError(response, 'Failed to send your answer'));
    }
    return response.json();
  }
}
