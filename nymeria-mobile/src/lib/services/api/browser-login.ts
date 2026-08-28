import { UiPromptsApi } from './ui-prompts';

/**
 * Browser login handoff: the session where a HUMAN drives one tab of the
 * agent's Chrome through a live screencast viewer, so a password is typed
 * by a person into a screen the model never sees.
 *
 * The wire contract (backend `api/routers/browser_login.py`):
 * - sessions are OWNER-ONLY with no admin exemption; a non-owner gets 404,
 *   indistinguishable from "gone".
 * - the stream yields `login_attach` (status), then `login_frame` (base64
 *   JPEG), then `login_end` (status with `end_reason`). The server buffers
 *   only the newest 8 frames and silently skips what a slow reader outran:
 *   a seq gap is normal video behavior, never an error.
 * - pointer coordinates POST as 0.0-1.0 fractions of the frame; an
 *   out-of-range value rejects the whole batch (max 32 events), so clamp
 *   client-side.
 */

export interface BrowserLoginSessionStatus {
  session_id: string;
  thread_id: string;
  tab_id: number;
  url: string;
  state: 'active' | 'ended' | string;
  end_reason: string | null;
  last_seq: number;
  frames_received: number;
  frames_dropped: number;
  has_frame: boolean;
  seconds_remaining: number;
  expires_at: string;
}

export type BrowserLoginInputEvent =
  | { type: 'key'; key: string; modifiers?: number }
  | { type: 'text'; text: string }
  | {
      type: 'mouse';
      action: 'click' | 'move';
      x: number;
      y: number;
      button?: 'left' | 'right';
      click_count?: number;
      modifiers?: number;
    }
  | { type: 'wheel'; x: number; y: number; delta_x: number; delta_y: number };

export type BrowserLoginStreamEvent =
  | ({ type: 'login_attach' } & BrowserLoginSessionStatus)
  | {
      type: 'login_frame';
      seq: number;
      data: string;
      metadata?: Record<string, unknown> | null;
    }
  | ({ type: 'login_end' } & BrowserLoginSessionStatus)
  /** Synthetic, client-side only: the HTTP stream failed or dropped. A 404
   * usually means the session already ended and was reaped server-side. */
  | { type: 'login_stream_lost'; http_status: number | null };

export class BrowserLoginApi extends UiPromptsApi {
  /** Live and recently ended sessions for this user (reload recovery). */
  async listBrowserLoginSessions(): Promise<{ sessions: BrowserLoginSessionStatus[] }> {
    const response = await fetch(`${this.getBaseUrl()}/browser-login/sessions`, {
      headers: this.getHeaders()
    });
    if (!response.ok) {
      throw new Error(await this._extractError(response, 'Failed to check for a login in progress'));
    }
    return response.json();
  }

  /**
   * End a session: `completed` is the user finishing (they logged in),
   * `cancelled` is closing the viewer without finishing. The agent side is
   * woken either way, and the backend TTL owns cleanup if this call never
   * lands, so callers close their UI even when it fails.
   */
  async endBrowserLoginSession(
    sessionId: string,
    reason: 'completed' | 'cancelled'
  ): Promise<BrowserLoginSessionStatus> {
    const response = await fetch(
      `${this.getBaseUrl()}/browser-login/${encodeURIComponent(sessionId)}/end`,
      {
        method: 'POST',
        headers: this.getHeaders(),
        body: JSON.stringify({ reason })
      }
    );
    if (!response.ok) {
      // Non-toasting: the viewer renders failures inline, and a failed
      // dismissal closes anyway (the TTL owns cleanup server-side).
      throw new Error(await this._extractError(response, 'Failed to end the login session'));
    }
    return response.json();
  }

  /**
   * Forward operator input. Fire-and-forget in spirit: the ack carries no
   * result and the user's acknowledgement is the next frame.
   * `session_active: false` means the session ended and the caller should
   * stop sending.
   */
  async sendBrowserLoginInput(
    sessionId: string,
    events: BrowserLoginInputEvent[]
  ): Promise<{ dispatched: number; session_active: boolean }> {
    const response = await fetch(
      `${this.getBaseUrl()}/browser-login/${encodeURIComponent(sessionId)}/input`,
      {
        method: 'POST',
        headers: this.getHeaders(),
        body: JSON.stringify({ events })
      }
    );
    if (!response.ok) {
      throw new Error(await this._extractError(response, 'Failed to send input to the tab'));
    }
    return response.json();
  }

  /**
   * Tail a session's frame stream. Yields `login_attach`, then
   * `login_frame` repeatedly, then `login_end`; on transport failure it
   * yields one synthetic `login_stream_lost` and returns, so the caller
   * owns reconnect policy (pass the last rendered seq as `fromSeq`).
   *
   * The caller owns the AbortController; aborting it is the ONE way to
   * detach early, and the `finally` cancels the reader so the backend
   * generator is torn down instead of staying open until the session TTL.
   * Deliberately not wired to the chat stream's module-level abort slot:
   * "stop the turn" must never kill a login view, nor the reverse.
   */
  async *streamBrowserLoginFrames(
    sessionId: string,
    options: { fromSeq?: number; signal: AbortSignal }
  ): AsyncGenerator<BrowserLoginStreamEvent> {
    const fromSeq = options.fromSeq ?? 0;
    const url =
      `${this.getBaseUrl()}/browser-login/${encodeURIComponent(sessionId)}/stream` +
      (fromSeq > 0 ? `?from_seq=${fromSeq}` : '');
    let response: Response;
    try {
      response = await fetch(url, {
        headers: { ...this.getHeaders(), Accept: 'text/event-stream' },
        signal: options.signal
      });
    } catch {
      // Aborted by the caller, or the network died before the handshake.
      if (!options.signal.aborted) {
        yield { type: 'login_stream_lost', http_status: null };
      }
      return;
    }
    if (!response.ok || !response.body) {
      yield { type: 'login_stream_lost', http_status: response.status };
      return;
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    try {
      while (true) {
        let done: boolean;
        let value: Uint8Array | undefined;
        try {
          ({ done, value } = await reader.read());
        } catch {
          if (!options.signal.aborted) {
            yield { type: 'login_stream_lost', http_status: null };
          }
          return;
        }
        if (done) break;
        buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n');
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';
        for (const line of lines) {
          const trimmed = line.trimStart();
          // Keepalive comment frames and blank separators carry no payload.
          if (!trimmed || trimmed.startsWith(':')) continue;
          if (!trimmed.startsWith('data:')) continue;
          const jsonStr = trimmed.slice(5).trimStart();
          if (!jsonStr || jsonStr === '[DONE]') continue;
          let event: BrowserLoginStreamEvent | null = null;
          try {
            event = JSON.parse(jsonStr) as BrowserLoginStreamEvent;
          } catch {
            continue;
          }
          if (event && typeof event === 'object' && typeof event.type === 'string') {
            yield event;
            if (event.type === 'login_end') return;
          }
        }
      }
      // Stream closed without a login_end: the connection dropped mid-tail.
      if (!options.signal.aborted) {
        yield { type: 'login_stream_lost', http_status: null };
      }
    } finally {
      // Abandoned mid-tail: tear the connection down instead of leaving the
      // SSE body open on the backend until the session's TTL.
      try {
        await reader.cancel();
      } catch {
        // Already errored or aborted; nothing to release beyond the lock.
      }
      reader.releaseLock();
    }
  }
}
