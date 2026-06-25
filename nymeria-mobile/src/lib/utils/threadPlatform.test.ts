import { describe, it, expect } from 'vitest';
import {
  platformFromThreadId,
  isNativeDisplayPlatform,
  platformAfterCallableChange,
} from './threadPlatform';

describe('platformFromThreadId', () => {
  it('maps each native chat-platform prefix to its platform', () => {
    expect(platformFromThreadId('discord_123')).toBe('discord');
    expect(platformFromThreadId('telegram_123')).toBe('telegram');
    expect(platformFromThreadId('slack_123')).toBe('slack');
    expect(platformFromThreadId('matrix_123')).toBe('matrix');
    expect(platformFromThreadId('whatsapp_123')).toBe('whatsapp');
    expect(platformFromThreadId('messenger_123')).toBe('messenger');
    expect(platformFromThreadId('instagram_123')).toBe('instagram');
    expect(platformFromThreadId('webex_123')).toBe('webex');
    expect(platformFromThreadId('mattermost_123')).toBe('mattermost');
    expect(platformFromThreadId('zulip_123')).toBe('zulip');
    expect(platformFromThreadId('rocketchat_123')).toBe('rocketchat');
    expect(platformFromThreadId('teams_123')).toBe('teams');
    expect(platformFromThreadId('googlechat_123')).toBe('googlechat');
    expect(platformFromThreadId('line_123')).toBe('line');
    expect(platformFromThreadId('signal_123')).toBe('signal');
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
