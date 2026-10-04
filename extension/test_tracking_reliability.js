'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {webcrypto} = require('node:crypto');

function harness() {
  const storage={}, listeners={}, calls=[];
  const event=name=>({addListener(fn){listeners[name]=fn;}});
  const context=vm.createContext({crypto:webcrypto,TextEncoder,TextDecoder,Uint8Array,atob,btoa,URL,
    dashboardBase:value=>value, permissionOrigin:value=>value+'/*',
    chrome:{
      storage:{local:{setAccessLevel:async()=>{},
        get:async key=>structuredClone(key ? {[key]:storage[key]} : storage),
        set:async values=>Object.assign(storage,structuredClone(values)),
        remove:async keys=>{for(const key of Array.isArray(keys)?keys:[keys]) delete storage[key];}}},
      runtime:{onMessage:event('message'),onStartup:event('startup'),onInstalled:event('installed'),getURL:p=>'chrome-extension://fixture/'+p},
      action:{setBadgeText:async()=>{},setBadgeBackgroundColor:async()=>{}},
      alarms:{get:async()=>true,create:async()=>{},onAlarm:event('alarm')},
      tabs:{sendMessage:async()=>{throw new Error('Document already navigated');},onRemoved:event('tabRemoved')},
      webNavigation:{onHistoryStateUpdated:event('route')},
      webRequest:{onBeforeRequest:event('request'),onCompleted:event('completed'),onErrorOccurred:event('failed')},
      permissions:{contains:async()=>false,onRemoved:event('permissionRemoved')}
    },
    post:async(base,route,body)=>{calls.push({base,route,body});throw new Error('Offline fixture');}
  });
  context.importScripts=(...names)=>names.forEach(name=>vm.runInContext(fs.readFileSync(path.join(__dirname,name),'utf8'),context));
  context.importScripts('tracking_worker.js');
  const connection={base:'https://career.fixture.ts.net',device_id:'fixture-device',device_token:'synthetic-private-key'};
  storage.browser_connection=connection;
  const page={tab_id:1,frame_id:0,document_id:'document',attempt_id:'fixture-attempt',last_attempt:Date.now(),
    job:context.JobTracking.identify('https://job-boards.greenhouse.io/acme/jobs/12345')};
  const snapshot={version:1,omitted_fields:0,truncated_values:0,fields:[
    {field_key:'why',prompt:'Why?',section:'',control:'textarea',value:'Private synthetic answer.'}]};
  return {context,storage,listeners,calls,connection,page,snapshot};
}

async function pendingConnectionChangesPreserveAnswers() {
  for(const type of ['trackingDisconnect','trackingConnect']) {
    const h=harness();await h.context.queueAnswers(h.page,h.snapshot,new Date().toISOString());
    const before=structuredClone(h.storage);
    const disconnect={type,dashboard_base:h.connection.base,pairing_code:'unused'};
    if(type==='trackingDisconnect') {
      const result=await h.context.trackingPopup(disconnect);
      assert.equal(result.ok,false);assert.equal(result.discard_required,true);
    } else await assert.rejects(()=>h.context.trackingPopup(disconnect),/waiting to sync/);
    assert.deepEqual(h.storage,before);
    assert.equal(h.calls.length,0,'must not consume a pairing code before protecting pending answers');
  }
}

async function explicitDisconnectCanDiscardLocalData() {
  const h=harness();await h.context.queueAnswers(h.page,h.snapshot,new Date().toISOString());
  assert.equal((await h.context.trackingPopup({type:'trackingDisconnect',discard_pending:true})).ok,true);
  assert.equal(Object.keys(h.storage).length,0);
}

async function permissionRevocationDoesNotErasePendingAnswers() {
  const h=harness();await h.context.queueAnswers(h.page,h.snapshot,new Date().toISOString());
  const pending=Object.keys(h.storage).find(k=>k.startsWith('tracking-answer-'));
  h.listeners.permissionRemoved();
  await new Promise(resolve=>setImmediate(resolve));
  assert(h.storage[pending],'permission loss must preserve the encrypted retry payload');
  assert.deepEqual(h.storage.browser_connection,h.connection,'retain the decryption credential');
  await assert.rejects(()=>h.context.trackingPopup({type:'trackingConnect',restore_connection:true,dashboard_base:h.connection.base}),/not been restored/);
  h.context.chrome.permissions.contains=async()=>true;
  await assert.rejects(()=>h.context.trackingPopup({type:'trackingConnect',restore_connection:true,dashboard_base:'https://different.fixture.ts.net'}),/existing dashboard/);
  assert.equal((await h.context.trackingPopup({type:'trackingConnect',restore_connection:true,dashboard_base:h.connection.base})).ok,true);
  assert.deepEqual(h.storage.browser_connection,h.connection);
  assert(h.storage[pending]);
  assert(!h.calls.some(call=>call.route.endsWith('/enroll')),'restoring permission must not replace the pairing key');
}

