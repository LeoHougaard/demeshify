const RETRYABLE_HTTP_STATUSES = new Set([408, 425, 429]);

async function fetchJson(fetchImpl, url) {
  const response = await fetchImpl(url, { cache: "no-store" });
  const payload = response.ok
    ? await response.json()
    : await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(payload.detail || `Request failed (${response.status}).`);
    error.retryable = response.status >= 500 || RETRYABLE_HTTP_STATUSES.has(response.status);
    throw error;
  }
  return payload;
}

export function waitForNextPoll(milliseconds = 650, environment = globalThis) {
  const documentRef = environment.document;
  const windowRef = environment.window;
  return new Promise((resolve) => {
    let timer;
    let finished = false;
    const finish = () => {
      if (finished) return;
      finished = true;
      environment.clearTimeout(timer);
      documentRef?.removeEventListener("visibilitychange", onVisibilityChange);
      windowRef?.removeEventListener("pageshow", finish);
      resolve();
    };
    const onVisibilityChange = () => {
      if (!documentRef?.hidden) finish();
    };
    documentRef?.addEventListener("visibilitychange", onVisibilityChange);
    windowRef?.addEventListener("pageshow", finish);
    timer = environment.setTimeout(finish, milliseconds);
  });
}

export async function pollReconstruction(runId, options = {}) {
  const fetchImpl = options.fetchImpl || globalThis.fetch.bind(globalThis);
  const isActive = options.isActive || (() => true);
  const onProgress = options.onProgress || (() => {});
  const pause = options.pause || ((milliseconds) => waitForNextPoll(milliseconds));
  let transientFailures = 0;

  while (isActive()) {
    let progress;
    try {
      progress = await fetchJson(fetchImpl, `/api/runs/${runId}/progress`);
    } catch (error) {
      if (!isActive()) return null;
      if (error.retryable === false) throw error;
      transientFailures += 1;
      await pause(Math.min(5000, 750 * 2 ** (transientFailures - 1)));
      continue;
    }
    if (!isActive()) return null;

    onProgress(progress);
    if (progress.status === "complete" || progress.status === "failed") {
      try {
        const report = await fetchJson(fetchImpl, `/api/runs/${runId}`);
        if (!isActive()) return null;
        return report;
      } catch (error) {
        if (!isActive()) return null;
        if (error.retryable === false) {
          if (progress.status === "failed" && progress.error) throw new Error(progress.error);
          throw error;
        }
        transientFailures += 1;
        await pause(Math.min(5000, 750 * 2 ** (transientFailures - 1)));
        continue;
      }
    }
    transientFailures = 0;
    await pause(650);
  }
  return null;
}
