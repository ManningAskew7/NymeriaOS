import { describe, expect, it } from 'vitest';
import type { BrowserLoginInputEvent } from '$lib/services/api/browser-login';
import {
  cdpModifiers,
  keyEventFor,
  normalizedPoint,
  takeWireBatch,
} from './browserLoginInput';

const plain = { altKey: false, ctrlKey: false, metaKey: false, shiftKey: false };

describe('cdpModifiers', () => {
  it('maps each modifier onto the CDP bitmask (Alt=1, Ctrl=2, Meta=4, Shift=8)', () => {
    expect(cdpModifiers(plain)).toBe(0);
    expect(cdpModifiers({ ...plain, altKey: true })).toBe(1);
    expect(cdpModifiers({ ...plain, ctrlKey: true })).toBe(2);
    expect(cdpModifiers({ ...plain, metaKey: true })).toBe(4);
    expect(cdpModifiers({ ...plain, shiftKey: true })).toBe(8);
    expect(cdpModifiers({ altKey: true, ctrlKey: true, metaKey: true, shiftKey: true })).toBe(15);
  });
});

describe('normalizedPoint', () => {
  const rect = { left: 100, top: 50, width: 200, height: 100 };

  it('returns fractions of the frame box', () => {
    expect(normalizedPoint(rect, 100, 50)).toEqual({ x: 0, y: 0 });
    expect(normalizedPoint(rect, 300, 150)).toEqual({ x: 1, y: 1 });
    expect(normalizedPoint(rect, 200, 100)).toEqual({ x: 0.5, y: 0.5 });
  });

  it('clamps outside the box, because one out-of-range value 422s a whole batch', () => {
    expect(normalizedPoint(rect, 50, 20)).toEqual({ x: 0, y: 0 });
    expect(normalizedPoint(rect, 400, 300)).toEqual({ x: 1, y: 1 });
  });

  it('refuses a zero-sized box rather than dividing by it', () => {
    expect(normalizedPoint({ left: 0, top: 0, width: 0, height: 100 }, 5, 5)).toBeNull();
    expect(normalizedPoint({ left: 0, top: 0, width: 100, height: 0 }, 5, 5)).toBeNull();
  });
});

describe('keyEventFor', () => {
  it('forwards an ordinary key with no modifiers field when none are held', () => {
    expect(keyEventFor({ ...plain, key: 'a' })).toEqual({ type: 'key', key: 'a' });
    expect(keyEventFor({ ...plain, key: 'Enter' })).toEqual({ type: 'key', key: 'Enter' });
  });

  it('carries the modifier mask on chords', () => {
    expect(keyEventFor({ ...plain, key: 'a', ctrlKey: true })).toEqual({
      type: 'key',
      key: 'a',
      modifiers: 2,
    });
    expect(keyEventFor({ ...plain, key: 'Tab', shiftKey: true })).toEqual({
      type: 'key',
      key: 'Tab',
      modifiers: 8,
    });
  });

  it('produces nothing for a bare modifier press', () => {
    for (const key of ['Shift', 'Control', 'Alt', 'Meta']) {
      expect(keyEventFor({ ...plain, key })).toBeNull();
    }
  });

  it('produces nothing mid-composition (the composed text goes as a text event)', () => {
    expect(keyEventFor({ ...plain, key: 'a', isComposing: true })).toBeNull();
  });

  it('suppresses the paste chord so the pasted text is not double-entered', () => {
    // Ctrl/Cmd+V is forwarded as the pasted TEXT by the paste handler; the
    // chord itself would paste the REMOTE browser's (empty) clipboard over it.
    expect(keyEventFor({ ...plain, key: 'v', ctrlKey: true })).toBeNull();
    expect(keyEventFor({ ...plain, key: 'V', metaKey: true })).toBeNull();
    // A plain v still types the letter v.
    expect(keyEventFor({ ...plain, key: 'v' })).toEqual({ type: 'key', key: 'v' });
  });

  it('refuses an over-long key name (the wire caps at 32 chars)', () => {
    expect(keyEventFor({ ...plain, key: 'x'.repeat(33) })).toBeNull();
  });
});

describe('takeWireBatch', () => {
  const move = (x: number): BrowserLoginInputEvent => ({ type: 'mouse', action: 'move', x, y: 0.5 });
  const key = (k: string): BrowserLoginInputEvent => ({ type: 'key', key: k });

  it('keeps only the NEWEST move, preserving everything else in order', () => {
    const { batch, rest } = takeWireBatch([move(0.1), key('a'), move(0.2), key('b'), move(0.9)]);
    expect(batch).toEqual([key('a'), key('b'), move(0.9)]);
    expect(rest).toEqual([]);
  });

  it('caps a batch at the wire limit and RETURNS the overflow instead of dropping it', () => {
    const keys = Array.from({ length: 40 }, (_, i) => key(String(i)));
    const { batch, rest } = takeWireBatch(keys);
    expect(batch).toHaveLength(32);
    expect(rest).toHaveLength(8);
    // A typed character must never be lost to a burst: the overflow is the
    // exact tail, in order.
    expect(rest[0]).toEqual(key('32'));
    expect(rest[7]).toEqual(key('39'));
  });
});
