// One normalized source drives Review, navigation counts, and application notices.
document.querySelector("#attention").innerHTML = `
  <div class="section-heading">
    <div><h2>Review</h2><p class="section-note">Decisions and updates waiting for you.</p></div>
    <button class="quiet" id="refresh-attention" type="button">Refresh</button>
  </div>
  <p id="review-feedback" class="notice" role="status" hidden></p>
  <p id="review-summary" class="meta" aria-live="polite"></p>
  <div id="attention-list" class="stack empty">Loading review items…</div>
  <details id="mail-review-history" class="review-history">
    <summary>Email review history</summary>
    <p class="help">Historical reprocessing and earlier email decisions are kept here, separate from current requests.</p>
    <button class="quiet" id="refresh-mail-history" type="button">Load email history</button>
    <p id="mail-history-feedback" class="notice" role="status" hidden></p>
    <div id="mail-history-list" class="stack"></div>
  </details>
  <details id="review-history" class="review-history">
    <summary>Action history</summary>
    <p class="help">Previously reviewed drafts and calendar holds.</p>
    <div id="action-list" class="stack"></div>
  </details>
`;

function actionNeedsReview(action) { return action.status === "pending" || action.status === "needs_reconciliation"; }
function reviewTitle(item) {
  if (item.kind === "mail_analysis") return item.analysis?.subject || "Email findings";
  if (item.kind === "lifecycle_correction") return "Proposed application update";
  if (item.kind === "interview_revision") return "Interview change";
  if (item.kind === "mail_discovery") return "Untracked recruiting conversation";
  if (item.kind === "event_proposal") return ({interview_requested: "Interview request", submission_confirmed: "Application confirmation", rejection_received: "Application outcome", offer_received: "Offer received"})[item.detail] || (item.detail || "Application update").replaceAll("_", " ");
  if (item.kind === "temporal_proposal") return item.detail === "interview" ? "Proposed interview time" : "Application deadline";
  if (item.kind === "browser_submission") return "Check submission";
  if (item.kind === "mail_processing_failure") return item.detail || "Email processing needs attention";
  return ({outlook_reply_draft: "Reply draft", outlook_calendar_hold: "Private interview hold"})[item.kind] || "Application action";
}
function reviewStatus(item) {
  return item.status === "needs_reconciliation" ? "Check outcome" : item.kind === "outlook_reply_draft" || item.kind === "outlook_calendar_hold" ? "Needs approval" : "Needs review";
}
function reviewEventActionLabel(item) {
  if (item.suggested_resolution?.action === "accept" && item.suggested_resolution.label) return item.suggested_resolution.label;
  return ({submission_confirmed: "Confirm application received", interview_requested: "Record interview request",
    recruiter_contact: "Record recruiter contact", assessment_requested: "Record assessment request",
    assessment_completed: "Record assessment completed", interview_scheduled: "Record interview scheduled",
    interview_completed: "Record interview completed", rejected: "Record rejection", rejection_received: "Record rejection",
    offer_received: "Record offer", offer_accepted: "Record offer accepted", withdrawn: "Record withdrawal"})[item.detail] || "Confirm update";
}
function recommendedReviewApplication(item, candidates) {
  const suggestion = item.suggested_resolution;
  return suggestion && !suggestion.requires_selection && candidates.includes(suggestion.application_id)
    ? suggestion.application_id : "";
}
function reviewJobValue(job) { return `job:${job.ats}:${job.id}`; }
function reviewJobMatches(item) {
  const jobs = [...(item.job_matches || []), item.suggested_resolution?.job].filter(job => job?.ats && job?.id);
  return [...new Map(jobs.map(job => [reviewJobValue(job), job])).values()];
}
function selectedReviewJob(item, value) {
  return reviewJobMatches(item).find(job => reviewJobValue(job) === value);
}
function recommendedReviewSelection(item, candidates) {
  const applicationId = recommendedReviewApplication(item, candidates);
  if (applicationId) return applicationId;
  const suggestion = item.suggested_resolution;
  return suggestion?.job && !suggestion.requires_selection && selectedReviewJob(item, reviewJobValue(suggestion.job))
    ? reviewJobValue(suggestion.job) : "";
}
function renderReviewSuggestion(item, detail) {
  const suggestion = item.suggested_resolution;
  if (!suggestion) return;
  const section = node("div", "review-suggestion");
  const match = (item.application_matches || []).find(app => app.application_id === suggestion.application_id);
  const application = state.applications.find(app => app.application_id === suggestion.application_id);
  if (suggestion.application_id) {
    const employer = application?.employer_snapshot || match?.employer;
    const title = application?.title_snapshot || match?.title;
    section.append(node("p", "review-match-label", "Likely application"),
      node("p", "review-match-title", title || employer || suggestion.application_id));
    if (employer && title) section.append(node("p", "review-match-employer", employer));
  } else if (suggestion.job) {
    section.append(node("p", "review-match-label", "Likely job"),
      node("p", "review-match-title", suggestion.job.title || suggestion.job.company),
      node("p", "review-match-employer", suggestion.job.company));
    section.append(node("p", "help", "Confirmation creates the application record and links this email."));
  } else if (["event_proposal", "mail_discovery"].includes(item.kind)) {
    section.append(node("p", "review-match-label", "Application match"));
  }
  if (suggestion.explanation) section.append(node("p", "meta", suggestion.explanation));
  detail.append(section);
  return section;
}
function getReviewItems() {
  const groups = consoleState.reviews.filter(item => item.kind === "mail_analysis" && item.analysis?.mode === "shared" && !item.analysis?.replay_id && item.analysis?.current !== false);
  const groupedProposals = new Set(groups.flatMap(item => (item.analysis.findings || []).filter(f => f.projection).map(f => `${f.projection.kind}:${f.projection.id}`)));
  const rows = [...consoleState.reviews.filter(item => item.kind !== "action_proposal" && [undefined, "review", "pending", "conflict", "failed"].includes(item.status)
    && (item.kind !== "mail_analysis" || groups.includes(item)) && !groupedProposals.has(`${item.kind}:${item.id}`)), ...consoleState.actions.filter(actionNeedsReview)];
  const unique = new Map();
  for (const raw of rows) {
    const id = raw.action_id || raw.id;
    const identity = raw.kind === "mail_processing_failure" ? [raw.account_id, raw.folder_ref, raw.query_version, id].join(":") : id;
    const key = `${raw.kind}:${identity}`;
    unique.set(key, {id, key, applicationId: raw.application_id || null, title: reviewTitle(raw), status: raw.status, kind: raw.kind,
      createdAt: raw.created_at, deadline: raw.due_at || raw.expires_at || null, raw});
  }
  const time = value => { const parsed = Date.parse(value); return Number.isFinite(parsed) ? parsed : Infinity; };
  return [...unique.values()].sort((a, b) => Number(b.status === "needs_reconciliation") - Number(a.status === "needs_reconciliation") || time(a.deadline) - time(b.deadline) || time(a.createdAt) - time(b.createdAt) || a.key.localeCompare(b.key));
}
function reviewItemsForApplication(id) {
  return getReviewItems().filter(item => item.applicationId ? item.applicationId === id : (item.raw.candidate_application_ids || []).includes(id));
}
function reviewHref(item) { return `#review/${encodeURIComponent(item.kind)}/${encodeURIComponent(item.id)}`; }
let reviewAttentionLoaded = false;
let reviewActionsLoaded = false;
let reviewAttentionError = null;
let reviewActionsError = null;
let lastFocusedReviewRoute = "";
function updateReviewCount() {
  const count = getReviewItems().length;
  const badge = document.querySelector("#review-count");
  const unavailable = reviewAttentionError || reviewActionsError;
  badge.textContent = count || (unavailable ? "—" : "");
  badge.title = unavailable ? "Review count unavailable; some items could not be loaded" : reviewAttentionLoaded && reviewActionsLoaded ? "" : "Review count is loading";
  renderApplicationTable();
  renderApplicationReviewNotices();
}
function captureReviewDisclosures(root) {
  return new Map([...root.querySelectorAll('[data-review-key]')].map(row => [row.dataset.reviewKey,
    new Map([...row.querySelectorAll('details')].map((detail, index) => [
      detail.querySelector(':scope > summary')?.textContent || String(index), detail.open,
    ])),
  ]));
}
function restoreReviewDisclosures(row, saved) {
  const disclosures = saved.get(row.dataset.reviewKey);
  if (!disclosures) return;
  [...row.querySelectorAll('details')].forEach((detail, index) => {
    const key = detail.querySelector(':scope > summary')?.textContent || String(index);
    if (disclosures.has(key)) detail.open = disclosures.get(key);
  });
}
function renderReviewQueue() {
  const applicationId = new URLSearchParams(location.hash.split("?")[1] || "").get("application");
  const items = applicationId ? reviewItemsForApplication(applicationId) : getReviewItems();
  const pendingIds = new Set(getReviewItems().map(item => item.id));
  for (const id of mailResolutionEditors.keys()) if (!pendingIds.has(id)) mailResolutionEditors.delete(id);
  const list = document.querySelector("#attention-list");
  // Async attention/action refreshes replace cards. Preserve the user's native
  // disclosure state for the same review identity, including mailbox scope.
  const disclosures = captureReviewDisclosures(list);
  const applicationSelections = new Map([...list.querySelectorAll('[data-review-key]')].flatMap(row => {
    const select = row.querySelector('[data-review-application]');
    return select?.dataset.reviewSelectionChanged ? [[row.dataset.reviewKey, select.value]] : [];
  }));
  const history = document.querySelector("#action-list");
  const historyDisclosures = captureReviewDisclosures(history);
  clear(list);
  list.classList.toggle("empty", !items.length);
  document.querySelector("#review-summary").textContent = items.length ? `${items.length} item${items.length === 1 ? "" : "s"} waiting for you` : "";
  if (!items.length) {
    const unavailable = reviewAttentionError || reviewActionsError;
    const loaded = reviewAttentionLoaded && reviewActionsLoaded;
    list.append(node("p", "", unavailable ? "Review items could not be fully loaded. Use Refresh to try again."
      : loaded ? "Nothing needs review. New application updates and decisions will appear here." : "Loading review items…"));
  }
  if (applicationId) {
    const context = node("p", "review-filter");
    const app = state.applications.find(item => item.application_id === applicationId);
    context.append(document.createTextNode(app ? `${app.employer_snapshot} · ${app.title_snapshot} · ` : "This application · "));
    const all = node("a", "", "Show all review items"); all.href = "#review";
    context.append(all); list.prepend(context);
  }
  const route = location.hash.split("?")[0].split("/");
  let focused;
  for (const item of items) {
    const row = item.raw.action_id ? actionItem(item.raw) : reviewItem(item);
    row.dataset.reviewKey = item.key;
    restoreReviewDisclosures(row, disclosures);
    const applicationSelect = row.querySelector('[data-review-application]');
    const savedApplication = applicationSelections.get(item.key);
    if (applicationSelect && savedApplication !== undefined && [...applicationSelect.options].some(option => option.value === savedApplication)) {
      applicationSelect.value = savedApplication;
      applicationSelect.dispatchEvent(new Event('change'));
    }
    row.tabIndex = -1;
    row.id = `review-${encodeURIComponent(item.key)}`;
    if (route[1] === encodeURIComponent(item.kind) && route[2] === encodeURIComponent(item.id)) { row.classList.add("review-selected"); focused = row; }
    list.append(row);
  }
  clear(history);
  const completed = consoleState.actions.filter(action => !actionNeedsReview(action));
  for (const action of completed) {
    const row = actionItem(action);
    row.dataset.reviewKey = `${action.kind}:${action.action_id}`;
    restoreReviewDisclosures(row, historyDisclosures);
    history.append(row);
  }
  if (!completed.length) history.append(node("p", "empty", "No previous actions."));
  updateReviewCount();
  const focusRoute = location.hash.split("?")[0];
  if (consoleState.view !== "review" || !route[1]) lastFocusedReviewRoute = "";
  if (focused && consoleState.view === "review" && focusRoute !== lastFocusedReviewRoute) {
    lastFocusedReviewRoute = focusRoute;
    requestAnimationFrame(() => {
      if (!focused.isConnected || location.hash.split("?")[0] !== focusRoute) return;
      focused.focus({preventScroll: true});
      focused.scrollIntoView({block: "center", behavior: "instant"});
    });
  }
  if (!focused && consoleState.view === "review" && route[1] === "mail_analysis" && route[2]) loadMailReviewRoute(route[2]);
}
let reviewAttentionEpoch = 0;
let reviewActionsEpoch = 0;
const reviewFailureAnalyses = new Map();
const reviewFailureAnalysisQueue = [];
let reviewFailureAnalysisTask = null;
function reviewFailureIdentity(item) {
  return JSON.stringify([item.account_id, item.folder_ref, item.query_version, item.id]);
}
function queueReviewFailureAnalysis(retryItem = null) {
  if (consoleState.view !== "review") return Promise.resolve();
  if (!retryItem && reviewFailureAnalysisTask) return reviewFailureAnalysisTask;
  // Snapshot the loaded queue. Refreshes during this batch cannot append more
  // work; each identity gets one automatic attempt and requests stay sequential.
  const candidates = retryItem ? [retryItem] : consoleState.reviews.filter(item =>
    item.kind === "mail_processing_failure" && item.can_analyze_archive === true
      && !reviewFailureAnalyses.has(reviewFailureIdentity(item)));
  for (const item of candidates) {
    if (item.can_analyze_archive !== true) continue;
    const identity = reviewFailureIdentity(item);
    const previous = reviewFailureAnalyses.get(identity);
    if (["pending", "running"].includes(previous?.status)) continue;
    reviewFailureAnalyses.set(identity, {status: "pending", commandId: previous?.commandId || key("mail-analysis")});
    reviewFailureAnalysisQueue.push(item);
  }
  if (retryItem && reviewFailureAnalysisQueue.length) renderReviewQueue();
  if (reviewFailureAnalysisTask || !reviewFailureAnalysisQueue.length) return reviewFailureAnalysisTask || Promise.resolve();
  renderReviewQueue();
  reviewFailureAnalysisTask = Promise.resolve().then(async () => {
    while (reviewFailureAnalysisQueue.length && consoleState.view === "review") {
      const item = reviewFailureAnalysisQueue.shift();
      const identity = reviewFailureIdentity(item);
      const analysis = reviewFailureAnalyses.get(identity);
      if (!consoleState.reviews.some(row => row.kind === "mail_processing_failure"
        && row.can_analyze_archive === true && reviewFailureIdentity(row) === identity)) {
        analysis.status = "complete";
        continue;
      }
      analysis.status = "running";
      renderReviewQueue();
      try {
        await api("/api/v1/mail/failures/analyze", {method: "POST", body: JSON.stringify({
          account_id: item.account_id, folder_ref: item.folder_ref,
          query_version: item.query_version, message_id: item.id, idempotency_key: analysis.commandId,
        })});
        analysis.status = "complete";
        // Only analysis is prepared here. The resulting proposal still requires
        // the user's normal confirmation before any application record changes.
        await loadReviewQueue();
      } catch (error) {
        analysis.status = "failed";
        analysis.error = error.message;
        renderReviewQueue();
      }
    }
  }).finally(() => { reviewFailureAnalysisTask = null; });
  return reviewFailureAnalysisTask;
}
async function fetchReviewAttention() {
  const epoch = ++reviewAttentionEpoch;
  try {
    const result = await api("/api/v1/attention");
    if (epoch === reviewAttentionEpoch) {
      consoleState.reviews = (result.items || []).filter(item => item.kind !== "action_proposal");
      reviewAttentionLoaded = true; reviewAttentionError = null;
    }
  } catch (error) { if (epoch === reviewAttentionEpoch) reviewAttentionError = error; throw error; }
}
async function fetchReviewActions() {
  const epoch = ++reviewActionsEpoch;
  try {
    const result = await api("/api/v1/actions");
    if (epoch === reviewActionsEpoch) {
      consoleState.actions = result.actions || [];
      reviewActionsLoaded = true; reviewActionsError = null;
    }
  } catch (error) { if (epoch === reviewActionsEpoch) reviewActionsError = error; throw error; }
}
async function loadAttention() {
  try { await fetchReviewAttention(); } finally { renderReviewQueue(); }
  queueReviewFailureAnalysis();
}
async function loadActions() { try { await fetchReviewActions(); } finally { renderReviewQueue(); } }
let reviewLoadEpoch = 0;
async function loadReviewQueue() {
  const epoch = ++reviewLoadEpoch;
  const button = document.querySelector("#refresh-attention");
  const feedback = document.querySelector("#review-feedback");
  button.disabled = true;
  feedback.hidden = true;
  document.querySelector("#attention-list").setAttribute("aria-busy", "true");
  try {
    const results = await Promise.allSettled([fetchReviewAttention(), fetchReviewActions()]);
    if (epoch !== reviewLoadEpoch) return;
    renderReviewQueue();
    const failure = results.find(result => result.status === "rejected");
    if (failure) { feedback.textContent = `Some review items could not be refreshed. Previous items are kept. ${failure.reason.message}`; feedback.hidden = false; }
    if (results[0].status === "fulfilled") queueReviewFailureAnalysis();
  } finally {
    if (epoch === reviewLoadEpoch) { button.disabled = false; document.querySelector("#attention-list").removeAttribute("aria-busy"); }
  }
}

