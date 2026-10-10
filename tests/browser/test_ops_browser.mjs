import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile, mkdir, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { chromium } from '../../extension/node_modules/playwright-core/index.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const output = path.resolve(process.env.OPS_TEST_OUTPUT || path.join(root, 'extension/test-results/ops'));
await mkdir(output, { recursive: true });
const now = '2026-09-20T16:00:00Z';
const capability = (id, status, extra = {}) => ({ id, status, configured: true, enabled: true, last_attempt_at: now, last_success_at: null, reason_code: '', next_action: 'none', ...extra });
function initialOps() {
  return {
    health: { status: 'healthy', applications: { preparing: 3, interviewing: 1 }, pending_reviews: 2, outbox: { counts: { pending: 1 } }, projection_failures: [], work: { schedules: [{}, {}] } },
    reminders: { counts: {}, items: [] },
    notifications: { counts: { pending: 1 }, items: [], reconciliation: [{ notification_id: 'notice-1', topic: 'Interview reminder', status: 'needs_reconciliation', attempts: 1, expected_attempts: 1, expected_payload_sha256: 'a'.repeat(64), bridge_state: 'unknown' }] },
    readiness: { schema_version: 1, status: 'blocked', checked_at: now, external_services_verified: false,
      release: { source_sha: '1234567890abcdef1234567890abcdef12345678', schema_version: 1, identity_verified: true },
      metrics: { blocked_capabilities: 1, stale_capabilities: 1 },
      inference_usage: { reserved_requests: 7, reserved_tokens: 12000, inflight: 1, limits: { daily_requests: 50, daily_tokens: 50000, max_inflight: 2 } },
      capabilities: [
        capability('database', 'ready', { last_success_at: now }),
        capability('inference_usage', 'ready', { reason_code: 'inference_usage_within_limits' }),
        capability('ats.ingestion', 'stale', { last_success_at: '2026-09-19T16:00:00Z', reason_code: 'scheduled_success_overdue', next_action: 'inspect_workflow' }),
        capability('outlook', 'blocked', { reason_code: 'reauth_required', next_action: 'reconnect_outlook' }),
        capability('resume', 'configured_unverified'),
        capability('ranking', 'paused', { reason_code: 'automation_paused', next_action: 'review_activation' }),
        capability('ats.discovery', 'disabled', { enabled: false, configured: false }),
      ] },
    recovery: { items: [
      { work_id: 'work-1', task_kind: 'ats.authoritative', status: 'failed', attempts: 3, revision: 4, external_outcome: 'none', failure_kind: 'retry_exhausted', reason_code: 'latest_work_failed', retry_allowed: true },
      { work_id: 'work-2', task_kind: 'outlook.mail.sync', status: 'failed', attempts: 1, revision: 2, external_outcome: 'unknown', failure_kind: 'transport', reason_code: 'external_reconciliation_required', retry_allowed: true },
    ] },
  };
}
let ops = initialOps();
let retryMode = 'success';
let opsFail = false;
let scanFail = false;
const posts = [];
const report = { passed: false, checks: [], screenshots: [], fixture: 'Actual dashboard HTML/CSS/JS with synthetic same-origin API responses. Backend contracts have separate Python tests.' };
const server = createServer(async (request, response) => {
  try {
    const pathname = new URL(request.url, 'http://localhost').pathname;
    function json(value, status = 200) { response.writeHead(status, { 'Content-Type': 'application/json' }); response.end(JSON.stringify(value)); }
    if (request.method === 'POST') {
      let body = '';
      for await (const chunk of request) body += chunk;
      const parsed = JSON.parse(body);
      posts.push({ path: pathname, body: parsed, csrf: request.headers['x-csrf-token'] });
      if (request.headers['x-csrf-token'] !== 'fixture-csrf') return json({ error: 'invalid CSRF' }, 403);
      if (pathname === '/api/v1/ops/scan') {
        if (scanFail) { scanFail = false; return json({error:'Temporary connection failure.'}, 503); }
        ops.collection.active = {status:'queued',work_id:'manual-scan'};
        return json({status:'queued',work_id:'manual-scan',coalesced:false}, 202);
      }
      if (pathname === '/api/v1/ops/work/work-1/retry') {
        if (retryMode === 'fail-once') { retryMode = 'success'; return json({ error: 'Temporary worker connection failure.' }, 503); }
        if (retryMode === 'conflict') { ops.recovery.items[0].retry_allowed = false; ops.recovery.items[0].reason_code = 'superseded_by_success'; return json({ error: 'stale revision' }, 409); }
        assert.equal(parsed.expected_revision, 4);
        ops.recovery.items = ops.recovery.items.filter((item) => item.work_id !== 'work-1');
        return json({ schema_version: 1, command_id: parsed.idempotency_key, work_id: 'work-1', status: 'queued', revision: 5, requested_at: now });
      }
      if (pathname === '/api/v1/ops/notifications/notice-1/reconcile') {
        assert.equal(parsed.expected_attempts, 1);
        assert.equal(parsed.expected_payload_sha256, 'a'.repeat(64));
        assert(['delivered', 'not_delivered', 'abandoned'].includes(parsed.outcome));
        ops.notifications.reconciliation = [];
        return json({ status: parsed.outcome });
      }
      return json({ error: 'Unexpected mutation' }, 404);
    }
    if (pathname === '/api/v1/health') return json(ops.health);
    if (pathname === '/api/v1/ops') return opsFail ? json({ error: 'Status temporarily unavailable.' }, 503) : json(ops);
    if (pathname === '/api/v1/ops/pipeline') return json({collection:ops.collection,ranking:ops.ranking});
    const api = {
      '/api/v1/session': { csrf_token: 'fixture-csrf', api_version: 'v1' },
      '/api/v1/shortlist': { recommendations: [] }, '/api/v1/applications': { applications: [] },
      '/api/v1/resume-lab/standards': { standards: [] }, '/api/v1/career-profile': { configured: false },
      '/api/v1/attention': { items: [] }, '/api/v1/interviews': { applications: [] },
      '/api/v1/actions': { actions: [] }, '/api/v1/settings': { timezone: 'America/Chicago' },
      '/api/v1/browser/devices': { devices: [] }, '/api/v1/curated-shortlists': { lists: [] },
    };
    if (Object.hasOwn(api, pathname)) return json(api[pathname]);
    const assets = { '/': ['index.html', 'text/html'], '/assets/app.js': ['app.js', 'text/javascript'], '/assets/console.js': ['console.js', 'text/javascript'], '/assets/styles.css': ['styles.css', 'text/css'] };
    for (const file of ['chief-view.js', 'lifecycle-view.js', 'job-preview.js', 'applications-view.js', 'owner-application-view.js', 'owner-review-view.js', 'shortlist-view.js', 'review-view.js', 'settings-view.js', 'applications-view.css', 'shortlist-view.css', 'review-view.css', 'settings-view.css']) assets['/assets/' + file] = [file, file.endsWith('.js') ? 'text/javascript' : 'text/css'];
    if (!assets[pathname]) { response.writeHead(404); return response.end(); }
    const [file, mime] = assets[pathname];
    response.writeHead(200, { 'Content-Type': mime });
    response.end(await readFile(path.join(root, 'job_search/web', file)));
  } catch (error) { response.writeHead(500); response.end(JSON.stringify({ error: String(error) })); }
});
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
const url = `http://127.0.0.1:${server.address().port}`;
const browser = await chromium.launch({ channel: 'chromium', headless: true });
const errors = [];
async function pageAt(width, height) {
  const page = await browser.newPage({ viewport: { width, height }, reducedMotion: 'reduce' });
  page.on('pageerror', (error) => errors.push(error.message));
  await page.goto(url + '/#ops');
  await page.locator('#readiness-list .readiness-row').first().waitFor();
  await page.locator('#ops').scrollIntoViewIfNeeded();
  return page;
}
async function refresh(page) {
  await page.locator('#refresh-health').click();
  await page.waitForFunction(() => !document.querySelector('#refresh-health').disabled);
}
async function notice(page, value) { await page.waitForFunction((value) => document.querySelector('#ops-feedback').textContent.includes(value), value); }
try {
  const page = await pageAt(1365, 1000);
  assert.equal(new URL(page.url()).hash, '#settings/operations');
  assert.equal(await page.locator('.sidebar a').count(), 4);
  assert.equal(await page.locator('.sidebar a[href="#ops"]').count(), 0);
  assert.equal(await page.locator('.sidebar a[href="#career"]').count(), 0);
  assert.equal(await page.locator('.sidebar a[aria-current="page"]').innerText(), 'Settings');
  await page.locator('#ops a[href="#settings"]').click();
  await page.locator('#settings').waitFor({state:'visible'});
  await page.locator('#settings .settings-link-row[href="#settings/operations"]').click();
  await page.locator('#ops').waitFor({state:'visible'});
  await page.reload();
  await page.locator('#ops').waitFor({state:'visible'});
  assert.equal(await page.locator('.sidebar a[aria-current="page"]').innerText(), 'Settings');
  await page.goBack();
  await page.locator('#settings').waitFor({state:'visible'});
  await page.locator('#header-notifications > summary').click();
  await page.locator('#header-health').click();
  await page.locator('#ops').waitFor({state:'visible'});
  assert.equal(new URL(page.url()).hash, '#settings/operations');
  report.checks.push('Operations lives under Settings; legacy links, reload, back navigation and the health shortcut reach the correct view.');
  await page.goto(url + '/#career');
  await page.locator('#career').waitFor({state:'visible'});
  assert.equal(new URL(page.url()).hash, '#settings/career-profile');
  assert.equal(await page.locator('.sidebar a[aria-current="page"]').innerText(), 'Settings');
  await page.locator('#career a[href="#settings"]').click();
  await page.locator('#settings a[href="#settings/career-profile"]').click();
  await page.locator('#career').waitFor({state:'visible'});
  await page.reload();
  await page.locator('#career').waitFor({state:'visible'});
  assert.equal(await page.title(), 'Career profile · Settings · Career Platform');
  await page.goBack();
  await page.locator('#settings').waitFor({state:'visible'});
  await page.locator('#header-notifications > summary').click();
  await page.locator('#header-health').click();
  await page.locator('#ops').waitFor({state:'visible'});
  report.checks.push('Career profile remains available inside Settings, including legacy links, refresh and back navigation.');
  await page.waitForFunction(() => !document.querySelector('#ops').hasAttribute('aria-busy') && document.querySelector('#readiness-list .readiness-row'));
  assert.match(await page.locator('#notification-status').textContent(), /needs attention/);
  assert.match(await page.locator('#readiness-summary').innerText(), /does not contact Outlook, Telegram, or model providers/);
  for (const label of ['Working', 'Overdue', 'Needs attention', 'Awaiting verification', 'Paused', 'Not enabled']) {
    assert(await page.locator('#readiness-list .ops-badge').filter({ hasText: label }).count());
  }
  assert.equal(await page.locator('.ops-passive-group > details').getAttribute('open'), null);
  assert.equal(await page.locator('#readiness-list > .readiness-row').count(), 2);
  await page.locator('.ops-passive-group > details > summary').click();
  const usage = page.locator('#readiness-list .readiness-row').filter({ hasText: 'Model usage limits' });
  await usage.locator('.ops-service-details > summary').click();
  assert.match(await usage.innerText(), /Requests reserved today/);
  assert.match(await usage.innerText(), /7 \/ 50/);
  assert.equal(await page.locator('#recovery-list button').count(), 1);
  const uncertain = page.locator('#recovery-list article').filter({ hasText: 'Outlook email sync' });
  assert.equal(await uncertain.locator('button').count(), 0);
  report.checks.push('Evidence-based states override healthy process liveness; unknown outcomes have no retry action.');

  await page.locator('#ops').screenshot({ path: path.join(output, 'ops-desktop.png') });
  report.screenshots.push('ops-desktop.png');
  const mobile = await pageAt(390, 844);
  assert(await mobile.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await mobile.locator('#ops').screenshot({ path: path.join(output, 'ops-mobile.png') });
  report.screenshots.push('ops-mobile.png');
  await mobile.close();
  report.checks.push('Desktop and 390px mobile layouts have no horizontal overflow.');

  const retry = page.locator('#recovery-list button');
  await page.keyboard.press('Tab');
  await retry.focus();
  assert.notEqual(await retry.evaluate((element) => getComputedStyle(element).outlineStyle), 'none');
  retryMode = 'fail-once';
  await page.keyboard.press('Enter');
  await notice(page, 'not confirmed as queued');
  await page.locator('#recovery-list button').click();
  await notice(page, 'Work queued');
  const retryPosts = posts.filter((item) => item.path.endsWith('/retry'));
  assert.equal(retryPosts.length, 2);
  assert.deepEqual(retryPosts[0].body, retryPosts[1].body);
  assert(retryPosts.every((item) => item.csrf === 'fixture-csrf'));
  assert.equal(await page.evaluate(() => document.activeElement.id), 'recovery-heading');
  report.checks.push('Keyboard retry uses CSRF, expected revision, and a stable idempotency key across a failed request.');

  const review = page.locator('.ops-notification-review');
  assert.equal(await review.locator('button:disabled').count(), 3);
  await review.locator('input[type=checkbox]').focus();
  await page.keyboard.press('Space');
  assert.equal(await review.locator('button:enabled').count(), 3);
  await review.getByRole('button', { name: 'It was delivered', exact: true }).focus();
  await page.keyboard.press('Enter');
  await notice(page, 'marked as delivered');
  const notificationPost = posts.find((item) => item.path.endsWith('/reconcile'));
  assert.equal(notificationPost.body.outcome, 'delivered');
  assert.equal(notificationPost.csrf, 'fixture-csrf');
  assert.equal(await page.locator('.ops-notification-review').count(), 0);
  report.checks.push('Uncertain delivery appears even outside recent notifications; explicit keyboard confirmation precedes reconciliation.');

  ops = initialOps();
  retryMode = 'conflict';
  await refresh(page);
  await page.locator('#recovery-list button').click();
  await notice(page, 'changed since the page loaded');
  assert.equal(await page.locator('#recovery-list button').count(), 0);
  report.checks.push('Stale recovery revision refreshes state and removes the outdated retry action.');

  for (const status of ['ready', 'paused', 'disabled', 'configured_unverified']) {
    ops = initialOps();
    ops.readiness.status = status;
    ops.readiness.capabilities = [capability('ats.ingestion', status)];
    ops.readiness.metrics = {blocked_capabilities:0, stale_capabilities:0};
    ops.recovery.items = [];
    ops.notifications = {counts:{},items:[],reconciliation:[]};
    await refresh(page);
    assert.equal(await page.locator('#notification-dot').isVisible(), false, status);
    assert.equal(await page.locator('#readiness-summary').isVisible(), true);
    const bell = page.locator('#header-notifications > summary');
    assert.equal(await bell.isVisible(), true);
    await bell.click();
    assert.equal(await page.locator('#notification-status').innerText(), 'No issues need attention.');
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#header-notifications').getAttribute('open'), null);
    assert.equal(await bell.evaluate(element => element === document.activeElement), true);
  }
  ops = initialOps();
  ops.readiness.status = 'ready';
  ops.readiness.capabilities = [capability('database', 'ready')];
  ops.recovery.items = [];
  await refresh(page);
  assert.equal(await page.locator('#notification-dot').isVisible(), true);
  assert.match(await page.locator('#notification-status').textContent(), /delivery needs attention/);
  ops.notifications = {counts:{}, items:[], reconciliation:[]};
  ops.recovery.items = initialOps().recovery.items;
  await refresh(page);
  assert.equal(await page.locator('#notification-dot').isVisible(), true);
  assert.match(await page.locator('#notification-status').textContent(), /Background work needs attention/);
  ops.recovery.items = [];
  ops.notifications.items = [{notification_id:'failed-delivery',topic:'Reminder',status:'failed',attempts:1}];
  await refresh(page);
  assert.equal(await page.locator('#notification-dot').isVisible(), true);
  ops.notifications.items = [];
  await refresh(page);
  assert.equal(await page.locator('#notification-dot').isVisible(), false);
  report.checks.push('Recovery work and uncertain or failed delivery remain actionable even when service readiness reports healthy.');
  ops.readiness.status = 'paused';
  ops.readiness.capabilities.push(capability('outlook', 'blocked'));
  await refresh(page);
  assert.equal(await page.locator('#notification-dot').isVisible(), true);
  await page.locator('.sidebar a[href="#applications"]').click();
  await page.locator('#header-notifications > summary').click();
  await page.locator('#header-health').click();
  await page.locator('#ops').waitFor({state:'visible'});
  assert.equal(new URL(page.url()).hash, '#settings/operations');
  ops.readiness.capabilities = [capability('ats.ingestion', 'paused')];
  await refresh(page);
  assert.equal(await page.locator('#notification-dot').isVisible(), false);
  await page.setViewportSize({width:390,height:844});
  await page.locator('#header-notifications > summary').click();
  const panel = await page.locator('.notification-panel').boundingBox();
  assert(panel.x >= 0 && panel.x + panel.width <= 390);
  await page.screenshot({path:path.join(output,'notifications-empty-mobile.png')});
  await page.locator('.brand').click();
  assert.equal(await page.locator('#header-notifications').getAttribute('open'), null);
  await page.goto(url + '/#settings/operations');
  await page.locator('#ops').waitFor({state:'visible'});
  await page.setViewportSize({width:1365,height:1000});
  report.checks.push('Header notification stays quiet during normal or intentionally paused work, appears for actionable issues and clears when resolved.');

  opsFail = true;
  await refresh(page);
  await notice(page, 'last displayed results may be out of date');
  assert.match(await page.locator('#notification-status').textContent(), /could not be checked/);
  assert.equal(await page.locator('#notification-dot').isVisible(), true);
  opsFail = false;
  ops = initialOps();
  delete ops.readiness;
  delete ops.recovery;
  await refresh(page);
  assert.match(await page.locator('#readiness-summary').innerText(), /Readiness is not available/);
  assert.equal(await page.locator('#notification-dot').isVisible(), true);
  assert.equal(await page.locator('#recovery-list button').count(), 0);
  report.checks.push('Failed checks and older servers do not claim readiness or expose recovery actions.');
  ops.collection = {available:true,active:null,next_scan_at:now,last_scan_at:now};
  ops.ranking = {state:'waiting_allowance',retry_at:now,available:true,postings:7000,total_families:6700,
    policies:{selective:{ranked_families:500,unranked_families:6200,freshness:'stale'},broad:{ranked_families:500,unranked_families:6200,freshness:'stale'}}};
  await refresh(page);
  assert.match(await page.locator('#ranking-progress').innerText(), /waiting for the model allowance/);
  assert.match(await page.locator('#ranking-progress').innerText(), /6,200 without rankings/);
  assert.match(await page.locator('#ranking-progress').innerText(), /do not appear in automatic model picks/);
  scanFail = true;
  await page.locator('#scan-now').click();
  await notice(page, 'scan request was not confirmed');
  const firstScan = posts.at(-1);
  await page.locator('#scan-now').click();
  await notice(page, 'Scan queued');
  assert.equal(posts.at(-1).body.idempotency_key, firstScan.body.idempotency_key);
  assert.equal(await page.locator('#scan-now').isDisabled(), true);
  ops.collection.active = null;
  ops.ranking.state = 'running';
  ops.ranking.current_pass = {status:'scoring',checked_families:3000,total_families:6700,reused:false,
    policies:{selective:{recomputed_families:0,reused_families:3000},broad:{recomputed_families:0,reused_families:3000}}};
  ops.ranking.policies = {selective:{ranked_families:6700,unranked_families:0,freshness:'stale'},broad:{ranked_families:6700,unranked_families:0,freshness:'stale'}};
  await refresh(page);
  assert.equal(await page.locator('#ranking-progress progress').getAttribute('value'), '3000');
  assert.doesNotMatch(await page.locator('#ranking-progress').innerText(), /Next attempt/);
  assert.match(await page.locator('#ranking-progress').innerText(), /Checking for ranking updates/);
  assert.match(await page.locator('#ranking-progress').innerText(), /Checked 3,000 of 6,700 job families for changes/);
  assert.match(await page.locator('#ranking-progress').innerText(), /6,700 families with saved rankings · 0 without rankings/);
  assert.match(await page.locator('#ranking-progress').innerText(), /Selective this check: 0 rankings recomputed · 3,000 saved rankings reused/);
  assert.match(await page.locator('#ranking-progress').innerText(), /Broad this check: 0 rankings recomputed · 3,000 saved rankings reused/);
  ops.ranking.current_pass.policies.broad = {recomputed_families:2,reused_families:2998};
  await refresh(page);
  assert.match(await page.locator('#ranking-progress').innerText(), /Broad this check: 2 rankings recomputed · 2,998 saved rankings reused/);
  const progressPosts = posts.length;
  for (const width of [390,1365]) {
    await page.setViewportSize({width,height:1000});
    ops.ranking.current_pass = {status:'checking',checked_families:0,total_families:null,reused:false,policies:{}};
    await refresh(page);
    assert.equal(await page.locator('#ranking-progress progress').count(), 0);
    assert.match(await page.locator('#ranking-progress').innerText(), /Preparing to check saved rankings/);
    // The server withholds the previous journal when a new attempt has not reported yet.
    ops.ranking.current_pass = null;
    await refresh(page);
    assert.equal(await page.locator('#ranking-progress progress').count(), 0);
    assert.doesNotMatch(await page.locator('#ranking-progress').innerText(), /Checked 3,000|this check:/);
    const reusedPass = {status:'succeeded',checked_families:0,total_families:6700,reused:true,
      policies:{selective:{recomputed_families:0,reused_families:6700},broad:{recomputed_families:0,reused_families:6700}}};
    for (const state of ['running','idle']) {
      ops.ranking.state = state;
      ops.ranking.current_pass = reusedPass;
      ops.ranking.last_pass = reusedPass;
      await refresh(page);
      assert.equal(await page.locator('#ranking-progress progress').count(), 0);
      assert.match(await page.locator('#ranking-progress').innerText(), state === 'idle'
        ? /Last check: saved rankings were current/ : /Saved rankings are already current/);
      assert.match(await page.locator('#ranking-progress').innerText(), /0 rankings recomputed · 6,700 saved rankings reused/);
      assert.doesNotMatch(await page.locator('#ranking-progress').innerText(), /Checked .* of|100%|Current pass/);
    }
    ops.ranking.state = 'queued';
    await refresh(page);
    assert.equal(await page.locator('#ranking-progress progress').count(), 0);
    assert.match(await page.locator('#ranking-progress').innerText(), /Ranking update check queued/);
    assert.doesNotMatch(await page.locator('#ranking-progress').innerText(), /already current|were current|this check:|last check:/);
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    ops.ranking.state = 'running';
  }
  assert.equal(posts.length, progressPosts, 'Viewing ranking progress must not start work');
  report.checks.push('Ranking progress separates checked families from recomputed/reused rankings, suppresses stale and queued counts, and never invents a percentage for fast reuse on mobile or desktop.');
  await page.locator('#scan-now').click();
  await notice(page, 'Scan queued');
  assert.notEqual(posts.at(-1).body.idempotency_key, firstScan.body.idempotency_key);
  ops.collection = {available:false,active:null,reason:'Job collection is paused.'};
  await refresh(page);
  assert.equal(await page.locator('#scan-now').isDisabled(), true);
  assert.match(await page.locator('#scan-status').innerText(), /paused/);
  const preservedCheck = page.locator('#notification-list input[type="checkbox"]').first();
  await preservedCheck.check();
  await page.evaluate(() => refreshPipeline());
  assert.equal(await preservedCheck.isChecked(), true);
  await page.setViewportSize({width:390,height:844});
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await page.screenshot({path:path.join(output,'scan-progress-mobile.png')});
  report.checks.push('Scan requests retain their key after uncertainty, disable duplicate clicks, respect paused collection and show per-policy coverage and allowance waits.');
  ops.costs = {available:true,generated_at:now,next_refresh_due_at:'2026-09-21T16:00:00Z',providers:[
    {id:'aws',status:'ok',stale:false,observed_at:now,attempted_at:now,period:{start:'2026-09-01',end:'2026-09-20'},estimated:true,
      metrics:{charges:'8.50',credits:'-8.00',refunds:'0.00',net:'0.50'},metric_labels:{charges:'Charges before credits / refunds',credits:'Credit adjustments',refunds:'Refund adjustments',net:'Net reported cost'}},
    {id:'runpod',status:'error',stale:true,observed_at:'2026-09-18T16:00:00Z',attempted_at:now,message:'The billing service could not be reached. Previous figures are retained when available.',
      metrics:{balance:'5.33',lifetime_usage:'4.67',hourly_rate:'0'},metric_labels:{balance:'Account credit balance',lifetime_usage:'Lifetime account usage',hourly_rate:'Current account rate / hour'}},
    {id:'openrouter',status:'partial',stale:false,observed_at:now,attempted_at:now,message:'Account credit balance needs an OpenRouter management key on the host. Shown usage covers only the configured inference key.',
      metrics:{key_usage:'0.014418',key_monthly_usage:'0.01'},metric_labels:{key_usage:'This key · lifetime usage',key_monthly_usage:'This key · provider-reported monthly usage'}},
  ],alerts:[{provider:'runpod',kind:'stale'},{provider:'aws',kind:'threshold',direction:'above',threshold:'5',stale:false}]};
  const postCountBeforeCosts = posts.length;
  await refresh(page);
  assert.equal(await page.locator('.costs-card').count(),3);
  assert.match(await page.locator('[data-provider="aws"]').innerText(),/Net reported cost[\s\S]*\$0.50/);
  assert.match(await page.locator('[data-provider="aws"]').innerText(),/not remaining promotional credit/);
  assert.match(await page.locator('[data-provider="runpod"]').innerText(),/Stale figures/);
  assert.match(await page.locator('[data-provider="runpod"]').innerText(),/last successful figures, not current totals/);
  assert.match(await page.locator('[data-provider="openrouter"]').innerText(),/Partial coverage/);
  assert.match(await page.locator('[data-provider="openrouter"]').innerText(),/\$0.0144/);
  assert.equal(await page.locator('[data-provider="openrouter"] .costs-metrics dt').filter({hasText:'Account credit balance'}).count(),0);
  assert.match(await page.locator('#costs-alerts').innerText(),/\$5.00 warning threshold/);
  assert.match(await page.locator('#costs-section').innerText(),/not spending caps/);
  assert.equal(posts.length,postCountBeforeCosts);
  await page.locator('#costs-section').scrollIntoViewIfNeeded();
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await page.locator('#costs-section').screenshot({path:path.join(output,'costs-mobile.png')});
  await page.setViewportSize({width:1365,height:1000});
  await page.locator('#costs-section').scrollIntoViewIfNeeded();
  await page.locator('#costs-section').screenshot({path:path.join(output,'costs-desktop.png')});
  ops.costs = {available:false,message:'The host has not published a cost snapshot yet.'};
  await refresh(page);
  assert.equal(await page.locator('.costs-card').count(),0);
  assert.match(await page.locator('#costs-status').innerText(),/has not published/);
  assert.equal(await page.locator('#costs-alerts').innerText(),'');
  report.checks.push('Cost cards distinguish AWS signed adjustments, prepaid balances and key usage; stale figures and threshold warnings remain explicit, refresh is read-only, and missing snapshots never show zero.');
  ops.health = {application_backend:'owners',status:'healthy',applications:{preparing:1},pending_reviews:2,
    reminders:{counts:{pending:1},recovery:{restore_quarantined_reminders:1}},schedules:{pending:1},
    actions:{counts:{},readiness:{uncertain:2,execution_counts:{failed:1}}},work:{counts:{pending:3}},application_restore:{required:true},
    application_coverage:{work_truncated:true}};
  ops.reminders = {counts:{pending:1},items:[{reminder_id:'reminder:one',application_id:'app:one',note:'Reviewed reminder',due_at:now,status:'pending'}]};
  await refresh(page);
  assert.match(await page.locator('#health-detail').textContent(), /Pending application work/);
  assert.match(await page.locator('#health-detail').textContent(), /Some totals show a bounded page/);
  const metric = name => page.locator('#health-detail .metric').filter({has:page.locator('.meta',{hasText:name})}).locator('strong');
  assert.equal(await metric('Unresolved external outcomes').textContent(),'2');
  assert.equal(await metric('Failed external actions').textContent(),'1');
  assert.equal(await metric('Quarantined reminders').textContent(),'1');
  assert.equal(await metric('Restore review required').textContent(),'Yes');
  assert.doesNotMatch(await page.locator('#health-detail').textContent(), /Projection errors|Pending outbox/);
  const ownerReminder = page.locator('#reminder-list');
  assert.match(await ownerReminder.textContent(), /Reviewed reminder/);
  assert.equal(await ownerReminder.getByRole('link',{name:'Open application',exact:true,includeHidden:true}).getAttribute('href'),'#applications/app%3Aone/overview');
  assert.equal(await ownerReminder.getByRole('button',{name:'Cancel',exact:true}).count(),0);
  ops.health.application_restore.required=false;
  ops.health.reminders.recovery.restore_quarantined_reminders=0;
  await refresh(page);
  assert.doesNotMatch(await page.locator('#health-detail').textContent(),/Quarantined reminders|Restore review required/);
  report.checks.push('Owner health renders its own bounded work and action counts without retired projections, and reminders link to owner review.');
  await page.evaluate(() => {
    state.applicationBackend = 'owners';
    state.applications = [{application_id:'app:tracking',current_phase:'preparing',ats:'ashby',title_snapshot:'Tracked role',employer_snapshot:'Fictional company'}];
    consoleState.applicationId = 'app:tracking';
    consoleState.workspace = {application:state.applications[0], review:{items:[{id:'proposal:tracking',operation:'add_note',blockers:[]}]}};
    renderStoredRecords();
    renderApplicationReviewNotices();
  });
  assert.equal(await page.evaluate(() => stageLabel('preparing')), 'Tracking');
  assert.equal(await page.evaluate(() => applicationScopeRows().length),1);
  assert.doesNotMatch(await page.locator('#stored-records-list').textContent(), /Read-only draft/);
  assert.equal(await page.locator('#stored-records-list a').getAttribute('href'),'#applications/app%3Atracking/overview');
  assert.doesNotMatch(await page.locator('#workspace-review-notices').textContent(), /historical record|read-only/);
  assert.equal(await page.locator('#workspace-review-notices a').getAttribute('href'),'#review/proposal/proposal%3Atracking?application=app%3Atracking');
  await page.evaluate(() => loadSettings());
  assert.equal(await page.locator('#settings a[href="#settings/chief"]').evaluate(link => link.hidden),true);
  assert.equal(await page.locator('#settings-home a[href="#applications"]').textContent(),'Tracked applicationsOpen notes, tasks, evidence, and application reviews.→');
  assert.doesNotMatch(await page.evaluate(() => readableResumeReason('application_not_preparing')),/left preparation/);
  report.checks.push('Owner tracking records remain active applications in legacy document entry points and link to current state and review.');

  assert.deepEqual(errors, []);
  report.passed = true;
} catch (error) {
  report.error = error.stack || String(error);
  report.browserErrors = errors;
  process.exitCode = 1;
  console.error(report.error);
} finally {
  await browser.close();
  await new Promise((resolve) => server.close(resolve));
  await writeFile(path.join(output, 'results.json'), JSON.stringify(report, null, 2) + '\n');
}
if (report.passed) console.log(`ok (${report.checks.length} Ops browser scenarios); ${path.join(output, 'results.json')}`);
