// Focused owner detail rendering: no provider requests or legacy mutations.
import assert from 'node:assert/strict';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
import {chromium} from '../../extension/node_modules/playwright-core/index.mjs';
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const browser = await chromium.launch({headless: true});
try {
  const page = await browser.newPage(), errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.setContent('<section id="applications"></section>');
  await page.evaluate(() => {
    window.state = {applicationBackend: 'owners'};
    window.consoleState = {applicationId: 'app:one'};
    window.node = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; };
    window.stageLabel = value => String(value || '').replaceAll('_', ' ');
    window.displayDate = value => value || 'Time unknown';
    window.key = () => 'fixture-' + (++window.counter); window.counter = 0;
    window.calls = []; window.failNext = false;
    window.loadApplications = async () => {};
    window.api = async (url, options = {}) => {
      window.calls.push({url, ...options});
      if (window.failNext) { window.failNext = false; throw Error('Response unavailable; retry.'); }
      if (url.includes('/conversation/')) return {available: true, excerpt: 'Exact e\u0301\n<script>unsafe()</script>', coverage: {complete: false}};
      if (url.includes('workspace-page')) return {items: [{id: 'task:two', description: 'Second task', status: 'open', version: 9, pursuit_no: 1}]};
      if (url.includes('closure-preview')) return {expected_version: 7, expected_records: {tasks: {'task:one': 2}}, records: [{}]};
      return {};
    };
    window.fixture = {
      application: {application_id: 'app:one', version: 7, disposition: 'open', pursuit_no: 1}, paused: true,
      records: {tasks: {items: [{id: 'task:one', description: 'Reply to recruiter', status: 'open', version: 2, pursuit_no: 1}], next_cursor: 'next-task'},
        notes: {items: [{text: 'Exact e\u0301\n<script>note</script>', created_at: '2026-10-09'}]},
        interviews: {items: [{title: 'Technical discussion', status: 'scheduled', start_at: '2026-10-10T13:00:00Z', end_at: '2026-10-10T14:00:00Z', timezone: 'America/Chicago'}]},
        reminders: {items: [{status: 'pending', kind: 'interview', at: '2026-10-10T12:00:00Z', next_notification_at: '2026-10-10T12:30:00Z'}]},
        schedules: {items: [{status: 'pending', operation: 'create_task', input: {description: 'Follow up next week'}, due_at: '2026-10-17T12:00:00Z'}]},
        progress: {items: [{status: 'active', kind: 'recruiter_contact'}]},
        offers: {items: [{status: 'offered', terms: {salary: '$100,000', remote: false}}]}},
      review: {items: [{id: 'proposal:one', operation: 'record_interview', blockers: []}]},
      conversation: {items: [{id: 'message:one', direction: 'incoming', occurred_at: '2026-10-09'}]},
      actions: {items: [{action_id: 'action:one', authorization: 'authorized', execution: 'uncertain', envelope: {kind: 'send_reply'}}]},
      results: {items: [{delivery: 'conflict', conflict_reason: 'Task changed.'}]},
      analysis_coverage: {items: [{analysis_id: 'analysis:one', status: 'failed', failure_code: 'source_unavailable', coverage: {complete: false}, recorded_at: '2026-10-09'}], truncated: true},
    };
  });
  await page.addScriptTag({path: path.join(root, 'job_search/web/applications-view.js')});
  await page.addScriptTag({path: path.join(root, 'job_search/web/owner-application-view.js')});
  await page.evaluate(() => {
    document.querySelector('#applications').classList.add('record-open');
    document.querySelector('#application-workspace').hidden = false;
    renderOwnerApplicationWorkspace(window.fixture); renderOwnerApplicationReviews(window.fixture);
  });
  assert.equal(await page.locator('#owner-notes p.answer-value').textContent(), 'Exact e\u0301\n<script>note</script>');
  assert.equal(await page.locator('#owner-notes script').count(), 0);
  assert.match(await page.locator('#owner-records').textContent(), /2026-10-10T13:00:00Z – 2026-10-10T14:00:00Z · America\/Chicago/);
  assert.match(await page.locator('#owner-records').textContent(), /Notification 2026-10-10T12:30:00Z/);
  assert.match(await page.locator('#owner-records').textContent(), /Follow up next week/);
  assert.match(await page.locator('#owner-records').textContent(), /recruiter contact/);
  assert.match(await page.locator('#owner-records').textContent(), /\$100,000/);
  assert.match(await page.locator('#workspace-review-notices a').getAttribute('href'), /^#review\/proposal\/proposal%3Aone\?application=app%3Aone$/);
  await page.getByRole('button', {name: 'Load more tasks', exact: true}).click();
  await page.getByText('Second task', {exact: true}).waitFor();
  await page.locator('#owner-tasks').getByRole('button', {name: 'Mark complete', exact: true}).first().click();
  const complete = await page.evaluate(() => window.calls.find(call => call.url.endsWith('/complete_task')));
  assert.deepEqual(JSON.parse(complete.body), {task_id: 'task:one', expected_version: 2, reason: 'Recorded by user'});
  assert(complete.headers['Idempotency-Key']);
  await page.locator('#owner-notes summary').click();
  await page.getByLabel('Add a note', {exact: true}).fill('A new exact e\u0301 note');
  await page.evaluate(() => { window.failNext = true; });
  await page.getByRole('button', {name: 'Add a note', exact: true}).click();
  await page.getByText('Response unavailable; retry.', {exact: true}).waitFor();
  await page.getByRole('button', {name: 'Add a note', exact: true}).click();
  const notes = await page.evaluate(() => window.calls.filter(call => call.url.endsWith('/add_note')));
  assert.equal(notes.length, 2); assert.equal(notes[0].headers['Idempotency-Key'], notes[1].headers['Idempotency-Key']);
  assert.equal(JSON.parse(notes[1].body).text, 'A new exact e\u0301 note');
  await page.getByRole('button', {name: 'Stop pursuing', exact: true}).click();
  await page.getByRole('button', {name: 'Confirm stop pursuing', exact: true}).click();
  const close = await page.evaluate(() => JSON.parse(window.calls.find(call => call.url.endsWith('/close_application')).body));
  assert.equal(close.expected_version, 7); assert.deepEqual(close.expected_records, {tasks: {'task:one': 2}});
  await page.evaluate(() => { document.querySelector('#workspace-overview').hidden = true; document.querySelector('#workspace-messages').hidden = false; });
  await page.getByText('Evidence processing needs attention. source unavailable. Recorded 2026-10-09.', {exact: true}).waitFor();
  await page.getByText('Showing recent evidence processing history.', {exact: true}).waitFor();
  await page.getByRole('button', {name: 'View message', exact: true}).click();
  assert.equal(await page.locator('.message-body').textContent(), 'Exact e\u0301\n<script>unsafe()</script>');
  assert.equal(await page.locator('.message-body script').count(), 0);
  await page.evaluate(() => {
    renderApplicationAnswers([{captured_at: '2026-10-09', review_status: 'unreviewed', snapshot: {fields: [
      {prompt: 'Exact answer', value: 'e\u0301\n<script>answer</script>', control: 'textarea'},
      {prompt: 'Years', value: 0, control: 'number'}, {prompt: 'Checked', value: false, control: 'checkbox'},
    ]}}, {review_status: 'unreviewed', snapshot: {unstructured: ['first', true]}}]);
    document.querySelector('#workspace-messages').hidden = true; document.querySelector('#workspace-answers').hidden = false;
  });
  assert.equal(await page.locator('.answer-value').filter({hasText: 'e\u0301\n<script>answer</script>'}).count(), 1);
  assert.equal(await page.getByText('Preserved browser capture; its link to a submitted application has not been reviewed.', {exact: true}).count(), 2);
  await page.getByText('Not selected', {exact: true}).waitFor();
  await page.getByText('0', {exact: true}).waitFor();
  assert.equal(await page.locator('#workspace-answers script').count(), 0);
  assert.equal((await page.evaluate(() => window.calls)).some(call => call.url.includes('/lifecycle/')), false);
  assert.deepEqual(errors, []);
  console.log('ok (owner application detail, exact commands, paging, safe correspondence)');
} finally { await browser.close(); }
