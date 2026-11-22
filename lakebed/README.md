# Demeshify launcher

[Demeshify on Lakebed](https://demeshify.lakebed.app/) opens the static converter
at [GitHub Pages](https://leohougaard.github.io/demeshify/). The browser loads the
conversion engine from pinned assets. Neither host receives the selected mesh.

Run `npx lakebed@0.0.39 deploy` from this folder to update the existing launcher.
The checked-in `lakebed.json` identifies its deploy.

Build the converter with `npm --prefix web run build:browser -- --base=/demeshify/`
from the repository root. GitHub Pages publishes `web/dist-browser` from the
`gh-pages` branch. Keep `.nojekyll` at the root of the published branch.

With the Python development environment available for independent STEP checks,
install the test runner with `npm --prefix tools/browser-smoke ci`. Install a
matching browser with Playwright or set `SMOKE_BROWSER_EXE` to a compatible
installed executable. Then run
`npm --prefix tools/browser-smoke run smoke -- https://leohougaard.github.io/demeshify/`.
The smoke test verifies two downloaded STEP solids, blocks model transmissions,
and runs the second conversion offline. Set `SMOKE_BROWSER=webkit` and
`SMOKE_DEVICE=iPhone 17 Pro` to exercise the mobile layout in Playwright WebKit.
