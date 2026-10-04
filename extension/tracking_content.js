(function() {
  'use strict';
  let job = null, connected = false, attempted = false, lastSignal = '', scheduled = false, digest = '', initialOutcome = null;
  let draftTimer=null, pendingDraft=null, pageEpoch=0, lastAttemptAt=0, lastAttemptSnapshot='';
  function describePage() {
    const title = document.querySelector('h1')?.textContent?.trim() || document.title;
    return {page_url:location.href, title:title.slice(0,500), employer:job?.board || '', resume_sha256:digest};
  }
  async function send(type, extra={}) {
    try { return await chrome.runtime.sendMessage({type,...describePage(),...extra}); } catch (_) { return null; }
  }
  async function page() {
    const epoch=++pageEpoch;
    const current = JobTracking.identify(location.href);
    if (!current) { job=null; pendingDraft=null; clearTimeout(draftTimer); return; }
    if (!JobTracking.sameJob(current,job)) { attempted=false; digest=''; lastSignal=''; initialOutcome=null; pendingDraft=null; lastAttemptAt=0; lastAttemptSnapshot=''; clearTimeout(draftTimer); }
    job=current;
    const result=await send('trackingPage');
    if(epoch!==pageEpoch) return;
    connected=!!result?.connected; attempted=attempted||!!result?.attempt_id;
    check();
  }
  function capture() {
    try {
      const selected=[JobAutofillGreenhouse,JobAutofillAshby,JobAutofillLever].find(a=>a.supports(location.hostname));
      if(!selected) return null;
      const form=selected.describe(document);
      return {fields:form.descriptors,answers:JobAutofillCommon.snapshot(form)};
    } catch (_) { return null; } // Legacy learning must not prevent answer history.
  }
  function snapshotDraft() {
    if(!job || !JobTracking.sameJob(job,JobTracking.identify(location.href))) return;
    const snapshot=JobAnswerCapture.collect(document);
    // Merge before a step's DOM disappears; explicit blank values replace old ones.
    const fields=new Map((pendingDraft?.fields||[]).map(field=>[field.field_key,field]));
    for(const field of snapshot.fields) fields.set(field.field_key,field);
    pendingDraft={...snapshot,fields:[...fields.values()]};
  }
  function flushDraft() {
    clearTimeout(draftTimer); draftTimer=null;
    if(pendingDraft?.fields.length) {
      const snapshot=pendingDraft; pendingDraft=null;
      send('trackingDraft',{answer_snapshot:snapshot});
    }
  }
  function changed() {
    snapshotDraft(); clearTimeout(draftTimer); draftTimer=setTimeout(flushDraft,300);
  }
  function attempt() {
    if(!job) return;
    snapshotDraft(); flushDraft();
    const snapshot=JobAnswerCapture.collect(document), signature=JSON.stringify(snapshot);
    // A native click is immediately followed by submit; retain only one identical
    // snapshot, but still capture values changed by a site's submit handler.
    if(Date.now()-lastAttemptAt<1500 && signature===lastAttemptSnapshot) return;
    lastAttemptAt=Date.now(); lastAttemptSnapshot=signature;
    initialOutcome=JobTracking.outcome(document,location.href,job);
    attempted=true; lastSignal='';
    send('trackingAttempt',{capture:capture(),answer_snapshot:snapshot,captured_at:new Date().toISOString()}).then(check);
  }
  function check() {
    if(!job || !connected || !attempted) return;
    const signal=JobTracking.outcome(document,location.href,job);
    if(signal && signal!==lastSignal && signal!==initialOutcome) {lastSignal=signal; send('trackingOutcome',{signal});}
  }
  function isFinalButton(button) {
    if(!button) return false;
    const label=(button.innerText||button.value||button.getAttribute('aria-label')||'').trim();
    const form=button.closest('form');
    const finalLabel=/^(?:submit(?: application)?|send application)$/i.test(label);
    const applyLabel=/^apply(?: now)?$/i.test(label) && form && button.type==='submit';
    return (finalLabel || applyLabel) && (form || document.querySelector('input[type="email"]'));
  }
  document.addEventListener('click',event=>{
    if(!event.isTrusted) return;
    const button=event.target.closest('button,input[type="submit"],[role="button"]');
    if(isFinalButton(button)) attempt();
    else if(button) { snapshotDraft(); flushDraft(); }
  },true);
  document.addEventListener('submit',event=>{
    const button=event.submitter || event.target.querySelector('button[type="submit"],input[type="submit"]');
    if(isFinalButton(button)) attempt();
  },true);
  document.addEventListener('invalid',()=>{if(attempted) send('trackingOutcome',{signal:'validation_error'});},true);
  document.addEventListener('input',changed,true);
  document.addEventListener('change',changed,true);
  document.addEventListener('change',async event=>{
    const input=event.target;
    if(input.type!=='file') return;
    const label=`${input.name||''} ${input.id||''} ${Array.from(input.labels||[]).map(l=>l.textContent).join(' ')}`;
    if(!/resume|cv|curriculum/i.test(label)) return;
    digest='';
    const file=input.files?.[0];
    if(!file || file.size>20*1024*1024) return;
    const bytes=await crypto.subtle.digest('SHA-256',await file.arrayBuffer());
    digest=Array.from(new Uint8Array(bytes)).map(b=>b.toString(16).padStart(2,'0')).join('');
    send('trackingPage');
  },true);
  new MutationObserver(()=>{
    if(scheduled) return;
    scheduled=true; setTimeout(()=>{scheduled=false; check();},200);
  }).observe(document.documentElement,{subtree:true,childList:true,characterData:true,attributes:true,attributeFilter:['hidden','class','style']});
  chrome.runtime.onMessage.addListener((message,_sender,respond)=>{
    if(message?.type==='trackingCapture') {
      snapshotDraft();
      const snapshot=pendingDraft || JobAnswerCapture.collect(document);
      pendingDraft=null; clearTimeout(draftTimer);
      attempted=true; initialOutcome=null; lastSignal='';
      respond({page_url:location.href,snapshot});
      setTimeout(check,0);
      return;
    }
    if(message?.type==='trackingRouteChanged' || message?.type==='trackingRefresh') page();
  });
  window.addEventListener('pageshow',page);
  window.addEventListener('pagehide',()=>{snapshotDraft(); flushDraft();});
  document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='hidden') {snapshotDraft();flushDraft();}});
  page();
})();
