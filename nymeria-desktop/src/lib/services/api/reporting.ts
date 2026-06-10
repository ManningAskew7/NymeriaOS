import { CLIProxyApi } from './cliproxy';

export class ReportingApi extends CLIProxyApi {
  // Error Reporting

  async reportProblem(data: {
    thread_id: string | null;
    message_id: string;
    description: string;
    messages: Array<{ role: string; content: string; timestamp: string; id: string }>;
    timestamp: string;
    client_info: Record<string, string>;
  }): Promise<void> {
    const response = await fetch(`${this.getBaseUrl()}/report`, {
      method: 'POST',
      headers: this.getHeaders(),
      body: JSON.stringify(data)
    });
    if (!response.ok) {
      throw new Error(await this._extractError(response, 'Report failed'));
    }
  }
}