async function incorrectSaveAcknowledgmentCannotDiscardAnswers() {
  const h=harness();await h.context.queueAnswers(h.page,h.snapshot,new Date().toISOString());
  h.storage['tracking-result-fixture-attempt']={application_id:'application-one'};
  h.context.post=async()=>({saved:true,capture_id:'wrong-capture',application_id:'application-two',field_count:1});
  await h.context.flushAnswers();
  assert(Object.keys(h.storage).some(k=>k.startsWith('tracking-answer-')&&!k.startsWith('tracking-answer-result-')));
  assert(h.storage.tracking_answers_error);
  assert(!h.storage['tracking-answer-result-fixture-attempt']);
  h.context.post=async(_base,_path,body)=>({saved:true,capture_id:body.capture_id,application_id:'application-one',field_count:body.snapshot.fields.length});
  delete h.storage.tracking_answers_retry_at;
  await h.context.flushAnswers();
  assert.equal(h.storage['tracking-answer-result-fixture-attempt'].saved,true);
  assert(!Object.keys(h.storage).some(k=>k.startsWith('tracking-answer-')&&!k.startsWith('tracking-answer-result-')));
}

async function requestFallbackPreservesDraftAfterNavigation() {
  const h=harness();delete h.page.attempt_id;delete h.page.last_attempt;
  h.storage['tracking-page-1-0']=h.page;
  await h.context.rememberAnswers(h.page,h.snapshot);
  await h.context.networkObservation({tabId:1,frameId:0,documentId:'document',requestId:'request',method:'POST',url:'https://job-boards.greenhouse.io/applications'},'request_sent');
  const queued=Object.entries(h.storage).find(([k])=>k.startsWith('tracking-answer-')&&!k.startsWith('tracking-answer-result-'));
  assert(queued);
  const body=await h.context.openAnswers(h.connection,queued[1]);
  assert.deepEqual(structuredClone(body.snapshot.fields),h.snapshot.fields);
  assert.equal(body.snapshot.omitted_fields,1,'a vanished final page cannot claim complete coverage');
  const observations=Object.values(h.storage).filter(v=>v?.item).map(v=>v.item.kind);
  assert(observations.includes('attempted'));assert(observations.includes('request_sent'));
  assert(!observations.includes('site_acknowledged'),'a request never proves submission acceptance');
  assert(!JSON.stringify(h.storage).includes('Private synthetic answer.'));
}

async function unrelatedRequestsCannotCreateAttempts() {
  for(const update of [{method:'GET'},{url:'https://job-boards.greenhouse.io/applications/draft'},{documentId:'other-document'},{frameId:1}]) {
    const h=harness();delete h.page.attempt_id;delete h.page.last_attempt;
    h.storage['tracking-page-1-0']=h.page;
    await h.context.networkObservation({tabId:1,frameId:0,documentId:'document',requestId:'request',method:'POST',url:'https://job-boards.greenhouse.io/applications',...update},'request_sent');
    assert(!Object.keys(h.storage).some(k=>k.startsWith('tracking-answer-')||k.startsWith('tracking-event-')));
  }
}

async function currentAshbyRequestsRecoverDirectApplicationsWithoutConfirmingThem() {
  for(const op of ['ApiSubmitSingleApplicationFormAction','ApiSubmitMultipleFormsAction']) {
    const h=harness();delete h.page.attempt_id;delete h.page.last_attempt;
    h.page.job=h.context.JobTracking.identify('https://jobs.ashbyhq.com/acme/00000000-0000-4000-8000-000000000001?source=external');
    h.storage['tracking-page-1-0']=h.page;
    const request={tabId:1,frameId:0,documentId:'document',requestId:'request',method:'POST',url:`https://jobs.ashbyhq.com/api/non-user-graphql?op=${op}`};
    await h.context.networkObservation(request,'request_sent');
    await h.context.networkObservation({...request,statusCode:200},'request_completed');
    const observations=Object.values(h.storage).filter(v=>v?.item).map(v=>v.item);
    assert(observations.some(v=>v.kind==='attempted'));
    assert(observations.some(v=>v.kind==='request_sent'));
    assert(observations.some(v=>v.kind==='request_completed'));
    assert(!observations.some(v=>v.kind==='site_acknowledged'),'HTTP 200 cannot confirm application acceptance');
    assert(observations.every(v=>v.page_url===h.page.job.canonical_url));
  }
}

const tests=[pendingConnectionChangesPreserveAnswers,explicitDisconnectCanDiscardLocalData,permissionRevocationDoesNotErasePendingAnswers,incorrectSaveAcknowledgmentCannotDiscardAnswers,requestFallbackPreservesDraftAfterNavigation,unrelatedRequestsCannotCreateAttempts,currentAshbyRequestsRecoverDirectApplicationsWithoutConfirmingThem];
(async()=>{
  let failures=0;
  for(const test of tests) {
    try {await test();console.log('ok '+test.name);} catch(error) {failures++;console.error(test.name,error);}
  }
  if(failures) process.exitCode=1;
  else console.log(`ok (${tests.length} tracking failure-recovery checks)`);
})();
