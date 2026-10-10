// Page-owned templates and rendering. Shared state and UI primitives live in app.js / console.js.
document.querySelector("#applications").innerHTML = `
      <div class="section-heading">
        <div><h2>Applications</h2><p id="application-count" class="meta application-total"></p></div>
        <button class="quiet" id="refresh-applications" type="button">Refresh</button>
      </div>
      <p class="section-note">Track your applications, conversations, and next steps.</p>
      <div class="application-filters controls">
        <label>Search roles<input id="application-search" type="search" placeholder="Company or role"></label>
        <label id="application-phase-label">Stage<select id="application-phase"><option value="">All stages</option><option value="awaiting_confirmation">Awaiting confirmation</option><option value="active">Submitted</option><option value="interviewing">Interviewing</option><option value="offer">Offer</option><option value="terminal">Closed</option></select></label>
        <label class="application-review-filter"><input id="application-needs-review" type="checkbox"><span id="application-review-filter-label">Needs review</span></label>
      </div>
      <div class="application-layout">
        <div id="application-list" class="empty">Loading applications…</div>
        <a class="workspace-back" href="#applications">← All applications</a>
        <aside id="application-workspace" aria-label="Application detail" hidden>
          <header class="workspace-heading"><p class="workspace-label">Application record</p><h3 id="workspace-title" tabindex="-1"></h3><p id="workspace-company" class="kicker"></p><div id="workspace-posting-dates"></div><div id="workspace-job-preview"></div><div id="workspace-status"></div></header>
          <nav class="workspace-tabs" aria-label="Application detail tabs">
            <a data-tab="overview" href="#applications">Overview</a><a data-tab="messages" href="#applications">Messages</a><a data-tab="answers" href="#applications">Answers</a><a data-tab="documents" href="#applications">Documents</a>
          </nav>
          <div id="workspace-feedback" role="status" hidden></div>
          <div id="workspace-overview"><div id="workspace-lifecycle"></div><div id="workspace-review-notices"></div><div id="workspace-interviews"></div><h4>Application history</h4><div id="timeline" class="timeline"></div><details id="workspace-posting-history"><summary>Posting history</summary><div id="workspace-job-history"></div></details></div>
          <div id="workspace-messages" hidden></div>
          <div id="workspace-answers" hidden></div>
          <div id="workspace-documents" hidden></div>
        </aside>
      </div>
`;

