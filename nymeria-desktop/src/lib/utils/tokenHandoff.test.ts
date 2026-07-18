import { describe, it, expect, vi, afterEach } from 'vitest';
import {
  extractTokenFromHash,
  consumeTokenHandoff,
  issuePersonalToken,
  exchangePastedToken,
} from './tokenHandoff';

describe('extractTokenFromHash', () => {
  it('extracts a well-formed nym_ token', () => {
    expect(extractTokenFromHash('#token=nym_abc123XYZ')).toBe('nym_abc123XYZ');
    expect(extractTokenFromHash('#token=nym_a-b_c')).toBe('nym_a-b_c');
  });

  it('rejects everything else', () => {
    expect(extractTokenFromHash('')).toBeNull();
    expect(extractTokenFromHash('#')).toBeNull();
    expect(extractTokenFromHash('#token=')).toBeNull();
    expect(extractTokenFromHash('#token=nym_')).toBeNull();
    // Provider keys are the classic paste mistake; never treat one as a token.
    expect(extractTokenFromHash('#token=sk-ant-xxxx')).toBeNull();
    // Trailing garbage means the fragment is not exactly one token.
    expect(extractTokenFromHash('#token=nym_abc&next=1')).toBeNull();
    expect(extractTokenFromHash('#token=nym_abc def')).toBeNull();
    expect(extractTokenFromHash('#other=nym_abc')).toBeNull();
    expect(extractTokenFromHash('token=nym_abc')).toBeNull();
  });
});

describe('consumeTokenHandoff', () => {
  const okProbe = vi.fn(async () => ({ ok: true as const }));

  function deps(overrides: Partial<Parameters<typeof consumeTokenHandoff>[0]> = {}) {
    return {
      hash: '#token=nym_valid123',
      origin: 'http://localhost:8000',
      scrub: vi.fn(),
      probe: okProbe,
      adopt: vi.fn(),
      ...overrides,
    };
  }

  it('is a no-op without a token: no scrub, no probe, no adopt', async () => {
    const d = deps({ hash: '#something-else' });
    await expect(consumeTokenHandoff(d)).resolves.toBe(false);
    expect(d.scrub).not.toHaveBeenCalled();
    expect(d.adopt).not.toHaveBeenCalled();
  });

  it('scrubs before probing, then adopts the validated credentials', async () => {
    const order: string[] = [];
    const d = deps({
      scrub: vi.fn(() => order.push('scrub')),
      probe: vi.fn(async () => {
        order.push('probe');
        return { ok: true };
      }),
    });
    await expect(consumeTokenHandoff(d)).resolves.toBe(true);
    expect(order).toEqual(['scrub', 'probe']);
    expect(d.adopt).toHaveBeenCalledExactlyOnceWith('http://localhost:8000', 'nym_valid123');
  });

  it('falls through silently when the probe rejects the token', async () => {
    const d = deps({ probe: vi.fn(async () => ({ ok: false as const })) });
    await expect(consumeTokenHandoff(d)).resolves.toBe(false);
    expect(d.scrub).toHaveBeenCalledOnce();
    expect(d.adopt).not.toHaveBeenCalled();
  });

  it('falls through silently when the probe throws', async () => {
    const d = deps({
      probe: vi.fn(async () => {
        throw new Error('network down');
      }),
    });
    await expect(consumeTokenHandoff(d)).resolves.toBe(false);
    expect(d.adopt).not.toHaveBeenCalled();
  });

  it('still scrubs but never probes an unusable origin', async () => {
    for (const origin of ['', 'null']) {
      const d = deps({ origin, probe: vi.fn(async () => ({ ok: true as const })) });
      await expect(consumeTokenHandoff(d)).resolves.toBe(false);
      expect(d.scrub).toHaveBeenCalledOnce();
      expect(d.probe).not.toHaveBeenCalled();
      expect(d.adopt).not.toHaveBeenCalled();
    }
  });

  it('adopts the upgraded long-lived token when the exchange succeeds', async () => {
    const d = deps({ issueToken: vi.fn(async () => 'nym_longlived456') });
    await expect(consumeTokenHandoff(d)).resolves.toBe(true);
    expect(d.issueToken).toHaveBeenCalledExactlyOnceWith(
      'http://localhost:8000',
      'nym_valid123'
    );
    expect(d.adopt).toHaveBeenCalledExactlyOnceWith(
      'http://localhost:8000',
      'nym_longlived456'
    );
  });

  it('keeps the validated fragment token when the exchange fails or throws', async () => {
    for (const issueToken of [
      vi.fn(async () => null),
      vi.fn(async () => {
        throw new Error('token limit');
      }),
    ]) {
      const d = deps({ issueToken });
      await expect(consumeTokenHandoff(d)).resolves.toBe(true);
      expect(d.adopt).toHaveBeenCalledExactlyOnceWith(
        'http://localhost:8000',
        'nym_valid123'
      );
    }
  });

  it('never attempts the exchange for a token the probe rejected', async () => {
    const issueToken = vi.fn(async () => 'nym_longlived456');
    const d = deps({
      probe: vi.fn(async () => ({ ok: false as const })),
      issueToken,
    });
    await expect(consumeTokenHandoff(d)).resolves.toBe(false);
    expect(issueToken).not.toHaveBeenCalled();
  });
});