const mailResolutionEditors = new Map();
function mailResolutionEditor(item) {
  if (mailResolutionEditors.has(item.id)) return mailResolutionEditors.get(item.id);
  const root = node("details", "mail-resolution");
  root.append(node("summary", "", "Review decision"));
  const form = node("form", "mail-resolution-form"); root.append(form);
  const field = (label, input, parent = form) => {
    const wrapper = node("label", "mail-resolution-field", label);
    input.setAttribute("aria-label", label); wrapper.append(input); parent.append(wrapper); return input;
  };
  const decision = field("What should happen?", mailChoice("Resolution", [
    ["record", "Record an application update"], ["keep", "Keep the message without changing status"],
    ["dismiss", "Dismiss from review"]], "record"));
  const updateFields = node("div", "mail-resolution-fields"); form.append(updateFields);
  const event = field("What does the email mean?", mailChoice("Email meaning", Object.entries(MAIL_EVENT_LABELS), item.detail), updateFields);
  const quote = field("Supporting words from the email", node("textarea"), updateFields);
  quote.rows = 3; quote.maxLength = 512; quote.value = item.evidence_quote || "";
  updateFields.append(node("p", "help", "If the interpretation is wrong, choose the right update and copy the supporting words from the message."));
  const applicationFields = node("div", "mail-resolution-fields"); form.append(applicationFields);
  const search = field("Find an application", node("input"), applicationFields); search.type = "search";
  search.placeholder = "Search employer or role";
  const application = field("Application", node("select"), applicationFields);
  const searchStatus = node("p", "help"); searchStatus.setAttribute("role", "status");
  const moreApplications = node("button", "quiet", "Load more matching applications");
  moreApplications.type = "button"; moreApplications.hidden = true;
  applicationFields.append(searchStatus, moreApplications);
  const knownApplications = new Map();
  let searchEpoch = 0, searchTimer = null, nextCursor = null;
  let selected = item.application_id || (item.candidate_application_ids?.length === 1 ? item.candidate_application_ids[0] : "");
  const populate = () => {
    const query = search.value.trim().toLowerCase();
    application.replaceChildren();
    for (const [id, label] of [["", "Choose an application…"], ["new", "Create a missing application…"]]) {
      const option = node("option", "", label); option.value = id; application.append(option);
    }
    const suggested = new Set(item.candidate_application_ids || []);
    for (const app of state.applications) knownApplications.set(app.application_id, app);
    const apps = [...knownApplications.values()].sort((a,b) => Number(suggested.has(b.application_id)) - Number(suggested.has(a.application_id)));
    for (const app of apps) {
      const label = `${app.employer_snapshot} · ${app.title_snapshot}`;
      if (query && !label.toLowerCase().includes(query) && app.application_id !== selected) continue;
      const option = node("option", "", `${suggested.has(app.application_id) ? "Suggested: " : ""}${label}`);
      option.value = app.application_id; application.append(option);
    }
    application.value = selected;
  };
  const searchApplications = async (after = '') => {
    const epoch = ++searchEpoch, query = search.value.trim();
    moreApplications.disabled = true; searchStatus.textContent = "Searching application history…";
    try {
      const result = await api(`/api/v1/mail-review/applications?${new URLSearchParams({search:query, after})}`);
      if (epoch !== searchEpoch) return;
      for (const app of result.applications || []) knownApplications.set(app.application_id, app);
      nextCursor = result.next_cursor;
      moreApplications.hidden = !nextCursor;
      searchStatus.textContent = nextCursor ? "More matches are available." : "Application history searched.";
      populate();
    } catch (_) {
      if (epoch === searchEpoch) {
        searchStatus.textContent = "Application search could not finish. Edit the search to retry.";
        moreApplications.hidden = true;
      }
    } finally { if (epoch === searchEpoch) moreApplications.disabled = false; }
  };
  moreApplications.addEventListener("click", () => searchApplications(nextCursor));
  populate();
  const newFields = node("div", "mail-resolution-fields"); applicationFields.append(newFields);
  const employer = field("Employer", node("input"), newFields); employer.maxLength = 300;
  const title = field("Role", node("input"), newFields); title.maxLength = 500;
  applicationFields.append(node("p", "help", "Suggestions are optional. You can select any application after checking the employer and role."));
  const taskFields = node("div", "mail-resolution-fields"); form.append(taskFields);
  const task = field("Your next step", mailChoice("Next step", [["", "No next step needed"],
    ["reply", "Reply to the sender"], ["send_availability", "Share availability"],
    ["complete_assessment", "Complete an assessment"], ["send_document", "Send a document"],
    ["attend_interview", "Attend an interview"], ["offer_decision", "Decide on an offer"], ["follow_up", "Book a time or follow up"]]), taskFields);
  const taskDetails = node("div", "mail-resolution-fields"); taskFields.append(taskDetails);
  const note = field("Next-step details", node("input"), taskDetails); note.maxLength = 2000;
  const due = field("Due date (optional)", node("input"), taskDetails); due.type = "datetime-local";
  taskFields.append(node("p", "help", "A next step creates a task for you. It does not send a reply, accept an invitation, or book an interview."));
  const reason = field("Review note (optional)", node("input")); reason.maxLength = 1000;
  const previewButton = node("button", "", "Preview resolution"); previewButton.type = "submit";
  const feedback = node("p", "notice"); feedback.setAttribute("role", "status"); feedback.hidden = true;
  const preview = node("div", "mail-resolution-preview"); preview.hidden = true;
  const save = node("button", "", "Save resolution"); save.type = "button"; save.hidden = true;
  form.append(previewButton, feedback, preview, save);
  let plan = null, commandId = null, version = 0, busy = false;
  const invalidate = () => { version++; plan = null; preview.hidden = true; save.hidden = true; feedback.hidden = true; };
  const update = () => {
    updateFields.hidden = decision.value !== "record";
    applicationFields.hidden = taskFields.hidden = decision.value === "dismiss";
    newFields.hidden = application.value !== "new";
    taskDetails.hidden = !task.value;
    invalidate();
  };
  form.addEventListener("input", invalidate);
  decision.addEventListener("change", update); task.addEventListener("change", update);
  application.addEventListener("change", () => { selected = application.value; update(); });
  search.addEventListener("input", () => {
    ++searchEpoch; moreApplications.hidden = true; populate(); clearTimeout(searchTimer);
    searchTimer = setTimeout(() => searchApplications(), 250);
  });
  root.addEventListener("toggle", () => { if (root.open) { populate(); searchApplications(); } });
  update();
  const collect = () => {
    const value = {proposal_id:item.id, decision:decision.value, reason:reason.value.trim() || "Reviewed email in dashboard."};
    if (value.decision === "dismiss") return value;
    if (!application.value) throw new Error("Choose an application or create a missing one.");
    if (application.value === "new") value.new_application = {employer:employer.value, title:title.value};
    else value.application_id = application.value;
    if (value.decision === "record") { value.event_type = event.value; value.evidence_quote = quote.value; }
    if (task.value) value.task = {kind:task.value, note:note.value, ...(due.value ? {due_at:new Date(due.value).toISOString().replace('.000Z','Z')} : {})};
    return value;
  };
  form.addEventListener("submit", async e => {
    e.preventDefault(); if (busy) return;
    invalidate(); const requestedVersion = version;
    busy = true; previewButton.disabled = true;
    try {
      const result = await api("/api/v1/mail-review/preview", {method:"POST", body:JSON.stringify({decisions:[collect()]})});
      if (requestedVersion !== version) return;
      plan = result; commandId = key("mail-resolution");
      preview.replaceChildren(node("h4", "", "What will change"));
      for (const change of result.changes) {
        preview.append(node("p", "", change.decision === "dismiss" ? "Remove this item from Review. The email stays in Outlook." :
          `${change.creates_application ? "Create" : "Use"} ${change.application}.`));
        if (change.event_type) preview.append(node("p", "", `Record: ${MAIL_EVENT_LABELS[change.event_type] || change.event_type}. Status: ${change.from_phase.replaceAll('_',' ')} → ${change.terminal_outcome || change.to_phase.replaceAll('_',' ')}.`));
        else if (change.decision === "keep") preview.append(node("p", "", "Attach the message to the application. Keep its current status."));
        if (change.closes_application_work) preview.append(node("p", "", "Close this application and cancel its outstanding tasks and reminders."));
        preview.append(node("p", "", change.next_step ? `Add next step: ${change.next_step.note}${change.next_step.due_at ? ' · Due '+displayDate(change.next_step.due_at) : ''}.` : "No new next step."));
      }
      preview.append(node("p", "help", result.notice)); preview.hidden = save.hidden = false;
    } catch (error) { if (requestedVersion === version) { feedback.textContent = error.message; feedback.hidden = false; } }
    finally { busy = false; previewButton.disabled = false; }
  });
  save.addEventListener("click", async () => {
    if (!plan || busy) return;
    busy = true; save.disabled = previewButton.disabled = true;
    for (const input of form.querySelectorAll('input,select,textarea')) input.disabled = true;
    const submitted = plan;
    try {
      await api("/api/v1/mail-review/resolve", {method:"POST", body:JSON.stringify({decisions:submitted.decisions, preview_hash:submitted.preview_hash, idempotency_key:commandId})});
      mailResolutionEditors.delete(item.id);
      await Promise.all([loadReviewQueue(), loadApplications()]);
      notice("Email review resolved.");
    } catch (error) {
      feedback.textContent = error.status === 409 ? "This review or application changed. Preview the resolution again before saving." : error.message;
      feedback.hidden = false;
      if (error.status === 409) { plan = null; save.hidden = true; }
    } finally {
      busy = false; save.disabled = previewButton.disabled = false;
      for (const input of form.querySelectorAll('input,select,textarea')) input.disabled = false;
    }
  });
  mailResolutionEditors.set(item.id, root);
  return root;
}

