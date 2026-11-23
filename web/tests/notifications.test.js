import assert from "node:assert/strict";
import test from "node:test";

import { createConversionNotifier } from "../src/notifications.js";

const runId = "b".repeat(32);

test("completion does not raise a system notification while the result is visible", async () => {
  let registrations = 0;
  const notifier = createConversionNotifier({
    Notification: { permission: "granted" },
    document: { hidden: false },
    navigator: {
      serviceWorker: {
        register: async () => {
          registrations += 1;
          return { showNotification: async () => {} };
        },
      },
    },
    location: new URL(`http://127.0.0.1:8421/?run=${runId}`),
  });

  assert.equal(await notifier.notifyComplete({ runId, fileName: "box.stl" }), false);
  assert.equal(registrations, 0);
});

test("a hidden tab receives a notification linked to the completed run", async () => {
  const shown = [];
  let workerPath;
  const notifier = createConversionNotifier({
    Notification: { permission: "granted" },
    document: { hidden: true },
    navigator: {
      serviceWorker: {
        register: async (path) => {
          workerPath = path;
          return { showNotification: async (...args) => shown.push(args) };
        },
      },
    },
    location: new URL("http://127.0.0.1:8421/"),
  });

  assert.equal(await notifier.notifyComplete({ runId, fileName: "box.stl" }), true);
  assert.equal(workerPath, "/conversion-worker.js");
  assert.equal(shown.length, 1);
  assert.equal(shown[0][0], "STEP conversion complete");
  assert.match(shown[0][1].body, /box\.stl/);
  assert.equal(
    new URL(shown[0][1].data.url).searchParams.get("run"),
    runId,
  );
});

test("notification permission is requested from the conversion action", async () => {
  let permissionRequests = 0;
  const notifier = createConversionNotifier({
    Notification: {
      permission: "default",
      requestPermission: async () => {
        permissionRequests += 1;
        return "granted";
      },
    },
  });

  assert.equal(await notifier.requestPermission(), "granted");
  assert.equal(permissionRequests, 1);
});

test("native notifications focus the existing converter when no worker is available", async () => {
  let focused = false;
  let nativeNotification;
  class FakeNotification {
    static permission = "granted";
    constructor(title, options) {
      this.title = title;
      this.options = options;
      nativeNotification = this;
    }
    close() {}
  }
  const location = new URL(`http://127.0.0.1:8421/?run=${runId}`);
  location.assign = () => assert.fail("The current result should not be reloaded");
  const notifier = createConversionNotifier({
    Notification: FakeNotification,
    document: { hidden: true },
    location,
    focus: () => {
      focused = true;
    },
  });

  assert.equal(await notifier.notifyComplete({ runId, fileName: "box.stl" }), true);
  assert.equal(focused, false);
  nativeNotification.onclick();
  assert.equal(focused, true);
});
