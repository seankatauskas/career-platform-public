const PROGRAM_QUERY = "days=30";

const state = {
  job: null,
  program: null,
  qualificationFit: "",
  primaryReason: "",
  hardBlockers: new Set(),
  recommendations: [],
  modelRunId: "",
  busy: false,
};

const $ = (selector) => document.querySelector(selector);

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

function showNotice(message, kind = "error") {
  const notice = $("#notice");
  notice.textContent = message;
  notice.className = `notice ${kind}`;
  notice.hidden = false;
  window.clearTimeout(showNotice.timer);
  showNotice.timer = window.setTimeout(() => { notice.hidden = true; }, 6000);
}

function setSelected(group, value) {
  document.querySelectorAll(`${group} button`).forEach((button) => {
    const selected = button.dataset.value === value;
    button.classList.toggle("selected", selected);
    button.setAttribute("aria-pressed", selected ? "true" : "false");
  });
}

function chooseChip(group, key) {
  const next = state[key] === this.dataset.value ? "" : this.dataset.value;
  state[key] = next;
  setSelected(group, next);
}

function chooseBlocker() {
  const value = this.dataset.value;
  if (state.hardBlockers.has(value)) state.hardBlockers.delete(value);
  else state.hardBlockers.add(value);
  const selected = state.hardBlockers.has(value);
  this.classList.toggle("selected", selected);
  this.setAttribute("aria-pressed", selected ? "true" : "false");
}

function resetAnnotations() {
  state.qualificationFit = "";
  state.primaryReason = "";
  state.hardBlockers.clear();
  setSelected("#qualification-options", "");
  setSelected("#primary-reason-options", "");
  document.querySelectorAll("#hard-blocker-options button").forEach((button) => {
    button.classList.remove("selected");
    button.setAttribute("aria-pressed", "false");
  });
  $("#skip-reason").value = "other";
  $("#note").value = "";
  $("#optional-context").open = false;
}

function fact(label, value) {
  if (!value) return null;
  const item = document.createElement("div");
  const term = document.createElement("span");
  const content = document.createElement("strong");
  term.textContent = label;
  content.textContent = value;
  item.append(term, content);
  return item;
}

function formatDate(value) {
  if (!value) return "Posting date unavailable";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  const days = Math.max(0, Math.floor((Date.now() - date.getTime()) / 86400000));
  const relative = days === 0 ? "today" : days === 1 ? "1 day ago" : `${days} days ago`;
  return `${relative} · ${date.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" })}`;
}

function renderProgram(program) {
  state.program = program;
  $("#decisive-count").textContent = program.decisive.toLocaleString();
  $("#interested-count").textContent = program.counts.interested.toLocaleString();
  $("#pass-count").textContent = program.counts.not_interested.toLocaleString();
  $("#overall-remaining").textContent = program.overall_remaining.toLocaleString();

  const list = $("#stage-list");
  list.replaceChildren(...program.stages.map((stage) => {
    const item = document.createElement("li");
    item.className = stage.complete ? "complete" : program.stage?.id === stage.id ? "active" : "";
    item.textContent = `${stage.id}. ${stage.name}`;
    const amount = document.createElement("span");
    amount.textContent = `${stage.completed}/${stage.quota}`;
    item.append(amount);
    return item;
  }));

  const card = $("#program-card");
  card.classList.toggle("complete", program.complete);
  if (program.complete) {
    $("#stage-number").textContent = "Program complete";
    $("#stage-name").textContent = "Your 1,000-decision dataset is finished";
    $("#stage-description").textContent = "The first 800 decisive labels are training data and the final 200 are protected evaluation data.";
    $("#stage-completed").textContent = program.overall_total.toLocaleString();
    $("#stage-total").textContent = program.overall_total.toLocaleString();
    $("#stage-remaining").textContent = "0";
    $("#stage-progress-bar").style.width = "100%";
    return;
  }

  const stage = program.stage;
  $("#stage-number").textContent = `Stage ${stage.number} of ${program.stage_count}`;
  $("#stage-name").textContent = stage.name;
  $("#stage-description").textContent = stage.description;
  $("#stage-completed").textContent = stage.completed.toLocaleString();
  $("#stage-total").textContent = stage.quota.toLocaleString();
  $("#stage-remaining").textContent = stage.remaining.toLocaleString();
  $("#stage-progress-bar").style.width = `${stage.completed * 100 / stage.quota}%`;
}

