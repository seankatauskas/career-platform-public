// Page-owned templates and rendering. Shared state and UI primitives live in app.js / console.js.
document.querySelector("#settings").innerHTML = `
  <div id="settings-home">
    <div class="section-heading"><div><h2>Settings</h2><p class="section-note">Connections, saved information, and background work.</p></div></div>
    <div class="settings-groups">
      <section class="settings-group" aria-labelledby="chief-settings-heading">
        <h3 id="chief-settings-heading">Your chief of staff</h3>
        <a class="settings-link-row" href="#settings/chief"><span><strong>Briefings, attention, and email reviews</strong><span class="meta">Choose when Hermes checks in, review what matters, and approve prepared replies.</span></span><span aria-hidden="true">→</span></a>
      </section>
      <section class="settings-group" aria-labelledby="connections-heading">
        <h3 id="connections-heading">Connections</h3>
        <p class="meta">Connect your browser to record applications as you submit them.</p>
        <div id="browser-devices" class="stack" aria-live="polite">Checking connected browsers…</div>
        <div class="actions"><button id="browser-connect-code" class="quiet" type="button">Connect a browser</button></div>
        <p id="browser-code-notice" role="status"></p><code id="browser-connection-code"></code>
        <p class="meta">Email connection health is available in <a href="#settings/operations">Operations</a>.</p>
      </section>
      <section class="settings-group" aria-labelledby="saved-information-heading">
        <h3 id="saved-information-heading">Career profile and saved resumes</h3>
        <a class="settings-link-row" href="#settings/career-profile"><span><strong>Career profile</strong><span class="meta">Your saved experience, projects, skills, and contact details.</span></span><span aria-hidden="true">→</span></a>
        <a class="settings-link-row" href="#settings/review-preferences"><span><strong>Codex review preferences</strong><span class="meta">Choose geography, career direction, and how stretches are considered.</span></span><span aria-hidden="true">→</span></a>
        <a class="settings-link-row" href="#settings/resumes"><span><strong>Saved resumes</strong><span class="meta">View and download existing resume documents.</span></span><span aria-hidden="true">→</span></a>
      </section>
      <section class="settings-group" aria-labelledby="operations-settings-heading">
        <h3 id="operations-settings-heading">Operations</h3>
        <a class="settings-link-row" href="#settings/operations"><span><strong>Connections, costs, and background work</strong><span class="meta">Check scans, provider spending, failures, and scheduled activity.</span></span><span aria-hidden="true">→</span></a>
      </section>
      <section class="settings-group" aria-labelledby="stored-records-heading">
        <h3 id="stored-records-heading">Stored records</h3>
        <a class="settings-link-row" href="#settings/stored-records"><span><strong>Earlier application drafts</strong><span class="meta">Read-only records saved before an application was submitted.</span></span><span aria-hidden="true">→</span></a>
        <details class="settings-diagnostics"><summary>Configuration details</summary><p class="meta">Read-only values from this installation.</p><dl id="settings-list" class="settings-list"></dl></details>
      </section>
    </div>
  </div>
  <div id="settings-resumes" hidden>
    <div class="section-heading"><div><p class="kicker"><a href="#settings">← Settings</a></p><h2>Saved resumes</h2></div><button id="refresh-saved-resumes" class="quiet" type="button">Refresh</button></div>
    <p class="section-note">Existing resume documents. Each application keeps its own recorded document.</p>
    <p id="saved-resumes-status" role="status"></p><div id="saved-resume-list" class="stack"></div>
  </div>
  <div id="settings-chief" hidden></div>
  <div id="settings-review-preferences" hidden>
    <div class="section-heading"><div><p class="kicker"><a href="#settings">← Settings</a></p><h2>Codex review preferences</h2></div><button id="reload-review-preferences" type="button" class="quiet">Reload saved preferences</button></div>
    <p class="section-note">Your preferences guide new reviews. Reviews already in progress keep the preferences they started with.</p>
    <p id="review-preferences-status" role="status" aria-live="polite"></p>
    <form id="review-preferences-form" class="settings-group">
      <fieldset id="review-preferences-fields" disabled>
        <legend>What to consider</legend>
        <div class="review-preference-fields">
          <label>Broad list geography<select name="broad_geography"><option value="us">United States</option><option value="worldwide">Worldwide</option></select></label>
          <label>Targeted list geography<select name="targeted_geography"><option value="us">United States</option><option value="worldwide">Worldwide</option></select></label>
          <label>Targeted career direction<select name="targeted_scope"><option value="software_building">Building software</option><option value="all_technical">All technical work</option></select></label>
          <label>Adjacent career paths<select name="adjacent_roles"><option value="broad_only">Explore in the broad list</option><option value="targeted">Consider for targeted recommendations</option><option value="exclude">Leave out of both lists</option></select></label>
          <label>Roles with eligibility questions<select name="conditional_order"><option value="technical_fit">Keep ordered by technical fit</option><option value="after_actionable">Place after actionable applications</option></select></label>
        </div>
        <label>How to consider stretches<textarea name="stretch_policy" rows="3" maxlength="1000" required></textarea></label>
        <label>Confirmed eligibility facts<textarea name="eligibility_facts" rows="3" aria-describedby="review-eligibility-help"></textarea></label>
        <p class="help" id="review-eligibility-help">Optional. One fact per line, such as confirmed work authorization. Leave uncertain information out.</p>
        <label>Other preferences<textarea name="notes" rows="3" aria-describedby="review-notes-help"></textarea></label>
        <p class="help" id="review-notes-help">Optional. One preference per line. These notes stay with your private career information.</p>
        <div class="actions"><button id="save-review-preferences" type="submit">Save review preferences</button></div>
      </fieldset>
    </form>
  </div>
  <div id="settings-stored-records" hidden>
    <div class="section-heading"><div><p class="kicker"><a href="#settings">← Settings</a></p><h2>Earlier application drafts</h2></div></div>
    <p class="section-note">These saved records are kept for reference. Opening one does not start or submit an application.</p>
    <div id="stored-records-list" class="stack"></div>
  </div>
`;

document.querySelector("#ops").innerHTML = `
  <div class="section-heading"><div><p class="kicker"><a href="#settings">← Settings</a></p><h2>Operations</h2></div><button class="quiet" id="refresh-health" type="button">Check now</button></div>
  <p class="section-note">Connection health, scheduled activity, and work that needs attention.</p>
  <p id="ops-feedback" class="ops-feedback" role="status" hidden></p>
  <section id="scan-section" aria-labelledby="scan-heading" hidden>
    <div class="ops-section-heading"><h3 id="scan-heading">Job scans and ranking</h3><button id="scan-now" class="quiet" type="button" disabled>Scan now</button></div>
    <p id="scan-status" class="meta"></p>
    <p id="pipeline-feedback" class="meta" role="status" hidden></p>
    <div id="ranking-progress" aria-live="polite"></div>
    <p class="help">Scanning checks your configured company boards for new jobs. Ranking checks saved results in the background and updates jobs that changed. You can keep applying while it runs.</p>
  </section>
  <div id="readiness-summary" class="readiness-summary" aria-live="polite">Checking recent work…</div>
  <section class="ops-recovery-section" aria-labelledby="recovery-heading"><div class="ops-section-heading"><h3 id="recovery-heading" tabindex="-1">Work that needs attention</h3></div><div id="recovery-list" class="stack empty">Checking unfinished work…</div></section>
  <section aria-labelledby="readiness-heading"><div class="ops-section-heading"><h3 id="readiness-heading">Services</h3><span id="readiness-checked" class="meta"></span></div><ul id="readiness-list" class="readiness-list"></ul></section>
  <section id="automation-section" hidden><h3>Scheduled activity</h3><div id="automation-controls" class="stack" aria-label="Background automation"></div></section>
  <section id="costs-section" aria-labelledby="costs-heading"><div class="ops-section-heading"><h3 id="costs-heading">Costs and credits</h3><span id="costs-checked" class="meta"></span></div>
    <p class="meta">AWS, Runpod, and OpenRouter · USD. Billing totals and prepaid balances use different time windows, so they are not added together.</p>
    <p id="costs-status" class="meta" role="status"></p><div id="costs-alerts" class="stack"></div><div id="costs-providers" class="costs-grid"></div>
    <p class="help">Collection runs at most once daily. Check now rereads the saved snapshot; it does not contact billing providers. Threshold warnings are informational, not spending caps.</p>
  </section>
  <section id="notification-section"><h3>Telegram delivery</h3><div id="notification-list" class="stack empty">No notification activity.</div></section>
  <details class="ops-activity"><summary>Reminders and delivery history</summary><div class="ops-grid"><div><h3>Reminders</h3><div id="reminder-list" class="stack empty">No reminders.</div></div><div><h3>Recent deliveries</h3><div id="notification-history" class="stack empty">No notification activity.</div></div></div></details>
  <details class="ops-activity"><summary>Activity counts and release details</summary><div id="health-detail" class="metrics empty">Health data is loading.</div><p id="release-detail" class="meta"></p></details>
`;

