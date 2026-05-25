import { describe, expect, it } from 'vitest';
import { createInitGate } from './appInit';

describe('createInitGate', () => {
  it('does not initialize while unconfigured', () => {
    const gate = createInitGate();
    expect(gate.shouldInitialize(false)).toBe(false);
    expect(gate.shouldInitialize(false)).toBe(false);
    expect(gate.initialized).toBe(false);
  });

  it('initializes exactly once on the first ready transition (wizard path)', () => {
    const gate = createInitGate();
    // Fresh launch: unconfigured at mount, then the Setup Wizard supplies
    // credentials and isConfigured flips true.
    expect(gate.shouldInitialize(false)).toBe(false);
    expect(gate.shouldInitialize(true)).toBe(true);
    expect(gate.initialized).toBe(true);
  });

  it('initializes immediately when configured at mount (returning user)', () => {
    const gate = createInitGate();
    expect(gate.shouldInitialize(true)).toBe(true);
    expect(gate.initialized).toBe(true);
  });

  it('does not re-initialize on later config changes', () => {
    const gate = createInitGate();
    expect(gate.shouldInitialize(true)).toBe(true);
    // Subsequent effect re-runs (token edited, toggled off and on, etc.)
    // must not trigger a second initialization.
    expect(gate.shouldInitialize(true)).toBe(false);
    expect(gate.shouldInitialize(false)).toBe(false);
    expect(gate.shouldInitialize(true)).toBe(false);
  });
});
