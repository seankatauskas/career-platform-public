const state = { item: null, selectedSource: "human" };

const el = (id) => document.getElementById(id);

function pretty(value) {
  return JSON.stringify(value, null, 2);
}

function renderStats(stats) {
  const batch = stats.batch ? ` · ${stats.batch}` : "";
  el("progress").textContent = `${stats.gold_labels} reviewed · ${stats.ready_to_review} ready now · ${stats.queued} sampled${batch}`;
}

function choose(source) {
  const prediction = state.item?.predictions?.[source];
  if (!prediction?.result) return;
  state.selectedSource = source;
  el("editor").value = pretty(prediction.result);
  el("message").textContent = "";
  el("editor").focus();
}

async function accept(source) {
  choose(source);
  await save(source);
}

function renderItem(item) {
  state.item = item;
  state.selectedSource = "human";
  el("message").textContent = "";
  if (!item) {
    el("job-meta").innerHTML = '<div class="empty">No generated predictions are waiting for review.</div>';
    el("description").textContent = "";
    el("predictions").innerHTML = "";
    el("editor").value = "";
    return;
  }
  el("job-meta").innerHTML = `<h2>${escapeHtml(item.title || "Untitled")}</h2>
    <p>${escapeHtml(item.company || "")} · ${escapeHtml(item.location || "")}</p>
    <p>${escapeHtml(item.ats)} · sample ${item.position} · ${escapeHtml(item.split)}${item.candidate_kind ? ` · ${escapeHtml(item.candidate_kind)} candidate` : ""}</p>`;
  el("description").textContent = item.description || "";
  el("note").value = item.note || "";
  const entries = Object.entries(item.predictions).filter(([, value]) => value.result);
  const legacy = item.legacy_result ? `
    <article class="prediction warning">
      <h3><span>legacy v2 gold (reference only)</span><span>not v3</span></h3>
      <pre>${escapeHtml(pretty(item.legacy_result))}</pre>
    </article>` : "";
  el("predictions").innerHTML = legacy + entries.map(([source, value]) => `
    <article class="prediction ${value.validation.length ? "warning" : ""}">
      <h3><span>${escapeHtml(source)}</span><span>${escapeHtml(value.status)}</span></h3>
      ${value.validation.length ? `<div>${escapeHtml(value.validation.join("; "))}</div>` : ""}
      <pre>${escapeHtml(pretty(value.result))}</pre>
      <button data-source="${escapeHtml(source)}">Accept ${escapeHtml(source)} ${source === "fireworks" ? "(1)" : source === "openai" ? "(2)" : ""}</button>
    </article>`).join("");
  document.querySelectorAll("[data-source]").forEach((button) => {
    button.addEventListener("click", () => accept(button.dataset.source));
  });
  const preferred = item.predictions.fireworks?.result ? "fireworks" : entries[0]?.[0];
  if (item.gold_result) {
    state.selectedSource = item.chosen_source || "human";
    el("editor").value = pretty(item.gold_result);
  } else if (preferred) {
    choose(preferred);
  } else {
    el("editor").value = pretty({ranges: []});
  }
  el("description").scrollTop = 0;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"
  })[char]);
}

async function request(path, options) {
  const response = await fetch(path, options);
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `Request failed (${response.status})`);
  return body;
}

async function load() {
  try {
    const body = await request("/api/item");
    renderStats(body.stats);
    renderItem(body.item);
  } catch (error) {
    el("message").textContent = error.message;
  }
}

async function save(source = "human") {
  if (!state.item) return;
  try {
    const result = JSON.parse(el("editor").value);
    const body = await request("/api/label", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        queue_id: state.item.id,
        source,
        result,
        note: el("note").value,
      }),
    });
    renderStats(body.stats);
    renderItem(body.item);
  } catch (error) {
    el("message").textContent = error.message;
  }
}

async function undo() {
  try {
    const body = await request("/api/undo", {method: "POST", headers: {"Content-Type": "application/json"}, body: "{}"});
    renderStats(body.stats);
    renderItem(body.item);
  } catch (error) {
    el("message").textContent = error.message;
  }
}

el("save").addEventListener("click", () => save("human"));
el("undo").addEventListener("click", undo);
document.addEventListener("keydown", (event) => {
  if (event.target.matches("textarea,input")) return;
  if (event.key === "1") accept("fireworks");
  if (event.key === "2") accept("openai");
  if (event.key.toLowerCase() === "e") el("editor").focus();
  if (event.key.toLowerCase() === "u") undo();
});

load();
