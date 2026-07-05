import type { ThreadPlatform } from '$lib/types';

// Single source of truth: each chat-platform thread-id prefix paired with its
// platform name. Every prefix uses a trailing underscore and the platform name
// is the prefix without it (discord_ -> discord). Adding a chat platform = one
// row here. The `satisfies` guard makes a name that is not a valid ThreadPlatform
// fail the build. Mirrors the desktop utils/platform.ts source and the backend
// core/thread_classification.py prefix list.
const CHAT_PLATFORMS = [
  ['discord_', 'discord'],
  ['telegram_', 'telegram'],
  ['slack_', 'slack'],
  ['whatsapp_', 'whatsapp'],
  ['teams_', 'teams'],
  ['twitch_', 'twitch'],
] as const satisfies ReadonlyArray<readonly [string, ThreadPlatform]>;

// Derived from the table above (no second hand-maintained list): the chat
// platform names plus the trigger pseudo-platform. Used as the narrowed return
// type of the isNativeDisplayPlatform predicate.
export type NativeDisplayPlatform = (typeof CHAT_PLATFORMS)[number][1] | 'trigger';

const CHAT_PLATFORM_PREFIXES = CHAT_PLATFORMS.map(([prefix]) => prefix);

// Native chat platforms plus the trigger pseudo-platform (excludes the callable
// agent-/spawned- threads). Used to decide whether a thread id is a native one.
const NATIVE_THREAD_PREFIXES = [...CHAT_PLATFORM_PREFIXES, 'trigger-'];

// Platform NAMES (not ids) that render as a native/display platform: the chat
// platforms plus trigger.
const NATIVE_DISPLAY_PLATFORMS: ReadonlySet<ThreadPlatform> = new Set<ThreadPlatform>([
  ...CHAT_PLATFORMS.map(([, platform]) => platform),
  'trigger',
]);

export function platformFromThreadId(threadId: string): ThreadPlatform {
  for (const [prefix, platform] of CHAT_PLATFORMS) {
    if (threadId.startsWith(prefix)) return platform;
  }
  if (threadId.startsWith('trigger-')) return 'trigger';
  if (threadId.startsWith('agent-') || threadId.startsWith('spawned-')) return 'callable';
  return 'desktop';
}

export function isNativeDisplayPlatform(platform?: ThreadPlatform): platform is NativeDisplayPlatform {
  return platform !== undefined && NATIVE_DISPLAY_PLATFORMS.has(platform);
}

// True when the thread id belongs to a native platform (chat platform or
// trigger), excluding callable agent/spawned threads. Keys off the id prefix
// only (not resolved thread.platform metadata), matching the saveCurrentThreadId
// persistence guard.
export function isPlatformNativeThreadId(id: string): boolean {
  return NATIVE_THREAD_PREFIXES.some((prefix) => id.startsWith(prefix));
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