function reviewMessageDisclosure(item, payload) {
  const details = node("details", "review-message");
  details.append(node("summary", "", "View message"));
  const content = node("div", "review-message-content");
  details.append(content);
  const show = message => {
    content.replaceChildren(node("h4", "", "Subject"), node("p", "message-subject", message.subject || "No subject recorded."),
      node("h4", "", "Body"), node("div", "message-body", message.body || "No message body is available."));
    if (!message.available) content.append(node("p", "meta", message.body
      ? "The full message is unavailable. Showing the saved evidence excerpt."
      : "No archived email is available for this item."));
    else if (!payload) content.append(node("p", "meta", message.truncated
      ? "The archived message was truncated when collected."
      : "Archived email text; original formatting and quoted history may have been removed."));
  };
  if (payload) {
    show({subject: payload.subject, body: typeof payload.body === "string" ? payload.body : payload.body?.content, available: true});
    return details;
  }
  let loaded = false, loading = false;
  const load = async () => {
    if (loaded || loading || !details.open) return;
    loading = true;
    content.replaceChildren(node("p", "meta", "Loading message…"));
    content.setAttribute("aria-busy", "true");
    const query = new URLSearchParams({kind: item.kind, id: item.id});
    if (item.kind === "mail_processing_failure") {
      for (const field of ["account_id", "folder_ref", "query_version"]) query.set(field, item[field]);
    }
    try {
      show(await api(`/api/v1/attention/message?${query}`));
      loaded = true;
    } catch (_) {
      const retry = node("button", "quiet", "Retry loading message"); retry.type = "button";
      retry.addEventListener("click", load);
      content.replaceChildren(node("p", "meta", "The message could not be loaded."), retry);
    } finally { loading = false; content.removeAttribute("aria-busy"); }
  };
  details.addEventListener("toggle", load);
  return details;
}

