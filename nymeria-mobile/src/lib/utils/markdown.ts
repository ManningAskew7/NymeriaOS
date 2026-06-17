import { marked } from 'marked';
import remend from 'remend';
import DOMPurify from 'dompurify';
import hljs from 'highlight.js/lib/core';
import javascript from 'highlight.js/lib/languages/javascript';
import typescript from 'highlight.js/lib/languages/typescript';
import python from 'highlight.js/lib/languages/python';
import json from 'highlight.js/lib/languages/json';
import bash from 'highlight.js/lib/languages/bash';
import css from 'highlight.js/lib/languages/css';
import xml from 'highlight.js/lib/languages/xml';
import markdown from 'highlight.js/lib/languages/markdown';
import rust from 'highlight.js/lib/languages/rust';
import go from 'highlight.js/lib/languages/go';
import yaml from 'highlight.js/lib/languages/yaml';
import sql from 'highlight.js/lib/languages/sql';

// Register languages once at module load
hljs.registerLanguage('javascript', javascript);
hljs.registerLanguage('js', javascript);
hljs.registerLanguage('typescript', typescript);
hljs.registerLanguage('ts', typescript);
hljs.registerLanguage('python', python);
hljs.registerLanguage('py', python);
hljs.registerLanguage('json', json);
hljs.registerLanguage('bash', bash);
hljs.registerLanguage('sh', bash);
hljs.registerLanguage('shell', bash);
hljs.registerLanguage('css', css);
hljs.registerLanguage('html', xml);
hljs.registerLanguage('xml', xml);
hljs.registerLanguage('markdown', markdown);
hljs.registerLanguage('md', markdown);
hljs.registerLanguage('rust', rust);
hljs.registerLanguage('rs', rust);
hljs.registerLanguage('go', go);
hljs.registerLanguage('yaml', yaml);
hljs.registerLanguage('yml', yaml);
hljs.registerLanguage('sql', sql);

// Configure marked exactly once with renderer as plain object
marked.use({
  breaks: true,
  gfm: true,
  async: false,
  renderer: {
    // Type signature matches _Renderer.code, but marked v12 passes an object at runtime
    code(code: string, infostring: string | undefined, _escaped: boolean) {
      let text: string;
      let lang: string | undefined;

      if (typeof code === 'object') {
        // marked v12+ object format
        const token = code as unknown as { text: string; lang?: string };
        text = token.text;
        lang = token.lang;
      } else {
        text = code;
        lang = infostring;
      }

      const language = lang && hljs.getLanguage(lang) ? lang : null;
      if (language) {
        const highlighted = hljs.highlight(text, { language }).value;
        return `<pre><code class="hljs language-${language}">${highlighted}</code></pre>`;
      }
      // No recognized language — return HTML-escaped plain text
      const escaped = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
      return `<pre><code>${escaped}</code></pre>`;
    }
  },
  // Wrap rendered tables in a horizontally-scrollable container so a wide
  // table scrolls instead of overflowing the message bubble on narrow
  // viewports (AI-UI §10). Runs as the final parse step, before DOMPurify,
  // which keeps the plain wrapper div. `table { width: 100% }` is retained,
  // so a narrow table still fills the bubble and only wide ones scroll.
  // Code-fenced `<table>` text is entity-escaped by the code renderer above,
  // so it is never matched here.
  hooks: {
    postprocess(html: string) {
      return html
        .replace(/<table>/g, '<div class="md-table-wrap">\n<table>')
        .replace(/<\/table>/g, '</table>\n</div>');
    }
  }
});

/**
 * Sanitize rendered HTML before it is injected via {@html ...}.
 *
 * marked v12 does not sanitize, so raw HTML embedded in model, tool, or trigger
 * content (e.g. attacker-controlled email subjects / RSS titles) such as
 * `<img src=x onerror=...>`, `<iframe srcdoc>`, or `javascript:` links would
 * otherwise execute inside the Capacitor webview. DOMPurify strips scripts,
 * event handlers, and dangerous URIs while preserving the syntax-highlight
 * markup (span/class) and link target attributes this app emits.
 */
function sanitizeHtml(html: string): string {
  return DOMPurify.sanitize(html, { ADD_ATTR: ['target'] });
}

/**
 * Render markdown content to HTML using the shared marked instance.
 * Configured once at module load — safe to call from any component.
 */
export function renderMarkdown(content: string): string {
  try {
    return sanitizeHtml(marked.parse(content) as string);
  } catch (e) {
    console.error('Markdown rendering failed:', e);
    return sanitizeHtml(content);
  }
}

const CURSOR_HTML = '<span class="streaming-cursor"></span>';

/**
 * Render markdown for actively streaming content.
 * Uses remend to close incomplete syntax, then injects a blinking cursor
 * inline at the end of the last block-level element.
 */
export function renderMarkdownStreaming(content: string): string {
  let html: string;
  try {
    // Remend's default link repair uses the placeholder URL
    // `streamdown:incomplete-link`, which can leak into the UI while tokens are
    // still arriving. Text-only mode keeps partial links readable without a fake
    // href, while preserving the other useful streaming repairs.
    html = sanitizeHtml(marked.parse(remend(content, { linkMode: 'text-only' })) as string);
  } catch (e) {
    console.error('Streaming markdown rendering failed:', e);
    html = sanitizeHtml(content);
  }

  // Cursor is a trusted constant appended AFTER sanitization so DOMPurify
  // does not strip it. Insert before the last closing block tag (inline).
  const match = html.match(/<\/[^>]+>\s*$/);
  if (match && match.index !== undefined) {
    return html.slice(0, match.index) + CURSOR_HTML + html.slice(match.index);
  }
  return html + CURSOR_HTML;
}
