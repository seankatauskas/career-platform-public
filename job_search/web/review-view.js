// One normalized source drives Review, navigation counts, and application notices.
document.querySelector("#attention").innerHTML = `
  <div class="section-heading">
    <div><h2>Review</h2><p class="section-note">Decisions and updates waiting for you.</p></div>
    <button class="quiet" id="refresh-attention" type="button">Refresh</button>
  </div>
  <p id="review-feedback" class="notice" role="status" hidden></p>
  <p id="review-summary" class="meta" aria-live="polite"></p>
  <div id="attention-list" class="stack empty">Loading review items…</div>
  <details id="review-history" class="review-history">
    <summary>Action history</summary>
    <p class="help">Previously reviewed drafts and calendar holds.</p>
    <div id="action-list" class="stack"></div>
  </details>
`;

function actionNeedsReview(action) { return action.status === "pending" || action.status === "needs_reconciliation"; }
function reviewTitle(item) {
  if (item.kind === "lifecycle_correction") return "Proposed application update";
  if (item.kind === "interview_revision") return "Interview change";
  if (item.kind === "mail_discovery") return "Untracked recruiting conversation";
  if (item.kind === "event_proposal") return ({interview_requested: "Interview request", submission_confirmed: "Application confirmation", rejected: "Application outcome", offer_received: "Offer received"})[item.detail] || (item.detail || "Application update").replaceAll("_", " ");
  if (item.kind === "temporal_proposal") return item.detail === "interview" ? "Proposed interview time" : "Application deadline";
  if (item.kind === "browser_submission") return "Check submission";
  if (item.kind === "mail_processing_failure") return item.detail || "Email processing needs attention";
  return ({outlook_reply_draft: "Reply draft", outlook_calendar_hold: "Private interview hold"})[item.kind] || "Application action";
}
function reviewStatus(item) {
  return item.status === "needs_reconciliation" ? "Check outcome" : item.kind === "outlook_reply_draft" || item.kind === "outlook_calendar_hold" ? "Needs approval" : "Needs review";
}
function getReviewItems() {
  const rows = [...consoleState.reviews.filter(item => item.kind !== "action_proposal" && [undefined, "review", "pending", "conflict", "failed"].includes(item.status)), ...consoleState.actions.filter(actionNeedsReview)];
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
  const list = document.querySelector("#attention-list");
  // Async attention/action refreshes replace cards. Preserve the user's native
  // disclosure state for the same review identity, including mailbox scope.
  const disclosures = captureReviewDisclosures(list);
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
}
let reviewAttentionEpoch = 0;
let reviewActionsEpoch = 0;
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
async function loadAttention() { try { await fetchReviewAttention(); } finally { renderReviewQueue(); } }
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
  } finally {
    if (epoch === reviewLoadEpoch) { button.disabled = false; document.querySelector("#attention-list").removeAttribute("aria-busy"); }
  }
}

function actionPreview(action) {
  const root = node("div", "action-preview");
  const payload = action.payload || {};
  const app = state.applications.find(item => item.application_id === action.application_id);
  if (app) { root.append(jobPreviewButton(app, `${app.employer_snapshot} · ${app.title_snapshot}`)); const link = node("a", "review-context-link", "Open application"); link.href = applicationHref(app.application_id, "overview"); root.append(link, postingDates(app)); }
  if (payload.body) root.append(node("p", "message-body", typeof payload.body === "string" ? payload.body : payload.body.content || ""));
  if (payload.subject) root.append(node("p", "", payload.subject));
  if (payload.start || payload.starts_at) root.append(node("p", "", `${displayDate(payload.starts_at || payload.start?.dateTime || payload.start)} → ${displayDate(payload.ends_at || payload.end?.dateTime || payload.end)}`));
  const details = node("details", "technical-details"); details.append(node("summary", "", "Action details"), node("pre", "payload-preview", JSON.stringify(payload, null, 2)), node("p", "meta", `Approved content fingerprint: ${(action.payload_sha256 || "").slice(0, 12)}`)); root.append(details);
  return root;
}

