/* Shared read-only preview. Only the server's allowlisted HTML is rendered. */
let jobPreviewEpoch = 0;
let jobPreviewRequest;
const previewDialog = document.querySelector("#job-preview");
const previewBody = document.querySelector("#job-preview-body");
const previewHeaderActions = document.createElement("div");
previewHeaderActions.className = "job-preview-header-actions";
document.querySelector(".job-preview-header").append(previewHeaderActions);
previewHeaderActions.append(document.querySelector("#job-preview-close"));
function setPreviewPostingLink(job) {
  previewHeaderActions.querySelector("a")?.remove();
  const link = previewPostingLink(job.jobUrl || job.job_url_snapshot);
  if (link) previewHeaderActions.prepend(link);
}
function cancelJobPreviewRequest() {
  jobPreviewEpoch += 1;
  jobPreviewRequest?.abort();
}
document.querySelector("#job-preview-close").addEventListener("click", () => previewDialog.close());
previewDialog.addEventListener("close", () => {
  // The close event is queued; a new preview may already have opened.
  if (!previewDialog.open) cancelJobPreviewRequest();
});
previewDialog.addEventListener("cancel", cancelJobPreviewRequest);
previewDialog.addEventListener("click", event => {
  if (event.target !== previewDialog) return;
  const rect = previewDialog.getBoundingClientRect();
  if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) previewDialog.close();
});
window.addEventListener("hashchange", () => { if (previewDialog.open) previewDialog.close(); });

// Shared previews accept posting facts only, including when opened from Model picks.
// Scores and model explanations belong to the ranking list, never the preview.
function previewPostingFacts(source) {
  const fields = ["ats", "id", "job_id", "application_id", "title", "title_snapshot", "company",
    "employer_snapshot", "jobUrl", "job_url_snapshot", "curated_list_id", "location", "isRemote",
    "workplaceType", "employmentType", "department", "team", "last_seen", "closed_at",
    "posted_at", "publishedAt", "source_updated_at", "first_seen"];
  const posting = {...source, ...source.job_posting};
  return Object.fromEntries(fields.filter(field => posting[field] !== undefined).map(field => [field, posting[field]]));
}

function jobPreviewButton(job, label) {
  job = previewPostingFacts(job);
  const button = node("button", "role-detail-button", label || job.title || job.title_snapshot || "Untitled role");
  button.type = "button";
  button.setAttribute("aria-haspopup", "dialog");
  button.addEventListener("click", () => openJobPreview(job));
  return button;
}
function previewPostingLink(value) {
  try {
    const url = new URL(value);
    if (!["https:", "http:"].includes(url.protocol)) return null;
    const link = node("a", "button-link", "Open original posting ↗");
    link.href = url.href; link.target = "_blank"; link.rel = "noopener noreferrer";
    return link;
  } catch { return null; }
}
async function openJobPreview(source) {
  source = previewPostingFacts(source);
  cancelJobPreviewRequest();
  const epoch = jobPreviewEpoch;
  jobPreviewRequest = new AbortController();
  document.querySelector("#job-preview-title").textContent = source.title || source.title_snapshot || "Job details";
  document.querySelector("#job-preview-company").textContent = source.company || source.employer_snapshot || "";
  setPreviewPostingLink(source);
  previewBody.replaceChildren(node("p", "meta", "Loading job description…"));
  previewBody.setAttribute("aria-busy", "true");
  if (!previewDialog.open) previewDialog.showModal();
  previewDialog.scrollTop = 0;
  const query = source.application_id ? new URLSearchParams({application_id: source.application_id})
    : new URLSearchParams({ats: source.ats || "", id: source.id || source.job_id || ""});
  try {
    const result = await api(`/api/v1/jobs/preview?${query}`, {signal: jobPreviewRequest.signal});
    if (epoch !== jobPreviewEpoch || !previewDialog.open) return;
    const job = {...source, ...previewPostingFacts(result.job || {})};
    document.querySelector("#job-preview-title").textContent = job.title || job.title_snapshot || "Job details";
    document.querySelector("#job-preview-company").textContent = job.company || job.employer_snapshot || "";
    setPreviewPostingLink(job);
    previewBody.replaceChildren();
    const facts = node("dl", "job-preview-facts");
    const remote = [true, 1, "1", "true", "True"].includes(job.isRemote) ? "Remote" : "";
    for (const [label, value] of [["Location", job.location], ["Workplace", job.workplaceType || remote],
      ["Employment", job.employmentType], ["Department", job.department], ["Team", job.team]]) {
      if (!value) continue;
      const pair = node("div"); pair.append(node("dt", "", label), node("dd", "", value)); facts.append(pair);
    }
    previewBody.append(facts, postingDates(job));
    const provenance = node("details", "job-preview-source-details");
    provenance.append(node("summary", "", "Posting details"));
    if (job.ats) provenance.append(node("p", "meta", `Source: ${job.ats}`));
    if (job.last_seen) provenance.append(node("p", "meta", `Last checked ${displayDate(job.last_seen)}`));
    if (result.catalog_note) provenance.append(node("p", "meta", result.catalog_note));
    if (provenance.childElementCount > 1) previewBody.append(provenance);
    if (job.closed_at) previewBody.append(node("p", "phase", "This posting is closed."));
    if (source.curated_list_id) previewBody.append(node("p", "meta", "Selected and ordered by Codex."));
    const description = node("section", "job-preview-description");
    description.setAttribute("aria-label", "Job description");
    // This field is created by job_preview.render_description; raw source HTML never reaches this sink.
    if (result.description_html) description.innerHTML = result.description_html;
    else description.append(node("p", "empty", result.description_note));
    previewBody.append(description);
    if (result.formatting_note) previewBody.append(node("p", "meta", result.formatting_note));

  } catch (error) {
    if (epoch !== jobPreviewEpoch || error.name === "AbortError" || !previewDialog.open) return;
    const retry = node("button", "quiet", "Try again"); retry.type = "button";
    retry.addEventListener("click", () => openJobPreview(source));
    previewBody.replaceChildren(node("p", "", `Could not load this job. ${error.message}`), retry);
  } finally {
    if (epoch === jobPreviewEpoch) previewBody.removeAttribute("aria-busy");
  }
}
