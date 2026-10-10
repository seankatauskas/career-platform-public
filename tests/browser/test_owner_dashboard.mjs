// Exercise the real production dashboard handler with isolated owner records.
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {mkdtemp, mkdir, rm} from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {chromium} from '../../extension/node_modules/playwright-core/index.mjs';
import {runFixturePython} from './demo-process.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const stateDir = await mkdtemp(path.join(os.tmpdir(), 'owner-dashboard-'));
const child = spawn(process.env.PYTHON || 'python3', ['-m', 'tests.browser.owner_dashboard_fixture', '--state-dir', stateDir],
  {cwd: root, stdio: ['ignore', 'pipe', 'pipe']});
let stderr = '';
child.stderr.on('data', data => { stderr = (stderr + data).slice(-16000); });
const fixture = await new Promise((resolve, reject) => {
  let stdout = '';
  const timer = setTimeout(() => reject(new Error('Dashboard startup timed out: ' + stderr)), 20000);
  child.once('error', error => { clearTimeout(timer); reject(error); });
  child.once('exit', code => { clearTimeout(timer); reject(new Error('Dashboard exited ' + code + ': ' + stderr)); });
  child.stdout.on('data', data => {
    stdout += data;
    try { const value = JSON.parse(stdout.split('\n')[0]); clearTimeout(timer); resolve(value); } catch {}
  });
}).catch(error => { child.kill('SIGTERM'); throw error; });
const browser = await chromium.launch({headless: true});
const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
await page.route('**/*', route => route.request().url().startsWith(fixture.url) ? route.continue() : route.abort());
const errors = [], failures = [], mutations = [], reads = [];
// Force the real application query through its continuation path.
await page.route('**/api/v1/applications', route => route.continue({url: route.request().url() + '?limit=1'}));
page.on('pageerror', error => errors.push(error.message));
page.on('response', response => { if (response.status() >= 400) failures.push(new URL(response.url()).pathname); });
page.on('request', request => { if (request.method() === 'GET') reads.push(request.url()); if (request.method() === 'POST') mutations.push(new URL(request.url()).pathname); });
const output = path.join(root, '.cache/owner-dashboard');
await mkdir(output, {recursive: true});
const openApp = async () => {
  await page.goto(`${fixture.url}/#applications/${fixture.application_id}/overview`);
  await page.locator('#owner-tasks').getByText('Prepare portfolio', {exact: true}).waitFor();
};
try {
  await page.goto(fixture.url);
  await page.locator('.application-link').filter({hasText: 'Platform Engineer'}).waitFor();
  assert.equal(new URL(page.url()).pathname, '/');
  assert.equal(await page.locator('.application-link').count(), 2);
  const datedCard = page.locator('#application-list tr').filter({hasText: 'Platform Engineer'});
  assert.match(await datedCard.locator('time.posting-date').innerText(), /Posted/);
  assert.equal(await datedCard.locator('time.posting-date').getAttribute('datetime'), '2026-09-15T12:00:00Z');
  assert.equal(await datedCard.locator('time.applied-date').getAttribute('datetime'), '2026-10-01T12:00:00Z');
  const undatedCard = page.locator('#application-list tr').filter({hasText: 'Product Engineer'});
  assert.match(await undatedCard.innerText(), /Posting date unavailable/);
  assert.match(await undatedCard.innerText(), /Not applied yet/);
  assert(reads.some(url => url.includes('/api/v1/applications?cursor=')), 'Applications load the next owner page');
  assert.deepEqual(await page.locator('.sidebar a').evaluateAll(links => links.map(link => link.childNodes[0].textContent)), ['Applications', 'Shortlist', 'Review', 'Settings']);
  await page.waitForFunction(() => ownerReviewState.loaded);
  assert.match(await page.locator('#review-count').textContent(), /2/);
  await page.screenshot({path: path.join(output, 'applications.png'), fullPage: true});
  await page.locator('.sidebar a[href="#shortlist"]').click();
  await page.locator('#shortlist').getByText('A role to explore.', {exact: true}).waitFor();
  assert.equal(new URL(page.url()).pathname, '/');
  await page.locator('.sidebar a[href="#review"]').click();
  await page.locator('#attention-list').getByText('Complete the design exercise', {exact: true}).first().waitFor();
  await page.locator('#attention-list').getByText('Review the product team.', {exact: true}).first().waitFor();
  await page.screenshot({path: path.join(output, 'review.png'), fullPage: true});
  const proposal = page.locator('#attention-list .review-card').filter({hasText: 'Complete the design exercise'});
  await proposal.getByRole('button', {name: 'Accept change', exact: true}).click();
  await page.waitForFunction(() => !ownerReviewNormalized().some(item => item.raw.input?.description === 'Complete the design exercise'));
  await openApp();
  assert.match(await page.locator('#workspace-posting-dates').innerText(), /Posted/);
  assert.match(await page.locator('#workspace-posting-dates').innerText(), /Applied/);
  await page.getByRole('link', {name: 'Messages', exact: true}).click();
  await page.locator('#workspace-messages').getByRole('button', {name: /Read|Open|View/}).first().click();
  await page.locator('#workspace-messages').getByText('Please bring your portfolio. <script>unsafe()</script>', {exact: true}).waitFor();
  assert.equal(await page.locator('#workspace-messages script').count(), 0);
  await page.getByRole('link', {name: 'Documents', exact: true}).click();
  await page.locator('#workspace-documents').getByRole('heading', {name: 'Recorded documents'}).waitFor();
  await page.getByRole('link', {name: 'Answers', exact: true}).click();
  await page.locator('#workspace-answers').getByRole('heading', {name: 'Saved application answers'}).waitFor();
  await page.getByRole('link', {name: 'Overview', exact: true}).click();
  await page.locator('#owner-notes summary').click();
  const note = 'Exact e\u0301\n  <script>unsafe()</script>';
  await page.locator('#owner-notes textarea').fill(note);
  await page.locator('#owner-notes').getByRole('button', {name: 'Add a note', exact: true}).click();
  await page.waitForFunction(value => document.querySelector('#owner-notes')?.textContent.includes(value), note);
  assert.equal(await page.locator('#owner-notes script').count(), 0);
  await page.locator('#owner-tasks').getByRole('button', {name: 'Mark complete', exact: true}).first().click();
  await page.waitForFunction(() => document.querySelector('#owner-tasks')?.textContent.includes('completed'));
  await page.screenshot({path: path.join(output, 'application.png'), fullPage: true});
  await page.goto(`${fixture.url}/applications?application_id=${fixture.application_id}`);
  await page.locator('#owner-notes').waitFor();
  assert.equal(new URL(page.url()).pathname, '/');
  assert.equal(new URL(page.url()).hash, `#applications/${fixture.application_id}/overview`);
  await page.setViewportSize({width: 390, height: 844});
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
  await page.screenshot({path: path.join(output, 'mobile.png'), fullPage: true});
  await page.goto(fixture.url + '/#applications');
  await page.waitForFunction(() => ownerReviewState.loaded && state.applications.length === 2);
  await page.evaluate(({applicationId, secondId}) => {
    ownerReviewState.pages.proposals = {next_cursor: 'more-items'};
    ownerReviewState.items.push({kind: 'processing', processing: {analysis_id: 'processing:visible',
      status: 'failed', candidate_ids: [applicationId, secondId], coverage: {complete: false}}});
    renderOwnerReviewQueue();
  }, {applicationId: fixture.application_id, secondId: fixture.second_id});
  assert.equal(await page.locator('#application-list .pending-note').filter({hasText: 'Email needs processing'}).count(), 0,
    'Unlinked processing stays in global Review, not on matching candidate cards');
  assert(await page.evaluate(() => ownerReviewNormalized().some(item => item.id === 'processing:visible')));
  await page.evaluate(applicationId => {
    const issue = ownerReviewState.items.find(item => item.processing?.analysis_id === 'processing:visible');
    issue.application_id = applicationId;
    renderOwnerReviewQueue();
  }, fixture.application_id);
  assert.equal(await page.locator('#application-list .pending-note').filter({hasText: 'Email needs processing'}).count(), 1);
  assert.equal(await undatedCard.locator('.pending-note').filter({hasText: 'Email needs processing'}).count(), 0);
  await page.locator('#application-needs-review').check();
  assert.equal(await page.locator('#application-review-filter-label').textContent(), 'Needs review (loaded items)');
  await page.locator('#application-list').getByText(/This filter covers the review items loaded so far/).waitFor();
  await page.locator('.application-link').filter({hasText: 'Platform Engineer'}).waitFor();
  assert.deepEqual(errors, [], 'No browser exceptions');
  assert.deepEqual(failures, [], 'All served assets and API requests succeed');
  assert(mutations.length >= 2);
  assert(mutations.every(p => p.startsWith('/api/v1/application-commands/')), 'Only owner commands mutate application state');
  const count = runFixturePython(['-c', 'import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute("SELECT count(*) FROM applications").fetchone()[0])', path.join(stateDir, 'operations.db')], {cwd: root, encoding: 'utf8'});
  assert.equal(count.trim(), '0', 'Original lifecycle tables remain untouched');
  console.log('ok (owner dashboard navigation, global review, details, owner writes, mobile and canonical links)');
} finally {
  await browser.close(); child.kill('SIGTERM'); await rm(stateDir, {recursive: true, force: true});
}
