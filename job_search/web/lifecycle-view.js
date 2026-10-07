// Lifecycle controls use the same service-backed briefing as Hermes.
function lifecycleField(form, label, name, choices, type = 'text') {
  const wrap = node('label', '', label);
  const input = node(choices ? 'select' : 'input');
  input.name = name; input.setAttribute('aria-label', label);
  if (choices) choices.forEach(([value, text]) => { const option = node('option', '', text); option.value = value; input.append(option); });
  else input.type = type;
  wrap.append(input); form.append(wrap); return input;
}
async function lifecycleCommand(operation, values, button) {
  if (button) button.disabled = true;
  try {
    const result = await api(`/api/v1/lifecycle/${operation}`, {method:'POST', body:JSON.stringify({...values, idempotency_key:key('lifecycle')})});
    await Promise.all([refreshApplicationWorkspace(), loadReviewQueue(), loadApplications()]);
    return result;
  } catch (error) {
    const feedback = document.querySelector('#workspace-feedback');
    feedback.textContent = error.message; feedback.hidden = false;
    notice(error.message);
    throw error;
  } finally { if (button) button.disabled = false; }
}
function lifecycleButton(label, operation, values) {
  const button = node('button', 'quiet', label); button.type = 'button';
  button.addEventListener('click', () => lifecycleCommand(operation, values, button).catch(() => {}));
  return button;
}
function renderLifecycleBriefing(briefing) {
  const root = document.querySelector('#workspace-lifecycle');
  root.replaceChildren();
  if (!briefing) return;
  const applicationId = briefing.application.application_id;
  root.append(node('h4', '', 'Next steps'), node('p', '', briefing.explanation));
  const coverage = briefing.coverage || {};
  const coverageText = coverage.complete ? 'Mail processing is current within its recorded coverage.' : 'Mail coverage is partial or unknown. Some updates may still be missing.';
  root.append(node('p', 'meta', coverageText));
  const successes=(coverage.connectors || []).map(c=>c.last_success_at).filter(Boolean).sort();
  if (successes.length) root.append(node('p', 'meta', `Last successful Outlook sync ${displayDate(successes.at(-1))}`));
  for (const task of briefing.tasks || []) {
    const row = node('article', 'lifecycle-item');
    row.append(node('h5', '', task.note || task.kind.replaceAll('_',' ')));
    row.append(node('p', 'meta', `${task.owner} · ${task.status}${task.due_at ? ` · Due ${displayDate(task.due_at)}` : ''}${task.snoozed_until ? ` · Snoozed until ${displayDate(task.snoozed_until)}` : ''}`));
    if (task.status === 'open') {
      const actions = node('div', 'actions');
      actions.append(lifecycleButton('Mark complete', 'tasks/transition', {task_id:task.task_id, operation:'complete', values:{}}), lifecycleButton('Cancel task', 'tasks/transition', {task_id:task.task_id, operation:'cancel', values:{}}));
      const snooze = node('input'); snooze.type = 'datetime-local'; snooze.setAttribute('aria-label', 'Snooze task until');
      const snoozeButton = node('button','quiet','Snooze'); snoozeButton.type='button';
      snoozeButton.addEventListener('click', () => { if (snooze.value) lifecycleCommand('tasks/transition', {task_id:task.task_id,operation:'snooze',values:{snoozed_until:new Date(snooze.value).toISOString().replace('.000Z','Z')}},snoozeButton).catch(()=>{}); });
      actions.append(snooze,snoozeButton); row.append(actions);
    }
    appendLifecycleHistory(row,'task',task.task_id);
    root.append(row);
  }
  if (briefing.application.current_phase !== 'terminal') {
    const add = node('details','lifecycle-form'); add.append(node('summary','','Add a next step'));
    const form = node('form','controls');
    const kind = lifecycleField(form,'Task','kind', [['reply','Reply'],['send_availability','Send availability'],['complete_assessment','Complete assessment'],['attend_interview','Attend interview'],['send_document','Send document'],['offer_decision','Decide on offer'],['follow_up','Follow up']]);
    const owner = lifecycleField(form,'Responsible party','owner',[['applicant','Me'],['employer','Employer'],['unknown','Unknown']]);
    const note = lifecycleField(form,'Description','note'); note.maxLength=2000; note.required=true;
    const due = lifecycleField(form,'Due (local time, optional)','due_at',null,'datetime-local');
    const submit = node('button','','Save next step'); submit.type='submit'; form.append(submit);
    form.addEventListener('submit', async event => { event.preventDefault(); const values={kind:kind.value,owner:owner.value,note:note.value}; if(due.value) values.due_at=new Date(due.value).toISOString().replace('.000Z','Z'); try { await lifecycleCommand('tasks/create',{application_id:applicationId,values},submit); } catch (_) {} });
    add.append(form); root.append(add);
  }
  const rounds = briefing.interviews?.rounds || [];
  if (rounds.length) {
    root.append(node('h4','','Interview rounds'));
    for (const round of rounds) {
      const row=node('article','lifecycle-item'); row.append(node('h5','',round.round_kind || 'Interview'),node('p','meta',`${round.status} · ${displayDate(round.starts_at)} · ${round.time_zone || ''}`));
      if (!['cancelled','completed'].includes(round.status)) {
        const actions=node('div','actions');
        for (const [status,label] of [['completed','Record completion'],['cancelled','Propose cancellation']]) actions.append(lifecycleButton(label,'interviews/propose',{application_id:applicationId,details:{round_id:round.round_id,status}}));
        row.append(actions);
      }
      root.append(row);
    }
  }
  for(const legacy of briefing.legacy_interviews || []) {
    const row=node('article','lifecycle-item');row.append(node('p','',`Saved interview · ${displayDate(legacy.starts_at)}`),lifecycleButton('Track interview changes','interviews/import',{schedule_id:legacy.interview_schedule_id}));root.append(row);
  }
  const meeting=node('details','lifecycle-form');meeting.append(node('summary','','Record or reschedule an interview'));
  const meetingForm=node('form','controls');
  const round=lifecycleField(meetingForm,'Interview round','round_id',[['','New round'],...rounds.map(r=>[r.round_id,`${r.round_kind} · ${displayDate(r.starts_at)}`])]);
  const roundKind=lifecycleField(meetingForm,'Round name','round_kind');roundKind.value='Interview';roundKind.maxLength=200;
  const start=lifecycleField(meetingForm,'Start (local time)','starts_at',null,'datetime-local');start.required=true;
  const end=lifecycleField(meetingForm,'End (local time)','ends_at',null,'datetime-local');end.required=true;
  const meetingSubmit=node('button','','Propose interview time');meetingSubmit.type='submit';meetingForm.append(meetingSubmit);
  meetingForm.addEventListener('submit',event=>{event.preventDefault();const details={round_kind:roundKind.value,status:round.value?'rescheduled':'confirmed',starts_at:new Date(start.value).toISOString().replace('.000Z','Z'),ends_at:new Date(end.value).toISOString().replace('.000Z','Z'),time_zone:Intl.DateTimeFormat().resolvedOptions().timeZone};if(round.value)details.round_id=round.value;lifecycleCommand('interviews/propose',{application_id:applicationId,details},meetingSubmit).catch(()=>{});});
  meeting.append(node('p','meta','The proposed change appears in Review. Saving it does not notify the employer.'),meetingForm);root.append(meeting);
  if ((briefing.reminders || []).length) {
    const section=node('details','lifecycle-form'); section.append(node('summary','','Reminders'));
    for(const reminder of briefing.reminders) {
      const row=node('div','lifecycle-item'); row.append(node('p','',`${reminder.note || reminder.kind || 'Reminder'} · ${displayDate(reminder.due_at)} · ${reminder.status}`));
      row.append(node('p','meta',`Notification: ${reminder.delivery_status || 'unknown'}. Delivery does not complete the next step.`));
      if (reminder.status === 'scheduled' || ['pending','delivering'].includes(reminder.delivery_status)) row.append(lifecycleButton('Cancel reminder','reminders/cancel',{reminder_id:reminder.reminder_id}));
      section.append(row);
    }
    root.append(section);
  }
  const followup=node('details','lifecycle-form'); followup.append(node('summary','','Follow-up preference'));
  const followForm=node('form','controls'); const days=lifecycleField(followForm,'Days after my last reply (0 disables)','after_days',null,'number'); days.min=0;days.max=90;days.value=briefing.follow_up?.after_days || 0;
  const save=node('button','quiet','Save preference');save.type='submit';followForm.append(save);
  followForm.addEventListener('submit',event=>{event.preventDefault();lifecycleCommand('follow-up/configure',{application_id:applicationId,after_days:Number(days.value)||null},save).catch(()=>{});});
  followup.append(node('p','meta','Creates a reminder when no newer employer reply has been observed. Silence never closes the application.'),followForm);root.append(followup);
  renderLifecycleDetails(root, briefing);
}
function renderLifecycleDetails(root, briefing) {
  const section=node('details','lifecycle-form'); section.append(node('summary','','Assessments and offers'));
  for(const item of briefing.details || []) {
    const row=node('article','lifecycle-item'); row.append(node('h5','',item.kind.replaceAll('_',' ')),node('p','meta',item.status));
    const values=item.details || {};
    for(const [key,value] of Object.entries(values)) if(value != null && typeof value !== 'object') row.append(node('p','',`${key.replaceAll('_',' ')}: ${value}`));
    const choices=item.kind==='offer'?[['negotiating','Negotiating'],['accepted','Accept offer'],['declined','Decline offer'],['expired','Offer expired'],['employer_withdrawn','Employer withdrew offer']]:[['submitted','Submitted'],['completed','Completed'],['cancelled','Cancelled']];
    const controls=node('div','actions');
    const status=lifecycleField(controls,'Record outcome','status',choices);
    const apply=node('button','quiet','Save outcome');apply.type='button';
    apply.addEventListener('click',()=>lifecycleCommand('details/record',{application_id:briefing.application.application_id,kind:item.kind,detail_id:item.detail_id,values:{status:status.value,expected_revision_no:item.revision_no}},apply).catch(()=>{}));controls.append(apply);row.append(controls);
    appendLifecycleHistory(row,'detail',item.detail_id);section.append(row);
  }
  const form=node('form','controls');
  const kind=lifecycleField(form,'Record type','kind',[['assessment','Assessment'],['offer','Offer']]);
  const title=lifecycleField(form,'Title','title'); title.required=true; title.maxLength=200;
  const note=lifecycleField(form,'Details','note'); note.maxLength=2000;
  const due=lifecycleField(form,'Decision or submission deadline','due_at',null,'datetime-local');
  const submit=node('button','','Save record'); submit.type='submit';form.append(submit);
  form.addEventListener('submit',event=>{event.preventDefault();const values={status:kind.value==='offer'?'offered':'requested',title:title.value,note:note.value};if(due.value)values.due_at=new Date(due.value).toISOString().replace('.000Z','Z');lifecycleCommand('details/record',{application_id:briefing.application.application_id,kind:kind.value,values},submit).catch(()=>{});});
  section.append(form);root.append(section);
}
function renderLifecycleReview(item, detail, actions) {
  const proposal=item.proposal || {};
  if(item.kind==='mail_discovery') {
    const observation=proposal.observation || {};
    detail.append(node('p','',observation.subject || 'Recruiting message'),node('p','meta',`${observation.sender || 'Sender unknown'} · ${observation.direction || 'Direction unknown'}`));
    if(observation.direction==='unknown') {
      detail.append(node('p','meta','Confirm which way this message was sent before using it as an incoming reply target.'));
      for(const [direction,label] of [['inbound','I received this message'],['outbound','I sent this message']]) {
        if(direction==='outbound'&&!observation.sent_at)continue;
        actions.append(lifecycleButton(label,'mail/direction',{observation_id:observation.observation_id,direction,reason:'Reviewed message direction in dashboard',expected_updated_at:observation.updated_at}));
      }
    }
    detail.append(node('p','','Link this recruiting conversation to an existing application, or review the employer and role to create a record.'));
    const form=node('div','controls');
    const employer=lifecycleField(form,'Employer','employer'); employer.value=proposal.employer || '';
    const title=lifecycleField(form,'Role','title'); title.value=proposal.title || '';
    const applications=new Map(state.applications.map(a=>[a.application_id,a]));
    for(const match of item.application_matches || []) {
      if(!applications.has(match.application_id)) applications.set(match.application_id,{
        application_id:match.application_id,employer_snapshot:match.employer,title_snapshot:match.title,
      });
    }
    const jobs=reviewJobMatches(item);
    const recommendedId=recommendedReviewSelection(item,[...applications.keys()]);
    const create=node('button',recommendedId?'quiet':'','Create application record');create.type='button';
    create.addEventListener('click',()=>lifecycleCommand('discoveries/decide',{discovery_id:item.id,decision:'create',employer:employer.value,title:title.value},create).catch(()=>{}));
    const matchIds=(item.application_matches || []).map(a=>a.application_id);
    const choices=[...applications.values()].sort((a,b)=>{
      const index=id=>{const value=matchIds.indexOf(id);return value<0?Infinity:value;};
      return index(a.application_id)-index(b.application_id);
    });
    const existing=lifecycleField(form,'Or link existing application','application_id',[['','Select application'],
      ...choices.map(a=>[a.application_id,`${a.employer_snapshot} · ${a.title_snapshot}${a.application_id===recommendedId?' (suggested)':''}`]),
      ...jobs.map(job=>[reviewJobValue(job),`${job.company} · ${job.title} · New application${reviewJobValue(job)===recommendedId?' (suggested)':''}`])]);
    existing.dataset.reviewApplication='true';
    existing.value=recommendedId;
    const link=node('button',recommendedId?'review-suggested-action':'quiet',selectedReviewJob(item,existing.value)?'Link job and create record':'Link application');link.type='button';link.disabled=!existing.value;
    existing.addEventListener('change',()=>{existing.dataset.reviewSelectionChanged='true';link.disabled=!existing.value;link.className=existing.value?'review-suggested-action':'quiet';create.className=existing.value?'quiet':'';link.textContent=selectedReviewJob(item,existing.value)?'Link job and create record':'Link application';});
    link.addEventListener('click',()=>{
      if(!existing.value)return;
      const job=selectedReviewJob(item,existing.value);
      const selection=job?{decision:'link_job',selected_job:{ats:job.ats,id:job.id}}:{decision:'link',application_id:existing.value};
      lifecycleCommand('discoveries/decide',{discovery_id:item.id,...selection},link).catch(()=>{});
    });
    detail.append(form);actions.append(link,create,lifecycleButton('Dismiss','discoveries/decide',{discovery_id:item.id,decision:'dismiss'}));
  } else {
    const values=proposal.payload || proposal.details || {};
    const describe=(object)=>{const list=node('dl','lifecycle-proposal');for(const [key,value] of Object.entries(object)){if(value===null || value==='' || ['calendar_account_id','calendar_event_id','calendar_uid','calendar_change_key','base_revision_id','base_round_status'].includes(key))continue; list.append(node('dt','',key.replaceAll('_',' '))); const dd=node('dd'); if(value && typeof value==='object'&&!Array.isArray(value))dd.append(describe(value));else dd.textContent=Array.isArray(value)?value.join(', '):String(value);list.append(dd);}return list;};
    detail.append(describe(values));
    if(item.status==='conflict') detail.append(node('p','notice','This change conflicts with the current schedule or a newer update. Review the latest application record before confirming.'));
    if(proposal.decision_reason) detail.append(node('p','meta',proposal.decision_reason));
    const operation=item.kind==='interview_revision'?'interviews/decide':'corrections/decide';
    actions.append(lifecycleButton('Confirm update',operation,{proposal_id:item.id,decision:'accepted'}),lifecycleButton('Reject',operation,{proposal_id:item.id,decision:'rejected'}));
  }
}

