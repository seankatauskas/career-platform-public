'use strict';
// Pending answers are encrypted in trusted extension storage using the paired
// device credential. This survives worker/browser restarts, not a disconnect.
async function answerKey(connection) {
  const material = await crypto.subtle.importKey('raw',new TextEncoder().encode(connection.device_token),'HKDF',false,['deriveKey']);
  return crypto.subtle.deriveKey({name:'HKDF',hash:'SHA-256',salt:new TextEncoder().encode(connection.device_id),info:new TextEncoder().encode('career-platform-answer-queue-v1')},material,{name:'AES-GCM',length:256},false,['encrypt','decrypt']);
}
function answerBase64(bytes) { let out=''; for(const byte of new Uint8Array(bytes)) out+=String.fromCharCode(byte); return btoa(out); }
function answerBytes(value) { return Uint8Array.from(atob(value),c=>c.charCodeAt(0)); }
async function sealAnswers(connection, value) {
  const iv=crypto.getRandomValues(new Uint8Array(12));
  const encrypted=await crypto.subtle.encrypt({name:'AES-GCM',iv},await answerKey(connection),new TextEncoder().encode(JSON.stringify(value)));
  return {device_id:connection.device_id,iv:answerBase64(iv),ciphertext:answerBase64(encrypted)};
}
async function openAnswers(connection, value) {
  if(value.device_id!==connection.device_id) throw new Error('Saved answers belong to a different browser connection.');
  return JSON.parse(new TextDecoder().decode(await crypto.subtle.decrypt({name:'AES-GCM',iv:answerBytes(value.iv)},await answerKey(connection),answerBytes(value.ciphertext))));
}
function mergeAnswers(previous, next) {
  const fields = new Map((previous?.fields || []).map(f=>[f.field_key,f]));
  for(const field of next?.fields || []) fields.set(field.field_key,field);
  const rank = f=>['textarea','richtext'].includes(f.control)?0:f.control==='text'?1:2;
  const sorted = [...fields.values()].sort((a,b)=>rank(a)-rank(b));
  let bytes=0, omitted=0, keptCount=0;
  const kept=sorted.filter(field=>{
    const size=new TextEncoder().encode(JSON.stringify(field)).length;
    if(bytes+size>1024*1024 || keptCount>=400) {omitted++; return false;}
    bytes+=size; keptCount++;
    return true;
  });
  return {version:1,fields:kept,omitted_fields:Math.max(previous?.omitted_fields||0,next?.omitted_fields||0)+omitted,
    truncated_values:Math.max(previous?.truncated_values||0,next?.truncated_values||0)};
}
async function rememberAnswers(page, snapshot) {
  if (!snapshot || snapshot.version!==1 || !Array.isArray(snapshot.fields)) throw new Error('Invalid application answers.');
  const connection=await trackingConnection();
  const key=`tracking-draft-${page.tab_id}-${page.frame_id}`;
  const stored=(await chrome.storage.local.get(key))[key];
  const previous=stored && JobTracking.sameJob(stored.job,page.job) ? await openAnswers(connection,stored) : null;
  const merged=mergeAnswers(previous,snapshot);
  await chrome.storage.local.set({[key]:{...await sealAnswers(connection,merged),job:page.job,updated_at:Date.now()}});
  return merged;
}
async function queueAnswers(page, snapshot, capturedAt) {
  const merged=await rememberAnswers(page,snapshot);
  const connection=await trackingConnection();
  const captureId=crypto.randomUUID();
  const body={capture_id:captureId,attempt_id:page.attempt_id,page_url:page.job.canonical_url,captured_at:capturedAt,snapshot:merged};
  await chrome.storage.local.set({[`tracking-answer-${captureId}`]:{...await sealAnswers(connection,body),attempt_id:page.attempt_id,queued_at:Date.now()}});
}
async function flushAnswers() {
  const connection=await trackingConnection();
  if(!connection) return;
  const all=await chrome.storage.local.get(null);
  if(Number(all.tracking_answers_retry_at||0)>Date.now()) return;
  const entries=Object.entries(all).filter(([key])=>key.startsWith('tracking-answer-') && !key.startsWith('tracking-answer-result-')).sort((a,b)=>a[1].queued_at-b[1].queued_at);
  let failed=false;
  for(const [key,item] of entries) {
    // An answer can only be attached after its observation has created the attempt.
    if(!all[`tracking-result-${item.attempt_id}`]) continue;
    try {
      const body=await openAnswers(connection,item);
      const result=await trackingPost('answers',body);
      if(result.saved!==true || result.capture_id!==body.capture_id ||
          result.application_id!==all[`tracking-result-${item.attempt_id}`].application_id ||
          result.field_count!==body.snapshot.fields.length)
        throw new Error('The dashboard did not confirm saving these application answers.');
      await chrome.storage.local.set({[`tracking-answer-result-${item.attempt_id}`]:{saved:true,field_count:result.field_count,captured_at:body.captured_at,
        incomplete:!!(body.snapshot.omitted_fields||body.snapshot.truncated_values)}});
      await chrome.storage.local.remove(key);
    } catch(_) {
      failed=true;
      await chrome.storage.local.set({tracking_answers_error:'Application answers are saved in this browser and waiting to sync. Keep this browser connected.',tracking_answers_retry_at:Date.now()+60000});
    }
  }
  if(!failed) await chrome.storage.local.remove(['tracking_answers_error','tracking_answers_retry_at']);
}
async function pruneAnswerDrafts() {
  const all=await chrome.storage.local.get(null);
  const expired=Object.entries(all).filter(([key,value])=>key.startsWith('tracking-draft-') && Date.now()-value.updated_at>7*86400000).map(([key])=>key);
  if(expired.length) await chrome.storage.local.remove(expired);
}
