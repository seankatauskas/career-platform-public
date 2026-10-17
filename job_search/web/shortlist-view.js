// Page-owned templates and rendering. Shared state and UI primitives live in app.js / console.js.
document.querySelector("#shortlist").innerHTML = `
      <div class="section-heading shortlist-heading"><h2>Shortlist</h2></div>
      <div class="shortlist-toolbar">
        <label id="curated-list-label">Saved list<select id="curated-list"><option value="">Latest list</option></select></label>
        <button id="refresh-curated" class="quiet" type="button">Refresh</button>
        <button id="generate-list" type="submit" form="list-generator-form" disabled>Generate list</button>
        <p id="list-generation-status" role="status" aria-live="polite" hidden></p>
        <details id="shortlist-options" class="shortlist-options"><summary>Options</summary>
          <div class="shortlist-options-panel">
            <label>Source<select id="shortlist-source"><option value="curated">Codex picks</option><option value="model">Model picks</option></select></label>
            <label>Sort by<select id="shortlist-sort">
              <option value="original">Recommended order</option>
              <option value="posted-newest">Posted: newest first</option><option value="posted-oldest">Posted: oldest first</option>
              <option value="updated-newest">Updated: newest first</option><option value="updated-oldest">Updated: oldest first</option>
            </select></label>
            <div class="actions">
              <button id="older-curated" class="quiet" type="button" hidden>Earlier lists</button>
            </div>
          </div>
        </details>
      </div>
      <form id="list-generator-form" class="controls">
        <label id="list-generator-start-label" hidden>First list starts at (your local time)
          <input id="list-generator-start" type="datetime-local">
        </label>
        <button id="resume-list-generation" type="button" hidden>Resume generation</button>
      </form>
      <form id="shortlist-form" class="shortlist-filters">
        <div class="controls">
          <label>Posting window<select id="shortlist-days">
            <option value="1">Last 24 hours</option><option value="7">Last 7 days</option>
            <option value="14">Last 14 days</option><option value="30" selected>Last 30 days</option><option value="90">Last 90 days</option>
          </select></label>
          <label class="check"><input id="remote-only" type="checkbox"> Remote only</label>
          <button type="submit">Refresh shortlist</button>
        </div>
        <details class="shortlist-advanced"><summary>Advanced filters</summary>
          <div class="controls">
            <label>Ranking policy<select id="policy"><option value="selective">Selective</option><option value="champion">Champion</option><option value="broad">Broad</option><option value="compare">Compare</option></select></label>
            <label>Number of jobs<input id="limit" type="number" min="1" max="100" value="20"></label>
            <label>Annual salary floor<input id="salary-floor" type="number" min="0" step="5000" placeholder="Optional"></label>
          </div>
        </details>
      </form>
      <p id="shortlist-feedback" class="notice" role="status" hidden></p>
      <p class="help" id="shortlist-window">Open postings from the last 30 days, sorted by relevance. Older Greenhouse records may use an update date.</p>
      <p class="help" id="shortlist-loading" role="status" aria-live="polite" hidden></p>
      <div class="shortlist-company-filter">
        <p class="meta" id="shortlist-company-filter-status" role="status" hidden></p>
        <label class="check"><input id="exclude-recent-companies" type="checkbox" checked aria-describedby="shortlist-company-filter-period"> Exclude recently applied companies</label>
        <span id="shortlist-company-filter-period" class="help">Last 180 days</span>
      </div>
      <div id="shortlist-list" class="card-grid empty">Refresh to find roles that match your search.</div>
`;

