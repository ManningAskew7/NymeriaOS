/**
 * Chat-bubble appearance preference (Settings > Appearance).
 *
 * Bubbles are the default look. The contract is deliberately one-sided so the
 * default never depends on any script having run: only an explicit 'off' is
 * stored, and only 'off' is applied as `html[data-chat-bubbles]`, which the
 * MessageBubble CSS uses to flatten AI responses. Anything else (no key, or
 * the legacy 'on' value from when bubbles were opt-in) means bubbles.
 *
 * The pre-boot script in `src/app.html` applies the same rule before first
 * paint; keep the two in step.
 */

export const CHAT_BUBBLES_KEY = 'nymeria_chat_bubbles';
export const CHAT_BUBBLES_ATTR = 'data-chat-bubbles';

type PreferenceStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;
type PreferenceRoot = Pick<Element, 'setAttribute' | 'removeAttribute'>;

/** Whether a stored preference value means "show bubbles". */
export function chatBubblesEnabled(stored: string | null | undefined): boolean {
  return stored !== 'off';
}

/** Read the preference; a missing store (SSR) yields the default, on. */
export function readChatBubblePreference(storage: PreferenceStorage | undefined): boolean {
  if (!storage) return true;
  return chatBubblesEnabled(storage.getItem(CHAT_BUBBLES_KEY));
}

/** Apply a choice to the document root and persist it. */
export function applyChatBubblePreference(
  on: boolean,
  root: PreferenceRoot | undefined,
  storage: PreferenceStorage | undefined
): void {
  if (root) {
    if (on) root.removeAttribute(CHAT_BUBBLES_ATTR);
    else root.setAttribute(CHAT_BUBBLES_ATTR, 'off');
  }
  if (storage) {
    if (on) storage.removeItem(CHAT_BUBBLES_KEY);
    else storage.setItem(CHAT_BUBBLES_KEY, 'off');
  }
}
