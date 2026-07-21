import { describe, expect, it } from 'vitest';
import { countChangedFields, fieldChanged } from './settingsDirty';

describe('fieldChanged', () => {
  it('treats null and undefined as the same unset value', () => {
    expect(fieldChanged(null, undefined)).toBe(false);
    expect(fieldChanged(undefined, null)).toBe(false);
    expect(fieldChanged(null, 0)).toBe(true);
  });

  it('compares strings trimmed', () => {
    expect(fieldChanged('claude-opus-4-8 ', 'claude-opus-4-8')).toBe(false);
    expect(fieldChanged('a', 'b')).toBe(true);
  });

  it('compares numbers and booleans strictly', () => {
    expect(fieldChanged(0.8, 0.8)).toBe(false);
    expect(fieldChanged(0.8, 0.81)).toBe(true);
    expect(fieldChanged(true, false)).toBe(true);
    expect(fieldChanged(NaN, NaN)).toBe(false);
  });

  it('does not equate empty string with unset', () => {
    expect(fieldChanged('', null)).toBe(true);
  });

  it('compares structured values by shape', () => {
    expect(fieldChanged(['a', 'b'], ['a', 'b'])).toBe(false);
    expect(fieldChanged(['a'], ['a', 'b'])).toBe(true);
  });
});

describe('countChangedFields', () => {
  const baseline = { model: 'claude-opus-4-8', temperature: 1, baseUrl: null };

  it('counts only changed fields', () => {
    expect(
      countChangedFields({ model: 'claude-opus-4-8', temperature: 0.7, baseUrl: '' }, baseline)
    ).toBe(2);
  });

  it('returns 0 for an identical snapshot', () => {
    expect(
      countChangedFields({ model: 'claude-opus-4-8', temperature: 1, baseUrl: undefined }, baseline)
    ).toBe(0);
  });

  it('treats a missing baseline as clean (tab not loaded yet)', () => {
    expect(countChangedFields({ model: 'x' }, undefined)).toBe(0);
  });
});
