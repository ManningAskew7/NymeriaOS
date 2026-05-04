import type { ThreadPlatform } from '$lib/types';

export type NativeDisplayPlatform = 'discord' | 'telegram' | 'slack' | 'twitch' | 'trigger';

export function platformFromThreadId(threadId: string): ThreadPlatform {
  if (threadId.startsWith('discord_')) return 'discord';
  if (threadId.startsWith('telegram_')) return 'telegram';
  if (threadId.startsWith('slack_')) return 'slack';
  if (threadId.startsWith('twitch_')) return 'twitch';
  if (threadId.startsWith('trigger-')) return 'trigger';
  if (threadId.startsWith('agent-') || threadId.startsWith('spawned-')) return 'callable';
  return 'desktop';
}

export function isNativeDisplayPlatform(platform?: ThreadPlatform): platform is NativeDisplayPlatform {
  return (
    platform === 'discord' ||
    platform === 'telegram' ||
    platform === 'slack' ||
    platform === 'twitch' ||
    platform === 'trigger'
  );
}

export function platformAfterCallableChange(
  threadId: string,
  currentPlatform: ThreadPlatform | undefined,
  callable: boolean
): ThreadPlatform {
  const detected = platformFromThreadId(threadId);
  if (isNativeDisplayPlatform(currentPlatform)) return currentPlatform;
  if (isNativeDisplayPlatform(detected)) return detected;
  if (callable) return 'callable';
  return currentPlatform === 'callable' ? 'desktop' : (currentPlatform ?? detected);
}
