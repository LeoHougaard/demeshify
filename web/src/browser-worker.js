import { bootGeometryRuntime } from './browser-runtime.js';
import { recognizePrisms } from './prism-profile.js';

const sources = import.meta.glob(['../../app/*.py', '../browser_python/*.py'], {
  eager: true, import: 'default', query: '?raw',
});
const sourceDir = '/demeshify';
const publicArtifacts = [
  'reconstruction.step', 'reconstruction.stl', 'surface_graph.json',
  'report.json', 'joined_surfaces.step', 'analytic_diagnostic.step',
];
let runtime;

async function initialize(onProgress) {
  const pyodide = await bootGeometryRuntime({ onProgress });
  pyodide.FS.mkdirTree(`${sourceDir}/app`);
  for (const [path, code] of Object.entries(sources)) {
    const name = path.split('/').at(-1);
    const directory = path.startsWith('../../app/') ? `${sourceDir}/app` : sourceDir;
    pyodide.FS.writeFile(`${directory}/${name}`, code);
  }
  pyodide.runPython(`
import sys
sys.path.insert(0, "${sourceDir}")
import ocp_compat
from app.browser_conversion import convert as demeshify_convert
`);
  return pyodide;
}

self.onmessage = async ({ data: job }) => {
  const send = (type, payload = {}, transfer = []) => {
    self.postMessage({ type, id: job.id, ...payload }, transfer);
  };
  let phase = 'startup';
  try {
    if (job.mode === 'faceted' || job.reason) {
      try {
        const started = performance.now();
        const { buildFaceted } = await import('./faceted-step.js');
        const result = await buildFaceted(job, started);
        if (result) {
          send('complete', result, Object.values(result.files).map(bytes => bytes.buffer));
          return;
        }
      } catch (error) {
        if (job.mode === 'faceted') throw error;
        // Recovery can still use the existing reconstruction engine.
      }
      if (job.mode === 'faceted') {
        throw new Error('Fast faceted mode needs closed, consistently oriented, separated bodies with up to 20,000 triangles in total. Try Recognized surfaces for this STL.');
      }
    }
    if (!job.reason) {
      try {
        const started = performance.now();
        const prisms = recognizePrisms(job.bytes, job.units);
        if (prisms) {
          const { buildPrism } = await import('./prism-step.js');
          const result = await buildPrism(prisms, job, started);
          if (result) {
            send('complete', result, Object.values(result.files).map(bytes => bytes.buffer));
            return;
          }
        }
      } catch {
        // Unrecognized or rejected inputs use the full reconstruction engine.
      }
    }
    runtime ??= initialize((stage) => send('progress', {
      progress: { stage, detail: 'Preparing the browser conversion engine', percent: 3, status: 'queued' },
    }));
    const pyodide = await runtime;
    const directory = `/tmp/demeshify-${job.id}`;
    const path = `${directory}/input.stl`;
    pyodide.FS.mkdir(directory);
    let convert;
    try {
      pyodide.FS.writeFile(path, new Uint8Array(job.bytes), { canOwn: true });
      convert = pyodide.globals.get('demeshify_convert');
      phase = 'conversion';
      send('ready');
      const report = JSON.parse(convert(path, job.filename, job.units, job.id,
        (progress) => send('progress', { progress: JSON.parse(progress) }), job.reason));
      pyodide.FS.writeFile(`${directory}/report.json`, JSON.stringify(report, null, 2));
      const files = {};
      for (const name of publicArtifacts) {
        const artifact = `${directory}/${name}`;
        if (pyodide.FS.analyzePath(artifact).exists) {
          files[name] = pyodide.FS.readFile(artifact);
        }
      }
      send('complete', { report, files }, Object.values(files).map((bytes) => bytes.buffer));
    } finally {
      convert?.destroy();
      pyodide.runPython(`import shutil; shutil.rmtree("${directory}", ignore_errors=True)`);
    }
  } catch (error) {
    const message = String(error.message || error).trim().split('\n').at(-1);
    send('error', {
      message, recoverable: phase === 'conversion' && error.type !== 'ValueError',
    });
  }
};
