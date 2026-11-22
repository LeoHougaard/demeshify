import assert from 'node:assert/strict';
import { beforeEach, test } from 'node:test';

import { convertInBrowser } from '../src/browser-conversion.js';

const originalWorker = globalThis.Worker;
let timers;
let clearedTimers;

class FakeWorker {
  static instance;

  constructor() {
    FakeWorker.instance = this;
    this.terminated = false;
  }

  postMessage(job) {
    this.job = job;
  }

  emit(data) {
    this.onmessage?.({ data: { id: this.job.id, ...data } });
  }

  terminate() {
    this.terminated = true;
  }
}

beforeEach((context) => {
  timers = [];
  clearedTimers = [];
  globalThis.Worker = FakeWorker;
  context.mock.method(globalThis, 'setTimeout', (callback, milliseconds) => {
    timers.push({ callback, milliseconds });
    return milliseconds;
  });
  context.mock.method(globalThis, 'clearTimeout', (timer) => {
    clearedTimers.push(timer);
  });
  context.after(() => {
    globalThis.Worker = originalWorker;
  });
});

function input() {
  return {
    name: 'part.stl',
    size: 1,
    arrayBuffer: async () => new Uint8Array([0]).buffer,
  };
}

async function postInput() {
  await new Promise((resolve) => setImmediate(resolve));
  assert.ok(FakeWorker.instance?.job);
}

test('conversion has no fixed deadline after the worker is ready', async () => {
  const conversion = convertInBrowser(input(), 'mm');
  await postInput();
  assert.deepEqual(timers.map(timer => timer.milliseconds), [180_000]);

  FakeWorker.instance.emit({ type: 'ready' });
  assert.equal(clearedTimers.at(-1), 180_000);
  assert.equal(timers.length, 1, 'Starting reconstruction must not schedule another timeout');

  FakeWorker.instance.emit({ type: 'complete', report: { status: 'complete' }, files: {} });
  assert.deepEqual((await conversion).report, { status: 'complete' });
});

test('startup still has a deadline', async () => {
  const conversion = convertInBrowser(input(), 'mm');
  await postInput();
  timers[0].callback();
  await assert.rejects(conversion, /took too long to load/);
  assert.ok(FakeWorker.instance.terminated);
});

test('cancel still stops a conversion without a fixed deadline', async () => {
  const controller = new AbortController();
  const conversion = convertInBrowser(input(), 'mm', { signal: controller.signal });
  await postInput();
  FakeWorker.instance.emit({ type: 'ready' });
  controller.abort();
  await assert.rejects(conversion, { name: 'AbortError' });
  assert.ok(FakeWorker.instance.terminated);
});
