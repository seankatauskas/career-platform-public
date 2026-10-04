"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");

const root = __dirname;
const common = require("./adapters/common.js");
const greenhouse = require("./adapters/greenhouse.js");
const ashby = require("./adapters/ashby.js");
const lever = require("./adapters/lever.js");
const connection = require("./dashboard_connection.js");
const vm = require("vm");

function fakeInput({ type = "text", value = "", attributes = {}, tagName = "INPUT", label = "" } = {}) {
  return {
    tagName,
    type,
    value,
    checked: false,
    disabled: false,
    readOnly: false,
    labels: label ? [{ textContent: label }] : [],
    options: [],
    getAttribute(name) { return attributes[name] || ""; },
    closest() { return null; },
    dispatchEvent() {}
  };
}

function testPrivateAndForbiddenGuardrails() {
  const privateFields = [
    "Race and ethnicity", "Gender identity", "Work authorization", "Visa sponsorship",
    "Disability", "Veteran status", "Voluntary EEO"
  ];
  privateFields.forEach((prompt) => {
    assert(common.isPrivate(prompt), prompt);
    assert.strictEqual(common.genericKind(prompt), "private_answer");
  });
  ["Legal attestation", "Salary expectation", "Upload resume", "Submit application"].forEach((prompt) => {
    assert(common.isForbidden(prompt), prompt);
    assert.strictEqual(common.genericKind(prompt), null);
  });
  ["file", "submit", "button", "hidden", "password"].forEach((type) => {
    assert.strictEqual(common.safeControl(fakeInput({ type }), "Email"), false, type);
  });
  ["checkbox", "radio"].forEach((type) => {
    assert.strictEqual(common.safeControl(fakeInput({ type }), "Race"), true, type);
  });
  assert.strictEqual(common.safeControl(fakeInput(), "Salary expectation"), false);
}

function testSafeInferenceAndApplication() {
  assert.strictEqual(common.genericKind("First name"), "first_name");
  assert.strictEqual(common.genericKind("Email address"), "email");
  assert.strictEqual(common.genericKind("Current employer"), "work_employer");
  assert.strictEqual(common.genericKind("Why this role?"), "approved_answer");
  assert.strictEqual(common.genericKind("Are you legally authorized to work?"), "private_answer");
  const element = fakeInput({ attributes: { name: "email" } });
  const record = {
    element,
    grouped: [element],
    optionElements: new Map(),
    metadata: "Email",
    descriptor: { control: "text", options: [] }
  };
  assert.strictEqual(common.applyOne(record, { value: "safe@example.test" }), true);
  assert.strictEqual(element.value, "safe@example.test");
  assert.strictEqual(common.applyOne(record, { value: "overwrite@example.test" }), false);
  const sensitive = fakeInput();
  assert.strictEqual(common.applyOne({
    element: sensitive,
    grouped: [sensitive],
    optionElements: new Map(),
    metadata: "Desired salary",
    descriptor: { control: "text", options: [] }
  }, { value: "1" }), false);
}

function testPrivateGroupsFillAndCaptureWithoutOverwrite() {
  const yes = fakeInput({ type: "radio", label: "Yes", attributes: { name: "disability" } });
  const no = fakeInput({ type: "radio", label: "No", attributes: { name: "disability" } });
  const record = {
    element: yes,
    grouped: [yes, no],
    optionElements: new Map([["yes", yes], ["no", no]]),
    metadata: "Disability status",
    descriptor: {
      field_id: "disability",
      kind: "private_answer",
      control: "radio_group",
      options: [
        { option_id: "yes", label: "Yes" },
        { option_id: "no", label: "No" }
      ]
    }
  };
  assert(common.applyOne(record, { option_ids: ["no"] }));
  assert.strictEqual(no.checked, true);
  assert.strictEqual(common.applyOne(record, { option_ids: ["yes"] }), false);
  const description = { elements: new Map([["disability", record]]) };
  assert.deepStrictEqual(common.snapshot(description), [
    { field_id: "disability", option_ids: ["no"] }
  ]);
}

function testAdaptersAreHostScoped() {
  assert(greenhouse.supports("job-boards.greenhouse.io"));
  assert(greenhouse.supports("boards.greenhouse.io"));
  assert(!greenhouse.supports("jobs.ashbyhq.com"));
  assert(ashby.supports("jobs.ashbyhq.com"));
  assert(!ashby.supports("evil.jobs.ashbyhq.com"));
  assert(lever.supports("jobs.lever.co"));
  assert(lever.supports("jobs.eu.lever.co"));
  assert(!lever.supports("lever.example.test"));
}