function applicationScopeRows() {
  return state.applicationBackend === "owners" ? state.applications : state.applications.filter(app => app.current_phase !== "preparing" || app.ats === "external");
}
function applicationReviewNotice(item) {
  const link = node("a", "pending-note", `${item.title} · ${reviewStatus(item)}`);
  link.href = reviewHref(item);
  return link;
}
function renderApplicationReviewNotices() {
  const root = document.querySelector("#workspace-review-notices");
  root.replaceChildren();
  const app = consoleState.workspace?.application;
  if (!app || app.application_id !== consoleState.applicationId) return;
  if (state.applicationBackend === "owners") {
    renderOwnerApplicationReviews(consoleState.workspace);
    return;
  }
  if (app.current_phase === "preparing") {
    root.append(node("p", "section-note", "Saved draft. This historical record is read-only; no submission has been recorded."));
    return;
  }
  const items = reviewItemsForApplication(app.application_id);
  if (items.length) {
    root.append(node("h4", "", "Needs your review"));
    items.forEach(item => root.append(applicationReviewNotice(item)));
  }
}
function renderApplicationTable() {
  const root = document.querySelector("#application-list");
  if (!root) return;
  root.replaceChildren();
  root.className = "application-list";
  const scoped = applicationScopeRows();
  document.querySelector("#application-count").textContent = `${scoped.length} ${scoped.length === 1 ? "application" : "applications"}`;
  const query = document.querySelector("#application-search").value.trim().toLowerCase();
  const phase = document.querySelector("#application-phase").value;
  const needsReview = document.querySelector("#application-needs-review").checked;
  const reviewIncomplete = state.applicationBackend === "owners" && ownerReviewHasMore();
  document.querySelector("#application-review-filter-label").textContent = reviewIncomplete ? "Needs review (loaded items)" : "Needs review";
  if (needsReview && reviewIncomplete) {
    const coverage = node("p", "section-note", "This filter covers the review items loaded so far. More items are available in Review. ");
    const link = node("a", "", "Open Review"); link.href = "#review"; coverage.append(link); root.append(coverage);
  }
  const matches = scoped.filter(app => (!phase || app.current_phase === phase)
    && `${app.employer_snapshot} ${app.title_snapshot}`.toLowerCase().includes(query)
    && (!needsReview || reviewItemsForApplication(app.application_id).length));
  if (!matches.length) {
    root.append(node("p", "empty", scoped.length ? "No applications match these filters." : state.applicationBackend === 'owners' ? "No applications tracked yet. Find your next role in Shortlist." : "No submitted applications yet. Find your next role in Shortlist."));
    return;
  }
  const table = node("table", "data-table");
  const head = node("thead"); const tr = node("tr");
  ["Role / company", "Stage", "Application updated"].forEach(label => { const th = node("th", "", label); th.scope = "col"; tr.append(th); });
  head.append(tr); table.append(head);
  const body = node("tbody");
  matches.forEach(app => {
    const row = node("tr", app.application_id === consoleState.applicationId ? "selected" : "");
    const identity = node("td");
    identity.append(jobSummary(app, true));
    const dates = postingDates(app, true);
    const appliedDate = postingDate(app.submitted_at);
    if (appliedDate) {
      const applied = node("time", "applied-date", `Applied ${appliedDate}`);
      applied.dateTime = app.submitted_at;
      applied.title = `Applied ${displayDate(app.submitted_at)}`;
      dates.append(applied);
    } else dates.append(node("span", "applied-date", app.current_phase === "preparing" ? "Not applied yet" : "Applied date not recorded"));
    identity.append(dates);
    reviewItemsForApplication(app.application_id).forEach(item => identity.append(applicationReviewNotice(item)));
    const stage = node("td"); stage.append(node("span", `phase ${app.current_phase}`, applicationStatusLabel(app)));
    row.append(identity, stage, node("td", "table-date", displayDate(app.updated_at))); body.append(row);
  });
  table.append(body); root.append(table);
}
async function refreshApplicationWorkspace() {
  const id = consoleState.applicationId;
  if (!id || consoleState.view !== "applications") return;
  const epoch = ++consoleState.epoch;
  const workspace = document.querySelector("#application-workspace");
  workspace.hidden = false;
  workspace.setAttribute("aria-busy", "true");
  document.querySelector("#workspace-feedback").hidden = true;
  if (!consoleState.workspace || consoleState.workspace.application.application_id !== id) {
    document.querySelector("#workspace-company").textContent = "Application";
    document.querySelector("#workspace-title").textContent = "Loading…";
    for (const selector of ["#workspace-lifecycle", "#workspace-status", "#workspace-job-preview", "#workspace-posting-dates", "#workspace-review-notices", "#workspace-interviews", "#timeline", "#workspace-messages", "#workspace-answers", "#workspace-documents", "#workspace-job-history"]) document.querySelector(selector).replaceChildren();
  }
  updateWorkspaceTabs();
  try {
    const data = await api(`/api/v1/applications/${encodeURIComponent(id)}/workspace`);
    if (epoch !== consoleState.epoch || id !== consoleState.applicationId || consoleState.view !== "applications") return;
    consoleState.workspace = data;
    if (state.applicationBackend !== "owners") renderLifecycleBriefing(data.briefing);
    const app = data.application;
    const savedDraft = state.applicationBackend !== "owners" && app.current_phase === "preparing" && app.ats !== "external";
    const back = document.querySelector(".workspace-back");
    back.href = savedDraft ? "#settings/stored-records" : "#applications";
    back.textContent = savedDraft ? "← Stored records" : "← All applications";
    document.querySelector(".workspace-label").textContent = savedDraft ? "Saved draft" : "Application record";
    document.querySelector("#workspace-company").textContent = app.employer_snapshot;
    document.querySelector("#workspace-title").textContent = app.title_snapshot;
    const dates = postingDates(app);
    const appliedDate = postingDate(app.submitted_at);
    if (appliedDate) {
      const applied = node("time", "posting-date", `Applied ${appliedDate}`);
      applied.dateTime = app.submitted_at;
      applied.title = `Applied ${displayDate(app.submitted_at)}`;
      dates.append(document.createTextNode(" · "), applied);
    }
    document.querySelector("#workspace-posting-dates").replaceChildren(jobContext(app, false), dates);
    for(const warning of app.submission_summary?.warnings || []) document.querySelector('#workspace-posting-dates').append(node('p','submission-warning',warning.message));
    const preview = jobPreviewButton(app, "Preview job description");
    preview.className = "quiet";
    const jobActions = document.querySelector("#workspace-job-preview");
    jobActions.replaceChildren(preview);
    const posting = previewPostingLink(app.job_posting?.jobUrl || app.job_url_snapshot);
    if (posting) {
      posting.className = "workspace-posting-link";
      posting.textContent = "Open posting ↗";
      jobActions.append(posting);
    }
    renderJobHistory(data.job_history || {events: [], history_note: "Posting history is unavailable."}, document.querySelector("#workspace-job-history"));
    document.querySelector("#workspace-status").replaceChildren(node("span", `phase ${app.current_phase}`, applicationStatusLabel(app)));
    renderApplicationReviewNotices();
    const interviews = document.querySelector("#workspace-interviews"); interviews.replaceChildren();
    for (const interview of data.briefing ? [] : (data.interviews || [])) {
      interviews.append(node("h4", "", "Interview"), node("p", "meta", `${displayDate(interview.starts_at)} – ${displayDate(interview.ends_at)} · ${interview.time_zone || ""}`));
    }
    const history = document.querySelector("#timeline"); history.replaceChildren();
    const confirmationEmail = event => event.event_type === "submission_confirmed" ? event.email_evidence : null;
    const timelineDate = event => confirmationEmail(event)?.received_at || event.occurred_at;
    [...data.events].sort((a, b) => Date.parse(timelineDate(b)) - Date.parse(timelineDate(a)) || b.event_seq - a.event_seq).forEach(event => {
      const entry = node("article", "timeline-event");
      const email = confirmationEmail(event);
      const source = String(event.source_kind || "application record").replaceAll("_", " ");
      entry.append(node("strong", "", stageLabel(event.event_type)), node("p", "meta", `${displayDate(timelineDate(event))}${email ? " · Confirmation email" : ""}`));
      if (email) {
        const details = node("details");
        details.append(node("summary", "", "Confirmation details"));
        details.append(node("p", "", email.subject), node("p", "meta", `From ${email.sender}`));
        details.append(node("blockquote", "", email.evidence_quote));
        details.append(node("p", "meta", `Email received ${displayDate(email.received_at)}`));
        details.append(node("p", "meta", `Linked through ${source === "codex" ? "Codex" : source} at ${displayDate(event.recorded_at)}`));
        entry.append(details);
      } else {
        const details = node("details");
        details.append(node("summary", "", "Event details"), node("p", "meta", `Recorded through ${source === "codex" ? "Codex" : source}${event.recorded_at ? ` at ${displayDate(event.recorded_at)}` : ""}`));
        entry.append(details);
      }
      history.append(entry);
    });
    if (!data.events.length) history.append(node("p", "empty", "No application history has been recorded yet."));
    if (data.browser_observations?.length) {
      const evidence=node("details", "timeline-event");
      evidence.append(node("summary", "", "Browser submission evidence"));
      const labels={attempted:"Submission attempted",request_sent:"Application request sent",request_completed:"Request completed; acceptance unconfirmed",site_acknowledged:"Website acknowledged submission",failed:"Submission failed"};
      for(const item of data.browser_observations) {
        const kind = item.kind || item.source?.kind || item.activity;
        evidence.append(node("p", "meta", `${displayDate(item.occurred_at)} · ${labels[kind] || stageLabel(kind)}`));
      }
      history.append(evidence);
    }
    if (state.applicationBackend === "owners") renderOwnerApplicationWorkspace(data);
    else renderLifecycleConversation(document.querySelector("#workspace-messages"), data);
    renderApplicationDocuments(data.documents || []);
    renderApplicationAnswers(data.answer_snapshots || []);
  } catch (error) {
    if (epoch !== consoleState.epoch) return;
    const feedback = document.querySelector("#workspace-feedback"); feedback.hidden = false;
    feedback.replaceChildren(node("p", "", `Could not load this application. ${error.message}`));
    const retry = node("button", "quiet", "Retry"); retry.onclick = refreshApplicationWorkspace; feedback.append(retry);
  } finally {
    if (epoch === consoleState.epoch) workspace.removeAttribute("aria-busy");
  }
}
function updateWorkspaceTabs() {
  document.querySelectorAll(".workspace-tabs a").forEach(link => {
    link.href = applicationHref(consoleState.applicationId, link.dataset.tab);
    if (link.dataset.tab === consoleState.tab) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
    document.querySelector(`#workspace-${link.dataset.tab}`).hidden = link.dataset.tab !== consoleState.tab;
  });
}
function renderApplicationDocuments(documents) {
  const root = document.querySelector("#workspace-documents");
  root.replaceChildren(node("h4", "", "Recorded documents"));
  if (!documents.length) {
    root.append(node("p", "empty", "No resume was recorded for this application."));
    return;
  }
  for (const document of documents) {
    const item = node("article", "application-document");
    item.append(node("h5", "", document.name || "Resume"));
    item.append(node("p", "meta", `${document.source || "Recorded resume"}${document.recorded_at ? ` · ${displayDate(document.recorded_at)}` : ""}`));
    if (document.available && document.preview_url && document.url) {
      const actions = node("div", "actions");
      const view = node("a", "workspace-posting-link", "View PDF ↗");
      view.href = document.preview_url; view.target = "_blank"; view.rel = "noopener noreferrer";
      const download = node("a", "text-link", "Download PDF"); download.href = document.url;
      actions.append(view, download); item.append(actions);
    } else item.append(node("p", "section-note", document.reason || "The recorded document is unavailable."));
    root.append(item);
  }
}

