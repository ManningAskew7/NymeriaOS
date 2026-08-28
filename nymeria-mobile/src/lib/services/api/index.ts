import { BrowserLoginApi } from './browser-login';

export { abortCurrentStream, hasActiveStreamForThread } from './chat';
export { probeConnection } from './base';
export type { ConnectionProbeResult } from './base';

export class NymeriaAPI extends BrowserLoginApi {}

export const api = new NymeriaAPI();