document.querySelector("#career").innerHTML = `
  <div class="section-heading"><div><p class="kicker"><a href="#settings">← Settings</a></p><h2>Career profile</h2></div><button class="quiet" id="refresh-career" type="button">Refresh profile</button></div>
  <p class="section-note">Your saved experience, projects, and skills. Open a section to update its details.</p>
  <p id="career-status" class="career-status" role="status">Loading career profile…</p>
  <form id="career-editor-form"><div id="career-editor"></div>
    <div class="career-save-bar">
      <div class="actions"><button id="save-career" type="submit">Save draft</button><a href="/api/v1/career-profile/export">Export profile</a></div>
      <div class="career-approval"><label class="review-confirm"><input id="career-reviewed" type="checkbox">I reviewed this saved profile and confirm its facts are accurate.</label><button id="approve-career" class="quiet" type="button" disabled>Approve saved facts</button></div>
    </div>
  </form>
  <details class="career-import-options"><summary>Import from a resume</summary>
    <p class="meta">Imported information becomes a draft for you to review.</p>
    <div class="career-import">
      <form id="career-import-form" class="career-upload"><label>Choose a document<input id="career-file" type="file" accept=".pdf,.docx,.tex,.txt,.md,.json" required></label><button type="submit">Import for review</button><span class="meta">PDF, DOCX, LaTeX, text, or career JSON · up to 5 MiB</span></form>
      <form id="career-standard-form" class="career-upload"><label>Use a saved resume<select id="career-standard"><option value="">Choose a resume</option></select></label><button class="quiet" type="submit">Use as starting point</button></form>
    </div>
    <details class="career-review"><summary>Paste resume text instead</summary><form id="career-paste-form" class="career-upload"><label>Resume text<textarea id="career-paste" rows="7" maxlength="256000" required placeholder="Paste the text from your existing resume here."></textarea></label><button type="submit">Import pasted text for review</button></form></details>
  </details>
  <div id="career-import-status" class="stack" role="status"></div>
  <details id="career-import-review" class="career-review" hidden><summary>Review imported source evidence</summary><div id="career-source-evidence"></div></details>
`;

const CAREER_SECTIONS = {
  education: { title: "Education", fields: [["institution", "School"], ["degree", "Degree"], ["location", "Location"], ["dates", "Dates"], ["details", "Details", true]] },
  experience: { title: "Experience", fields: [["company", "Company"], ["role", "Role"], ["location", "Location"], ["dates", "Dates"]], facts: "bullets" },
  projects: { title: "Projects", fields: [["name", "Project name"], ["context", "Technologies / context"], ["dates", "Dates"], ["url", "Project link"]], facts: "bullets" },
  skills: { title: "Skills", fields: [["category", "Category"]], facts: "items" },
};

function emptyCareerContent() {
  return { identity: { name: "", contact_line: "", email: "", linkedin: "", github: "" }, summary: "", education: [], experience: [], projects: [], skills: [] };
}

function markCareerDirty() {
  state.careerDirty = true;
  state.careerEpoch += 1;
  $("#career-reviewed").checked = false;
  $("#approve-career").disabled = true;
  $("#career-status").textContent = "Unsaved edits. Save this draft before reviewing it for approval. Your last approved facts remain saved.";
}

function careerField(target, name, labelText, multiline = false) {
  const label = node("label", multiline ? "wide" : "", labelText);
  const input = document.createElement(multiline ? "textarea" : "input");
  if (!multiline) input.type = "text";
  input.value = String(target[name] || "");
  input.maxLength = multiline ? 4000 : 1000;
  input.addEventListener("input", () => { target[name] = input.value; markCareerDirty(); });
  label.append(input);
  return label;
}

function careerSectionPanel(section, title, content, fieldset, openSections) {
  const panel = node("section", "career-summary-section");
  panel.append(node("h3", "", title));
  const summary = node("div", "career-saved-summary");
  if (section === "identity") {
    const identity = content.identity || {};
    summary.append(node("p", "", [identity.name, identity.contact_line, identity.email].filter(Boolean).join(" · ") || "No contact details saved."));
    if (identity.linkedin || identity.github) summary.append(node("p", "meta", [identity.linkedin, identity.github].filter(Boolean).join(" · ")));
    if (content.summary) summary.append(node("p", "career-background", content.summary));
  } else {
    const entries = (content[section] || []).filter((entry) => !entry.retired);
    if (!entries.length) summary.append(node("p", "meta", `No ${title.toLowerCase()} saved.`));
    entries.forEach((entry) => {
      const row = node("div", "career-summary-entry");
      row.append(node("strong", "", [entry.role, entry.company, entry.name, entry.institution, entry.category].filter(Boolean).join(" · ")));
      const detail = [entry.degree, entry.context, entry.location, entry.dates, entry.details].filter(Boolean);
      if (detail.length) row.append(node("p", "meta", detail.join(" · ")));
      const facts = (entry.bullets || entry.items || []).filter((fact) => typeof fact === "string" || !fact.retired).map((fact) => typeof fact === "string" ? fact : fact.text).filter(Boolean);
      if (facts.length) {
        if (section === "skills") row.append(node("p", "", facts.join(", ")));
        else { const list = node("ul"); facts.forEach((fact) => list.append(node("li", "", fact))); row.append(list); }
      }
      summary.append(row);
    });
  }
  const edit = node("details", "career-section-editor");
  edit.dataset.section = section;
  edit.open = openSections.has(section);
  edit.append(node("summary", "", `Edit ${title.toLowerCase()}`), fieldset);
  panel.append(summary, edit);
  return panel;
}

function renderCareerEditor() {
  const editor = $("#career-editor");
  const openSections = new Set([...editor.querySelectorAll("details[open]")].map((item) => item.dataset.section));
  editor.replaceChildren();
  const content = state.careerContent;
  if (!content) return;
  const identity = node("fieldset", "career-section");
  identity.append(node("legend", "", "Contact details"));
  const identityFields = node("div", "career-fields");
  [["name", "Full name"], ["contact_line", "Phone / location"], ["email", "Email"], ["linkedin", "LinkedIn URL"], ["github", "GitHub URL"]].forEach(([name, label]) => identityFields.append(careerField(content.identity, name, label)));
  identityFields.append(careerField(content, "summary", "Background notes", true));
  identity.append(identityFields);
  editor.append(careerSectionPanel("identity", "Contact details", content, identity, openSections));
  Object.entries(CAREER_SECTIONS).forEach(([section, definition]) => {
    const fieldset = node("fieldset", "career-section");
    fieldset.append(node("legend", "", definition.title));
    const entries = Array.isArray(content[section]) ? content[section] : [];
    content[section] = entries;
    entries.forEach((entry, index) => {
      const box = node("article", `career-entry${entry.retired ? " retired" : ""}`);
      const heading = node("div", "career-entry-header");
      heading.append(node("h3", "", `${definition.title} ${index + 1}${entry.retired ? " · retired" : ""}`));
      const retire = node("button", "quiet", entry.retired ? "Restore" : "Retire");
      retire.type = "button";
      retire.addEventListener("click", () => { entry.retired = !entry.retired; markCareerDirty(); renderCareerEditor(); });
      heading.append(retire);
      const fields = node("div", "career-fields");
      definition.fields.forEach(([name, label, multiline]) => fields.append(careerField(entry, name, label, multiline)));
      box.append(heading, fields);
      if (definition.facts) {
        const facts = node("div", "career-facts");
        entry[definition.facts] = (entry[definition.facts] || []).map((fact) => typeof fact === "string" ? { text: fact, retired: false } : fact);
        entry[definition.facts].forEach((fact, factIndex) => {
          const row = node("div", `career-fact${fact.retired ? " retired" : ""}`);
          row.append(careerField(fact, "text", `${definition.facts === "items" ? "Skill" : "Accomplishment"} ${factIndex + 1}`, true));
          const remove = node("button", "quiet", fact.retired ? "Restore" : "Retire");
          remove.type = "button";
          remove.addEventListener("click", () => { fact.retired = !fact.retired; markCareerDirty(); renderCareerEditor(); });
          row.append(remove);
          facts.append(row);
        });
        const addFact = node("button", "quiet", definition.facts === "items" ? "Add skill" : "Add accomplishment");
        addFact.type = "button";
        addFact.addEventListener("click", () => { entry[definition.facts].push({ text: "", retired: false }); markCareerDirty(); renderCareerEditor(); });
        box.append(facts, addFact);
      }
      fieldset.append(box);
    });
    const add = node("button", "quiet", `Add ${definition.title.toLowerCase()}`);
    add.type = "button";
    add.addEventListener("click", () => {
      const entry = { retired: false };
      definition.fields.forEach(([name]) => { entry[name] = ""; });
      if (definition.facts) entry[definition.facts] = [];
      entries.push(entry);
      markCareerDirty();
      renderCareerEditor();
    });
    fieldset.append(add);
    editor.append(careerSectionPanel(section, definition.title, content, fieldset, openSections));
  });
}

