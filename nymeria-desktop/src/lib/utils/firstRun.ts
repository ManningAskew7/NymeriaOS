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

export type FirstRunSurface = 'app' | 'probing' | 'setup';

/** Which surface the root route renders. */
export function firstRunSurface(input: {
  needsSetup: boolean;
  /** The setup surface armed its session flag (stays up mid-session). */
  setupActive: boolean;
  probe: OriginProbe;
}): FirstRunSurface {
  if (input.setupActive) return 'setup';
  if (!input.needsSetup) return 'app';
  // Hold a connecting state rather than flashing the hub and swapping it for
  // the sign-in card when the probe lands (same reasoning as the token
  // handoff splash).
  if (input.probe === 'pending') return 'probing';
  return 'setup';
}

export type SetupView = 'signin' | 'welcome';

/** The view the setup surface opens on. */
export function initialSetupView(input: { connected: boolean; probe: OriginProbe }): SetupView {
  if (input.connected) return 'welcome';
  return input.probe === 'served' ? 'signin' : 'welcome';
}
