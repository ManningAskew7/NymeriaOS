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
      throw new Error(await this._toastAndExtractError(response, 'Failed to send your answer'));
    }
    return response.json();
  }
}
