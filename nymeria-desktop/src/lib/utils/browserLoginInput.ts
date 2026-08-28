/**
 * Pure mapping from DOM events to the browser-login input wire shape
 * (`POST /browser-login/{id}/input`), kept out of the component so the
 * rules with teeth are unit-testable:
 *
 * - Pointer coordinates are 0.0-1.0 FRACTIONS of the rendered frame and
 *   the backend 422s a whole batch on one out-of-range value, so clamping
 *   here is load-bearing, not cosmetic.
 * - Modifiers ride the CDP bitmask: Alt=1, Ctrl=2, Meta=4, Shift=8.
 * - A key event is atomic (no down/up split) and carries the KeyboardEvent
 *   `key` name; bare modifier presses and composition sequences produce
 *   nothing (the composed text goes through a `text` event instead).
 * - Ctrl/Cmd+V produces nothing: the remote browser's clipboard is not the
 *   user's, so the paste is forwarded as the pasted TEXT by the component's
 *   paste handler, and forwarding the chord too would paste garbage over it.
 */

import type { BrowserLoginInputEvent } from '$lib/services/api/browser-login';

export function cdpModifiers(event: {
  altKey: boolean;
  ctrlKey: boolean;
  metaKey: boolean;
  shiftKey: boolean;
}): number {
  return (
    (event.altKey ? 1 : 0) | (event.ctrlKey ? 2 : 0) | (event.metaKey ? 4 : 0) | (event.shiftKey ? 8 : 0)
  );
}

/** Fraction of the frame box, clamped so a drag that leaves the image can
 * never 422 the batch it rides in. */
export function normalizedPoint(
  rect: { left: number; top: number; width: number; height: number },
  clientX: number,
  clientY: number
): { x: number; y: number } | null {
  if (rect.width <= 0 || rect.height <= 0) return null;
  const clamp = (value: number) => Math.min(1, Math.max(0, value));
  return {
    x: clamp((clientX - rect.left) / rect.width),
    y: clamp((clientY - rect.top) / rect.height)
  };
}

const MODIFIER_KEY_NAMES = new Set(['Shift', 'Control', 'Alt', 'Meta', 'AltGraph', 'CapsLock', 'NumLock']);

/**
 * The wire event for one keydown, or null when the press must not be
 * forwarded: a bare modifier, an in-flight IME composition, an
 * over-long key name, or the paste chord (see module docstring).
 */
export function keyEventFor(event: {
  key: string;
  isComposing?: boolean;
  altKey: boolean;
  ctrlKey: boolean;
  metaKey: boolean;
  shiftKey: boolean;
}): BrowserLoginInputEvent | null {
  if (event.isComposing) return null;
  const key = event.key;
  if (!key || key.length > 32 || MODIFIER_KEY_NAMES.has(key)) return null;
  if ((event.ctrlKey || event.metaKey) && key.toLowerCase() === 'v') return null;
  const modifiers = cdpModifiers(event);
  return modifiers ? { type: 'key', key, modifiers } : { type: 'key', key };
}

/**
 * Take one wire batch off a pending queue: at most `limit` events (the
 * backend rejects bigger batches), with only the NEWEST `move` surviving
 * the collapse (a hover trail is video-shaped state, where the latest
 * position supersedes the ones it outran). Everything else keeps its order
 * and count, and what does not fit stays queued for the next batch rather
 * than being dropped: a typed character must never be lost to a burst.
 */
export function takeWireBatch(
  queue: BrowserLoginInputEvent[],
  limit = 32
): { batch: BrowserLoginInputEvent[]; rest: BrowserLoginInputEvent[] } {
  let lastMoveIndex = -1;
  for (let i = queue.length - 1; i >= 0; i -= 1) {
    const entry = queue[i];
    if (entry.type === 'mouse' && entry.action === 'move') {
      lastMoveIndex = i;
      break;
    }
  }
  const collapsed = queue.filter(
    (entry, index) =>
      !(entry.type === 'mouse' && entry.action === 'move' && index !== lastMoveIndex)
  );
  return { batch: collapsed.slice(0, limit), rest: collapsed.slice(limit) };
}
