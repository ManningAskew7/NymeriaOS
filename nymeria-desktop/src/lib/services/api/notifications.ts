import type {
  NotificationChannelType,
  NotificationDestination,
  NotificationDestinationCreate,
  NotificationDestinationTestResult,
  NotificationDestinationUpdate,
  NotificationPreferences,
  NotificationProfile,
  NotificationProfileCreate,
  NotificationProfileUpdate
} from '$lib/types';
import { TodosApi } from './todos';

/**
 * Destination + profile + preference CRUD for the notification system.
 *
 * Bell-feed methods (`getNotifications` / `markRead` / `delete`) live on
 * `TodosApi` because they share the activity router and have been there
 * since the original notification implementation; this class only adds
 * the destination / profile / preference surface introduced by the
 * profile-based notify refactor.
 */
export class NotificationsApi extends TodosApi {
  // -- channel types ----------------------------------------------------

  async getNotificationChannelTypes(): Promise<NotificationChannelType[]> {
    const response = await fetch(
      `${this.getBaseUrl()}/notifications/channel-types`,
      { headers: this.getHeaders() }
    );
    if (!response.ok) throw new Error(`API error: ${response.status}`);
    const data = await response.json();
    return ((data.channel_types as Record<string, unknown>[]) || []).map((t) => ({
      name: t.name as string,
      description: t.description as string,
      configFields: ((t.config_fields as Record<string, unknown>[]) || []).map((f) => ({
        key: f.key as string,
        label: f.label as string,
        secret: Boolean(f.secret),
        required: Boolean(f.required),
        help: (f.help as string | null | undefined) ?? null
      }))
    }));
  }

  // -- destinations -----------------------------------------------------

  async listNotificationDestinations(): Promise<NotificationDestination[]> {
    const response = await fetch(
      `${this.getBaseUrl()}/notifications/destinations`,
      { headers: this.getHeaders() }
    );
    if (!response.ok) throw new Error(`API error: ${response.status}`);
    const data = await response.json();
    return ((data.destinations as Record<string, unknown>[]) || []).map(
      this._mapDestination
    );
  }

  async createNotificationDestination(
    request: NotificationDestinationCreate
  ): Promise<NotificationDestination> {
    const response = await fetch(
      `${this.getBaseUrl()}/notifications/destinations`,
      {
        method: 'POST',
        headers: this.getHeaders(),
        body: JSON.stringify({
          name: request.name,
          type: request.type,
          config: request.config ?? {},
          secret_fields: request.secretFields ?? {},
          enabled: request.enabled ?? true
        })
      }
    );
    if (!response.ok) {
      const detail = await this._readErrorDetail(response);
      throw new Error(detail || `API error: ${response.status}`);
    }
    return this._mapDestination(await response.json());
  }

  async updateNotificationDestination(
    destId: string,
    request: NotificationDestinationUpdate
  ): Promise<NotificationDestination> {
    const body: Record<string, unknown> = {};
    if (request.name !== undefined) body.name = request.name;
    if (request.config !== undefined) body.config = request.config;
    if (request.secretFields !== undefined) body.secret_fields = request.secretFields;
    if (request.enabled !== undefined) body.enabled = request.enabled;
    const response = await fetch(
      `${this.getBaseUrl()}/notifications/destinations/${encodeURIComponent(destId)}`,
      {
        method: 'PATCH',
        headers: this.getHeaders(),
        body: JSON.stringify(body)
      }
    );
    if (!response.ok) {
      const detail = await this._readErrorDetail(response);
      throw new Error(detail || `API error: ${response.status}`);
    }
    return this._mapDestination(await response.json());
  }

  async deleteNotificationDestination(destId: string): Promise<void> {
    const response = await fetch(
      `${this.getBaseUrl()}/notifications/destinations/${encodeURIComponent(destId)}`,
      { method: 'DELETE', headers: this.getHeaders() }
    );
    if (!response.ok) throw new Error(`API error: ${response.status}`);
  }

  async testNotificationDestination(
    destId: string,
    message = 'Test notification from Nymeria'
  ): Promise<NotificationDestinationTestResult> {
    const response = await fetch(
      `${this.getBaseUrl()}/notifications/destinations/${encodeURIComponent(destId)}/test`,
      {
        method: 'POST',
        headers: this.getHeaders(),
        body: JSON.stringify({ message })
      }
    );
    if (!response.ok) throw new Error(`API error: ${response.status}`);
    const data = await response.json();
    return { ok: Boolean(data.ok), detail: (data.detail as string) || '' };
  }

