/* Owner Review uses the dashboard shell and the owners' explicit command contracts. */
const ownerReviewState = {sources: new Map(), groups: [], totals: null, policy: null, items: [], pages: {}, loaded: false, error: '', epoch: 0, busy: false, stale: false, selections: new Map(), keys: new Map(), resolutions: new Map(), processingPending: new Map()};
const ownerReviewWorkspace = {selected:null, index:0, filter:'all', mode:'focused', scrolls:new Map(), route:null, metadata:new Map(), sourceRequests:new Map()};
const ownerDiscoveryState = new Map();
const ownerTargetState = new Map();
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
  return [...ownerReviewState.groups.filter(group => group.proposals.length).map(group => ({id:group.id,key:'evidence_group:'+group.id,kind:'evidence_group',
    applicationId:group.application?.id || null,applicationIds:group.applications.map(a=>a.id),
    title:group.proposals.length ? `${group.proposals.length} proposed change${group.proposals.length===1?'':'s'} from this evidence` : 'Email processing needs attention',
    status:'pending',raw:group})), ...ownerReviewNormalized().filter(item=>item.kind==='external_action')];
}
function ownerReviewFeedback(text) {
  const box = document.querySelector('#review-feedback'); box.textContent = text; box.hidden = !text; box.setAttribute('role', 'status'); box.setAttribute('aria-live', 'polite');
}
function ownerReviewCount() {
  const badge = document.querySelector('#review-count');
  const count = ownerReviewState.totals?.decisions ?? ownerReviewCardItems().length;
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
      if (!group) for (const saved of ownerTargetState.values()) saved.loaded = false;
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
    const reviewedKey = ownerReviewWorkspace.selected;
    ownerReviewState.busy = true; renderOwnerReviewQueue();
    try {
      await ownerReviewCommand(operation, payload);
      ownerReviewState.selections.clear();
      await loadOwnerReviewQueue();
      if (!ownerReviewState.error) ownerReviewFeedback(reviewedKey && reviewedKey !== ownerReviewWorkspace.selected ? 'Saved. Next email ready for review.' : 'Saved.');
    } catch (error) {
      if (error.status === 409) { ownerReviewState.stale = true; ownerReviewState.selections.clear(); }
      ownerReviewFeedback(error.status === 409 ? 'This review changed. No new decision was applied by this request. Refresh and review the current version.' :
        error.status ? error.message : 'Could not confirm whether the update was saved. Use the same button to retry safely.');
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
function ownerReviewSources(content, sources, label = 'Source evidence') {
  const unique = new Map((sources || []).map(source => [`${source.owner}:${source.source_id}:${source.revision}:${source.start}:${source.end}:${source.quote}`, source]));
  for (const source of unique.values()) {
    if (source.quote) content.append(node('blockquote', 'review-evidence', source.quote));
    const sourceDetails = node('details', 'review-message');
    sourceDetails.append(node('summary', '', label));
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
function ownerReviewBundle(group, proposal) {
  if (!group || group.source_type !== 'email' || !proposal.application_id || proposal.blockers?.length || group.applications.length !== 1) return null;
  if (group.applications[0].id !== proposal.application_id || proposal.input?.application_id && proposal.input.application_id !== proposal.application_id) return null;
  if (group.association && group.association.application_id !== proposal.application_id) return null;
  const included = new Map([[proposal.id, proposal]]), visiting = new Set();
  function visit(item) {
    if (visiting.has(item.id)) return false;
    visiting.add(item.id);
    for (const id of item.dependencies || []) {
      const dependency = group.proposals.find(p => p.id === id) || (item.prerequisites || []).find(p => p.id === id);
      if (!dependency) return false;
      if (dependency.status === 'applied') continue;
      if (dependency.status !== 'pending' || dependency.operation !== 'link_message' || dependency.blockers?.length ||
          dependency.application_id !== proposal.application_id || dependency.input?.application_id !== proposal.application_id ||
          dependency.input?.message_id !== group.id.slice(6) || !group.proposals.some(p => p.id === id) || !visit(dependency)) return false;
      included.set(id, dependency);
    }
    visiting.delete(item.id); return true;
  }
  if (!visit(proposal) || included.size > 50 || [...included.values()].filter(p => p.operation === 'link_message').length > 1) return null;
  if (!group.association && ![...included.values()].some(p => p.operation === 'link_message')) return null;
  return [...included.values()];
}
function ownerGroupDecision(group, proposals, decision = 'accept') {
  return {group_id:group.id, expected_digest:group.preview_digest,
    decisions:proposals.map(p => ({proposal_id:p.id, expected_version:p.version, decision}))};
}
function ownerReviewPresentation(proposal) {
  const input = proposal.input || {};
  if (proposal.operation === 'record_submission_receipt') return ['Application received', 'Record receipt', 'Record this email as confirmation that your application was received.'];
  if (proposal.operation === 'confirm_submission') return ['Application received', 'Confirm receipt', 'Confirm the recorded submission using this email.'];
  if (proposal.operation === 'add_note') return ['Application note', 'Save note', input.note || input.text || input.description || 'Add this note to the application.'];
  if (proposal.operation === 'create_task') return ['Next step', 'Add task', input.description || input.title || 'Add this task to the application.'];
  if (proposal.operation === 'record_interview') return ['Interview update', 'Record interview', input.title || input.description || 'Record the interview details below.'];
  if (proposal.operation === 'record_request') return ['Action requested', 'Add task', input.description];
  if (proposal.operation === 'record_assessment') return ['Assessment update', 'Record assessment', input.description];
  if (proposal.operation === 'schedule_interview') return ['Interview update', 'Record interview', input.title || 'Record the interview details below.'];
  if (proposal.operation === 'close_application') {
    if (!Array.isArray(proposal.closure?.records)) return null;
    const labels = {rejected:['Application rejected','Record rejection'],accepted:['Offer accepted','Record acceptance'],
      withdrawn:['Application withdrawn','Record withdrawal'],stopped_pursuing:['Stop pursuing this role','Stop pursuing']};
    const label = labels[input.outcome];
    if (label) return [...label, 'Close this application as ' + ownerReviewLabel(input.outcome) + '.'];
  }
  return null;
}
function ownerCompactProposal(group, proposal, bundle, presentation) {
  const row = node('section', 'review-proposed-change review-outcome'), actions = node('div', 'actions review-outcome-actions');
  row.dataset.proposalId = proposal.id;
  row.append(node('h4', '', presentation[0]));
  const quote = proposal.evidence?.find(e => e.quote)?.quote;
  if (quote) row.append(node('blockquote', 'review-evidence', quote));
  row.append(node('p', 'review-result', presentation[2]));
  if (bundle.some(p => p.operation === 'link_message')) row.append(node('p', 'review-included', 'Also attaches this email to the application.'));
  if (proposal.operation !== 'close_application') row.append(ownerReviewFields(Object.fromEntries(Object.entries(proposal.input).filter(([k,v]) =>
    !/(?:_id|_ref|_version)$/.test(k) && !['evidence','expected_records','description','title','kind','completion_rule'].includes(k) &&
    (!['create_task','reminders_enabled'].includes(k) || v === true)))));
  if (proposal.closure) {
    row.append(node('p', 'notice', `Closing this application will cancel ${proposal.closure.records.length} open tasks, reminders, and scheduled operations.`));
    if (proposal.closure.records.length) {
      const affected = node('details'); affected.append(node('summary', '', 'View affected work'));
      for (const record of proposal.closure.records) affected.append(ownerReviewFields(record));
      row.append(affected);
    }
  }
  const accept = ownerReviewButton(presentation[1], 'review_changes', ownerGroupDecision(group, bundle)); accept.classList.remove('quiet');
  // Dismiss an included link only when no other pending update needs it.
  const dismissed = bundle.filter(p => p.id === proposal.id || !group.proposals.some(other =>
    other.id !== proposal.id && other.id !== p.id && (other.dependencies || []).includes(p.id)));
  actions.append(accept, ownerReviewButton('Dismiss update', 'review_changes', ownerGroupDecision(group, dismissed, 'reject')));
  row.append(actions);
  const details = node('details', 'review-bundle-details'); details.append(node('summary', '', 'Review details'));
  for (const item of bundle) {
    details.append(node('h5', '', ownerReviewTitle(item.operation)), ownerReviewFields({proposal_id:item.id,version:item.version,input:item.input}));
  }
  row.append(details); return row;
}
function ownerProposalCard(proposal, content, actions, grouped = false, catalogAvailable = false, group = null) {
  const visible = Object.fromEntries(Object.entries(proposal.input || {}).filter(([name]) => !/(?:_id|_ref|_version)$/.test(name) && !['evidence','expected_records'].includes(name)));
  content.append(ownerReviewFields(visible));
  const technical = node('details', 'technical-details'); technical.append(node('summary', '', 'Review record details'), ownerReviewFields({proposal_id:proposal.id,version:proposal.version,input:proposal.input})); content.append(technical);
  if (!grouped) ownerReviewSources(content, proposal.evidence);
  const blockers = proposal.blockers || [], dependencies = proposal.dependencies || [];
  if (blockers.length) {
    const descriptions = blockers.map(blocker => {
      if (catalogAvailable && !proposal.application_id && ['needs_target','unresolved_association'].includes(blocker))
        return 'Choose a catalog role below to create or reuse its application record.';
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
  const bundle = ownerReviewBundle(group, proposal);
  if (bundle?.some(p => p.id !== proposal.id)) content.append(node('p', 'help', 'Accepting this update also attaches the email to the application.'));
  else if (dependencies.length) content.append(node('p', 'help', 'Requires: ' + (proposal.prerequisites || []).map(p => ownerReviewTitle(p.operation) + ' (' + p.status + ')').join(', ') + '. Select prerequisites together or accept them first.'));
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
    actions.append(ownerReviewButton('Accept change', 'review_changes', group ? ownerGroupDecision(group, bundle || [proposal]) : {decisions: [{proposal_id: proposal.id, expected_version: proposal.version, decision: 'accept'}]}));
  }
  actions.append(ownerReviewButton('Reject change', 'review_changes', group ? ownerGroupDecision(group, [proposal], 'reject') : {decisions: [{proposal_id: proposal.id, expected_version: proposal.version, decision: 'reject'}]}));
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
  partial_analysis: 'Some findings need repair. Verified findings are available for your review; no application changes were approved.',
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
  const retry = node('button', 'quiet', queued ? 'Retry already queued' : 'Retry processing');
  retry.disabled = ownerReviewState.busy || ownerReviewState.stale || queued;
  retry.addEventListener('click', () => ownerProcessingSubmit(issue, 'retry_processing', 'User requested another analysis attempt from System attention.'));
  actions.append(retry);
  const details = node('details', 'technical-details'); details.append(node('summary', '', 'Resolve without retrying'));
  const saved = ownerReviewState.resolutions.get(issue.issue_id) || {disposition:'', note:''};
  const label = node('label', 'owner-processing-resolution', 'Resolution'), disposition = node('select');
  disposition.setAttribute('aria-label', 'Resolution');
  for (const text of ['', 'Already handled', 'Not related to my applications', 'No further analysis needed']) {
    const option = node('option', '', text || 'Choose a resolution'); option.value = text; disposition.append(option);
  }
  disposition.value = saved.disposition; label.append(disposition); details.append(label);
  const noteLabel = node('label', 'owner-processing-resolution', 'Optional explanation'), note = node('textarea');
  note.rows = 2; note.maxLength = 800; note.value = saved.note; noteLabel.append(note); details.append(noteLabel);
  details.append(node('p', 'help', 'Closes this system issue without accepting updates or changing the application. Earlier attempts remain in history.'));
  const resolve = node('button', 'quiet', 'Resolve processing problem');
  const update = () => {
    ownerReviewState.resolutions.set(issue.issue_id, {disposition:disposition.value, note:note.value});
    resolve.disabled = ownerReviewState.busy || ownerReviewState.stale || !disposition.value;
  };
  disposition.disabled = note.disabled = ownerReviewState.busy || ownerReviewState.stale;
  disposition.addEventListener('change', update); note.addEventListener('input', update);
  resolve.addEventListener('click', () => ownerProcessingSubmit(issue, 'resolve_processing', disposition.value + (note.value.trim() ? ': ' + note.value.trim() : '')));
  update(); details.append(resolve); content.append(details);
}
function ownerProcessingCard(processing, content, actions, historical = false, grouped = false) {
  if (processing.recorded_at) content.append(node('p', 'meta', 'Last attempt: ' + (typeof displayDate === 'function' ? displayDate(processing.recorded_at) : processing.recorded_at)));
  content.append(node('p', 'meta', ({open:'Needs attention',retry_queued:'Retry queued',retry_running:'Retry running',processing:'Analysis running'}[processing.status] || ownerReviewLabel(processing.status))));
  if (processing.failure_code) content.append(node('p', 'notice', ownerProcessingFailures[processing.failure_code] || 'This email could not be fully processed. Its source and earlier attempts remain available.'));
  if (processing.coverage?.complete === false) content.append(node('p', 'help', 'Missing evidence: ' + (processing.coverage.reasons || []).map(ownerReviewLabel).join(', ')));
  const currentIssue = processing.current_issue || processing;
  if (historical && processing.current_issue) content.append(node('p', 'meta', 'Current processing status: ' + ownerReviewLabel(currentIssue.status)));
  if (currentIssue.resolution_reason) content.append(node('p', 'help', (currentIssue.status === 'resolved_manually' ? 'Resolution reason: ' : 'Latest review reason: ') + currentIssue.resolution_reason));
  for (const finding of processing.findings || []) {
    const section = node('details', 'technical-details'); section.append(node('summary', '', ownerReviewLabel(finding.category)), ownerReviewFields(finding.content)); content.append(section);
  }
  if (processing.findings_truncated) content.append(node('p', 'help', 'Additional findings are not shown on this page.'));
  if (!grouped) ownerReviewSources(content, processing.sources);
  const details = node('details', 'technical-details'); details.append(node('summary', '', 'Processing record details'), ownerReviewFields({issue_id: processing.issue_id, analysis_id: processing.analysis_id, version: processing.version, failure_code: processing.failure_code, attempts_retained: processing.attempt_count, validation: processing.validation_diagnostic})); content.append(details);
  if (!historical) ownerProcessingControls(processing, content, actions);
}
function ownerGroupNeedsTarget(group) {
  return group.proposals.some(p => !p.application_id && p.blockers.some(b => ['needs_target', 'unresolved_association'].includes(b)));
}
function ownerApplicationLabel(app) {
  return [app.job?.employer, app.job?.title, app.job?.location].filter(Boolean).join(' · ') || app.id;
}
function ownerGroupTarget(group, content) {
  if (group.source_type !== 'email' || !group.proposals.length) return;
  const unresolved = ownerGroupNeedsTarget(group);
  let saved = ownerTargetState.get(group.id);
  if (!saved) {
    saved = {selected:'', search:'', scope:'preserve', apps:new Map(), loaded:false, loading:null, error:'', pending:null, none:false};
    ownerTargetState.set(group.id, saved);
  }
  for (const app of [...(group.application_candidates || []), ...group.applications]) saved.apps.set(app.id, app);
  const details = node('details', 'review-target'); details.open = unresolved;
  details.append(node('summary', '', unresolved ? 'Which role matches this email?' : 'Change application'));
  const form = node('form'), feedback = node('p', 'help'); feedback.setAttribute('role', 'status');
  form.append(node('p', 'help', 'Link this email to the role you applied for. Application updates remain a separate approval.'));
  const label = node('label', '', 'Search applications'), search = node('input'); search.type = 'search'; search.value = saved.search;
  label.append(search); form.append(label);
  const selected = node('p', 'review-target-selected'), choices = node('div', 'review-target-choices');
  choices.setAttribute('role', 'radiogroup'); choices.setAttribute('aria-label', 'Application');
  form.append(selected, choices);
  const scopeDetails = node('details', 'technical-details'); scopeDetails.append(node('summary', '', 'Receipt options'));
  const scopeLabel = node('label', '', 'Submission evidence'), scope = node('select');
  for (const [value, title] of [['preserve','Keep proposed attempt links'],['application','Application receipt; no attempt selected']]) {
    const option = node('option', '', title); option.value = value; scope.append(option);
  }
  scope.value = saved.scope; scope.addEventListener('change', () => {saved.scope = scope.value;});
  scopeLabel.append(scope); scopeDetails.append(scopeLabel); form.append(scopeDetails);
  const save = node('button', '', 'Continue'); save.type = 'submit';
  const none = node('button', 'quiet', 'None of these'); none.type = 'button';
  const discovery = node('div');
  function showDiscovery() {
    discovery.replaceChildren();
    if (!group.association && !group.applications.length) ownerGroupDiscovery(group, discovery);
    else discovery.append(node('p', 'help', 'Search all tracked applications above. To track a new role, add it in Applications, then refresh this review.'));
  }
  none.addEventListener('click', () => {saved.none = !saved.none; discovery.hidden = !saved.none; if (saved.none) showDiscovery(); draw();});
  if (saved.none) showDiscovery();
  function draw() {
    choices.replaceChildren();
    const locked = ownerReviewState.busy || ownerReviewState.stale || Boolean(saved.pending);
    search.disabled = scope.disabled = none.disabled = locked;
    for (const element of [label,selected,choices,scopeDetails,feedback,save]) element.hidden = saved.none;
    none.textContent = saved.none ? 'Search tracked applications instead' : 'None of these';
    const chosen = saved.apps.get(saved.selected);
    selected.textContent = chosen ? 'Selected: ' + ownerApplicationLabel(chosen) : 'No application selected.';
    const conflict = group.association && saved.selected && group.association.application_id !== saved.selected;
    save.disabled = ownerReviewState.busy || ownerReviewState.stale || (!saved.pending && (!chosen || conflict));
    save.textContent = saved.pending ? 'Retry same preview request' : 'Continue';
    // An uncertain preview must remain recoverable even if catalog search was opened.
    if (saved.pending) {save.hidden=false;feedback.hidden=false;}
    const terms = saved.search.toLocaleLowerCase().trim().split(/\s+/).filter(Boolean);
    const matches = [...saved.apps.values()].filter(app => terms.every(t => ownerApplicationLabel(app).toLocaleLowerCase().includes(t)));
    for (const app of matches.slice(0, 50)) {
      const row = node('label', 'review-target-option'), radio = node('input'); radio.type = 'radio'; radio.name = 'target-' + group.id; radio.value = app.id;
      radio.checked = saved.selected === app.id; radio.disabled = locked;
      radio.addEventListener('change', () => {saved.selected = app.id; draw(); choices.querySelector('input:checked')?.focus({preventScroll:true});});
      row.append(radio, node('span', '', ownerApplicationLabel(app))); choices.append(row);
    }
    feedback.textContent = saved.pending ? 'The preview request could not be confirmed. Retry the same request before changing your selection.'
      : conflict ? 'This email is already attached to another application. Changing an accepted link requires association correction and review of its earlier consequences.'
      : saved.error || (saved.loading ? 'Loading tracked applications…' : matches.length > 50 ? 'Showing 50 matches. Refine your search to find a role.' : !matches.length ? 'No matching applications. Try another search or choose None of these.' : 'No update is accepted by Continue.');
  }
  async function load() {
    if (saved.loaded || saved.loading) { draw(); return; }
    saved.error = '';
    saved.loading = (async () => {
      let after = null; const seen = new Set(), apps = new Map([...(group.application_candidates || []), ...group.applications].map(app => [app.id, app]));
      do {
        const page = await api('/api/v1/application-owner/applications?limit=100' + (after ? '&after=' + encodeURIComponent(after) : ''));
        for (const app of page.items) apps.set(app.id, app);
        after = page.next_cursor || page.next_after;
        if (after && (seen.has(after) || seen.size >= 100)) throw new Error('Application list is incomplete. Refine your records in Applications and refresh to try again.');
        seen.add(after);
      } while (after);
      saved.apps = apps; saved.loaded = true;
    })();
    draw();
    try { await saved.loading; } catch (error) { saved.error = error.message; }
    finally { saved.loading = null; draw(); }
  }
  search.addEventListener('input', () => {saved.search = search.value; draw();});
  details.addEventListener('toggle', () => {if (details.open) load();});
  // A render can replace this form while its read is in flight.
  if (saved.loading) saved.loading.finally(() => {if (form.isConnected) draw();}).catch(() => {});
  form.addEventListener('submit', async event => {
    event.preventDefault(); if (save.disabled || ownerReviewState.busy || ownerReviewState.stale) return;
    const payload = saved.pending || {group_id:group.id,expected_digest:group.preview_digest,application_id:saved.selected,
      reason:'User selected this application while reviewing the source email.',receipt_scope:saved.scope};
    saved.pending = payload; ownerReviewState.busy = true; draw();
    let prepared = false;
    try {
      await ownerReviewCommand('correct_review_target', payload);
      saved.pending = null; ownerReviewState.selections.clear(); await loadOwnerReviewQueue();
      prepared = !ownerReviewState.error;
      if (!ownerReviewState.error) ownerReviewFeedback('Revised update ready. Review the selected application and changes, then accept.');
    } catch (error) {
      if (error.status) saved.pending = null;
      if (error.status === 409) ownerReviewState.stale = true;
      ownerReviewFeedback(error.status === 409 ? 'This review changed. Your selection is saved. Refresh and preview again.' : error.message);
    } finally {
      ownerReviewState.busy = false; renderOwnerReviewQueue();
      if (prepared) [...document.querySelectorAll('#attention-list [data-review-key]')].find(c => c.dataset.reviewKey === 'evidence_group:' + group.id)?.focus({preventScroll:true});
    }
  });
  const buttons = node('div', 'actions'); buttons.append(save, none);
  form.append(feedback, buttons); details.append(form, discovery); content.append(details); draw();
}

function ownerGroupDiscovery(group, content) {
  if (group.source_type !== 'email' || !group.preview_digest || group.association || group.applications.length) return;
  let saved = ownerDiscoveryState.get(group.id);
  if (!saved) { saved = {employer:'', title:'', preview:null, digest:null, pending:null}; ownerDiscoveryState.set(group.id, saved); }
  if (saved.digest !== group.preview_digest) { saved.preview = null; saved.digest = group.preview_digest; }
  const section = node('section', 'review-discovery');
  section.append(node('h4', '', 'Find a role in the jobs database'));
  const feedback = node('p', 'help', 'Checking whether this email identifies a role outside your tracked applications and jobs catalog…');
  feedback.setAttribute('role', 'status');
  const form = node('form', 'controls');
  const inputs = {};
  for (const [name,label] of [['employer','Employer from email'],['title','Role from email']]) {
    const wrap = node('label', '', label), input = node('input');
    input.name = name; input.required = true; input.maxLength = 160; input.value = saved[name];
    input.disabled = ownerReviewState.busy || ownerReviewState.stale || Boolean(saved.pending);
    wrap.append(input); form.append(wrap); inputs[name] = input;
    input.addEventListener('input', () => { saved[name] = input.value; saved.preview = null; version++; choices.replaceChildren(); feedback.textContent = 'Check the edited employer and role before creating a record.'; });
  }
  const check = node('button', 'quiet', 'Check for existing role'); check.type = 'submit';
  check.disabled = ownerReviewState.busy || ownerReviewState.stale || Boolean(saved.pending); form.append(check);
  const choices = node('div', 'actions'); section.append(feedback, form, choices); content.append(section);
  let version = 0;
  async function create(payload) {
    if (ownerReviewState.busy || ownerReviewState.stale) return;
    saved.pending = payload; ownerReviewState.busy = true; renderOwnerReviewQueue();
    try {
      await ownerReviewCommand('create_application_from_review', payload);
      saved.pending = null; ownerReviewState.selections.clear();
      await loadOwnerReviewQueue();
      if (typeof loadApplications === 'function') await loadApplications();
      ownerReviewFeedback('Tracking application created and email attached. Proposed updates remain in Review.');
    } catch (error) {
      if (error.status) saved.pending = null;
      if (error.status === 409) { ownerReviewState.stale = true; saved.preview = null; }
      ownerReviewFeedback(error.message);
    } finally { ownerReviewState.busy = false; renderOwnerReviewQueue(); }
  }
  if (group.catalog_candidates?.length) {
    const catalog = node('section', 'review-discovery');
    const unknown = group.catalog_candidates.some(c => c.application_recorded === null);
    catalog.append(node('h4', '', 'Roles found in the jobs catalog'), node('p', 'help',
      (unknown ? 'No application was found in the searched records, but the application search was incomplete. ' : 'No application or browser submission was recorded for these roles. ') +
      'Tracking may have missed your submission. Compare the email with the role and location before choosing.'));
    if (group.catalog_retrieval?.complete === false) catalog.append(node('p', 'notice', 'The catalog search was incomplete; other roles may also match.'));
    for (const candidate of group.catalog_candidates) {
      const job = candidate.job, row = node('div', 'review-proposed-change');
      row.append(node('p', '', [job.employer, job.title, job.location].filter(Boolean).join(' · ')));
      if (candidate.suggested) row.append(node('p', 'help', 'Suggested by email analysis; requires your confirmation.'));
      if (job.closed_at) row.append(node('p', 'meta', 'This posting is closed in the catalog.'));
      const button = node('button', '', 'Use this role and attach email'); button.type = 'button';
      button.disabled = ownerReviewState.busy || ownerReviewState.stale || Boolean(saved.pending);
      button.addEventListener('click', () => create({group_id:group.id,expected_digest:group.preview_digest,
        expected_match_digest:candidate.match_digest,catalog_id:candidate.id,employer:job.employer,title:job.title}));
      row.append(button); catalog.append(row);
    }
    content.insertBefore(catalog, section);
  }
  function display(result) {
    feedback.textContent = result.catalog_choices?.length ? 'Select the matching database role to create or reuse its tracking record and attach this email. Proposed updates still need approval.' : result.reason;
    choices.replaceChildren();
    for (const app of result.applications || []) {
      const link = node('a', 'review-context-link', `Review existing application: ${app.employer || ''} · ${app.title || ''}`);
      link.href = '#applications/' + encodeURIComponent(app.id); choices.append(link);
    }
    if (result.catalog_choices?.length) {
      for (const candidate of result.catalog_choices) {
        const job = candidate.job, row = node('section', 'review-catalog-choice');
        row.append(node('h5', '', job.title), node('p', 'meta', [job.employer,job.location].filter(Boolean).join(' · ')));
        const attach = node('button', '', 'Use this role and attach email'); attach.type = 'button';
        attach.disabled = ownerReviewState.busy || ownerReviewState.stale || Boolean(saved.pending);
        attach.addEventListener('click', () => create({group_id:group.id,expected_digest:group.preview_digest,
          expected_match_digest:candidate.match_digest,catalog_id:candidate.id,employer:job.employer,title:job.title,
          search_employer:result.employer,search_title:result.title}));
        row.append(node('p', 'help', 'Creates or reuses a tracking record and attaches this email. Application updates still need approval.'), attach);
        choices.append(row);
      }
    } else for (const job of result.jobs || []) choices.append(jobPreviewButton(job, `Review matching role: ${job.company} · ${job.title}`));
    if (result.can_create) {
      const button = node('button', '', 'Create application record'); button.type = 'button';
      button.disabled = ownerReviewState.busy || ownerReviewState.stale;
      const payload = {group_id:group.id,expected_digest:group.preview_digest,expected_match_digest:result.match_digest,employer:result.employer,title:result.title};
      button.addEventListener('click', () => create(payload)); choices.append(button);
    }
  }
  async function lookup(manual) {
    const current = ++version; check.disabled = true; choices.replaceChildren();
    try {
      const query = new URLSearchParams({group_id:group.id,expected_digest:group.preview_digest});
      if (manual) { query.set('employer', saved.employer); query.set('title', saved.title); }
      const result = await api('/api/v1/application-owner/discovery-preview?' + query);
      if (current !== version || !section.isConnected) return;
      saved.preview = result;
      if (!manual) for (const name of ['employer','title']) inputs[name].value = saved[name] = result[name] || '';
      display(result);
    } catch (error) { if (current === version) feedback.textContent = error.message; }
    finally { check.disabled = ownerReviewState.busy || ownerReviewState.stale || Boolean(saved.pending); }
  }
  form.addEventListener('submit', event => { event.preventDefault(); if (!ownerReviewState.busy && !ownerReviewState.stale) lookup(true); });
  if (saved.pending) {
    feedback.textContent = 'The creation outcome is unconfirmed. Retry the same request to recover its recorded result.';
    const retry = node('button', 'quiet', 'Retry same creation'); retry.disabled = ownerReviewState.busy || ownerReviewState.stale;
    retry.addEventListener('click', () => create(saved.pending)); choices.append(retry);
  } else if (saved.preview) display(saved.preview);
  else if (group.catalog_candidates?.length) feedback.textContent = 'Choose a catalog role above, or check another employer and role from the email.';
  else queueMicrotask(() => { if (section.isConnected) lookup(Boolean(saved.employer && saved.title)); });
}

function ownerEvidenceGroupCard(group) {
  const card = node('article', 'stack-item review-card evidence-group'), content = node('div', 'review-card-content');
  card.dataset.reviewKey = 'evidence_group:' + group.id; card.tabIndex = -1;
  content.append(node('p', 'review-card-kind', group.source_type === 'browser' ? 'Browser application attempt' : group.source_type === 'email' ? 'Email review' : 'Proposed application change'));
  const title = group.applications.map(a => [a.job?.employer,a.job?.title].filter(Boolean).join(' · ')).join(' / ');
  content.append(node('h3', '', title || (group.catalog_candidates?.length ? 'Review matching catalog roles' : 'Match this email to a role')));
  ownerReviewSourceHeading(group, content);
  const choices = new Map(group.proposals.map(p => [p.id, {bundle:ownerReviewBundle(group, p), presentation:ownerReviewPresentation(p)}])
    .filter(([, choice]) => choice.bundle && choice.presentation));
  const includedLinks = new Set([...choices.values()].flatMap(choice => choice.bundle.filter(p => p.operation === 'link_message').map(p => p.id)));
  const updates = group.proposals.filter(p => !includedLinks.has(p.id));
  content.append(node('p', 'meta', `${updates.length} update${updates.length === 1 ? '' : 's'} to review${group.processing.length ? ' · Processing needs attention' : ''}`));
  for (const app of group.applications) { const link = node('a', 'review-context-link', 'Open application'); link.href = '#applications/' + encodeURIComponent(app.id); content.append(link); }
  const needsTarget = ownerGroupNeedsTarget(group);
  // Each meaningful update includes its required link; independent updates stay separate.
  if (!choices.size) for (const quote of new Set(group.proposals.flatMap(p => p.evidence.map(e => e.quote)).filter(Boolean))) content.append(node('blockquote', 'review-evidence', quote));
  if (needsTarget) {
    ownerReviewSources(content, group.evidence.map(({quote, ...ref}) => ref), 'View email');
    ownerGroupTarget(group, content);
  }
  const proposalContent = needsTarget ? node('details', 'technical-details') : content;
  if (needsTarget) {proposalContent.append(node('summary', '', 'Proposed updates after matching')); content.append(proposalContent);}
  for (const proposal of updates) {
    const target = needsTarget && !proposal.application_id ? proposalContent : content;
    const choice = choices.get(proposal.id);
    if (choice) { target.append(ownerCompactProposal(group, proposal, choice.bundle, choice.presentation)); continue; }
    const row = node('section', 'review-proposed-change'), actions = node('div', 'actions');
    row.dataset.proposalId = proposal.id;
    row.append(node('h4', '', ownerReviewTitle(proposal.operation)));
    if (proposal.before?.application) row.append(node('p', 'meta', 'Current progress: ' + ownerReviewLabel(proposal.before.application.stage || proposal.before.application)));
    if (proposal.closure) row.append(node('p', 'notice', `Closing this application will cancel ${proposal.closure.records.length} open tasks, reminders, and scheduled operations.`));
    ownerProposalCard(proposal, row, actions, true, Boolean(group.catalog_candidates?.length), group); row.append(actions); target.append(row);
  }
  if (!needsTarget) ownerReviewSources(content, group.evidence.map(({quote, ...ref}) => ref), choices.size ? 'View email' : 'Source evidence');
  if (group.processing.length) {
    const link = node('a', 'review-context-link', 'Analysis needs attention · View system issue'); link.href = '#review/evidence_group/' + encodeURIComponent(group.id);
    link.addEventListener('click', event => {event.preventDefault(); document.getElementById('system-' + group.id)?.focus({preventScroll:true});}); content.append(link);
  }
  if (group.source_type === 'email' && group.proposals.some(p => p.blockers.length) && !group.processing.some(p => p.status === 'retry_queued')) {
    proposalContent.append(ownerReviewButton('Refresh analysis', 'refresh_review_group', {group_id:group.id,expected_digest:group.preview_digest}));
  }
  const selected = group.proposals.filter(p => ownerReviewState.selections.has(p.id));
  const selectedBundle = [...new Map(selected.flatMap(p => ownerReviewBundle(group,p) || [p]).map(p => [p.id,p])).values()];
  if (selected.length) content.append(ownerReviewButton(`Accept ${selected.length} selected change${selected.length === 1 ? '' : 's'}`, 'review_changes', ownerGroupDecision(group,selectedBundle)));
  if (!needsTarget) ownerGroupTarget(group, content); card.append(content); return card;
}
function renderOwnerReviewQueue() {
  const list = document.querySelector('#attention-list'); if (!list) return;
  const position = ownerReviewCapturePosition();
  const disclosures = captureReviewDisclosures(document.querySelector('#attention')); list.replaceChildren(); list.classList.remove('empty');
  document.querySelector('.review-workspace-preferences')?.remove();
  const oldProcessing = document.querySelector('#owner-processing-section'); if (oldProcessing) oldProcessing.remove();
  const oldHeading = document.querySelector('#owner-review-decision-heading'); if (oldHeading) oldHeading.remove();
  const legacyHistory = document.querySelector('#review-history'); if (legacyHistory) legacyHistory.hidden = true;
  const totals = ownerReviewState.totals || {cards:0,operations:0,processing:0};
  const decisionCount = totals.decisions ?? ownerReviewCardItems().length;
  document.querySelector('#review-summary').textContent = `${decisionCount} decision${decisionCount===1?'':'s'} · ${totals.processing} system issue${totals.processing===1?'':'s'}`;
  const policy = ownerReviewState.policy;
  if (policy) {
    const settings = node('details', 'browser-recording-preference'); settings.append(node('summary', '', 'Browser recording: ' + (policy.enabled ? 'automatic' : 'review first')));
    settings.append(node('p', 'help', 'Automatically record unambiguous paired-browser submission attempts, website acknowledgments, and captured answers. Conflicting or incomplete evidence still needs review. Email changes remain manual.'));
    settings.append(ownerReviewButton(policy.enabled ? 'Review future browser evidence manually' : 'Enable automatic browser recording', 'configure_browser_recording', {enabled:!policy.enabled,expected_version:policy.version})); settings.classList.add('review-workspace-preferences'); document.querySelector('#attention').append(settings);
  }
  for (const group of ownerReviewState.groups.filter(g => g.proposals.length)) { const card = ownerEvidenceGroupCard(group); list.append(card); restoreReviewDisclosures(card, disclosures); }
  const actions = ownerReviewNormalized().filter(i => i.kind === 'external_action');
  for (const item of actions) { const card = node('article','stack-item review-card'), content = node('div','review-card-content'), buttons = node('div','actions'); card.dataset.reviewKey=item.key; content.append(node('h3','',item.title)); ownerActionCard(item.raw,content,buttons); card.append(content,buttons); list.append(card); }
  if (!ownerReviewState.groups.some(g => g.proposals.length) && !actions.length) list.append(node('p','empty',ownerReviewState.error ? 'Review could not be loaded. Refresh to try again.' : ownerReviewState.loaded ? 'Nothing needs review.' : 'Loading review…'));
  const processingGroups = ownerReviewState.groups.filter(g => g.processing.length);
  if (processingGroups.length) {
    const section = node('section', 'owner-processing-section'); section.id = 'owner-processing-section';
    section.append(node('h3', 'owner-review-group-heading', 'System attention'), node('p', 'help', 'Analysis issues are separate from your decisions. Eligible failures receive limited automatic retries; you can request another attempt here.'));
    const stack = node('div', 'stack'); section.append(stack); list.after(section);
    for (const group of processingGroups) {
      const card = node('article', 'stack-item review-card'), content = node('div', 'review-card-content');
      card.id = 'system-' + group.id; card.tabIndex = -1;
      card.dataset.reviewKey = (group.proposals.length ? 'processing_group:' : 'evidence_group:') + group.id;
      content.append(node('h4', '', group.applications.map(ownerApplicationLabel).join(' / ') || 'Email analysis needs attention'));
      ownerReviewSourceHeading(group, content);
      if (group.proposals.length) {
        const link = node('a', 'review-context-link', 'View proposed updates'); link.href = '#review/evidence_group/' + encodeURIComponent(group.id);
        link.addEventListener('click', event => {event.preventDefault(); [...list.querySelectorAll('[data-review-key]')].find(c => c.dataset.reviewKey === 'evidence_group:' + group.id)?.focus({preventScroll:true});}); content.append(link);
      } else ownerReviewSources(content, group.evidence);
      for (const issue of group.processing) {
        const row = node('div'), buttons = node('div', 'actions'); ownerProcessingCard(issue, row, buttons, false, true); row.append(buttons); content.append(row);
      }
      card.append(content); stack.append(card); restoreReviewDisclosures(card, disclosures);
    }
  }
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
  for (const [group,page] of Object.entries(ownerReviewState.pages)) if (page?.next_cursor) { const more=node('button','quiet',group==='groups'?'Load more emails':group==='external_actions'?'Load more external actions':'Load more processing history'); more.dataset.reviewPage=group; more.disabled=ownerReviewState.busy; more.addEventListener('click',()=>loadOwnerReviewQueue(group)); (group==='processing_history'?history:list).append(more); }
  const route=location.hash.split('?')[0].split('/');
  if (route[2]) { const id=decodeURIComponent(route[2]); const selected=ownerReviewState.groups.find(g => g.id===id || g.proposals.some(p=>p.id===id)); if(selected) [...document.querySelectorAll('#attention article')].find(c=>c.dataset.reviewKey==='evidence_group:'+selected.id)?.classList.add('review-selected'); }
  ownerReviewCount();
  ownerReviewBuildWorkspace(true);
  ownerReviewRestorePosition(position);
}

// Keep the source being reviewed stable across busy renders, refresh, and corrections.
// A completed source advances to the following queue position; pending siblings stay put.
function ownerReviewCapturePosition() {
  const panel = document.querySelector('.owner-review-detail');
  const active = document.activeElement;
  const card = active?.closest('[data-review-key]');
  const buttons = card ? [...card.querySelectorAll('button,input,select,textarea,summary,a')] : [];
  return {x:window.scrollX,y:window.scrollY,panel:panel?.scrollTop || 0,
    key:card?.dataset.reviewKey, control:buttons.indexOf(active), label:active?.textContent, selected:ownerReviewWorkspace.selected};
}
function ownerReviewRestorePosition(position) {
  const panel = document.querySelector('.owner-review-detail');
  if (panel) panel.scrollTop = position.selected === ownerReviewWorkspace.selected ? position.panel : 0;
  if (position.key) {
    const card = [...document.querySelectorAll('#attention [data-review-key]')].find(c => c.dataset.reviewKey === position.key && !c.hidden);
    const control = card && [...card.querySelectorAll('button,input,select,textarea,summary,a')][position.control];
    if (control && !control.disabled && control.textContent === position.label) control.focus({preventScroll:true});
    else card?.focus({preventScroll:true});
  }
  window.scrollTo(position.x,position.y);
}
function ownerReviewPrimarySource(group) {
  if (group.source_type !== 'email') return null;
  return group.evidence.find(e => e.owner === 'correspondence' && e.source_id === group.id.slice(6));
}
function ownerReviewSourceMetadata(group) {
  const ref = ownerReviewPrimarySource(group);
  return ref && ownerReviewWorkspace.metadata.get(JSON.stringify([ref.source_id,ref.revision,ref.sha256]));
}
function ownerReviewSourceHeading(group, content) {
  const source = {...(group.source || {}), ...(ownerReviewSourceMetadata(group) || {})};
  const subject = node('p', 'review-source-subject', source.subject || ''); subject.hidden = !source.subject; subject.dataset.sourceGroup = group.id; content.append(subject);
  const date = source.occurred_at || group.source_at;
  const sender = typeof source.sender === 'string' ? source.sender : source.sender?.address;
  if (sender || date) content.append(node('p', 'meta review-source-meta', [sender, date && (typeof displayDate === 'function' ? displayDate(date) : new Date(date).toLocaleString())].filter(Boolean).join(' · ')));
}
function ownerReviewWorkspaceRows() {
  const rows = ownerReviewState.groups.filter(g => g.proposals.length || g.processing.length).map(group => {
    const app = group.applications[0];
    const title = app?.job?.employer || ownerReviewSourceMetadata(group)?.subject || group.source?.subject || group.catalog_candidates?.[0]?.job?.employer || 'Unmatched email';
    const quote = group.evidence.find(e => e.quote)?.quote?.replace(/\s+/g,' ').trim();
    const detail = app?.job?.title || ownerReviewSourceMetadata(group)?.subject || ownerReviewSourceMetadata(group)?.excerpt || group.source?.subject || quote || 'Open the saved email to identify its role';
    const changes = [...new Set(group.proposals.filter(p => p.operation !== 'link_message')
      .map(p => ownerReviewPresentation(p)?.[0] || ownerReviewTitle(p.operation)))];
    return {key:'evidence_group:'+group.id,title,detail,group,
      attention:Boolean(group.processing.length), decision:Boolean(group.proposals.length),
      label:group.proposals.length ? (ownerGroupNeedsTarget(group) ? 'Match role' : changes.slice(0,2).join(' · ') || 'Link email') : 'Email attention'};
  });
  for (const action of ownerReviewNormalized().filter(i => i.kind === 'external_action')) rows.push({key:action.key,title:action.title,detail:'Check exact content and recipient',label:'External action',decision:true,attention:false});
  return rows;
}
function ownerReviewChoose(key) {
  ownerReviewFeedback('');
  ownerReviewWorkspace.selected = key;
  const panel = document.querySelector('.owner-review-detail');
  if (panel) panel.scrollTop = 0;
  ownerReviewBuildWorkspace();
  const card = [...document.querySelectorAll('#attention [data-review-key]')].find(c => !c.hidden && c.dataset.reviewKey === key);
  card?.focus({preventScroll:true});
}
function ownerReviewBuildWorkspace(fresh = false) {
  const list = document.querySelector('#attention-list');
  let workspace = document.querySelector('#owner-review-workspace');
  if (!workspace) {
    workspace = node('div', 'owner-review-workspace'); workspace.id = 'owner-review-workspace';
    const rail = node('aside', 'owner-review-rail'); rail.setAttribute('aria-label','Review queue');
    const detail = node('div', 'owner-review-detail'); detail.setAttribute('aria-label','Selected review');
    list.before(workspace); workspace.append(rail,detail); detail.append(document.querySelector('#review-feedback'),list);
  }
  const rail = workspace.querySelector('.owner-review-rail'), detail = workspace.querySelector('.owner-review-detail');
  const processing = document.querySelector('#owner-processing-section'); if (processing) detail.append(processing);
  const allRows = ownerReviewWorkspaceRows();
  const route = location.hash.split('?')[0];
  if (ownerReviewWorkspace.route !== route) {
    ownerReviewWorkspace.route = route;
    const id = decodeURIComponent(route.split('/')[2] || '');
    const linked = allRows.find(row => row.group && (row.group.id === id || row.group.proposals.some(p => p.id === id)));
    if (linked) { ownerReviewWorkspace.selected = linked.key; ownerReviewWorkspace.filter = 'all'; }
  }
  const filtered = allRows.filter(row => ownerReviewWorkspace.filter === 'all' || (ownerReviewWorkspace.filter === 'decisions' ? row.decision : row.attention));
  let selected = filtered.find(row => row.key === ownerReviewWorkspace.selected);
  if (!selected && filtered.length) selected = filtered[Math.min(ownerReviewWorkspace.index,filtered.length - 1)];
  ownerReviewWorkspace.selected = selected?.key || null;
  ownerReviewWorkspace.index = Math.max(0,filtered.indexOf(selected));
  if (selected?.group && ownerReviewWorkspace.mode === 'focused') ownerReviewLoadSelectedSource(selected.group);
  const pageButtons = [...(fresh ? list : workspace).querySelectorAll('[data-review-page]')];
  const queueScroll = rail.querySelector('.review-queue-items')?.scrollTop || 0;
  rail.replaceChildren();
  const heading = node('div','review-queue-heading'); heading.append(node('h3','','Review queue'),node('p','help','Choose an email. Check its role and proposed result.'));
  rail.append(heading);
  const filters = node('div','review-queue-filters'); filters.setAttribute('role','group'); filters.setAttribute('aria-label','Filter reviews');
  for (const [value,label] of [['all','All'],['decisions','Decisions'],['attention','Email attention']]) {
    const button = node('button','quiet',label); button.setAttribute('aria-pressed',String(ownerReviewWorkspace.filter === value));
    button.addEventListener('click',()=>{ownerReviewFeedback('');ownerReviewWorkspace.filter=value;ownerReviewBuildWorkspace();workspace.querySelector('.review-queue-filters [aria-pressed=true]')?.focus({preventScroll:true});}); filters.append(button);
  }
  rail.append(filters);
  const queue = node('div','review-queue-items'); queue.setAttribute('role','list');
  for (const row of filtered) {
    const item = node('div'); item.setAttribute('role','listitem');
    const button = node('button','review-queue-item'); button.type='button'; button.dataset.queueKey=row.key;
    button.setAttribute('aria-current',String(selected?.key === row.key));
    button.append(node('span','review-queue-status',row.label),node('strong','',row.title),node('span','review-queue-description',row.detail));
    button.addEventListener('click',()=>ownerReviewChoose(row.key)); item.append(button); queue.append(item);
  }
  if (!filtered.length) queue.append(node('p','empty',ownerReviewState.loaded?'No items in this view.':'Loading review…'));
  rail.append(queue); queue.scrollTop = queueScroll;
  for (const button of pageButtons) rail.append(button);
  const mode = node('button','quiet review-display-mode',ownerReviewWorkspace.mode === 'focused' ? 'Show all items together' : 'Review one item at a time');
  mode.addEventListener('click',()=>{ownerReviewWorkspace.mode=ownerReviewWorkspace.mode==='focused'?'all':'focused';ownerReviewBuildWorkspace();workspace.querySelector('.review-display-mode')?.focus({preventScroll:true});}); rail.append(mode);
  workspace.classList.toggle('review-workspace-all',ownerReviewWorkspace.mode === 'all');
  for (const card of list.querySelectorAll(':scope > [data-review-key]')) card.hidden = ownerReviewWorkspace.mode === 'focused' && card.dataset.reviewKey !== selected?.key;
  for (const card of processing?.querySelectorAll('[data-review-key]') || []) {
    const sourceKey = card.dataset.reviewKey.replace(/^processing_group:/,'evidence_group:');
    card.hidden = ownerReviewWorkspace.mode === 'focused' && sourceKey !== selected?.key;
  }
  if (processing) processing.hidden = ownerReviewWorkspace.mode === 'focused' && !selected?.attention;
  for (const paragraph of list.querySelectorAll(':scope > .empty')) paragraph.hidden = ownerReviewWorkspace.mode === 'focused' && Boolean(selected?.attention) && !selected?.decision;
  workspace.classList.toggle('review-workspace-processing',ownerReviewWorkspace.mode === 'focused' && Boolean(selected?.attention) && !selected?.decision);
  // Loading, empty states and pagination stay in the workspace, never in the queue rail.
  let empty = detail.querySelector(':scope > .review-workspace-empty');
  if (!empty) {empty=node('p','empty review-workspace-empty','Choose a review from the queue.');detail.append(empty);}
  empty.hidden = Boolean(selected) || !allRows.length || ownerReviewWorkspace.mode === 'all';
}

// Fetch only the selected source through the authorized, revision-bound reader.
// Queue labels improve when private archived subject metadata is available.
async function ownerReviewLoadSelectedSource(group) {
  const ref = ownerReviewPrimarySource(group);
  if (!ref) return;
  const sourceKey = JSON.stringify([ref.source_id,ref.revision,ref.sha256]);
  if (ownerReviewWorkspace.sourceRequests.has(sourceKey)) return;
  ownerReviewWorkspace.sourceRequests.set(sourceKey,true);
  try {
    const query = new URLSearchParams({source_id:ref.source_id,revision:String(ref.revision),sha256:ref.sha256 || ''});
    const result = await api('/api/v1/application-owner/review-source?' + query);
    const excerpt = (result.text || result.excerpt || '').replace(/\s+/g,' ').trim().slice(0,180);
    ownerReviewWorkspace.metadata.set(sourceKey,{subject:result.subject || null,excerpt});
    const current = ownerReviewState.groups.find(g => g.id === group.id);
    if (!current?.evidence.some(e => e.owner === 'correspondence' && JSON.stringify([e.source_id,e.revision,e.sha256]) === sourceKey)) return;
    if (!group.applications.length) for (const button of document.querySelectorAll('[data-queue-key]')) if (button.dataset.queueKey === 'evidence_group:'+group.id) button.querySelector('.review-queue-description').textContent=result.subject || excerpt || button.querySelector('.review-queue-description').textContent;
    if (result.subject) {
      for (const subject of document.querySelectorAll('.review-source-subject')) if (subject.dataset.sourceGroup === group.id) {subject.textContent=result.subject;subject.hidden=false;}
      if (!group.applications.length) for (const button of document.querySelectorAll('[data-queue-key]')) if (button.dataset.queueKey === 'evidence_group:'+group.id) button.querySelector('strong').textContent=result.subject;
    }
    let text = result.text || result.excerpt || result.body || (result.available ? 'This message has no text.' : 'This source is unavailable.');
    if (result.coverage?.complete === false || result.truncated) text += '\n\nSome source content is unavailable.';
    ownerReviewState.sources.set(sourceKey,text);
    for (const card of document.querySelectorAll('[data-review-key]')) if (card.dataset.reviewKey.endsWith(group.id)) {
      for (const body of card.querySelectorAll('.review-message .message-body')) if (!body.textContent) body.textContent=text;
    }
  } catch (_) {
    // The explicit reader remains available to retry; metadata never gates a decision.
    ownerReviewWorkspace.sourceRequests.delete(sourceKey);
  }
}
