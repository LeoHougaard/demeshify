import { bootGeometryRuntime } from './browser-runtime.js';
import { recognizePrisms } from './prism-profile.js';

const sourceDir = '/demeshify';
const publicArtifacts = [
  'reconstruction.step', 'reconstruction.stl', 'surface_graph.json',
  'report.json', 'joined_surfaces.step', 'analytic_diagnostic.step',
];
let runtime;

async function initialize(onProgress) {
  const [pyodide, { sources }] = await Promise.all([
    bootGeometryRuntime({ onProgress }), import('./browser-python-sources.js'),
  ]);
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
  const started = performance.now();
  const complete = result => send('complete', result, Object.values(result.files).map(bytes => bytes.buffer));

  async function tryFaceted(reason, pyodide, directory, requireRecovery = false) {
    try {
      const { buildFaceted } = await import('./faceted-step.js');
      const result = await buildFaceted(job, started, reason);
      if (!result || (requireRecovery && result.report.implementation !== 'hybrid')) return false;
      const diagnostic = `${directory}/analytic_diagnostic.step`;
      if (pyodide?.FS.analyzePath(diagnostic).exists) {
        result.files['analytic_diagnostic.step'] = pyodide.FS.readFile(diagnostic);
      }
      complete(result);
      return true;
    } catch (error) {
      if (job.mode === 'faceted') throw error;
      return false;
    }
  }

  let phase = 'startup';
  try {
    if (job.mode === 'faceted' || job.reason) {
      if (await tryFaceted(job.reason)) return;
      if (job.mode === 'faceted') {
        throw new Error('Fast faceted mode needs closed, consistently oriented, separated bodies with up to 20,000 triangles in total. Try Recognized surfaces for this STL.');
      }
    }
    if (!job.reason) {
      try {
        const prisms = recognizePrisms(job.bytes, job.units);
        if (prisms) {
          const { buildPrism } = await import('./prism-step.js');
          const result = await buildPrism(prisms, job, started);
          if (result) {
            complete(result);
            return;
          }
        }
      } catch {
        // Unrecognized or rejected inputs use the full reconstruction engine.
      }
      try {
        const { recognizeLayered } = await import('./layered-profile.js');
        const proposal = recognizeLayered(job.bytes, job.units);
        if (proposal) {
          const { buildLayered } = await import('./layered-step.js');
          const result = await buildLayered(proposal, job, started);
          if (result) {
            complete(result);
            return;
          }
        }
      } catch {
        // Continue to full reconstruction if the layered candidate is rejected.
      }
    }
    let pyodide;
    try {
      runtime ??= initialize((stage) => send('progress', {
        progress: { stage, detail: 'Preparing the browser conversion engine', percent: 3, status: 'queued' },
      }));
      pyodide = await runtime;
    } catch (error) {
      runtime = undefined;
      if (!job.reason && await tryFaceted('The full reconstruction engine could not load.')) return;
      throw error;
    }
    const directory = `/tmp/demeshify-${job.id}`;
    const path = `${directory}/input.stl`;
    pyodide.FS.mkdir(directory);
    let convert;
    try {
      pyodide.FS.writeFile(path, new Uint8Array(job.bytes), { canOwn: true });
      convert = pyodide.globals.get('demeshify_convert');
      phase = 'conversion';
      send('ready');
      const reconstruct = reason => JSON.parse(convert(path, job.filename, job.units, job.id,
        (progress) => send('progress', { progress: JSON.parse(progress) }), reason));
      let { report, pending_recovery: pendingRecovery } = reconstruct(job.reason);
      if (pendingRecovery) {
        send('progress', { progress: { stage: 'Recovering faceted solid', percent: 96, status: 'running' } });
        if (await tryFaceted('The fitted STEP did not pass geometry verification.', pyodide, directory)) return;
        ({ report } = reconstruct(pendingRecovery));
      } else if (!job.reason && report.surface?.source_mesh_fallback) {
        if (await tryFaceted('The full engine returned a faceted solid.', pyodide, directory, true)) return;
      }
      pyodide.FS.writeFile(`${directory}/report.json`, JSON.stringify(report, null, 2));
      const files = {};
      for (const name of publicArtifacts) {
        const artifact = `${directory}/${name}`;
        if (pyodide.FS.analyzePath(artifact).exists) {
          files[name] = pyodide.FS.readFile(artifact);
        }
      }
      complete({ report, files });
    } catch (error) {
      if (!job.reason && error.type === 'ValueError' && String(error.message).includes('Preview tessellation')
          && await tryFaceted('The fitted model could not produce a valid preview.')) return;
      throw error;
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