function actionPreview(action) {
  const root = node("div", "action-preview");
  const payload = action.payload || {};
  const app = state.applications.find(item => item.application_id === action.application_id);
  if (app) { root.append(jobPreviewButton(app, `${app.employer_snapshot} · ${app.title_snapshot}`)); const link = node("a", "review-context-link", "Open application"); link.href = applicationHref(app.application_id, "overview"); root.append(link, postingDates(app)); }
  root.append(reviewMessageDisclosure(action, payload));
  if (payload.start || payload.starts_at) root.append(node("p", "", `${displayDate(payload.starts_at || payload.start?.dateTime || payload.start)} → ${displayDate(payload.ends_at || payload.end?.dateTime || payload.end)}`));
  const details = node("details", "technical-details"); details.append(node("summary", "", "Action details"), node("pre", "payload-preview", JSON.stringify(payload, null, 2)), node("p", "meta", `Approved content fingerprint: ${(action.payload_sha256 || "").slice(0, 12)}`)); root.append(details);
  return root;
}

async function decideProposal(item, decision, selectedApplicationId, button) {
  button.disabled = true;
  try {
    const job = selectedReviewJob(item, selectedApplicationId);
    await api(`/api/v1/proposals/${item.id}/decision`, {
      method: "POST",
      body: JSON.stringify({
        idempotency_key: key("review"),
        decision,
        ...(job && decision === "accepted" ? {selected_job: {ats: job.ats, id: job.id}} : {selected_application_id: job ? null : selectedApplicationId}),
        reason: decision === "accepted" ? "confirmed in dashboard" : "rejected in dashboard",
      }),
    });
    await Promise.all([loadReviewQueue(), loadApplications()]);
  } catch (error) {
    button.disabled = false;
    notice(error.message);
  }
}