let listGeneration = null;
let listGenerationTimer = null;
let listGenerationCommand = null;
function renderListGeneration(value) {
  const justFinished = listGeneration?.status === 'active' && value.status === 'published';
  listGeneration = value;
  const active = value.status === 'active';
  $('#generate-list').disabled = active;
  $('#generate-list').textContent = active && !value.can_resume ? 'Generating…' : 'Generate list';
  $('#list-generator-form').hidden = !value.needs_initial_start && !value.can_resume;
  $('#list-generator-start-label').hidden = !value.needs_initial_start;
  $('#list-generator-start').required = Boolean(value.needs_initial_start);
  $('#resume-list-generation').hidden = !value.can_resume;
  const root = $('#list-generation-status');
  root.replaceChildren();
  const phases = {awaiting_inputs: 'Queued; waiting to gather jobs', queued: 'Queued', running: 'Reviewing jobs',
    validating: 'Validating recommendations', published: 'Lists published', failed: 'Generation failed',
    interrupted: 'Generation interrupted', dispatch_failed: 'Generation could not start'};
  root.hidden = !active;
  root.classList.toggle('is-running', active && !value.can_resume);
  const phaseLabel = phases[value.phase] || 'Generating your list';
  const resumeLabel = value.phase === 'interrupted' ? 'Resume to continue.' : 'Resume to retry.';
  if (active) root.append(node('span', '', value.can_resume
    ? `${phaseLabel}. ${resumeLabel}` : `${phaseLabel}…`));
  if (justFinished) loadSavedShortlists();
  clearTimeout(listGenerationTimer);
  if (active && !value.can_resume) listGenerationTimer = setTimeout(loadListGeneration, 10000);
}
async function loadListGeneration() {
  try {
    const result = await api('/api/v1/job-reviews/generation-status');
    if (result.review_id) listGenerationCommand = null;
    renderListGeneration(result);
  }
  catch (error) { $('#list-generation-status').classList.remove('is-running'); $('#list-generation-status').hidden = false; $('#list-generation-status').textContent = error.message; }
}
async function requestListGeneration(resume = false) {
  const button = $(resume ? '#resume-list-generation' : '#generate-list');
  button.disabled = true;
  try {
    if (!listGenerationCommand) {
      const body = {idempotency_key: key('generate-list')};
      if (resume) body.review_id = listGeneration.review_id;
      else if (listGeneration?.needs_initial_start) {
        const date = new Date($('#list-generator-start').value);
        if (!Number.isFinite(date.valueOf())) throw new Error('Choose the starting date and time.');
        body.initial_start = date.toISOString();
      }
      listGenerationCommand = {action: resume ? 'generation-resume' : 'generate', body};
    }
    const command = listGenerationCommand;
    const result = await api(`/api/v1/job-reviews/${command.action}`, {method: 'POST', body: JSON.stringify(command.body)});
    listGenerationCommand = null;
    renderListGeneration(result);
  } catch (error) {
    if (error.status >= 400 && error.status < 500) listGenerationCommand = null;
    $('#list-generation-status').classList.remove('is-running');
    $('#list-generation-status').hidden = false;
    $('#list-generation-status').textContent = error.message;
    button.disabled = false;
  } finally { $('#resume-list-generation').disabled = false; }
}
document.querySelector('#list-generator-form').addEventListener('submit', event => { event.preventDefault(); requestListGeneration(); });
document.querySelector('#resume-list-generation').addEventListener('click', () => requestListGeneration(true));

let shortlistSource = "auto";
let shortlistLoadEpoch = 0;
let curatedBefore = null;
let modelShortlist = null;
const shortlistSortKey = "career-platform:shortlist-sort";
const shortlistCompanyFilterKey = "career-platform:exclude-recent-companies";
let excludeRecentCompanies = true;
try {
  excludeRecentCompanies = localStorage.getItem(shortlistCompanyFilterKey) !== "false";
} catch (_) { /* The default still applies when browser storage is blocked. */ }
document.querySelector("#exclude-recent-companies").checked = excludeRecentCompanies;
document.querySelector("#exclude-recent-companies").addEventListener("change", (event) => {
  excludeRecentCompanies = event.target.checked;
  try { localStorage.setItem(shortlistCompanyFilterKey, String(excludeRecentCompanies)); } catch (_) { /* Session-only preference. */ }
  if (state.shortlist) renderShortlist(state.shortlist, true);
});
const shortlistSortLabels = {
  "posted-newest": "Newest posted first",
  "posted-oldest": "Oldest posted first",
  "updated-newest": "Most recently updated first",
  "updated-oldest": "Least recently updated first",
};
let shortlistSort = "original";
try {
  const saved = localStorage.getItem(shortlistSortKey);
  if (Object.hasOwn(shortlistSortLabels, saved)) shortlistSort = saved;
} catch (_) { /* Sorting still works when browser storage is blocked. */ }

