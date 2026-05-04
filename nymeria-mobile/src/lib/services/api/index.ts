import { ReportingApi } from './reporting';

export { abortCurrentStream, hasActiveStreamForThread } from './chat';
export { probeConnection } from './base';
export type { ConnectionProbeResult } from './base';

export class NymeriaAPI extends ReportingApi {}

export const api = new NymeriaAPI();
