"use strict";

// Apply the saved theme before styles paint; controls work even if the API is unavailable.
const themeStorageKey = "career-platform:theme";
function applyTheme(value) {
  const theme = value === "dark" ? "dark" : "light";
  document.documentElement.dataset.theme = theme;
  document.querySelector('meta[name="color-scheme"]').content = theme;
  document.querySelector("#theme-toggle")?.setAttribute("aria-pressed", String(theme === "dark"));
  document.querySelector("#theme-toggle")?.setAttribute("title", theme === "dark" ? "Switch to light mode" : "Switch to dark mode");
}
let savedTheme = "light";
try { savedTheme = localStorage.getItem(themeStorageKey); } catch (_) { /* Storage can be disabled. */ }
applyTheme(savedTheme);
document.addEventListener("DOMContentLoaded", () => {
  const toggle = document.querySelector("#theme-toggle");
  applyTheme(document.documentElement.dataset.theme);
  toggle.addEventListener("click", () => {
    const theme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    applyTheme(theme);
    try { localStorage.setItem(themeStorageKey, theme); } catch (_) { /* Keep the session usable. */ }
  });
});
window.addEventListener("storage", event => {
  if (event.key === themeStorageKey || event.key === null) applyTheme(event.newValue);
});

// Presentation state is independent of the services that own application changes.
const consoleState = { view: "applications", applicationId: "", tab: "overview", epoch: 0, workspace: null, reviews: [], actions: [] };
const stageLabel = (value) => ({ active: "Submitted", awaiting_confirmation: "Awaiting confirmation", preparing: state.applicationBackend === "owners" ? "Tracking" : "Draft", interviewing: "Interviewing", offer: "Offer", terminal: "Closed" }[value] || String(value || "Unknown").replaceAll("_", " "));
function displayDate(value) {
  if (!value) return "Not recorded";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? "Not recorded" : date.toLocaleString(undefined, { year: "numeric", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}
function postingDateValue(value) {
  // A date-only source value is a calendar date, not midnight UTC.
  return typeof value === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value)
    ? new Date(`${value}T12:00:00`) : new Date(value);
}
function postingDate(value) {
  if (!value) return "";
  const date = postingDateValue(value);
  return Number.isNaN(date.valueOf()) ? "" : date.toLocaleDateString(undefined, {year: "numeric", month: "short", day: "numeric"});
}
function relativePostingDate(value, now = new Date()) {
  if (!value) return "";
  const date = postingDateValue(value);
  if (Number.isNaN(date.valueOf())) return "";
  // Compare local calendar days, not elapsed hours (including DST boundaries).
  const day = d => Date.UTC(d.getFullYear(), d.getMonth(), d.getDate());
  const age = (day(now) - day(date)) / 86400000;
  return age === 0 ? "today" : age === 1 ? "yesterday" : postingDate(value);
}
function postingDates(item, prominent = false) {
  const job = item.job_posting || item;
  const posted = job.posted_at || (["ashby", "lever"].includes(job.ats) ? job.publishedAt : null);
  const parts = [];
  if (postingDate(posted)) parts.push(["Posted", posted]);
  if (postingDate(job.source_updated_at)) parts.push(["Updated", job.source_updated_at]);
  if (!parts.length && postingDate(job.publishedAt)) parts.push(["Posted or updated", job.publishedAt]);
  const root = node("p", `meta posting-dates${prominent ? " posting-dates-prominent" : ""}`);
  if (!parts.length) root.append(node("span", "posting-date", "Posting date unavailable"));
  parts.forEach(([label, value], index) => {
    if (index && !prominent) root.append(document.createTextNode(" · "));
    const time = node("time", "posting-date", `${label} ${prominent ? relativePostingDate(value) : postingDate(value)}`);
    time.dateTime = value;
    time.title = `${label} ${displayDate(postingDateValue(value))}`;
    if (prominent && ["today", "yesterday"].includes(relativePostingDate(value))) time.classList.add("recent");
    root.append(time);
  });
  return root;
}
function renderJobHistory(history, root, append = false) {
  if (!append) {
    root.replaceChildren(node("h3", "", "Job posting history"), node("p", "help", history.history_note));
    if (history.history_started_at) root.append(node("p", "meta", `Change tracking began ${displayDate(history.history_started_at)}. First-seen and closure dates recorded earlier are included where available.`));
    if (history.job) root.append(postingDates(history.job), node("p", "meta", history.job.closed_at ? "Posting currently closed" : "Posting currently open"));
  }
  const labels = {opened: "First seen open", first_seen: "First seen open", modified: "Posting modified", closed: "Posting closed", reopened: "Posting reopened"};
  const fields = {title: "Title", company: "Company", location: "Location", department: "Department", team: "Team", employmentType: "Employment type", isRemote: "Remote", workplaceType: "Workplace", jobUrl: "Posting URL", description: "Description", posted_at: "Posting date", source_updated_at: "Employer update date"};
  for (const event of history.events || []) {
    const entry = node("article", "stack-item posting-event");
    entry.append(node("h4", "", labels[event.event_type] || event.event_type), node("p", "meta", `Observed ${displayDate(event.observed_at)}`));
    if (event.source_at) entry.append(node("p", "meta", `Employer timestamp ${displayDate(event.source_at)}`));
    const changes = Object.entries(event.changes || {});
    if (changes.length) {
      const details = node("details", "technical-details");
      details.append(node("summary", "", `Changed: ${changes.map(([field]) => fields[field] || field).join(", ")}`));
      changes.forEach(([field, change]) => details.append(node("p", "message-body", field === "description"
        ? `Description changed (${change.before_length || 0} → ${change.after_length || 0} characters).`
        : `${fields[field] || field}: ${change.before || "Not specified"} → ${change.after || "Not specified"}`)));
      entry.append(details);
    }
    root.append(entry);
  }
  if (!append && !history.events?.length) root.append(node("p", "empty", "No posting observations recorded yet."));
  if (history.next_before) {
    const more = node("button", "quiet", "Load earlier history");
    const id = consoleState.applicationId, epoch = consoleState.epoch;
    more.onclick = async () => {
      more.disabled = true;
      try {
        const next = await api(`/api/v1/applications/${encodeURIComponent(id)}/job-history?before=${history.next_before}`);
        if (id !== consoleState.applicationId || epoch !== consoleState.epoch) return;
        more.remove(); renderJobHistory(next, root, true);
      } catch (error) { notice(error.message); more.disabled = false; }
    };
    root.append(more);
  }
}
function applicationHref(id, tab = "overview") { return `#applications/${encodeURIComponent(id)}/${tab}`; }
function openApplication(id, tab = "overview") {
  const hash = applicationHref(id, tab);
  if (location.hash === hash) routeConsole(); else location.hash = hash;
}
function applicationStatusLabel(app) {
  return stageLabel(app.current_phase);
}
function saveApplicationFilters() {
  const filters = {search: $("#application-search").value, phase: $("#application-phase").value, review: $("#application-needs-review").checked};
  try { sessionStorage.setItem("career-platform:application-filters", JSON.stringify(filters)); } catch (_) {}
  renderApplicationTable();
}
function initializeConsole() {
  const notifications = $("#header-notifications");
  document.addEventListener("click", event => { if (!notifications.contains(event.target)) notifications.open = false; });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && notifications.open) { notifications.open = false; notifications.querySelector("summary").focus(); }
  });
  $("#header-health").addEventListener("click", () => { notifications.open = false; });
  try {
    const filters = JSON.parse(sessionStorage.getItem("career-platform:application-filters") || "{}");
    $("#application-search").value = filters.search || "";
    $("#application-phase").value = filters.phase || "";
    $("#application-needs-review").checked = Boolean(filters.review);
  } catch (_) {}
  $("#application-search").addEventListener("input", saveApplicationFilters);
  $("#application-phase").addEventListener("change", saveApplicationFilters);
  $("#application-needs-review").addEventListener("change", saveApplicationFilters);
  window.addEventListener("beforeunload", event => { if (state.careerDirty) { event.preventDefault(); event.returnValue = ""; } });
  window.addEventListener("hashchange", routeConsole);
  routeConsole();
}
function routeConsole() {
  $("#header-notifications").open = false;
  const previousView = consoleState.view, previousId = consoleState.applicationId;
  const previousHash = consoleState.hash;
  let [view, rawId, tab] = location.hash.slice(1).split("?")[0].split("/");
  view = ({attention: "review", "outlook-actions": "review", interviews: "applications"})[view] || view || "applications";
  if (view === "resumes") { view = "settings"; rawId = "resumes"; }
  const settingsPages = {ops: "operations", career: "career-profile"};
  if (settingsPages[view]) { rawId = settingsPages[view]; view = "settings"; }
  if (previousView === "career" && !(view === "settings" && rawId === "career-profile") && state.careerDirty) {
    if (!window.confirm("Leave your unsaved profile edits?")) { history.replaceState(null, "", previousHash); return; }
    state.careerDirty = false; state.careerEpoch += 1;
  }
  const settingsPage = view === "settings" ? rawId || "home" : "";
  if (view === "settings") view = ({operations: "ops", "career-profile": "career"})[rawId] || view;
  if (!["applications", "shortlist", "review", "career", "ops", "settings"].includes(view)) view = "applications";
  let id = "";
  try { id = rawId ? decodeURIComponent(rawId) : ""; } catch (_) {}
  if (view === "applications" && id && tab === "actions") {
    history.replaceState(null, "", `#review?application=${encodeURIComponent(id)}`); return routeConsole();
  }
  const expandHistory = tab === "job-history";
  if (tab === "resume") tab = "documents";
  if (previousView === "applications" && !previousId) consoleState.listScroll = window.scrollY;
  const nextId = view === "applications" ? id : "";
  consoleState.epoch += 1;
  if (nextId !== previousId) consoleState.workspace = null;
  Object.assign(consoleState, {view, applicationId: nextId, settingsPage, tab: ["overview", "messages", "answers", "documents"].includes(tab) ? tab : "overview"});
  const canonical = view === "ops" || view === "career" ? `#settings/${settingsPages[view]}`
    : view === "settings" ? (settingsPage === "home" ? "#settings" : `#settings/${settingsPage}`)
    : view === "applications" ? (id ? applicationHref(id, consoleState.tab) : "#applications")
    : location.hash || `#${view}`;
  if (canonical !== location.hash) history.replaceState(null, "", canonical);
  consoleState.hash = location.hash;
  const recordOpen = view === "applications" && Boolean(nextId);
  consoleState.restoreListScroll = view === "applications" && !recordOpen && (previousView !== view || Boolean(previousId));
  $("#applications").classList.toggle("record-open", recordOpen);
  $("#application-workspace").hidden = !recordOpen;
  $("#workspace-posting-history").open = expandHistory;
  document.querySelectorAll("main > section").forEach(el => { el.hidden = el.id !== (view === "review" ? "attention" : view); });
  document.querySelectorAll(".sidebar a").forEach(el => {
    if (el.hash === `#${settingsPages[view] ? "settings" : view}`) el.setAttribute("aria-current", "page"); else el.removeAttribute("aria-current");
  });
  if (view === "settings") renderSettingsSubview(settingsPage);
  document.title = `${({applications:"Applications",shortlist:"Shortlist",review:"Review",career:"Career profile · Settings",ops:"Operations · Settings",settings:"Settings"})[view]} · Career Platform`;
  notice("");
  if (previousView !== view || previousId !== nextId) requestAnimationFrame(() => {
    window.scrollTo({top: view === "applications" && !recordOpen ? consoleState.listScroll || 0 : 0, behavior: "instant"});
  });
  renderApplicationTable();
  if (consoleState.initialized) loadConsoleView();
}
function jobSummary(item, applicationRecord = false) {
  const job = {...item, ...item.job_posting};
  const root = node("div", "job-summary");
  const heading = node("div", "job-summary-heading");
  const title = node("h3");
  if (applicationRecord) {
    const link = node("a", "application-link", item.title_snapshot || job.title || "Untitled role");
    link.href = applicationHref(item.application_id);
    title.append(link);
  } else title.append(jobPreviewButton(item));
  heading.append(title);
  root.append(heading, jobContext(item));
  return root;
}
function jobContext(item, includeCompany = true) {
  const job = {...item, ...item.job_posting};
  const context = node("div", "job-context");
  const company = item.employer_snapshot || job.company;
  if (includeCompany && company) context.append(node("strong", "job-company", company));
  if (job.location) {
    const location = node("span", "job-location");
    const pin = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    pin.setAttribute("viewBox", "0 0 24 24"); pin.setAttribute("aria-hidden", "true");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", "M20 10c0 6-8 11-8 11S4 16 4 10a8 8 0 1 1 16 0Z M15 10a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z");
    pin.append(path); location.append(pin, node("span", "", job.location)); context.append(location);
  }
  if (job.employmentType) {
    const employment = String(job.employmentType).replace(/([a-z])([A-Z])/g, "$1 $2").replace(/[_-]+/g, " ");
    context.append(node("span", "job-employment", employment));
  }
  return context;
}
function renderHeaderNotification(message, attention = false) {
  $("#notification-status").textContent = message;
  $("#notification-dot").hidden = !attention;
  $("#header-notifications summary").setAttribute("aria-label", attention ? "Notifications, attention needed" : "Notifications");
  $("#header-health").textContent = attention ? "View details" : "View system status";
}
