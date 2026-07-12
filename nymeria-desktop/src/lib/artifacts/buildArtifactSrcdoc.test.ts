import { describe, expect, it } from 'vitest';

import {
  buildArtifactSrcdoc,
  escapeInlineScript,
  escapeInlineStyle,
} from './buildArtifactSrcdoc';
// Source text of the component that owns the iframe (and its sandbox
// attribute), for the source-level pins below.
import modalSource from '../components/artifacts/UiPromptModal.svelte?raw';

/**
 * Sandbox-critical invariants of the ui_prompt srcdoc document. The iframe's
 * containment story is sandbox="allow-scripts" (set by UiPromptModal, pinned
 * at source level below) plus the default-src 'none' meta CSP and full asset
 * inlining pinned here; if any of these assertions start failing, treat it
 * as a security regression, not a formatting nit.
 */

const BASE = { html: '<form><input name="q"></form>', promptId: 'uip_test123' };

describe('buildArtifactSrcdoc', () => {
  it('carries the locked meta CSP (default-src none, no network directives)', () => {
    const doc = buildArtifactSrcdoc(BASE);
    expect(doc).toContain(
      '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; ' +
        "script-src 'unsafe-inline' 'unsafe-eval'; style-src 'unsafe-inline' data:; " +
        "img-src data:; font-src data:; base-uri 'none'; form-action 'none'\">"
    );
  });

  it('never emits allow-same-origin anywhere in the document', () => {
    const doc = buildArtifactSrcdoc(BASE);
    expect(doc).not.toContain('allow-same-origin');
  });

  it('inlines all three vendored assets (no CDN, no external refs)', () => {
    const doc = buildArtifactSrcdoc(BASE);
    // Version markers from the pinned vendor builds.
    expect(doc).toContain('daisyUI 5.6.18');
    expect(doc).toContain('"4.3.2"'); // @tailwindcss/browser version constant
    expect(doc).toContain('window.Alpine='); // alpine cdn build global
    // No external loads: srcdoc must not reference http(s) sources.
    expect(doc).not.toMatch(/<(script|link)[^>]+(src|href)=["']https?:/i);
  });

  it('includes the agent html inside the body root', () => {
    const doc = buildArtifactSrcdoc(BASE);
    expect(doc).toContain('<main id="nymeria-root">');
    expect(doc).toContain(BASE.html);
  });

  it('JSON-injects the prompt id into the bootstrap', () => {
    const doc = buildArtifactSrcdoc(BASE);
    expect(doc).toContain('var PROMPT_ID = "uip_test123";');
    expect(doc).toContain("source: 'nymeria-ui-prompt'");
  });

  it('places the auto-starting Alpine build as the last body script', () => {
    const doc = buildArtifactSrcdoc(BASE);
    const alpineAt = doc.indexOf('window.Alpine=');
    const bootstrapAt = doc.indexOf('var PROMPT_ID =');
    expect(bootstrapAt).toBeGreaterThan(-1);
    expect(alpineAt).toBeGreaterThan(bootstrapAt);
  });

  it('selects the daisyUI theme', () => {
    expect(buildArtifactSrcdoc(BASE)).toContain('<html data-theme="dark">');
    expect(buildArtifactSrcdoc({ ...BASE, theme: 'light' })).toContain(
      '<html data-theme="light">'
    );
  });
});

describe('UiPromptModal sandbox attribute (source-level pin)', () => {
  // No component-render infra exists in this suite (node environment), so
  // the security-critical sandbox attribute is pinned against the component
  // source instead: the iframe must carry exactly sandbox="allow-scripts".

  it('sets exactly sandbox="allow-scripts" on the prompt iframe', () => {
    // Every sandbox="..." occurrence (the iframe attribute, plus the header
    // comment quoting it) must grant allow-scripts and NOTHING else: in
    // particular never allow-same-origin, with which the agent's script
    // would inherit the app origin. An extra capability token anywhere
    // changes the matched string and fails this pin.
    const sandboxAttrs = modalSource.match(/sandbox="[^"]*"/g) ?? [];
    expect(sandboxAttrs.length).toBeGreaterThan(0);
    for (const attr of sandboxAttrs) {
      expect(attr).toBe('sandbox="allow-scripts"');
    }
  });

  it('guards the residual self-navigation channel with a load counter', () => {
    // CSP cannot stop a frame navigating its own document; the modal cancels
    // the prompt on any iframe load after the initial srcdoc render.
    expect(modalSource).toContain('onload={handleFrameLoad}');
  });
});

describe('inline payload escaping', () => {
  it('defuses </script> terminators in script payloads', () => {
    const escaped = escapeInlineScript('var x = "</script><script>alert(1)";');
    expect(escaped).not.toContain('</script>');
    expect(escaped).toContain('<\\/script>');
  });

  it('defuses HTML comment openers in script payloads', () => {
    expect(escapeInlineScript('var y = "<!--";')).not.toContain('<!--');
  });

  it('defuses </style> terminators in style payloads', () => {
    const escaped = escapeInlineStyle('.a::before { content: "</style>"; }');
    expect(escaped).not.toContain('</style>');
  });
});
