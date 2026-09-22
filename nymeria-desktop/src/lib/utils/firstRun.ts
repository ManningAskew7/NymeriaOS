/**
 * First-open routing for a client that has no usable connection yet.
 *
 * The web client is the same build the backend serves at its own origin. A
 * visitor there is standing in front of a deployment that is, by definition,
 * already set up, so the install-shaped setup hub (providers, RAG, keys) is
 * noise: all they can supply is an account token. The Tauri desktop app and a
 * browser build not served by a backend (vite dev, static hosting) still get
 * the full hub, since there the backend may genuinely not exist yet.
 *
 * The signal is the origin probe in the config store (`/health` at
 * `window.location.origin`), which never runs under Tauri. Pure functions so
 * the routing is unit-testable without a DOM.
 */

/** Outcome of probing the page's own origin for a Nymeria backend. */
export type OriginProbe =
  /** Not applicable: Tauri, or no window. The full hub is the only path. */
  | 'skipped'
  /** In flight (bounded by the store's probe timeout). */
  | 'pending'
  /** The origin answered /health as a Nymeria backend. */
  | 'served'
  /** The origin is not a backend (dev server, static host). */
  | 'unserved';

/**
 * Drive one origin probe through its states. Always settles: a detector that
 * throws or rejects counts as "not served", so no surface can wait on
 * 'pending' forever (the `probing` splash has no other exit).
 */
export async function runOriginProbe(
  detect: () => Promise<string | null>,
  setState: (state: OriginProbe) => void
): Promise<string | null> {
  setState('pending');
  let detected: string | null = null;
  try {
    detected = (await detect()) || null;
  } catch {
    detected = null;
  }
  setState(detected ? 'served' : 'unserved');
  return detected;
}

/** Bound on the boot-time probes: they gate a full-screen splash. */
export const SERVER_PROBE_TIMEOUT_MS = 1500;

/**
 * The desktop app's counterpart to the origin probe (#323). Does the backend
 * at `url` report itself already set up? Reads the unauthenticated
 * `configured` flag on GET /health. `null` for an empty URL, no answer within
 * the timeout, a non-ok status, a body that is not a Nymeria health body, or
 * a backend too old to carry the flag, so a caller only ever acts on a
 * definite answer. Pure: the fetch is injected, nothing touches a store.
 */
export async function readServerConfigured(
  url: string,
  options: { fetchImpl?: typeof fetch; timeoutMs?: number } = {}
): Promise<boolean | null> {
  const base = url.trim().replace(/\/$/, '');
  if (!base) return null;
  const fetchImpl = options.fetchImpl ?? fetch;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), options.timeoutMs ?? SERVER_PROBE_TIMEOUT_MS);
  try {
    const response = await fetchImpl(`${base}/health`, {
      headers: { Accept: 'application/json' },
      cache: 'no-store',
      signal: controller.signal,
    });
    if (!response.ok) return null;
    const body = (await response.json()) as { status?: unknown; configured?: unknown };
    if (body.status !== 'ok' || typeof body.configured !== 'boolean') return null;
    return body.configured;
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Whether the stored-URL probe applies: only the Tauri app (a web build has
 * the origin probe), only with a URL to ask, and only while setup is not
 * completed (a signed-in session has nothing to route).
 */
export function shouldProbeStoredServer(input: {
  hasWindow: boolean;
  isTauri: boolean;
  url: string;
  setupCompleted: boolean;
}): boolean {
  return input.hasWindow && input.isTauri && !input.setupCompleted && input.url.trim().length > 0;
}

export type FirstRunSurface = 'app' | 'probing' | 'setup';

/** Which surface the root route renders. */
export function firstRunSurface(input: {
  needsSetup: boolean;
  /** The setup surface armed its session flag (stays up mid-session). */
  setupActive: boolean;
  probe: OriginProbe;
  /**
   * The desktop app's counterpart to the origin probe (#323): the STORED
   * backend URL asked whether it is already set up ('served' means yes).
   * Optional so web builds, which only run the origin probe, pass nothing.
   */
  serverProbe?: OriginProbe;
}): FirstRunSurface {
  if (input.setupActive) return 'setup';
  if (!input.needsSetup) return 'app';
  // Hold a connecting state rather than flashing the hub and swapping it for
  // the sign-in card when a probe lands (same reasoning as the token
  // handoff splash).
  if (input.probe === 'pending' || input.serverProbe === 'pending') return 'probing';
  return 'setup';
}

export type SetupView = 'signin' | 'welcome';

/** The view the setup surface opens on. */
export function initialSetupView(input: {
  connected: boolean;
  probe: OriginProbe;
  /** See firstRunSurface: the stored backend reports itself configured. */
  serverProbe?: OriginProbe;
}): SetupView {
  if (input.connected) return 'welcome';
  return input.probe === 'served' || input.serverProbe === 'served' ? 'signin' : 'welcome';
}
