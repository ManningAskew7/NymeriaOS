import { describe, expect, it } from 'vitest';
import { isTopOverlay, pushOverlay, removeOverlay } from './overlayStack';

describe('overlayStack', () => {
  it('reports the most recently pushed layer as top', () => {
    const outer = pushOverlay('outer');
    expect(isTopOverlay(outer)).toBe(true);

    const inner = pushOverlay('inner');
    expect(isTopOverlay(inner)).toBe(true);
    expect(isTopOverlay(outer)).toBe(false);

    removeOverlay(inner);
    removeOverlay(outer);
  });

  it('restores the layer below when the top layer is removed', () => {
    const outer = pushOverlay('outer');
    const inner = pushOverlay('inner');

    removeOverlay(inner);
    expect(isTopOverlay(outer)).toBe(true);

    removeOverlay(outer);
    expect(isTopOverlay(outer)).toBe(false);
  });

  it('handles removal of a non-top layer (e.g. outer closed programmatically)', () => {
    const outer = pushOverlay('outer');
    const inner = pushOverlay('inner');

    removeOverlay(outer);
    expect(isTopOverlay(inner)).toBe(true);

    removeOverlay(inner);
  });

  it('ignores duplicate removals', () => {
    const outer = pushOverlay('outer');
    const inner = pushOverlay('inner');

    removeOverlay(inner);
    removeOverlay(inner);
    expect(isTopOverlay(outer)).toBe(true);

    removeOverlay(outer);
  });
});
