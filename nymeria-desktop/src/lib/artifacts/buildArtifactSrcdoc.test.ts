import { describe, expect, it } from 'vitest';

import {
  buildArtifactSrcdoc,
  escapeInlineScript,
  escapeInlineStyle,
} from './buildArtifactSrcdoc';

/**
 * Sandbox-critical invariants of the ui_prompt srcdoc document. The iframe's
 * containment story is sandbox="allow-scripts" (set by UiPromptModal) plus
 * the meta CSP and full asset inlining pinned here; if any of these
 * assertions start failing, treat it as a security regression, not a
 * formatting nit.
 */

const BASE = { html: '<form><input name="q"></form>', promptId: 'uip_test123' };

describe('buildArtifactSrcdoc', () => {
  it('carries the locked meta CSP (connect-src none)', () => {
    const doc = buildArtifactSrcdoc(BASE);
    expect(doc).toContain(
      '<meta http-equiv="Content-Security-Policy" content="connect-src \'none\'">'
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
