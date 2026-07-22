export type PollTask = (signal: AbortSignal) => Promise<void>;

export interface PollScheduler {
  setTimeout(callback: () => void, delayMs: number): unknown;
  clearTimeout(handle: unknown): void;
}

export interface PollingOptions {
  intervalMs: number;
  onError: (error: unknown) => void;
  scheduler?: PollScheduler;
}

export function requestJson<T>(
  url: string,
  signal: AbortSignal,
  fetcher?: typeof fetch,
): Promise<T | null>;

export function startCompletionScheduledPolling(
  task: PollTask,
  options: PollingOptions,
): () => void;
