// Owner records rendered inside the dashboard's existing application workspace.
// Commands retain the exact version shown; a stale view must refresh before retry.
function ownerApplicationFeedback(message) {
  const feedback = document.querySelector('#workspace-feedback');
  feedback.hidden = false; feedback.textContent = message;
}
function ownerApplicationButton(label, callback) {
  const button = node('button', 'quiet', label); button.type = 'button';
  button.addEventListener('click', async () => {
    button.disabled = true;
    try { await callback(); } catch (error) { ownerApplicationFeedback(error.message); }
    finally { button.disabled = false; }
  });
  return button;
}
async function ownerApplicationCommand(operation, payload, control) {
  // Keep the key after a lost response, but do not reuse it for changed input.
  const fingerprint = JSON.stringify({operation, payload});
  if (control.ownerCommand?.fingerprint !== fingerprint) control.ownerCommand = {fingerprint, key: key('application')};
  await api('/api/v1/application-commands/' + operation, {
    method: 'POST', headers: {'Idempotency-Key': control.ownerCommand.key}, body: JSON.stringify(payload),
  });
  delete control.ownerCommand;
  await loadApplications();
  ownerApplicationFeedback('Saved.');
}
function ownerApplicationDecision(label, operation, payload) {
  const button = ownerApplicationButton(label, () => ownerApplicationCommand(operation, payload, button));
  return button;
}
function ownerApplicationPage(container, page, applicationId, group, render) {
  for (const item of page?.items || []) render(item);
  if (!page?.next_cursor) return;
  const more = ownerApplicationButton('Load more ' + group, async () => {
    const next = await api('/api/v1/application-owner/workspace-page?application_id=' + encodeURIComponent(applicationId)
      + '&group=' + encodeURIComponent(group) + '&cursor=' + encodeURIComponent(page.next_cursor));
    more.remove(); ownerApplicationPage(container, next, applicationId, group, render);
  });
  container.append(more);
}
function ownerApplicationReviewLink(kind, id, applicationId, title) {
  const link = node('a', 'pending-note', title);
  link.href = '#review/' + kind + '/' + encodeURIComponent(id) + '?application=' + encodeURIComponent(applicationId);
  return link;
}
function renderOwnerApplicationReviews(data) {
  const root = document.querySelector('#workspace-review-notices'); root.replaceChildren();
  const appId = data.application.application_id;
  root.append(node('h4', '', 'Needs your review'));
  ownerApplicationPage(root, data.review, appId, 'review', proposal => {
    root.append(ownerApplicationReviewLink('proposal', proposal.id, appId, stageLabel(proposal.operation)));
    if (proposal.blockers?.length) root.append(node('p', 'meta', 'Needs clarification: ' + proposal.blockers.join(', ')));
  });
  if (!data.review?.items.length) root.append(node('p', 'meta', 'No pending application changes.'));
}
function ownerApplicationForm(root, title, fieldName, operation, applicationId, extra = {}) {
  const details = node('details', 'lifecycle-form'); details.append(node('summary', '', title));
  const form = node('form', 'owner-application-form'), label = node('label', '', title);
  const input = node('textarea'); input.name = fieldName; input.required = true; input.rows = 3;
  input.maxLength = fieldName === 'text' ? 10000 : 2000; label.append(input);
  const submit = node('button', '', title); submit.type = 'submit'; form.append(label, submit);
  form.addEventListener('submit', async event => {
    event.preventDefault(); submit.disabled = true;
    try { await ownerApplicationCommand(operation, {application_id: applicationId, ...extra, [fieldName]: input.value}, form); }
    catch (error) { ownerApplicationFeedback(error.message); }
    finally { submit.disabled = false; }
  });
  details.append(form); root.append(details);
}
function ownerApplicationRecord(record) {
  const card = node('article', 'lifecycle-item');
  card.append(node('h5', '', record.title || record.description || record.input?.description || stageLabel(record.kind || record.operation || record.status)));
  const interviewTime = record.start_at ? displayDate(record.start_at) + (record.end_at ? ' – ' + displayDate(record.end_at) : '') : '';
  const reminderTime = record.next_notification_at || record.at;
  const status = [record.status, interviewTime, record.timezone || '',
    record.occurred_at ? displayDate(record.occurred_at) : '',
    record.due_at ? 'Due ' + displayDate(record.due_at) : '',
    reminderTime ? 'Notification ' + displayDate(reminderTime) : '',
    record.snoozed_until ? 'Snoozed until ' + displayDate(record.snoozed_until) : '',
    record.responsible_party ? 'Responsible: ' + stageLabel(record.responsible_party) : '',
  ].filter(Boolean);
  card.append(node('p', 'meta', status.join(' · ')));
  for (const field of ['note', 'text', 'location', 'deadline_text', 'resolution_reason']) if (record[field]) card.append(node('p', 'answer-value', record[field]));
  if (record.participants?.length) card.append(node('p', 'meta', 'Participants: ' + record.participants.join(', ')));
  if (record.terms && Object.keys(record.terms).length) {
    const terms = node('dl', 'lifecycle-proposal');
    for (const [name, value] of Object.entries(record.terms)) terms.append(node('dt', '', stageLabel(name)), node('dd', 'answer-value', typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value)));
    card.append(terms);
  }
  return card;
}
function renderOwnerApplicationWorkspace(data) {
  const app = data.application, appId = app.application_id, root = document.querySelector('#workspace-lifecycle');
  root.replaceChildren();
  const tasks = node('section'); tasks.id = 'owner-tasks'; tasks.append(node('h4', '', 'Next steps'));
  ownerApplicationPage(tasks, data.records?.tasks, appId, 'tasks', task => {
    const card = ownerApplicationRecord(task);
    if (task.status === 'open' && app.disposition !== 'closed' && task.pursuit_no === app.pursuit_no) {
      const values = {task_id: task.id, expected_version: task.version, reason: 'Recorded by user'};
      const controls = node('div', 'actions');
      controls.append(ownerApplicationDecision('Mark complete', 'complete_task', values), ownerApplicationDecision('Cancel task', 'cancel_task', values));
      card.append(controls);
    }
    tasks.append(card);
  });
  if (!data.records?.tasks?.items.length) tasks.append(node('p', 'meta', 'No tasks recorded.'));
  if (app.disposition !== 'closed') ownerApplicationForm(tasks, 'Add a next step', 'description', 'create_task', appId, {kind: 'other'});
  const notes = node('section'); notes.id = 'owner-notes'; notes.append(node('h4', '', 'Notes'));
  ownerApplicationPage(notes, data.records?.notes, appId, 'notes', note => {
    const card = node('article', 'lifecycle-item'); card.append(node('p', 'answer-value', note.text), node('p', 'meta', displayDate(note.created_at))); notes.append(card);
  });
  if (!data.records?.notes?.items.length) notes.append(node('p', 'meta', 'No notes recorded.'));
  ownerApplicationForm(notes, 'Add a note', 'text', 'add_note', appId);
  root.append(tasks, notes);
  const records = node('section'); records.id = 'owner-records'; records.append(node('h4', '', 'Application lifecycle'));
  for (const kind of ['submissions', 'interviews', 'assessments', 'offers', 'reminders', 'schedules', 'progress']) {
    const page = data.records?.[kind]; if (!page?.items.length) continue;
    const section = node('details', 'lifecycle-form'); section.append(node('summary', '', stageLabel(kind)));
    ownerApplicationPage(section, page, appId, kind, record => section.append(ownerApplicationRecord(record))); records.append(section);
  }
  if (app.disposition === 'closed') {
    records.append(node('p', 'meta', 'Closed · ' + stageLabel(app.outcome)), ownerApplicationDecision('Reopen application', 'reopen_application', {application_id: appId, expected_version: app.version, reason: 'Reopened by user'}));
  } else {
    const close = ownerApplicationButton('Stop pursuing', async () => {
      const preview = await api('/api/v1/application-owner/closure-preview?application_id=' + encodeURIComponent(appId));
      const confirmation = node('div', 'lifecycle-item');
      confirmation.append(node('p', '', `Stop pursuing and cancel ${preview.records.length} open tasks, pending reminders, and scheduled operations?`));
      confirmation.append(ownerApplicationDecision('Confirm stop pursuing', 'close_application', {application_id: appId, expected_version: preview.expected_version, expected_records: preview.expected_records, outcome: 'stopped_pursuing', reason: 'Stopped by user'}));
      confirmation.append(ownerApplicationButton('Keep pursuing', async () => confirmation.replaceWith(close)));
      close.replaceWith(confirmation);
    }); records.append(close);
  }
  root.append(records);
  renderOwnerApplicationMessages(data);
  renderOwnerApplicationActions(data, root);
}
function renderOwnerApplicationMessages(data) {
  const root = document.querySelector('#workspace-messages'), appId = data.application.application_id;
  root.replaceChildren(node('h4', '', 'Correspondence'));
  ownerApplicationPage(root, data.conversation, appId, 'conversation', message => {
    const card = node('article', 'lifecycle-item');
    card.append(node('h5', '', message.subject || 'Linked message'), node('p', 'meta', `${message.direction === 'outgoing' ? 'Sent' : 'Received'} · ${displayDate(message.occurred_at)}`));
    const body = node('p', 'message-body'), coverage = node('p', 'meta');
    card.append(ownerApplicationButton('View message', async () => {
      const content = await api('/api/v1/applications/' + encodeURIComponent(appId) + '/conversation/' + encodeURIComponent(message.id));
      body.textContent = content.excerpt || (content.available ? 'This message has no text.' : 'Message body is unavailable.');
      coverage.textContent = content.coverage?.complete === false ? 'Some evidence is unavailable.' : '';
    }), body, coverage); root.append(card);
  });
  if (!data.conversation?.items.length) root.append(node('p', 'empty', 'No correspondence linked.'));
  for (const item of data.analysis_coverage?.items || []) {
    if (item.status === 'failed') root.append(node('p', 'section-note', 'Evidence processing needs attention.'
      + (item.failure_code ? ' ' + stageLabel(item.failure_code) + '.' : '')
      + (item.recorded_at ? ' Recorded ' + displayDate(item.recorded_at) + '.' : '')));
    else if (!item.coverage?.complete) root.append(node('p', 'section-note', 'Some evidence is unavailable.'));
  }
  if (data.analysis_coverage?.truncated) root.append(node('p', 'meta', 'Showing recent evidence processing history.'));
}
function renderOwnerApplicationActions(data, root) {
  const section = node('section'); section.id = 'owner-actions'; section.append(node('h4', '', 'External actions'));
  if (data.paused) section.append(node('p', 'section-note', 'External delivery is paused.'));
  ownerApplicationPage(section, data.actions, data.application.application_id, 'actions', action => {
    const card = node('article', 'lifecycle-item');
    card.append(node('h5', '', stageLabel(action.envelope.kind)), node('p', 'meta', `${stageLabel(action.authorization)} · ${stageLabel(action.execution)}`));
    if (action.authorization === 'pending' || action.execution === 'uncertain') card.append(ownerApplicationReviewLink('external_action', action.action_id, data.application.application_id, action.execution === 'uncertain' ? 'Review uncertain outcome' : 'Review exact action'));
    section.append(card);
  });
  if (!data.actions?.items.length) section.append(node('p', 'meta', 'No external actions recorded.'));
  ownerApplicationPage(section, data.results, data.application.application_id, 'results', result => {
    if (result.delivery === 'conflict') section.append(node('p', 'section-note', 'An external action completed, but its application update needs review. ' + (result.conflict_reason || '')));
  });
  root.append(section);
}
