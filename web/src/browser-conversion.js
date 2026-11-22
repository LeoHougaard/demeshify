const STARTUP_TIMEOUT = 180_000;
const CONVERSION_TIMEOUT = 120_000;
const MAX_INPUT_BYTES = 250 * 1024 * 1024;
const contentTypes = { step: 'application/step', stl: 'model/stl', json: 'application/json' };

let worker;
let running = false;

function discardWorker() {
  worker?.terminate();
  worker = undefined;
}

function run(file, units, { mode, onProgress, signal }, reason = '') {
  return new Promise((resolve, reject) => {
    const id = Array.from(crypto.getRandomValues(new Uint8Array(16)),
      (value) => value.toString(16).padStart(2, '0')).join('');
    const started = performance.now();
    let phase = 'startup';
    let timer;
    let finished = false;

    function finish(error, result) {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      signal?.removeEventListener('abort', abort);
      if (worker) {
        worker.onmessage = null;
        worker.onerror = null;
      }
      if (error) {
        discardWorker();
        reject(error);
      } else {
        resolve(result);
      }
    }

    function fail(message, recoverable = false) {
      finish(Object.assign(new Error(message), { recoverable }));
    }

    function abort() {
      finish(new DOMException('Conversion canceled.', 'AbortError'));
    }

    function timeout(milliseconds, message, recoverable) {
      clearTimeout(timer);
      timer = setTimeout(() => fail(message, recoverable), milliseconds);
    }

    if (signal?.aborted) return abort();
    signal?.addEventListener('abort', abort, { once: true });
    onProgress?.({
      stage: reason ? 'Starting recovery' : 'Loading CAD engine',
      detail: reason ? 'Preparing a faceted STEP for verification' : 'Preparing the browser conversion engine',
      percent: 2, elapsed_seconds: 0, status: 'queued',
    });

    try {
      worker ??= new Worker(new URL('./browser-worker.js', import.meta.url), { type: 'module' });
    } catch (error) {
      return fail(`Could not start the browser conversion engine. ${error.message}`);
    }
    worker.onerror = () => fail('The browser stopped the CAD worker.', phase === 'conversion');
    worker.onmessage = ({ data }) => {
      if (data.id !== id) return;
      switch (data.type) {
        case 'ready':
          phase = 'conversion';
          timeout(CONVERSION_TIMEOUT, 'CAD reconstruction exceeded two minutes.', true);
          break;
        case 'progress':
          onProgress?.({ ...data.progress, elapsed_seconds: (performance.now() - started) / 1000 });
          break;
        case 'complete': {
          const files = Object.fromEntries(Object.entries(data.files).map(([name, bytes]) => [
            name, new Blob([bytes], { type: contentTypes[name.split('.').at(-1)] }),
          ]));
          finish(null, { report: data.report, files });
          break;
        }
        case 'error':
          fail(data.message, data.recoverable);
          break;
      }
    };
    timeout(STARTUP_TIMEOUT, 'The browser conversion engine took too long to load.');
    file.arrayBuffer().then((bytes) => {
      if (!finished) worker.postMessage({ id, bytes, filename: file.name, units, mode, reason }, [bytes]);
    }).catch((error) => finish(error));
  });
}

export async function convertInBrowser(file, inputUnits, options = {}) {
  if (!file.size) throw new Error('Choose a nonempty STL file.');
  if (file.size > MAX_INPUT_BYTES) throw new Error('Choose an STL file under 250 MiB.');
  if (running) throw new Error('A conversion is already running.');
  running = true;
  try {
    return await run(file, inputUnits, options);
  } catch (error) {
    if (!error.recoverable || options.signal?.aborted) throw error;
    return await run(file, inputUnits, options, error.message);
  } finally {
    running = false;
  }
}
