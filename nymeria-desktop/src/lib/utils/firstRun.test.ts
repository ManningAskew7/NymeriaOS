import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  firstRunSurface,
  initialSetupView,
  readServerConfigured,
  runOriginProbe,
  shouldProbeStoredServer,
  type OriginProbe
} from './firstRun';

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

  it('holds the connecting state while the desktop app asks its stored backend (#323)', () => {
    expect(
      firstRunSurface({ needsSetup: true, setupActive: false, probe: 'skipped', serverProbe: 'pending' })
    ).toBe('probing');
    for (const serverProbe of ['skipped', 'served', 'unserved'] as const) {
      expect(
        firstRunSurface({ needsSetup: true, setupActive: false, probe: 'skipped', serverProbe })
      ).toBe('setup');
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
    expect(initialSetupView({ connected: true, probe: 'skipped', serverProbe: 'served' })).toBe(
      'welcome'
    );
  });

  it('opens on sign-in when the stored backend reports itself already set up (#323)', () => {
    expect(initialSetupView({ connected: false, probe: 'skipped', serverProbe: 'served' })).toBe(
      'signin'
    );
    for (const serverProbe of ['skipped', 'pending', 'unserved'] as const) {
      expect(initialSetupView({ connected: false, probe: 'skipped', serverProbe })).toBe('welcome');
    }
  });
});

// #323: the desktop app's pre-auth "already set up" read against its stored
// backend URL, and the gate deciding whether that read applies.

function respond(status: number, body: unknown) {
  return vi.fn(async () => ({
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  })) as unknown as typeof fetch;
}

describe('readServerConfigured', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('reports a configured backend, hitting /health at the trimmed URL', async () => {
    const fetchImpl = respond(200, { status: 'ok', version: '1', configured: true });
    await expect(
      readServerConfigured(' https://nym.example/ ', { fetchImpl })
    ).resolves.toBe(true);
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    expect((vi.mocked(fetchImpl).mock.calls[0] as unknown[])[0]).toBe('https://nym.example/health');
  });

  it('reports a fresh backend as not configured', async () => {
    const fetchImpl = respond(200, { status: 'ok', version: '1', configured: false });
    await expect(readServerConfigured('https://nym.example', { fetchImpl })).resolves.toBe(false);
  });

  it('answers null for an empty URL without fetching', async () => {
    const fetchImpl = respond(200, { status: 'ok', configured: true });
    await expect(readServerConfigured('   ', { fetchImpl })).resolves.toBeNull();
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it('answers null when the backend is too old to carry the flag', async () => {
    const fetchImpl = respond(200, { status: 'ok', version: '0.1' });
    await expect(readServerConfigured('https://nym.example', { fetchImpl })).resolves.toBeNull();
  });

  it('answers null for a non-ok status', async () => {
    const fetchImpl = respond(503, { status: 'down', configured: true });
    await expect(readServerConfigured('https://nym.example', { fetchImpl })).resolves.toBeNull();
  });

  it('answers null for a body that is not a Nymeria health body', async () => {
    const fetchImpl = respond(200, { hello: 'world', configured: true });
    await expect(readServerConfigured('https://nym.example', { fetchImpl })).resolves.toBeNull();
  });

  it('answers null on a network failure', async () => {
    const fetchImpl = vi.fn(async () => {
      throw new TypeError('Failed to fetch');
    }) as unknown as typeof fetch;
    await expect(readServerConfigured('https://nym.example', { fetchImpl })).resolves.toBeNull();
  });

  it('gives up after the timeout, aborting the request, so the splash always settles', async () => {
    vi.useFakeTimers();
    let aborted = false;
    const fetchImpl = vi.fn(
      (_url: string, init?: RequestInit) =>
        new Promise((_resolve, reject) => {
          init?.signal?.addEventListener('abort', () => {
            aborted = true;
            reject(new DOMException('aborted', 'AbortError'));
          });
        })
    ) as unknown as typeof fetch;
    const pending = readServerConfigured('https://blackhole.example', { fetchImpl, timeoutMs: 200 });
    await vi.advanceTimersByTimeAsync(199);
    expect(aborted).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    await expect(pending).resolves.toBeNull();
    expect(aborted).toBe(true);
  });
});

describe('shouldProbeStoredServer', () => {
  const applies = { hasWindow: true, isTauri: true, url: 'http://localhost:8000', setupCompleted: false };

  it('applies to the desktop app with a stored URL and setup not completed', () => {
    expect(shouldProbeStoredServer(applies)).toBe(true);
  });

  it('never applies to a web build, without a window, without a URL, or once signed in', () => {
    expect(shouldProbeStoredServer({ ...applies, isTauri: false })).toBe(false);
    expect(shouldProbeStoredServer({ ...applies, hasWindow: false })).toBe(false);
    expect(shouldProbeStoredServer({ ...applies, url: '   ' })).toBe(false);
    expect(shouldProbeStoredServer({ ...applies, setupCompleted: true })).toBe(false);
  });
});
