import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Trigger } from '$lib/types';

const mocks = vi.hoisted(() => ({
  getTriggers: vi.fn(),
  registerIdentityReloadHook: vi.fn(),
  resumeTrigger: vi.fn(),
}));

vi.mock('$lib/services/api.svelte', () => ({
  api: {
    getTriggers: mocks.getTriggers,
    resumeTrigger: mocks.resumeTrigger,
  },
}));

vi.mock('./config.svelte', () => ({
  registerIdentityReloadHook: mocks.registerIdentityReloadHook,
}));

import { isTriggerRunning, triggersStore } from './triggers.svelte';

function trigger(overrides: Partial<Trigger> = {}): Trigger {
  return {
    id: 'trig-1',
    name: 'Inbox watcher',
    source_type: 'outlook_email',
    source_config: {},
    action: { type: 'agent_prompt', config: {} },
    conditions: [],
    enabled: true,
    cooldown_seconds: 0,
    last_fired: null,
    fire_count: 3,
    thread_id: 'thread-1',
    created_at: '2026-08-30T00:00:00Z',
    created_by: 'user',
    consecutive_errors: 0,
    last_error: null,
    health_status: 'healthy',
    action_failures: 0,
    auto_paused_at: null,
    ...overrides,
  };
}

const autoPaused = trigger({
  id: 'trig-paused',
  name: 'Failing webhook',
  action_failures: 5,
  auto_paused_at: '2026-08-30T09:00:00Z',
  consecutive_errors: 5,
  last_error: 'Action failed: thread not found',
  health_status: 'failing',
});

const switchedOff = trigger({ id: 'trig-off', name: 'Old RSS', enabled: false });

async function seed(triggers: Trigger[]): Promise<void> {
  mocks.getTriggers.mockResolvedValue(triggers);
  await triggersStore.loadTriggers();
}

describe('isTriggerRunning', () => {
  it('treats an auto-paused trigger as not running even though it is enabled', () => {
    expect(autoPaused.enabled).toBe(true);
    expect(isTriggerRunning(autoPaused)).toBe(false);
  });

  it('runs only when the trigger is enabled and carries no pause stamp', () => {
    expect(isTriggerRunning(trigger())).toBe(true);
    expect(isTriggerRunning(switchedOff)).toBe(false);
  });
});

describe('triggersStore auto-pause accounting', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('keeps an auto-paused trigger out of the active list and the live count', async () => {
    await seed([trigger(), autoPaused, switchedOff]);

    expect(triggersStore.enabledCount).toBe(1);
    expect(triggersStore.activeTriggers.map((t) => t.id)).toEqual(['trig-1']);
    expect(triggersStore.pausedTriggers.map((t) => t.id)).toEqual(['trig-paused', 'trig-off']);
  });

  it('replaces the row with the resumed trigger the backend returns', async () => {
    await seed([trigger(), autoPaused]);

    const repaired = trigger({
      id: 'trig-paused',
      name: 'Failing webhook',
      fire_count: 3,
    });
    mocks.resumeTrigger.mockResolvedValue(repaired);

    const returned = await triggersStore.resumeTrigger('trig-paused');

    expect(mocks.resumeTrigger).toHaveBeenCalledWith('trig-paused');
    expect(returned).toEqual(repaired);

    const row = triggersStore.triggers.find((t) => t.id === 'trig-paused');
    expect(row?.auto_paused_at).toBeNull();
    expect(row?.action_failures).toBe(0);
    expect(row?.health_status).toBe('healthy');
    expect(row?.last_error).toBeNull();

    // The repaired trigger is live again without a reload, and the other row
    // is untouched.
    expect(triggersStore.enabledCount).toBe(2);
    expect(triggersStore.triggers.map((t) => t.id)).toEqual(['trig-1', 'trig-paused']);
    expect(mocks.getTriggers).toHaveBeenCalledTimes(1);
  });

  it('leaves the row paused when the resume call fails', async () => {
    await seed([autoPaused]);
    mocks.resumeTrigger.mockRejectedValue(new Error('API error: 500 - boom'));

    await expect(triggersStore.resumeTrigger('trig-paused')).rejects.toThrow('boom');

    const row = triggersStore.triggers.find((t) => t.id === 'trig-paused');
    expect(row?.auto_paused_at).toBe('2026-08-30T09:00:00Z');
    expect(triggersStore.enabledCount).toBe(0);
  });
});