function sortedShortlistJobs(jobs, order) {
  if (!Object.hasOwn(shortlistSortLabels, order)) return [...jobs];
  const updated = order.startsWith("updated-");
  const direction = order.endsWith("oldest") ? 1 : -1;
  const dated = jobs.map((item, index) => {
    const job = item.job_posting || item;
    // Never treat discovery or an ambiguous legacy Greenhouse timestamp as publication.
    const value = updated ? job.source_updated_at
      : job.posted_at || (["ashby", "lever"].includes(job.ats) ? job.publishedAt : null);
    const timestamp = value ? postingDateValue(value).valueOf() : NaN;
    return {item, index, timestamp};
  });
  return dated.sort((a, b) => {
    const aMissing = !Number.isFinite(a.timestamp);
    const bMissing = !Number.isFinite(b.timestamp);
    if (aMissing !== bMissing) return aMissing ? 1 : -1;
    return (aMissing ? 0 : direction * (a.timestamp - b.timestamp)) || a.index - b.index;
  }).map(entry => entry.item);
}

function changeShortlistSort() {
  shortlistSort = $("#shortlist-sort").value;
  try { localStorage.setItem(shortlistSortKey, shortlistSort); } catch (_) { /* Session-only preference. */ }
  if (state.shortlist) renderShortlist(state.shortlist, true);
}

