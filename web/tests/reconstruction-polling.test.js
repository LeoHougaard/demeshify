import assert from "node:assert/strict";
import test from "node:test";

import {
  pollReconstruction,
  waitForNextPoll,
} from "../src/reconstruction-polling.js";

const runId = "a".repeat(32);
const response = (body, status = 200) => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () => body,
});

test("polling keeps one run through completion and loads its report", async () => {
  const requests = [];
  const progressUpdates = [];
  const fetchImpl = async (url) => {
    requests.push(url);
    if (url.endsWith("/progress") && requests.length === 1) {
      return response({ status: "running", percent: 40 });
    }
    if (url.endsWith("/progress")) {
      return response({ status: "complete", percent: 100 });
    }
    return response({ id: runId, status: "complete" });
  };

  const report = await pollReconstruction(runId, {
    fetchImpl,
    onProgress: (progress) => progressUpdates.push(progress),
    pause: async () => {},
  });

  assert.equal(report.id, runId);
  assert.deepEqual(requests, [
    `/api/runs/${runId}/progress`,
    `/api/runs/${runId}/progress`,
    `/api/runs/${runId}`,
  ]);
  assert.deepEqual(progressUpdates.map((progress) => progress.percent), [40, 100]);
});

test("temporary connection failures retry the same run instead of abandoning it", async () => {
  let progressRequests = 0;
  const pauses = [];
  const fetchImpl = async (url) => {
    if (!url.endsWith("/progress")) return response({ id: runId });
    progressRequests += 1;
    if (progressRequests === 1) throw new Error("Tab was suspended");
    if (progressRequests === 2) return response({ status: "running" });
    return response({ status: "complete" });
  };

  const report = await pollReconstruction(runId, {
    fetchImpl,
    pause: async (milliseconds) => pauses.push(milliseconds),
  });

  assert.equal(report.id, runId);
  assert.equal(progressRequests, 3);
  assert.deepEqual(pauses, [750, 650]);
});

test("a missing run remains a permanent error", async () => {
  await assert.rejects(
    pollReconstruction(runId, {
      fetchImpl: async () => response({ detail: "Run not found" }, 404),
      pause: async () => assert.fail("A missing run must not be retried"),
    }),
    /Run not found/,
  );
});

test("returning to the tab wakes the pending poll immediately", async () => {
  const documentRef = new EventTarget();
  documentRef.hidden = true;
  const windowRef = new EventTarget();
  let clearedTimer;
  const pendingPoll = waitForNextPoll(60_000, {
    document: documentRef,
    window: windowRef,
    setTimeout: () => 42,
    clearTimeout: (timer) => {
      clearedTimer = timer;
    },
  });

  documentRef.hidden = false;
  documentRef.dispatchEvent(new Event("visibilitychange"));
  await pendingPoll;

  assert.equal(clearedTimer, 42);
});
