import type {
  ActivityEntry,
  ActivityLogResponse,
  Notification,
  NotificationsResponse,
  TodoCreateRequest,
  TodoItem,
  TodoListResponse,
  TodoUpdateRequest
} from '$lib/types';
import { ThreadsApi } from './threads';

export class TodosApi extends ThreadsApi {
  // Dashboard API Methods

  async getThreadTaskCounts(): Promise<Record<string, number>> {
    const response = await fetch(`${this.getBaseUrl()}/todos/thread-counts`, {
      headers: this.getHeaders()
    });
    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }
    return response.json();
  }

  async getTodos(filterStatus?: string, threadId?: string): Promise<TodoListResponse> {
    const params = new URLSearchParams();
    if (filterStatus) {
      params.set('filter_status', filterStatus);
    }
    if (threadId) {
      params.set('thread_id', threadId);
    }

    const url = `${this.getBaseUrl()}/todos${params.toString() ? `?${params}` : ''}`;
    const response = await fetch(url, {
      headers: this.getHeaders()
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }

    const data = await response.json();

    // Convert API response to our format
    return {
      userId: data.user_id,
      items: (data.items || []).map(
        (item: Record<string, unknown>) =>
          ({
            id: item.id as string,
            task: item.task as string,
            status: item.status as string,
            createdAt: this.parseUtcTimestamp(item.created_at as string),
            updatedAt: this.parseUtcTimestamp(item.updated_at as string),
            notes: item.notes as string | undefined,
            // Scheduling fields
            scheduledFor: item.scheduled_for ? this.parseUtcTimestamp(item.scheduled_for as string) : undefined,
            threadId: item.thread_id as string | undefined,
            lastExecution: item.last_execution ? this.parseUtcTimestamp(item.last_execution as string) : undefined,
            // User management & recurrence fields
            createdBy: (item.created_by as string) || 'agent',
            recurrence: item.recurrence as string | undefined
          }) as TodoItem
      ),
      total: data.total
    };
  }

  private todoFromResponse(item: Record<string, unknown>): TodoItem {
    return {
      id: item.id as string,
      task: item.task as string,
      status: item.status as string,
      createdAt: this.parseUtcTimestamp(item.created_at as string),
      updatedAt: this.parseUtcTimestamp(item.updated_at as string),
      notes: item.notes as string | undefined,
      scheduledFor: item.scheduled_for ? this.parseUtcTimestamp(item.scheduled_for as string) : undefined,
      threadId: item.thread_id as string | undefined,
      lastExecution: item.last_execution ? this.parseUtcTimestamp(item.last_execution as string) : undefined,
      createdBy: (item.created_by as string) || 'agent',
      recurrence: item.recurrence as string | undefined
    } as TodoItem;
  }

  async createTodo(request: TodoCreateRequest): Promise<TodoItem> {
    const response = await fetch(`${this.getBaseUrl()}/todos`, {
      method: 'POST',
      headers: this.getHeaders(),
      body: JSON.stringify({
        task: request.task,
        notes: request.notes,
        scheduled_for: request.scheduledFor,
        recurrence: request.recurrence,
        thread_id: request.threadId
      })
    });

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(`API error: ${response.status} - ${errorText}`);
    }

    const data = await response.json();
    return this.todoFromResponse(data);
  }

  async updateTodo(todoId: string, request: TodoUpdateRequest): Promise<TodoItem> {
    const response = await fetch(`${this.getBaseUrl()}/todos/${todoId}`, {
      method: 'PATCH',
      headers: this.getHeaders(),
      body: JSON.stringify({
        task: request.task,
        status: request.status,
        notes: request.notes,
        scheduled_for: request.scheduledFor,
        recurrence: request.recurrence,
        thread_id: request.threadId,
        clear_schedule: request.clearSchedule,
        clear_recurrence: request.clearRecurrence
      })
    });

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(`API error: ${response.status} - ${errorText}`);
    }

    const data = await response.json();
    return this.todoFromResponse(data);
  }

  async deleteTodo(todoId: string): Promise<void> {
    const response = await fetch(`${this.getBaseUrl()}/todos/${todoId}`, {
      method: 'DELETE',
      headers: this.getHeaders()
    });

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(`API error: ${response.status} - ${errorText}`);
    }
  }

  async completeTodo(todoId: string): Promise<TodoItem> {
    const response = await fetch(`${this.getBaseUrl()}/todos/${todoId}/complete`, {
      method: 'POST',
      headers: this.getHeaders()
    });

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(`API error: ${response.status} - ${errorText}`);
    }

    const data = await response.json();
    return this.todoFromResponse(data);
  }

  async getActivity(limit: number = 50, threadId?: string): Promise<ActivityLogResponse> {
    const params = new URLSearchParams();
    params.set('limit', limit.toString());
    if (threadId) {
      params.set('thread_id', threadId);
    }

    const response = await fetch(`${this.getBaseUrl()}/activity?${params}`, {
      headers: this.getHeaders()
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }

    const data = await response.json();

    // Convert API response to our format
    return {
      entries: (data.entries || []).map(
        (entry: Record<string, unknown>) =>
          ({
            id: entry.id as string,
            timestamp: this.parseUtcTimestamp(entry.timestamp as string),
            type: entry.type as string,
            message: entry.message as string,
            threadId: entry.thread_id as string | undefined,
            metadata: entry.metadata as Record<string, unknown> | undefined
          }) as ActivityEntry
      ),
      total: data.total
    };
  }

  // Notification API Methods

  async getNotifications(): Promise<NotificationsResponse> {
    const response = await fetch(`${this.getBaseUrl()}/notifications`, {
      headers: this.getHeaders()
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }

    const data = await response.json();

    // Convert API response to our format
    return {
      notifications: (data.notifications || []).map(
        (n: Record<string, unknown>) =>
          ({
            id: n.id as string,
            summary: n.summary as string,
            threadId: n.thread_id as string | undefined,
            taskId: n.task_id as string | undefined,
            createdAt: this.parseUtcTimestamp(n.created_at as string),
            read: n.read as boolean,
            profile: (n.profile as string | null) ?? null,
            attempted: Array.isArray(n.attempted) ? (n.attempted as string[]) : [],
            deliveredTo: Array.isArray(n.delivered_to) ? (n.delivered_to as string[]) : [],
            errors: (n.errors as Record<string, string>) ?? {}
          }) as Notification
      ),
      unreadCount: data.unread_count as number
    };
  }

  async markNotificationRead(notificationId: string): Promise<void> {
    const response = await fetch(
      `${this.getBaseUrl()}/notifications/${notificationId}/read`,
      {
        method: 'POST',
        headers: this.getHeaders()
      }
    );

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }
  }

  async markAllNotificationsRead(): Promise<void> {
    const response = await fetch(
      `${this.getBaseUrl()}/notifications/read-all`,
      {
        method: 'POST',
        headers: this.getHeaders()
      }
    );

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }
  }

  async deleteNotification(notificationId: string): Promise<void> {
    const response = await fetch(
      `${this.getBaseUrl()}/notifications/${encodeURIComponent(notificationId)}`,
      {
        method: 'DELETE',
        headers: this.getHeaders()
      }
    );

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }
  }

  async clearAllNotifications(): Promise<void> {
    const response = await fetch(
      `${this.getBaseUrl()}/notifications`,
      {
        method: 'DELETE',
        headers: this.getHeaders()
      }
    );

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }
  }
}
