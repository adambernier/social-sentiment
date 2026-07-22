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

export function createLatestRequestRunner() {
  let activeController;

  return {
    async run(task) {
      activeController?.abort();
      const controller = new AbortController();
      activeController = controller;

      try {
        const result = await task(controller.signal);
        return activeController === controller && !controller.signal.aborted
          ? result
          : undefined;
      } catch (error) {
        if (
          activeController !== controller ||
          controller.signal.aborted ||
          isAbortError(error)
        ) {
          return undefined;
        }
        throw error;
      } finally {
        if (activeController === controller) {
          activeController = undefined;
        }
      }
    },
    cancel() {
      activeController?.abort();
      activeController = undefined;
    },
  };
}

export function createAbortableRequestGroup() {
  const activeControllers = new Set();

  return {
    async run(task) {
      const controller = new AbortController();
      activeControllers.add(controller);

      try {
        const result = await task(controller.signal);
        return controller.signal.aborted ? undefined : result;
      } catch (error) {
        if (controller.signal.aborted || isAbortError(error)) {
          return undefined;
        }
        throw error;
      } finally {
        activeControllers.delete(controller);
      }
    },
    cancelAll() {
      for (const controller of activeControllers) {
        controller.abort();
      }
      activeControllers.clear();
    },
  };
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
