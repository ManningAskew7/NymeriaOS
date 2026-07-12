/**
 * Builds the srcdoc document for the ui_prompt sandboxed iframe.
 *
 * The agent-authored HTML fragment becomes the body of a fully
 * self-contained document: no CDN, no parent-origin fetch. All assets are
 * vendored at pinned versions under ./vendor/ and inlined here:
 *
 *   - daisyui 5.6.18 (`daisyui.css`, plain compiled CSS, no plugin needed)
 *   - @tailwindcss/browser 4.3.2 (`dist/index.global.js`, self-contained
 *     runtime compiler: zero fetch/XHR call sites, works on a null origin)
 *   - alpinejs 3.15.12 (`dist/cdn.min.js`; auto-starts via queueMicrotask at
 *     evaluation time, so it MUST stay the last script in <body>)
 *
 * Upgrade procedure: download the same three dist files at the new pinned
 * versions from npm, re-prepend the `// @ts-nocheck` first line on the two
 * JS files (checkJs is on), update the filenames + imports + the version
 * table in Nymeria/docs/private/plans/ui-prompt-tool.md.
 *
 * Security invariants (see the spec doc; do not weaken):
 *   - The iframe uses sandbox="allow-scripts" with NO allow-same-origin,
 *     so this document runs with a null origin. NEVER add
 *     allow-same-origin: it would hand agent-authored script the app
 *     origin (Tauri IPC, tokens, backend access).
 *   - The meta CSP `connect-src 'none'` blocks fetch/XHR/WebSocket/
 *     EventSource exfiltration; the sandbox itself blocks forms, popups,
 *     and navigation.
 *   - postMessage to the parent is the only channel out.
 */

// @ts-nocheck on the first line of each vendored .js keeps checkJs away;
// ?raw imports inline the file contents as strings at build time. The
// daisyUI stylesheet carries a .txt suffix because Vitest stubs imports of
// *.css (even with ?raw) to empty strings; as .css.txt it stays raw text
// everywhere.
import daisyUiCss from './vendor/daisyui-5.6.18.css.txt?raw';
import tailwindBrowserJs from './vendor/tailwindcss-browser-4.3.2.js?raw';
import alpineJs from './vendor/alpinejs-3.15.12.min.js?raw';

export interface BuildSrcdocOptions {
  /** Agent-authored HTML fragment (rendered as body markup, not escaped). */
  html: string;
  /** Prompt id echoed on every postMessage so the parent can correlate. */
  promptId: string;
  /** daisyUI theme for the document. Defaults to dark. */
  theme?: 'light' | 'dark';
}

/**
 * A literal `</script` (or `<!--`) inside an inlined script would terminate
 * the script element early and break (or worse, restructure) the document.
 * The current vendored builds contain neither, but a version bump could
 * introduce one, so escape both forms defensively. The replacements are
 * no-ops for JS semantics (`<\/` in source parses as `/`; string escapes
 * only).
 */
export function escapeInlineScript(source: string): string {
  return source.replace(/<\/script/gi, '<\\/script').replace(/<!--/g, '<\\!--');
}

/** Same early-termination guard for inlined stylesheet payloads. */
export function escapeInlineStyle(source: string): string {
  return source.replace(/<\/style/gi, '<\\/style');
}

/**
 * The in-frame bootstrap. Runs before Alpine so `window.nymeria` exists when
 * directives evaluate. Auto-wires plain <form> submission (FormData
 * serialization: repeated keys become arrays, File values become their
 * filename), exposes nymeria.submit/cancel for script-driven UIs, and
 * reports content height for the modal to size the iframe.
 */
function bootstrapScript(promptId: string): string {
  return `(function () {
  var PROMPT_ID = ${JSON.stringify(promptId)};
  function post(type, extra) {
    var msg = { source: 'nymeria-ui-prompt', prompt_id: PROMPT_ID, type: type };
    if (extra) { for (var k in extra) { msg[k] = extra[k]; } }
    window.parent.postMessage(msg, '*');
  }
  function coerce(value) {
    if (typeof value === 'string') return value;
    if (value && typeof value.name === 'string') return value.name;
    return String(value);
  }
  function collect(form) {
    var values = {};
    new FormData(form).forEach(function (value, key) {
      var v = coerce(value);
      if (Object.prototype.hasOwnProperty.call(values, key)) {
        if (!Array.isArray(values[key])) values[key] = [values[key]];
        values[key].push(v);
      } else {
        values[key] = v;
      }
    });
    return values;
  }
  document.addEventListener('submit', function (event) {
    event.preventDefault();
    var form = event.target;
    if (form && form.tagName === 'FORM') post('submit', { values: collect(form) });
  }, true);
  window.nymeria = {
    submit: function (values) { post('submit', { values: values || {} }); },
    cancel: function () { post('cancel'); }
  };
  var report = function () {
    var doc = document.documentElement;
    var body = document.body;
    post('resize', {
      height: Math.ceil(Math.max(doc.scrollHeight, body ? body.scrollHeight : 0))
    });
  };
  if (typeof ResizeObserver === 'function') {
    new ResizeObserver(report).observe(document.documentElement);
  }
  window.addEventListener('load', report);
})();`;
}

export function buildArtifactSrcdoc(options: BuildSrcdocOptions): string {
  const theme = options.theme === 'light' ? 'light' : 'dark';
  return `<!doctype html>
<html data-theme="${theme}">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="connect-src 'none'">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>${escapeInlineStyle(daisyUiCss)}</style>
<style>
  html { color-scheme: ${theme}; }
  body { margin: 0; padding: 16px; background: transparent; }
</style>
<script>${escapeInlineScript(tailwindBrowserJs)}</script>
</head>
<body>
<main id="nymeria-root">
${options.html}
</main>
<script>${escapeInlineScript(bootstrapScript(options.promptId))}</script>
<script>${escapeInlineScript(alpineJs)}</script>
</body>
</html>`;
}
