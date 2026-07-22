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