function renderApplicationAnswers(snapshots) {
  const root=document.querySelector('#workspace-answers');
  root.replaceChildren(node('h4','','Saved application answers'));
  if(!snapshots.length) {
    root.append(node('p','empty','No answers were captured for this application. Answer capture starts with browser extension version 1.3.'));
    return;
  }
  root.append(node('p','help','Captured when you attempted to submit. Earlier form steps are included; this record does not confirm which answers the employer accepted.'));
  snapshots.forEach((saved,index)=>{
    const history=node('details','answer-snapshot'); history.open=index===0;
    const snapshot=saved.snapshot;
    if (!snapshot || !Array.isArray(snapshot.fields)) {
      history.append(node("summary", "", "Recorded answers"), node("pre", "answer-value", JSON.stringify(snapshot ?? saved, null, 2)));
      if (saved.review_status === "unreviewed") history.append(node("p", "section-note", "Preserved browser capture; its link to a submitted application has not been reviewed."));
      root.append(history); return;
    }
    history.append(node('summary','',`${index===0?'Latest capture':'Earlier capture'} · ${displayDate(saved.captured_at)} · ${snapshot.fields.length} fields`));
    if (saved.review_status === 'unreviewed') history.append(node('p', 'section-note', 'Preserved browser capture; its link to a submitted application has not been reviewed.'));
    if(snapshot.omitted_fields || snapshot.truncated_values) history.append(node('p','section-note',`Capture limits: ${snapshot.omitted_fields} fields omitted; ${snapshot.truncated_values} long values shortened.`));
    const priority=field=>['textarea','richtext'].includes(field.control)?0:field.control==='text'?1:2;
    for(const field of [...snapshot.fields].sort((a,b)=>priority(a)-priority(b))) {
      const answer=node('article','application-answer');
      if(field.section) answer.append(node('p','meta',field.section));
      answer.append(node('h5','',field.prompt));
      const value=typeof field.value==='boolean' ? (field.value?'Selected':'Not selected') : Array.isArray(field.value)?field.value.join('\n'):field.value;
      answer.append(node('p','answer-value',value === '' || value == null ? 'Not answered' : value));
      if(field.control==='file') answer.append(node('p','meta','File names recorded; file contents are not part of this snapshot.'));
      if(value && typeof field.value!=='boolean') {
        const copy=node('button','quiet','Copy answer'); copy.type='button';
        copy.addEventListener('click',async()=>{try {await navigator.clipboard.writeText(value);copy.textContent='Copied';} catch(_) {copy.textContent='Select the text to copy';}});
        answer.append(copy);
      }
      history.append(answer);
    }
    root.append(history);
  });
}

let applicationListEpoch = 0;
async function loadApplications() {
  if (state.applicationBackend === 'owners' && !document.querySelector('#application-phase option[value="preparing"]')) {
    const option = node('option', '', 'Tracking'); option.value = 'preparing';
    document.querySelector('#application-phase').append(option);
  }
  const epoch = ++applicationListEpoch;
  const applications = [];
  let cursor = null;
  do {
    const result = await api("/api/v1/applications" + (cursor ? "?cursor=" + encodeURIComponent(cursor) : ""));
    if (epoch !== applicationListEpoch) return;
    applications.push(...(result.applications || []));
    cursor = result.next_cursor || null;
  } while (cursor);
  state.applications = applications;
  renderApplicationTable();
  await refreshApplicationWorkspace();
}

async function loadTimeline(applicationId) { openApplication(applicationId); }
