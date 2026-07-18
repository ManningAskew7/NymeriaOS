/**
 * Desktop-only version-skew nudge (beta-readiness 08, work item 4).
 *
 * Backend and desktop versions are tag-identical by construction
 * (scripts/sync_versions.py keeps the five manifests in lockstep and
 * release.yml builds the wheel and the installer from the same tag), so ANY
 * difference between the running app's version and the backend's /health
 * version means one side is due a refresh. For the shipped desktop app,
 * which has no auto-update in the beta, that is "grab the new installer
 * from the release page".
 *
 * Returns null in a plain browser (the web client is served BY the backend,
 * so it can never skew) and on any lookup failure. Dependencies are
 * injectable for Vitest (node environment, no window or Tauri IPC); real
 * callers use the defaults, where the Tauri API is loaded via a guarded
 * dynamic import so the web bundle never touches it.
 */

export interface VersionSkewDeps {
  isTauri?: () => boolean;
  getAppVersion?: () => Promise<string>;
}

function defaultIsTauri(): boolean {
  return typeof window !== 'undefined' && '__TAURI__' in window;
}

async function defaultGetAppVersion(): Promise<string> {
  const { getVersion } = await import('@tauri-apps/api/app');
  return getVersion();
}

export async function versionSkewNote(
  backendVersion: string | undefined,
  deps: VersionSkewDeps = {}
): Promise<string | null> {
  if (!backendVersion) return null;
  if (!(deps.isTauri ?? defaultIsTauri)()) return null;
  try {
    const appVersion = await (deps.getAppVersion ?? defaultGetAppVersion)();
    if (!appVersion || appVersion === backendVersion) return null;
    return `This app is v${appVersion} but the backend is v${backendVersion}. A matching desktop installer may be available on the release page.`;
  } catch {
    return null;
  }
}
