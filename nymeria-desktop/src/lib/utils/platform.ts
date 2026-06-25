import type { ThreadPlatform } from '$lib/types';

// Single source of truth: each chat-platform thread-id prefix paired with its
// platform name. Every prefix uses a trailing underscore. Adding a chat
// platform = one row here. The `satisfies` guard makes a name that is not a
// valid ThreadPlatform fail the build. The backend mirror is
// core/thread_classification.py (which folds trigger- into its own prefix list).
const CHAT_PLATFORMS = [
  ['discord_', 'discord'],
  ['telegram_', 'telegram'],
  ['slack_', 'slack'],
  ['matrix_', 'matrix'],
  ['whatsapp_', 'whatsapp'],
  ['messenger_', 'messenger'],
  ['instagram_', 'instagram'],
  ['webex_', 'webex'],
  ['mattermost_', 'mattermost'],
  ['zulip_', 'zulip'],
  ['rocketchat_', 'rocketchat'],
  ['teams_', 'teams'],
  ['googlechat_', 'googlechat'],
  ['line_', 'line'],
  ['signal_', 'signal'],
  ['twitch_', 'twitch'],
] as const satisfies ReadonlyArray<readonly [string, ThreadPlatform]>;

const CHAT_PLATFORM_PREFIXES = CHAT_PLATFORMS.map(([prefix]) => prefix);

// Native chat platforms plus the trigger pseudo-platform (excludes the callable
// agent-/spawned- threads). Used to decide whether a thread id is a native one.
const NATIVE_THREAD_PREFIXES = [...CHAT_PLATFORM_PREFIXES, 'trigger-'];

// Every non-desktop thread id: native plus the callable agent-/spawned- threads.
const NON_DESKTOP_THREAD_PREFIXES = [...NATIVE_THREAD_PREFIXES, 'agent-', 'spawned-'];

// Platform NAMES (not ids) that render as a native/display platform: the chat
// platforms plus trigger.
const NATIVE_DISPLAY_PLATFORMS: ReadonlySet<ThreadPlatform> = new Set<ThreadPlatform>([
  ...CHAT_PLATFORMS.map(([, platform]) => platform),
  'trigger',
]);

export function detectThreadPlatform(threadId: string): ThreadPlatform {
  for (const [prefix, platform] of CHAT_PLATFORMS) {
    if (threadId.startsWith(prefix)) return platform;
  }
  if (threadId.startsWith('trigger-')) return 'trigger';
  if (threadId.startsWith('agent-') || threadId.startsWith('spawned-')) return 'callable';
  return 'desktop';
}

// The native union below must stay in lockstep with CHAT_PLATFORMS + 'trigger'
// (the NATIVE_DISPLAY_PLATFORMS set): TypeScript cannot derive a type predicate
// from a runtime Set, so the literal union is maintained by hand.
export function isNativeDisplayPlatform(
  platform?: ThreadPlatform
): platform is 'discord' | 'telegram' | 'slack' | 'matrix' | 'whatsapp' | 'messenger' | 'instagram' | 'webex' | 'mattermost' | 'zulip' | 'rocketchat' | 'teams' | 'googlechat' | 'line' | 'signal' | 'twitch' | 'trigger' {
  return platform !== undefined && NATIVE_DISPLAY_PLATFORMS.has(platform);
}

// True when the thread id belongs to a native platform (chat platform or
// trigger), excluding callable agent/spawned threads.
export function isPlatformNativeThreadId(id: string): boolean {
  return NATIVE_THREAD_PREFIXES.some((prefix) => id.startsWith(prefix));
}

// True when the thread id is anything other than a plain desktop thread: a
// native platform thread, a trigger thread, or a callable agent/spawned thread.
export function isNonDesktopThreadId(id: string): boolean {
  return NON_DESKTOP_THREAD_PREFIXES.some((prefix) => id.startsWith(prefix));
}
