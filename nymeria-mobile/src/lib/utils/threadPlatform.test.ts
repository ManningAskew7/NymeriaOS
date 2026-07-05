import { describe, it, expect } from 'vitest';
import {
  platformFromThreadId,
  isNativeDisplayPlatform,
  isPlatformNativeThreadId,
  platformAfterCallableChange,
} from './threadPlatform';

describe('platformFromThreadId', () => {
  it('maps each native chat-platform prefix to its platform', () => {
    expect(platformFromThreadId('discord_123')).toBe('discord');
    expect(platformFromThreadId('telegram_123')).toBe('telegram');
    expect(platformFromThreadId('slack_123')).toBe('slack');
    expect(platformFromThreadId('whatsapp_123')).toBe('whatsapp');
    expect(platformFromThreadId('teams_123')).toBe('teams');
    expect(platformFromThreadId('twitch_123')).toBe('twitch');
  });

  it('maps the trigger- prefix (hyphen, not underscore)', () => {
    expect(platformFromThreadId('trigger-abc')).toBe('trigger');
  });

  it('maps agent- and spawned- threads to callable', () => {
    expect(platformFromThreadId('agent-abc')).toBe('callable');
    expect(platformFromThreadId('spawned-abc')).toBe('callable');
  });

  it('falls back to desktop for unprefixed or unknown ids', () => {
    expect(platformFromThreadId('a1b2c3-uuid-like')).toBe('desktop');
    expect(platformFromThreadId('')).toBe('desktop');
    // A bare prefix without the separator must not match (guards startsWith use).
    expect(platformFromThreadId('discord')).toBe('desktop');
    expect(platformFromThreadId('trigger_abc')).toBe('desktop');
  });
});

describe('isNativeDisplayPlatform', () => {
  it('is true for native chat platforms and trigger', () => {
    expect(isNativeDisplayPlatform('discord')).toBe(true);
    expect(isNativeDisplayPlatform('telegram')).toBe(true);
    expect(isNativeDisplayPlatform('twitch')).toBe(true);
    expect(isNativeDisplayPlatform('trigger')).toBe(true);
  });

  it('is false for desktop, callable, and undefined', () => {
    expect(isNativeDisplayPlatform('desktop')).toBe(false);
    expect(isNativeDisplayPlatform('callable')).toBe(false);
    expect(isNativeDisplayPlatform(undefined)).toBe(false);
  });
});

describe('isPlatformNativeThreadId', () => {
  it('is true for every native chat-platform id and a trigger- id', () => {
    const nativeIds = [
      'discord_1',
      'telegram_1',
      'slack_1',
      'whatsapp_1',
      'teams_1',
      'twitch_1',
      'trigger-1',
    ];
    for (const id of nativeIds) {
      expect(isPlatformNativeThreadId(id)).toBe(true);
    }
  });

  it('is false for callable agent-/spawned- ids and plain desktop ids', () => {
    expect(isPlatformNativeThreadId('agent-1')).toBe(false);
    expect(isPlatformNativeThreadId('spawned-1')).toBe(false);
    expect(isPlatformNativeThreadId('a1b2c3-uuid-like')).toBe(false);
    expect(isPlatformNativeThreadId('')).toBe(false);
  });

  it('keys off the id prefix, not resolved metadata (the persistence-guard contract)', () => {
    // A native chat-app thread id is recognized and its current selection is not
    // persisted across reloads.
    expect(isPlatformNativeThreadId('telegram_42')).toBe(true);
    // A desktop-created UUID bound to a native platform has no native id prefix,
    // so it is NOT treated as native here and remains restorable. The OR-chain
    // this replaced behaved identically; the resolved-platform predicate would not.
    expect(isPlatformNativeThreadId('a1b2c3-uuid-like')).toBe(false);
  });
});

describe('platformAfterCallableChange', () => {
  it('keeps an existing native platform regardless of the toggle', () => {
    expect(platformAfterCallableChange('telegram_1', 'telegram', false)).toBe('telegram');
    expect(platformAfterCallableChange('telegram_1', 'telegram', true)).toBe('telegram');
  });

  it('prefers a native platform detected from the thread id', () => {
    expect(platformAfterCallableChange('discord_1', undefined, false)).toBe('discord');
    expect(platformAfterCallableChange('discord_1', 'desktop', true)).toBe('discord');
  });

  it('returns callable when toggled on for a non-native thread', () => {
    expect(platformAfterCallableChange('agent-1', 'desktop', true)).toBe('callable');
    expect(platformAfterCallableChange('plain-id', 'desktop', true)).toBe('callable');
  });

  it('reverts a previously-callable thread to desktop when toggled off', () => {
    expect(platformAfterCallableChange('plain-id', 'callable', false)).toBe('desktop');
  });

  it('preserves a non-native current platform, else falls back to detected', () => {
    expect(platformAfterCallableChange('plain-id', 'desktop', false)).toBe('desktop');
    expect(platformAfterCallableChange('plain-id', undefined, false)).toBe('desktop');
  });
});
