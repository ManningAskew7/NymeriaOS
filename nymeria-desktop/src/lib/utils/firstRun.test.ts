import { describe, expect, it } from 'vitest';
import { firstRunSurface, initialSetupView, runOriginProbe, type OriginProbe } from './firstRun';

const PROBES: OriginProbe[] = ['skipped', 'pending', 'served', 'unserved'];

describe('runOriginProbe', () => {
  function recorder() {
    const states: OriginProbe[] = [];
    return { states, set: (s: OriginProbe) => void states.push(s) };
  }

  it('is pending while the detector runs and served once it answers with an origin', async () => {
    const r = recorder();
    let resolve!: (v: string | null) => void;
    const probe = runOriginProbe(() => new Promise((res) => (resolve = res)), r.set);
    expect(r.states).toEqual(['pending']);
    resolve('https://nymeria.example');
    await expect(probe).resolves.toBe('https://nymeria.example');
    expect(r.states).toEqual(['pending', 'served']);
  });

  it('settles to unserved when the origin is not a backend', async () => {
    const r = recorder();
    await expect(runOriginProbe(async () => null, r.set)).resolves.toBeNull();
    expect(r.states).toEqual(['pending', 'unserved']);
  });

  it('settles to unserved when the detector throws, never stranding the splash', async () => {
    const r = recorder();
    await expect(
      runOriginProbe(async () => {
        throw new Error('aborted');
      }, r.set)
    ).resolves.toBeNull();
    expect(r.states).toEqual(['pending', 'unserved']);
  });
});

describe('firstRunSurface', () => {
  it('renders the app once a connection exists, whatever the probe says', () => {
    for (const probe of PROBES) {
      expect(firstRunSurface({ needsSetup: false, setupActive: false, probe })).toBe('app');
    }
  });

  it('keeps the setup surface up while its session flag is armed', () => {
    // Adopting a connection mid-session flips needsSetup false; the surface
    // must not be yanked away under the user.
    for (const probe of PROBES) {
      expect(firstRunSurface({ needsSetup: false, setupActive: true, probe })).toBe('setup');
    }
  });

  it('holds a connecting state while the origin probe is in flight', () => {
    expect(firstRunSurface({ needsSetup: true, setupActive: false, probe: 'pending' })).toBe(
      'probing'
    );
  });

  it('shows the setup surface once the probe has settled or was never applicable', () => {
    for (const probe of ['skipped', 'served', 'unserved'] as const) {
      expect(firstRunSurface({ needsSetup: true, setupActive: false, probe })).toBe('setup');
    }
  });
});

describe('initialSetupView', () => {
  it('opens on the token-only sign-in when the page is served by a backend', () => {
    expect(initialSetupView({ connected: false, probe: 'served' })).toBe('signin');
  });

  it('opens on the full welcome otherwise (desktop app, dev server, static host)', () => {
    for (const probe of ['skipped', 'unserved', 'pending'] as const) {
      expect(initialSetupView({ connected: false, probe })).toBe('welcome');
    }
  });

  it('never opens on sign-in for an already connected session (review flow)', () => {
    expect(initialSetupView({ connected: true, probe: 'served' })).toBe('welcome');
  });
});