async function decideTemporalProposal(item, decision, button) {
  button.disabled = true;
  try {
    await api(`/api/v1/temporal-proposals/${item.id}/decision`, {
      method: "POST",
      body: JSON.stringify({
        idempotency_key: key("temporal-review"),
        decision,
        reason: decision === "accepted" ? "confirmed in dashboard" : "rejected in dashboard",
      }),
    });
    await Promise.all([loadReviewQueue(), loadApplications()]);
  } catch (error) {
    button.disabled = false;
    notice(error.message);
  }
}

const MAIL_EVENT_LABELS = {
  submission_confirmed: "Application confirmation", recruiter_contact: "Recruiter response",
  assessment_requested: "Assessment requested", assessment_completed: "Assessment completed",
  interview_requested: "Interview invitation", interview_scheduled: "Interview confirmed",
  interview_completed: "Interview completed", offer_received: "Offer received", offer_accepted: "Offer accepted",
  rejection_received: "Rejection received", withdrawn: "Application withdrawn",
};
const MAIL_ACTION_LABELS = {reply: "Reply", send_availability: "Share availability", complete_assessment: "Complete assessment", offer_decision: "Decide on offer", other: "Other request"};
function mailFindingLabel(finding) {
  const value = finding.value || {};
  if (finding.type === "event") return MAIL_EVENT_LABELS[value.event_type] || "Application update";
  if (finding.type === "action") return value.description || MAIL_ACTION_LABELS[value.kind] || "Requested action";
  if (finding.type === "temporal") return value.wording || (value.kind === "interview" ? "Interview time" : "Deadline");
  return value.description || "This email needs clarification";
}
function mailChoice(label, choices, value = "") {
  const select = node("select"); select.setAttribute("aria-label", label);
  for (const [id, title] of choices) { const option = node("option", "", title); option.value = id; select.append(option); }
  select.value = value;
  return select;
}
function mailCorrection(finding) {
  const value = finding.value;
  const root = node("details"); root.append(node("summary", "", "Correct this finding"));
  root.append(node("p", "help", "Corrections keep the original finding and its supporting quotes in history. Choose Accept to save the correction."));
  const inputs = new Map();
  function field(name, label, choices) {
    const wrapper = node("label", "form-field", label);
    const input = choices ? mailChoice(label, choices, value[name] || "") : node("input");
    if (!choices) { input.type = "text"; input.value = value[name] || ""; input.maxLength = 512; input.setAttribute("aria-label", label); }
    wrapper.append(input); root.append(wrapper); inputs.set(name, input);
  }
  if (finding.type === "event") field("event_type", "Corrected application update", Object.entries(MAIL_EVENT_LABELS));
  if (finding.type === "action") {
    field("kind", "Corrected action", Object.entries(MAIL_ACTION_LABELS).filter(([id]) => id !== "other" || value.kind === "other"));
    if (value.kind === "other") field("task_kind", "Task for this request", [["", "Choose a task…"], ["follow_up", "Book or follow up"], ["send_document", "Send a document"]]);
    field("description", "Requested action");
    field("actor", "Who must act", [["applicant", "You"], ["employer", "Employer"], ["unknown", "Unclear"]]);
    field("obligation", "Request requirement", [["required", "Required"], ["optional", "Optional"], ["unclear", "Unclear"]]);
    field("channel", "Action channel", [["email", "Email"], ["portal", "Employer portal"], ["other", "Other"], ["unknown", "Unclear"]]);
  }
  if (finding.type === "temporal") {
    field("kind", "Time or deadline", [["interview", "Interview time"], ["deadline", "Deadline"]]);
    field("wording", "Date wording");
    field("starts_at", "Starts at (ISO date and time)"); field("ends_at", "Ends at (ISO date and time)");
    field("due_at", "Due at (ISO date and time)"); field("time_zone", "Time zone");
  }
  return {root, replacement() {
    if (!root.open) return null;
    const updated = {...value};
    for (const [name, input] of inputs) updated[name] = input.value || (["starts_at", "ends_at", "due_at", "time_zone"].includes(name) ? null : "");
    if (updated.kind !== "other") delete updated.task_kind;
    return JSON.stringify(updated) === JSON.stringify(value) ? null : updated;
  }};
}
function mailAnalysisItem(analysis, historical = false) {
  const row = node("article", "stack-item review-card mail-analysis");
  row.dataset.analysisId = analysis.analysis_id;
  const detail = node("div", "stack review-card-content");
  detail.append(node("h3", "", analysis.subject || "Email findings"));
  detail.append(meta([historical ? "Email review history" : "Needs review", displayDate(analysis.created_at)]));
  detail.append(reviewMessageDisclosure({kind:"mail_analysis", id:analysis.analysis_id}));
  const permalink = node("a", "review-context-link", "Open this email review");
  permalink.href = `#review/mail_analysis/${encodeURIComponent(analysis.analysis_id)}${historical ? "?history=true" : ""}`;
  detail.append(permalink);
  if (analysis.replay_id || analysis.mode === "replay") detail.append(node("p", "help", "Historical reprocessing. These findings do not appear in regular briefings."));
  if (analysis.current === false) detail.append(node("p", "help", "A newer analysis replaced this review. Its earlier findings remain here for reference."));
  detail.append(node("p", "help", "Choose each finding separately. Saving a request records the task; sending email and changing calendars have their own approvals."));
  for (const gap of analysis.coverage || []) detail.append(node("p", "notice", "Coverage: " + (gap.reason || "Some source material was not available").replaceAll("_", " ")));
  const controls = [];
  for (const finding of analysis.findings || []) {
    const block = node("fieldset", "mail-finding"); block.dataset.findingId = finding.finding_id;
    block.append(node("legend", "", mailFindingLabel(finding)));
    const value = finding.value || {};
    if (finding.type === "action") block.append(meta([MAIL_ACTION_LABELS[value.kind], value.obligation === "required" ? "Required request" : value.obligation === "optional" ? "Optional request" : "Requirement unclear", value.channel && value.channel.replaceAll("_", " ")]));
    if (finding.type === "temporal") block.append(meta([value.starts_at && "Starts " + displayDate(value.starts_at), value.ends_at && "Ends " + displayDate(value.ends_at), value.due_at && "Due " + displayDate(value.due_at), value.time_zone || "Time zone not confirmed"]));
    for (const evidence of value.evidence || []) {
      const quote = node("blockquote", "message-body", evidence.quote);
      quote.append(node("cite", "meta", "Source: " + evidence.source_id)); block.append(quote);
    }
    if (finding.replacement_of) block.append(node("p", "meta", "Reviewed correction of an earlier finding."));
    if (!["pending", "held"].includes(finding.status) || analysis.current === false) {
      block.append(node("p", "phase", ({accepted: "Accepted", rejected: "Rejected"})[finding.status] || finding.status));
      detail.append(block); continue;
    }
    const options = [["", "Leave for later"]];
    if (finding.type !== "uncertainty") options.push(["accepted", "Accept"]);
    options.push(["rejected", finding.type === "uncertainty" ? "Dismiss" : "Reject"]);
    const choice = mailChoice("Decision for " + mailFindingLabel(finding), options);
    const appChoices = [["", "Choose an application…"], ...(analysis.candidate_application_ids || []).map(id => {
      const app = state.applications.find(item => item.application_id === id);
      return [id, app ? `${app.employer_snapshot} · ${app.title_snapshot}` : id];
    })];
    if (value.application_id && !appChoices.some(([id]) => id === value.application_id)) appChoices.push([value.application_id, value.application_id]);
    const application = mailChoice("Application for " + mailFindingLabel(finding), appChoices, value.application_id || "");
    block.append(choice);
    if (finding.type !== "uncertainty") block.append(application);
    const correction = finding.type !== "uncertainty" ? mailCorrection(finding) : null;
    if (correction) block.append(correction.root);
    const reason = node("input"); reason.type = "text"; reason.maxLength = 512; reason.placeholder = "Optional explanation"; reason.setAttribute("aria-label", "Reason for " + mailFindingLabel(finding)); block.append(reason);
    controls.push({finding, choice, application, correction, reason}); detail.append(block);
  }
  const feedback = node("p", "notice"); feedback.hidden = true; feedback.setAttribute("role", "status");
  const save = node("button", "", "Save selected decisions"); save.type = "button"; save.disabled = true;
  function updateSave() {
    save.disabled = !controls.some(c => c.choice.value) || controls.some(c => {
      if (c.choice.value !== "accepted") return false;
      const value = c.correction?.replacement() || c.finding.value;
      return !c.application.value || (c.finding.type === "action" && value.kind === "other" && !value.task_kind);
    });
  }
  for (const c of controls) for (const input of [c.choice, c.application, ...(c.correction?.root.querySelectorAll("input,select") || [])]) input.addEventListener("change", updateSave);
  for (const c of controls) c.correction?.root.addEventListener("toggle", updateSave);
  save.addEventListener("click", async () => {
    const decisions = controls.filter(c => c.choice.value).map(c => {
      const decision = {finding_id: c.finding.finding_id, decision: c.choice.value, application_id: c.application.value || null, reason: c.reason.value || "Reviewed in dashboard"};
      const replacement = c.choice.value === "accepted" ? c.correction?.replacement() : null;
      if (replacement) decision.replacement = {...replacement, application_id: c.application.value || null};
      return decision;
    });
    if (!decisions.length) return;
    save.disabled = true; feedback.hidden = true;
    try {
      const updated = await api(`/api/v1/mail-analyses/${encodeURIComponent(analysis.analysis_id)}/decisions`, {method: "POST", headers: {"Idempotency-Key": key("mail-review")}, body: JSON.stringify({revision: analysis.revision, decisions})});
      row.replaceWith(mailAnalysisItem(updated, historical));
      await Promise.all([loadReviewQueue(), loadApplications()]);
      if (historical) await loadMailReviewHistory();
    } catch (error) {
      feedback.textContent = error.status === 409 ? "This email review changed. Refresh it before choosing decisions again; no decisions from this batch were saved." : error.message;
      feedback.hidden = false;
      if (error.status === 409) {
        const refresh = node("button", "quiet", "Refresh this email review"); refresh.type = "button";
        refresh.addEventListener("click", async () => { try { const updated = await api(`/api/v1/mail-analyses/${encodeURIComponent(analysis.analysis_id)}`); row.replaceWith(mailAnalysisItem(updated, historical)); } catch (failure) { feedback.textContent = failure.message; } });
        feedback.append(refresh);
      } else updateSave();
    }
  });
  if (controls.length) {
    const actions = node("footer", "actions review-card-actions");
    actions.append(save); row.append(actions);
  }
  detail.append(feedback); row.prepend(detail); return row;
}
let mailHistoryEpoch = 0;
let mailDetailRoute = "";
async function loadMailReviewRoute(encodedId) {
  const route = location.hash;
  if (mailDetailRoute === route) return;
  mailDetailRoute = route;
  try {
    const analysis = await api(`/api/v1/mail-analyses/${encodedId}`);
    if (location.hash !== route) return;
    const historical = !!analysis.replay_id || analysis.mode !== "shared" || new URLSearchParams(route.split("?")[1] || "").get("history") === "true";
    const list = historical ? $("#mail-history-list") : $("#attention-list");
    if (historical) $("#mail-review-history").open = true;
    const row = mailAnalysisItem(analysis, historical); row.tabIndex = -1; row.classList.add("review-selected");
    list.prepend(row); row.focus({preventScroll: true}); row.scrollIntoView({block: "center", behavior: "instant"});
  } catch (error) {
    const feedback = $("#review-feedback"); feedback.textContent = error.message; feedback.hidden = false; mailDetailRoute = "";
  }
}
async function loadMailReviewHistory() {
  const epoch = ++mailHistoryEpoch, button = $("#refresh-mail-history"), feedback = $("#mail-history-feedback");
  button.disabled = true; feedback.hidden = true;
  try {
    const result = await api("/api/v1/mail-analyses?history=true&limit=100");
    if (epoch !== mailHistoryEpoch) return;
    const list = $("#mail-history-list"); clear(list);
    for (const analysis of result.analyses || []) list.append(mailAnalysisItem(analysis, true));
    if (!(result.analyses || []).length) list.append(node("p", "empty", "No email review history."));
    if ((result.analyses || []).length === 100) list.prepend(node("p", "help", "Showing the latest 100 email reviews."));
  } catch (error) { if (epoch === mailHistoryEpoch) { feedback.textContent = error.message; feedback.hidden = false; } }
  finally { if (epoch === mailHistoryEpoch) button.disabled = false; }
}
document.querySelector("#refresh-mail-history").addEventListener("click", loadMailReviewHistory);

