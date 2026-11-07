export type Participant = 'alice' | 'bob' | 'jack'
export type DemoMode = 'mock' | 'live'
export type Scenario = 'main' | 'revision_after' | 'cancellation' | 'ambiguity'

export interface Message {
  message_id: string; chat_id: string; user_id: string; content: string
  task_id: string | null; sequence: number; routing_status: string; created_at: string
}
export interface Task {
  task_id: string; user_id: string; chat_id: string; version: number
  content: string; status: string; result: string | null; error: string | null
  dependencies: {prerequisite_task_id: string; prerequisite_version: number}[]
  pending_decisions: Decision[]
}
export interface Decision {
  decision_id: string; task_id: string; version: number; prerequisite_task_id: string
  original_version: number; updated_version: number; status: string; choice: string | null
  answer_published: boolean
}
export interface Event {
  event_id: string; chat_id: string; sequence: number; type: string; bot_id: string
  task_id: string | null; version: number | null; message_id: string | null
  payload: Record<string, unknown>; created_at: string
}
export interface Config {mock_available: boolean; live_available: boolean; live_delay_seconds: number}

export class ApiError extends Error {
  constructor(message: string, public status: number) { super(message) }
}

export async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  let response: Response
  try {
    response = await fetch(`/api${path}`, { ...options, headers: { 'Content-Type': 'application/json', ...options.headers } })
  } catch {
    throw new ApiError('Cannot reach CastleBot. Check that FastAPI is running.', 0)
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    const detail = typeof body.detail === 'string' ? body.detail : `Request failed (${response.status})`
    throw new ApiError(detail, response.status)
  }
  return response.json() as Promise<T>
}

export const api = {
  config: () => request<Config>('/demo-config'),
  send: (chatId: string, userId: Participant, content: string) => request<{message_id: string; status: string}>(
    '/messages', { method: 'POST', body: JSON.stringify({chat_id: chatId, user_id: userId, content}) }),
  message: (id: string, signal?: AbortSignal) => request<Message>(`/messages/${encodeURIComponent(id)}`, {signal}),
  messages: (chatId: string, after: number, signal?: AbortSignal) => request<Message[]>(`/chats/${encodeURIComponent(chatId)}/messages?after=${after}&limit=200`, {signal}),
  events: (chatId: string, after: number, signal?: AbortSignal) => request<{events: Event[]; next_cursor: number}>(
    `/chats/${encodeURIComponent(chatId)}/events?after=${after}&limit=200`, {signal}),
  tasks: (chatId: string, signal?: AbortSignal) => request<Task[]>(`/chats/${encodeURIComponent(chatId)}/tasks?limit=200`, {signal}),
  task: (id: string) => request<Task>(`/tasks/${encodeURIComponent(id)}`),
  decisions: (chatId: string, signal?: AbortSignal) => request<Decision[]>(`/chats/${encodeURIComponent(chatId)}/decisions`, {signal}),
  resolve: (id: string, choice: 'keep_original' | 'use_updated') => request<Decision>(
    `/dependency-decisions/${encodeURIComponent(id)}/resolve`, {method: 'POST', body: JSON.stringify({user_id: 'jack', choice})}),
  cancel: (id: string) => request<Task>(`/tasks/${encodeURIComponent(id)}/cancel`, {method: 'POST'}),
}
