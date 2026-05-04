import type { ThreadPlatform } from '$lib/types';

export function detectThreadPlatform(threadId: string): ThreadPlatform {
  if (threadId.startsWith('discord_')) return 'discord';
  if (threadId.startsWith('telegram_')) return 'telegram';
  if (threadId.startsWith('slack_')) return 'slack';
  if (threadId.startsWith('twitch_')) return 'twitch';
  if (threadId.startsWith('trigger-')) return 'trigger';
  if (threadId.startsWith('agent-') || threadId.startsWith('spawned-')) return 'callable';
  return 'desktop';
}

export function isNativeDisplayPlatform(
  platform?: ThreadPlatform
): platform is 'discord' | 'telegram' | 'slack' | 'twitch' | 'trigger' {
  return (
    platform === 'discord' ||
    platform === 'telegram' ||
    platform === 'slack' ||
    platform === 'twitch' ||
    platform === 'trigger'
  );
}
