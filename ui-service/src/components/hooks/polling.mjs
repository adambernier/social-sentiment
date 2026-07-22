const defaultScheduler = {
  setTimeout: (callback, delayMs) => globalThis.setTimeout(callback, delayMs),
  clearTimeout: (handle) => globalThis.clearTimeout(handle),
};

function isAbortError(error) {
  return (
    typeof error === 'object' &&
    error !== null &&
    'name' in error &&
    error.name === 'AbortError'
  );
}

export async function requestJson(url, signal, fetcher = fetch) {
  const response = await fetcher(url, { signal });
  if (!response.ok) {
    return null;
  }

  const data = await response.json();
  return signal.aborted ? null : data;
}

export function startCompletionScheduledPolling(
  task,
  { intervalMs, onError, scheduler = defaultScheduler },
) {
  let stopped = false;
  let activeController;
  let timeoutHandle;

  const run = async () => {
    if (stopped) {
      return;
    }

    const controller = new AbortController();
    activeController = controller;
    try {
      await task(controller.signal);
    } catch (error) {
      if (!controller.signal.aborted && !isAbortError(error)) {
        onError(error);
      }
    } finally {
      if (activeController === controller) {
        activeController = undefined;
      }
      if (!stopped) {
        timeoutHandle = scheduler.setTimeout(() => {
          void run();
        }, intervalMs);
      }
    }
  };

  void run();

  return () => {
    stopped = true;
    activeController?.abort();
    if (timeoutHandle !== undefined) {
      scheduler.clearTimeout(timeoutHandle);
    }
  };
}
