export type PollTask = (signal: AbortSignal) => Promise<void>;

export interface PollScheduler {
  setTimeout(callback: () => void, delayMs: number): unknown;
  clearTimeout(handle: unknown): void;
}

export interface VisibilitySource {
  readonly visibilityState: string;
  addEventListener?(type: 'visibilitychange', listener: () => void): void;
  removeEventListener?(type: 'visibilitychange', listener: () => void): void;
}

export interface PollingOptions {
  intervalMs: number;
  onError: (error: unknown) => void;
  scheduler?: PollScheduler;
  visibilitySource?: VisibilitySource;
}

export interface ReconnectingWebSocketOptions {
  onOpen?: () => void;
  onClose?: () => void;
  onMessage?: (event: MessageEvent) => void;
  onError?: (event: Event) => void;
  baseDelayMs?: number;
  maxDelayMs?: number;
  scheduler?: PollScheduler;
  createSocket?: (url: string) => WebSocket;
}

export interface LatestRequestRunner {
  run<T>(task: (signal: AbortSignal) => Promise<T>): Promise<T | undefined>;
  cancel(): void;
}

export interface AbortableRequestGroup {
  run<T>(task: (signal: AbortSignal) => Promise<T>): Promise<T | undefined>;
  cancelAll(): void;
}

export function requestJson<T>(
  url: string,
  signal: AbortSignal,
  fetcher?: typeof fetch,
): Promise<T | null>;

export function createLatestRequestRunner(): LatestRequestRunner;

export function createAbortableRequestGroup(): AbortableRequestGroup;

export function startCompletionScheduledPolling(
  task: PollTask,
  options: PollingOptions,
): () => void;

export function startReconnectingWebSocket(
  url: string,
  options: ReconnectingWebSocketOptions,
): () => void;
