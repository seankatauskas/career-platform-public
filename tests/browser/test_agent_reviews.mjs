import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {mkdir} from 'node:fs/promises';
import {chromium} from '../../extension/node_modules/playwright-core/index.mjs';

const fixture = spawn(process.env.PYTHON || 'python3', ['-u', '-m', 'tests.fixtures.agent_review_demo'], {stdio:['ignore','pipe','pipe']});
let stderr = ''; fixture.stderr.on('data', chunk => {stderr += chunk;});
const ready = await new Promise((resolve, reject) => {
  let data = '';
  const timer = setTimeout(() => reject(new Error(`fixture timeout: ${stderr}`)), 15000);
  fixture.stdout.on('data', chunk => { data += chunk; if(data.includes('\n')) {clearTimeout(timer); resolve(JSON.parse(data.split('\n')[0]));} });
  fixture.on('exit', code => {clearTimeout(timer); reject(new Error(`fixture exited ${code}: ${stderr}`));});
});
const browser = await chromium.launch({headless:true});
try {
  const page = await browser.newPage({viewport:{width:1280,height:900}});
  const errors=[]; page.on('pageerror', e=>errors.push(e.message));
  await page.route('**/*', route => route.request().url().startsWith(ready.url) ? route.continue() : route.abort());
  await page.goto(ready.url + '/#shortlist/' + ready.lists[1].list_id);
  await page.locator('#agent-review-status').getByText('Reviewed by Codex', {exact:true}).waitFor();
  assert.equal(await page.locator('#agent-review-details').evaluate(el => el.open), false);
  assert.equal(await page.locator('#agent-review-summary').getByText('Reviewed by agents', {exact:true}).isVisible(), false);
  assert.equal(await page.getByRole('button', {name:'Refresh review progress'}).isVisible(), false);
  await page.getByText('Review details', {exact:true}).click();
  await page.locator('#agent-review-summary').getByText('Reviewed by agents', {exact:true}).waitFor();
  assert.match(await page.locator('#agent-review-summary').innerText(), /2 of 2 postings assessed/);
  assert.match(await page.locator('#agent-review-summary').innerText(), /Strict posting window/);
  await page.getByText('Assessment evidence', {exact:true}).click();
  await page.locator('.agent-assessment').getByText('Python API experience', {exact:true}).waitFor();
  assert.match(await page.locator('.agent-assessment').innerText(), /Python APIs/);
  await page.getByRole('button', {name:'Refresh review progress'}).click();
  await page.locator('.agent-review-entry').first().locator('summary').click();
  await page.locator('#agent-review-history').getByText('Review in progress', {exact:true}).waitFor();
  assert.match(await page.locator('#agent-review-history').innerText(), /0 of 2 postings assessed/);
  await page.selectOption('#shortlist-sort', 'posted-newest');
  assert.equal(await page.locator('#shortlist-list .card').count(), 1);
  await page.getByText('Review details', {exact:true}).click();
  assert.equal(await page.locator('#agent-review-details').evaluate(el => el.open), false);
  await mkdir('.cache/agent-review-browser', {recursive:true});
  await page.screenshot({path:'.cache/agent-review-browser/desktop.png',fullPage:true});
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth <= innerWidth), true);
  await page.screenshot({path:'.cache/agent-review-browser/mobile.png',fullPage:true});
  assert.deepEqual(errors, []);
  console.log('ok: review summary, evidence, resumable progress, sorting, and mobile layout');
} finally {
  await browser.close(); fixture.kill('SIGTERM');
}