describe('issuePersonalToken', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function stubFetch(response: { ok: boolean; body?: unknown; throwOnJson?: boolean }) {
    const fetchMock = vi.fn(async () => ({
      ok: response.ok,
      json: async () => {
        if (response.throwOnJson) throw new SyntaxError('bad json');
        return response.body;
      },
    }));
    vi.stubGlobal('fetch', fetchMock);
    return fetchMock;
  }

  it('POSTs /me/tokens with the bearer token and returns the raw token', async () => {
    const fetchMock = stubFetch({ ok: true, body: { raw_token: 'nym_new789' } });
    await expect(
      issuePersonalToken('http://localhost:8000/', 'nym_boot123')
    ).resolves.toBe('nym_new789');
    expect(fetchMock).toHaveBeenCalledExactlyOnceWith(
      'http://localhost:8000/me/tokens',
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({ Authorization: 'Bearer nym_boot123' }),
        body: JSON.stringify({ label: 'web-signin' }),
      })
    );
  });

  it('returns null on a non-2xx response (e.g. the 409 token limit)', async () => {
    stubFetch({ ok: false });
    await expect(
      issuePersonalToken('http://localhost:8000', 'nym_boot123')
    ).resolves.toBeNull();
  });

  it('returns null when the response body is not a nym_ token', async () => {
    for (const body of [{}, { raw_token: 42 }, { raw_token: 'sk-ant-oops' }]) {
      stubFetch({ ok: true, body });
      await expect(
        issuePersonalToken('http://localhost:8000', 'nym_boot123')
      ).resolves.toBeNull();
    }
  });

  it('lets a malformed-JSON throw propagate (the consume layer degrades safely)', async () => {
    stubFetch({ ok: true, throwOnJson: true });
    await expect(
      issuePersonalToken('http://localhost:8000', 'nym_boot123')
    ).rejects.toThrow();
  });

  it('sends a caller-provided label instead of the web-signin default', async () => {
    const fetchMock = stubFetch({ ok: true, body: { raw_token: 'nym_new789' } });
    await expect(
      issuePersonalToken('http://localhost:8000', 'nym_boot123', 'desktop-signin')
    ).resolves.toBe('nym_new789');
    expect(fetchMock).toHaveBeenCalledExactlyOnceWith(
      'http://localhost:8000/me/tokens',
      expect.objectContaining({ body: JSON.stringify({ label: 'desktop-signin' }) })
    );
  });

  it('bounds the exchange with an abort signal so a hung backend cannot stall sign-in', async () => {
    const fetchMock = stubFetch({ ok: true, body: { raw_token: 'nym_new789' } });
    await issuePersonalToken('http://localhost:8000', 'nym_boot123');
    expect(fetchMock).toHaveBeenCalledExactlyOnceWith(
      'http://localhost:8000/me/tokens',
      expect.objectContaining({ signal: expect.any(AbortSignal) })
    );
  });
});

describe('exchangePastedToken', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function stubFetch(response: { ok: boolean; body?: unknown; throwOnFetch?: boolean }) {
    const fetchMock = vi.fn(async () => {
      if (response.throwOnFetch) throw new TypeError('network down');
      return {
        ok: response.ok,
        json: async () => response.body,
      };
    });
    vi.stubGlobal('fetch', fetchMock);
    return fetchMock;
  }

  it('returns the upgraded token and passes the label through', async () => {
    const fetchMock = stubFetch({ ok: true, body: { raw_token: 'nym_longlived456' } });
    await expect(
      exchangePastedToken('http://localhost:8000', 'nym_boot123', 'desktop-signin')
    ).resolves.toBe('nym_longlived456');
    expect(fetchMock).toHaveBeenCalledExactlyOnceWith(
      'http://localhost:8000/me/tokens',
      expect.objectContaining({
        headers: expect.objectContaining({ Authorization: 'Bearer nym_boot123' }),
        body: JSON.stringify({ label: 'desktop-signin' }),
      })
    );
  });

  it('keeps the pasted token on a non-2xx response (e.g. the 409 token limit)', async () => {
    stubFetch({ ok: false });
    await expect(
      exchangePastedToken('http://localhost:8000', 'nym_boot123', 'desktop-signin')
    ).resolves.toBe('nym_boot123');
  });

  it('keeps the pasted token when the response body is not a nym_ token', async () => {
    stubFetch({ ok: true, body: { raw_token: 'sk-ant-oops' } });
    await expect(
      exchangePastedToken('http://localhost:8000', 'nym_boot123', 'desktop-signin')
    ).resolves.toBe('nym_boot123');
  });

  it('keeps the pasted token when the exchange throws (offline mid-commit)', async () => {
    stubFetch({ ok: true, throwOnFetch: true });
    await expect(
      exchangePastedToken('http://localhost:8000', 'nym_boot123', 'desktop-signin')
    ).resolves.toBe('nym_boot123');
  });
});
