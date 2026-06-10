import { describe, expect, it } from 'vitest';

import { humanizeError, humanizeErrorText } from './humanizeError';

describe('humanizeError', () => {
  it('builds a "Couldn\'t <verb> <resource>" headline', () => {
    const { title } = humanizeError(new Error('whatever'), { action: 'save', resource: 'the trigger' });
    expect(title).toBe("Couldn't save the trigger");
  });

  it('uses the prepositional verb for connect', () => {
    const { title } = humanizeError(new Error('x'), { action: 'connect', resource: 'the backend' });
    expect(title).toBe("Couldn't connect to the backend");
  });

  it('covers toggle-style verbs (disable)', () => {
    const body = humanizeErrorText(new Error('x (500)'), { action: 'disable', resource: 'the credential' });
    expect(body).toBe("Couldn't disable the credential. Try again in a moment.");
  });

  it('surfaces a presentable backend detail with provenance', () => {
    const body = humanizeErrorText(new Error('Source URL is unreachable'), {
      action: 'save',
      resource: 'the trigger',
    });
    expect(body).toBe(
      "Couldn't save the trigger. The server said: Source URL is unreachable. " +
        'Try again, or check your connection to the backend.',
    );
  });

  it('does not double the period when the detail already ends in one', () => {
    const body = humanizeErrorText(new Error('Name already taken.'), {
      action: 'create',
      resource: 'the user',
    });
    expect(body).toContain('The server said: Name already taken.');
    expect(body).not.toContain('taken..');
  });

  it('suppresses our own status-only fallback shape', () => {
    const body = humanizeErrorText(new Error('Failed to load history (500)'), {
      action: 'load',
      resource: 'the history',
    });
    expect(body).not.toContain('The server said');
    expect(body).toBe("Couldn't load the history. Try again, or check that the backend is reachable.");
  });

  it('suppresses bare "API error: <status>" strings', () => {
    const body = humanizeErrorText(new Error('API error: 503'), { action: 'load', resource: 'tokens' });
    expect(body).not.toContain('The server said');
    expect(body).not.toContain('API error');
  });

  it('suppresses raw Pydantic / validation noise', () => {
    const body = humanizeErrorText(
      new Error('value_error.email: value is not a valid email address'),
      { action: 'create', resource: 'the user' },
    );
    expect(body).not.toContain('The server said');
    expect(body).not.toContain('value_error');
  });

  it('suppresses markup and multi-line stack fragments', () => {
    expect(humanizeErrorText(new Error('<html>502 Bad Gateway</html>'), { action: 'save', resource: 'x' })).not.toContain(
      'The server said',
    );
    expect(humanizeErrorText(new Error('boom\n  at Object.<anonymous>'), { action: 'save', resource: 'x' })).not.toContain(
      'The server said',
    );
  });

  it('suppresses implausibly long strings', () => {
    const long = 'e'.repeat(200);
    expect(humanizeErrorText(new Error(long), { action: 'save', resource: 'x' })).not.toContain('The server said');
  });

  it('handles non-Error throws (strings) and falls back cleanly for objects', () => {
    expect(humanizeErrorText('Disk is full', { action: 'save', resource: 'the file' })).toContain(
      'The server said: Disk is full.',
    );
    const body = humanizeErrorText({ weird: true }, { action: 'save', resource: 'the file' });
    expect(body).toBe("Couldn't save the file. Try again, or check your connection to the backend.");
  });

  it('falls back to the default hint for actions without a specific one', () => {
    const body = humanizeErrorText(new Error('x (500)'), { action: 'delete', resource: 'the user' });
    expect(body).toBe("Couldn't delete the user. Try again in a moment.");
  });

  it('humanizeErrorText equals humanizeError(...).body', () => {
    const ctx = { action: 'reset' as const, resource: 'the system prompt' };
    const e = new Error('nope');
    expect(humanizeErrorText(e, ctx)).toBe(humanizeError(e, ctx).body);
  });
});
