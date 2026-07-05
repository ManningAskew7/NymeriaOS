// Humanize a caught error into user-facing copy.
//
// AI-UI guide §9: "Error messages should identify what went wrong and what the
// user can do about it" -- e.g. "That email is already registered. Try signing
// in instead", not "Error: invalid input". This helper replaces the repo-wide
//
//     e instanceof Error ? e.message : 'Failed to <verb>'
//
// anti-pattern, which named the failure, offered no recovery path, and piped
// raw backend strings (Pydantic validation noise, status-only fallbacks)
// straight to the user.
//
// Two entry points for the two error surfaces this app has:
//   - Inline component error state renders a single string -> humanizeErrorText.
//   - The toast layer (ErrorToast.svelte) has a title slot   -> humanizeError,
//     then read `.title` / `.body`.

export type ErrorAction =
  | 'save'
  | 'load'
  | 'create'
  | 'update'
  | 'delete'
  | 'disable'
  | 'enable'
  | 'test'
  | 'connect'
  | 'send'
  | 'import'
  | 'export'
  | 'reset'
  | 'start'
  | 'install'
  | 'copy'
  | 'run'
  | 'complete'
  | 'download'
  | 'register'
  | 'remove'
  | 'disconnect'
  | 'approve'
  | 'decline'
  | 'rewind';

export interface HumanErrorContext {
  /** What the user was trying to do. Drives the verb in the headline. */
  action: ErrorAction;
  /**
   * The thing being acted on, written as the natural noun phrase that should
   * follow the verb -- include the article: 'the trigger', 'your settings',
   * 'this user'. Caller-supplied so copy reads naturally without grammar
   * heuristics on our side.
   */
  resource: string;
}

export interface HumanError {
  /** Short headline, e.g. "Couldn't save the trigger". Toast title slot. */
  title: string;
  /**
   * Full self-contained sentence: headline + (server detail when presentable)
   * + recovery hint. This is what inline error UIs should render.
   */
  body: string;
}

// "connect" takes a preposition; every other action is a plain transitive verb.
const ACTION_VERB: Record<ErrorAction, string> = {
  save: 'save',
  load: 'load',
  create: 'create',
  update: 'update',
  delete: 'delete',
  disable: 'disable',
  enable: 'enable',
  test: 'test',
  connect: 'connect to',
  send: 'send',
  import: 'import',
  export: 'export',
  reset: 'reset',
  start: 'start',
  install: 'install',
  copy: 'copy',
  run: 'run',
  complete: 'complete',
  download: 'download',
  register: 'register',
  remove: 'remove',
  disconnect: 'disconnect',
  approve: 'approve',
  decline: 'decline',
  rewind: 'rewind',
};

// Recovery hint per action -- generic but actionable, which is the §9 bar
// ("what can the user do"), not a bespoke sentence per endpoint.
const DEFAULT_HINT = 'Try again in a moment.';
const ACTION_HINT: Partial<Record<ErrorAction, string>> = {
  save: 'Try again, or check your connection to the backend.',
  update: 'Try again, or check your connection to the backend.',
  create: 'Try again, or check your connection to the backend.',
  load: 'Try again, or check that the backend is reachable.',
  test: 'Check the details and try again.',
  connect: 'Check the address and credentials, then try again.',
  import: 'Check the file and try again.',
  install: 'Try again, or check that the source is reachable.',
  copy: 'You can copy it manually instead.',
  download: 'Try again, or check your connection to the backend.',
  register: 'Check the token and try again.',
};

function rawMessage(e: unknown): string {
  if (e instanceof Error) return e.message;
  if (typeof e === 'string') return e;
  return '';
}

/**
 * Is this backend/runtime string clean enough to show the user verbatim?
 * Rejects our own status-only fallbacks ("Failed to X (500)", "API error: 500"),
 * Pydantic / stack-trace fragments, markup, and anything implausibly long.
 */
function isPresentableDetail(raw: string): boolean {
  const t = raw.trim();
  if (!t) return false;
  if (t.length > 180) return false;
  if (/\(\d{3}\)\s*$/.test(t)) return false; // our own "(500)" fallback shape
  if (/^api error\b/i.test(t)) return false;
  if (/traceback|value_error|pydantic|errno/i.test(t)) return false;
  if (/[<>{}]|\n/.test(t)) return false;
  return true;
}

function ensureSentence(s: string): string {
  const t = s.trim();
  return /[.!?]$/.test(t) ? t : `${t}.`;
}

/**
 * A fetch() that never reached the server: backend down or restarting, DNS, a
 * dropped/reset connection, or an HTTP/3 (QUIC) transport failure. Browsers
 * surface these as a TypeError whose message varies by engine ("Failed to
 * fetch", "NetworkError when attempting to fetch resource", "Load failed",
 * "network error"). Detecting them lets the UI say the backend is unreachable
 * instead of leaking a raw "network error" string.
 */
export function isConnectivityError(e: unknown): boolean {
  const raw = (e instanceof Error ? e.message : typeof e === 'string' ? e : '').toLowerCase();
  if (!raw) return e instanceof TypeError;
  return (
    raw.includes('failed to fetch') ||
    raw.includes('networkerror') ||
    raw.includes('network error') ||
    raw.includes('load failed') ||
    raw.includes('err_quic') ||
    raw.includes('err_network') ||
    raw.includes('err_connection') ||
    raw.includes('err_internet') ||
    raw.includes('connection refused') ||
    raw.includes('connection reset')
  );
}

/**
 * Shared copy for a lost backend connection. Action-neutral so it reads
 * correctly on any surface; streaming surfaces (chat) add their own
 * "reconnecting" note at the call site.
 */
const CONNECTIVITY_ERROR: HumanError = {
  title: 'Connection lost',
  body:
    "Couldn't reach the backend. It may be restarting, or your connection " +
    'dropped. Try again in a moment.',
};

/**
 * Build a titled error from a caught value and the action/resource context.
 * The body is self-contained, so inline UIs can render it directly while the
 * toast layer can split out `.title`.
 */
export function humanizeError(e: unknown, ctx: HumanErrorContext): HumanError {
  if (isConnectivityError(e)) return CONNECTIVITY_ERROR;

  const title = `Couldn't ${ACTION_VERB[ctx.action]} ${ctx.resource}`;
  const hint = ACTION_HINT[ctx.action] ?? DEFAULT_HINT;

  const raw = rawMessage(e);
  const detail = isPresentableDetail(raw) ? `The server said: ${ensureSentence(raw)}` : '';

  const body = [ensureSentence(title), detail, hint].filter(Boolean).join(' ');
  return { title, body };
}

/**
 * Convenience for the dominant case: an inline error variable rendered as a
 * single string. Equivalent to `humanizeError(e, ctx).body`.
 */
export function humanizeErrorText(e: unknown, ctx: HumanErrorContext): string {
  return humanizeError(e, ctx).body;
}
