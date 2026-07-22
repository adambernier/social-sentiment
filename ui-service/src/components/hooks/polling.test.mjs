import assert from 'node:assert/strict';
import test from 'node:test';

import {
  createAbortableRequestGroup,
  createLatestRequestRunner,
  requestJson,
  startCompletionScheduledPolling,
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