function testManifestAndSourcesHaveNoSubmitCapability() {
  const manifest = JSON.parse(fs.readFileSync(path.join(root, "manifest.json"), "utf8"));
  assert.strictEqual(manifest.manifest_version, 3);
  assert.deepStrictEqual(manifest.host_permissions.sort(), ["http://127.0.0.1/*", "http://localhost/*", ...manifest.content_scripts[0].matches, "https://boards-api.greenhouse.io/*"].sort());
  assert(!manifest.host_permissions.includes("<all_urls>"));
  assert(!manifest.permissions.includes("webRequestBlocking"));
  const trackingContent = fs.readFileSync(path.join(root, "tracking_content.js"), "utf8");
  assert(!/\.(?:submit|click)\s*\(/.test(trackingContent));
  assert(!/preventDefault\s*\(/.test(trackingContent));
  assert.deepStrictEqual(manifest.optional_host_permissions, ["https://*.ts.net/*"]);
  const matches = manifest.content_scripts[0].matches.join(" ");
  ["ashbyhq.com", "greenhouse.io", "lever.co"].forEach((host) => assert(matches.includes(host)));
  const content = fs.readFileSync(path.join(root, "content.js"), "utf8");
  assert(!/\.submit\s*\(/.test(content));
  assert(!/\.click\s*\(/.test(content));
  assert(/addEventListener\s*\(\s*["']submit/.test(content));
  assert(!/preventDefault\s*\(/.test(content));
  const popup = fs.readFileSync(path.join(root, "popup/popup.html"), "utf8");
  assert(popup.includes("Mark submitted"));
  assert(popup.includes("never clicks Submit"));
  assert(popup.includes('id="resume-status"'));
  const popupScript = fs.readFileSync(path.join(root, "popup/popup.js"), "utf8");
  assert(popupScript.includes("window.confirm("));
  assert(popupScript.includes('resumeDecision === "not_tracked"'));
  assert(popupScript.includes("resume_decision: resumeDecision"));
  assert(popupScript.includes('resumeStatus.status === "selected"'));
  const worker = fs.readFileSync(path.join(root, "service_worker.js"), "utf8");
  assert(worker.includes('"/api/v1/autofill/exchange"'));
  assert(worker.includes('"/api/v1/autofill/capture"'));
  assert(worker.includes("pairing_code: message.pairing_code"));
  assert(worker.includes("page_url: currentForm.page_url"));
  assert(worker.includes("ats: currentForm.ats"));
  assert(worker.includes("resume_decision: message.resume_decision"));
  assert(worker.includes('["selected", "not_tracked"]'));
  assert(/return\s*\{\s*ok:\s*true,[\s\S]*active[,:]/.test(worker));
  assert(!/\?[^\n]*pairing_code/.test(worker));
  const storageWrites = worker.match(/chrome\.storage\.session\.set\(\{[\s\S]*?\n\s*\}\);/g) || [];
  assert(storageWrites.length > 0);
  storageWrites.forEach((write) => {
    assert(!/\banswers\b/.test(write));
    assert(!/\bvalue\b/.test(write));
    assert(!/\boption_ids\b/.test(write));
  });
}

function testStrictDashboardOrigins() {
  for (const [input, expected] of [
    ["http://127.0.0.1:8766", "http://127.0.0.1:8766"],
    ["http://localhost:9000/", "http://localhost:9000"],
    ["https://career.example-tailnet.ts.net:443/", "https://career.example-tailnet.ts.net"],
    ["https://c.e.ts.net", "https://c.e.ts.net"]
  ]) assert.strictEqual(connection.dashboardBase(input), expected);
  for (const input of [
    "https://career.example.com", "http://career.example.ts.net", "https://example.ts.net",
    "https://career.example.ts.net.evil.test", "https://*.example.ts.net", "https://a.b.c.ts.net",
    "https://user:pass@career.example.ts.net", "http://user@localhost:8766", "http://127.1:8766",
    "http://localhost:0", "http://localhost:65536", "http://localhost", "http://localhost:9000/path",
    "https://career.example.ts.net:9443", "https://career.example.ts.net/?query=1",
    "https://career.example.ts.net/#fragment", "https://CAREER.example.ts.net", "https://-c.e.ts.net",
    "https://career.example.ts.net/../", "http://localhost:9000\\", "https://career.example.ts.net\n/x"
  ]) assert.throws(() => connection.dashboardBase(input), undefined, input);
  assert.strictEqual(connection.permissionOrigin("https://c.e.ts.net"), "https://c.e.ts.net/*");
}

function workerHarness() {
  const memory = { local: {}, session: {} };
  const calls = [];
  let permitted = true;
  let listener;
  let removal;
  const form = { supported: true, ats: "greenhouse", page_url: "https://job-boards.greenhouse.io/acme/job-1", fields: [], answers: [] };
  const storage = (area) => ({
    async get(key) { return key === null ? { ...memory[area] } : { [key]: memory[area][key] }; },
    async set(values) { Object.assign(memory[area], values); },
    async remove(key) { delete memory[area][key]; }
  });
  const context = vm.createContext({
    JobDashboardConnection: connection,
    importScripts() {}, URL, AbortController, setTimeout, clearTimeout,
    crypto: require("crypto").webcrypto,
    chrome: {
      runtime: { onMessage: { addListener(fn) { listener = fn; } } },
      permissions: {
        async contains(value) { calls.push({ permission: value }); return permitted; },
        onRemoved: { addListener(fn) { removal = fn; } }
      },
      storage: { local: storage("local"), session: storage("session") },
      tabs: { async sendMessage(_tab, message) {
        return message.type === "applyAssignments" ? { filled: 2 } : form;
      } }
    },
    fetch: async (url, options) => {
      calls.push({ url, options });
      return { ok: true, json: async () => ({
        assignments: [], submission_token: "receipt-token", application: { title: "Engineer" }, resume: { status: "not_selected" }
      }) };
    }
  });
  vm.runInContext(fs.readFileSync(path.join(root, "service_worker.js"), "utf8"), context);
  return {
    calls, memory, form, context,
    permission(value) { permitted = value; },
    removed() { removal(); },
    send(message) { return new Promise((resolve) => listener(message, {}, resolve)); },
    evaluate(source) { return vm.runInContext(source, context); }
  };
}

async function testPermissionAndRequestBoundary() {
  const worker = workerHarness();
  worker.permission(false);
  await assert.rejects(worker.evaluate('post("https://c.e.ts.net", "/api/v1/autofill/exchange", {})'), /access was denied or removed/);
  assert.strictEqual(worker.calls.filter((call) => call.url).length, 0);
  worker.permission(true);
  await worker.evaluate('post("https://c.e.ts.net", "/api/v1/autofill/exchange", {pairing_code:"secret"})');
  const request = worker.calls.find((call) => call.url);
  assert.strictEqual(request.url, "https://c.e.ts.net/api/v1/autofill/exchange");
  assert.strictEqual(request.options.credentials, "omit");
  assert.strictEqual(request.options.redirect, "error");
  assert.strictEqual(request.options.referrerPolicy, "no-referrer");
  assert(!request.url.includes("secret"));
}

async function testFailedPairingDoesNotPersistAndErrorsDiffer() {
  const worker = workerHarness();
  const cases = [
    [400, "pairing code is invalid or expired", /expired or was already used/],
    [400, "submission token is invalid or expired", /application handoff expired/],
    [400, "invalid Host header", /refused access/],
    [400, "pairing code is scoped to a different ATS", /different ATS/]
  ];
  for (const [status, error, pattern] of cases) {
    worker.context.fetch = async () => ({ ok: false, status, json: async () => ({ error }) });
    const result = await worker.send({ type: "exchangeHandoff", dashboard_base: "https://c.e.ts.net", pairing_code: "bad", form: worker.form });
    assert.strictEqual(result.ok, false);
    assert.match(result.error, pattern);
    assert.deepStrictEqual(worker.memory.local, {});
  }
  worker.context.fetch = async () => { throw new Error("offline"); };
  await assert.rejects(worker.evaluate('post("https://c.e.ts.net", "/api/v1/autofill/exchange", {})'), /Check Tailscale/);
  await assert.rejects(worker.evaluate('post("http://localhost:8766", "/api/v1/autofill/exchange", {})'), /Start it and check/);
}

async function testSuccessfulPairingAndPermissionRevocation() {
  const worker = workerHarness();
  const result = await worker.send({ type: "exchangeHandoff", dashboard_base: "https://c.e.ts.net:443/", pairing_code: "valid", tab_id: 1, form: worker.form });
  assert.strictEqual(result.ok, true);
  assert.strictEqual(worker.memory.local.dashboard_base, "https://c.e.ts.net");
  assert(worker.memory.session["submission-1"]);
  worker.permission(false);
  worker.removed();
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepStrictEqual(worker.memory.local, {});
  assert.deepStrictEqual(worker.memory.session, {});
}

async function testSubmissionRetryDoesNotRestageUsedReceipt() {
  const worker = workerHarness();
  await worker.send({ type: "exchangeHandoff", dashboard_base: "http://localhost:8766", pairing_code: "valid", tab_id: 1, form: worker.form });
  worker.form.answers = [{ field_id: "why", value: "Private answer" }];
  let captureCount = 0;
  const attempts = [];
  worker.context.fetch = async (url, options) => {
    if (url.endsWith("/capture")) { captureCount++; return { ok: true, json: async () => ({ staged: true }) }; }
    attempts.push(JSON.parse(options.body));
    if (attempts.length === 1) throw new Error("committed but response lost");
    return { ok: true, json: async () => ({ application: { title: "Engineer" } }) };
  };
  const message = { type: "markSubmitted", tab_id: 1, resume_decision: "not_tracked" };
  assert.strictEqual((await worker.send(message)).ok, false);
  assert.strictEqual((await worker.send(message)).ok, true);
  assert.strictEqual(captureCount, 1);
  assert.deepStrictEqual(attempts[0], attempts[1]);
  assert.deepStrictEqual(worker.memory.session, {});
}

async function testPopupPermissionUsesExactOriginAndUserGesture() {
  const clicks = {};
  const elements = {};
  const requested = [];
  const messages = [];
  const element = (selector) => elements[selector] ||= { value: "", style: {}, parentElement: {}, addEventListener(event, fn) { clicks[`${selector}:${event}`] = fn; } };
  element("#pairing-code").value = "code";
  element("#dashboard-base").value = "https://c.e.ts.net";
  const context = vm.createContext({
    JobDashboardConnection: connection,
    setInterval() {}, document: { querySelector: element }, window: { confirm: () => true },
    chrome: {
      storage: { local: { get: async () => ({}) } },
      tabs: { query: async () => [{ id: 1 }], sendMessage: async () => ({ supported: true, ats: "greenhouse", fields: [] }) },
      runtime: { sendMessage: async (value) => { messages.push(value); return { ok: true, active: false }; } },
      permissions: { request: (value) => { requested.push(value); return Promise.resolve(false); } }
    }
  });
  vm.runInContext(fs.readFileSync(path.join(root, "popup/popup.js"), "utf8"), context);
  await new Promise((resolve) => setImmediate(resolve));
  const pending = clicks["#fill:click"]();
  // The call happened synchronously, before the click handler yielded its gesture.
  assert.strictEqual(requested.length, 1);
  assert.strictEqual(requested[0].origins[0], "https://c.e.ts.net/*");
  await pending;
  assert.match(element("#notice").textContent, /access was denied/);
  assert(!messages.some((message) => message.type === "exchangeHandoff"));
  assert.strictEqual(element("#pairing-code").value, "");
}

function testGreenhouseConfirmationIdentity() {
  const tracking = require('./tracking.js');
  const original = 'https://job-boards.greenhouse.io/acme/jobs/12345';
  const job = tracking.identify(original);
  assert.deepStrictEqual(tracking.identify(original+'/confirmation?source=email'), job);
  assert.strictEqual(tracking.identify(original+'/verification'), null);
  assert.strictEqual(tracking.identify(original+'/confirmation/other'), null);
  assert(!tracking.sameJob(job, tracking.identify(original.replace('12345', '12346')+'/confirmation')));
  const doc = text => ({querySelectorAll: () => [{textContent:text, getClientRects:()=>[{}]}]});
  assert.strictEqual(tracking.outcome(doc('Thank you for applying.'), original+'/confirmation', job), 'success_dom');
  assert.strictEqual(tracking.outcome(doc('Enter the security code to submit your application.'), original, job), null);
  assert.strictEqual(tracking.outcome(doc(''), original+'/confirmation', job), null);
  const embedded = 'https://job-boards.greenhouse.io/embed/job_app?for=acme&token=12345';
  const confirmation = embedded.replace('job_app?', 'job_app/confirmation?');
  assert.deepStrictEqual(tracking.identify(embedded), job);
  assert.deepStrictEqual(tracking.identify(confirmation), job);
  assert.strictEqual(tracking.identify(confirmation.replace('/confirmation?', '/confirmation/other?')), null);
  assert.strictEqual(tracking.identify(confirmation.replace('12345', 'invalid')), null);
  assert(!tracking.sameJob(job, tracking.identify(confirmation.replace('12345', '54321'))));
  assert.strictEqual(tracking.outcome(doc('Thank you for applying.'), confirmation, job), 'success_dom');
  assert.strictEqual(tracking.outcome(doc(''), confirmation, job), null);
  const request = {method:'POST', url:'https://boards.greenhouse.io/embed/acme/jobs/12345'};
  assert(tracking.submissionRequest(request, job, true));
  for (const url of [request.url+'/other', request.url.replace('12345','54321'), request.url.replace('/acme/','/other/')])
    assert(!tracking.submissionRequest({...request,url},job,true));
  assert(!tracking.submissionRequest({...request,method:'GET'},job,true));
}

function testAshbyConfirmationContainers() {
  const tracking = require('./tracking.js');
  const url = 'https://jobs.ashbyhq.com/acme/00000000-0000-4000-8000-000000000001';
  const job = tracking.identify(url);
  const doc = ({success=false, failure=false, visible=true, text='SuccessThank you for applying to Acme!'}={}) => ({
    querySelectorAll: selector => {
      const node={textContent:text,getClientRects:()=>visible?[{}]:[]};
      if(selector === '.ashby-application-form-success-container [role="status"]') return success?[node]:[];
      if(selector === '.ashby-application-form-failure-container, .ashby-application-form-blocked-application-container') return failure?[node]:[];
      return [node];
    }
  });
  assert.equal(tracking.outcome(doc({success:true}),url,job),'success_dom');
  assert.equal(tracking.outcome(doc({success:true,text:'SuccessVielen Dank!'}),url,job),'success_dom');
  assert.equal(tracking.outcome(doc({success:true,visible:false}),url,job),null);
  assert.equal(tracking.outcome(doc(),url,job),null,'unscoped Success text is not acknowledgment');
  assert.equal(tracking.outcome(doc({success:true}),url.replace('000001','000002'),job),null,'a different job cannot acknowledge this attempt');
  assert.equal(tracking.outcome(doc({failure:true,text:"We couldn't submit your application"}),url,job),'validation_error');
}

function testAshbySubmissionRequests() {
  const tracking=require('./tracking.js');
  const job=tracking.identify('https://jobs.ashbyhq.com/acme/00000000-0000-4000-8000-000000000001');
  const request=op=>({method:'POST',url:`https://jobs.ashbyhq.com/api/non-user-graphql?op=${op}`});
  for(const op of ['ApiSubmitApplication','SubmitApplication','ApiSubmitSingleApplicationFormAction','ApiSubmitMultipleFormsAction']) {
    assert.equal(tracking.submissionRequest(request(op),job,true),true);
    assert.equal(tracking.submissionRequest({...request(op),method:'GET'},job,true),false);
  }
  for(const op of ['ApiSubmitSurveyFormAction','ApiSubmitRegistrationFormAction','ApiSubmitSourcingFormAction','ApiSubmitCandidateTextingConsent','ApiSubmitSingleApplicationFormActionDraft'])
    assert.equal(tracking.submissionRequest(request(op),job,true),false);
}

async function testDurableAnswerQueue() {
  const storage={},calls=[],accepted=new Map(); let loseAck=true;
  const paired={device_id:'fixture-device',device_token:'fictional-pairing-token'};
  const context=vm.createContext({crypto:require('node:crypto').webcrypto,TextEncoder,TextDecoder,Uint8Array,atob,btoa,
    JobTracking:require('./tracking.js'),trackingConnection:async()=>paired,
    chrome:{storage:{local:{
      get:async key=>key ? {[key]:storage[key]} : {...storage},
      set:async values=>Object.assign(storage,JSON.parse(JSON.stringify(values))),
      remove:async keys=>{for(const key of Array.isArray(keys)?keys:[keys]) delete storage[key];}
    }}},
    trackingPost:async(path,body)=>{
      assert.equal(path,'answers');calls.push(body);
      accepted.set(body.capture_id,JSON.stringify(body));
      if(loseAck) {loseAck=false;throw new Error('Response lost after server saved it');}
      return {saved:true,capture_id:body.capture_id,application_id:'fixture-application',field_count:body.snapshot.fields.length};
    }
  });
  vm.runInContext(fs.readFileSync(path.join(root,'answer_worker.js'),'utf8'),context);
  const page={tab_id:1,frame_id:0,attempt_id:'fixture-attempt',job:require('./tracking.js').identify('https://job-boards.greenhouse.io/acme/jobs/12345')};
  const field=(key,value)=>({field_key:key,prompt:key,section:'',control:'textarea',value});
  const snapshot=fields=>({version:1,fields,omitted_fields:0,truncated_values:0});
  await context.rememberAnswers(page,snapshot([field('Earlier step','PRIVATE-PROSE'),field('Cleared','old')]));
  await context.queueAnswers(page,snapshot([field('Cleared',''),field('Final step','final')]),'2026-10-01T00:00:00Z');
  assert(!JSON.stringify(storage).includes('PRIVATE-PROSE'));
  await context.flushAnswers();assert.equal(calls.length,0); // No server attempt yet.
  storage['tracking-result-fixture-attempt']={application_id:'fixture-application'};
  await context.flushAnswers();
  const keys=()=>Object.keys(storage).filter(k=>k.startsWith('tracking-answer-')&&!k.startsWith('tracking-answer-result-'));
  assert.equal(keys().length,1);assert(storage.tracking_answers_error);
  assert.equal(calls[0].snapshot.fields.find(f=>f.field_key==='Earlier step').value,'PRIVATE-PROSE');
  assert.equal(calls[0].snapshot.fields.find(f=>f.field_key==='Cleared').value,'');
  storage.tracking_answers_retry_at=0;
  await context.flushAnswers();
  assert.equal(accepted.size,1);assert.equal(calls[0].capture_id,calls[1].capture_id);
  assert.equal(keys().length,0);assert.equal(storage['tracking-answer-result-fixture-attempt'].field_count,3);
  assert(!storage.tracking_answers_error);
  const encrypted=await context.sealAnswers(paired,{value:'secret'});
  await assert.rejects(()=>context.openAnswers({...paired,device_id:'other'},encrypted),/different browser/);
  const other={...page,job:require('./tracking.js').identify('https://job-boards.greenhouse.io/acme/jobs/54321')};
  const clean=await context.rememberAnswers(other,snapshot([field('Different job','unrelated')]));
  assert.equal(clean.fields.length,1);
  storage['tracking-draft-1-0'].updated_at=0;
  await context.pruneAnswerDrafts();assert(!storage['tracking-draft-1-0']);
  const crowded=context.mergeAnswers(snapshot(Array.from({length:400},(_,i)=>({...field(String(i),'x'),control:'checkbox',value:true}))),snapshot([field('Important prose','kept first')]));
  assert.equal(crowded.fields.length,400);assert.equal(crowded.fields[0].value,'kept first');assert.equal(crowded.omitted_fields,1);
}

const tests = [
  testDurableAnswerQueue,
  testGreenhouseConfirmationIdentity,
  testAshbyConfirmationContainers,
  testAshbySubmissionRequests,
  testPrivateAndForbiddenGuardrails,
  testSafeInferenceAndApplication,
  testPrivateGroupsFillAndCaptureWithoutOverwrite,
  testAdaptersAreHostScoped,
  testManifestAndSourcesHaveNoSubmitCapability,
  testStrictDashboardOrigins,
  testPermissionAndRequestBoundary,
  testFailedPairingDoesNotPersistAndErrorsDiffer,
  testSuccessfulPairingAndPermissionRevocation,
  testSubmissionRetryDoesNotRestageUsedReceipt,
  testPopupPermissionUsesExactOriginAndUserGesture
];
(async () => {
  for (const test of tests) await test();
  console.log(`ok (${tests.length} Chromium extension tests)`);
})().catch((error) => { console.error(error); process.exitCode = 1; });
