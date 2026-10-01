'use strict';
importScripts('tracking.js');
const trackingReady = chrome.storage.local.setAccessLevel({accessLevel: 'TRUSTED_CONTEXTS'});
let trackingDrain = null;
const volatileCaptures = new Map();
const trackKey = (tab, frame) => `tracking-page-${tab}-${frame}`;
async function trackingConnection() { await trackingReady; return (await chrome.storage.local.get('browser_connection')).browser_connection; }
async function trackingPost(path, body) {
  const connection = await trackingConnection();
  if (!connection) throw new Error('Connect this browser in Career Platform Settings first.');
  return post(connection.base, `/api/v1/extension/${path}`, {...body, device_token: connection.device_token});
}
async function trackingBadge(tab, text, color = '#a99d83') {
  try { await chrome.action.setBadgeText({tabId: tab, text}); await chrome.action.setBadgeBackgroundColor({tabId:tab, color}); } catch (_) {}
}
async function trackingQueue(page, kind, metadata = {}) {
  const item = {observation_id: crypto.randomUUID(), attempt_id: page.attempt_id,
    page_url: page.job.canonical_url, kind, occurred_at: new Date().toISOString(),
    title: page.title, employer: page.employer, metadata: {...metadata, adapter_version:'browser-v1'},
    resume_sha256: page.resume_sha256 || ''};
  await chrome.storage.local.set({[`tracking-event-${item.observation_id}`]: {item, queued_at: Date.now()}});
}
async function flushTracking() {
  if (trackingDrain) return trackingDrain;
  trackingDrain = (async () => {
    if (!await trackingConnection()) return;
    const all = await chrome.storage.local.get(null);
    if (Number(all.tracking_retry_at || 0) > Date.now()) return;
    const entries = Object.entries(all).filter(([k]) => k.startsWith('tracking-event-'))
      .sort((a,b) => a[1].queued_at-b[1].queued_at || (a[1].item.kind === 'attempted' ? -1 : 1));
    for (const [key, queued] of entries) {
      try {
        const result = await trackingPost('observations', queued.item);
        await chrome.storage.local.remove(key);
        await chrome.storage.local.set({[`tracking-result-${result.attempt_id}`]: result});
        const capture = volatileCaptures.get(result.attempt_id);
        if (capture) {
          try { await trackingPost('capture', {attempt_id:result.attempt_id, ...capture}); } catch (_) { /* Tracking is independent of answer capture. */ }
          volatileCaptures.delete(result.attempt_id);
        }
      } catch (error) {
        const failures = Math.min(Number(all.tracking_retry_count || 0)+1,4);
        await chrome.storage.local.set({tracking_error:error.message, tracking_retry_count:failures, tracking_retry_at:Date.now()+Math.min(60000*2**(failures-1),300000)});
        return;
      }
    }
    await chrome.storage.local.remove(['tracking_error','tracking_retry_at','tracking_retry_count']);
  })().finally(() => { trackingDrain = null; });
  return trackingDrain;
}
async function trackingPage(message, sender) {
  if (!sender.tab || !Number.isInteger(sender.frameId)) throw new Error('Tracking requires an application frame.');
  const job = JobTracking.identify(sender.url);
  if (!job || !JobTracking.sameJob(job, JobTracking.identify(message.page_url))) throw new Error('Application frame identity mismatch.');
  if (!await trackingConnection()) return {ok:true, connected:false};
  const key = trackKey(sender.tab.id, sender.frameId);
  const old = (await chrome.storage.local.get(key))[key];
  const same = old && JobTracking.sameJob(old.job, job);
  const page = same ? {...old, document_id:sender.documentId} : {job, tab_id:sender.tab.id, frame_id:sender.frameId, document_id:sender.documentId};
  page.title = String(message.title || page.title || 'Job application').slice(0,500);
  page.employer = String(message.employer || page.employer || job.board).slice(0,500);
  // Metadata is bounded and credentials/answers are never persisted here.
  if (typeof message.resume_sha256 === 'string' && /^(?:[a-f0-9]{64})?$/.test(message.resume_sha256)) page.resume_sha256 = message.resume_sha256;
  if (message.type === 'trackingAttempt') {
    if (!page.attempt_id || Date.now()-(page.last_attempt || 0)>1500) {
      page.attempt_id = crypto.randomUUID(); page.last_attempt = Date.now(); page.success = false;
      await trackingQueue(page, 'attempted', {signal:'final_submit'});
    }
    if (message.capture && Array.isArray(message.capture.fields) && Array.isArray(message.capture.answers))
      volatileCaptures.set(page.attempt_id, {fields:message.capture.fields, answers:message.capture.answers});
    await trackingBadge(sender.tab.id, '…');
  } else if (message.type === 'trackingOutcome' && page.attempt_id) {
    const signal = message.signal;
    if (['success_dom','success_route'].includes(signal) && !page.success) {
      page.success = true;
      await trackingQueue(page, 'site_acknowledged', {signal});
    } else if (signal === 'validation_error' && !page.success) {
      await trackingQueue(page, 'failed', {signal});
      await trackingBadge(sender.tab.id, '!');
    }
  }
  await chrome.storage.local.set({[key]:page});
  await flushTracking();
  const result = page.attempt_id && (await chrome.storage.local.get(`tracking-result-${page.attempt_id}`))[`tracking-result-${page.attempt_id}`];
  if (result && ['site_acknowledged','email_confirmed'].includes(result.status)) await trackingBadge(sender.tab.id, '✓', '#628064');
  let resolved = null;
  if (message.type === 'trackingPage') {
    try { resolved = await trackingPost('resolve', {page_url:job.canonical_url}); } catch (_) {}
  }
  return {ok:true, connected:true, attempt_id:page.attempt_id, resolved, result};
}
let trackingSerial = Promise.resolve();
function serializeTracking(work) {
  const result = trackingSerial.then(work);
  trackingSerial = result.catch(() => {});
  return result;
}
async function trackingPopup(message) {
  if (message.type === 'trackingConnect') {
    const base = dashboardBase(message.dashboard_base);
    const result = await post(base, '/api/v1/extension/enroll', {pairing_code:message.pairing_code});
    await trackingReady;
    await chrome.storage.local.set({browser_connection:{base, ...result}, dashboard_base:base});
    await ensureTrackingAlarm();
    return {ok:true, connected:true};
  }
  if (message.type === 'trackingDisconnect') {
    await chrome.storage.local.remove('browser_connection');
    const keys = Object.keys(await chrome.storage.local.get(null)).filter(k=>k.startsWith('tracking-') || k==='tracking_error');
    await chrome.storage.local.remove(keys); volatileCaptures.clear();
    return {ok:true};
  }
  const connected = !!await trackingConnection();
  const all = await chrome.storage.local.get(null);
  const pages = Object.entries(all).filter(([k,p])=>k.startsWith('tracking-page-') && p.tab_id===message.tab_id);
  // Avoid choosing between different embedded applications in one tab.
  const distinct = new Set(pages.map(([,p])=>`${p.job.ats}:${p.job.job_id}`));
  const page = distinct.size===1 ? pages[0]?.[1] : null;
  if (message.type === 'trackingFill') {
    if (!page) throw new Error('Open one supported application form.');
    const form = await chrome.tabs.sendMessage(message.tab_id, {type:'describeForm'}, {frameId:page.frame_id});
    if (!JobTracking.sameJob(page.job, JobTracking.identify(form.page_url))) throw new Error('Application changed; refresh the popup.');
    const result = await trackingPost('assignments', {page_url:form.page_url, fields:form.fields});
    const applied = await chrome.tabs.sendMessage(message.tab_id, {type:'applyAssignments', assignments:result.assignments}, {frameId:page.frame_id});
    let resume;
    const target = page.document_id ? {documentId:page.document_id} : {frameId:page.frame_id};
    try {
      resume = await chrome.tabs.sendMessage(message.tab_id, {type:'inspectResumeUpload',page_url:form.page_url}, target);
      if (resume.status === 'ready') {
        const saved = await trackingPost('resume', {page_url:form.page_url});
        resume = saved.resume
          ? await chrome.tabs.sendMessage(message.tab_id, {type:'attachResume',page_url:form.page_url,resume:saved.resume}, target)
          : {status:'manual',message:'No saved PDF resume is available. Attach it manually.'};
      }
    } catch (error) { resume = {status:'manual',message:`Resume could not be attached: ${error.message}`}; }
    return {ok:true, filled:applied.filled, resume};
  }
  let result = page && all[`tracking-result-${page.attempt_id}`];
  if (page?.attempt_id && connected) {
    try { result = await trackingPost('status', {attempt_id:page.attempt_id}); } catch (_) {}
  }
  return {ok:true, connected, supported:!!page, job:page?.job, result, error:all.tracking_error,
    queued:Object.keys(all).filter(k=>k.startsWith('tracking-event-')).length};
}
chrome.runtime.onMessage.addListener((message, sender, respond) => {
  if (!message || !String(message.type).startsWith('tracking')) return false;
  const fromPage = ['trackingPage','trackingAttempt','trackingOutcome'].includes(message.type);
  // Content scripts cannot invoke popup actions or obtain the device credential.
  if (!fromPage && (!sender.url?.startsWith(chrome.runtime.getURL('popup/')))) {
    respond({ok:false,error:'Unsupported extension caller.'}); return false;
  }
  serializeTracking(()=>fromPage ? trackingPage(message,sender) : trackingPopup(message))
    .then(respond).catch(error=>respond({ok:false,error:error.message}));
  return true;
});
async function ensureTrackingAlarm() { if (!await chrome.alarms.get('tracking-retry')) await chrome.alarms.create('tracking-retry',{periodInMinutes:1}); }
chrome.alarms.onAlarm.addListener(alarm=>{ if(alarm.name==='tracking-retry') flushTracking().catch(()=>{}); });
chrome.runtime.onStartup.addListener(()=>{ ensureTrackingAlarm(); flushTracking().catch(()=>{}); });
chrome.runtime.onInstalled.addListener(()=>{ ensureTrackingAlarm(); });
chrome.webNavigation.onHistoryStateUpdated.addListener(details=>{
  if (JobTracking.identify(details.url)) chrome.tabs.sendMessage(details.tabId,{type:'trackingRouteChanged'},{frameId:details.frameId}).catch(()=>{});
}, {url: Object.values(JobTracking.hosts).flat().map(hostEquals=>({hostEquals}))});
const trackingRequestHosts = [...Object.values(JobTracking.hosts).flat(), 'boards-api.greenhouse.io'];
const trackingFilter = {urls:trackingRequestHosts.map(h=>`https://${h}/*`)};
async function networkObservation(details, phase) {
  if (details.tabId < 0 || !await trackingConnection()) return;
  const key = trackKey(details.tabId,details.frameId);
  const page = (await chrome.storage.local.get(key))[key];
  if (!page?.attempt_id || Date.now()-page.last_attempt > 120000 || page.success || !JobTracking.submissionRequest(details,page.job)) return;
  if (details.documentId && page.document_id && details.documentId!==page.document_id) return;
  const signal = phase==='failed' ? 'network_error' : 'application_request';
  await trackingQueue(page,phase,{signal,request_status:String(details.statusCode||'')});
  await flushTracking();
}
chrome.webRequest.onBeforeRequest.addListener(d=>{serializeTracking(()=>networkObservation(d,'request_sent')).catch(()=>{});},trackingFilter);
chrome.webRequest.onCompleted.addListener(d=>{serializeTracking(()=>networkObservation(d,d.statusCode>=400?'failed':'request_completed')).catch(()=>{});},trackingFilter);
chrome.webRequest.onErrorOccurred.addListener(d=>{serializeTracking(()=>networkObservation(d,'failed')).catch(()=>{});},trackingFilter);
chrome.tabs.onRemoved.addListener(tabId=>{ (async()=>{
  const all=await chrome.storage.local.get(null); await chrome.storage.local.remove(Object.keys(all).filter(k=>k.startsWith(`tracking-page-${tabId}-`)));
})().catch(()=>{}); });
chrome.permissions.onRemoved.addListener(()=>{ (async()=>{
  const c=await trackingConnection(); if(c && !await chrome.permissions.contains({origins:[permissionOrigin(c.base)]})) await trackingPopup({type:'trackingDisconnect'});
})().catch(()=>{}); });
