// Read-only smoke test of the actual served dashboard, including packaged assets.
// Used against synthetic production-Compose state in CI and the live private URL.
import assert from 'node:assert/strict';
import {chromium} from '../../extension/node_modules/playwright-core/index.mjs';

const base = process.argv[2];
assert(base, 'Provide the dashboard origin');
const browser = await chromium.launch({channel: 'chromium', headless: true});
const page = await browser.newPage();
const errors = [], failedAssets = [], rendered = [];
page.on('pageerror', error => errors.push(error.message));
page.on('response', response => {
  if (new URL(response.url()).pathname.startsWith('/assets/') && response.status() >= 400)
    failedAssets.push({path: new URL(response.url()).pathname, status: response.status()});
});
page.on('requestfailed', request => {
  if (new URL(request.url()).pathname.startsWith('/assets/'))
    failedAssets.push({path: new URL(request.url()).pathname, error: request.failure()?.errorText});
});
try {
  for (const [route, section] of [
    ['applications', '#applications'], ['shortlist', '#shortlist'],
    ['review', '#attention'], ['settings', '#settings'],
    ['settings/career-profile', '#career'],
  ]) {
    await page.goto(`${base}/#${route}`);
    await page.locator(`${section} h2`).first().waitFor({state: 'visible', timeout: 15000});
    if (section === '#career') {
      await page.locator('#career-editor-form').waitFor({state: 'visible'});
      await page.waitForFunction(() => !!state.careerProfile);
    }
    rendered.push(route);
  }
  assert.deepEqual(failedAssets, [], 'Dashboard assets must load');
  assert.deepEqual(errors, [], 'Dashboard scripts must run without errors');
  console.log(JSON.stringify({passed: true, rendered, scriptErrors: errors, failedAssets}));
} finally {
  await browser.close();
}
