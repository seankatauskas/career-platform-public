/* Owner Review uses the dashboard shell and the owners' explicit command contracts. */
const ownerReviewState = {items: [], pages: {}, loaded: false, error: '', epoch: 0, busy: false, stale: false, selections: new Map(), keys: new Map(), resolutions: new Map(), processingPending: new Map()};
const ownerOperationTitles = {add_note:'Add a note',create_task:'Create a task',record_request:'Record a request',record_submission:'Record a submission',confirm_submission:'Confirm application received',record_progress:'Update application progress',link_message:'Link this email',close_application:'Close application',reopen_application:'Reopen application',record_interview:'Record an interview',record_assessment:'Record an assessment',record_offer:'Record an offer',create_reminder:'Create a reminder',attach_submission_answers:'Attach application answers',send_reply:'Send reply',create_reply_draft:'Create reply draft',create_calendar_entry:'Create calendar entry'};
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
function ownerReviewFeedback(text) {
  const box = document.querySelector('#review-feedback'); box.textContent = text; box.hidden = !text;
}
function ownerReviewCount() {
  const badge = document.querySelector('#review-count');
  const count = ownerReviewNormalized().length;
  badge.textContent = count ? `${count}${ownerReviewHasMore() ? '+' : ''}` : ownerReviewState.error ? '—' : '';
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
    const result = await api('/api/v1/application-owner/review?limit=25' + (group ? '&group=' + encodeURIComponent(group) + '&cursor=' + encodeURIComponent(ownerReviewState.pages[group].next_cursor) : ''));
    if (epoch !== ownerReviewState.epoch) return;
    if (!Array.isArray(result.items)) throw new Error('The review response was incomplete.');
    const previous = group ? ownerReviewState.items : [];
    const rows = new Map([...previous, ...result.items].map(item => {
      const raw = item.proposal || item.action || item.processing || item;
      const id = item.kind === 'processing_history' ? item.id || raw.analysis_id : raw.issue_id || raw.id || raw.action_id || raw.analysis_id;
      return [`${item.kind || 'proposal'}:${id}`, item];
    }));
    ownerReviewState.items = [...rows.values()]; ownerReviewState.pages = group ? {...ownerReviewState.pages, ...result.pages} : result.pages || {};
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
    const body = node('p', 'message-body', ''), read = node('button', 'quiet', 'Read source email');
    read.addEventListener('click', async () => {
      read.disabled = true;
      try {
        const query = new URLSearchParams({source_id: source.source_id, revision: String(source.revision), sha256: source.sha256 || ''});
        const result = await api('/api/v1/application-owner/review-source?' + query);
        body.textContent = result.text || result.excerpt || result.body || (result.available ? 'This message has no text.' : 'This source is unavailable.');
        if (result.coverage?.complete === false || result.truncated) body.append(node('p', 'notice', 'Some source content is unavailable.'));
      } catch (error) { body.textContent = error.message; } finally { read.disabled = false; }
    });
    sourceDetails.append(read, body); content.append(sourceDetails);
  }
}
function ownerProposalCard(proposal, content, actions) {
  const visible = Object.fromEntries(Object.entries(proposal.input || {}).filter(([name]) => !/(?:_id|_ref|_version)$/.test(name) && !['evidence','expected_records'].includes(name)));
  content.append(ownerReviewFields(visible));
  const technical = node('details', 'technical-details'); technical.append(node('summary', '', 'Review record details'), ownerReviewFields({proposal_id:proposal.id,version:proposal.version,input:proposal.input})); content.append(technical);
  ownerReviewSources(content, proposal.evidence);
  const blockers = proposal.blockers || [], dependencies = proposal.dependencies || [];
  if (blockers.length) content.append(node('p', 'notice', 'Needs clarification: ' + blockers.map(ownerReviewLabel).join(', ')));
  if (dependencies.length) content.append(node('p', 'help', 'This change depends on earlier changes. Accept its prerequisites first, or select them together.'));
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
function ownerProcessingCard(processing, content, actions, historical = false) {
  if (processing.recorded_at) content.append(node('p', 'meta', 'Last attempt: ' + (typeof displayDate === 'function' ? displayDate(processing.recorded_at) : processing.recorded_at)));
  content.append(node('p', 'meta', ownerReviewLabel(processing.status)));
  if (processing.failure_code) content.append(node('p', 'notice', ownerProcessingFailures[processing.failure_code] || 'This email could not be fully processed. Its source and earlier attempts remain available.'));
  if (processing.coverage?.complete === false) content.append(node('p', 'help', 'Missing evidence: ' + (processing.coverage.reasons || []).map(ownerReviewLabel).join(', ')));
  if (processing.attempt_count) content.append(node('p', 'meta', `${processing.attempt_count} processing attempt${processing.attempt_count === 1 ? '' : 's'} retained`));
  const currentIssue = processing.current_issue || processing;
  if (historical && processing.current_issue) content.append(node('p', 'meta', 'Current processing status: ' + ownerReviewLabel(currentIssue.status)));
  if (currentIssue.resolution_reason) content.append(node('p', 'help', (currentIssue.status === 'resolved_manually' ? 'Resolution reason: ' : 'Latest review reason: ') + currentIssue.resolution_reason));
  for (const finding of processing.findings || []) {
    const section = node('details', 'technical-details'); section.append(node('summary', '', ownerReviewLabel(finding.category)), ownerReviewFields(finding.content)); content.append(section);
  }
  if (processing.findings_truncated) content.append(node('p', 'help', 'Additional findings are not shown on this page.'));
  ownerReviewSources(content, processing.sources);
  const details = node('details', 'technical-details'); details.append(node('summary', '', 'Processing record details'), ownerReviewFields({issue_id: processing.issue_id, analysis_id: processing.analysis_id, version: processing.version, failure_code: processing.failure_code})); content.append(details);
  if (!historical) ownerProcessingControls(processing, content, actions);
}
function renderOwnerReviewQueue() {
  const list = document.querySelector('#attention-list'); if (!list) return;
  if (!document.querySelector('#owner-processing-section')) {
    const heading = node('h3', 'owner-review-group-heading', 'Decisions'); heading.id = 'owner-review-decision-heading'; list.before(heading);
    const section = node('section', 'owner-processing-section'); section.id = 'owner-processing-section';
    section.append(node('h3', 'owner-review-group-heading', 'Processing problems'), node('p', 'help', 'Emails that could not be fully understood. Retrying preserves earlier attempts; resolving records your reason.'));
    const problems = node('div', 'stack'); problems.id = 'owner-processing-list'; section.append(problems); list.after(section);
  }
  const problems = document.querySelector('#owner-processing-list');
  const problemDisclosures = captureReviewDisclosures(problems); problems.replaceChildren();
  const disclosures = typeof captureReviewDisclosures === 'function' ? captureReviewDisclosures(list) : new Map();
  list.replaceChildren(); list.classList.remove('empty');
  for (const selector of ['#review-history']) { const element = document.querySelector(selector); if (element) element.hidden = true; }
  const history = document.querySelector('#mail-history-list'), historyRoot = document.querySelector('#mail-review-history');
  const historyDisclosures = captureReviewDisclosures(history); history.replaceChildren(); historyRoot.hidden = false;
  historyRoot.querySelector('summary').textContent = 'Email processing history';
  historyRoot.querySelector('.help').textContent = 'Earlier attempts and resolved problems are retained here. They do not count as current processing problems.';
  document.querySelector('#refresh-mail-history').hidden = true;
  const applicationId = new URLSearchParams(location.hash.split('?')[1] || '').get('application');
  const all = ownerReviewNormalized(), items = applicationId ? all.filter(item => item.applicationId === applicationId || (item.raw.candidate_ids || []).includes(applicationId)) : all;
  const problemItems = items.filter(item => item.kind === 'processing'), decisions = items.filter(item => item.kind !== 'processing');
  document.querySelector('#review-summary').textContent = `${decisions.length} decision${decisions.length === 1 ? '' : 's'} · ${problemItems.length} processing problem${problemItems.length === 1 ? '' : 's'}${ownerReviewHasMore() ? ' · More items available' : ''}`;
  if (!problemItems.length) problems.append(node('p', 'empty', ownerReviewState.pages.processing?.next_cursor ? 'More email processing records are available.' : 'No current processing problems in the loaded records.'));
  if (items.length && !decisions.length) list.append(node('p', 'empty', 'No application decisions in the loaded records.'));
  if (applicationId) { const link = node('a', 'review-context-link', 'Show all review items'); link.href = '#review'; list.append(link); }
  if (!items.length) list.append(node('p', 'empty', ownerReviewState.error ? 'Review could not be fully loaded. Refresh to try again.' : !ownerReviewState.loaded ? 'Loading review items…' : ownerReviewHasMore() ? 'No matching review items in the loaded pages. Load more to check the remaining items.' : 'Nothing needs review. New evidence and decisions will appear here.'));
  const route = location.hash.split('?')[0].split('/');
  if (route[1] && route[2] && !items.some(item => route[1] === encodeURIComponent(item.kind) && route[2] === encodeURIComponent(item.id))) list.append(node('p', 'notice', ownerReviewHasMore() ? 'The linked review is not in the loaded pages. Load more review items to find it.' : 'The linked review is no longer pending in this queue.'));
  const historyItems = ownerReviewNormalized(true).filter(item => ownerReviewIsHistory(item.entry) && (!applicationId || item.applicationId === applicationId || (item.raw.candidate_ids || []).includes(applicationId)));
  if (!historyItems.length) history.append(node('p', 'empty', 'No processing history in the loaded pages.'));
  for (const item of [...items, ...historyItems]) {
    const historical = ownerReviewIsHistory(item.entry);
    const card = node('article', 'stack-item review-card'), content = node('div', 'review-card-content'), header = node('header', 'review-card-header'), actions = node('div', 'actions review-card-actions');
    card.dataset.reviewKey = item.key; card.tabIndex = -1;
    header.append(node('p', 'review-card-kind', item.kind === 'external_action' ? 'External action' : ['processing','processing_history'].includes(item.kind) ? (historical ? 'Processing history' : 'Processing problem') : 'Application change'), node('h3', '', item.title));
    const app = item.entry.application;
    if (item.applicationId) { const link = node('a', 'review-context-link', [app?.job?.employer, app?.job?.title].filter(Boolean).join(' · ') || 'Open application'); link.href = '#applications/' + encodeURIComponent(item.applicationId); header.append(link); }
    else header.append(node('p', 'meta', 'Not linked to an application'));
    content.append(header);
    if (item.kind === 'external_action') ownerActionCard(item.raw, content, actions);
    else if (['processing','processing_history'].includes(item.kind)) ownerProcessingCard(item.raw, content, actions, historical);
    else ownerProposalCard(item.raw, content, actions);
    card.append(content); if (actions.childNodes.length) card.append(actions); (historical ? history : item.kind === 'processing' ? problems : list).append(card);
    if (typeof restoreReviewDisclosures === 'function') restoreReviewDisclosures(card, historical ? historyDisclosures : item.kind === 'processing' ? problemDisclosures : disclosures);
    if (route[1] === encodeURIComponent(item.kind) && route[2] === encodeURIComponent(item.id)) card.classList.add('review-selected');
  }
  if (ownerReviewState.selections.size) list.append(ownerReviewButton('Accept selected changes', 'review_changes', {decisions: [...ownerReviewState.selections.values()]}));
  for (const [group, page] of Object.entries(ownerReviewState.pages)) if (page.next_cursor) { const more = node('button', 'quiet', 'Load more ' + ({proposals:'application changes', external_actions:'external actions', processing:'processing problems', processing_history:'processing history'}[group] || group)); more.disabled = ownerReviewState.busy; more.addEventListener('click', () => loadOwnerReviewQueue(group)); (group === 'processing' ? problems : group === 'processing_history' ? history : list).append(more); }
  ownerReviewCount();
}
