/**
 * Backend client: REST, server-sent events for chat, and the WebSocket stream.
 *
 * In dev, Vite proxies /api to the backend so this is always same-origin and
 * the session cookie works without any CORS credentials dance.
 */

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

const BASE = '/api';

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    credentials: 'include',
    headers: {
      ...(init.body && !(init.body instanceof Blob) ? { 'Content-Type': 'application/json' } : {}),
      ...(init.headers ?? {}),
    },
    ...init,
  });

  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      detail = body.detail ?? detail;
    } catch {
      /* body wasn't JSON — keep the status line */
    }
    throw new ApiError(detail, response.status);
  }
  if (response.status === 204) return undefined as T;
  const contentType = response.headers.get('content-type') ?? '';
  if (!contentType.includes('application/json')) return (await response.blob()) as unknown as T;
  return response.json();
}

export const api = {
  get: <T,>(path: string) => request<T>(path),
  post: <T,>(path: string, body?: unknown) =>
    request<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) }),
  put: <T,>(path: string, body?: unknown) =>
    request<T>(path, { method: 'PUT', body: JSON.stringify(body) }),
  patch: <T,>(path: string, body?: unknown) =>
    request<T>(path, { method: 'PATCH', body: JSON.stringify(body) }),
  del: <T,>(path: string) => request<T>(path, { method: 'DELETE' }),
  raw: (path: string, init: RequestInit = {}) =>
    fetch(`${BASE}${path}`, { credentials: 'include', ...init }),
};

// ---------------------------------------------------------------------------
// chat streaming
// ---------------------------------------------------------------------------
export interface ChatEvent {
  type: 'start' | 'delta' | 'tool_start' | 'tool_end' | 'final' | 'error';
  text?: string;
  tool?: string;
  input?: Record<string, unknown>;
  ok?: boolean;
  summary?: string;
  error?: string;
  conversation_id?: string;
  /** Tool output that looked like an attempt to instruct the model. */
  flagged?: boolean;
  flag_level?: string;
  /** How many secrets were removed from the tool output. */
  redacted?: number;
  /** How long the tool took. */
  duration_ms?: number;
  /** Pages the tool read, listed under the answer as links. */
  sources?: { title: string; url: string; source: string }[];
  /** Measurements of the reply, sent with `final`. */
  telemetry?: Telemetry;
}

/** What one reply cost to produce, as measured. */
export interface Telemetry {
  provider?: string;
  model?: string;
  calls?: number;
  input_tokens?: number;
  output_tokens?: number;
  tokens_per_sec?: number | null;
  ttft_ms?: number | null;
  total_ms?: number;
  load_ms?: number | null;
  energy_j?: number | null;
  avg_power_w?: number | null;
  tokens_per_joule?: number | null;
  cloud_cost_usd?: number | null;
}

export async function streamChat(
  message: string,
  conversationId: string | null,
  onEvent: (event: ChatEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(`${BASE}/ai/chat`, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message, conversation_id: conversationId, stream: true }),
    signal,
  });

  if (!response.ok || !response.body) {
    throw new ApiError(`Chat failed: ${response.status}`, response.status);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    const lines = buffer.split('\n\n');
    buffer = lines.pop() ?? '';
    for (const line of lines) {
      const payload = line.replace(/^data: ?/, '').trim();
      if (!payload || payload === '[DONE]') continue;
      try {
        onEvent(JSON.parse(payload) as ChatEvent);
      } catch {
        console.warn('[chat] unparsable event', payload);
      }
    }
  }
}

// ---------------------------------------------------------------------------
// websocket
// ---------------------------------------------------------------------------
export interface BusEvent {
  type: string;
  data: Record<string, any>;
  ts: string;
  source: string;
}

type Listener = (event: BusEvent) => void;

class EventStream {
  private socket: WebSocket | null = null;
  private listeners = new Set<Listener>();
  private reconnectDelay = 1000;
  private closed = false;
  private heartbeat: number | null = null;
  private statusListeners = new Set<(connected: boolean) => void>();

  connect() {
    if (this.socket && this.socket.readyState <= WebSocket.OPEN) return;
    this.closed = false;

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const url = `${protocol}//${window.location.host}${BASE}/ws`;
    const socket = new WebSocket(url);
    this.socket = socket;

    socket.onopen = () => {
      this.reconnectDelay = 1000;
      this.statusListeners.forEach((listener) => listener(true));
      this.heartbeat = window.setInterval(() => {
        if (socket.readyState === WebSocket.OPEN) {
          socket.send(JSON.stringify({ type: 'ping' }));
        }
      }, 25000);
    };

    socket.onmessage = (message) => {
      try {
        const event = JSON.parse(message.data) as BusEvent;
        this.listeners.forEach((listener) => listener(event));
      } catch {
        /* ignore malformed frames */
      }
    };

    socket.onclose = () => {
      if (this.heartbeat) window.clearInterval(this.heartbeat);
      this.heartbeat = null;
      this.statusListeners.forEach((listener) => listener(false));
      this.socket = null;
      if (!this.closed) {
        // Exponential backoff, capped — a backend restart shouldn't hammer the port.
        window.setTimeout(() => this.connect(), this.reconnectDelay);
        this.reconnectDelay = Math.min(this.reconnectDelay * 1.6, 15000);
      }
    };

    socket.onerror = () => socket.close();
  }

  disconnect() {
    this.closed = true;
    if (this.heartbeat) window.clearInterval(this.heartbeat);
    this.socket?.close();
    this.socket = null;
  }

  /** Returns an unsubscribe function usable directly as a React cleanup. */
  subscribe(listener: Listener): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  onStatus(listener: (connected: boolean) => void) {
    this.statusListeners.add(listener);
    return () => this.statusListeners.delete(listener);
  }

  get connected() {
    return this.socket?.readyState === WebSocket.OPEN;
  }
}

export const eventStream = new EventStream();