  // -- profiles ---------------------------------------------------------

  async listNotificationProfiles(): Promise<NotificationProfile[]> {
    const response = await fetch(
      `${this.getBaseUrl()}/notifications/profiles`,
      { headers: this.getHeaders() }
    );
    if (!response.ok) throw new Error(`API error: ${response.status}`);
    const data = await response.json();
    return ((data.profiles as Record<string, unknown>[]) || []).map(this._mapProfile);
  }

  async createNotificationProfile(
    request: NotificationProfileCreate
  ): Promise<NotificationProfile> {
    const response = await fetch(
      `${this.getBaseUrl()}/notifications/profiles`,
      {
        method: 'POST',
        headers: this.getHeaders(),
        body: JSON.stringify({
          name: request.name,
          destination_names: request.destinationNames ?? []
        })
      }
    );
    if (!response.ok) {
      const detail = await this._readErrorDetail(response);
      throw new Error(detail || `API error: ${response.status}`);
    }
    return this._mapProfile(await response.json());
  }

  async updateNotificationProfile(
    profileId: string,
    request: NotificationProfileUpdate
  ): Promise<NotificationProfile> {
    const body: Record<string, unknown> = {};
    if (request.name !== undefined) body.name = request.name;
    if (request.destinationNames !== undefined)
      body.destination_names = request.destinationNames;
    const response = await fetch(
      `${this.getBaseUrl()}/notifications/profiles/${encodeURIComponent(profileId)}`,
      {
        method: 'PATCH',
        headers: this.getHeaders(),
        body: JSON.stringify(body)
      }
    );
    if (!response.ok) {
      const detail = await this._readErrorDetail(response);
      throw new Error(detail || `API error: ${response.status}`);
    }
    return this._mapProfile(await response.json());
  }

  async deleteNotificationProfile(profileId: string): Promise<void> {
    const response = await fetch(
      `${this.getBaseUrl()}/notifications/profiles/${encodeURIComponent(profileId)}`,
      { method: 'DELETE', headers: this.getHeaders() }
    );
    if (!response.ok) throw new Error(`API error: ${response.status}`);
  }

  // -- preferences ------------------------------------------------------

  async getNotificationPreferences(): Promise<NotificationPreferences> {
    const response = await fetch(
      `${this.getBaseUrl()}/notifications/preferences`,
      { headers: this.getHeaders() }
    );
    if (!response.ok) throw new Error(`API error: ${response.status}`);
    const data = await response.json();
    return { defaultProfile: data.default_profile as string };
  }

  async updateNotificationPreferences(
    request: Partial<NotificationPreferences>
  ): Promise<NotificationPreferences> {
    const body: Record<string, unknown> = {};
    if (request.defaultProfile !== undefined) body.default_profile = request.defaultProfile;
    const response = await fetch(
      `${this.getBaseUrl()}/notifications/preferences`,
      {
        method: 'PATCH',
        headers: this.getHeaders(),
        body: JSON.stringify(body)
      }
    );
    if (!response.ok) throw new Error(`API error: ${response.status}`);
    const data = await response.json();
    return { defaultProfile: data.default_profile as string };
  }

  // -- helpers ----------------------------------------------------------

  private _mapDestination = (d: Record<string, unknown>): NotificationDestination => ({
    id: d.id as string,
    name: d.name as string,
    type: d.type as string,
    config: (d.config as Record<string, unknown>) || {},
    secretFieldNames: Array.isArray(d.secret_field_names)
      ? (d.secret_field_names as string[])
      : [],
    enabled: Boolean(d.enabled),
    createdAt: (d.created_at as string) || '',
    updatedAt: (d.updated_at as string) || ''
  });

  private _mapProfile = (p: Record<string, unknown>): NotificationProfile => ({
    id: p.id as string,
    name: p.name as string,
    destinationNames: Array.isArray(p.destination_names)
      ? (p.destination_names as string[])
      : [],
    createdAt: (p.created_at as string) || '',
    updatedAt: (p.updated_at as string) || ''
  });

  private async _readErrorDetail(response: Response): Promise<string> {
    try {
      const body = await response.json();
      if (body && typeof body.detail === 'string') return body.detail;
    } catch {
      // ignore; fall through to generic error
    }
    return '';
  }
}
