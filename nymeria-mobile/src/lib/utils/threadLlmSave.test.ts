import { describe, expect, it } from 'vitest';
import { connectionOverrideForSave } from './threadLlmSave';

// A stored "" and a stored null are different routes (Anthropic direct versus
// inherit), and the backend treats a changed route value as a route edit that
// ends an active fallback hold. An unrelated Save must send back exactly what
// it was seeded from.

describe('connectionOverrideForSave', () => {
  it('an unedited "" on an unchanged route stays "", even where the field is hidden', () => {
    expect(connectionOverrideForSave({ field: '', saved: '', routeUnchanged: true, applies: false })).toBe('');
    expect(connectionOverrideForSave({ field: '', saved: '', routeUnchanged: true, applies: true })).toBe('');
  });

  it('an unedited null stays null and an unedited URL stays that URL', () => {
    expect(connectionOverrideForSave({ field: '', saved: null, routeUnchanged: true, applies: true })).toBeNull();
    expect(connectionOverrideForSave({ field: '', saved: undefined, routeUnchanged: true, applies: true })).toBeNull();
    expect(
      connectionOverrideForSave({ field: 'http://proxy:8317/v1', saved: 'http://proxy:8317/v1', routeUnchanged: true, applies: false }),
    ).toBe('http://proxy:8317/v1');
  });

  it('an edit where the override applies sends the form value, blank meaning inherit', () => {
    expect(
      connectionOverrideForSave({ field: 'http://new:8317/v1', saved: 'http://old:8317/v1', routeUnchanged: true, applies: true }),
    ).toBe('http://new:8317/v1');
    expect(connectionOverrideForSave({ field: '', saved: 'http://old:8317/v1', routeUnchanged: true, applies: true })).toBeNull();
    expect(connectionOverrideForSave({ field: 'http://typed', saved: null, routeUnchanged: false, applies: true })).toBe('http://typed');
  });

  it('a provider or route change to one the override does not apply to drops a stale value', () => {
    expect(
      connectionOverrideForSave({ field: 'http://old:8317/v1', saved: 'http://old:8317/v1', routeUnchanged: false, applies: false }),
    ).toBeNull();
    expect(connectionOverrideForSave({ field: '', saved: '', routeUnchanged: false, applies: false })).toBeNull();
  });

  it('an unchanged field on a changed route where the override applies keeps the form mapping', () => {
    expect(
      connectionOverrideForSave({ field: 'http://kept', saved: 'http://kept', routeUnchanged: false, applies: true }),
    ).toBe('http://kept');
    expect(connectionOverrideForSave({ field: '', saved: '', routeUnchanged: false, applies: true })).toBeNull();
  });
});
