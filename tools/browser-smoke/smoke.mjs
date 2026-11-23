import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { access, mkdir, mkdtemp, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { chromium, devices, firefox, webkit } from 'playwright';

const here = import.meta.dirname;
const target = new URL(process.argv[2]);
const inputsArg = process.argv.slice(3);
const complex = inputsArg[0] === '--complex';
const visual = inputsArg[0] === '--visual';
let corpus = inputsArg.map(file => path.resolve(file));
if (complex || visual) {
  assert.ok(inputsArg.length <= (visual ? 1 : 2), 'Only --complex takes a dataset name prefix');
  const fixtureGroups = JSON.parse(await readFile(path.join(here, 'complex-fixtures.json'), 'utf8'));
  if (visual) {
    fixtureGroups.fusion_official_test.push(...JSON.parse(
      await readFile(path.join(here, 'edge-fixtures.json'), 'utf8'),
    ));
  }
  const corpusRoot = process.env.SMOKE_CORPUS_ROOT || path.resolve(here, '../../datasets');
  const prefix = inputsArg[1] || '';
  corpus = Object.entries(fixtureGroups).filter(([folder]) => folder.startsWith(prefix))
    .flatMap(([folder, names]) => names.map(name => path.resolve(corpusRoot, folder, name + '.stl')));
  assert.ok(corpus.length, 'No complex fixtures matched the dataset prefix');
  for (const file of corpus) await access(file);
}
const units = process.env.SMOKE_UNITS || 'mm';
const python = process.env.SMOKE_PYTHON || path.resolve(here, '../../.venv',
  process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
const root = path.join(here, 'artifacts');
await mkdir(root, { recursive: true });
const dir = await mkdtemp(path.join(root, 'run-'));
console.log(dir);
const cad = action => execFileSync(python, [path.join(here, 'cad.py'), action, dir],
  { encoding: 'utf8' });
if (!corpus.length) cad('generate');
const engine = process.env.SMOKE_BROWSER || 'chromium';
const browser = await { chromium, firefox, webkit }[engine].launch({
  executablePath: process.env.SMOKE_BROWSER_EXE,
  args: engine === 'chromium' ? ['--enable-unsafe-swiftshader', '--disable-quic'] : [],
});
const requests = [];
const violations = [];
const transfers = [];
const conversions = [];
const minFaceReduction = Number(process.env.SMOKE_MIN_FACE_REDUCTION || 0);
assert.ok(minFaceReduction >= 0 && minFaceReduction < 1, 'Expected a minimum face reduction between 0 and 1');
const assetCache = process.env.SMOKE_ASSET_CACHE;
const assetTypes = { '.mjs': 'text/javascript', '.wasm': 'application/wasm', '.json': 'application/json' };
let offline = false;
try {
  const device = process.env.SMOKE_DEVICE;
  if (device && !devices[device]) throw new Error(`Unknown Playwright device: ${device}`);
  const context = await browser.newContext({ ...devices[device], serviceWorkers: 'block' });
  context.on('requestfinished', request => {
    if (!assetCache && request.url().startsWith('https://')) {
      transfers.push(request.sizes().then(size => size.responseBodySize));
    }
  });
  await context.route('**/*', async route => {
    const req = route.request();
    const url = new URL(req.url());
    if (!['http:', 'https:'].includes(url.protocol)) return route.continue();
    const entry = { method: req.method(), url: req.url(), offline,
      hasBody: req.postDataBuffer() !== null };
    requests.push(entry);
    const knownAssetOrigin = url.origin === target.origin
      || (url.hostname === 'cdn.jsdelivr.net' && url.pathname.startsWith('/pyodide/'))
      || (url.hostname === 'files.pythonhosted.org' && url.pathname.startsWith('/packages/'));
    if (!['GET', 'HEAD'].includes(entry.method) || entry.hasBody ||
        !knownAssetOrigin || /(?:^|\/)api(?:\/|$)|(?:spacer|pin)\.stl/i.test(url.pathname)) {
      violations.push(entry);
      return route.abort('blockedbyclient');
    }
    if (offline) return route.abort('internetdisconnected');
    if (process.env.SMOKE_FAIL_PYTHON && url.origin !== target.origin) return route.abort('failed');
    if (assetCache && url.origin !== target.origin) {
      const file = path.posix.basename(url.pathname);
      const body = await readFile(path.join(assetCache, file));
      transfers.push(body.byteLength);
      return route.fulfill({ body, headers: { 'Access-Control-Allow-Origin': '*' },
        contentType: assetTypes[path.extname(file)] || 'application/octet-stream' });
    }
    return route.continue();
  });
  await context.routeWebSocket('**/*', ws => {
    violations.push({ websocket: ws.url() });
    ws.close();
  });
  const page = await context.newPage();
  page.on('pageerror', error => console.error(error.message));
  page.on('requestfailed', req => console.error(req.url(), req.failure()));
  await page.goto(target.href);
  assert.match(await page.locator('#engineStatus').innerText(), /browser/i);
  if (process.env.SMOKE_MODE) {
    await page.locator('#advancedToggle').click();
    await page.locator('#engine').selectOption(process.env.SMOKE_MODE);
    await page.locator('#advancedClose').click();
  }
  const inputs = corpus.length ? corpus : ['spacer', 'pin'].map(name => path.join(dir, `${name}.stl`));
  for (const file of inputs) {
    const name = path.basename(file, '.stl');
    const outputName = name.replace(/[^A-Za-z0-9._-]+/g, '_').replace(/^[._]+|[._]+$/g, '') || 'model';
    if (!corpus.length && name === 'pin') {
      await context.setOffline(true);
      offline = true;
    }
    await page.locator('#fileInput').setInputFiles(file);
    await page.locator('#units').selectOption(units);
    const start = performance.now();
    await page.locator('#reconstructButton').click();
    const link = page.locator('#downloads a[download="' + outputName + '_reconstructed.step"]');
    const error = page.locator('#errorBox');
    await link.or(page.locator('#errorBox:visible')).first().waitFor({timeout: 360000});
    if (await error.isVisible()) {
      if (!corpus.length) throw new Error(await error.innerText());
      conversions.push({ model: name, error: await error.innerText() });
      console.log('Failed', JSON.stringify(conversions.at(-1)));
      continue;
    }
    const milliseconds = Math.round(performance.now() - start);
    const reportLink = page.locator('#advancedDownloads a[download="report.json"]');
    const report = await reportLink.evaluate(async link => (await fetch(link.href)).json());
    await writeFile(path.join(dir, name + '.report.json'), JSON.stringify(report, null, 2));
    if (minFaceReduction) {
      const { mesh, surface } = report;
      assert.ok(surface.brep_face_count <= mesh.triangle_count * (1 - minFaceReduction),
        `${name} kept ${surface.brep_face_count} faces for ${mesh.triangle_count} source triangles`);
      assert.ok(surface.recognized_surface_count > 0, `${name} has no recovered faces`);
    }
    if (visual || process.env.SMOKE_SCREENSHOTS) {
      await page.locator('#outputCanvas').waitFor({ state: 'visible' });
      await page.locator('#surfaceLegend').waitFor({ state: 'visible' });
      if (process.env.SMOKE_EXPECT_STEP_EDGES && !report.surface.faceted_fallback) {
        assert.equal(await page.locator('#surfaceBoundaryLabel').innerText(), 'STEP edges');
      }
      await page.screenshot({ path: path.join(dir, name + '.png') });
    }
    conversions.push({ model: name, milliseconds, implementation: report.implementation || 'reconstruction' });
    const [download] = await Promise.all([page.waitForEvent('download'), link.click()]);
    await download.saveAs(path.join(dir, name + '.step'));
    console.log('Downloaded', name);
  }
  assert.ok(requests.some(r => !r.offline), 'Expected online HTTP traffic');
  assert.deepEqual(violations, []);
  const report = corpus.length ? execFileSync(python,
    [path.join(here, 'cad.py'), 'verify-corpus', dir, '--units', units, ...corpus], { encoding: 'utf8' }) : cad('verify');
  await writeFile(path.join(dir, 'geometry.json'), report);
  console.log(report);
  const assets = await Promise.all(transfers);
  const metrics = { conversions, httpsAssetBytes: assets.reduce((sum, bytes) => sum + bytes, 0) };
  await writeFile(path.join(dir, 'metrics.json'), JSON.stringify(metrics, null, 2));
  console.log(JSON.stringify(metrics));
  if (corpus.length) {
    const failures = JSON.parse(report).filter(result => !result.verified);
    if (minFaceReduction) {
      for (const record of JSON.parse(report)) {
        if (!record.verified) continue;
        const faces = Object.values(record.faces).reduce((sum, count) => sum + count, 0);
        assert.ok(faces <= record.triangles * (1 - minFaceReduction),
          `${record.file} reopened with ${faces} faces for ${record.triangles} triangles`);
      }
    }
    if (complex) {
      const audit = execFileSync(python, [path.join(here, 'cad.py'), 'audit-references', dir, ...corpus],
        { encoding: 'utf8' });
      await writeFile(path.join(dir, 'reference-audit.json'), audit);
      const auditFailures = JSON.parse(audit).filter(result => result.error || result.strict_cad === null);
      assert.deepEqual(auditFailures, [], 'Some complex STEP files could not be audited against reference CAD');
    }
    assert.deepEqual(failures, [], 'Some downloaded STEP files failed independent verification');
  }
  if (process.env.SMOKE_EXPECT_IMPLEMENTATION) {
    assert.ok(conversions.every(result => result.implementation === process.env.SMOKE_EXPECT_IMPLEMENTATION),
      `Expected all files to use ${process.env.SMOKE_EXPECT_IMPLEMENTATION}`);
  }
} finally {
  await browser.close();
  await writeFile(path.join(dir, 'network.json'), JSON.stringify({requests, violations}, null, 2));
}