function renderJob(job) {
  state.job = job;
  resetAnnotations();
  $("#loading").hidden = true;
  if (!job) {
    $("#job-content").hidden = true;
    $("#loading").hidden = false;
    if (state.program?.complete) {
      $("#loading").innerHTML = "<strong>Labeling complete.</strong><br>You have finished every guided stage. The evaluation set is protected and ready for model assessment.";
    } else {
      $("#loading").innerHTML = "<strong>No eligible recent job is available.</strong><br>Run the job collector again to add fresh described jobs, then reload this page.";
    }
    return;
  }

  $("#job-content").hidden = false;
  $("#published-at").textContent = formatDate(job.publishedAt);
  $("#job-title").textContent = job.title || "Untitled role";
  $("#company").textContent = job.company || "Unknown company";
  $("#job-link").href = job.jobUrl || "#";
  $("#job-link").hidden = !job.jobUrl;

  const facts = [
    fact("Location", job.location),
    fact("Workplace", job.workplaceType || (String(job.isRemote).toLowerCase() === "true" ? "Remote" : "")),
    fact("Employment", job.employmentType),
    fact("Department", job.department),
    fact("Team", job.team),
  ].filter(Boolean);
  $("#job-facts").replaceChildren(...facts);

  const description = $("#job-description");
  description.hidden = !job.description;
  description.textContent = job.description || "";
}

async function refreshStats() {
  const stats = await api(`/api/stats?${PROGRAM_QUERY}`);
  renderProgram(stats.program);
  return stats;
}

async function loadJob() {
  if (state.busy) return;
  state.busy = true;
  $("#job-content").hidden = true;
  $("#loading").hidden = false;
  $("#loading").textContent = "Selecting a varied recent job…";
  try {
    const [result, stats] = await Promise.all([
      api(`/api/job?${PROGRAM_QUERY}`),
      refreshStats(),
    ]);
    renderProgram(stats.program);
    renderJob(result.job);
  } catch (error) {
    showNotice(error.message);
    $("#loading").textContent = "Could not load a job.";
  } finally {
    state.busy = false;
  }
}

async function label(interest) {
  if (state.busy || !state.job) return;
  state.busy = true;
  document.querySelectorAll(".decision").forEach((button) => { button.disabled = true; });
  try {
    const result = await api("/api/label", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ats: state.job.ats,
        id: state.job.id,
        selection_token: state.job.selection_token,
        interest,
        skip_reason: interest === "skipped" ? $("#skip-reason").value : "",
        qualification_fit: state.qualificationFit,
        primary_reason: state.primaryReason,
        hard_blockers: [...state.hardBlockers],
        note: $("#note").value,
      }),
    });
    renderProgram(result.program);
    if (result.stage_transition) {
      const message = result.program.complete
        ? "All five stages are complete."
        : `Stage complete. Starting ${result.program.stage.name}.`;
      showNotice(message, "success");
    }
    state.busy = false;
    await loadJob();
  } catch (error) {
    state.busy = false;
    showNotice(error.message);
  } finally {
    document.querySelectorAll(".decision").forEach((button) => { button.disabled = false; });
  }
}

async function undo() {
  if (state.busy) return;
  state.busy = true;
  try {
    const result = await api("/api/undo", { method: "POST", headers: { "Content-Length": "0" } });
    if (result.undone) {
      showNotice("Last label removed. That job is back in the program.", "success");
      state.busy = false;
      await loadJob();
    } else {
      showNotice("There is no label to undo.");
    }
  } catch (error) {
    showNotice(error.message);
  } finally {
    state.busy = false;
  }
}

function showRecommendationNotice(message, kind = "error") {
  const notice = $("#recommendation-notice");
  notice.textContent = message;
  notice.className = `notice ${kind}`;
  notice.hidden = false;
}

function showView(name) {
  const labeling = name === "labeling";
  $("#labeling-view").hidden = !labeling;
  $("#recommendations-view").hidden = labeling;
  $("#show-labeling").classList.toggle("active", labeling);
  $("#show-recommendations").classList.toggle("active", !labeling);
  if (!labeling) loadRecommendations();
}