function renderLifecycleConversation(root, data) {
  root.replaceChildren(node('h4','','Conversation history'));
  const applicationId=data.application.application_id;
  const saved=new Map((data.messages || []).map(m=>[m.evidence_id,m]));
  const list=node('div');root.append(list);
  const appendPage=page=>{
    for(const message of page.items || []) {
      const card=node('article','message');
      card.append(node('h5','',message.subject || 'Untitled message'),node('p','meta',`${message.direction} · ${message.sender || 'Sender unknown'} · ${displayDate(message.source_at)}`));
      const evidence=saved.get(message.evidence_id);
      if(evidence)card.append(node('p','message-body',evidence.excerpt));
      else {
        const view=node('button','quiet','Read message excerpt');view.type='button';card.append(view);
        view.addEventListener('click',async()=>{view.disabled=true;try{const body=await api(`/api/v1/applications/${encodeURIComponent(applicationId)}/conversation/${encodeURIComponent(message.observation_id)}`);card.append(node('p','message-body',body.excerpt || 'No archived excerpt is available.'));if(!body.available)card.append(node('p','meta','Archive unavailable; showing saved evidence when available.'));view.remove();}catch(error){notice(error.message);view.disabled=false;}});
      }
      if(message.evidence_id)card.append(node('p','meta',`Evidence ${message.evidence_id}`));
      if(message.direction==='unknown') {
        const actions=node('div','actions');
        for(const [direction,label] of [['inbound','I received this message'],['outbound','I sent this message']]) {
          if(direction==='outbound'&&!message.sent_at)continue;
          actions.append(lifecycleButton(label,'mail/direction',{observation_id:message.observation_id,direction,reason:'Reviewed linked message in dashboard',expected_updated_at:message.updated_at}));
        }
        card.append(actions);
      }
      list.append(card);
    }
    if(page.next_cursor) {
      const more=node('button','quiet','Load older messages');more.type='button';list.append(more);
      more.addEventListener('click',async()=>{more.disabled=true;try{const next=await api(`/api/v1/applications/${encodeURIComponent(applicationId)}/conversation?cursor=${encodeURIComponent(page.next_cursor)}&limit=25`);more.remove();appendPage(next);}catch(error){notice(error.message);more.disabled=false;}});
    }
  };
  const page=data.briefing?.conversation;
  if(page?.items.length)appendPage(page);
  else for(const message of data.messages || []) {const card=node('article','message');card.append(node('h5','',message.subject),node('p','meta',`${message.sender} · ${displayDate(message.received_at)}`),node('p','message-body',message.excerpt));list.append(card);}
  if(!list.children.length)list.append(node('p','empty','No messages are linked to this application yet.'));
  root.append(node('p','meta','This history contains linked observations. Unprocessed or unlinked messages may still be missing.'));
  renderMailReplayControls(root);
}
function renderMailReplayControls(root) {
  const section=node('details','lifecycle-form');section.append(node('summary','','Review older Outlook history'));
  section.append(node('p','meta','Reprocess messages already collected in a chosen window of up to 366 days. Results appear in Review across applications. This does not fetch missing mailbox history or send messages.'));
  const status=node('div');const form=node('form','controls');
  const account=lifecycleField(form,'Outlook account','account_id',[]);
  const start=lifecycleField(form,'From (local time)','since_at',null,'datetime-local');start.required=true;
  const end=lifecycleField(form,'Until (local time)','until_at',null,'datetime-local');end.required=true;
  const submit=node('button','','Review collected history');submit.type='submit';form.append(submit);submit.disabled=true;
  section.append(form,status);root.append(section);
  const refresh=async()=>{
    const page=await api('/api/v1/lifecycle/replays');
    account.replaceChildren();for(const id of page.accounts || []){const option=node('option','',id);option.value=id;account.append(option);}
    submit.disabled=!account.options.length;
    status.replaceChildren();
    if(!account.options.length)status.append(node('p','meta','No collected Outlook accounts are available yet.'));
    for(const job of page.items || []) {
      const row=node('article','lifecycle-item');row.append(node('p','',`${job.status} · ${displayDate(job.since_at)} to ${displayDate(job.until_at)}`));
      if(job.last_error)row.append(node('p','meta',job.last_error));
      for(const [operation,label] of job.status==='failed'?[['retry','Retry'],['cancel','Cancel']]:['pending','running'].includes(job.status)?[['cancel','Cancel']]:[]) {
        const button=node('button','quiet',label);button.type='button';button.addEventListener('click',async()=>{button.disabled=true;try{await api('/api/v1/lifecycle/replay/transition',{method:'POST',body:JSON.stringify({replay_id:job.replay_id,operation,idempotency_key:key('replay')})});await refresh();}catch(error){notice(error.message);button.disabled=false;}});row.append(button);
      }
      status.append(row);
    }
    if(!page.complete)status.append(node('p','meta','Showing the latest replay requests.'));
  };
  section.addEventListener('toggle',()=>{if(section.open)refresh().catch(error=>notice(error.message));});
  const reload=node('button','quiet','Refresh replay status');reload.type='button';reload.addEventListener('click',()=>refresh().catch(error=>notice(error.message)));section.append(reload);
  form.addEventListener('submit',async event=>{event.preventDefault();submit.disabled=true;try{await api('/api/v1/lifecycle/replay/start',{method:'POST',body:JSON.stringify({account_id:account.value,since_at:new Date(start.value).toISOString(),until_at:new Date(end.value).toISOString(),idempotency_key:key('replay')})});await refresh();}catch(error){notice(error.message);submit.disabled=false;}});
}

function appendLifecycleHistory(root, kind, id) {
  const section=node('details');section.append(node('summary','','Change history'));root.append(section);
  const entries=node('div');section.append(entries);let loaded=false;
  const load=async(after=0)=>{
    const page=await api(`/api/v1/lifecycle/history/${kind}/${encodeURIComponent(id)}?after_revision=${after}`);
    for(const revision of page.items) {
      const state=revision.state || {};
      entries.append(node('p','meta',`Revision ${revision.revision_no} · ${state.status || ''} · ${revision.actor_kind || ''} · ${displayDate(revision.created_at || revision.recorded_at)}`));
      if(state.note)entries.append(node('p','',state.note));
    }
    if(page.next_revision){const more=node('button','quiet','More changes');more.type='button';entries.append(more);more.addEventListener('click',async()=>{more.disabled=true;try{await load(page.next_revision);more.remove();}catch(error){notice(error.message);more.disabled=false;}});}
  };
  section.addEventListener('toggle',async()=>{if(section.open&&!loaded){loaded=true;try{await load();}catch(error){loaded=false;notice(error.message);}}});
}