function reviewItem(normalized) {
    const item = normalized.raw;
    if (item.kind === "mail_analysis") return mailAnalysisItem(item.analysis);
    const row = node("article", "stack-item review-card");
    const detail = node("div", "review-card-content");
    const header = node("header", "review-card-header");
    if (item.subject) header.append(node("p", "review-card-kind", normalized.title));
    header.append(node("h3", "", item.subject || normalized.title));
    header.append(meta([item.sender, reviewStatus(normalized), `Received ${displayDate(item.created_at)}`]));
    detail.append(header);
    let technical;
    if (item.confidence !== undefined) {
      technical = node("details", "technical-details");
      technical.append(node("summary", "", "Review details"), node("p", "meta", `${Math.round(item.confidence * 100)}% confidence`));
    }
    const application = state.applications.find(app => app.application_id === item.application_id);
    if (application) { detail.append(jobPreviewButton(application, `${application.employer_snapshot} · ${application.title_snapshot}`)); const link = node("a", "review-context-link", "Open application messages"); link.href = applicationHref(application.application_id, "messages"); detail.append(link); }
    if (item.evidence_quote) detail.append(node("blockquote", "review-evidence", item.evidence_quote));
    const suggestion = renderReviewSuggestion(item, detail);
    detail.append(reviewMessageDisclosure(item));
    if (technical) detail.append(technical);
    if (item.kind === "temporal_proposal") {
      const when = item.detail === "interview"
        ? `${displayDate(item.starts_at)} – ${displayDate(item.ends_at)}`
        : `Due ${displayDate(item.due_at)}`;
      detail.append(meta([item.employer, item.title]));
      detail.append(node("p", "meta", `${when} · ${item.time_zone}`));
      detail.append(node("p", "help", "Saves the proposed time to this application. It does not accept an invitation or notify anyone."));
    }
    const actions = node("footer", "actions review-card-actions");
    if (["lifecycle_correction","interview_revision","mail_discovery"].includes(item.kind)) renderLifecycleReview(item, detail, actions);
    if (item.kind === "browser_submission") {
      detail.append(node("p", "", item.detail));
      const link = node("a", "review-context-link", "Check application record");
      link.href = applicationHref(item.application_id, "overview");
      actions.append(link);
    }
    if (item.kind === "mail_processing_failure") {
      const analysis = reviewFailureAnalyses.get(reviewFailureIdentity(item));
      const analyzing = ["pending", "running"].includes(analysis?.status);
      if (analyzing) {
        const progress = node("p", "review-analysis-status meta", "Finding matching job and suggested action…");
        progress.setAttribute("role", "status");
        detail.append(progress);
      } else if (analysis?.status === "failed") {
        detail.append(node("p", "review-analysis-status meta", "A suggested action could not be prepared. You can try again or use the controls below."));
        const error = node("details");
        error.append(node("summary", "", "Analysis details"), node("p", "meta", analysis.error));
        detail.append(error);
      } else if (analysis?.status === "complete") {
        detail.append(node("p", "review-analysis-status meta", "Analysis completed. Refresh to see the prepared review item."));
      }
      if (!analyzing) detail.append(node("p", "meta", "Retry this email on the next mailbox sync, or dismiss it if no application update is needed. Dismissing keeps the email in Outlook."));
      if (item.can_analyze_archive === true) {
        const analyze = node("button", analyzing ? "quiet" : "review-suggested-action", analysis?.status === "failed" ? "Retry suggested action" : "Find suggested action");
        analyze.type = "button";
        analyze.disabled = analyzing || analysis?.status === "complete";
        analyze.addEventListener("click", () => queueReviewFailureAnalysis(item));
        actions.append(analyze);
      }
      if (item.error) {
        const explanation = /evidence quote.*span|span.*sanitized/i.test(item.error)
          ? "The analysis could not verify its supporting text in the email. No application status was changed."
          : "The email processing step failed. No application update was approved.";
        detail.append(node("p", "meta", explanation));
        const debug = node("details");
        debug.append(node("summary", "", "Technical details"), node("p", "meta", item.error));
        detail.append(debug);
      }
      try {
        const url = new URL(item.web_link);
        if (url.protocol === "https:" && ["outlook.live.com", "outlook.office.com", "outlook.office365.com"].includes(url.hostname)) {
          const link = node("a", "review-context-link", "Open in Outlook");
          link.href = url.href; link.target = "_blank"; link.rel = "noopener noreferrer";
          actions.append(link);
        }
      } catch (_) {}
      const suggestedFailureAction = item.suggested_resolution?.action === "dismiss" || item.can_retry === false ? "dismiss" : "retry";
      for (const [action, label] of [["retry", "Retry processing"], ["dismiss", "Dismiss"]]) {
        const button = node("button", !item.can_analyze_archive && action === suggestedFailureAction ? "review-suggested-action" : "quiet", label); button.type = "button";
        button.disabled = analyzing || action === "retry" && item.can_retry === false;
        const commandId = key("mail-review");
        button.addEventListener("click", async () => {
          const buttons = [...actions.querySelectorAll("button")];
          buttons.forEach(b => { b.disabled = true; });
          try {
            await api("/api/v1/mail/failures/resolve", {method:"POST", body:JSON.stringify({
              idempotency_key:commandId, account_id:item.account_id, folder_ref:item.folder_ref,
              message_id:item.id, query_version:item.query_version, action,
            })});
            await loadReviewQueue();
            notice(action === "retry" ? "Email queued for the next mailbox sync." : "Review item dismissed. The email remains in Outlook.");
          } catch (error) {
            buttons.forEach(b => { b.disabled = b.textContent === "Retry processing" && item.can_retry === false; });
            notice(error.message);
          }
        });
        actions.append(button);
      }
    }
    if (item.kind === "event_proposal") {
      const candidateIds = [...new Set([item.application_id, ...(item.candidate_application_ids || [])].filter(Boolean))];
      const jobs = reviewJobMatches(item);
      const recommendedId = recommendedReviewSelection(item, candidateIds);
      let selectedApplicationId = item.suggested_resolution ? recommendedId : item.application_id;
      const accept = node("button", "review-suggested-action", reviewEventActionLabel(item));
      if (jobs.length || (candidateIds.length && (!item.application_id || candidateIds.length > 1 || item.suggested_resolution?.requires_selection))) {
        const select = node("select");
        select.setAttribute("aria-label", "Application for this proposal");
        select.dataset.reviewApplication = "true";
        const placeholder = node("option", "", "Choose an application…");
        placeholder.value = "";
        select.append(placeholder);
        const orderedIds = [...new Set([...(item.application_matches || []).map(match => match.application_id).filter(id => candidateIds.includes(id)), ...candidateIds])];
        orderedIds.forEach((candidate) => {
          const match = state.applications.find(app => app.application_id === candidate);
          const suggestedMatch = (item.application_matches || []).find(app => app.application_id === candidate);
          const description = match ? `${match.employer_snapshot} · ${match.title_snapshot}`
            : suggestedMatch ? `${suggestedMatch.employer} · ${suggestedMatch.title}` : candidate;
          const option = node("option", "", `${description}${candidate === recommendedId ? " (suggested)" : ""}`);
          option.value = candidate;
          select.append(option);
        });
        for (const job of jobs) {
          const value = reviewJobValue(job);
          const option = node("option", "", `${job.company} · ${job.title} · New application${value === recommendedId ? " (suggested)" : ""}`);
          option.value = value;
          select.append(option);
        }
        select.value = selectedApplicationId;
        select.addEventListener("change", () => {
          select.dataset.reviewSelectionChanged = "true";
          selectedApplicationId = select.value;
          accept.disabled = !selectedApplicationId;
        });
        const choice = node("label", "review-application-choice", "Application or job");
        choice.append(select);
        (suggestion || detail).append(choice);
      } else if (!selectedApplicationId) {
        (suggestion || detail).append(node("p", "review-match-empty", "No matching application yet. Use Resolve email to find or create the right record."));
      }
      accept.type = "button";
      accept.disabled = !selectedApplicationId;
      accept.addEventListener("click", () => decideProposal(item, "accepted", selectedApplicationId, accept));
      const reject = node("button", "danger", "Reject");
      reject.type = "button";
      reject.addEventListener("click", () => decideProposal(item, "rejected", selectedApplicationId, reject));
      actions.append(accept, reject);
      const editor = mailResolutionEditor(item);
      detail.append(editor);
      const resolve = node("button", "quiet", "Resolve email"); resolve.type = "button";
      resolve.addEventListener("click", () => { editor.open = true; editor.querySelector("select")?.focus(); });
      actions.append(resolve);
    }
    if (item.kind === "temporal_proposal") {
      const accept = node("button", "", item.kind === "temporal_proposal" ? (item.detail === "interview" ? "Save proposed time" : "Save deadline") : "Confirm update");
      accept.type = "button";
      accept.addEventListener("click", () => decideTemporalProposal(item, "accepted", accept));
      const reject = node("button", "danger", "Reject");
      reject.type = "button";
      reject.addEventListener("click", () => decideTemporalProposal(item, "rejected", reject));
      actions.append(accept, reject);
    }
    row.append(detail, actions);
    return row;
}
async function decideAction(action, approve, button) {
  button.disabled = true;
  try {
    await api(`/api/v1/actions/${action.action_id}/decision`, {
      method: "POST",
      body: JSON.stringify({
        idempotency_key: key("action"),
        approve,
        payload_sha256: action.payload_sha256,
      }),
    });
    await Promise.all([loadReviewQueue(), refreshApplicationWorkspace()]);
  } catch (error) {
    button.disabled = false;
    notice(error.message);
  }
}