function renderCareerEvidence(revision) {
  const panel = $("#career-import-review");
  const rows = $("#career-source-evidence");
  rows.replaceChildren();
  const provenance = revision && revision.provenance || {};
  const review = provenance.import_review || revision && revision.import_review || {};
  const spans = review.source_spans || provenance.source_spans || [];
  const sourceText = review.source_text || provenance.source_text || "";
  const candidates = Array.isArray(spans) ? spans : [];
  candidates.forEach((span) => {
    const quote = span.quote || span.text || sourceText.slice(span.start, span.end);
    const row = node("div", "career-source-row");
    row.append(node("p", "composition-key", span.path || span.fact_id || "Imported fact"), node("p", "meta", quote || "Review this field against your original document."));
    if (span.source_pointer) row.append(node("p", "composition-key", `Source field: ${span.source_pointer}`));
    rows.append(row);
  });
  const warnings = review.warnings || provenance.warnings || [];
  if (Array.isArray(warnings)) warnings.forEach((warning) => rows.append(node("p", "meta", typeof warning === "string" ? warning : warning.message || JSON.stringify(warning))));
  if (Array.isArray(review)) review.forEach((item) => {
    const row = node("div", "career-source-row");
    row.append(node("p", "composition-key", String(item.kind || "Import note").replaceAll("_", " ")));
    if (item.text) row.append(node("p", "meta", item.text));
    if (item.existing) row.append(node("p", "meta", `Existing: ${typeof item.existing === "string" ? item.existing : JSON.stringify(item.existing)}`));
    if (item.incoming) row.append(node("p", "meta", `Imported: ${typeof item.incoming === "string" ? item.incoming : JSON.stringify(item.incoming)}`));
    if (item.resolution) row.append(node("p", "meta", item.resolution));
    rows.append(row);
  });
  panel.hidden = !rows.children.length;
}

function renderCareerProfile(profile) {
  state.careerProfile = profile;
  const revision = profile.draft || profile.approved;
  state.careerContent = JSON.parse(JSON.stringify(revision && revision.content || emptyCareerContent()));
  state.careerContent.identity = state.careerContent.identity || {};
  state.careerDirty = false;
  state.careerEpoch += 1;
  $("#career-reviewed").checked = false;
  $("#career-reviewed").disabled = !profile.draft_revision_id || profile.draft_revision_id === profile.approved_revision_id;
  $("#approve-career").disabled = true;
  $("#career-editor-form").hidden = profile.configured === false;
  $("#career-status").textContent = profile.configured === false
    ? "Career profile storage is not configured for this installation."
    : profile.draft_revision_id && profile.draft_revision_id !== profile.approved_revision_id
      ? "Draft ready for review. Check your saved information, then approve its facts."
      : profile.approved_revision_id
        ? "Your saved facts are approved. Editing a section creates a new draft."
        : "Import your resume or enter your information below to create your first draft.";
  renderCareerEditor();
  renderCareerEvidence(revision);
  const imports = (profile.imports || []).filter((item) => ["queued", "running"].includes(item.status));
  if (imports.length) pollCareerImport(imports[0].import_id);
  else if (profile.imports && profile.imports.length && profile.imports[0].status === "failed") {
    const latest = profile.imports[0];
    $("#career-import-status").replaceChildren(node("p", "meta", `Last import needs attention: ${readableResumeReason(latest.error_code || latest.error)}. Review your current draft before importing again.`));
  }
}

async function loadCareerProfile() {
  if (state.careerDirty) { notice("Save your profile edits before refreshing."); return; }
  const epoch = state.careerEpoch;
  try {
    const profile = await api("/api/v1/career-profile");
    if (state.careerEpoch === epoch && !state.careerDirty) renderCareerProfile(profile);
  } catch (error) { notice(error.message); }
}

async function saveCareerProfile(event) {
  event.preventDefault();
  const button = $("#save-career");
  button.disabled = true;
  const epoch = state.careerEpoch;
  const payload = { content: state.careerContent, expected_revision_id: state.careerProfile && state.careerProfile.draft_revision_id || null };
  try {
    // Personal information is kept in memory, never in command session storage.
    const stablePayload = JSON.parse(JSON.stringify(payload));
    const digest = await careerRequestDigest(new TextEncoder().encode(JSON.stringify(stablePayload)));
    const result = await resumeMutation(`career-save:${digest}`, "career-save", "/api/v1/career-profile", stablePayload);
    if (state.careerEpoch === epoch) renderCareerProfile(result);
    else { state.careerProfile = result; notice("Draft saved. Your newer edits remain unsaved."); }
  } catch (error) { notice(error.message); }
  finally { button.disabled = false; }
}

async function careerRequestDigest(bytes) {
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].map((value) => value.toString(16).padStart(2, "0")).join("");
}

async function approveCareerProfile() {
  if (state.careerDirty || !$("#career-reviewed").checked) return;
  const revisionId = state.careerProfile && state.careerProfile.draft_revision_id;
  if (!revisionId) return;
  const epoch = state.careerEpoch;
  $("#approve-career").disabled = true;
  try {
    const profile = await resumeMutation(`career-approve:${revisionId}`, "career-approve", "/api/v1/career-profile/approve", { revision_id: revisionId });
    if (state.careerEpoch === epoch) renderCareerProfile(profile);
    else state.careerProfile = profile;
    notice("Saved career facts approved.");

  } catch (error) { notice(error.message); $("#approve-career").disabled = false; }
}

async function pollCareerImport(importId) {
  if (state.careerImportTimer) clearTimeout(state.careerImportTimer);
  state.careerImportTimer = null;
  try {
    const imported = await api(`/api/v1/career-profile/imports/${encodeURIComponent(importId)}`);
    const box = $("#career-import-status");
    box.replaceChildren(node("p", "meta", imported.status === "succeeded"
      ? "Import finished. Review the extracted facts in your draft before approving them."
      : imported.status === "failed" ? `Import needs attention: ${readableResumeReason(imported.error_code)}. Review your current draft before importing again.`
        : "Reading your resume and preparing a draft…"));
    if (["queued", "running"].includes(imported.status)) {
      state.careerImportTimer = setTimeout(() => pollCareerImport(importId), 2000);
    } else if (imported.status === "succeeded") {
      if (!state.careerDirty) await loadCareerProfile();
      else notice("Import finished. Save your current edits before refreshing the imported draft.");
    }
  } catch (error) { notice(error.message); }
}

async function importCareerDocument(event) {
  event.preventDefault();
  await uploadCareerDocument($("#career-file").files[0], event.currentTarget.querySelector("button"));
}

async function importCareerText(event) {
  event.preventDefault();
  const text = $("#career-paste").value;
  if (!text.trim()) { notice("Paste your resume text first."); return; }
  await uploadCareerDocument(new File([text], "pasted-resume.txt", { type: "text/plain" }), event.currentTarget.querySelector("button"));
}

