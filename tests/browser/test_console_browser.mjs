import assert from 'node:assert/strict';
import {startDemo, runFixturePython} from './demo-process.mjs';
import {mkdtemp, mkdir, readFile, writeFile, rename} from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {randomUUID} from 'node:crypto';
import {chromium} from '../../extension/node_modules/playwright-core/index.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const stateDir = await mkdtemp(path.join(os.tmpdir(), 'career-console-'));
const output = path.resolve(process.env.CONSOLE_OUTPUT || '.cache/console-browser');
await mkdir(output, {recursive:true});
const record = process.env.CONSOLE_RECORD === '1';
const viewport = {width: Number(process.env.CONSOLE_WIDTH || 1440), height: Number(process.env.CONSOLE_HEIGHT || 900)};
const pace = async (page, ms=5500) => { if(record) await page.waitForTimeout(ms); };
const demoSession = await startDemo({root, stateDir});
const demo = demoSession.process;
const url = demoSession.url;
const browser = await chromium.launch({channel:'chromium',headless:true});
const context = await browser.newContext({viewport, reducedMotion:'reduce', ...(record ? {recordVideo:{dir:output,size:viewport}}: {})});
await context.route('**/*', route=>route.request().url().startsWith(url) || route.request().url().startsWith('data:') ? route.continue() : route.abort());
const captureDark = process.env.CONSOLE_THEME === 'dark';
if (captureDark) await context.addInitScript(() => {
  if (!localStorage.getItem('career-platform:theme')) localStorage.setItem('career-platform:theme', 'dark');
});
const recordingStarted=Date.now();
const page = await context.newPage();
const pageErrors=[]; page.on('pageerror',error=>pageErrors.push(error.message));
const report={passed:false, checks:[], fixture:'Real dashboard, ledger, workers and MCP; fictional records and external responses.', screenshots:[]};
async function snapshot(name) {
  if (/^#applications\/[^/]+/.test(new URL(page.url()).hash) && name !== 'failure') {
    await page.waitForFunction(() => consoleState.workspace?.application.application_id === decodeURIComponent(location.hash.split('/')[1])
      && !document.querySelector('#application-workspace').hasAttribute('aria-busy'));
  }
  await page.evaluate(() => scrollTo(0, 0));
  await page.screenshot({path:path.join(output,name+'.png')}); report.screenshots.push(name+'.png');
}
async function advance(step) {
  const id=randomUUID();
  const temporary=path.join(stateDir,`demo-command-${id}.tmp`);
  await writeFile(temporary,JSON.stringify({id,step}));
  await rename(temporary,path.join(stateDir,'demo-command.json'));
  const started=Date.now();
  while(Date.now()-started<15000) {
    const status=JSON.parse(await readFile(path.join(stateDir,'demo-status.json'),'utf8'));
    if(status.command_id===id) {assert.equal(status.status,'ready',JSON.stringify(status));return status;}
    await new Promise(resolve=>setTimeout(resolve,150));
  }
  throw new Error(`Fixture step ${step} timed out: ${JSON.stringify(demoSession.diagnostics())}`);
}
try {
  await page.goto(url); await page.locator('.application-link').first().waitFor({state:'attached'});
  assert.match(await page.title(),/Applications/);
  await page.waitForFunction(() => consoleState.initialized);
  assert.equal(await page.locator('#application-workspace').isVisible(), false);
  await page.goto(url+'/#applications');
  await page.waitForFunction(() => consoleState.initialized);
  await page.reload();
  await page.waitForFunction(() => consoleState.initialized);
  assert.equal(new URL(page.url()).hash, '#applications');
  assert.equal(await page.locator('#application-workspace').isVisible(), false);
  const refreshedList = page.waitForResponse(response => response.url().endsWith('/api/v1/applications'));
  await page.locator('#refresh-applications').click();
  await (await refreshedList).finished();
  assert.equal(new URL(page.url()).hash, '#applications');
  assert.equal(await page.locator('#application-workspace').isVisible(), false);
  assert.equal(await page.locator('#application-scope').count(), 0);
  assert.equal(await page.locator('#application-list .application-link').count(), 1);
  assert.equal(await page.locator('#application-count').innerText(), '1 application');
  assert.deepEqual(await page.locator('.sidebar a').allTextContents(), ['Applications', 'Shortlist', 'Review', 'Settings']);
  assert(!/Waypoint/.test(await page.locator('#application-list').innerText()));
  await page.fill('#application-search', 'Cedar');
  await page.selectOption('#application-phase', 'awaiting_confirmation');
  await page.locator('#application-list .application-link').first().click();
  await page.locator('#workspace-company').filter({hasText:'Cedar Health'}).waitFor();
  const cedarRecordUrl = page.url();
  await page.reload();
  await page.locator('#workspace-company').filter({hasText:'Cedar Health'}).waitFor();
  assert.equal(page.url(), cedarRecordUrl);
  assert.equal(await page.locator('#job-preview').isVisible(), false);
  await page.getByRole('button', {name:'Preview job description', exact:true}).click();
  await page.locator('#job-preview').filter({hasText:'No saved job description'}).waitFor();
  assert.equal(await page.locator('#job-preview').getByRole('link', {name:'Open original posting'}).count(), 1);
  await page.getByRole('button', {name:'Close job preview'}).click();
  assert.equal(await page.locator('#application-list').isVisible(), false);
  assert.deepEqual(await page.locator('.workspace-tabs a').allTextContents(), ['Overview', 'Messages', 'Answers', 'Documents']);
  await page.locator('.workspace-back').click();
  assert.equal(await page.locator('#application-search').inputValue(), 'Cedar');
  await page.fill('#application-search', 'Cedar');
  await page.locator('#application-list .application-link').first().click();
  await page.locator('.workspace-back').click();
  assert.equal(await page.locator('#application-search').inputValue(), 'Cedar');
  await page.fill('#application-search', '');
  await page.selectOption('#application-phase', '');
  await page.locator('#application-workspace').waitFor({state:'hidden'});
  await page.locator('#refresh-applications').click();
  assert.equal(new URL(page.url()).hash, '#applications');
  assert.equal(await page.locator('#application-workspace').isVisible(), false);
  report.checks.push('Initial load and refresh keep Applications on the list; specific record reloads retain the selected record.');
  await snapshot('applications'); await pace(page);
  await page.goto(url+'/#settings/stored-records');
  const savedDraft = page.locator('#stored-records-list article').filter({hasText:'Waypoint'}).getByRole('link');
  await savedDraft.waitFor(); await savedDraft.click();
  await page.locator('#workspace-company').filter({hasText:'Waypoint'}).waitFor();
  assert.equal(await page.locator('.workspace-label').innerText(), 'Saved draft');
  assert.match(await page.locator('#workspace-review-notices').innerText(), /read-only/);
  assert.equal(await page.getByRole('button', {name:'I submitted',exact:true}).count(), 0);
  const draftId=await page.evaluate(()=>consoleState.applicationId);
  await page.goto(url+`/#applications/${draftId}/resume`);
  await page.locator('#workspace-documents').waitFor({state:'visible'});
  assert.match(await page.locator('#workspace-documents').innerText(), /No resume was recorded/);
  report.checks.push('Stored drafts remain read-only under Settings; legacy resume links open Documents without generation controls.');
  await page.locator('.sidebar a[href="#shortlist"]').click();
  await page.selectOption('#shortlist-source', 'model');
  await page.locator('.shortlist-advanced > summary').click();
  await page.selectOption('#policy','champion');
  await page.getByRole('button',{name:'Refresh shortlist'}).click();
  const role=page.locator('#shortlist-list .card').filter({hasText:'Northstar Labs'});
  await role.waitFor();
  assert.equal(await role.locator('time.posting-date').first().innerText(), 'Posted today');
  assert(await role.locator('time.posting-date').first().getAttribute('datetime'));
  const relativeDates = await page.evaluate(() => {
    const now = new Date(2026, 8, 28, 0, 5);
    return [relativePostingDate(new Date(2026, 8, 28, 0, 1).toISOString(), now),
      relativePostingDate(new Date(2026, 8, 27, 23, 55).toISOString(), now),
      relativePostingDate(new Date(2026, 8, 26, 12).toISOString(), now),
      relativePostingDate('invalid', now), relativePostingDate('2026-09-28', now)];
  });
  assert.equal(relativeDates[0], 'today'); assert.equal(relativeDates[1], 'yesterday');
  assert.match(relativeDates[2], /2026/); assert.equal(relativeDates[3], ''); assert.equal(relativeDates[4], 'today');
  await page.route('**/api/v1/shortlist', async route => {
    if (route.request().method() === 'POST') await new Promise(resolve => setTimeout(resolve, 700));
    await route.continue();
  });

  assert.match(await role.locator('.posting-dates').innerText(), /Posted /);
  await page.selectOption('#shortlist-days', '7');
  const refresh = page.waitForResponse(response => response.url().endsWith('/api/v1/shortlist') && response.request().method() === 'POST');
  await page.getByRole('button',{name:'Refresh shortlist'}).click();
  assert.equal(await page.getByRole('button', {name:'Loading shortlist…', exact:true}).isDisabled(), true);
  assert.match(await page.locator('#shortlist-loading').innerText(), /Loading saved rankings/);
  const refreshed = await refresh;
  await page.getByRole('button', {name:'Refresh shortlist', exact:true}).waitFor();
  assert.match(await page.locator('#shortlist-loading').innerText(), /jobs shown from saved rankings/);
  await page.unroute('**/api/v1/shortlist');
  report.checks.push('Prominent Today/Yesterday date labels use local calendar days; refresh shows a loading state and restores its button.');

  assert.equal(refreshed.request().postDataJSON().options.days, 7);
  assert.match(await page.locator('#shortlist-window').innerText(), /last 7 days/);
  report.checks.push('Posting dates appear on shortlisted jobs and the visible 7-day filter reaches the API.');
  const beforeBrowsing = (await (await context.request.get(url+'/api/v1/applications')).json()).applications.length;
  await role.getByRole('button', {name:'Platform Engineer'}).click();
  await page.locator('#job-preview').filter({hasText:'Kubernetes production experience'}).waitFor();
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('#job-preview').isVisible(), false);
  assert.equal(await role.getByRole('button', {name:'Platform Engineer'}).evaluate(el => el === document.activeElement), true);
  await role.getByRole('button', {name:'Platform Engineer'}).click();
  await page.locator('#job-preview .job-preview-description').waitFor();
  assert.equal(await page.locator('#job-preview h2').filter({hasText:'Requirements'}).count(), 1);
  assert.equal(await page.locator('#job-preview li').innerText(), 'Preferred: PostgreSQL experience.');
  assert.equal(await page.locator('#job-preview :is(script,img,iframe)').count(), 0);
  assert.equal(await page.evaluate(() => !!window.unsafePreview), false);
  await snapshot('job-preview');
  await page.setViewportSize({width:390,height:844});
  assert(await page.locator('#job-preview').evaluate(el => el.scrollWidth <= el.clientWidth));
  await snapshot('job-preview-mobile');
  await page.getByRole('button',{name:'Close job preview'}).click();
  await page.setViewportSize(viewport);
  let failPreview = true;
  await page.route('**/api/v1/jobs/preview?**', async route => {
    if (failPreview) { failPreview = false; await route.fulfill({status:503, contentType:'application/json', body:JSON.stringify({error:'Temporary fixture outage'})}); }
    else await route.continue();
  });
  await role.getByRole('button', {name:'Platform Engineer'}).click();
  await page.getByRole('button', {name:'Try again', exact:true}).click();
  await page.locator('#job-preview').filter({hasText:'Kubernetes production experience'}).waitFor();
  await page.getByRole('button', {name:'Close job preview'}).click();
  await page.unroute('**/api/v1/jobs/preview?**');
  let releasePreview;
  const heldPreview = new Promise(resolve => { releasePreview = resolve; });
  await page.route('**/api/v1/jobs/preview?**', async route => {
    if (new URL(route.request().url()).searchParams.get('id') === 'job-1') {
      await heldPreview;
      await route.fulfill({contentType:'application/json',body:JSON.stringify({job:{title:'Stale response'},description_html:'<p>Old result</p>'})}).catch(() => {});
    } else await route.continue();
  });
  await role.getByRole('button', {name:'Platform Engineer'}).click();
  await page.locator('#job-preview').filter({hasText:'Loading job description'}).waitFor();
  await page.getByRole('button', {name:'Close job preview'}).click();
  await page.locator('#shortlist-list .card').filter({hasText:'Harbor Systems'}).getByRole('button', {name:'Backend Engineer'}).click();
  await page.locator('#job-preview').filter({hasText:'Build TypeScript APIs'}).waitFor();
  releasePreview();
  await page.unroute('**/api/v1/jobs/preview?**');
  assert.equal(await page.locator('#job-preview-title').innerText(), 'Backend Engineer');
  await page.getByRole('button', {name:'Close job preview'}).click();
  await page.evaluate(() => {
    const dialog = document.querySelector('#job-preview');
    openJobPreview({ats:'ashby',id:'job-1',title:'Platform Engineer'});
    dialog.close();
    openJobPreview({ats:'ashby',id:'job-2',title:'Backend Engineer'});
  });
  await page.locator('#job-preview').filter({hasText:'Build TypeScript APIs'}).waitFor();
  await page.getByRole('button', {name:'Close job preview'}).click();
  report.checks.push('Shared preview handles drafts, missing descriptions, safe HTML, mobile, keyboard focus, retry, and stale requests.');
  const popupPromise=page.waitForEvent('popup');
  await role.getByRole('link',{name:'Open posting'}).click();
  const postingPopup=await popupPromise; await postingPopup.close();
  assert.equal((await (await context.request.get(url+'/api/v1/applications')).json()).applications.length, beforeBrowsing);
  report.checks.push('Applications excludes drafts by default; reading descriptions and opening postings create no application records.');
  await snapshot('shortlist'); await pace(page);
  assert.equal(await role.getByRole('button',{name:'Prepare application'}).count(), 0);
  await role.getByRole('button',{name:'Platform Engineer'}).click();
  assert.equal(await page.locator('#job-preview').getByRole('button',{name:'Prepare application'}).count(), 0);
  await page.getByRole('button',{name:'Close job preview'}).click();
  // Historical resume records are seeded through the real API. Their old
  // generation/selection controls must remain absent from the product UI.
  const applicationId = await page.evaluate(async () => {
    const job = state.shortlist.recommendations.find(job => job.company === 'Northstar Labs');
    const prepared = await api('/api/v1/resume-lab/prepare', {method:'POST', body:JSON.stringify({
      session_id:state.shortlist.session_id, impression_id:job.impression_id, idempotency_key:'browser-existing-draft'
    })});
    const id = prepared.application.application_id;
    const runId = prepared.resume_lab.run_id;
    let result;
    for (let tries=0; tries<100; tries++) {
      result = await api(`/api/v1/resume-lab/runs/${runId}/result`);
      if (result.status === 'succeeded') break;
      await new Promise(resolve=>setTimeout(resolve,200));
    }
    if (result.status !== 'succeeded') throw new Error('Fixture resume did not complete');
    const candidate = result.comparisons[0];
    await api(`/api/v1/resume-lab/runs/${runId}/approve`, {method:'POST',body:JSON.stringify({comparison_kind:'grounded_rewrite',idempotency_key:'browser-recorded-approval'})});
    await api(`/api/v1/applications/${id}/resume-selection`, {method:'POST',body:JSON.stringify({artifact_id:candidate.artifact_id,evaluation_id:candidate.evaluation_id,idempotency_key:'browser-recorded-selection'})});
    await api(`/api/v1/applications/${id}/submitted`, {method:'POST',body:JSON.stringify({resume_decision:'selected',idempotency_key:'browser-recorded-submit'})});
    return id;
  });
  await page.goto(url+`/#applications/${applicationId}/documents`);
  await page.locator('#workspace-documents').getByRole('link', {name:'View PDF'}).waitFor();
  assert.equal(await page.getByRole('button', {name:'I submitted',exact:true}).count(), 0);
  assert.equal(await page.getByRole('button', {name:'Pair autofill',exact:true}).count(), 0);
  assert.equal(await page.locator('#resumes').count(), 0);
  const artifactUrl=await page.locator('#workspace-documents').getByRole('link', {name:'View PDF'}).getAttribute('href');
  const response=await context.request.get(url+artifactUrl);
  assert.equal(response.status(),200); assert((await response.body()).subarray(0,5).equals(Buffer.from('%PDF-')));
  assert.match(response.headers()['content-disposition'], /inline/);
  const download=await context.request.get(url+await page.locator('#workspace-documents').getByRole('link', {name:'Download PDF'}).getAttribute('href'));
  assert.match(download.headers()['content-disposition'], /attachment/);
  await snapshot('recorded-documents');
  await page.locator('.workspace-tabs a[data-tab="overview"]').click();
  await page.locator('#workspace-status').filter({hasText:'Awaiting confirmation'}).waitFor();
  const applicationRole = page.locator('#application-list tr').filter({hasText:'Northstar Labs'});
  assert.match(await applicationRole.locator('.job-location').innerText(), /Chicago, IL/);
  assert.equal(await applicationRole.locator('.job-employment').innerText(), 'Full Time');
  assert.equal(await page.locator('#workspace-job-preview').getByRole('link', {name:'Open posting'}).getAttribute('href'), 'https://example.test/jobs/1');
  await page.locator('.workspace-tabs a[data-tab="answers"]').click();
  await page.locator('#workspace-answers').getByText(/No answers were captured/).waitFor();
  await page.locator('.workspace-tabs a[data-tab="overview"]').click();
  report.checks.push('Dedicated record provides Overview, Messages, Answers and Documents; old records have an honest empty answer state and PDFs remain readable/downloadable.');
  const submittedAt = await page.evaluate(() => consoleState.workspace.application.submitted_at);
  assert(submittedAt);
  assert.equal(await applicationRole.locator('time.applied-date').getAttribute('datetime'), submittedAt);
  assert.match(await applicationRole.locator('.applied-date').innerText(), /^Applied /);
  const recordDates = page.locator('#workspace-posting-dates .posting-dates');
  assert.match(await recordDates.innerText(), /Posted .+ · Applied /);
  assert.equal(await recordDates.locator('time').filter({hasText:'Applied'}).getAttribute('datetime'), submittedAt);
  assert.equal(await page.locator('#application-workspace .applied-date').count(), 0);
  assert.equal(await page.locator('#application-list').getByRole('link', {name:'Open posting'}).count(), 0);
  await snapshot('submitted'); await pace(page);
  runFixturePython(['-c', `
import sqlite3,sys
from job_search.collection.boards import _prepare
with sqlite3.connect(sys.argv[1]) as con:
    _prepare(con)
    con.execute("UPDATE jobs SET location='Remote US',last_seen='2026-09-28T12:00:00Z' WHERE id='job-1'")
    con.execute("UPDATE jobs SET closed_at='2026-09-28T13:00:00Z' WHERE id='job-1'")
    con.execute("UPDATE jobs SET closed_at=NULL,last_seen='2026-09-28T14:00:00Z' WHERE id='job-1'")
`, path.join(stateDir, 'jobs.db')], {cwd:root});
  await page.goto(url+`/#applications/${applicationId}/job-history`);
  await page.getByRole('heading', {name:'Job posting history',exact:true}).waitFor();
  await page.getByRole('heading', {name:'Posting reopened',exact:true}).waitFor();
  assert.match(await page.locator('#workspace-job-history').innerText(), /Posting closed/);
  assert.match(await page.locator('#workspace-job-history').innerText(), /Posting modified/);
  assert.match(await page.locator('#workspace-posting-dates').innerText(), /Posted /);
  await page.locator('#workspace-job-history summary').first().click();
  assert.match(await page.locator('#workspace-job-history').innerText(), /Chicago, IL → Remote US/);
  assert.match(await page.locator('#workspace-status').innerText(), /Awaiting confirmation/);
  await page.reload();
  await page.locator('#workspace-posting-history > summary').click();
  await page.getByRole('heading', {name:'Posting reopened',exact:true}).waitFor();
  await snapshot('job-history');
  report.checks.push('Job history shows observed modifications, closures and reopenings without changing application status, and its legacy route opens the expandable overview section.');

  await advance('mail');
  await page.reload(); await page.locator('.sidebar a[href="#review"]').click();
  await page.locator('#attention-list').filter({hasText:'schedule a conversation'}).waitFor();
  assert.equal(await page.locator('#review-count').innerText(), '1');
  await page.locator('.sidebar a[href="#applications"]').click();
  await page.locator('#application-needs-review').check();
  assert.equal(await page.locator('#application-list .application-link').count(), 1);
  await page.locator('#application-needs-review').uncheck();
  await applicationRole.getByRole('link', {name:'Interview request · Needs review',exact:true}).click();
  await page.locator('#attention-list').filter({hasText:'schedule a conversation'}).waitFor();
  await snapshot('review'); await pace(page);
  await page.locator('#attention-list').getByRole('button',{name:'Confirm update',exact:true}).click();
  await page.waitForFunction(()=>consoleState.reviews.length===0);
  await page.locator('#review-count').filter({hasText:/^$/}).waitFor({state:'attached'});
  await applicationRole.locator('.pending-note').waitFor({state:'detached'});
  assert.equal(await page.locator('#review-count').innerText(), '');
  assert.equal(await applicationRole.locator('.pending-note').count(), 0);
  await page.goto(url+`/#applications/${applicationId}/messages`);
  await page.locator('.message-body').filter({hasText:'schedule a conversation'}).waitFor();
  await snapshot('messages'); await pace(page);
  await advance('reply');
  await page.reload();
  await page.goto(url+`/#applications/${applicationId}/actions`);
  await page.locator('#attention-list .message-body').filter({hasText:'Hi Morgan'}).waitFor();
  assert.equal(await page.locator('#review-count').innerText(), '1');
  assert.match(await applicationRole.locator('.pending-note').textContent(), /Reply draft · Needs (approval|review)/);
  await snapshot('reply'); await pace(page);
  await page.locator('#attention-list').getByRole('button',{name:'Create Outlook draft'}).click();
  await page.waitForFunction(()=>consoleState.actions.some(action=>action.status==='approved'));
  await page.locator('#review-count').filter({hasText:/^$/}).waitFor({state:'attached'});
  await applicationRole.locator('.pending-note').waitFor({state:'detached'});
  assert.equal(await page.locator('#review-count').innerText(), '');
  assert.equal(await applicationRole.locator('.pending-note').count(), 0);
  const executed=await advance('execute'); assert.equal(executed.drafts_created,1);
  await advance('execute');
  const replay=JSON.parse(await readFile(path.join(stateDir,'demo-status.json'),'utf8')); assert.equal(replay.drafts_created,1);
  await page.reload(); await page.locator('#review-history > summary').click(); await page.locator('#action-list .phase').filter({hasText:'executed'}).waitFor();
  report.checks.push('Recruiter evidence review, MCP reply proposal, exact approval and one draft despite replay.');
  report.checks.push('Only Review has an action count; application cards link pending interview reviews and reply approvals to Review, and clear resolved notices.');
  await snapshot('action-completed'); await pace(page);
  await page.goto(url+`/#applications/${applicationId}/overview`);
  await page.locator('#timeline').filter({hasText:'interview requested'}).waitFor();
  await snapshot('application-history'); await pace(page);
  report.walkthrough_seconds=(Date.now()-recordingStarted)/1000;
  if (captureDark) await page.getByRole('button',{name:'Dark mode',exact:true}).click();
  await page.reload(); await page.locator('#workspace-status').filter({hasText:'Interviewing'}).waitFor();
  await page.locator('.workspace-tabs a[data-tab="messages"]').click();
  await page.goBack(); await page.locator('#workspace-overview').waitFor({state:'visible'});
  report.checks.push('Application and tab context survives reload and browser Back.');
  // Delay one application response, then switch: old data must never replace the new selection.
  const links=await page.locator('.application-link').evaluateAll(items=>items.map(item=>item.getAttribute('href')));
  const other=links.find(link=>!link.includes(applicationId));
  await page.route(`**/applications/${applicationId}/workspace`,async route=>{await new Promise(resolve=>setTimeout(resolve,500));await route.continue();});
  await page.goto(url+`/#applications/${applicationId}/overview`);
  await page.locator('.workspace-back').click();
  await page.locator(`.application-link[href="${other}"]`).click();
  await page.waitForTimeout(900);
  assert.notEqual(await page.locator('#workspace-company').innerText(),'Northstar Labs');
  await page.unroute(`**/applications/${applicationId}/workspace`);
  report.checks.push('A delayed application response cannot replace a newer selection.');
  await page.locator('.workspace-back').click();
  await page.fill('#application-search','no matching application');
  await page.getByText('No applications match these filters.').waitFor();
  await page.fill('#application-search','');
  // Failed mail has real recovery controls, not an inert review card.
  runFixturePython(['-c', `
from pathlib import Path
from dataclasses import replace
from job_search.outlook.state import SQLiteOutlookState
from tests.test_job_search_sync import change
import sys
state=SQLiteOutlookState(Path(sys.argv[1])/'applications.db')
for name in ['retry','dismiss']:
 state.stage_changes('review-test','inbox',[replace(change('review-'+name),subject='Mail failure '+name,web_link='https://outlook.live.com/mail/0/inbox/id/fixture')],query_version=2)
 state.mark_message('review-test','inbox','review-'+name,'failed','evidence quote and span do not match sanitized mail',query_version=2)
`, stateDir], {cwd:root});
  await page.goto(url+'/#review');
  await page.locator('#refresh-attention').click();
  const failedMail = page.locator('#attention-list article').filter({hasText:'Mail failure retry'});
  await failedMail.getByRole('button',{name:'Retry processing',exact:true}).waitFor();
  assert.equal(await failedMail.getByRole('link',{name:'Open in Outlook'}).getAttribute('href'),'https://outlook.live.com/mail/0/inbox/id/fixture');
  await failedMail.getByText('Technical details',{exact:true}).click();
  await failedMail.getByText('evidence quote and span do not match sanitized mail',{exact:true}).waitFor({state:'visible'});
  await snapshot('mail-failure-review');
  const retryResponse = page.waitForResponse(response => response.url().endsWith('/api/v1/mail/failures/resolve'));
  await failedMail.getByRole('button',{name:'Retry processing',exact:true}).click();
  const retryResult = await retryResponse;
  assert.equal(retryResult.status(), 200, await retryResult.text());
  await failedMail.waitFor({state:'detached'});
  const dismissedMail = page.locator('#attention-list article').filter({hasText:'Mail failure dismiss'});
  await dismissedMail.getByRole('button',{name:'Dismiss',exact:true}).click();
  await dismissedMail.waitFor({state:'detached'});
  const mailStates=JSON.parse(runFixturePython(['-c', "import sqlite3,json,sys; c=sqlite3.connect(sys.argv[1]); print(json.dumps(c.execute(\"SELECT immutable_message_id,processing_status FROM outlook_message_stage WHERE account_id='review-test' ORDER BY immutable_message_id\").fetchall()))",path.join(stateDir,'applications.db')],{encoding:'utf8'}));
  assert.deepEqual(mailStates,[['review-dismiss','ignored'],['review-retry','pending']]);
  report.checks.push('Failed-mail review displays evidence errors and Outlook links; Retry and Dismiss update the exact staged messages through the real API.');
  // Light remains the default, independent of the operating system's appearance.
  await page.emulateMedia({colorScheme:'dark'});
  assert.equal(await page.locator('html').getAttribute('data-theme'),'light');
  const toggle=page.getByRole('button',{name:'Dark mode',exact:true});
  assert.equal((await toggle.innerText()).trim(), '');
  assert.equal(await toggle.locator('.theme-moon').isVisible(), true);
  assert.equal(await toggle.locator('.theme-sun').isVisible(), false);
  assert.equal(await toggle.getAttribute('title'), 'Switch to dark mode');
  await toggle.focus(); await page.keyboard.press('Space');
  assert.equal(await toggle.getAttribute('aria-pressed'),'true');
  assert.equal(await toggle.locator('.theme-moon').isVisible(), false);
  assert.equal(await toggle.locator('.theme-sun').isVisible(), true);
  assert.equal(await toggle.getAttribute('title'), 'Switch to light mode');
  assert.equal(await page.evaluate(()=>getComputedStyle(document.documentElement).colorScheme),'dark');
  await page.reload(); await page.waitForFunction(() => consoleState.initialized);
  assert.equal(await page.locator('html').getAttribute('data-theme'),'dark');
  assert.equal(await page.evaluate(()=>localStorage.getItem('career-platform:theme')),'dark');
  await snapshot('dark-applications');
  for(const hash of ['#review','#shortlist','#career','#ops','#settings']) {
    await page.goto(url+'/'+hash); await page.waitForTimeout(250);
    assert.equal(await page.locator('html').getAttribute('data-theme'),'dark');
    await snapshot('dark-'+hash.slice(1));
  }
  // Preference updates propagate to other open tabs.
  const secondPage=await context.newPage(); await secondPage.goto(url);
  await secondPage.getByRole('button',{name:'Dark mode',exact:true}).click();
  await page.waitForFunction(()=>document.documentElement.dataset.theme==='light');
  await secondPage.close();
  await toggle.click();
  report.checks.push('Keyboard theme toggle, reload persistence, navigation and cross-tab preference updates.');
  for(const width of [1440,1280,768,390]) {
    await page.setViewportSize({width,height:900});
    for (const theme of ['light','dark']) {
      if (await page.locator('html').getAttribute('data-theme') !== theme) await toggle.click();
      for(const hash of [`#applications/${applicationId}/messages`,'#review','#shortlist','#career','#ops','#settings']) {
        await page.goto(url+'/'+hash); await page.waitForTimeout(200);
        assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth), `overflow at ${width} ${theme} ${hash}`);
      }
    }
    await page.goto(url+`/#applications/${applicationId}/messages`); await page.locator('#workspace-messages .message-body').waitFor(); await snapshot(`mobile-${width}`);
    assert.equal(await page.locator('#application-list').isVisible(), false);
    await page.locator('.workspace-back').click();
    await page.locator('#application-search').waitFor();
    assert.equal(await page.locator('#application-workspace').isVisible(), false);
    await page.locator(`.application-link[href="#applications/${applicationId}/overview"]`).click();
    await page.locator('#workspace-overview').waitFor({state:'visible'});
    await page.goBack(); await page.locator('#application-search').waitFor();
  }
  report.checks.push('Search, dedicated records, navigation and all primary/nested views fit 390, 768, 1280 and 1440px.');
  await toggle.click(); await page.reload();
  assert.equal(await page.locator('html').getAttribute('data-theme'),'light');
  // Disabling browser storage must not disable theme selection or the application.
  const restricted=await browser.newContext();
  await restricted.addInitScript(()=>Object.defineProperty(window,'localStorage',{get(){throw new DOMException('Blocked','SecurityError');}}));
  const restrictedPage=await restricted.newPage();
  restrictedPage.on('pageerror',error=>pageErrors.push(error.message));
  await restrictedPage.goto(url);
  await restrictedPage.getByRole('button',{name:'Dark mode',exact:true}).click();
  assert.equal(await restrictedPage.locator('html').getAttribute('data-theme'),'dark');
  await restricted.close();
  report.checks.push('Light preference persists and theme selection works with browser storage blocked.');
  // Publish over the real authenticated MCP boundary while the dashboard is running.
  await page.goto(url+'/#shortlist');
  await advance('curated');
  await page.selectOption('#shortlist-source', 'curated');
  await page.getByRole('button',{name:'Refresh saved lists'}).click();
  await page.locator('.curated-explanation').first().waitFor();
  assert.equal(await page.locator('#shortlist-source').inputValue(), 'curated');
  assert.equal(await page.locator('#shortlist-form').isVisible(), false);
  assert.match(await page.locator('.curated-explanation').first().innerText(), /<script>unsafe/);
  assert.equal(await page.locator('.curated-explanation script').count(), 0);
  const firstList = (await page.locator('#curated-list option').all()).length > 1
    ? await page.locator('#curated-list option').nth(1).getAttribute('value') : null;
  assert(firstList);
  const savedCards=page.locator('#shortlist-list .card');
  assert.equal(await savedCards.count(), 2);
  assert.equal(await savedCards.nth(1).getByRole('link',{name:'View application'}).count(), 1);
  assert(!/Ranking score/.test(await page.locator('#shortlist-list').innerText()));
  // Date sorting must be independent of publication/ranking and never invent dates.
  // Isolate the temporary date fixture from in-flight loads and window-focus refresh.
  // Reload below restores normal routing and verifies preference persistence.
  await page.evaluate(() => { ++shortlistLoadEpoch; consoleState.view = 'sort-fixture'; });
  const dateOrders = await page.evaluate(() => {
    const jobs = [
      {id:'missing', first_seen:'2026-10-01T00:00:00Z'},
      {id:'new', posted_at:'2026-09-30T00:00:00Z', source_updated_at:'2026-10-01T00:00:00Z'},
      {id:'old', ats:'ashby', publishedAt:'2026-09-28T00:00:00Z'},
      {id:'tie', posted_at:'2026-09-29T19:00:00-05:00', source_updated_at:'invalid'},
      {id:'legacy', ats:'greenhouse', publishedAt:'2026-09-30T12:00:00Z'},
      {id:'updated', source_updated_at:'2026-09-29T00:00:00Z'},
    ];
    return Object.fromEntries(['original','posted-newest','posted-oldest','updated-newest','updated-oldest'].map(order =>
      [order, sortedShortlistJobs(jobs, order).map(job=>job.id)]));
  });
  assert.deepEqual(dateOrders['posted-newest'], ['new','tie','old','missing','legacy','updated']);
  assert.deepEqual(dateOrders['posted-oldest'], ['old','new','tie','missing','legacy','updated']);
  assert.deepEqual(dateOrders['updated-newest'], ['new','updated','missing','old','tie','legacy']);
  assert.deepEqual(dateOrders['updated-oldest'], ['updated','new','missing','old','tie','legacy']);
  assert.deepEqual(dateOrders.original, ['missing','new','old','tie','legacy','updated']);
  for (const source of ['curated','model']) {
    const originalRanks = await page.evaluate(source => {
      const result = {...state.shortlist, source, recommendations:state.shortlist.recommendations.map((job,index)=> {
        const posted_at = index ? '2026-09-30T00:00:00Z' : '2026-09-28T00:00:00Z';
        return {...job, posted_at, ...(job.job_posting ? {job_posting:{...job.job_posting, posted_at}} : {})};
      })};
      renderShortlist(result);
      return result.recommendations.map(job=>String(job.rank).padStart(2,'0'));
    }, source);
    let requests = 0;
    // Normal focus/poll refreshes may read data while this test runs; sorting must
    // never submit a new shortlist/ranking/publication request.
    const countRequest = request => { if (request.method() !== 'GET' && /\/api\/v1\//.test(request.url())) requests++; };
    page.on('request', countRequest);
    await page.selectOption('#shortlist-sort', 'posted-newest');
    assert.deepEqual(await page.locator('#shortlist-list .rank').allTextContents(), [...originalRanks].reverse());
    assert.deepEqual(await page.evaluate(()=>state.shortlist.recommendations.map(job=>String(job.rank).padStart(2,'0'))), originalRanks);
    await page.selectOption('#shortlist-sort', 'original');
    assert.deepEqual(await page.locator('#shortlist-list .rank').allTextContents(), originalRanks);
    page.off('request', countRequest);
    assert.equal(requests, 0, 'display sorting must not mutate data or rerank jobs');
  }
  await page.selectOption('#shortlist-sort', 'posted-oldest');
  await page.reload(); await page.locator('.curated-explanation').first().waitFor();
  assert.equal(await page.locator('#shortlist-sort').inputValue(), 'posted-oldest');
  await page.selectOption('#shortlist-sort', 'original');
  report.checks.push('Both sources sort by posting/update dates in either direction, retain original ranks/order, put missing dates last, avoid network writes and persist across reload.');
  await page.selectOption('#shortlist-source', 'curated');
  await advance('curated');
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await page.waitForFunction(() => document.querySelectorAll('#curated-list option').length === 3);
  await page.selectOption('#curated-list', firstList);
  await page.waitForURL('**/#shortlist/'+firstList);
  await page.reload(); await page.locator('.curated-explanation').first().waitFor();
  assert.equal(await page.locator('#curated-list').inputValue(), firstList);
  await page.selectOption('#shortlist-source', 'model');
  await page.locator('#shortlist-form').waitFor({state:'visible'});
  await page.selectOption('#shortlist-source', 'curated');
  await page.locator('.curated-explanation').first().waitFor();
  await page.locator('#shortlist-list .card').first().getByRole('button').first().click();
  await page.locator('#job-preview').filter({hasText:'Selected and ordered by Codex'}).waitFor();
  await page.getByRole('button',{name:'Close job preview'}).click();
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth), 'curated layout overflows');
  const reasonBox = await page.locator('.curated-explanation').first().boundingBox();
  const headingBox = await page.locator('#shortlist-list h3').first().boundingBox();
  assert(Math.abs(reasonBox.x-headingBox.x)<2 && reasonBox.width>200, 'explanation must span the text column');
  await snapshot('curated-mobile');
  await page.locator('#shortlist-list .card').first().getByRole('button').first().click();
  assert.equal(await page.locator('#job-preview').getByRole('button',{name:'Prepare application'}).count(), 0);
  await page.getByRole('button',{name:'Close job preview'}).click();
  report.checks.push('Authenticated agent publishing appears on saved-list refresh and curated focus, survives reload, preserves order/history, escapes explanations, switches sources and previews roles without a preparation action.');
  await page.setViewportSize(viewport);
  runFixturePython(['-c', `
import sys
from job_search.service import JobSearchLedger
from job_search.contracts import EventProposalInput, ApplicationEventType, ProducerKind, MutationContext
ledger = JobSearchLedger(sys.argv[1])
quote = 'Your application has been received'
evidence = ledger.record_mail_evidence({
    'account_id': 'fixture', 'immutable_message_id': 'confirmation-ui',
    'sender': 'no-reply@example.test', 'subject': 'Thank you for applying <script>inert</script>',
    'received_at': '2026-09-27T12:00:00Z', 'body_sha256': 'd' * 64, 'excerpt': quote,
}, MutationContext('confirmation-ui-mail', 'system', 'outlook_sync'))['evidence']['evidence_id']
proposal = ledger.create_event_proposal(EventProposalInput(
    evidence, sys.argv[2], ApplicationEventType.SUBMISSION_CONFIRMED, ProducerKind.RULE,
    'fixture', .99, [sys.argv[2]], quote, 0, len(quote), {}, 'confirmation-ui-proposal',
), MutationContext('confirmation-ui-proposal', 'system', 'classifier'))['proposal']
ledger.decide_event_proposal(proposal['proposal_id'], 'accepted', sys.argv[2], 'Confirmed match',
    MutationContext('confirmation-ui-accept', 'user', 'codex'))
`, path.join(stateDir, 'applications.db'), applicationId], {cwd:root});
  await page.goto(url+`/#applications/${applicationId}/overview`);
  const confirmation = page.locator('#timeline .timeline-event').filter({has:page.getByText(/^submission confirmed$/i)});
  await confirmation.getByText('Confirmation details', {exact:true}).waitFor();
  const receivedLabel = await page.evaluate(() => displayDate('2026-09-27T12:00:00Z'));
  assert.equal(await confirmation.locator(':scope > p').innerText(), `${receivedLabel} · Confirmation email`);
  assert(!/Codex/i.test(await confirmation.innerText()));
  await confirmation.locator('summary').click();
  assert.match(await confirmation.innerText(), /Linked through Codex at /);
  assert.match(await confirmation.innerText(), /Your application has been received/);
  assert.match(await confirmation.innerText(), /no-reply@example.test/);
  assert.equal(await confirmation.locator('script').count(), 0);
  assert.equal(await page.locator('#workspace-status').innerText(), 'Interviewing');
  const stagePrecedence = await page.evaluate(() => ['interviewing','offer','terminal'].map(current_phase => applicationStatusLabel({current_phase, terminal_outcome:'rejected', browser_tracking:{label:'Email confirmed'}})));
  assert.deepEqual(stagePrecedence, ['Interviewing','Offer','Closed']);
  await snapshot('confirmation-email');
  report.checks.push('Confirmation shows email receipt time and evidence, with Codex link provenance in expandable details.');

  assert.deepEqual(pageErrors,[]);
  report.passed=true;
} catch (error) {
  report.failure = error.message; report.page_errors = pageErrors;
  await snapshot("failure").catch(() => {});
  throw error;
} finally {
  report.demoDiagnostics = demoSession.diagnostics();
  await writeFile(path.join(output,'results.json'),JSON.stringify(report,null,2)+'\n');
  const video=page.video(); await context.close(); if(video) await video.saveAs(path.join(output,'walkthrough.webm'));
  await browser.close(); demo.kill('SIGINT');
}
console.log(`ok (${report.checks.length} console browser scenarios); ${output}`);
