/**
 * First-run token handoff: consume a `#token=nym_...` URL fragment planted by
 * the setup wizard's browser auto-open (`nymeria init` opens
 * `http://localhost:<port>/#token=<raw>` once the backend answers /health),
 * so the local happy path signs in without the user ever seeing a token.
 *
 * Security model (docs/agent-systems/accounts.md): a URL fragment is
 * never sent to the server, its access logs, or a Referer header. The
 * residual exposure is browser history between page load and the
 * history.replaceState scrub, so the scrub runs BEFORE any network probe.
 * The token is persisted only through the app's normal config store (each
 * app's own persistence: the desktop store redacts it to the OS keychain /
 * secure storage) and only after the backend confirmed it is valid; a failed
 * probe adopts nothing and the caller falls through to the SetupWizard
 * without surfacing the bad token.
 *
 * Dependencies are injected (Vitest runs in a node environment with no
 * window/history/location); the +page.svelte wiring passes the real ones.
 */

const TOKEN_FRAGMENT_PATTERN = /^#token=(nym_[A-Za-z0-9_-]+)$/;

/** The `nym_...` token carried by a `#token=` fragment, or null. */
export function extractTokenFromHash(hash: string): string | null {
  const match = TOKEN_FRAGMENT_PATTERN.exec(hash ?? '');
  return match ? match[1] : null;
}

export interface TokenHandoffDeps {
  /** window.location.hash at boot. */
  hash: string;
  /** window.location.origin at boot; '' and 'null' opaque origins are rejected. */
  origin: string;
  /** Remove the fragment from the address bar (history.replaceState wrapper). */
  scrub: () => void;
  /** Validate origin + token against the backend (probeConnection). */
  probe: (url: string, key: string) => Promise<{ ok: boolean }>;
  /**
   * Exchange the fragment token for a long-lived personal token
   * (POST /me/tokens). The wizard plants the BOOTSTRAP token, which expires
   * after 24 hours and whose on-disk copy the probe's first successful auth
   * already deleted, so without this exchange the session dies a day after
   * install with nothing left to recover from. Return null (or throw) to
   * keep the fragment token as-is.
   */
  issueToken?: (url: string, key: string) => Promise<string | null>;
  /** Commit the validated credentials (configStore writes + completeSetup). */
  adopt: (url: string, key: string) => void;
}

/**
 * One-shot consume of a fragment token. Returns true when the token was
 * validated and adopted, false when there was no token, the origin is
 * unusable, or validation failed. The scrub always runs first, even when the
 * probe never happens, so the raw token leaves the address bar (and any
 * history entry written for it) as early as possible.
 */
export async function consumeTokenHandoff(deps: TokenHandoffDeps): Promise<boolean> {
  const token = extractTokenFromHash(deps.hash);
  if (!token) return false;
  deps.scrub();
  if (!deps.origin || deps.origin === 'null') return false;
  try {
    const result = await deps.probe(deps.origin, token);
    if (!result.ok) return false;
  } catch {
    return false;
  }
  let adoptedKey = token;
  if (deps.issueToken) {
    try {
      const upgraded = await deps.issueToken(deps.origin, token);
      if (upgraded) adoptedKey = upgraded;
    } catch {
      // Best effort: the validated fragment token still signs in; it just
      // carries the bootstrap TTL instead of a long-lived one.
    }
  }
  deps.adopt(deps.origin, adoptedKey);
  return true;
}

/**
 * Default `issueToken` implementation: self-issue a long-lived personal
 * token over the raw REST endpoint (module-level fetch, mirroring
 * probeConnection: no configStore reads or writes, safe pre-setup).
 */
export async function issuePersonalToken(url: string, key: string): Promise<string | null> {
  const base = url.trim().replace(/\/$/, '');
  const response = await fetch(`${base}/me/tokens`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${key}`,
    },
    body: JSON.stringify({ label: 'web-signin' }),
  });
  if (!response.ok) return null;
  const data = (await response.json()) as { raw_token?: string };
  return typeof data.raw_token === 'string' && data.raw_token.startsWith('nym_')
    ? data.raw_token
    : null;
}