function formatScore(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return number >= 0 && number <= 1 ? `${Math.round(number * 100)}%` : number.toFixed(3);
}

function explanationText(explanation) {
  if (!explanation || typeof explanation !== "object") return "";
  if (typeof explanation.summary === "string") return explanation.summary;
  const phrases = explanation.positive_sparse_phrases
    || explanation.positive_phrases
    || explanation.sparse_positive_phrases
    || [];
  if (Array.isArray(phrases) && phrases.length) {
    return `Matching signals: ${phrases.slice(0, 5).map((item) => (
      typeof item === "string" ? item : item.phrase || item.text || ""
    )).filter(Boolean).join(", ")}.`;
  }
  const neighbors = explanation.similar_liked_family_ids
    || explanation.nearest_liked_examples
    || explanation.similar_liked
    || [];
  if (Array.isArray(neighbors) && neighbors.length) {
    const names = neighbors.slice(0, 3).map((item) => (
      typeof item !== "object" || item === null
        ? ""
        : item.title && item.company
          ? `${item.title} at ${item.company}`
          : item.title || item.company || item.label || ""
    )).filter(Boolean);
    if (names.length) return `Similar to jobs you liked: ${names.join(", ")}.`;
  }
  return "";
}

function recommendationCard(job) {
  const card = document.createElement("article");
  card.className = `recommendation-card ${job.segment || ""}`;

  const rank = document.createElement("div");
  rank.className = "recommendation-rank";
  rank.textContent = `#${job.rank}`;
  if (job.policy_id && job.policy_id !== "champion") {
    const policy = document.createElement("span");
    policy.className = "explore-label";
    policy.textContent = job.policy_id;
    rank.append(policy);
  }
  if (job.segment === "explore") {
    const explore = document.createElement("span");
    explore.className = "explore-label";
    explore.textContent = "Explore";
    rank.append(explore);
  }

  const title = document.createElement("h2");
  title.textContent = job.title || "Untitled role";
  const company = document.createElement("p");
  company.className = "company";
  company.textContent = job.company || "Unknown company";

  const facts = document.createElement("div");
  facts.className = "job-facts";
  facts.replaceChildren(...[
    fact("Location", job.location),
    fact("Workplace", job.workplaceType || (String(job.isRemote).toLowerCase() === "true" ? "Remote" : "")),
    fact("Employment", job.employmentType),
    fact("Posted", formatDate(job.publishedAt)),
    fact("Variants", String(job.variant_count || 1)),
  ].filter(Boolean));

  const scores = document.createElement("div");
  scores.className = "score-line";
  const components = job.score_components || {};
  [
    ["Preference", formatScore(job.final_score)],
    ["Combined", formatScore(job.ranking_score)],
    ["Semantic", formatScore(components.dense_linear)],
    ["Neighbors", formatScore(components.dense_neighbor)],
    ["Lexical", formatScore(components.sparse)],
    ["Salary", job.salary?.status || "unknown"],
  ].forEach(([label, value]) => {
    const item = document.createElement("span");
    item.append(`${label} `);
    const strong = document.createElement("strong");
    strong.textContent = value;
    item.append(strong);
    scores.append(item);
  });

  const reason = document.createElement("p");
  reason.className = "recommendation-explanation";
  reason.textContent = explanationText(job.explanation)
    || "Ranked from your learned interest signals; practical constraints are shown separately.";

  const actions = document.createElement("div");
  actions.className = "recommendation-actions";
  const open = document.createElement("a");
  open.href = job.jobUrl || "#";
  open.target = "_blank";
  open.rel = "noopener noreferrer";
  open.textContent = "Open posting ↗";
  if (!job.jobUrl) open.hidden = true;
  actions.append(open);
  [
    ["applied", "Applied"],
    ["saved", "Save"],
    ["dismissed_preference", "Not for me"],
    ["blocked", "Blocked"],
    ["duplicate", "Duplicate"],
  ].forEach(([action, label]) => {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    button.dataset.action = action;
    if (action === "dismissed_preference") button.className = "pass-action";
    button.addEventListener("click", () => recordFeedback(job, action, button));
    actions.append(button);
  });

  card.append(rank, title, company, facts, scores, reason, actions);
  return card;
}

