import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {chromium} from '../../extension/node_modules/playwright-core/index.mjs';

const fixture = spawn(process.env.PYTHON || 'python3', ['-u', '-m', 'tests.fixtures.agent_review_demo'], {stdio:['ignore','pipe','pipe']});
let stderr = ''; fixture.stderr.on('data', chunk => {stderr += chunk;});
const ready = await new Promise((resolve, reject) => {
  let data = '';
  const timer = setTimeout(() => reject(new Error(`fixture timeout: ${stderr}`)), 15000);
  fixture.stdout.on('data', chunk => {data += chunk; if (data.includes('\n')) {clearTimeout(timer); resolve(JSON.parse(data.split('\n')[0]));}});
  fixture.on('exit', code => {clearTimeout(timer); reject(new Error(`fixture exited ${code}: ${stderr}`));});
});
const browser = await chromium.launch({headless:true});
try {
  const page = await browser.newPage({timezoneId:'America/Chicago'});
  const errors=[]; page.on('pageerror', error => errors.push(error.message));
  await page.route('**/*', route => route.request().url().startsWith(ready.url) ? route.continue() : route.abort());
  await page.goto(ready.url + '/#shortlist');
  await page.locator('#list-generator-start-label').waitFor({state:'visible'});
  assert.deepEqual(errors, []);
  assert.equal(await page.locator('#shortlist-options').evaluate(el => el.open), false);
  assert.equal(await page.locator('#exclude-recent-companies').isVisible(), true);
  assert.equal(await page.locator('#refresh-list-generation').count(), 0);
  await page.locator('#list-generator-start').fill('2026-10-01T00:00');
  const sent = page.waitForRequest(request => request.url().endsWith('/job-reviews/generate'));
  await page.getByRole('button', {name:'Generate list', exact:true}).click();
  const command = (await sent).postDataJSON();
  assert.equal(command.initial_start, '2026-10-01T05:00:00.000Z');
  await page.locator('#list-generation-status').getByText(/Queued; waiting to gather jobs/).waitFor();
  assert.equal(await page.locator('#generate-list').isDisabled(), true);
  assert.equal(await page.locator('#list-generator-start-label').isVisible(), false);
  await page.reload();
  await page.locator('#list-generation-status').getByText(/Queued; waiting to gather jobs/).waitFor();
  assert.equal(await page.locator('#generate-list').isDisabled(), true);
  // Exercise the controls against a synthetic host failure and subsequent publication.
  const status = await page.evaluate(async () => (await fetch('/api/v1/job-reviews/generation-status')).json());
  await page.route('**/api/v1/job-reviews/generation-status', route => route.fulfill({json:{...status, phase:'failed', can_resume:true}}));
  await page.locator('#refresh-curated').click();
  await page.locator('#resume-list-generation').waitFor({state:'visible'});
  await page.locator('#list-generation-status').getByText('Generation failed. Resume to retry.', {exact:true}).waitFor();
  for (const [phase, message] of [
    ['interrupted', 'Generation interrupted. Resume to continue.'],
    ['dispatch_failed', 'Generation could not start. Resume to retry.'],
  ]) {
    await page.route('**/api/v1/job-reviews/generation-status', route => route.fulfill({json:{...status, phase, can_resume:true}}));
    await page.locator('#refresh-curated').click();
    await page.locator('#list-generation-status').getByText(message, {exact:true}).waitFor();
    assert.equal(await page.locator('#resume-list-generation').isVisible(), true);
  }
  const resumed = page.waitForRequest(request => request.url().endsWith('/job-reviews/generation-resume'));
  await page.locator('#resume-list-generation').click();
  assert.equal((await resumed).postDataJSON().review_id, status.review_id);
  await page.route('**/api/v1/job-reviews/generation-status', route => route.fulfill({json:{...status, status:'published', phase:'published', can_resume:false,
    receipt:{lists:ready.lists.map((list, index) => ({...list, kind:index ? 'targeted':'broad'}))}}}));
  await page.locator('#refresh-curated').click();
  await page.waitForFunction(() => document.querySelector('#generate-list').disabled === false);
  assert.equal(await page.locator('#list-generation-status').isVisible(), false);
  assert.equal(await page.locator('#generate-list').isDisabled(), false);
  assert.deepEqual(errors, []);
  console.log('ok (generator browser controls, timezone, reload, resume and publication)');
} finally {
  await browser.close();
  fixture.kill('SIGTERM');
}
