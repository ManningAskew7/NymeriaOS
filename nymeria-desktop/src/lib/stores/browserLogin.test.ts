import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { BrowserLoginSessionStatus } from '$lib/services/api/browser-login';

const mocks = vi.hoisted(() => ({
  endBrowserLoginSession: vi.fn(),
}));

vi.mock('$lib/services/api.svelte', () => ({
  api: {
    endBrowserLoginSession: mocks.endBrowserLoginSession,
  },
}));

import { browserLoginStore } from './browserLogin.svelte';

function status(id: string): BrowserLoginSessionStatus {
  return {
    session_id: id,
    thread_id: `thread-${id}`,
    tab_id: 7,
    url: 'https://accounts.google.com/signin',
    state: 'active',
    end_reason: null,
    last_seq: 0,
    frames_received: 0,
    frames_dropped: 0,
    has_frame: false,
    seconds_remaining: 600,
    expires_at: '2026-01-01T00:00:00+00:00',
  };
}

describe('browserLoginStore', () => {
  beforeEach(() => {
    mocks.endBrowserLoginSession.mockReset();
    mocks.endBrowserLoginSession.mockResolvedValue(undefined);
    const active = browserLoginStore.active;
    if (active) browserLoginStore.clearById(active.session.session_id);
  });

  it('open() sets the active session with a local receipt anchor', () => {
    const before = Date.now();
    browserLoginStore.open(status('A'), 'agent');
    const active = browserLoginStore.active;
    expect(active?.session.session_id).toBe('A');
    expect(active?.origin).toBe('agent');
    expect(active?.ended).toBeNull();
    // The countdown anchors to LOCAL receipt time, never the server clock.
    expect(active!.receivedAtMs).toBeGreaterThanOrEqual(before);
    expect(active!.receivedAtMs).toBeLessThanOrEqual(Date.now());
  });

  it('a second session displaces the first and ends it so its tab is freed', () => {
    browserLoginStore.open(status('A'), 'agent');
    browserLoginStore.open(status('B'), 'command');
    expect(browserLoginStore.active?.session.session_id).toBe('B');
    expect(mocks.endBrowserLoginSession).toHaveBeenCalledTimes(1);
    expect(mocks.endBrowserLoginSession).toHaveBeenCalledWith('A', 'cancelled');
  });

  it('a displaced session that already ended is not re-cancelled', () => {
    browserLoginStore.open(status('A'), 'agent');
    browserLoginStore.endById('A', 'expired');
    browserLoginStore.open(status('B'), 'agent');
    expect(browserLoginStore.active?.session.session_id).toBe('B');
    expect(mocks.endBrowserLoginSession).not.toHaveBeenCalled();
  });

  it('re-announcing the session already showing keeps the original anchor', () => {
    browserLoginStore.open(status('A'), 'agent');
    const anchor = browserLoginStore.active!.receivedAtMs;
    browserLoginStore.open(status('A'), 'recovered');
    expect(browserLoginStore.active?.origin).toBe('agent');
    expect(browserLoginStore.active?.receivedAtMs).toBe(anchor);
    expect(mocks.endBrowserLoginSession).not.toHaveBeenCalled();
  });

  it('endById marks the matching session and the first reason sticks', () => {
    browserLoginStore.open(status('A'), 'agent');
    browserLoginStore.endById('A', 'expired');
    expect(browserLoginStore.active?.ended).toBe('expired');
    // The bus event and the viewer's own stream both feed this: whichever
    // lands second must not overwrite the reason the user was shown.
    browserLoginStore.endById('A', 'cancelled');
    expect(browserLoginStore.active?.ended).toBe('expired');
  });

  it('endById ignores an unknown session id', () => {
    browserLoginStore.open(status('A'), 'agent');
    // A failed start can end a session that never announced itself; that
    // ending must not touch the session on screen.
    browserLoginStore.endById('B', 'failed');
    expect(browserLoginStore.active?.session.session_id).toBe('A');
    expect(browserLoginStore.active?.ended).toBeNull();
  });

  it('clearById clears only the matching session', () => {
    browserLoginStore.open(status('A'), 'agent');
    browserLoginStore.clearById('B');
    expect(browserLoginStore.active?.session.session_id).toBe('A');
    browserLoginStore.clearById('A');
    expect(browserLoginStore.active).toBeNull();
  });
});
