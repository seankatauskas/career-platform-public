'use strict';
let csrf = '', selected = new URLSearchParams(location.search).get('application_id');
let applicationAfter = null;
const applicationPrevious = [];
const ownerHost = location.pathname === '/applications';
const node = (tag, text, cls) => { const n = document.createElement(tag); if (text != null) n.textContent = text; if (cls) n.className = cls; return n; };
async function request(path, payload) {
  if (ownerHost && payload === undefined && path !== '/api/v1/session' && !path.startsWith('/api/v1/applications/')) path = path.replace('/api/v1/', '/api/v1/application-owner/');
  const options = payload === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf, 'Idempotency-Key': crypto.randomUUID()}, body: JSON.stringify(payload)};
  const response = await fetch(path, options), result = await response.json();
  if (!response.ok) throw new Error(result.message || result.error);
  return result;
}
async function perform(operation, payload) {
  await request('/api/v1/application-commands/' + operation, payload);
  document.querySelector('#notice').textContent = 'Saved.';
  await refresh();
}
function guarded(fn) { return async event => { event?.preventDefault(); const control = event?.currentTarget; if (control?.tagName === 'BUTTON') control.disabled = true; try { await fn(event); } catch(error) { document.querySelector('#notice').textContent = error.message; } finally { if (control) control.disabled = false; } }; }
function details(value) {
  const dl = node('dl');
  for (const [key, item] of Object.entries(value || {})) {
    if (item == null || ['application_id','analysis_id','finding_id'].includes(key)) continue;
    dl.append(node('dt', key.replaceAll('_',' ')), node('dd', typeof item === 'object' ? JSON.stringify(item, null, 2) : String(item)));
  }
  return dl;
}
function button(label, fn, cls) { const b = node('button', label, cls); b.addEventListener('click', guarded(fn)); return b; }
function empty(container, text) { if (!container.childNodes.length) container.append(node('p', text, 'empty')); }
function workspacePage(container, page, applicationId, group, render) {
  for(const item of page.items) render(item);
  if(page.next_cursor) {
    const more=button('Load more '+group,async()=>{
      const next=await request('/api/v1/workspace-page?application_id='+encodeURIComponent(applicationId)+'&group='+encodeURIComponent(group)+'&cursor='+encodeURIComponent(page.next_cursor));
      more.remove();workspacePage(container,next,applicationId,group,render);
    },'secondary');
    container.append(more);
  }
}
async function refresh() {
  const list = await request('/api/v1/applications'+(applicationAfter?'?after='+encodeURIComponent(applicationAfter):'')), nav = document.querySelector('#applications'); nav.replaceChildren();
  for (const app of list.items) nav.append(button((app.job?.employer || 'Application') + ' · ' + (app.job?.title || app.progress.stage), async () => { selected = app.id; await refresh(); }, selected === app.id ? '' : 'secondary'));
  if(applicationPrevious.length) nav.append(button('Previous applications',async()=>{applicationAfter=applicationPrevious.pop();await refresh();},'secondary'));
  if(list.next_cursor) nav.append(button('Next applications',async()=>{applicationPrevious.push(applicationAfter);applicationAfter=list.next_cursor;await refresh();},'secondary'));
  if (!selected) return;
  const view = await request('/api/v1/workspace?application_id=' + encodeURIComponent(selected));
  const workspace = document.querySelector('#workspace'); workspace.replaceChildren(document.querySelector('#workspace-template').content.cloneNode(true));
  if(ownerHost) { workspace.querySelector('#delivery-note').textContent='Approval applies to the exact message or calendar change shown.'; const resumeLink=node('a','Resume and documents');resumeLink.href='/?application_workspace=resume#applications/'+encodeURIComponent(selected)+'/documents';workspace.prepend(resumeLink); }
  workspace.querySelector('#role').textContent = view.job?.title || 'Application';
  workspace.querySelector('#employer').textContent = view.job?.employer || '';
  workspace.querySelector('#stage').textContent = view.progress.stage;
  for(const warning of view.application.submission_summary?.warnings || []) workspace.querySelector('.application-heading').after(node('p',warning.message,'submission-warning'));
  const reviews = workspace.querySelector('#reviews'), selections = new Map();
  const acceptSelected=button('Accept selected changes',()=>perform('review_changes',{decisions:[...selections.values()]}));
  acceptSelected.disabled=true;
  workspacePage(reviews,view.review,view.application.id,'review',proposal=>{
    const card = node('article', null, 'card');
    if (!proposal.blockers.length) { const label=node('label'), checkbox=node('input'); checkbox.type='checkbox'; checkbox.addEventListener('change',()=>{ if(checkbox.checked) selections.set(proposal.id,{proposal_id:proposal.id,expected_version:proposal.version,decision:'accept'}); else selections.delete(proposal.id); acceptSelected.disabled=selections.size===0; }); label.append(checkbox,node('span','Include in selected changes'));card.append(label); }
    card.append(node('h4', proposal.operation.replaceAll('_',' ')), details(proposal.input));
    for (const evidence of proposal.evidence) if (evidence.quote) card.append(node('blockquote', evidence.quote));
    if (proposal.dependencies.length) card.append(node('p', 'Accept prerequisite changes before this change.'));
    if (proposal.blockers.length) card.append(node('p', 'Needs clarification: ' + proposal.blockers.join(', ')));
    else card.append(button('Accept change', () => perform('review_changes', {decisions: [{proposal_id: proposal.id, expected_version: proposal.version, decision: 'accept'}]})));
    card.append(button('Reject', () => perform('review_changes', {decisions: [{proposal_id: proposal.id, expected_version: proposal.version, decision: 'reject'}]}), 'secondary')); reviews.append(card);
  });
  empty(reviews,'No pending changes.');
  if(view.review.items.length) reviews.append(acceptSelected);
  const records=workspace.querySelector('#lifecycle-records');
  for(const kind of ['submissions','interviews','assessments','offers','reminders','schedules']) {
    const group=view.records[kind]; if(!group?.items.length) continue;
    const section=node('details');section.append(node('summary',kind[0].toUpperCase()+kind.slice(1)));
    workspacePage(section,group,view.application.id,kind,record=>section.append(details(record)));
    records.append(section);
  }
  const tasks = workspace.querySelector('#tasks');
  workspacePage(tasks,view.records.tasks,view.application.id,'tasks',task=>{
    const card = node('article', null, 'card'); card.append(node('p', task.description), node('small', task.status + (task.due_at ? ' · Due ' + task.due_at : '')));
    if (task.status === 'open') card.append(button('Complete task', () => perform('complete_task', {task_id: task.id, expected_version: task.version, reason: 'Completed by user'})));
    tasks.append(card);
  });
  empty(tasks,'No tasks.');
  const notes=workspace.querySelector('#notes');
  workspacePage(notes,view.records.notes,view.application.id,'notes',note=>notes.append(node('p',note.text)));
  workspace.querySelector('#new-note').addEventListener('submit',guarded(event => perform('add_note',{application_id:selected,text:new FormData(event.currentTarget).get('text')})));
  workspace.querySelector('#new-task').addEventListener('submit',guarded(event => perform('create_task',{application_id:selected,kind:'other',description:new FormData(event.currentTarget).get('description')})));
  const messages = workspace.querySelector('#messages');
  workspacePage(messages,view.conversation,view.application.id,'conversation',message=>{
    const card=node('article',null,'card');
    card.append(node('p',(message.direction === 'incoming' ? 'Received' : 'Outgoing') + ' · ' + (message.occurred_at || 'Time unknown')));
    const body=node('p',null,'message-body'), coverage=node('p',null,'meta');
    const applicationId=view.application.id;
    card.append(button('View message',async()=>{
      if(!ownerHost) { body.textContent='Message body is unavailable in this standalone workspace.';return; }
      const content=await request('/api/v1/applications/'+encodeURIComponent(applicationId)+'/conversation/'+encodeURIComponent(message.id));
      body.textContent=content.excerpt || (content.available ? 'This message has no text.' : 'Message body is unavailable.');
      coverage.textContent=content.coverage && !content.coverage.complete ? 'Some evidence is unavailable.' : '';
    },'secondary'),body,coverage);
    messages.append(card);
  });
  empty(messages,'No correspondence linked.');
  for (const item of (view.processing?.items || [])) workspace.querySelector('#coverage').append(node('p', item.status === 'failed' ? 'Evidence processing needs attention: ' + item.failure_code : (item.coverage?.complete ? 'Evidence processed.' : 'Some evidence is unavailable.')));
  const actions = workspace.querySelector('#actions');
  workspacePage(actions,view.actions,view.application.id,'actions',action=>{
    const card=node('article',null,'card'); card.append(node('h4',action.envelope.kind.replaceAll('_',' ')),details({account:action.envelope.account_id,target:action.envelope.target,...action.envelope.payload,internal_consequence:action.envelope.consequence,expires_at:action.expires_at}),node('p',action.authorization+' · '+action.execution));
    if (action.envelope.consequence) card.append(node('p','Verified sending will complete the linked task.'));
    if (action.authorization==='pending') card.append(button('Approve exact action',()=>perform('authorize_action',{action_id:action.action_id,expected_digest:action.digest})));
    actions.append(card);
  });
  empty(actions,'No external actions.');
  const close=workspace.querySelector('#close-application'), reopen=workspace.querySelector('#reopen-application');
  close.hidden=view.application.disposition==='closed'; reopen.hidden=!close.hidden;
  close.addEventListener('click',guarded(async()=>{
    const preview=await request('/api/v1/closure-preview?application_id='+encodeURIComponent(selected));
    const card=node('article',null,'card');card.append(node('p','Stop pursuing this application and cancel '+preview.records.length+' open tasks, pending reminders, and scheduled operations?'));
    card.append(button('Confirm stop pursuing',()=>perform('close_application',{application_id:selected,expected_version:preview.expected_version,expected_records:preview.expected_records,outcome:'stopped_pursuing',reason:'Stopped by user'})));close.replaceWith(card);
  }));
  reopen.addEventListener('click',guarded(()=>perform('reopen_application',{application_id:selected,expected_version:view.application.version,reason:'Reopened by user'})));
}
document.querySelector('#save-job').addEventListener('submit',guarded(async event=>{
  const values=Object.fromEntries(new FormData(event.currentTarget));
  const app=await request('/api/v1/application-commands/save_job',{job_source:{source:'manual',...values}});selected=app.id;await refresh();
}));
(async()=>{ const session=await request('/api/v1/session');csrf=session.csrf_token;
  if(ownerHost) { document.title='Applications · Career Platform'; document.querySelector('header .badge').textContent='Reviewed application workspace'; document.querySelector('#dashboard-link').hidden=false; }
  await refresh(); })().catch(error=>{document.querySelector('#notice').textContent=error.message;});
