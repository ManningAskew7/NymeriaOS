import { UiPromptsApi } from './ui-prompts';

export { abortCurrentStream, hasActiveStreamForThread } from './chat';
export { probeConnection } from './base';
export type { ConnectionProbeResult } from './base';

export class NymeriaAPI extends UiPromptsApi {}

export const api = new NymeriaAPI();