function shortlistDate(value) {
  const date = new Date(value);
  return Number.isFinite(date.valueOf()) ? new Intl.DateTimeFormat(undefined,
    {month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'}).format(date) : 'Unknown date';
}
function shortlistLabel(list) {
  const title = list.title || 'Recommendations';
  if (/^OLD METHOD \|/i.test(title) && list.window_start && list.window_end) {
    const kind = /\btargeted\b/i.test(title) ? 'Targeted' : 'Broad';
    const format = value => new Intl.DateTimeFormat('en-US', {timeZone: 'America/Chicago', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZoneName: 'short'}).format(new Date(value));
    return `${kind} · ${format(list.window_start)} → ${format(list.window_end)}`;
  }
  return title;
}

function setShortlistControls(source) {
  $("#shortlist-source").value = source;
  $("#shortlist-form").hidden = source !== "model";
  $("#shortlist-loading").hidden = source !== "model" || !$("#shortlist-loading").textContent;
  $("#curated-list-label").hidden = source !== "curated";
  $("#refresh-curated").hidden = source !== "curated";
  $("#older-curated").hidden = source !== "curated" || !curatedBefore;
}

async function loadSavedShortlists(older = false) {
  const epoch = ++shortlistLoadEpoch;
  const requestedId = location.hash.startsWith("#shortlist/curated_") ? location.hash.slice(11) : "";
  $("#shortlist-feedback").hidden = true;
  try {
    const data = await api(`/api/v1/curated-shortlists${older && curatedBefore ? `?before=${curatedBefore}` : ""}`);
    if (epoch !== shortlistLoadEpoch) return;
    curatedBefore = data.next_before;
    const select = $("#curated-list");
    if (!older) select.replaceChildren(new Option("Latest list", ""));
    for (const item of data.lists) {
      if (![...select.options].some(option => option.value === item.list_id)) {
        select.append(new Option(shortlistLabel(item), item.list_id));
      }
    }
    const source = requestedId ? "curated" : shortlistSource === "auto" ? (data.lists.length ? "curated" : "model") : shortlistSource;
    setShortlistControls(source);
    if (source === "model") {
      modelShortlist = await api("/api/v1/shortlist");
      if (epoch === shortlistLoadEpoch) renderShortlist(modelShortlist);
      return;
    }
    const id = requestedId || select.options[1]?.value;
    if (!id) {
      renderShortlist({source: "curated", recommendations: []});
      return;
    }
    const result = await api(`/api/v1/curated-shortlist?list_id=${encodeURIComponent(id)}`);
    if (epoch !== shortlistLoadEpoch) return;
    if (requestedId && ![...select.options].some(option => option.value === id)) select.append(new Option(shortlistLabel(result), id));
    select.value = id;
    renderShortlist(result);
  } catch (error) { if (epoch === shortlistLoadEpoch) { const feedback = $("#shortlist-feedback"); feedback.textContent = `Could not load this shortlist. Your previous results are unchanged. ${error.message}`; feedback.hidden = false; } }
}

const reviewFitLabels = {
  close: "Close technical fit", slight_stretch: "Slight stretch", bigger_stretch: "Bigger stretch",
  broad_only: "Broad consideration", needs_info: "More information needed", exclude: "Not recommended",
};

function appendReviewSummary(content, job) {
  const summary = job.review_summary;
  if (!summary) {
    if (job.explanation) content.append(node("p", "curated-explanation", job.explanation));
    return;
  }
  const labels = node("p", "review-fit-labels");
  const fit = reviewFitLabels[summary.decision];
  if (fit) labels.append(node("strong", "review-fit", fit));
  if (summary.eligibility === "unresolved") labels.append(node("span", "review-condition", "Eligibility needs clarification"));
  if (summary.eligibility === "ineligible") labels.append(node("span", "review-condition", "Eligibility requirement unmet"));
  if (summary.category === "alternative") labels.append(node("span", "review-alternative", "Career alternative"));
  if (labels.childElementCount) content.append(labels);
  if (job.explanation) content.append(node("p", "curated-explanation", job.explanation));
  // New publications contain the complete, reviewed narrative. Older records keep
  // their original explanation and reveal recorded caveats without rewriting it.
  if (!summary.narrative_complete) {
    const existing = (job.explanation || "").toLocaleLowerCase();
    const seen = new Set();
    for (const [label, entries] of [
      ["Eligibility", [summary.eligibility_condition]],
      ["Gap", summary.gaps || []], ["Unconfirmed", summary.unknowns || []],
    ]) {
      for (const entry of entries) {
        if (!entry || seen.has(entry) || existing.includes(entry.toLocaleLowerCase())) continue;
        seen.add(entry);
        const caveat = node("p", "review-caveat");
        caveat.append(node("strong", "", `${label}: `), document.createTextNode(entry));
        content.append(caveat);
      }
    }
  }
  const nextSteps = {apply: "Apply", clarify: "Clarify eligibility before applying", explore: "Explore this career direction"};
  if (!summary.narrative_complete && nextSteps[summary.next_step]) content.append(node("p", "meta review-next-step", `Next step: ${nextSteps[summary.next_step]}.`));
  const availability = summary.availability;
  if (availability) {
    const status = {open: "Open on the employer board", absent: "Not found on the employer board", unknown: "Employer availability unconfirmed"}[availability.status];
    if (status) content.append(node("p", "meta review-availability", `${status}${availability.checked_at ? ` · Checked ${displayDate(availability.checked_at)}` : ""}`));
  }
  if (summary.related_group) content.append(node("p", "meta review-related-marker", `Related postings: ${summary.related_group.label}`));
}

function shortlistCardContainers(list, recommendations, curated) {
  const containers = new Map();
  // Date sorts remain global posting sorts. Grouping must not pull siblings away
  // from their dates or modify persisted recommendation order.
  if (!curated || shortlistSort !== "original") return containers;
  let category = null, categoryRoot = list;
  for (let index = 0; index < recommendations.length;) {
    const job = recommendations[index];
    const currentCategory = job.review_summary?.category === "alternative" ? "alternative" : "core";
    if (currentCategory !== category) {
      category = currentCategory;
      categoryRoot = list;
      if (category === "alternative") {
        categoryRoot = node("section", "shortlist-alternatives");
        categoryRoot.append(node("h3", "", "Career alternatives"), node("p", "help", "Related technical work with a different career direction. These roles are included for exploration."));
        list.append(categoryRoot);
      }
    }
    const group = job.review_summary?.related_group;
    let end = index + 1;
    if (group?.id) {
      while (end < recommendations.length && recommendations[end].review_summary?.related_group?.id === group.id
        && (recommendations[end].review_summary?.category === "alternative" ? "alternative" : "core") === category) end++;
    }
    let root = categoryRoot;
    if (end - index > 1) {
      root = node("section", "shortlist-related-group");
      root.append(node("h3", "", group.label), node("p", "help", `${end - index} related postings. Each has its own requisition and application.`));
      categoryRoot.append(root);
    }
    for (; index < end; index++) {
      // Placeholders retain order when a later section is inserted before cards
      // are rendered into their assigned containers.
      const slot = node("div", "shortlist-card-slot");
      root.append(slot); containers.set(recommendations[index], slot);
    }
  }
  return containers;
}

function renderShortlist(result, preserveFilters = false) {
  state.shortlist = result;
  const curated = result.source === "curated";
  $("#shortlist-sort").value = shortlistSort;
  $("#shortlist-sort option[value=original]").textContent = curated ? "Recommended order" : "Relevance";
  if (!curated) modelShortlist = result;
  setShortlistControls(curated ? "curated" : "model");
  const days = Number(result.options?.days || 30);
  if (!preserveFilters) $("#shortlist-days").value = String(days);
  if (result.options && !preserveFilters) {
    $("#policy").value = result.options.policy || "selective";
    $("#limit").value = result.options.limit || 20;
    $("#salary-floor").value = result.options.salary_floor ?? "";
    $("#remote-only").checked = !!result.options.remote_only;
  }
  const ordering = shortlistSortLabels[shortlistSort]
    ? `${shortlistSortLabels[shortlistSort]}. Original ranks shown; jobs without this date appear last.`
    : curated ? "Ordered by Codex." : "Sorted by relevance.";
  $("#shortlist-window").textContent = curated
    ? (result.list_id ? `Saved ${shortlistDate(result.created_at)}` : "Your recommendations will appear here.")
    : `Postings from the last ${days === 1 ? "24 hours" : `${days} days`}. ${ordering}`;
  $("#shortlist-window").hidden = curated && Boolean(result.list_id);
  const list = $("#shortlist-list");
  clear(list);
  const freshness = document.getElementById("shortlist-freshness") || node("p", "help");
  freshness.id = "shortlist-freshness";
  const stale = !curated && Object.values(result.data_status || {}).find((item) => item.status === "stale" || !item.freshness_verified);
  freshness.textContent = stale ? `Saved model scores: ${postingDate(stale.latest_score_at) || "date unavailable"}. Latest posting check: ${postingDate(stale.source_last_seen) || "unknown"}. Ranking refresh is incomplete; newly collected or changed jobs may be missing.` : "";
  freshness.hidden = !stale;
  list.before(freshness);
  const allRecommendations = result.recommendations || [];
  const visibleRecommendations = excludeRecentCompanies
    ? allRecommendations.filter(job => !job.recent_company_application) : allRecommendations;
  const recommendations = sortedShortlistJobs(visibleRecommendations, shortlistSort);
  const hiddenCount = allRecommendations.length - recommendations.length;
  const filterStatus = $("#shortlist-company-filter-status");
  filterStatus.hidden = !allRecommendations.length;
  filterStatus.textContent = `${recommendations.length} of ${allRecommendations.length} roles shown${hiddenCount ? ` · ${hiddenCount} hidden` : ""}.`;
  if (!recommendations.length) {
    list.classList.add("empty");
    list.textContent = hiddenCount ? 'All roles in this list are from companies you applied to in the last 180 days. Uncheck “Exclude recently applied companies” to show them.'
      : curated ? (result.list_id ? "No jobs were selected for this list." : "No Codex picks have been published yet.") : !result.session_id && !result.options
      ? "Choose your posting window and refresh to load saved model rankings."
      : result.model?.ready ? "No jobs matched these filters. Try a wider posting window or adjust Advanced filters."
      : "Model rankings are not available for this policy yet. Try another policy in Advanced filters or browse Codex picks.";
    return;
  }
  list.classList.remove("empty");
  const cardContainers = shortlistCardContainers(list, recommendations, curated);
  recommendations.forEach((job) => {
    const card = node("article", curated ? "card curated-card" : "card");
    card.dataset.ats = job.ats || "";
    card.dataset.jobId = job.id || "";
    const rank = node("span", "rank", String(job.rank || "–").padStart(2, "0"));
    rank.title = curated ? "Original Codex rank" : "Original relevance rank";
    card.append(rank);
    const content = jobSummary({...job, session_id: job.session_id || result.session_id});
    const recentApplication = job.recent_company_application;
    const company = content.querySelector(".job-company");
    if (company && recentApplication) {
      const companyLine = node("span", "shortlist-company");
      company.replaceWith(companyLine);
      const badge = node("a", "shortlist-company-applied", "Applied recently");
      badge.href = `#applications/${encodeURIComponent(recentApplication.application_id)}/overview`;
      const detail = `Applied to this company within the last 180 days. Applied to ${company.textContent} on ${displayDate(recentApplication.applied_at)}. View application.`;
      badge.title = detail;
      badge.setAttribute("aria-label", `Applied recently. ${detail}`);
      companyLine.append(company, badge);
    }
    if (curated) appendReviewSummary(content, job);
    if (curated && job.closed_at) content.append(node("p", "meta", "Posting closed"));
    const score = job.ranking_score ?? job.final_score;
    if (!curated && score !== undefined && score !== null) {
      const diagnostics = node("details", "ranking-details");
      diagnostics.append(node("summary", "", "Ranking details"), node("p", "meta shortlist-score", `Ranking score ${Number(score).toFixed(3)}`));
      content.append(diagnostics);
    }
    card.append(content);
    const actions = node("div", "actions");
    actions.append(postingDates(job, true));
    const posting = previewPostingLink(job.jobUrl);
    if (posting) {
      posting.className = "quiet";
      posting.textContent = "Open posting ↗";
      actions.append(posting);
    }
    if (curated && job.application_id && (state.applicationBackend === "owners" || job.application_phase !== "preparing")) {
      const existing = node("a", "primary-action", "View application");
      existing.href = applicationHref(job.application_id);
      actions.append(existing);
    }
    card.append(actions);
    const slot = cardContainers.get(job);
    if (slot) slot.replaceWith(card); else list.append(card);
  });
}

async function refreshShortlist(event) {
  event.preventDefault();
  if (state.shortlistLoading) return;
  state.shortlistLoading = true;
  shortlistSource = "model";
  const epoch = ++shortlistLoadEpoch;
  notice("");
  $("#shortlist-feedback").hidden = true;
  const button = event.submitter || $("#shortlist-form button[type=submit]");
  const status = $("#shortlist-loading");
  button.disabled = true;
  button.textContent = "Loading shortlist…";
  status.hidden = false;
  status.textContent = "Loading saved rankings for this posting window…";
  $("#shortlist-list").setAttribute("aria-busy", "true");
  const controller = new AbortController();
  const slow = setTimeout(() => { status.textContent = "This lookup is taking longer than expected. Background scoring runs separately."; }, 5000);
  const timeout = setTimeout(() => controller.abort(), 20000);
  const floor = $("#salary-floor").value.trim();
  try {
    const result = await api("/api/v1/shortlist", {
      method: "POST",
      signal: controller.signal,
      body: JSON.stringify({
        idempotency_key: key("shortlist"),
        options: {
          policy: $("#policy").value,
          days: Number($("#shortlist-days").value),
          limit: Number($("#limit").value),
          remote_only: $("#remote-only").checked,
          salary_floor: floor ? Number(floor) : null,
        },
      }),
    });
    modelShortlist = result;
    if (epoch === shortlistLoadEpoch) {
      renderShortlist(result);
      status.textContent = `${(result.recommendations || []).length} jobs loaded from saved rankings.`;
      status.hidden = false;
    }
  } catch (error) {
    if (epoch === shortlistLoadEpoch) {
      status.textContent = "Shortlist was not refreshed. Your previous results are unchanged.";
      const feedback = $("#shortlist-feedback");
      feedback.textContent = controller.signal.aborted ? "The shortlist lookup timed out. Background ranking continues separately; please retry." : error.message;
      feedback.hidden = false;
    }
  } finally {
    clearTimeout(slow);
    clearTimeout(timeout);
    state.shortlistLoading = false;
    $("#shortlist-list").removeAttribute("aria-busy");
    button.disabled = false;
    button.textContent = "Refresh shortlist";
  }
}