async function reconcileAction(action, resolution, remoteId, button) {
  button.disabled = true;
  try {
    await api(`/api/v1/actions/${action.action_id}/reconcile`, {
      method: "POST",
      body: JSON.stringify({
        idempotency_key: key("reconcile"),
        resolution,
        remote_id: remoteId || "",
      }),
    });
    await Promise.all([loadReviewQueue(), refreshApplicationWorkspace()]);
  } catch (error) {
    button.disabled = false;
    notice(error.message);
  }
}

function actionItem(action) {
    const row = node("article", "stack-item review-card");
    const detail = node("div", "review-card-content");
    detail.append(node("h3", "", ({ outlook_reply_draft: "Reply draft", outlook_calendar_hold: "Private interview hold" })[action.kind] || action.kind.replaceAll("_", " ")));
    detail.append(meta([action.status === "needs_reconciliation" ? "Check outcome" : action.status.replaceAll("_", " "), action.expires_at ? `Expires ${displayDate(action.expires_at)}` : ""]));
    if (action.status === "pending") detail.append(node("p", "help", action.kind === "outlook_reply_draft" ? "Creates a draft in Outlook for you to review and send." : "Creates a private, tentative calendar hold. No attendees are invited."));
    detail.append(actionPreview(action));

    const actions = node("footer", "actions review-card-actions");
    if (action.status === "pending") {
      const approve = node("button", "", action.kind === "outlook_reply_draft" ? "Create Outlook draft" : "Create private tentative hold");
      approve.type = "button";
      approve.addEventListener("click", () => decideAction(action, true, approve));
      const reject = node("button", "danger", "Reject");
      reject.type = "button";
      reject.addEventListener("click", () => decideAction(action, false, reject));
      actions.append(approve, reject);
    } else if (action.status === "needs_reconciliation") {
      detail.append(node("p", "section-note", "Check Outlook before resolving this uncertain write."));
      const notCreated = node("button", "", "No item was created");
      notCreated.type = "button";
      notCreated.addEventListener("click", () => reconcileAction(action, "not_created", "", notCreated));
      const abandon = node("button", "danger", "Abandon action");
      abandon.type = "button";
      abandon.addEventListener("click", () => reconcileAction(action, "abandon", "", abandon));
      const created = node("button", "", "Item exists in Outlook");
      created.type = "button";
      created.addEventListener("click", () => reconcileAction(action, "created", "", created));
      actions.append(created, notCreated, abandon);
    } else {
      actions.append(node("span", "phase", action.status));
    }
    row.append(detail, actions);
    return row;
}

