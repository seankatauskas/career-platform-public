// Page-owned templates and rendering. Shared state and UI primitives live in app.js / console.js.
document.querySelector("#shortlist").innerHTML = `
      <div class="section-heading"><div><h2>Shortlist</h2><p class="section-note">Explore roles selected for you.</p></div><a href="#settings/operations">Scan jobs and check ranking</a></div>
      <div class="shortlist-toolbar controls">
        <label>Source<select id="shortlist-source"><option value="curated">Codex picks</option><option value="model">Model picks</option></select></label>
        <label id="curated-list-label">Saved list<select id="curated-list"><option value="">Latest list</option></select></label>
        <label>Sort by<select id="shortlist-sort">
          <option value="original">List order</option>
          <option value="posted-newest">Posted: newest first</option>
          <option value="posted-oldest">Posted: oldest first</option>
          <option value="updated-newest">Updated: newest first</option>
          <option value="updated-oldest">Updated: oldest first</option>
        </select></label>
        <button id="refresh-curated" class="quiet" type="button">Refresh saved lists</button>
        <button id="older-curated" class="quiet" type="button" hidden>Earlier lists</button>
      </div>
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
      <div id="shortlist-list" class="card-grid empty">Refresh to find roles that match your search.</div>
`;

let shortlistSource = "auto";
let shortlistLoadEpoch = 0;
let curatedBefore = null;
let modelShortlist = null;
const shortlistSortKey = "career-platform:shortlist-sort";
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
        select.append(new Option(`${displayDate(item.created_at)} · ${item.title} (${item.job_count})`, item.list_id));
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
    if (requestedId && ![...select.options].some(option => option.value === id)) select.append(new Option(`${displayDate(result.created_at)} · ${result.title}`, id));
    select.value = requestedId || "";
    renderShortlist(result);
  } catch (error) { if (epoch === shortlistLoadEpoch) { const feedback = $("#shortlist-feedback"); feedback.textContent = `Could not load this shortlist. Your previous results are unchanged. ${error.message}`; feedback.hidden = false; } }
}

function renderShortlist(result, preserveFilters = false) {
  state.shortlist = result;
  const curated = result.source === "curated";
  $("#shortlist-sort").value = shortlistSort;
  $("#shortlist-sort option[value=original]").textContent = curated ? "Codex order" : "Relevance";
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
    ? (result.list_id ? `${result.title} · Saved ${displayDate(result.created_at)}${result.window_start ? ` · Reviewed ${displayDate(result.window_start)} – ${displayDate(result.window_end)}` : ""}. ${ordering}` : "Codex can publish its selected jobs here after reviewing the catalog.")
    : `Open postings from the last ${days === 1 ? "24 hours" : `${days} days`}. ${ordering} Older Greenhouse records may use an update date for filtering.`;
  const list = $("#shortlist-list");
  clear(list);
  const freshness = document.getElementById("shortlist-freshness") || node("p", "help");
  freshness.id = "shortlist-freshness";
  const stale = Object.values(result.data_status || {}).find((item) => item.status === "stale" || !item.freshness_verified);
  freshness.textContent = stale ? `Saved model scores: ${postingDate(stale.latest_score_at) || "date unavailable"}. Latest posting check: ${postingDate(stale.source_last_seen) || "unknown"}. Ranking refresh is incomplete; newly collected or changed jobs may be missing.` : "";
  freshness.hidden = !stale;
  list.before(freshness);
  const recommendations = sortedShortlistJobs(result.recommendations || [], shortlistSort);
  if (!recommendations.length) {
    list.classList.add("empty");
    list.textContent = curated ? (result.list_id ? "No jobs were selected for this list." : "No Codex picks have been published yet.") : !result.session_id && !result.options
      ? "Choose your posting window and refresh to load saved model rankings."
      : result.model?.ready ? "No jobs matched these filters. Try a wider posting window or adjust Advanced filters."
      : "Model rankings are not available for this policy yet. Try another policy in Advanced filters or browse Codex picks.";
    return;
  }
  list.classList.remove("empty");
  recommendations.forEach((job) => {
    const card = node("article", curated ? "card curated-card" : "card");
    const rank = node("span", "rank", String(job.rank || "–").padStart(2, "0"));
    rank.title = curated ? "Original Codex rank" : "Original relevance rank";
    card.append(rank);
    const content = jobSummary({...job, session_id: job.session_id || result.session_id});
    if (curated && job.explanation) content.append(node("p", "curated-explanation", job.explanation));
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
    if (curated && job.application_id && job.application_phase !== "preparing") {
      const existing = node("a", "primary-action", "View application");
      existing.href = `#applications/${encodeURIComponent(job.application_id)}/overview`;
      actions.append(existing);
    }
    card.append(actions);
    list.append(card);
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
      status.textContent = `${(result.recommendations || []).length} jobs shown from saved rankings.`;
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