async function uploadCareerDocument(file, button) {
  if (state.careerDirty) { notice("Save your profile edits before importing another document."); return; }
  if (!file) return;
  if (!file.size || file.size > 5 * 1024 * 1024) { notice("Choose a document between 1 byte and 5 MiB."); return; }
  const suffix = file.name.split(".").pop().toLowerCase();
  const types = { pdf: "application/pdf", docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document", tex: "text/plain", txt: "text/plain", md: "text/plain", json: "application/json" };
  if (!types[suffix]) { notice("Choose a PDF, DOCX, LaTeX, text, or career JSON file."); return; }
  button.disabled = true;
  let commandId = "";
  try {
    const digest = await careerRequestDigest(await file.arrayBuffer());
    const requestDigest = await careerRequestDigest(new TextEncoder().encode(JSON.stringify({ digest, filename: file.name, type: types[suffix] })));
    commandId = `career-import:${requestDigest}`;
    const envelope = resumeCommandEnvelope(commandId, "career-import");
    const response = await fetch("/api/v1/career-profile/imports", { method: "POST", credentials: "same-origin", headers: {
      "Content-Type": types[suffix], "X-CSRF-Token": state.csrf, "X-File-Name": encodeURIComponent(file.name), "Idempotency-Key": envelope.idempotency_key,
    }, body: file });
    const result = await response.json();
    if (response.ok || response.status >= 400 && response.status < 500) clearResumeCommandEnvelope(commandId);
    if (!response.ok) throw new Error(result.error || `Import failed (${response.status})`);
    await pollCareerImport(result.import_id);
  } catch (error) { notice(error.message); }
  finally { button.disabled = false; }
}

async function importCareerStandard(event) {
  event.preventDefault();
  if (state.careerDirty) { notice("Save your profile edits before importing a standard."); return; }
  const versionId = $("#career-standard").value;
  if (!versionId) { notice("Choose an imported resume first."); return; }
  const button = event.currentTarget.querySelector("button");
  button.disabled = true;
  try {
    await resumeMutation(`career-standard:${versionId}`, "career-standard", "/api/v1/career-profile/import-standard", { standard_version_id: versionId });
    await loadCareerProfile();
  } catch (error) { notice(error.message); }
  finally { button.disabled = false; }
}

const OPS_STATUS = {
  ready: ["Working", "ready"],
  configured_unverified: ["Awaiting verification", "pending"],
  disabled: ["Not enabled", "muted"],
  paused: ["Paused", "pending"],
  stale: ["Overdue", "attention"],
  blocked: ["Needs attention", "attention"],
};
const OPS_NAMES = {
  job_collection: "Job collection", company_discovery: "Company discovery", email_sync: "Email sync",
  inference_usage: "Model usage limits", dependency_snapshot: "Worker dependency checks",
  database: "Application records", automation: "Automation", "ats.ingestion": "Job collection",
  "ats.discovery": "Company discovery", outlook: "Outlook", notifications: "Telegram delivery",
  ranking: "Scheduled ranking", shortlist: "Scheduled shortlist checks", work_queue: "Scheduled work",
  application_outbox: "Application updates", notification_outbox: "Notification queue",
  inference: "Model service", resume: "Resume generation", notification_transport: "Telegram connection",
  ranking_model: "Ranking model", mail_processing: "Recruiter email processing",
  "system.worker_tick": "Automation cycle", "ats.authoritative": "Full job collection",
  "ats.new_only": "New job collection", "ats.refresh_recent": "Recent company discovery",
  "opportunity.location_refresh": "Job locations", "opportunity.preference_refresh": "Job ranking",
  "opportunity.salary_drain": "Salary estimates", "notification.shortlist_evaluate": "Shortlist notifications",
  "notification.reminders_due": "Scheduled reminders", "outlook.mail.sync": "Outlook email sync",
};
const OPS_REASONS = {
  inference_usage_within_limits: "Model requests are within the configured usage limits.",
  inference_limits_not_configured: "Platform request and token limits have not been set.",
  inference_reconciliation_required: "A model request has an uncertain result. Review it before retrying.",
  inference_daily_request_limit: "The daily request allowance is reserved. New requests wait until the next UTC day.",
  inference_daily_token_limit: "The daily token allowance is reserved. New requests wait until the next UTC day.",
  inference_inflight_limit: "The active request slots are occupied. New requests wait for capacity.",
  awaiting_first_scheduled_run: "Waiting for its first scheduled run.",
  scheduled_success_overdue: "A successful run is overdue.",
  latest_work_failed: "The latest attempt did not finish successfully.",
  external_reconciliation_required: "An external action may already have happened. Check its outcome before continuing.",
  reauth_required: "Outlook needs you to sign in again.",
  connector_failed: "The last connection attempt failed.",
  migration_required: "The ranking model needs review before it can use the configured provider.",
  dependency_not_ready: "A required connection or tool is not ready.",
  automation_paused: "Scheduled work is paused.",
  awaiting_core_observation: "Waiting for the core worker to check its dependencies.",
  dependency_snapshot_expired: "The core worker’s dependency check is more than 15 minutes old.",
  dependency_snapshot_release_changed: "The core worker has not reported dependencies for this release yet.",
  schema_upgrade_required: "The running release needs a database upgrade.",
  use_domain_recovery: "Use this feature's review flow to recover the work.",
  inspect_failure: "Review the failure before deciding what to do next.",
  failure_not_retryable: "This failure needs review before another attempt.",
  superseded_by_success: "A later attempt already succeeded.",
  schedule_disabled: "This schedule is not enabled.",
};
const OPS_ACTIONS = {
  inspect_inference_recovery: "Inspect the provider result using the inference-recovery operator command.",
  review_usage_limits: "Set optional inference_usage_limits in the runtime configuration.",
  none: "", inspect_workflow: "Review the work that needs attention.",
  inspect_failed_work: "Review the work that needs attention.",
  inspect_reconciliation: "Check the external result, then use the relevant review controls.",
  reconnect_outlook: "Reconnect Outlook through the existing account setup.",
  inspect_connector: "Check the connection details in your runtime setup.",
  review_model_readiness: "Review the model's migration and activation status.",
  review_configuration: "Complete the missing runtime configuration.",
  review_activation: "Review activation before resuming scheduled work.",
  inspect_core_worker: "Check the core worker’s progress and restart status.",
  upgrade_release: "Install the compatible release before resuming work.",
};

function costMoney(value) {
  const amount = Number(value);
  return value == null || !Number.isFinite(amount) ? "Unavailable"
    : new Intl.NumberFormat("en-US", {style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 4}).format(amount);
}

function renderCosts(report) {
  const providers = $("#costs-providers"), alerts = $("#costs-alerts");
  providers.replaceChildren(); alerts.replaceChildren();
  $("#costs-checked").textContent = report && report.generated_at ? `Snapshot ${opsTime(report.generated_at)}` : "";
  $("#costs-status").textContent = report && report.available
    ? `Next collection due ${opsTime(report.next_refresh_due_at)}. Provider reporting can lag behind collection.`
    : report && report.message || "Cost collection is not configured for this installation.";
  if (!report || !report.available) return;
  const names = {aws: "AWS", runpod: "Runpod", openrouter: "OpenRouter"};
  const states = {ok: "Collected", partial: "Partial coverage", error: "Update failed", not_configured: "Not configured", no_data: "Not yet reported"};
  const notes = {
    aws: "Account-wide UnblendedCost, including other services in this AWS account. Credits and refunds are signed adjustments, not remaining promotional credit. Net cost is an estimate, not a final invoice. AWS data may lag more than 24 hours.",
    runpod: "Account-wide usage and prepaid balance, including resources outside this application. The current hourly rate is not a forecast or monthly spending cap.",
    openrouter: "Account totals cover all keys. Metrics labeled “This key” cover only the configured inference key. Credit balance is not the key’s remaining spending allowance; BYOK provider charges are not included here.",
  };
  (report.providers || []).forEach((provider) => {
    const card = node("article", "costs-card"); card.dataset.provider = provider.id;
    const heading = node("div", "ops-section-heading");
    heading.append(node("h4", "", names[provider.id] || provider.id), node("span", "ops-badge", provider.stale ? "Stale figures" : states[provider.status] || "Unavailable"));
    card.append(heading);
    if (provider.message) card.append(node("p", "meta", provider.message));
    if (provider.status === "error" && provider.observed_at) card.append(node("p", "meta", "Showing last successful figures, not current totals."));
    if (provider.period) card.append(node("p", "meta", `${provider.period.start} to ${provider.period.end} (UTC, end exclusive; today's partial day is not included).${provider.estimated ? " Provider marks this period estimated." : ""}`));
    const metrics = node("dl", "costs-metrics");
    Object.entries(provider.metrics || {}).forEach(([metric, value]) => {
      metrics.append(node("dt", "meta", provider.metric_labels && provider.metric_labels[metric] || metric), node("dd", "", costMoney(value)));
    });
    if (metrics.children.length) card.append(metrics);
    else card.append(node("p", "meta", "No verified figures available. This does not mean zero spending."));
    if (provider.observed_at) card.append(node("p", "meta", `Last successful collection ${opsTime(provider.observed_at)}.`));
    if (provider.attempted_at && provider.attempted_at !== provider.observed_at) card.append(node("p", "meta", `Last attempt ${opsTime(provider.attempted_at)}.`));
    card.append(node("p", "help", notes[provider.id] || ""));
    providers.append(card);
  });
  (report.alerts || []).forEach((alert) => {
    let text = `${names[alert.provider] || alert.provider}: `;
    if (alert.kind === "threshold") text += `${alert.direction === "below" ? "Balance is at or below" : "Reported charges are at or above"} your ${costMoney(alert.threshold)} warning threshold.${alert.stale ? " Based on last successful figures; refresh is overdue or failed." : ""}`;
    else text += alert.kind === "stale" ? "Cost figures are over 36 hours old." : "The latest billing update failed.";
    alerts.append(node("p", "costs-warning", text));
  });
}

function opsFeedback(message) {
  const target = $("#ops-feedback");
  target.textContent = message;
  target.hidden = !message;
}

function opsName(value) { return OPS_NAMES[value] || String(value || "Work").replaceAll(/[_.]/g, " "); }

function opsTime(value) {
  if (!value) return "Not yet recorded";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Not available" : date.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

function opsBadge(status) {
  const [label, tone] = OPS_STATUS[status] || ["Status unavailable", "pending"];
  return node("span", `ops-badge ${tone}`, label);
}

function opsAttempts(value, maximum) {
  const count = Number(value || 0);
  return maximum ? `${count}/${maximum} attempts` : `${count} ${count === 1 ? "attempt" : "attempts"}`;
}

function renderReadiness(report) {
  const summary = $("#readiness-summary");
  const list = $("#readiness-list");
  summary.replaceChildren();
  list.replaceChildren();
  if (!report || !Array.isArray(report.capabilities)) {
    summary.append(node("h3", "", "Readiness is not available"), node("p", "meta", "Activity counts are available below. Check again after the running release supports readiness reports."));
    renderHeaderNotification("System status is unavailable.", true);
    $("#readiness-checked").textContent = "";
    $("#release-detail").textContent = "Release identity is not available.";
    return;
  }
  const count = report.capabilities.filter((item) => ["blocked", "stale"].includes(item.status)).length;
  const awaiting = report.capabilities.filter((item) => item.status === "configured_unverified").length;
  const attention = count > 0 || ["blocked", "stale"].includes(report.status);
  const title = attention ? "Your search needs attention" : report.status === "ready" ? "Recent work is healthy" : report.status === "paused" ? "Some work is paused" : "Your system is waiting to get started";
  summary.className = `readiness-summary${attention ? " attention" : ""}`;
  summary.append(node("h3", "", title));
  const description = count
    ? `${count} ${count === 1 ? "part needs" : "parts need"} attention. Check the last successful run and next step below.`
    : awaiting ? `${awaiting} ${awaiting === 1 ? "connection is" : "connections are"} configured and waiting for a successful operation.`
    : "Readiness reflects recorded work and the current configuration.";
  summary.append(node("p", "", description));
  if (report.external_services_verified !== true) {
    summary.append(node("p", "meta", "This check reads local evidence. It does not contact Outlook, Telegram, or model providers."));
  }
  renderHeaderNotification(attention ? "Your search needs attention." : "No issues need attention.", attention);
  $("#readiness-checked").textContent = `Checked ${opsTime(report.checked_at)}`;
  const passive = node("li", "ops-passive-group");
  const passiveDetails = node("details");
  const passiveRows = node("ul", "readiness-list");
  const passiveCount = report.capabilities.filter((item) => !["blocked", "stale"].includes(item.status)).length;
  passiveDetails.append(node("summary", "", `Other services (${passiveCount})`), passiveRows);
  passive.append(passiveDetails);
  report.capabilities.forEach((item) => {
    const row = node("li", "readiness-row");
    const identity = node("div", "readiness-identity");
    identity.append(node("h4", "", opsName(item.id)), opsBadge(item.status));
    const evidence = node("dl", "readiness-evidence");
    const success = node("div");
    success.append(node("dt", "", "Last success"), node("dd", "", opsTime(item.last_success_at)));
    const attempt = node("div");
    attempt.append(node("dt", "", "Last attempt"), node("dd", "", opsTime(item.last_attempt_at)));
    if (item.id === "inference_usage" && report.inference_usage) {
      const usage = report.inference_usage;
      for (const [label, used, limit] of [
        ["Requests reserved today", usage.reserved_requests, usage.limits?.daily_requests],
        ["Allowance units reserved today", usage.reserved_tokens, usage.limits?.daily_tokens],
        ["Active requests", usage.inflight, usage.limits?.max_inflight],
      ]) {
        const entry = node("div");
        const value = `${Number(used || 0).toLocaleString()} / ${limit == null ? "no limit" : Number(limit).toLocaleString()}`;
        entry.append(node("dt", "", label), node("dd", "", value));
        evidence.append(entry);
      }
      evidence.append(node("p", "help", "Allowance units are conservative request estimates, not measured model tokens or provider charges."));
    } else {
      evidence.append(success, attempt);
    }
    const next = node("div", "readiness-next");
    const reason = OPS_REASONS[item.reason_code] || (item.status === "ready" ? "No action needed." : item.status === "disabled" ? "This capability is not enabled." : item.status === "paused" ? "Work is paused until activation is reviewed." : item.status === "configured_unverified" ? "Configuration is present; a successful operation has not been verified." : "Review the current state before continuing.");
    next.append(node("p", "", reason));
    if (OPS_ACTIONS[item.next_action]) next.append(node("p", "meta", OPS_ACTIONS[item.next_action]));
    const details = node("details", "ops-row-details");
    details.append(node("summary", "", "Technical details"), node("p", "meta", [item.id, item.reason_code, `Configured: ${item.configured ? "yes" : "no"}`, `Enabled: ${item.enabled ? "yes" : "no"}`].filter(Boolean).join(" · ")));
    next.append(details);
    const recent = node("p", "meta", item.last_success_at ? `Last success ${opsTime(item.last_success_at)}` : "No successful run recorded");
    const expanded = node("details", "ops-service-details");
    expanded.append(node("summary", "", "Activity details"), evidence, next);
    row.append(identity, recent);
    if (["blocked", "stale"].includes(item.status)) {
      row.append(node("p", "ops-next-action", reason));
      if (OPS_ACTIONS[item.next_action]) row.append(node("p", "meta", OPS_ACTIONS[item.next_action]));
      list.append(row);
    } else passiveRows.append(row);
    row.append(expanded);
  });
  if (passiveCount) list.append(passive);
  const release = report.release || {};
  $("#release-detail").textContent = `Release ${release.source_sha ? String(release.source_sha).slice(0, 12) : "not recorded"} · Schema ${release.schema_version ?? "not recorded"} · Build source ${release.identity_verified ? "recorded" : "not recorded"}`;
}

const opsCommandKeys = new Map();
function opsCommandKey(command) {
  if (!opsCommandKeys.has(command)) {
    let existing;
    try { existing = sessionStorage.getItem(`job-search:ops:${command}`); } catch (_error) { /* Memory fallback. */ }
    const value = existing || key("ops");
    opsCommandKeys.set(command, value);
    try { sessionStorage.setItem(`job-search:ops:${command}`, value); } catch (_error) { /* Memory fallback. */ }
  }
  return opsCommandKeys.get(command);
}

async function retryOpsWork(item, button) {
  button.disabled = true;
  opsFeedback("");
  try {
    await api(`/api/v1/ops/work/${encodeURIComponent(item.work_id)}/retry`, {
      method: "POST", body: JSON.stringify({ expected_revision: item.revision, idempotency_key: opsCommandKey(`retry:${item.work_id}:${item.revision}`) }),
    });
    const refreshed = await loadHealth();
    opsFeedback(refreshed ? "Work queued for another attempt. The worker will pick it up when it is ready." : "Work was queued, but system status could not be refreshed. Use Check now to see its progress.");
    $("#recovery-heading").focus({ preventScroll: true });
  } catch (error) {
    if (error.status === 409) {
      await loadHealth();
      opsFeedback("This work changed since the page loaded. Its current state is shown below.");
    } else {
      button.disabled = false;
      opsFeedback(`Work was not confirmed as queued. ${error.message} You can retry this same request.`);
    }
  }
}

function renderRecovery(recovery) {
  const list = $("#recovery-list");
  clear(list);
  const items = recovery && Array.isArray(recovery.items) ? recovery.items : [];
  if (!items.length) {
    list.classList.add("empty");
    list.textContent = recovery ? "No unfinished work needs recovery." : "Recovery information is not available in this release.";
    return;
  }
  items.forEach((item) => {
    const row = node("article", "stack-item ops-recovery-row");
    const detail = node("div");
    detail.append(node("h4", "", opsName(item.task_kind)), meta([String(item.status || "unknown").replaceAll("_", " "), opsAttempts(item.attempts)]));
    const uncertain = ["unknown", "uncertain", "needs_reconciliation"].includes(item.external_outcome) || item.reason_code === "external_reconciliation_required";
    detail.append(node("p", "meta", uncertain ? "The external result is uncertain. Review it before another attempt." : OPS_REASONS[item.reason_code] || "A previous attempt did not finish. Review its state before continuing."));
    const diagnostic = node("details", "ops-row-details");
    diagnostic.append(node("summary", "", "Technical details"), node("p", "meta", [item.work_id, item.failure_kind, item.reason_code, `Revision ${item.revision}`].filter(Boolean).join(" · ")));
    detail.append(diagnostic);
    const actions = node("div", "actions");
    if (item.retry_allowed === true && ["none", "terminal"].includes(item.external_outcome) && !uncertain) {
      const retry = node("button", "quiet", "Retry work");
      retry.type = "button";
      retry.addEventListener("click", () => retryOpsWork(item, retry));
      actions.append(retry);
    } else {
      actions.append(node("span", "ops-review-label", uncertain ? "Review outcome first" : "Review required"));
    }
    row.append(detail, actions);
    list.append(row);
  });
}

async function reconcileOpsNotification(item, outcome, buttons, check) {
  buttons.forEach((button) => { button.disabled = true; });
  check.disabled = true;
  opsFeedback("");
  try {
    await api(`/api/v1/ops/notifications/${encodeURIComponent(item.notification_id)}/reconcile`, {
      method: "POST", body: JSON.stringify({ expected_attempts: item.expected_attempts, expected_payload_sha256: item.expected_payload_sha256, outcome, idempotency_key: opsCommandKey(`notification:${item.notification_id}:${item.expected_attempts}:${outcome}`) }),
    });
    const refreshed = await loadHealth();
    const result = outcome === "not_delivered" ? "The same notification is queued for delivery." : outcome === "delivered" ? "Notification marked as delivered." : "Notification closed without another delivery attempt.";
    opsFeedback(result + (refreshed ? "" : " Status could not be refreshed; use Check now."));
  } catch (error) {
    if (error.status === 409) {
      await loadHealth();
      opsFeedback("This notification changed since the page loaded. Review its current state before continuing.");
    } else {
      opsFeedback(`The notification review was not confirmed. ${error.message} Use Check now before making another decision.`);
    }
  }
}

function notificationReview(item) {
  const review = node("div", "ops-notification-review");
  review.append(node("p", "", "Delivery may already have happened. Check Telegram for this notification before deciding what to do."));
  const label = node("label", "review-confirm");
  const check = node("input");
  check.type = "checkbox";
  label.append(check, node("span", "", "I checked Telegram for this notification."));
  const actions = node("div", "actions");
  const buttons = [["delivered", "It was delivered"], ["not_delivered", "It did not arrive · queue again"], ["abandoned", "Close without resending"]].map(([outcome, text]) => {
    const button = node("button", "quiet", text);
    button.type = "button";
    button.disabled = true;
    button.addEventListener("click", () => reconcileOpsNotification(item, outcome, buttons, check));
    return button;
  });
  check.addEventListener("change", () => { buttons.forEach((button) => { button.disabled = !check.checked; }); });
  actions.append(...buttons);
  review.append(label, actions);
  return review;
}

let opsLoadEpoch = 0;
let scanCommand = null;
let scanPending = false;
let pipelineEpoch = 0;

function renderPipeline(collection, ranking) {
  pipelineEpoch += 1;
  $("#pipeline-feedback").hidden = true;
  $("#scan-section").hidden = !collection;
  if (!collection) return;
  const button = $("#scan-now");
  button.disabled = scanPending || !collection.available || Boolean(collection.active);
  button.textContent = collection.active ? (collection.active.status === "running" ? "Scanning…" : "Scan queued") : "Scan now";
  $("#scan-status").textContent = [collection.reason,
    collection.last_scan_at ? `Last completed scan ${opsTime(collection.last_scan_at)}` : "No completed scan yet",
    collection.next_scan_at ? `Next scheduled scan ${opsTime(collection.next_scan_at)}` : "No scheduled scan"].filter(Boolean).join(" · ");
  const target = $("#ranking-progress");
  clear(target);
  if (!ranking) return;
  const labels = {running: "Checking for ranking updates", queued: "Ranking update check queued", idle: "Ranking is idle", paused: "Ranking is paused",
    dead: "Ranking needs attention", waiting_allowance: "Ranking is waiting for the model allowance", waiting_provider: "Ranking is waiting for the model service"};
  target.append(node("p", "", labels[ranking.state] || "Checking ranking"));
  if (ranking.retry_at && ["waiting_allowance", "waiting_provider"].includes(ranking.state)) target.append(node("p", "meta", `Next attempt ${opsTime(ranking.retry_at)}. Completed work is saved.`));
  if (!ranking.available) { target.append(node("p", "meta", ranking.reason || "Coverage is not available yet.")); return; }
  const number = (value) => Number(value || 0).toLocaleString();
  target.append(node("p", "meta", `${number(ranking.postings)} collected postings · ${number(ranking.total_families)} distinct job families`));
  for (const [policy, counts] of Object.entries(ranking.policies || {})) {
    target.append(node("p", "meta", `${policy === "selective" ? "Selective" : "Broad"}: ${number(counts.ranked_families)} families with saved rankings · ${number(counts.unranked_families)} without rankings`));
  }
  const pass = ranking.state === "running" ? ranking.current_pass : ranking.state === "idle" ? ranking.last_pass : null;
  if (pass?.reused && pass.status === "succeeded") {
    target.append(node("p", "meta", ranking.state === "idle"
      ? "Last check: saved rankings were current. No jobs needed recomputing."
      : "Saved rankings are already current. No jobs needed recomputing."));
  } else if (ranking.state === "running" && pass?.status !== "succeeded") {
    const checked = pass?.checked_families, total = pass?.total_families;
    if (Number.isInteger(checked) && checked >= 0 && Number.isInteger(total) && total > 0 && checked <= total) {
      const progress = node("progress", "");
      progress.max = total;
      progress.value = checked;
      progress.setAttribute("aria-label", "Job families checked for ranking updates");
      target.append(progress, node("p", "meta", `Checked ${number(checked)} of ${number(total)} job families for changes`));
    } else {
      target.append(node("p", "meta", "Preparing to check saved rankings. Progress for this attempt is not available yet."));
    }
  } else if (pass?.status === "succeeded") {
    target.append(node("p", "meta", "Ranking update check completed."));
  }
  for (const [policy, counts] of Object.entries(pass?.policies || {})) {
    if (!Number.isInteger(counts.recomputed_families) || counts.recomputed_families < 0) continue;
    const reused = Number.isInteger(counts.reused_families) && counts.reused_families >= 0
      ? ` · ${number(counts.reused_families)} saved rankings reused` : "";
    target.append(node("p", "meta", `${policy === "selective" ? "Selective" : "Broad"} ${ranking.state === "idle" ? "last" : "this"} check: ${number(counts.recomputed_families)} rankings recomputed${reused}`));
  }
  target.append(node("p", "help", "Checking saved rankings does not mean every job is ranked again. New or changed jobs need updated rankings. Jobs without rankings are saved but do not appear in automatic model picks."));
  if (Object.values(ranking.policies || {}).some((p) => p.freshness !== "ready")) {
    target.append(node("p", "meta", "Saved rankings may still need checking against the latest collection."));
  }
}

async function refreshPipeline() {
  const epoch = ++pipelineEpoch;
  try {
    const result = await api("/api/v1/ops/pipeline");
    if (epoch === pipelineEpoch && !scanPending) renderPipeline(result.collection, result.ranking);
  } catch (_error) {
    if (epoch !== pipelineEpoch) return;
    $("#pipeline-feedback").textContent = "Progress could not refresh. The counts shown may be out of date; use Check now to retry.";
    $("#pipeline-feedback").hidden = false;
  }
}

async function scanNow() {
  if (scanPending) return;
  scanPending = true;
  pipelineEpoch += 1;
  $("#scan-now").disabled = true;
  scanCommand ||= opsCommandKey("scan");
  try {
    const result = await api("/api/v1/ops/scan", {method: "POST", body: JSON.stringify({idempotency_key: scanCommand})});
    scanCommand = null;
    opsCommandKeys.delete("scan");
    try { sessionStorage.removeItem("job-search:ops:scan"); } catch (_error) { /* Memory fallback. */ }
    scanPending = false;
    await loadHealth();
    opsFeedback(result.coalesced ? "A scan is already queued or running. Following that scan." : "Scan queued. The background worker will collect new jobs, then rank them within the model allowance.");
  } catch (error) {
    opsFeedback(`The scan request was not confirmed. ${error.message} Retry to check the same request.`);
  } finally {
    scanPending = false;
    // Only offer a retry when the request's result is uncertain.
    if (scanCommand) $("#scan-now").disabled = false;
  }
}

async function loadHealth() {
  const epoch = ++opsLoadEpoch;
  const refresh = $("#refresh-health");
  opsFeedback("");
  refresh.disabled = true;
  $("#ops").setAttribute("aria-busy", "true");
  try {
  const ops = await api("/api/v1/ops");
  if (epoch !== opsLoadEpoch) return false;
  const health = ops.health;
  renderPipeline(ops.collection, ops.ranking);
  renderReadiness(ops.readiness);
  renderCosts(ops.costs);
  const controls = $("#automation-controls");
  clear(controls);
  $("#automation-section").hidden = !(ops.automation || []).length;
  (ops.automation || []).forEach((control) => {
    const row = node("article", "stack-item");
    const label = node("div");
    label.append(node("h3", "", opsName(control.capability)));
    label.append(node("span", "meta", control.enabled ? "Enabled" : "Paused"));
    const toggle = node("button", "quiet", control.enabled ? "Pause" : "Enable");
    toggle.type = "button";
    const decisionKey = key("automation");
    toggle.addEventListener("click", async () => {
      toggle.disabled = true;
      try {
        await api("/api/v1/automation", {method: "POST", body: JSON.stringify({capability: control.capability,
          enabled: !control.enabled, expected_revision: control.revision, idempotency_key: decisionKey})});
        await loadHealth();
      } catch (error) { notice(error.message); toggle.disabled = false; }
    });
    row.append(label, toggle); controls.append(row);
  });
  renderRecovery(ops.recovery);
  const detail = $("#health-detail");
  clear(detail);
  const metrics = [
    ["Applications", Object.values(health.applications).reduce((a, b) => a + b, 0)],
    ["Pending reviews", health.pending_reviews],
    ["Pending outbox", health.outbox.counts.pending || 0],
    ["Pending notifications", ops.notifications.counts.pending || 0],
    ["Scheduled reminders", ops.reminders.counts.scheduled || 0],
    ["Projection errors", health.projection_failures.length],
    ["Scheduled jobs", health.work.schedules.length],
  ];
  metrics.forEach(([label, value]) => {
    const metric = node("div", "metric");
    metric.append(node("span", "meta", label), node("strong", "", value));
    detail.append(metric);
  });

  const reminders = $("#reminder-list");
  clear(reminders);
  if (!ops.reminders.items.length) {
    reminders.classList.add("empty");
    reminders.textContent = "No reminders.";
  } else {
    ops.reminders.items.forEach((reminder) => {
      const row = node("article", "stack-item");
      const description = node("div");
      description.append(node("h3", "", reminder.note));
      description.append(meta([reminder.status, reminder.due_at]));
      const actions = node("div", "actions");
      if (reminder.status === "scheduled") {
        const cancel = node("button", "danger", "Cancel");
        cancel.type = "button";
        cancel.addEventListener("click", async () => {
          cancel.disabled = true;
          try {
            await api(`/api/v1/reminders/${reminder.reminder_id}/cancel`, {
              method: "POST",
              body: JSON.stringify({ idempotency_key: key("reminder-cancel") }),
            });
            await loadHealth();
          } catch (error) {
            cancel.disabled = false;
            notice(error.message);
          }
        });
        actions.append(cancel);
      }
      row.append(description, actions);
      reminders.append(row);
    });
  }

  const notifications = $("#notification-list");
  clear(notifications);
  const notificationHistory = $("#notification-history");
  clear(notificationHistory);
  const pendingNotifications = ops.notifications.reconciliation || [];
  const notificationItems = [...ops.notifications.items];
  pendingNotifications.forEach((item) => {
    if (!notificationItems.some((existing) => existing.notification_id === item.notification_id)) notificationItems.unshift(item);
  });
  if (!notificationItems.length) {
    notifications.classList.add("empty");
    notifications.textContent = "No notification activity.";
  } else {
    notificationItems.forEach((notification) => {
      const row = node("article", "stack-item");
      const description = node("div");
      description.append(node("h3", "", notification.topic));
      description.append(meta([
        notification.status === "needs_reconciliation" ? "Delivery uncertain" : String(notification.status || "unknown").replaceAll("_", " "),
        opsAttempts(notification.attempts, notification.max_attempts),
        notification.available_at,
      ]));
      const pending = pendingNotifications.find((item) => item.notification_id === notification.notification_id);
      if (pending) description.append(notificationReview(pending));
      row.append(description);
      (pending || ["failed", "needs_reconciliation"].includes(notification.status) ? notifications : notificationHistory).append(row);
    });
  }
  if (!notificationHistory.children.length) notificationHistory.append(node("p", "meta", "No recent deliveries."));
  $("#notification-section").hidden = !notifications.querySelector("article");
  const recoveryNeedsAttention = Boolean(ops.recovery?.items?.length);
  const deliveryNeedsAttention = pendingNotifications.length > 0 || notificationItems.some((item) => ["failed", "needs_reconciliation"].includes(item.status));
  if (recoveryNeedsAttention || deliveryNeedsAttention) {
    renderHeaderNotification(deliveryNeedsAttention ? "Notification delivery needs attention." : "Background work needs attention.", true);
  }
  return true;
  } catch (error) {
    if (epoch !== opsLoadEpoch) return false;
    renderHeaderNotification("System status could not be checked.", true);
    opsFeedback(`Could not refresh system status. ${error.message} The last displayed results may be out of date.`);
    return false;
  } finally {
    if (epoch === opsLoadEpoch) {
      refresh.disabled = false;
      $("#ops").removeAttribute("aria-busy");
    }
  }
}

const SETTINGS_LABELS = {
  timezone: "Time zone", demo_mode: "Demo installation", resume_mode: "Document mode",
  host: "Local address", port: "Dashboard port", model_provider: "Model provider",
  telegram_configured: "Telegram configured", outlook_configured: "Outlook configured",
};
let settingsLoadEpoch = 0;
async function loadSettings() {
  const epoch = ++settingsLoadEpoch;
  const results = await Promise.allSettled([api("/api/v1/settings"), api("/api/v1/browser/devices")]);
  if (epoch !== settingsLoadEpoch) return;
  const [settingsResult, devicesResult] = results;
  const target = $("#browser-devices");
  if (devicesResult.status === "fulfilled") {
    const devices = (devicesResult.value.devices || []).filter((device) => !device.revoked_at);
    target.replaceChildren();
    $("#browser-connect-code").textContent = devices.length ? "Connect another browser" : "Connect a browser";
    if (!devices.length) target.append(node("p", "meta", "No browser connected yet."));
    devices.forEach((device) => {
      const row = node("div", "settings-device");
      const description = node("div");
      description.append(node("strong", "", "Browser connected"), node("p", "meta", `Connected ${opsTime(device.created_at)}`));
      if (device.last_seen_at) description.append(node("p", "meta", `Last active ${opsTime(device.last_seen_at)}`));
      const controls = node("details", "settings-device-controls");
      controls.append(node("summary", "", "Manage connection"));
      const revoke = node("button", "quiet", "Disconnect browser");
      revoke.type = "button";
      revoke.addEventListener("click", async () => {
        revoke.disabled = true;
        try {
          await api("/api/v1/browser/revoke", {method:"POST", body:JSON.stringify({device_id:device.device_id, idempotency_key:crypto.randomUUID()})});
          await loadSettings();
        } catch (error) { $("#browser-code-notice").textContent = error.message; revoke.disabled = false; }
      });
      controls.append(revoke); row.append(description, controls); target.append(row);
    });
  } else target.replaceChildren(node("p", "meta", "Could not check connected browsers. Refresh Settings to try again."));
  if (settingsResult.status === "rejected") {
    $("#settings-list").replaceChildren(node("p", "meta", "Configuration details could not be loaded."));
    return;
  }
  const settings = settingsResult.value;
  $("#demo-badge").hidden = !settings.demo_mode;
  state.resumeMode = settings.resume_mode || "tailored";
  const list = $("#settings-list"); list.replaceChildren();
  Object.entries(settings).forEach(([name, value]) => {
    const item = node("div");
    const friendly = SETTINGS_LABELS[name] || name.replaceAll("_", " ").replace(/^./, (letter) => letter.toUpperCase());
    item.append(node("dt", "", friendly), node("dd", "", typeof value === "boolean" ? (value ? "Yes" : "No") : typeof value === "object" ? JSON.stringify(value) : value));
    list.append(item);
  });
}

function renderSettingsSubview(subpage = "") {
  subpage = subpage.split('?')[0];
  const view = ["resumes", "stored-records", "chief", "review-preferences"].includes(subpage) ? subpage : "home";
  ["home", "resumes", "stored-records", "chief", "review-preferences"].forEach((name) => { $(`#settings-${name}`).hidden = name !== view; });
  if (view === "stored-records") renderStoredRecords();
}

function renderStoredRecords() {
  const list = $("#stored-records-list"); list.replaceChildren();
  const drafts = (state.applications || []).filter((application) => application.current_phase === "preparing");
  if (!drafts.length) { list.append(node("p", "empty", "No earlier application drafts.")); return; }
  drafts.forEach((application) => {
    const row = node("article", "settings-stored-record");
    const link = node("a", "", application.title_snapshot || application.role_title || application.title || "Application record");
    link.href = `#applications/${encodeURIComponent(application.application_id)}/overview`;
    row.append(link, node("p", "meta", [application.employer_snapshot || application.employer_name || application.company, "Read-only draft"].filter(Boolean).join(" · ")));
    list.append(row);
  });
}

let savedResumesEpoch = 0;
function savedStandardVersion(standard) {
  const version = standard.active_version || standard.version || {};
  return standard.standard_version_id || standard.active_version_id || standard.version_id || version.standard_version_id || version.version_id || "";
}
async function loadSavedResumes() {
  const epoch = ++savedResumesEpoch;
  const status = $("#saved-resumes-status");
  status.textContent = "Loading saved resumes…";
  try {
    const result = await api("/api/v1/resume-lab/standards?limit=25");
    if (epoch !== savedResumesEpoch) return;
    const standards = result.standards || result.items || [];
    state.resumeStandards = standards;
    const select = $("#career-standard"); const selected = select.value;
    select.replaceChildren(node("option", "", "Choose a resume")); select.firstChild.value = "";
    standards.forEach((standard) => {
      const version = savedStandardVersion(standard);
      if (!version) return;
      const option = node("option", "", standard.name || standard.label || "Saved resume");
      option.value = version; select.append(option);
    });
    select.value = selected;
    const list = $("#saved-resume-list"); list.replaceChildren();
    if (!standards.length) list.append(node("p", "empty", result.configured === false ? "Resume storage is not configured for this installation." : "No saved resumes yet."));
    standards.forEach((standard) => {
      const row = node("article", "settings-resume-record");
      row.append(node("h3", "", standard.name || standard.label || standard.title || "Saved resume"));
      if (standard.updated_at || standard.created_at) row.append(node("p", "meta", `Saved ${opsTime(standard.updated_at || standard.created_at)}`));
      const artifact = standard.artifact || standard.resume_artifact || {};
      const version = standard.active_version || standard.version || {};
      const artifactId = standard.artifact_id || artifact.artifact_id || version.artifact_id;
      if (standard.document_url || artifactId) {
        const actions = node("div", "actions");
        const base = standard.document_url || `/api/v1/resume-lab/artifacts/${encodeURIComponent(artifactId)}`;
        const view = node("a", "", "View document"); view.href = standard.preview_url || `${base}?disposition=inline`; view.target = "_blank"; view.rel = "noopener noreferrer";
        const download = node("a", "", "Download"); download.href = base;
        actions.append(view, download); row.append(actions);
      } else row.append(node("p", "meta", "A downloadable document is not recorded for this resume."));
      list.append(row);
    });
    status.textContent = "";
  } catch (error) { if (epoch === savedResumesEpoch) status.textContent = `Could not load saved resumes. ${error.message}`; }
}
async function loadResumeStandards() { return loadSavedResumes(); }
let reviewBrief = null;
let reviewBriefDirty = false;
let reviewBriefEpoch = 0;
const reviewBriefSelects = ["broad_geography", "targeted_geography", "targeted_scope", "adjacent_roles", "conditional_order"];

function renderReviewBrief(result) {
  reviewBrief = result;
  const form = $("#review-preferences-form");
  for (const name of [...reviewBriefSelects, "stretch_policy"]) form.elements.namedItem(name).value = result.brief[name];
  for (const name of ["eligibility_facts", "notes"]) form.elements.namedItem(name).value = (result.brief[name] || []).join("\n");
  reviewBriefDirty = false;
  $("#reload-review-preferences").textContent = "Reload saved preferences";
  $("#review-preferences-fields").disabled = false;
}

async function loadReviewBrief() {
  if (reviewBriefDirty) return;
  const epoch = ++reviewBriefEpoch;
  const status = $("#review-preferences-status");
  status.textContent = "Loading review preferences…";
  $("#review-preferences-fields").disabled = true;
  try {
    const result = await api("/api/v1/job-reviews/brief");
    if (epoch !== reviewBriefEpoch) return;
    renderReviewBrief(result);
    status.textContent = result.revision ? "Your saved preferences are ready for new reviews." : "These defaults have not been saved yet. Review them, then save your preferences.";
  } catch (error) {
    if (epoch === reviewBriefEpoch) status.textContent = `Could not load review preferences. ${error.message}`;
  }
}

async function saveReviewBrief(event) {
  event.preventDefault();
  if (!reviewBrief) return;
  const form = $("#review-preferences-form");
  const brief = Object.fromEntries([...reviewBriefSelects, "stretch_policy"].map(name => [name, form.elements.namedItem(name).value.trim()]));
  for (const name of ["eligibility_facts", "notes"]) brief[name] = form.elements.namedItem(name).value.split("\n").map(line => line.trim()).filter(Boolean);
  const status = $("#review-preferences-status");
  if ([brief.eligibility_facts, brief.notes].some(lines => lines.length > 20 || lines.some(line => line.length > 500))) {
    status.textContent = "Use up to 20 lines per field, with no more than 500 characters on each line.";
    return;
  }
  $("#review-preferences-fields").disabled = true;
  status.textContent = "Saving review preferences…";
  try {
    const result = await api("/api/v1/job-reviews/save-brief", {method: "POST", body: JSON.stringify({
      brief, expected_revision: reviewBrief.revision, idempotency_key: key("review-brief"),
    })});
    renderReviewBrief(result);
    status.textContent = "Review preferences saved. They will apply to new reviews.";
  } catch (error) {
    status.textContent = `Preferences were not saved. Your edits are still here. ${error.message}`;
  } finally { $("#review-preferences-fields").disabled = false; }
}

async function loadSettingsPage(subpage = "") {
  const [pageName, queryString] = subpage.split('?');
  const briefingId = new URLSearchParams(queryString || '').get('briefing');
  subpage = pageName;
  renderSettingsSubview(subpage);
  if (subpage === "chief") await loadChief(briefingId);
  else if (subpage === "review-preferences") await loadReviewBrief();
  else if (subpage === "resumes") await loadSavedResumes();
  else if (subpage === "stored-records") { await loadApplications(); renderStoredRecords(); }
  else await loadSettings();
}
function initializeSettingsView() {
  initializeChief();
  $("#review-preferences-form").addEventListener("submit", saveReviewBrief);
  $("#reload-review-preferences").addEventListener("click", () => { reviewBriefDirty = false; loadReviewBrief(); });
  $("#review-preferences-form").addEventListener("input", () => {
    reviewBriefDirty = true;
    $("#reload-review-preferences").textContent = "Discard edits and reload";
    $("#review-preferences-status").textContent = "You have unsaved review preferences.";
  });
  $("#browser-connect-code").addEventListener("click", async () => {
    const button = $("#browser-connect-code"); button.disabled = true;
    try {
      const result = await api("/api/v1/browser/pairing", {method:"POST", body:JSON.stringify({idempotency_key:crypto.randomUUID()})});
      $("#browser-connection-code").textContent = result.pairing_code;
      $("#browser-code-notice").textContent = "Paste this code into the extension and click Connect browser. It expires in five minutes.";
      setTimeout(() => { $("#browser-connection-code").textContent = ""; }, 300000);
    } catch (error) { $("#browser-code-notice").textContent = error.message; }
    finally { button.disabled = false; }
  });
  $("#refresh-health").addEventListener("click", loadHealth);
  $("#scan-now").addEventListener("click", scanNow);
  setInterval(() => {
    if (!document.hidden && !$("#ops").hidden && !scanPending && !$("#refresh-health").disabled) refreshPipeline();
  }, 30000);
  $("#refresh-saved-resumes").addEventListener("click", loadSavedResumes);
  $("#refresh-career").addEventListener("click", loadCareerProfile);
  $("#career-editor-form").addEventListener("submit", saveCareerProfile);
  $("#career-import-form").addEventListener("submit", importCareerDocument);
  $("#career-paste-form").addEventListener("submit", importCareerText);
  $("#career-standard-form").addEventListener("submit", importCareerStandard);
  $("#approve-career").addEventListener("click", approveCareerProfile);
  $("#career-reviewed").addEventListener("change", (event) => { $("#approve-career").disabled = state.careerDirty || !event.target.checked; });
}