function initializeDiscoveryViews() {
  $("#shortlist-sort").addEventListener("change", changeShortlistSort);
  $("#shortlist-form").addEventListener("submit", refreshShortlist);
  // Editing model controls explicitly opts into that source; returning from another
  // window must not replace those choices with newly published saved lists.
  $("#shortlist-form").addEventListener("input", () => { shortlistSource = "model"; });
  $("#shortlist-form").addEventListener("change", () => { shortlistSource = "model"; });
  $("#shortlist-source").addEventListener("change", () => {
    shortlistSource = $("#shortlist-source").value;
    if (location.hash !== "#shortlist") location.hash = "#shortlist";
    loadSavedShortlists();
  });
  $("#curated-list").addEventListener("change", () => {
    shortlistSource = "curated";
    location.hash = $("#curated-list").value ? `#shortlist/${$("#curated-list").value}` : "#shortlist";
    loadSavedShortlists();
  });
  $("#refresh-curated").addEventListener("click", () => loadSavedShortlists());
  $("#older-curated").addEventListener("click", () => loadSavedShortlists(true));
  $("#refresh-attention").addEventListener("click", loadReviewQueue);
  window.addEventListener("focus", () => {
    if (consoleState.view === "shortlist" && !state.shortlistLoading
      && (state.shortlist?.source === "curated" || shortlistSource === "auto")) loadSavedShortlists();
  });
}