async function recordFeedback(job, action, button) {
  button.disabled = true;
  try {
    await api("/api/recommendation-feedback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ats: job.ats,
        id: job.id,
        family_id: job.family_id,
        action,
        rank: job.rank,
        model_run_id: job.model_run_id,
        policy_id: job.policy_id,
        session_id: job.session_id,
        impression_id: job.impression_id,
        semantic_score: job.semantic_score,
        ranking_score: job.ranking_score,
      }),
    });
    button.textContent = "Recorded";
    showRecommendationNotice("Feedback saved. It will guide future ranking without creating a manual label.", "success");
  } catch (error) {
    button.disabled = false;
    showRecommendationNotice(error.message);
  }
}

async function loadRecommendations() {
  const list = $("#recommendation-list");
  list.innerHTML = '<div class="empty-state">Loading recommendations…</div>';
  $("#recommendation-notice").hidden = true;
  const query = new URLSearchParams({
    limit: $("#recommendation-limit").value || "20",
    remote_only: $("#remote-only").checked ? "true" : "false",
    policy: $("#recommendation-policy").value || "selective",
  });
  const floor = $("#salary-floor").value.trim();
  if (floor) query.set("salary_floor", floor);
  try {
    const result = await api(`/api/recommendations?${query}`);
    state.recommendations = result.recommendations;
    state.modelRunId = result.model.champion_run_id || "";
    if (!result.model.ready) {
      $("#model-summary").textContent = "No model is available for this preference mode yet.";
      list.innerHTML = '<div class="empty-state"><strong>No ranked jobs yet.</strong><br>Finish the proxy teacher and distillation commands, then refresh.</div>';
      return;
    }
    $("#model-summary").textContent = `${result.model.score_count.toLocaleString()} jobs scored by ${result.model.model_revision || "the champion model"}.`;
    if (result.candidate_pool_truncated && result.recommendations.length < result.options.limit) {
      showRecommendationNotice("The bounded candidate scan reached its safety limit. Refresh scores or relax constraints to fill the shortlist.");
    }
    if (!result.recommendations.length) {
      list.innerHTML = '<div class="empty-state"><strong>No jobs meet this view.</strong><br>Relax a constraint or refresh the collected postings.</div>';
      return;
    }
    list.replaceChildren(...result.recommendations.map(recommendationCard));
  } catch (error) {
    list.replaceChildren();
    showRecommendationNotice(error.message);
  }
}

document.querySelectorAll("#qualification-options button").forEach((button) => {
  button.addEventListener("click", chooseChip.bind(button, "#qualification-options", "qualificationFit"));
});
document.querySelectorAll("#primary-reason-options button").forEach((button) => {
  button.addEventListener("click", chooseChip.bind(button, "#primary-reason-options", "primaryReason"));
});
document.querySelectorAll("#hard-blocker-options button").forEach((button) => {
  button.addEventListener("click", chooseBlocker.bind(button));
});
$("#interested").addEventListener("click", () => label("interested"));
$("#maybe").addEventListener("click", () => label("maybe"));
$("#not-interested").addEventListener("click", () => label("not_interested"));
$("#skip").addEventListener("click", () => label("skipped"));
$("#undo").addEventListener("click", undo);
$("#show-labeling").addEventListener("click", () => showView("labeling"));
$("#show-recommendations").addEventListener("click", () => showView("recommendations"));
$("#refresh-recommendations").addEventListener("click", loadRecommendations);
$("#recommendation-controls").addEventListener("submit", (event) => {
  event.preventDefault();
  loadRecommendations();
});

document.addEventListener("keydown", (event) => {
  const typing = ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName);
  if (typing || $("#labeling-view").hidden || event.metaKey || event.ctrlKey || event.altKey) return;
  if (event.key.toLowerCase() === "i") label("interested");
  if (event.key.toLowerCase() === "m") label("maybe");
  if (event.key.toLowerCase() === "n") label("not_interested");
  if (event.key.toLowerCase() === "s") label("skipped");
  if (event.key.toLowerCase() === "u") undo();
});

showView("recommendations");
