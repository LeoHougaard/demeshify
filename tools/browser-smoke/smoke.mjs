import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdir, mkdtemp, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { chromium, devices, firefox, webkit } from 'playwright';

const here = import.meta.dirname;
const target = new URL(process.argv[2]);
const python = process.env.SMOKE_PYTHON || path.resolve(here, '../../.venv',
  process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
const root = path.join(here, 'artifacts');
await mkdir(root, { recursive: true });
const dir = await mkdtemp(path.join(root, 'run-'));
console.log(dir);
const cad = action => execFileSync(python, [path.join(here, 'cad.py'), action, dir],
  { encoding: 'utf8' });
cad('generate');
const engine = process.env.SMOKE_BROWSER || 'chromium';
const browser = await { chromium, firefox, webkit }[engine].launch({
  executablePath: process.env.SMOKE_BROWSER_EXE,
  args: engine === 'chromium' ? ['--enable-unsafe-swiftshader', '--disable-quic'] : [],
});
const requests = [];
const violations = [];
const transfers = [];
const conversions = [];
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
  for (const name of ['spacer', 'pin']) {
    if (name === 'pin') {
      await context.setOffline(true);
      offline = true;
    }
    await page.locator('#fileInput').setInputFiles(path.join(dir, name + '.stl'));
    await page.locator('#units').selectOption('mm');
    const start = performance.now();
    await page.locator('#reconstructButton').click();
    const link = page.locator('#downloads a[download="' + name + '_reconstructed.step"]');
    const error = page.locator('#errorBox');
    await link.or(page.locator('#errorBox:visible')).first().waitFor({timeout: 360000});
    if (await error.isVisible()) throw new Error(await error.innerText());
    conversions.push({ model: name, milliseconds: Math.round(performance.now() - start) });
    const [download] = await Promise.all([page.waitForEvent('download'), link.click()]);
    await download.saveAs(path.join(dir, name + '.step'));
    console.log('Downloaded', name);
  }
  assert.ok(requests.some(r => !r.offline), 'Expected online HTTP traffic');
  assert.deepEqual(violations, []);
  const report = cad('verify');
  await writeFile(path.join(dir, 'geometry.json'), report);
  console.log(report);
  const assets = await Promise.all(transfers);
  const metrics = { conversions, httpsAssetBytes: assets.reduce((sum, bytes) => sum + bytes, 0) };
  await writeFile(path.join(dir, 'metrics.json'), JSON.stringify(metrics, null, 2));
  console.log(JSON.stringify(metrics));
} finally {
  await browser.close();
  await writeFile(path.join(dir, 'network.json'), JSON.stringify({requests, violations}, null, 2));
}
