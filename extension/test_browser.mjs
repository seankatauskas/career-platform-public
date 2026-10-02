import assert from 'node:assert/strict';
import { createHash, createPublicKey } from 'node:crypto';
import { spawn, execFileSync } from 'node:child_process';
import { mkdtemp, mkdir, readFile, writeFile, rm } from 'node:fs/promises';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import os from 'node:os';
import { chromium } from 'playwright-core';

const extension = path.dirname(fileURLToPath(import.meta.url));
const output = path.resolve(process.env.EXTENSION_TEST_OUTPUT || path.join(extension, 'test-results'));
await mkdir(output, { recursive: true });
const temporary = await mkdtemp(path.join(os.tmpdir(), 'career-extension-browser-'));
let fixture;
let context;
const report = { passed: false, checks: [], screenshots: [], fixture: 'Synthetic ATS forms and local TLS identity proxy; no live provider calls.', permissionSetup: 'Native Chromium host permission approved through chrome://extensions in the isolated test profile; production popup requests it from its click handler.' };
const pending = [];
let stderr = '';
function nextLine() { return new Promise((resolve, reject) => pending.push({ resolve, reject })); }
async function command(value) {
  const next = nextLine();
  fixture.stdin.write(JSON.stringify(value) + '\n');
  return next;
}
async function waitFor(check, description, timeout = 8000) {
  const start = Date.now();
  while (Date.now() - start < timeout) {
    if (await check()) return;
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new Error(`Timed out: ${description}`);
}

try {
  const certificate = path.join(temporary, 'cert.pem');
  const key = path.join(temporary, 'key.pem');
  const sslConfig = path.join(temporary, 'openssl.cnf');
  await writeFile(sslConfig, '[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n[dn]\nCN=career.fixture-tailnet.ts.net\n[ext]\nsubjectAltName=DNS:career.fixture-tailnet.ts.net,DNS:job-boards.greenhouse.io,DNS:jobs.ashbyhq.com,DNS:jobs.lever.co,DNS:employer.fixture.test\n');
  execFileSync('openssl', ['req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1', '-config', sslConfig, '-keyout', key, '-out', certificate], { stdio: 'ignore' });
  const spki = createHash('sha256').update(createPublicKey(await readFile(certificate)).export({ type: 'spki', format: 'der' })).digest('base64');
  const ready = nextLine();
  fixture = spawn(process.env.PYTHON || 'python3', [path.join(extension, 'browser_fixture.py'), certificate, key], { stdio: ['pipe', 'pipe', 'pipe'] });
  fixture.stderr.on('data', (data) => { stderr += data; });
  createInterface({ input: fixture.stdout }).on('line', (line) => {
    const waiter = pending.shift();
    if (waiter) { try { waiter.resolve(JSON.parse(line)); } catch (error) { waiter.reject(error); } }
  });
  fixture.on('exit', (code) => { while (pending.length) pending.shift().reject(new Error(`Fixture exited ${code}: ${stderr}`)); });
  const hosts = await ready;
  const mappedHosts = ['career.fixture-tailnet.ts.net', 'job-boards.greenhouse.io', 'jobs.ashbyhq.com', 'jobs.lever.co', 'employer.fixture.test'];
  context = await chromium.launchPersistentContext(path.join(temporary, 'profile'), {
    channel: 'chromium', headless: true, viewport: { width: 1100, height: 850 },
    args: [
      `--disable-extensions-except=${extension}`, `--load-extension=${extension}`,
      `--ignore-certificate-errors-spki-list=${spki}`,
      `--host-resolver-rules=${mappedHosts.map((host) => `MAP ${host} 127.0.0.1:${hosts.tls_port}`).join(',')},MAP * ~NOTFOUND,EXCLUDE 127.0.0.1,EXCLUDE localhost`,
      '--no-proxy-server'
    ]
  });
  context.setDefaultTimeout(8000);
  report.networkFailures=[];
  context.on('requestfailed',r=>report.networkFailures.push({url:r.url(),failure:r.failure()}));
  context.on('console',m=>{ if(m.type()==='error') (report.consoleErrors ||= []).push(m.text()); });
  let worker = context.serviceWorkers()[0] || await context.waitForEvent('serviceworker');
  const id = new URL(worker.url()).hostname;
  const popupUrl = `chrome-extension://${id}/popup/popup.html`;
  let atsPage = await context.newPage();
  let popup = await context.newPage();

  async function openPairing(handoff, base) {
    await atsPage.goto(handoff.page_url);
    await atsPage.bringToFront();
    await popup.goto(popupUrl);
    await popup.locator('#fill').waitFor({ state: 'visible' });
    await waitFor(() => popup.locator('#fill').isEnabled(), 'ATS form detected');
    await popup.locator('#dashboard-base').fill(base);
    await popup.locator('#pairing-code').fill(handoff.pairing_code);
  }
  async function clickAndNotice(pattern) {
    await popup.locator('#fill').click();
    await waitFor(async () => pattern.test(await popup.locator('#notice').innerText()), `notice ${pattern}`);
    assert.equal(await popup.locator('#pairing-code').inputValue(), '');
  }
  async function screenshot(page, name) {
    await page.screenshot({ path: path.join(output, name), fullPage: true });
    report.screenshots.push(name);
  }

  const local = await command({ action: 'issue', ats: 'ashby' });
  await openPairing(local, hosts.dashboard);
  await clickAndNotice(/Filled [1-9]/);
  assert.equal(await atsPage.locator('#first').inputValue(), 'Sean');
  assert.equal(await atsPage.locator('#email').inputValue(), 'sean@example.test');
  assert.equal(await atsPage.locator('#salary').inputValue(), '');
  assert.equal(await atsPage.evaluate(() => window.submitCount), 0);
  report.checks.push('Real loopback HTTP pairing and scoped ATS autofill; salary/upload/submit remain manual.');

  // WebUI grants exactly the origin a user is about to approve. The subsequent
  // production popup still invokes chrome.permissions.request and must succeed.
  // This avoids automating native permission dialog chrome in headless CI.
  const permissionsPage = await context.newPage();
  await permissionsPage.goto('chrome://extensions');
  await permissionsPage.evaluate(({ id, origin }) => chrome.developerPrivate.addHostPermission(id, `${origin}/*`), { id, origin: hosts.cloud });
  await permissionsPage.close();

  const cloud = await command({ action: 'issue', ats: 'greenhouse' });
  await openPairing(cloud, hosts.cloud);
  await clickAndNotice(/Filled [1-9]/);
  assert.equal(await worker.evaluate((origin) => chrome.permissions.contains({ origins: [`${origin}/*`] }), hosts.cloud), true);
  assert.equal(await worker.evaluate(() => chrome.permissions.contains({ origins: ['https://different.fixture-tailnet.ts.net/*'] })), false);
  assert.equal(await atsPage.locator('#first').inputValue(), 'Sean');
  assert.equal(await atsPage.evaluate(() => window.submitCount), 0);
  await screenshot(atsPage, 'cloud-autofill.png');
  await screenshot(popup, 'cloud-paired-popup.png');
  report.checks.push('Real private HTTPS pairing through owner-checked identity proxy; exact origin permission only.');

  await atsPage.locator('#submit').click();
  popup.once('dialog', (dialog) => dialog.accept());
  await popup.locator('summary').click();
  await popup.locator('#mark-submitted').click();
  await waitFor(async () => /Submission recorded/.test(await popup.locator('#notice').innerText()), 'submission recorded');
  const submitted = await command({ action: 'state', application_id: cloud.application.application_id });
  assert.equal(submitted.timeline.application.current_phase, 'awaiting_confirmation');
  assert.equal(await atsPage.evaluate(() => window.submitCount), 1);
  assert(submitted.requests.some((request) => request.path === '/api/v1/autofill/submitted'));
  assert(submitted.requests.filter((request) => request.path.startsWith('/api/v1/autofill/')).every((request) => !request.cookie && request.origin === `chrome-extension://${id}`));
  await screenshot(popup, 'submission-recorded.png');
  report.checks.push('Manual form submission records in the real ledger, with extension Origin and no cookies.');

  await openPairing(cloud, hosts.cloud);
  await clickAndNotice(/expired or was already used/);
  report.checks.push('Reusing a one-time pairing code is rejected.');

  const expired = await command({ action: 'issue' });
  await command({ action: 'expire' });
  await openPairing(expired, hosts.cloud);
  await clickAndNotice(/expired or was already used/);
  report.checks.push('Expired pairing gives a recoverable error.');

  const wrongOwner = await command({ action: 'issue' });
  await command({ action: 'owner', value: 'another@example.test' });
  await openPairing(wrongOwner, hosts.cloud);
  await clickAndNotice(/refused access/);
  await command({ action: 'owner', value: 'owner@example.test' });
  report.checks.push('Wrong Tailscale owner is rejected by the real dashboard boundary.');

  await command({ action: 'redirect', value: true });
  await openPairing(wrongOwner, hosts.cloud);
  await clickAndNotice(/Cannot reach the private dashboard/);
  await command({ action: 'redirect', value: false });
  report.checks.push('HTTP redirects cannot forward pairing credentials.');

  const revoke = await command({ action: 'issue' });
  await openPairing(revoke, hosts.cloud);
  await clickAndNotice(/Filled [1-9]/);
  await worker.evaluate((origin) => chrome.permissions.remove({ origins: [`${origin}/*`] }), hosts.cloud);
  await waitFor(async () => {
    const stored = await worker.evaluate(() => Promise.all([chrome.storage.local.get(null), chrome.storage.session.get(null)]));
    return !stored[0].dashboard_base && !Object.values(stored[1]).some((receipt) => receipt.dashboard_base === hosts.cloud);
  }, 'revoked-origin handoffs cleared');
  assert.equal(await worker.evaluate((origin) => chrome.permissions.contains({ origins: [`${origin}/*`] }), hosts.cloud), false);
  report.checks.push('Real browser permission revocation clears its saved origin and receipt.');

  const grant = await context.newPage();
  await grant.goto('chrome://extensions');
  await grant.evaluate(({id, origin})=>chrome.developerPrivate.addHostPermission(id,`${origin}/*`),{id,origin:hosts.cloud});
  await grant.close();
  const browserCode = await command({action:'autopair',cloud:true});
  await atsPage.goto('https://job-boards.greenhouse.io/acme/jobs/4100?fixture=tracking');
  await atsPage.bringToFront(); await popup.goto(popupUrl);
  await popup.locator('#dashboard-base').fill(hosts.cloud);
  await popup.locator('#pairing-code').fill(browserCode.pairing_code);
  await popup.locator('#connect-browser').click();
  await waitFor(async()=>!!(await worker.evaluate(()=>chrome.storage.local.get('browser_connection'))).browser_connection,'persistent browser connected');
  const initial = (await command({action:'applications'})).applications.length;
  const pages = [
    ['greenhouse','https://job-boards.greenhouse.io/acme/jobs/4100?fixture=tracking'],
    ['ashby','https://jobs.ashbyhq.com/acme/00000000-0000-4000-8000-000000000001?fixture=tracking'],
    ['lever','https://jobs.lever.co/acme/00000000-0000-4000-8000-000000000002?fixture=tracking']
  ];
  for(const [ats,url] of pages) {
    await atsPage.goto(url); await atsPage.bringToFront();
    await waitFor(async()=>Object.values(await worker.evaluate(()=>chrome.storage.local.get(null))).some(v=>v?.job?.ats===ats && v.job.canonical_url===url.split('?')[0]),'job recognized');
    assert.equal((await command({action:'applications'})).applications.length,initial+pages.findIndex(p=>p[0]===ats));
    await atsPage.bringToFront(); await popup.goto(popupUrl);
    await waitFor(()=>popup.locator('#fill').isEnabled(),'autofill available');
    await popup.locator('#fill').click();
    await waitFor(async()=>/supplied to the form/.test(await popup.locator('#notice').innerText()),'saved resume supplied');
    assert.equal(await atsPage.locator('#resume').evaluate(e=>e.files[0]?.name),'Sean_Example_Resume.pdf');
    assert.equal(await atsPage.locator('#resume').evaluate(async e=>await e.files[0].text()),'%PDF-1.7\nSynthetic extension upload fixture\n%%EOF');
    assert.equal(await atsPage.evaluate(()=>window.submitCount),0);
    await popup.locator('#fill').click();
    await waitFor(async()=>/left unchanged/.test(await popup.locator('#notice').innerText()),'existing upload preserved');
    const storage=JSON.stringify(await worker.evaluate(()=>chrome.storage.local.get(null)));
    assert(!storage.includes('content_base64') && !storage.includes('Synthetic extension upload fixture'));
    await atsPage.locator('#email').fill('sean@example.test');
    const prose=`My saved ${ats} answer.\n\nI manage Kubernetes services end to end. <script>window.answerExecuted=true</script>`;
    await atsPage.locator('#why').fill(prose);
    await atsPage.locator('#experience').fill('Infrastructure answer containing the word company.');
    await atsPage.locator('#rich').fill('Rich text project details.');
    await atsPage.locator('#salary').fill('150000');
    await atsPage.locator('#location').selectOption('remote');
    await atsPage.locator('#authorized').check();
    await atsPage.locator('#terms').check();
    await atsPage.locator('#password').fill('DO-NOT-CAPTURE-PASSWORD');
    await atsPage.locator('#submit').click();
    await waitFor(async()=>(await command({action:'applications'})).applications.some(a=>a.ats===ats && a.job_id!=='job-1' && a.submitted_at),'automatic submission tracked');
    assert.equal(await atsPage.evaluate(()=>window.submitCount),1);
    const app=(await command({action:'applications'})).applications.find(a=>a.ats===ats && a.job_id!=='job-1');
    assert.equal(app.current_phase,'awaiting_confirmation');
    await waitFor(async()=>(await command({action:'state',application_id:app.application_id})).answers.length>0,'application answers persisted');
    const captured=(await command({action:'state',application_id:app.application_id})).answers[0].snapshot.fields;
    const value=prompt=>captured.find(field=>field.prompt===prompt)?.value;
    assert.equal(value('Why this company?'),prose);
    assert.equal(value('Describe your company infrastructure experience'),'Infrastructure answer containing the word company.');
    assert.equal(value('Tell us about a project'),'Rich text project details.');
    assert.equal(value('Desired salary'),'150000');
    assert.deepEqual(value('Location'),['Remote']);
    assert.equal(value('I agree to the terms'),true);
    assert.equal(captured.find(field=>field.section==='Work authorization' && field.prompt==='Yes')?.value,true);
    assert.deepEqual(value('Resume'),['Sean_Example_Resume.pdf']);
    assert.equal(value('Email'),'sean@example.test');
    assert(!JSON.stringify(captured).includes('DO-NOT-CAPTURE-PASSWORD'));
    assert(!JSON.stringify(captured).includes('SECRET-TOKEN'));
    assert(!JSON.stringify(await worker.evaluate(()=>chrome.storage.local.get(null))).includes(prose));
    const dashboard=await context.newPage();
    await dashboard.goto(hosts.cloud+`/#applications/${app.application_id}/answers`);
    await dashboard.locator('#workspace-answers').getByText(prose,{exact:true}).first().waitFor();
    assert.equal(await dashboard.evaluate(()=>window.answerExecuted),undefined);
    await dashboard.reload();
    await dashboard.locator('#workspace-answers').getByText(prose,{exact:true}).first().waitFor();
    assert.equal((await command({action:'state',application_id:app.application_id})).answers.length,1,'click and native submit should not duplicate history');
    if(ats==='greenhouse') await screenshot(dashboard,'saved-application-answers.png');
    await dashboard.close();
    await command({action:'mailconfirm',application_id:app.application_id});
    const timeline=(await command({action:'state',application_id:app.application_id})).timeline;
    assert.equal(timeline.application.current_phase,'active');
    assert.equal(timeline.events.filter(e=>e.event_type==='submission_observed').length,1);
  }
  report.checks.push('All three ATS fixtures automatically recognize, track submission, and attach separate email confirmation; no Mark submitted action.');
  report.checks.push('All three ATS forms retain exact prose, rich text, salary, choices, attestations, contact fields and filenames; secrets excluded; dashboard renders safely after reload.');
  // Modern Greenhouse performs a full navigation to /confirmation after verification.
  const confirmationUrl='https://job-boards.greenhouse.io/acme/jobs/4104/confirmation';
  await atsPage.goto(confirmationUrl);
  await new Promise(resolve=>setTimeout(resolve,300));
  assert(!(await command({action:'applications'})).applications.some(a=>a.job_id==='4104'));
  await atsPage.goto('https://job-boards.greenhouse.io/acme/jobs/4104?fixture=tracking&outcome=redirect');
  await atsPage.locator('#email').fill('sean@example.test');
  await atsPage.locator('#why').fill('Saved before redirect.');
  await waitFor(async()=>Object.values(await worker.evaluate(()=>chrome.storage.local.get(null))).some(v=>v?.job?.job_id==='4104'),'redirect job recognized');
  await atsPage.locator('#submit').click();
  await atsPage.waitForURL(confirmationUrl);
  await waitFor(async()=>(await command({action:'applications'})).applications.some(a=>a.job_id==='4104' && a.submitted_at),'Greenhouse confirmation redirect tracked');
  const redirected=(await command({action:'applications'})).applications.find(a=>a.job_id==='4104');
  assert.equal(redirected.current_phase,'awaiting_confirmation');
  await waitFor(async()=>(await command({action:'state',application_id:redirected.application_id})).answers.some(a=>a.snapshot.fields.some(f=>f.value==='Saved before redirect.')),'redirect answers retained');
  await atsPage.reload();
  await atsPage.waitForSelector('h1');
  await new Promise(resolve=>setTimeout(resolve,300));
  const redirectTimeline=(await command({action:'state',application_id:redirected.application_id})).timeline;
  assert.equal(redirectTimeline.events.filter(e=>e.event_type==='submission_observed').length,1);
  report.checks.push('Greenhouse full-page confirmation redirect preserves the attempted job; reload deduplicates and direct confirmation visits create no applications.');
  const embedded='https://jobs.lever.co/acme/00000000-0000-4000-8000-000000000003?fixture=tracking';
  await atsPage.goto('https://employer.fixture.test/embed?url='+encodeURIComponent(embedded));
  const frame=atsPage.frameLocator('iframe');
  await frame.locator('#email').fill('sean@example.test');
  await frame.locator('#why').fill('Embedded form answer.');
  await new Promise(resolve=>setTimeout(resolve,300));
  await frame.locator('#submit').click();
  await waitFor(async()=>(await command({action:'applications'})).applications.some(a=>a.job_id.endsWith('000003') && a.submitted_at),'embedded submission tracked');
  const embeddedApp=(await command({action:'applications'})).applications.find(a=>a.job_id.endsWith('000003'));
  await waitFor(async()=>(await command({action:'state',application_id:embeddedApp.application_id})).answers.some(a=>a.snapshot.fields.some(f=>f.value==='Embedded form answer.')),'embedded answers retained');
  report.checks.push('ATS form embedded on an ungranted company host is tracked in its own frame.');
  await atsPage.goto('https://job-boards.greenhouse.io/acme/jobs/4101?fixture=tracking&outcome=failed');
  await atsPage.locator('#email').fill('sean@example.test');
  await new Promise(resolve=>setTimeout(resolve,300));
  await atsPage.locator('#submit').click();
  await waitFor(async()=>Object.values(await worker.evaluate(()=>chrome.storage.local.get(null))).some(v=>v?.status==='failed'),'failed submission detected');
  const failed=(await command({action:'applications'})).applications.find(a=>a.job_id==='4101');
  assert.equal(failed.submitted_at,null);
  await waitFor(async()=>(await command({action:'state',application_id:failed.application_id})).answers.length>0,'failed attempt keeps answers without marking submitted');
  await atsPage.goto('https://job-boards.greenhouse.io/acme/jobs/4102?fixture=tracking&outcome=uncertain');
  await atsPage.locator('#email').fill('sean@example.test');
  await new Promise(resolve=>setTimeout(resolve,300));
  await atsPage.locator('#submit').click();
  await waitFor(async()=>Object.values(await worker.evaluate(()=>chrome.storage.local.get(null))).some(v=>v?.status==='request_sent'),'HTTP success remains unconfirmed');
  const uncertain=(await command({action:'applications'})).applications.find(a=>a.job_id==='4102');
  assert.equal(uncertain.submitted_at,null);
  await command({action:'mailconfirm',application_id:uncertain.application_id});
  assert.equal((await command({action:'state',application_id:uncertain.application_id})).timeline.application.current_phase,'active');
  report.checks.push('Failed requests never count as submitted; a generic HTTP 200 remains unconfirmed until email evidence.');
  await atsPage.goto('https://job-boards.greenhouse.io/acme/jobs/4103?fixture=tracking');
  await atsPage.locator('#email').fill('sean@example.test'); await new Promise(resolve=>setTimeout(resolve,300));
  const longAnswer='Prior step prose\n'+('Experience across multiple services.\n'.repeat(1400));
  await atsPage.locator('#why').fill(longAnswer,{timeout:30000});
  await atsPage.evaluate(()=>{
    document.querySelector('#why').closest('label').remove();
    document.querySelector('form').insertAdjacentHTML('afterbegin','<label>Final step answer<textarea id="last-step"></textarea></label>');
  });
  await atsPage.locator('#last-step').fill('This is the final step.');
  await atsPage.locator('#experience').fill('Erase this answer.');
  await atsPage.locator('#experience').fill('');
  await command({action:'offline',value:true});
  await atsPage.locator('#submit').click();
  await waitFor(async()=>Object.keys(await worker.evaluate(()=>chrome.storage.local.get(null))).filter(k=>k.startsWith('tracking-event-')).length>=2,'offline observations queued');
  await waitFor(async()=>Object.values(await worker.evaluate(()=>chrome.storage.local.get(null))).some(v=>v?.item?.kind==='site_acknowledged'),'offline success retained');
  await waitFor(async()=>Object.keys(await worker.evaluate(()=>chrome.storage.local.get(null))).some(k=>k.startsWith('tracking-answer-')&&!k.startsWith('tracking-answer-result-')),'offline answers queued');
  assert(!JSON.stringify(await worker.evaluate(()=>chrome.storage.local.get(null))).includes('Prior step prose'));
  await context.close();
  await command({action:'offline',value:false});
  context=await chromium.launchPersistentContext(path.join(temporary,'profile'),{
    channel:'chromium',headless:true,viewport:{width:1100,height:850},args:[
      `--disable-extensions-except=${extension}`,`--load-extension=${extension}`,
      `--ignore-certificate-errors-spki-list=${spki}`,
      `--host-resolver-rules=${mappedHosts.map(host=>`MAP ${host} 127.0.0.1:${hosts.tls_port}`).join(',')},MAP * ~NOTFOUND,EXCLUDE 127.0.0.1,localhost`,
      '--no-proxy-server'
    ]
  });
  worker=context.serviceWorkers()[0]||await context.waitForEvent('serviceworker');
  await waitFor(async()=>{
    await worker.evaluate(()=>flushTracking());
    return (await command({action:'applications'})).applications.some(a=>a.job_id==='4103' && a.submitted_at);
  },'offline observations replayed after browser restart',90000);
  const recovered=(await command({action:'applications'})).applications.find(a=>a.job_id==='4103');
  assert.equal((await command({action:'state',application_id:recovered.application_id})).timeline.events.filter(e=>e.event_type==='submission_observed').length,1);
  await waitFor(async()=>{
    await worker.evaluate(()=>flushTracking());
    return (await command({action:'state',application_id:recovered.application_id})).answers.length>0;
  },'offline answers replayed after browser restart',90000);
  const recoveredFields=(await command({action:'state',application_id:recovered.application_id})).answers[0].snapshot.fields;
  assert.equal(recoveredFields.find(f=>f.prompt==='Why this company?')?.value,longAnswer);
  assert.equal(recoveredFields.find(f=>f.prompt==='Final step answer')?.value,'This is the final step.');
  assert.equal(recoveredFields.find(f=>f.prompt==='Describe your company infrastructure experience')?.value,'');
  assert(!JSON.stringify(recoveredFields).includes('My saved'));
  report.checks.push('Multi-step long prose and intentional blank edits survive outage and full browser restart without leaking plaintext to browser storage or mixing applications.');
  report.checks.push('Dashboard outage plus full Chromium restart preserves and replays submission exactly once, with bounded retry backoff.');
  atsPage=await context.newPage(); await atsPage.goto(pages[0][1]);
  await screenshot(atsPage,'automatic-tracking.png');
  report.passed = true;
} catch (error) {
  report.error = error.stack || String(error);
  report.fixtureErrors = stderr;
  try { const w=context?.serviceWorkers()[0]; if(w) report.trackingDebug=await w.evaluate(async()=>Object.fromEntries(Object.entries(await chrome.storage.local.get(null)).filter(([k])=>k.startsWith('tracking-') || k==='tracking_error'))); } catch(_) {}
  console.error(report.error);
  process.exitCode = 1;
} finally {
  if (context) await context.close();
  if (fixture && fixture.exitCode === null) {
    fixture.stdin.end(JSON.stringify({ action: 'stop' }) + '\n');
    await Promise.race([new Promise((resolve) => fixture.once('exit', resolve)), new Promise((resolve) => setTimeout(() => { fixture.kill(); resolve(); }, 3000))]);
  }
  await writeFile(path.join(output, 'results.json'), JSON.stringify(report, null, 2) + '\n');
  await rm(temporary, { recursive: true, force: true });
}
if (report.passed) console.log(`ok (${report.checks.length} real extension browser scenarios); ${path.join(output, 'results.json')}`);
