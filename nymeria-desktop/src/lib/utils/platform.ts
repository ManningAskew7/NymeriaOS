import type { ThreadPlatform } from '$lib/types';

export function detectThreadPlatform(threadId: string): ThreadPlatform {
  if (threadId.startsWith('discord_')) return 'discord';
  if (threadId.startsWith('telegram_')) return 'telegram';
  if (threadId.startsWith('slack_')) return 'slack';
  if (threadId.startsWith('matrix_')) return 'matrix';
  if (threadId.startsWith('whatsapp_')) return 'whatsapp';
  if (threadId.startsWith('messenger_')) return 'messenger';
  if (threadId.startsWith('instagram_')) return 'instagram';
  if (threadId.startsWith('webex_')) return 'webex';
  if (threadId.startsWith('mattermost_')) return 'mattermost';
  if (threadId.startsWith('zulip_')) return 'zulip';
  if (threadId.startsWith('rocketchat_')) return 'rocketchat';
  if (threadId.startsWith('teams_')) return 'teams';
  if (threadId.startsWith('googlechat_')) return 'googlechat';
  if (threadId.startsWith('line_')) return 'line';
  if (threadId.startsWith('signal_')) return 'signal';
  if (threadId.startsWith('twitch_')) return 'twitch';
  if (threadId.startsWith('trigger-')) return 'trigger';
  if (threadId.startsWith('agent-') || threadId.startsWith('spawned-')) return 'callable';
  return 'desktop';
}

export function isNativeDisplayPlatform(
  platform?: ThreadPlatform
): platform is 'discord' | 'telegram' | 'slack' | 'matrix' | 'whatsapp' | 'messenger' | 'instagram' | 'webex' | 'mattermost' | 'zulip' | 'rocketchat' | 'teams' | 'googlechat' | 'line' | 'signal' | 'twitch' | 'trigger' {
  return (
    platform === 'discord' ||
    platform === 'telegram' ||
    platform === 'slack' ||
    platform === 'matrix' ||
    platform === 'whatsapp' ||
    platform === 'messenger' ||
    platform === 'instagram' ||
    platform === 'webex' ||
    platform === 'mattermost' ||
    platform === 'zulip' ||
    platform === 'rocketchat' ||
    platform === 'teams' ||
    platform === 'googlechat' ||
    platform === 'line' ||
    platform === 'signal' ||
    platform === 'twitch' ||
    platform === 'trigger'
  );
}
