/* Owner Review uses the dashboard shell and the owners' explicit command contracts. */
const ownerReviewState = {sources: new Map(), groups: [], totals: null, policy: null, items: [], pages: {}, loaded: false, error: '', epoch: 0, busy: false, stale: false, selections: new Map(), keys: new Map(), resolutions: new Map(), processingPending: new Map()};
const ownerOperationTitles = {record_submission_receipt:'Record application receipt',add_note:'Add a note',create_task:'Create a task',record_request:'Record a request',record_submission:'Record a submission',confirm_submission:'Confirm application received',record_progress:'Update application progress',link_message:'Link this email',close_application:'Close application',reopen_application:'Reopen application',record_interview:'Record an interview',record_assessment:'Record an assessment',record_offer:'Record an offer',create_reminder:'Create a reminder',attach_submission_answers:'Attach application answers',send_reply:'Send reply',create_reply_draft:'Create reply draft',create_calendar_entry:'Create calendar entry'};
function ownerReviewIsHistory(item) { return item.kind === 'processing_history' || item.kind === 'processing' && (item.processing?.status === 'resolved_manually' || item.processing?.status === 'succeeded' && item.processing?.coverage?.complete === true); }
function ownerReviewTitle(value) { return ownerOperationTitles[value] || ownerReviewLabel(value).replace(/^./, letter => letter.toUpperCase()); }
function ownerReviewLabel(value) { return String(value || '').replaceAll('_', ' '); }
function ownerReviewNormalized(includeHistory = false) {
  return ownerReviewState.items.filter(item => includeHistory || !ownerReviewIsHistory(item)).map(item => {
    const raw = item.proposal || item.action || item.processing || item;
    const kind = item.kind || 'proposal';
    const id = kind === 'processing_history' ? item.id || raw.analysis_id : raw.issue_id || raw.id || raw.action_id || raw.analysis_id;
    return {id, key: `${kind}:${id}`, kind, applicationId: raw.application_id || item.application_id || item.application?.id || null,
      title: kind === 'proposal' ? ownerReviewTitle(raw.operation) : kind === 'external_action' ? ownerReviewTitle(raw.envelope?.kind) : raw.subject || (kind === 'processing_history' ? 'Email processing attempt' : 'Email needs processing'),
      status: raw.status || raw.execution, raw, entry: item};
  });
}
function ownerReviewCardItems() {
  return [...ownerReviewState.groups.map(group => ({id:group.id,key:'evidence_group:'+group.id,kind:'evidence_group',
    applicationId:group.application?.id || null,applicationIds:group.applications.map(a=>a.id),
    title:group.proposals.length ? `${group.proposals.length} proposed change${group.proposals.length===1?'':'s'} from this evidence` : 'Email processing needs attention',
    status:'pending',raw:group})), ...ownerReviewNormalized().filter(item=>item.kind==='external_action')];
}
function ownerReviewFeedback(text) {
  const box = document.querySelector('#review-feedback'); box.textContent = text; box.hidden = !text;
}
function ownerReviewCount() {
  const badge = document.querySelector('#review-count');
  const count = ownerReviewState.totals?.cards ?? ownerReviewNormalized().length;
  badge.textContent = count ? `${count}` : ownerReviewState.error ? '—' : '';
  badge.title = ownerReviewState.error ? 'Review could not be fully refreshed' : ownerReviewHasMore() ? 'More review items are available' : '';
  if (typeof renderApplicationTable === 'function') renderApplicationTable();
  if (typeof renderApplicationReviewNotices === 'function') renderApplicationReviewNotices();
}
function ownerReviewHasMore() { return Object.entries(ownerReviewState.pages).some(([group,page]) => group !== 'processing_history' && page.next_cursor); }
async function loadOwnerReviewQueue(group = null) {
  const epoch = ++ownerReviewState.epoch;
  const refresh = document.querySelector('#refresh-attention'); refresh.disabled = true;
  const list = document.querySelector('#attention-list'); list.setAttribute('aria-busy', 'true');
  try {
    const historyOnly = group === 'processing_history';
    const groupQuery = new URLSearchParams({limit: '25'});
    if (group === 'groups') groupQuery.set('cursor', ownerReviewState.pages.groups.next_cursor);
    if (group === 'external_actions') groupQuery.set('action_cursor', ownerReviewState.pages.external_actions.next_cursor);
    const applicationId = new URLSearchParams(location.hash.split('?')[1] || '').get('application');
    if (applicationId) groupQuery.set('application_id', applicationId);
    const route = location.hash.split('?')[0].split('/');
    const deepLink = !group && route[1] && route[2] && ['evidence_group','proposal'].includes(decodeURIComponent(route[1])) ? decodeURIComponent(route[2]) : null;
    const historyQuery = '/api/v1/application-owner/review?limit=25&group=processing_history' + (historyOnly ? '&cursor=' + encodeURIComponent(ownerReviewState.pages.processing_history.next_cursor) : '');
    const [result, history, linked] = await Promise.all([
      historyOnly ? null : api('/api/v1/application-owner/review-groups?' + groupQuery),
      ['groups','external_actions'].includes(group) ? null : api(historyQuery),
      deepLink ? api('/api/v1/application-owner/review-groups?limit=25&group_id=' + encodeURIComponent(deepLink)) : null,
    ]);
    if (epoch !== ownerReviewState.epoch) return;
    if (result && !Array.isArray(result.items)) throw new Error('The review response was incomplete.');
    if (result) {
      const rows = new Map([...(group ? ownerReviewState.groups : []), ...result.items, ...(linked?.items || [])].map(item => [item.id, item]));
      ownerReviewState.groups = [...rows.values()];
      ownerReviewState.totals = result.totals; ownerReviewState.policy = result.policy;
      const histories = ownerReviewState.items.filter(ownerReviewIsHistory);
      const actions = new Map([...(group ? ownerReviewState.items.filter(i => i.kind === 'external_action').map(i => i.action) : []), ...(result.actions || [])].map(a => [a.action_id,a]));
      ownerReviewState.items = [...ownerReviewState.groups.flatMap(g => [
        ...g.proposals.map(proposal => ({kind:'proposal', proposal, application:g.applications.find(a => a.id === proposal.application_id)})),
        ...g.processing.map(processing => ({kind:'processing', processing, application:g.application})),
      ]), ...[...actions.values()].map(action => ({kind:'external_action', action})), ...histories];
      if (group !== 'external_actions') ownerReviewState.pages.groups = {next_cursor:result.next_cursor};
      if (group !== 'groups') ownerReviewState.pages.external_actions = {next_cursor:result.next_action_cursor};
    }
    if (history) {
      ownerReviewState.items = [...ownerReviewState.items.filter(item => !ownerReviewIsHistory(item)),
        ...(historyOnly ? ownerReviewState.items.filter(ownerReviewIsHistory) : []), ...history.items];
      ownerReviewState.pages.processing_history = history.pages.processing_history;
    }
    ownerReviewState.loaded = true; ownerReviewState.error = ''; ownerReviewState.stale = false;
    const current = new Map(ownerReviewNormalized().filter(item => item.kind === 'proposal').map(item => [item.id, item.raw]));
    for (const [id, decision] of ownerReviewState.selections) if (current.get(id)?.version !== decision.expected_version) ownerReviewState.selections.delete(id);
    for (const item of ownerReviewNormalized()) {
      const pending = ownerReviewState.processingPending.get(item.id);
      if (pending && (item.raw.version !== pending.payload.expected_version || item.raw.analysis_id !== pending.payload.expected_analysis_id || item.raw.status !== 'open')) ownerReviewState.processingPending.delete(item.id);
    }
    ownerReviewFeedback('');
  } catch (error) {
    if (epoch !== ownerReviewState.epoch) return;
    ownerReviewState.error = error.message; ownerReviewState.stale = true;
    ownerReviewFeedback('Review could not be fully refreshed. Previous items are kept; refresh before deciding. ' + error.message);
  } finally {
    if (epoch === ownerReviewState.epoch) { refresh.disabled = false; list.removeAttribute('aria-busy'); renderOwnerReviewQueue(); }
  }
}
async function ownerReviewCommand(operation, payload) {
  // A lost response must retry the same command. Storage contains only a hash and key.
  const serialized = JSON.stringify({operation, payload});
  let storageKey = null;
  if (globalThis.crypto?.subtle) {
    const bytes = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(serialized));
    storageKey = 'owner-review-command:' + [...new Uint8Array(bytes)].map(b => b.toString(16).padStart(2, '0')).join('');
  }
  let commandKey = ownerReviewState.keys.get(serialized);
  try { commandKey ||= storageKey && sessionStorage.getItem(storageKey); } catch (_) { /* In-memory retries remain stable. */ }
  commandKey ||= key('owner-review'); ownerReviewState.keys.set(serialized, commandKey);
  try { if (storageKey) sessionStorage.setItem(storageKey, commandKey); } catch (_) { /* Storage may be disabled. */ }
  const result = await api('/api/v1/application-commands/' + operation, {method: 'POST', headers: {'Idempotency-Key': commandKey}, body: JSON.stringify(payload)});
  ownerReviewState.keys.delete(serialized);
  try { if (storageKey) sessionStorage.removeItem(storageKey); } catch (_) { /* No persistent state is required. */ }
  return result;
}
function ownerReviewButton(label, operation, payload) {
  const button = node('button', 'quiet', label);
  button.disabled = ownerReviewState.busy || ownerReviewState.stale;
  button.addEventListener('click', async () => {
    if (ownerReviewState.busy || ownerReviewState.stale) return;
    ownerReviewState.busy = true; renderOwnerReviewQueue();
    try {
      await ownerReviewCommand(operation, payload);
      ownerReviewState.selections.clear();
      await loadOwnerReviewQueue();
      if (!ownerReviewState.error) ownerReviewFeedback('Saved.');
    } catch (error) {
      if (error.status === 409) { ownerReviewState.stale = true; ownerReviewState.selections.clear(); }
      ownerReviewFeedback(error.status === 409 ? 'This review changed. No new decision was applied by this request. Refresh and review the current version.' : error.message);
    } finally { ownerReviewState.busy = false; renderOwnerReviewQueue(); }
  });
  return button;
}
function ownerReviewFields(value) {
  const root = node('dl', 'owner-review-fields');
  for (const [name, item] of Object.entries(value || {})) {
    if (item === undefined || item === null) continue;
    root.append(node('dt', 'meta', ownerReviewLabel(name)));
    if (typeof item === 'object' && !Array.isArray(item)) { const definition = node('dd'); definition.append(ownerReviewFields(item)); root.append(definition); }
    else root.append(node('dd', 'message-body', Array.isArray(item) ? item.map(entry => typeof entry === 'object' ? JSON.stringify(entry) : String(entry)).join('\n') : (['kind','status','responsible_party','completion_rule','outcome'].includes(name) ? ownerReviewLabel(item) : String(item))));
  }
  return root;
}
function ownerReviewSources(content, sources) {
  const unique = new Map((sources || []).map(source => [`${source.owner}:${source.source_id}:${source.revision}:${source.start}:${source.end}:${source.quote}`, source]));
  for (const source of unique.values()) {
    if (source.quote) content.append(node('blockquote', 'review-evidence', source.quote));
    const sourceDetails = node('details', 'review-message');
    sourceDetails.append(node('summary', '', 'Source evidence'));
    sourceDetails.append(node('p', 'meta', `Revision ${source.revision ?? 'unknown'}${source.start != null ? ` · characters ${source.start}–${source.end}` : ''}`));
    if (source.owner !== 'correspondence') { sourceDetails.append(node('p', 'meta', (source.owner === 'applications' ? 'Browser observation · ' : 'Preserved source · ') + source.source_id)); content.append(sourceDetails); continue; }
    const sourceKey = JSON.stringify([source.source_id, source.revision, source.sha256]);
    const body = node('p', 'message-body', ownerReviewState.sources.get(sourceKey) || ''), read = node('button', 'quiet', 'Read source email');
    read.addEventListener('click', async () => {
      read.disabled = true;
      try {
        const query = new URLSearchParams({source_id: source.source_id, revision: String(source.revision), sha256: source.sha256 || ''});
        const result = await api('/api/v1/application-owner/review-source?' + query);
        body.textContent = result.text || result.excerpt || result.body || (result.available ? 'This message has no text.' : 'This source is unavailable.');
        if (result.coverage?.complete === false || result.truncated) body.append(node('p', 'notice', 'Some source content is unavailable.'));
        ownerReviewState.sources.set(sourceKey, body.textContent);
      } catch (error) { body.textContent = error.message; } finally { read.disabled = false; }
    });
    sourceDetails.append(read, body); content.append(sourceDetails);
  }
}
function ownerProposalCard(proposal, content, actions, grouped = false) {
  const visible = Object.fromEntries(Object.entries(proposal.input || {}).filter(([name]) => !/(?:_id|_ref|_version)$/.test(name) && !['evidence','expected_records'].includes(name)));
  content.append(ownerReviewFields(visible));
  const technical = node('details', 'technical-details'); technical.append(node('summary', '', 'Review record details'), ownerReviewFields({proposal_id:proposal.id,version:proposal.version,input:proposal.input})); content.append(technical);
  if (!grouped) ownerReviewSources(content, proposal.evidence);
  const blockers = proposal.blockers || [], dependencies = proposal.dependencies || [];
  if (blockers.length) {
    const descriptions = blockers.map(blocker => {
      if (blocker === 'needs_target') return proposal.application_id
        ? 'The target record could not be identified. Refresh the analysis before reviewing it.' : 'Which application is this email about?';
      if (blocker === 'unresolved_association') return proposal.application_id
        ? 'The email link needs repair. Refresh the analysis to rebuild it.' : 'Which application is this email about?';
      if (blocker === 'target_disagrees_with_selected_association') return 'The proposed application conflicts with the email link. Choose the correct application.';
      if (blocker === 'needs_time') return 'Confirm the date, time, and timezone before recording this update.';
      if (blocker.startsWith('compare_prior:')) return 'This interpretation differs from an earlier decision. Review the earlier decision before applying it.';
      if (blocker === 'source_revision_changed') return 'The email changed after analysis. Refresh the analysis to use the latest version.';
      if (blocker === 'incomplete_evidence_coverage') return 'Some supporting context could not be loaded. Analysis needs recovery.';
      return 'The proposed update could not be prepared correctly. Refresh the analysis before reviewing it.';
    });
    content.append(node('p', 'notice', [...new Set(descriptions)].join(' ')));
  }
  if (dependencies.length) content.append(node('p', 'help', 'Requires: ' + (proposal.prerequisites || []).map(p => ownerReviewTitle(p.operation) + ' (' + p.status + ')').join(', ') + '. Select prerequisites together or accept them first.'));
  if (!blockers.length) {
    const label = node('label', 'owner-review-selection'), checkbox = node('input'); checkbox.type = 'checkbox';
    checkbox.checked = ownerReviewState.selections.has(proposal.id);
    checkbox.disabled = ownerReviewState.busy || ownerReviewState.stale;
    checkbox.addEventListener('change', () => {
      if (checkbox.checked) ownerReviewState.selections.set(proposal.id, {proposal_id: proposal.id, expected_version: proposal.version, decision: 'accept'});
      else ownerReviewState.selections.delete(proposal.id);
      renderOwnerReviewQueue();
    });
    label.append(checkbox, document.createTextNode('Include this change')); content.append(label);
    actions.append(ownerReviewButton('Accept change', 'review_changes', {decisions: [{proposal_id: proposal.id, expected_version: proposal.version, decision: 'accept'}]}));
  }
  actions.append(ownerReviewButton('Reject change', 'review_changes', {decisions: [{proposal_id: proposal.id, expected_version: proposal.version, decision: 'reject'}]}));
}
function ownerActionCard(action, content, actions) {
  const envelope = action.envelope || {};
  content.append(node('p', 'meta', ownerReviewLabel(action.authorization) + ' · ' + ownerReviewLabel(action.execution)));
  content.append(ownerReviewFields({account: envelope.account_id, target: envelope.target, content: envelope.payload,
    internal_consequence: envelope.consequence, expires_at: action.expires_at}));
  if (envelope.consequence) content.append(node('p', 'help', 'Only a verified successful result can apply the linked application update.'));
  const exact = {action_id: action.action_id, expected_digest: action.digest};
  if (action.authorization === 'pending' && ['not_started', 'failed', 'cancelled'].includes(action.execution) && !action.cancellation_requested) {
    content.append(node('p', 'help', 'Approval authorizes exactly the account, recipients or calendar target, and content shown here.'));
    actions.append(ownerReviewButton('Approve exact action', 'authorize_action', exact), ownerReviewButton('Reject action', 'reject_action', exact));
  }
  if (['uncertain', 'needs_reconciliation', 'accepted', 'executing'].includes(action.execution)) {
    content.append(node('p', 'notice', 'The external outcome is not confirmed. Recovery must check the provider before another attempt.'));
    const recovery = node('a', 'quiet', 'View recovery status'); recovery.href = '#ops'; actions.append(recovery);
  }
  if (action.authorization === 'approved' && action.execution !== 'succeeded' && !action.cancellation_requested) {
    content.append(node('p', 'help', 'Stopping prevents further execution; it cannot undo an effect that already occurred.'));
    actions.append(ownerReviewButton('Stop further execution', 'revoke_action', exact));
  }
}
const ownerProcessingFailures = {
  context_budget_exceeded: 'The email and its context were too large to analyze in one request.',
  invalid_json: 'The analysis returned an unreadable response. No inferred application changes were accepted.',
  evidence_mismatch: 'The analysis cited words that could not be verified in the saved email. No inferred application changes were accepted.',
  output_truncated: 'The analysis response was cut off before it finished. No inferred application changes were accepted.',
  output_too_large: 'The analysis response exceeded the supported size. No inferred application changes were accepted.',
  provider_failed: 'The analysis service could not finish this attempt.',
  projection_failed: 'The analysis was saved, but proposed application changes could not be prepared.',
  projection_pending: 'The analysis was saved and is waiting for its proposed changes to be prepared.',
  invalid_output: 'The email analysis did not meet the required format. No inferred application changes were accepted.',
  incomplete_coverage: 'Some of the evidence needed to understand this email was unavailable.',
  relevance_uncertain: 'The analysis could not determine whether this email concerns your applications. Review the source before resolving it.',
  source_unavailable: 'The saved source email could not be read. Its evidence reference is still preserved.',
  provider_unavailable: 'The analysis service could not be reached.',
  provider_error: 'The analysis service could not finish processing this email.',
  model_unavailable: 'The configured analysis model is unavailable.',
  invalid_source: 'The source email could not be verified against its saved revision.',
};
async function ownerProcessingSubmit(issue, operation, reason, repeat = null) {
  if (ownerReviewState.busy || ownerReviewState.stale) return;
  const payload = repeat || {issue_id: issue.issue_id, expected_version: issue.version, expected_analysis_id: issue.analysis_id, reason: reason.trim()};
  if (!payload.reason) return;
  ownerReviewState.processingPending.set(issue.issue_id, {operation, payload, accepted: false});
  ownerReviewState.busy = true; renderOwnerReviewQueue();
  try {
    await ownerReviewCommand(operation, payload);
    const pending = ownerReviewState.processingPending.get(issue.issue_id);
    if (pending) pending.accepted = true;
    ownerReviewState.resolutions.delete(issue.issue_id);
    await loadOwnerReviewQueue();
    if (!ownerReviewState.error) ownerReviewFeedback(operation === 'retry_processing' ? 'Retry queued. Any proposed application changes still need review.' : 'Processing problem resolved with your reason. Earlier attempts are retained.');
  } catch (error) {
    if (error.status) ownerReviewState.processingPending.delete(issue.issue_id);
    if (error.status === 409) ownerReviewState.stale = true;
    ownerReviewFeedback(error.status === 409 ? 'This processing issue changed. Refresh before retrying or resolving it.' : error.message);
  } finally { ownerReviewState.busy = false; renderOwnerReviewQueue(); }
}
function ownerProcessingControls(issue, content, actions) {
  if (!issue.issue_id || !Number.isInteger(issue.version) || !issue.analysis_id) {
    content.append(node('p', 'help', 'Refresh to load the current processing issue before taking an action.')); return;
  }
  const pending = ownerReviewState.processingPending.get(issue.issue_id);
  if (pending) {
    content.append(node('p', 'notice', pending.accepted ? 'The request was accepted. Refresh to see its current status.' : 'The request outcome could not be confirmed. Check its status before making another decision.'));
    const check = node('button', 'quiet', 'Check processing status'); check.disabled = ownerReviewState.busy;
    check.addEventListener('click', () => loadOwnerReviewQueue()); actions.append(check);
    if (!pending.accepted) {
      const repeat = node('button', 'quiet', 'Retry same request'); repeat.disabled = ownerReviewState.busy || ownerReviewState.stale;
      repeat.addEventListener('click', () => ownerProcessingSubmit(issue, pending.operation, pending.payload.reason, pending.payload)); actions.append(repeat);
    }
    return;
  }
  const queued = ['retry_queued', 'retry_running', 'processing'].includes(issue.status);
  if (queued) content.append(node('p', 'notice', issue.status === 'retry_queued' ? 'Retry queued. Use Refresh to check progress.' : 'Retry is running. Use Refresh to check progress.'));
  if (!queued && issue.status !== 'open') return;
  const label = node('label', 'owner-processing-resolution', 'Reason for retry or resolution');
  const reason = node('textarea'); reason.rows = 2; reason.maxLength = 1000; reason.required = true;
  reason.value = ownerReviewState.resolutions.get(issue.issue_id) || ''; label.append(reason); content.append(label);
  content.append(node('p', 'help', 'Retry runs analysis again. Resolve closes this processing problem without accepting findings or changing application state. Both retain the earlier attempts.'));
  const retry = node('button', 'quiet', queued ? 'Retry already queued' : 'Retry processing');
  const resolve = node('button', 'quiet', 'Resolve processing problem');
  const update = () => { const disabled = ownerReviewState.busy || ownerReviewState.stale || !reason.value.trim(); retry.disabled = disabled || queued; resolve.disabled = disabled; };
  reason.disabled = ownerReviewState.busy || ownerReviewState.stale;
  reason.addEventListener('input', () => { ownerReviewState.resolutions.set(issue.issue_id, reason.value); update(); });
  retry.addEventListener('click', () => ownerProcessingSubmit(issue, 'retry_processing', reason.value));
  resolve.addEventListener('click', () => ownerProcessingSubmit(issue, 'resolve_processing', reason.value));
  update(); actions.append(retry, resolve);
}
function ownerProcessingCard(processing, content, actions, historical = false, grouped = false) {
  if (processing.recorded_at) content.append(node('p', 'meta', 'Last attempt: ' + (typeof displayDate === 'function' ? displayDate(processing.recorded_at) : processing.recorded_at)));
  content.append(node('p', 'meta', ownerReviewLabel(processing.status)));
  if (processing.failure_code) content.append(node('p', 'notice', ownerProcessingFailures[processing.failure_code] || 'This email could not be fully processed. Its source and earlier attempts remain available.'));
  if (processing.coverage?.complete === false) content.append(node('p', 'help', 'Missing evidence: ' + (processing.coverage.reasons || []).map(ownerReviewLabel).join(', ')));
  if (processing.validation_diagnostic) content.append(node('p', 'meta', 'Validation: ' + ownerReviewLabel(processing.validation_diagnostic.reason) + ' in ' + ownerReviewLabel(processing.validation_diagnostic.category) + ' finding ' + (processing.validation_diagnostic.position + 1)));
  if (processing.attempt_count) content.append(node('p', 'meta', `${processing.attempt_count} processing attempt${processing.attempt_count === 1 ? '' : 's'} retained`));
  const currentIssue = processing.current_issue || processing;
  if (historical && processing.current_issue) content.append(node('p', 'meta', 'Current processing status: ' + ownerReviewLabel(currentIssue.status)));
  if (currentIssue.resolution_reason) content.append(node('p', 'help', (currentIssue.status === 'resolved_manually' ? 'Resolution reason: ' : 'Latest review reason: ') + currentIssue.resolution_reason));
  for (const finding of processing.findings || []) {
    const section = node('details', 'technical-details'); section.append(node('summary', '', ownerReviewLabel(finding.category)), ownerReviewFields(finding.content)); content.append(section);
  }
  if (processing.findings_truncated) content.append(node('p', 'help', 'Additional findings are not shown on this page.'));
  if (!grouped) ownerReviewSources(content, processing.sources);
  const details = node('details', 'technical-details'); details.append(node('summary', '', 'Processing record details'), ownerReviewFields({issue_id: processing.issue_id, analysis_id: processing.analysis_id, version: processing.version, failure_code: processing.failure_code})); content.append(details);
  if (!historical) ownerProcessingControls(processing, content, actions);
}
function ownerGroupTarget(group, content) {
  if (group.source_type !== 'email' || !group.proposals.length) return;
  const details = node('details', 'review-target'); details.append(node('summary', '', 'Correct application or receipt scope'));
  const form = node('form'), targetLabel = node('label', '', 'Application'), select = node('select');
  const placeholder = node('option', '', 'Choose an application'); placeholder.value = ''; select.append(placeholder);
  select.required = true; targetLabel.append(select); form.append(targetLabel);
  const scopeLabel = node('label', '', 'Submission evidence'), scope = node('select');
  for (const [value, title] of [['preserve','Keep proposed attempt links'],['application','Application receipt; no attempt selected']]) {
    const option = node('option', '', title); option.value = value; scope.append(option);
  }
  scopeLabel.append(scope); form.append(scopeLabel);
  const reasonLabel = node('label', '', 'Reason for correction'), reason = node('textarea'); reason.required = true; reason.maxLength = 2000;
  reasonLabel.append(reason); form.append(reasonLabel);
  const save = node('button', 'quiet', 'Replace pending proposals'); save.type = 'submit'; save.disabled = true; form.append(save);
  const feedback = node('p', 'help', 'This prepares corrected proposals for review.'); form.append(feedback);
  let loaded = false;
  details.addEventListener('toggle', async () => {
    if (!details.open || loaded) return;
    try {
      let after = null;
      do {
        const page = await api('/api/v1/application-owner/applications?limit=100' + (after ? '&after=' + encodeURIComponent(after) : ''));
        for (const app of page.items) { const option = node('option', '', [app.job?.employer,app.job?.title].filter(Boolean).join(' · ') || app.id); option.value = app.id; select.append(option); }
        after = page.next_cursor || page.next_after;
      } while (after);
      select.value = group.application?.id || '';
      loaded = true; save.disabled = ownerReviewState.busy || ownerReviewState.stale;
    } catch (error) { feedback.textContent = error.message; }
  });
  form.addEventListener('submit', async event => {
    event.preventDefault(); if (ownerReviewState.busy || ownerReviewState.stale || !form.reportValidity()) return;
    ownerReviewState.busy = true; save.disabled = true;
    try {
      await ownerReviewCommand('correct_review_target', {group_id:group.id,expected_digest:group.preview_digest,application_id:select.value,reason:reason.value.trim(),receipt_scope:scope.value});
      ownerReviewState.selections.clear(); await loadOwnerReviewQueue();
      ownerReviewFeedback('Corrected proposals are ready for review.');
    } catch (error) { ownerReviewFeedback(error.message); if (error.status === 409) ownerReviewState.stale = true; }
    finally { ownerReviewState.busy = false; renderOwnerReviewQueue(); }
  });
  details.append(form); content.append(details);
}
function ownerEvidenceGroupCard(group) {
  const card = node('article', 'stack-item review-card evidence-group'), content = node('div', 'review-card-content');
  card.dataset.reviewKey = 'evidence_group:' + group.id; card.tabIndex = -1;
  content.append(node('p', 'review-card-kind', group.source_type === 'browser' ? 'Browser application attempt' : group.source_type === 'email' ? 'Email evidence' : 'Proposed application change'));
  const title = group.applications.map(a => [a.job?.employer,a.job?.title].filter(Boolean).join(' · ')).join(' / ');
  content.append(node('h3', '', title || 'Choose the application for this evidence'));
  content.append(node('p', 'meta', `${group.proposals.length} proposed change${group.proposals.length === 1 ? '' : 's'}${group.processing.length ? ' · Processing needs attention' : ''}`));
  for (const app of group.applications) { const link = node('a', 'review-context-link', 'Open application'); link.href = '#applications/' + encodeURIComponent(app.id); content.append(link); }
  // All quotes stay visible; the saved source is opened once per source/revision.
  for (const quote of new Set(group.proposals.flatMap(p => p.evidence.map(e => e.quote)).filter(Boolean))) content.append(node('blockquote', 'review-evidence', quote));
  ownerReviewSources(content, group.evidence.map(({quote, ...ref}) => ref));
  for (const proposal of group.proposals) {
    const row = node('section', 'review-proposed-change'), actions = node('div', 'actions');
    row.dataset.proposalId = proposal.id;
    row.append(node('h4', '', ownerReviewTitle(proposal.operation)));
    if (proposal.before?.application) row.append(node('p', 'meta', 'Current progress: ' + ownerReviewLabel(proposal.before.application.stage || proposal.before.application)));
    if (proposal.closure) row.append(node('p', 'notice', `Closing this application will cancel ${proposal.closure.records.length} open tasks, reminders, and scheduled operations.`));
    ownerProposalCard(proposal, row, actions, true); row.append(actions); content.append(row);
  }
  for (const issue of group.processing) { const row = node('section', 'review-proposed-change'), actions = node('div', 'actions'); row.append(node('h4', '', 'Processing problem')); ownerProcessingCard(issue, row, actions, false, true); row.append(actions); content.append(row); }
  if (group.source_type === 'email' && group.proposals.some(p => p.blockers.length) && !group.processing.some(p => p.status === 'retry_queued')) {
    content.append(ownerReviewButton('Refresh analysis', 'refresh_review_group', {group_id:group.id,expected_digest:group.preview_digest}));
  }
  const selected = group.proposals.filter(p => ownerReviewState.selections.has(p.id)).map(p => ownerReviewState.selections.get(p.id));
  if (selected.length) content.append(ownerReviewButton(`Accept ${selected.length} selected change${selected.length === 1 ? '' : 's'}`, 'review_changes', {decisions:selected}));
  ownerGroupTarget(group, content); card.append(content); return card;
}
function renderOwnerReviewQueue() {
  const list = document.querySelector('#attention-list'); if (!list) return;
  const disclosures = captureReviewDisclosures(list); list.replaceChildren(); list.classList.remove('empty');
  const oldProcessing = document.querySelector('#owner-processing-section'); if (oldProcessing) oldProcessing.remove();
  const oldHeading = document.querySelector('#owner-review-decision-heading'); if (oldHeading) oldHeading.remove();
  const legacyHistory = document.querySelector('#review-history'); if (legacyHistory) legacyHistory.hidden = true;
  const totals = ownerReviewState.totals || {cards:0,operations:0,processing:0};
  document.querySelector('#review-summary').textContent = `${totals.cards} evidence and action cards · ${totals.operations} proposed changes · ${totals.processing} processing problems`;
  const policy = ownerReviewState.policy;
  if (policy) {
    const settings = node('details', 'browser-recording-preference'); settings.append(node('summary', '', 'Browser recording: ' + (policy.enabled ? 'automatic' : 'review first')));
    settings.append(node('p', 'help', 'Automatically record unambiguous paired-browser submission attempts, website acknowledgments, and captured answers. Conflicting or incomplete evidence still needs review. Email changes remain manual.'));
    settings.append(ownerReviewButton(policy.enabled ? 'Review future browser evidence manually' : 'Enable automatic browser recording', 'configure_browser_recording', {enabled:!policy.enabled,expected_version:policy.version})); list.append(settings);
  }
  for (const group of ownerReviewState.groups) { const card = ownerEvidenceGroupCard(group); list.append(card); restoreReviewDisclosures(card, disclosures); }
  const actions = ownerReviewNormalized().filter(i => i.kind === 'external_action');
  for (const item of actions) { const card = node('article','stack-item review-card'), content = node('div','review-card-content'), buttons = node('div','actions'); card.dataset.reviewKey=item.key; content.append(node('h3','',item.title)); ownerActionCard(item.raw,content,buttons); card.append(content,buttons); list.append(card); }
  if (!ownerReviewState.groups.length && !actions.length) list.append(node('p','empty',ownerReviewState.error ? 'Review could not be loaded. Refresh to try again.' : ownerReviewState.loaded ? 'Nothing needs review.' : 'Loading review…'));
  const applicationId = new URLSearchParams(location.hash.split('?')[1] || '').get('application');
  if (applicationId) { const link=node('a','review-context-link','Show all review items'); link.href='#review'; list.append(link); }
  const history = document.querySelector('#mail-history-list'), historyRoot = document.querySelector('#mail-review-history');
  const previous = captureReviewDisclosures(history); history.replaceChildren(); historyRoot.hidden=false;
  historyRoot.querySelector('summary').textContent='Email processing history';
  historyRoot.querySelector('.help').textContent='Earlier attempts and resolved problems remain available here.';
  document.querySelector('#refresh-mail-history').hidden=true;
  for (const item of ownerReviewNormalized(true).filter(i => ownerReviewIsHistory(i.entry))) {
    const card=node('article','stack-item review-card'), content=node('div','review-card-content'), buttons=node('div','actions'); card.dataset.reviewKey=item.key;
    content.append(node('h3','',item.title)); ownerProcessingCard(item.raw,content,buttons,true); card.append(content); history.append(card); restoreReviewDisclosures(card,previous);
  }
  for (const [group,page] of Object.entries(ownerReviewState.pages)) if (page?.next_cursor) { const more=node('button','quiet',group==='groups'?'Load more evidence':group==='external_actions'?'Load more external actions':'Load more processing history'); more.disabled=ownerReviewState.busy; more.addEventListener('click',()=>loadOwnerReviewQueue(group)); (group==='processing_history'?history:list).append(more); }
  const route=location.hash.split('?')[0].split('/');
  if (route[2]) { const id=decodeURIComponent(route[2]); const selected=ownerReviewState.groups.find(g => g.id===id || g.proposals.some(p=>p.id===id)); if(selected) [...list.querySelectorAll('article')].find(c=>c.dataset.reviewKey==='evidence_group:'+selected.id)?.classList.add('review-selected'); }
  ownerReviewCount();
}
