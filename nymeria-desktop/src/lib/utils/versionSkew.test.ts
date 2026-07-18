import { describe, it, expect, vi } from 'vitest';
import { versionSkewNote } from './versionSkew';

const inTauri = () => true;
const inBrowser = () => false;

describe('versionSkewNote', () => {
  it('names both versions when the app trails the backend', async () => {
    const note = await versionSkewNote('0.1.0-beta.2', {
      isTauri: inTauri,
      getAppVersion: async () => '0.1.0-beta.1',
    });
    expect(note).toContain('v0.1.0-beta.1');
    expect(note).toContain('v0.1.0-beta.2');
  });

  it('also flags the reverse skew (backend trailing the app)', async () => {
    const note = await versionSkewNote('0.1.0-beta.1', {
      isTauri: inTauri,
      getAppVersion: async () => '0.1.0-beta.2',
    });
    expect(note).not.toBeNull();
  });

  it('is silent when versions match', async () => {
    await expect(
      versionSkewNote('0.1.0-beta.1', {
        isTauri: inTauri,
        getAppVersion: async () => '0.1.0-beta.1',
      })
    ).resolves.toBeNull();
  });

  it('is silent in a plain browser (the web client is served by the backend)', async () => {
    const getAppVersion = vi.fn(async () => '0.1.0-beta.1');
    await expect(
      versionSkewNote('0.1.0-beta.2', { isTauri: inBrowser, getAppVersion })
    ).resolves.toBeNull();
    expect(getAppVersion).not.toHaveBeenCalled();
  });

  it('is silent without a backend version or when the lookup fails', async () => {
    await expect(
      versionSkewNote(undefined, { isTauri: inTauri, getAppVersion: async () => '1' })
    ).resolves.toBeNull();
    await expect(
      versionSkewNote('0.1.0-beta.2', {
        isTauri: inTauri,
        getAppVersion: async () => {
          throw new Error('no IPC');
        },
      })
    ).resolves.toBeNull();
    await expect(
      versionSkewNote('0.1.0-beta.2', { isTauri: inTauri, getAppVersion: async () => '' })
    ).resolves.toBeNull();
  });
});
