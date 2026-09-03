import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import {
  CHAT_BUBBLES_ATTR,
  CHAT_BUBBLES_KEY,
  applyChatBubblePreference,
  chatBubblesEnabled,
  readChatBubblePreference,
} from './chatBubbles';

function fakeStorage(initial: Record<string, string> = {}) {
  const map = new Map(Object.entries(initial));
  return {
    getItem: (k: string) => map.get(k) ?? null,
    setItem: (k: string, v: string) => void map.set(k, v),
    removeItem: (k: string) => void map.delete(k),
    snapshot: () => Object.fromEntries(map),
  };
}

function fakeRoot() {
  const attrs = new Map<string, string>();
  return {
    setAttribute: (k: string, v: string) => void attrs.set(k, v),
    removeAttribute: (k: string) => void attrs.delete(k),
    attrs,
  };
}

describe('chatBubblesEnabled', () => {
  it('treats a fresh profile (no key) as bubbles on', () => {
    expect(chatBubblesEnabled(null)).toBe(true);
    expect(chatBubblesEnabled(undefined)).toBe(true);
  });

  it('treats the legacy opt-in value as bubbles on', () => {
    expect(chatBubblesEnabled('on')).toBe(true);
  });

  it('only an explicit off disables bubbles', () => {
    expect(chatBubblesEnabled('off')).toBe(false);
    expect(chatBubblesEnabled('')).toBe(true);
    expect(chatBubblesEnabled('false')).toBe(true);
  });
});

describe('readChatBubblePreference', () => {
  it('defaults to on without a storage (SSR)', () => {
    expect(readChatBubblePreference(undefined)).toBe(true);
  });

  it('reads back an explicit off', () => {
    expect(readChatBubblePreference(fakeStorage({ [CHAT_BUBBLES_KEY]: 'off' }))).toBe(false);
  });
});

describe('applyChatBubblePreference', () => {
  it('unticking flattens the page and persists off', () => {
    const root = fakeRoot();
    const storage = fakeStorage();

    applyChatBubblePreference(false, root, storage);

    expect(root.attrs.get(CHAT_BUBBLES_ATTR)).toBe('off');
    expect(storage.snapshot()).toEqual({ [CHAT_BUBBLES_KEY]: 'off' });
    // What a reload would see.
    expect(readChatBubblePreference(storage)).toBe(false);
  });

  it('re-ticking restores the default look and leaves no key behind', () => {
    const root = fakeRoot();
    const storage = fakeStorage({ [CHAT_BUBBLES_KEY]: 'off' });
    root.attrs.set(CHAT_BUBBLES_ATTR, 'off');

    applyChatBubblePreference(true, root, storage);

    expect(root.attrs.has(CHAT_BUBBLES_ATTR)).toBe(false);
    expect(storage.snapshot()).toEqual({});
    expect(readChatBubblePreference(storage)).toBe(true);
  });

  it('never writes the attribute with any value other than off', () => {
    const root = fakeRoot();
    const storage = fakeStorage({ [CHAT_BUBBLES_KEY]: 'on' });

    applyChatBubblePreference(true, root, storage);

    expect(root.attrs.size).toBe(0);
    expect(storage.snapshot()).toEqual({});
  });

  it('tolerates a missing root or storage', () => {
    expect(() => applyChatBubblePreference(false, undefined, undefined)).not.toThrow();
    const storage = fakeStorage();
    applyChatBubblePreference(false, undefined, storage);
    expect(storage.snapshot()).toEqual({ [CHAT_BUBBLES_KEY]: 'off' });
  });
});

describe('app.html pre-boot script', () => {
  // The pre-boot block cannot import this util (plain script, runs before the
  // bundle), so pin its copy of the rule here: it must apply only 'off', and
  // never re-grow the old opt-in 'on' branch.
  const html = readFileSync(new URL('../../app.html', import.meta.url), 'utf8');
  const block = html
    .split('\n')
    .filter((line) => line.includes(CHAT_BUBBLES_KEY) || line.includes(CHAT_BUBBLES_ATTR))
    .join('\n');

  it('reads the preference key and applies only the off state', () => {
    expect(block).toContain(`localStorage.getItem('${CHAT_BUBBLES_KEY}')`);
    expect(block).toContain(`setAttribute('${CHAT_BUBBLES_ATTR}', 'off')`);
    expect(block).not.toContain(`'${CHAT_BUBBLES_ATTR}', 'on'`);
  });

  it('keeps LF line endings so the served CSP hash matches the browser', () => {
    expect(html).not.toContain('\r');
  });
});
