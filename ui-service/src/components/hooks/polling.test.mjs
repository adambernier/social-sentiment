import assert from 'node:assert/strict';
import test from 'node:test';

import {
  createAbortableRequestGroup,
  createLatestRequestRunner,
  requestJson,
  startCompletionScheduledPolling,
  startReconnectingWebSocket,
} from './polling.mjs';

function deferred() {
  let resolve;
  const promise = new Promise((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

function fakeScheduler() {
  let nextHandle = 1;
  const callbacks = new Map();
  const delays = [];
  const scheduler = {
    setTimeout(callback, delayMs) {
      const handle = nextHandle++;
      callbacks.set(handle, callback);
      delays.push(delayMs);
      return handle;
    },
    clearTimeout(handle) {
      callbacks.delete(handle);
    },
  };

  return {
    scheduler,
    callbacks,
    delays,
    runNext() {
      const entry = callbacks.entries().next().value;
      assert.ok(entry);
      const [handle, callback] = entry;
      callbacks.delete(handle);
      callback();
    },
  };
}

async function flushPromises() {
  await new Promise((resolve) => setImmediate(resolve));
}

test('polling schedules the next cycle only after the active cycle completes', async () => {
  const clock = fakeScheduler();
  const cycles = [];
  const signals = [];

  const stop = startCompletionScheduledPolling(
    async (signal) => {
      signals.push(signal);
      const cycle = deferred();
      cycles.push(cycle);
      await cycle.promise;
    },
    {
      intervalMs: 60_000,
      onError: (error) => assert.fail(String(error)),
      scheduler: clock.scheduler,
    },
  );

  assert.equal(cycles.length, 1);
  assert.equal(clock.callbacks.size, 0);

  cycles[0].resolve();
  await flushPromises();
  assert.equal(clock.callbacks.size, 1);
  assert.deepEqual(clock.delays, [60_000]);

  clock.runNext();
  assert.equal(cycles.length, 2);
  assert.equal(clock.callbacks.size, 0);

  stop();
  assert.equal(signals[1].aborted, true);
  cycles[1].resolve();
  await flushPromises();
  assert.equal(clock.callbacks.size, 0);
});

test('stopping polling aborts an active request and suppresses abort errors', async () => {
  const clock = fakeScheduler();
  const errors = [];
  let signal;

  const stop = startCompletionScheduledPolling(
    async (activeSignal) => {
      signal = activeSignal;
      await new Promise((_resolve, reject) => {
        activeSignal.addEventListener('abort', () => {
          reject(new DOMException('cancelled', 'AbortError'));
        });
      });
    },
    {
      intervalMs: 100,
      onError: (error) => errors.push(error),
      scheduler: clock.scheduler,
    },
  );

  stop();
  await flushPromises();

  assert.equal(signal?.aborted, true);
  assert.deepEqual(errors, []);
  assert.equal(clock.callbacks.size, 0);
});

test('requestJson discards a response completed after cancellation', async () => {
  const controller = new AbortController();
  const body = deferred();
  const fetcher = async (_url, init) => {
    assert.equal(init?.signal, controller.signal);
    return {
      ok: true,
      json: () => body.promise,
    };
  };

  const result = requestJson('/api/value', controller.signal, fetcher);
  await flushPromises();
  controller.abort();
  body.resolve({ value: 'stale' });

  assert.equal(await result, null);
});

test('requestJson returns null for a non-success response', async () => {
  const fetcher = async () => ({ ok: false });

  assert.equal(
    await requestJson('/api/value', new AbortController().signal, fetcher),
    null,
  );
});

test('latest request runner aborts and discards a superseded request', async () => {
  const runner = createLatestRequestRunner();
  const firstResult = deferred();
  const secondResult = deferred();
  const signals = [];

  const first = runner.run(async (signal) => {
    signals.push(signal);
    return firstResult.promise;
  });
  const second = runner.run(async (signal) => {
    signals.push(signal);
    return secondResult.promise;
  });

  assert.equal(signals[0].aborted, true);
  assert.equal(signals[1].aborted, false);

  firstResult.resolve('stale');
  secondResult.resolve('current');

  assert.equal(await first, undefined);
  assert.equal(await second, 'current');
});

test('latest request runner cancels active work without surfacing abort errors', async () => {
  const runner = createLatestRequestRunner();
  let activeSignal;

  const result = runner.run(async (signal) => {
    activeSignal = signal;
    await new Promise((_resolve, reject) => {
      signal.addEventListener('abort', () => {
        reject(new DOMException('cancelled', 'AbortError'));
      });
    });
  });

  runner.cancel();

  assert.equal(activeSignal?.aborted, true);
  assert.equal(await result, undefined);
});

test('abortable request group preserves concurrency and cancels all active work', async () => {
  const group = createAbortableRequestGroup();
  const firstResult = deferred();
  const secondResult = deferred();
  const signals = [];

  const first = group.run(async (signal) => {
    signals.push(signal);
    return firstResult.promise;
  });
  const second = group.run(async (signal) => {
    signals.push(signal);
    return secondResult.promise;
  });

  assert.equal(signals[0].aborted, false);
  assert.equal(signals[1].aborted, false);

  group.cancelAll();
  assert.equal(signals[0].aborted, true);
  assert.equal(signals[1].aborted, true);

  firstResult.resolve('first');
  secondResult.resolve('second');
  assert.equal(await first, undefined);
  assert.equal(await second, undefined);
});

function fakeVisibilitySource(initialState = 'visible') {
  const listeners = new Set();
  return {
    visibilityState: initialState,
    addEventListener(type, listener) {
      if (type === 'visibilitychange') {
        listeners.add(listener);
      }
    },
    removeEventListener(type, listener) {
      if (type === 'visibilitychange') {
        listeners.delete(listener);
      }
    },
    setState(nextState) {
      this.visibilityState = nextState;
      for (const listener of listeners) {
        listener();
      }
    },
    get listenerCount() {
      return listeners.size;
    },
  };
}

test('polling pauses while tab is hidden and refreshes immediately on becoming visible', async () => {
  const clock = fakeScheduler();
  const visibility = fakeVisibilitySource('visible');
  let runs = 0;

  const stop = startCompletionScheduledPolling(
    async () => {
      runs += 1;
    },
    {
      intervalMs: 60_000,
      onError: (error) => assert.fail(String(error)),
      scheduler: clock.scheduler,
      visibilitySource: visibility,
    },
  );

  await flushPromises();
  assert.equal(runs, 1);
  assert.equal(clock.callbacks.size, 1);

  visibility.setState('hidden');
  assert.equal(clock.callbacks.size, 0);

  visibility.setState('visible');
  await flushPromises();
  assert.equal(runs, 2);
  assert.equal(clock.callbacks.size, 1);

  stop();
  assert.equal(visibility.listenerCount, 0);
  assert.equal(clock.callbacks.size, 0);
});

test('polling started while hidden defers first cycle until tab becomes visible', async () => {
  const clock = fakeScheduler();
  const visibility = fakeVisibilitySource('hidden');
  let runs = 0;

  const stop = startCompletionScheduledPolling(
    async () => {
      runs += 1;
    },
    {
      intervalMs: 60_000,
      onError: (error) => assert.fail(String(error)),
      scheduler: clock.scheduler,
      visibilitySource: visibility,
    },
  );

  await flushPromises();
  assert.equal(runs, 0);
  assert.equal(clock.callbacks.size, 0);

  visibility.setState('visible');
  await flushPromises();
  assert.equal(runs, 1);
  assert.equal(clock.callbacks.size, 1);

  stop();
});

test('reconnecting WebSocket backs off exponentially, resets on open, and stops cleanly', () => {
  const clock = fakeScheduler();
  const sockets = [];
  const events = [];

  const createSocket = (url) => {
    const socket = {
      url,
      closed: false,
      close() {
        this.closed = true;
      },
    };
    sockets.push(socket);
    return socket;
  };

  const stop = startReconnectingWebSocket('ws://localhost/api/stats/stream', {
    onOpen: () => events.push('open'),
    onClose: () => events.push('close'),
    onMessage: (event) => events.push(`msg:${event.data}`),
    baseDelayMs: 1_000,
    maxDelayMs: 4_000,
    scheduler: clock.scheduler,
    createSocket,
  });

  assert.equal(sockets.length, 1);
  sockets[0].onclose();
  assert.deepEqual(clock.delays, [1_000]);

  clock.runNext();
  assert.equal(sockets.length, 2);
  sockets[1].onclose();
  assert.deepEqual(clock.delays, [1_000, 2_000]);

  clock.runNext();
  assert.equal(sockets.length, 3);
  sockets[2].onclose();
  assert.deepEqual(clock.delays, [1_000, 2_000, 4_000]);

  clock.runNext();
  assert.equal(sockets.length, 4);
  sockets[3].onclose();
  assert.deepEqual(clock.delays, [1_000, 2_000, 4_000, 4_000]);

  // Successful open resets backoff back to baseDelayMs.
  clock.runNext();
  assert.equal(sockets.length, 5);
  sockets[4].onopen();
  sockets[4].onmessage({ data: 'hello' });
  sockets[4].onclose();
  assert.deepEqual(clock.delays, [1_000, 2_000, 4_000, 4_000, 1_000]);

  // Stopping cancels pending reconnect and closes active socket.
  clock.runNext();
  assert.equal(sockets.length, 6);
  stop();
  assert.equal(sockets[5].closed, true);
  sockets[5].onclose();
  assert.equal(clock.callbacks.size, 0);
  assert.ok(events.includes('open'));
  assert.ok(events.includes('msg:hello'));
});
