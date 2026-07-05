import { describe, it, expect } from 'vitest';
import type { ThreadPlatform } from '$lib/types';
import {
  detectThreadPlatform,
  isNativeDisplayPlatform,
  isNonDesktopThreadId,
  isPlatformNativeThreadId
} from './platform';

// The 6 chat platforms, each id prefix paired with its platform name (the
// prefix without the trailing underscore).
const CHAT_PLATFORMS: ReadonlyArray<[string, ThreadPlatform]> = [
  ['discord_', 'discord'],
  ['telegram_', 'telegram'],
  ['slack_', 'slack'],
  ['whatsapp_', 'whatsapp'],
  ['teams_', 'teams'],
  ['twitch_', 'twitch']
];

describe('detectThreadPlatform', () => {
  it.each(CHAT_PLATFORMS)('maps %s to the platform name %s', (prefix, name) => {
    expect(detectThreadPlatform(`${prefix}123:456`)).toBe(name);
  });

  it('maps trigger- ids to trigger', () => {
    expect(detectThreadPlatform('trigger-abc')).toBe('trigger');
  });

  it('maps agent-/spawned- ids to callable', () => {
    expect(detectThreadPlatform('agent-xyz')).toBe('callable');
    expect(detectThreadPlatform('spawned-xyz')).toBe('callable');
  });

  it('maps plain UUID / unknown ids to desktop', () => {
    expect(detectThreadPlatform('550e8400-e29b-41d4-a716-446655440000')).toBe('desktop');
    expect(detectThreadPlatform('cli-session')).toBe('desktop');
  });

  it('only matches a prefix at the START of the id (startsWith semantics)', () => {
    expect(detectThreadPlatform('my-discord_thread')).toBe('desktop');
  });
});

describe('isNativeDisplayPlatform', () => {
  it.each(CHAT_PLATFORMS)('is true for the chat platform name %s', (_prefix, name) => {
    expect(isNativeDisplayPlatform(name)).toBe(true);
  });

  it('is true for trigger', () => {
    expect(isNativeDisplayPlatform('trigger')).toBe(true);
  });

  it('is false for desktop, cli, callable, and undefined', () => {
    expect(isNativeDisplayPlatform('desktop')).toBe(false);
    expect(isNativeDisplayPlatform('cli')).toBe(false);
    expect(isNativeDisplayPlatform('callable')).toBe(false);
    expect(isNativeDisplayPlatform(undefined)).toBe(false);
  });
});

describe('isPlatformNativeThreadId', () => {
  it.each(CHAT_PLATFORMS)('is true for a %s id', (prefix) => {
    expect(isPlatformNativeThreadId(`${prefix}1`)).toBe(true);
  });

  it('is true for trigger- ids', () => {
    expect(isPlatformNativeThreadId('trigger-1')).toBe(true);
  });

  it('is false for callable agent-/spawned- ids and desktop ids', () => {
    expect(isPlatformNativeThreadId('agent-1')).toBe(false);
    expect(isPlatformNativeThreadId('spawned-1')).toBe(false);
    expect(isPlatformNativeThreadId('550e8400-e29b-41d4-a716-446655440000')).toBe(false);
  });
});

describe('isNonDesktopThreadId', () => {
  it.each(CHAT_PLATFORMS)('is true for a %s id', (prefix) => {
    expect(isNonDesktopThreadId(`${prefix}1`)).toBe(true);
  });

  it('is true for trigger-, agent-, and spawned- ids', () => {
    expect(isNonDesktopThreadId('trigger-1')).toBe(true);
    expect(isNonDesktopThreadId('agent-1')).toBe(true);
    expect(isNonDesktopThreadId('spawned-1')).toBe(true);
  });

  it('is false for a plain desktop UUID', () => {
    expect(isNonDesktopThreadId('550e8400-e29b-41d4-a716-446655440000')).toBe(false);
  });
});

describe('cross-helper invariants (drift guards)', () => {
  const SAMPLE_IDS = [
    ...CHAT_PLATFORMS.map(([prefix]) => `${prefix}1`),
    'trigger-1',
    'agent-1',
    'spawned-1',
    '550e8400-e29b-41d4-a716-446655440000',
    'cli-session'
  ];

  it('isNonDesktopThreadId == isPlatformNativeThreadId OR agent-/spawned-', () => {
    for (const id of SAMPLE_IDS) {
      const expected =
        isPlatformNativeThreadId(id) || id.startsWith('agent-') || id.startsWith('spawned-');
      expect(isNonDesktopThreadId(id)).toBe(expected);
    }
  });

  it('every chat-platform id detects to a native display platform', () => {
    for (const [prefix] of CHAT_PLATFORMS) {
      expect(isNativeDisplayPlatform(detectThreadPlatform(`${prefix}1`))).toBe(true);
    }
  });

  it('a native thread id resolves to a native display platform (and vice-versa for desktop)', () => {
    expect(isNativeDisplayPlatform(detectThreadPlatform('trigger-1'))).toBe(true);
    expect(isNativeDisplayPlatform(detectThreadPlatform('agent-1'))).toBe(false);
    expect(isNativeDisplayPlatform(detectThreadPlatform('plain-uuid'))).toBe(false);
  });
});