async function decideProposal(item, decision, selectedApplicationId, button) {
  button.disabled = true;
  try {
    await api(`/api/v1/proposals/${item.id}/decision`, {
      method: "POST",
      body: JSON.stringify({
        idempotency_key: key("review"),
        decision,
        selected_application_id: selectedApplicationId,
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

function reviewItem(normalized) {
    const item = normalized.raw;
    const row = node("article", "stack-item");
    const detail = node("div");
    detail.append(node("h3", "", normalized.title));
    detail.append(meta([reviewStatus(normalized), `Received ${displayDate(item.created_at)}`]));
    if (item.confidence !== undefined) {
      const technical = node("details", "technical-details");
      technical.append(node("summary", "", "Review details"), node("p", "meta", `${Math.round(item.confidence * 100)}% confidence`));
      detail.append(technical);
    }
    const application = state.applications.find(app => app.application_id === item.application_id);
    if (application) { detail.append(jobPreviewButton(application, `${application.employer_snapshot} · ${application.title_snapshot}`)); const link = node("a", "review-context-link", "Open application messages"); link.href = applicationHref(application.application_id, "messages"); detail.append(link); }
    if (item.evidence_quote) detail.append(node("p", "meta", `“${item.evidence_quote}”`));
    if (item.kind === "temporal_proposal") {
      const when = item.detail === "interview"
        ? `${displayDate(item.starts_at)} – ${displayDate(item.ends_at)}`
        : `Due ${displayDate(item.due_at)}`;
      detail.append(meta([item.employer, item.title]));
      detail.append(node("p", "meta", `${when} · ${item.time_zone}`));
      detail.append(node("p", "help", "Saves the proposed time to this application. It does not accept an invitation or notify anyone."));
    }
    const actions = node("div", "actions");
    if (["lifecycle_correction","interview_revision","mail_discovery"].includes(item.kind)) renderLifecycleReview(item, detail, actions);
    if (item.kind === "event_proposal") detail.append(node("p", "help", "Confirm that this email updates the application. This does not reply to the sender or accept an invitation."));
    if (item.kind === "browser_submission") {
      detail.append(node("p", "", item.detail));
      const link = node("a", "review-context-link", "Check application record");
      link.href = applicationHref(item.application_id, "overview");
      actions.append(link);
    }
    if (item.kind === "mail_processing_failure") {
      detail.append(node("p", "meta", "This email could not be analyzed. Retry it on the next mailbox sync, or dismiss it if no application update is needed. Dismissing keeps the email in Outlook."));
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
      for (const [action, label] of [["retry", "Retry processing"], ["dismiss", "Dismiss"]]) {
        const button = node("button", "", label); button.type = "button";
        button.disabled = action === "retry" && item.can_retry === false;
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
      let selectedApplicationId = item.application_id;
      const accept = node("button", "", "Confirm update");
      if (!selectedApplicationId && (item.candidate_application_ids || []).length) {
        const select = node("select");
        select.setAttribute("aria-label", "Application for this proposal");
        const placeholder = node("option", "", "Choose an application…");
        placeholder.value = "";
        select.append(placeholder);
        item.candidate_application_ids.forEach((candidate) => {
          const match = state.applications.find(app => app.application_id === candidate);
          const option = node("option", "", match ? `${match.employer_snapshot} · ${match.title_snapshot}` : candidate);
          option.value = candidate;
          select.append(option);
        });
        select.addEventListener("change", () => {
          selectedApplicationId = select.value;
          accept.disabled = !selectedApplicationId;
        });
        actions.append(select);
      } else if (!selectedApplicationId) {
        actions.append(node("p", "review-context-link", "No matching application yet. This email will stay here for review; refresh after the application appears."));
      }
      accept.type = "button";
      accept.disabled = !selectedApplicationId;
      accept.addEventListener("click", () => decideProposal(item, "accepted", selectedApplicationId, accept));
      const reject = node("button", "danger", "Reject");
      reject.type = "button";
      reject.addEventListener("click", () => decideProposal(item, "rejected", selectedApplicationId, reject));
      actions.append(accept, reject);
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
    const row = node("article", "stack-item");
    const detail = node("div");
    detail.append(node("h3", "", ({ outlook_reply_draft: "Reply draft", outlook_calendar_hold: "Private interview hold" })[action.kind] || action.kind.replaceAll("_", " ")));
    detail.append(meta([action.status === "needs_reconciliation" ? "Check outcome" : action.status.replaceAll("_", " "), action.expires_at ? `Expires ${displayDate(action.expires_at)}` : ""]));
    if (action.status === "pending") detail.append(node("p", "help", action.kind === "outlook_reply_draft" ? "Creates a draft in Outlook for you to review and send." : "Creates a private, tentative calendar hold. No attendees are invited."));
    detail.append(actionPreview(action));

    const actions = node("div", "actions");
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
