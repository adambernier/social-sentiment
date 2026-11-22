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
  {
    intervalMs,
    onError,
    scheduler = defaultScheduler,
    visibilitySource = typeof document !== 'undefined' ? document : undefined,
  },
) {
  let stopped = false;
  let activeController;
  let timeoutHandle;
  let pendingWhenVisible = false;

  const isHidden = () => visibilitySource?.visibilityState === 'hidden';

  const clearScheduled = () => {
    if (timeoutHandle !== undefined) {
      scheduler.clearTimeout(timeoutHandle);
      timeoutHandle = undefined;
    }
  };

  const run = async () => {
    if (stopped) {
      return;
    }
    if (isHidden()) {
      pendingWhenVisible = true;
      return;
    }

    pendingWhenVisible = false;
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
        if (isHidden()) {
          pendingWhenVisible = true;
        } else {
          timeoutHandle = scheduler.setTimeout(() => {
            timeoutHandle = undefined;
            void run();
          }, intervalMs);
        }
      }
    }
  };

  const handleVisibilityChange = () => {
    if (stopped) {
      return;
    }
    if (isHidden()) {
      if (timeoutHandle !== undefined) {
        clearScheduled();
        pendingWhenVisible = true;
      }
      return;
    }
    if (pendingWhenVisible && !activeController) {
      pendingWhenVisible = false;
      clearScheduled();
      void run();
    }
  };

  visibilitySource?.addEventListener?.('visibilitychange', handleVisibilityChange);
  void run();

  return () => {
    stopped = true;
    visibilitySource?.removeEventListener?.('visibilitychange', handleVisibilityChange);
    activeController?.abort();
    clearScheduled();
  };
}

export function startReconnectingWebSocket(
  url,
  {
    onOpen,
    onClose,
    onMessage,
    onError,
    baseDelayMs = 1_000,
    maxDelayMs = 30_000,
    scheduler = defaultScheduler,
    createSocket = (targetUrl) => new WebSocket(targetUrl),
  },
) {
  let stopped = false;
  let attempt = 0;
  let socket;
  let reconnectTimer;

  const clearReconnect = () => {
    if (reconnectTimer !== undefined) {
      scheduler.clearTimeout(reconnectTimer);
      reconnectTimer = undefined;
    }
  };

  const connect = () => {
    if (stopped) {
      return;
    }

    const current = createSocket(url);
    socket = current;

    current.onopen = () => {
      if (stopped || socket !== current) {
        return;
      }
      attempt = 0;
      onOpen?.();
    };

    current.onmessage = (event) => {
      if (stopped || socket !== current) {
        return;
      }
      onMessage?.(event);
    };

    current.onerror = (event) => {
      if (stopped || socket !== current) {
        return;
      }
      onError?.(event);
    };

    current.onclose = () => {
      if (socket === current) {
        socket = undefined;
      }
      if (stopped) {
        return;
      }
      onClose?.();
      const delay = Math.min(maxDelayMs, baseDelayMs * 2 ** attempt);
      attempt += 1;
      clearReconnect();
      reconnectTimer = scheduler.setTimeout(() => {
        reconnectTimer = undefined;
        connect();
      }, delay);
    };
  };

  connect();

  return () => {
    stopped = true;
    clearReconnect();
    const current = socket;
    socket = undefined;
    current?.close?.();
  };
}
