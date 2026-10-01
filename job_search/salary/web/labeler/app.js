const state = { item: null, busy: false };
const $ = (id) => document.getElementById(id);

function money(value, currency) {
  if (value == null) return "?";
  const formatted = Number(value).toLocaleString(undefined, { maximumFractionDigits: 2 });
  return `${currency === "UNKNOWN" ? "" : currency + " "}${formatted}`;
}

function rangeMarkup(range) {
  const values = range.min_value === range.max_value
    ? money(range.min_value, range.currency)
    : `${money(range.min_value, range.currency)} – ${money(range.max_value, range.currency)}`;
  const scope = range.location_scope ? ` · ${range.location_scope}` : "";
  const badges = [range.source_type, range.component, range.value_kind, range.period + scope].join(" · ");
  return `<article class="range-card">
    <div class="range-value">${escapeHtml(values)}</div>
    <div class="range-detail">${escapeHtml(badges)}</div>
    <blockquote class="evidence">${escapeHtml(range.evidence_text || "No evidence text")}</blockquote>
  </article>`;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;"
  })[char]);
}

function render(item, stats) {
  state.item = item;
  if (stats) {
    const estimates = [];
    const precision = stats.estimate_details.positive_precision;
    const miss = stats.estimate_details.negative_miss_rate;
    if (precision) {
      estimates.push(`${(100 * precision.estimate).toFixed(1)}% precision (95% CI ${(100 * precision.ci95[0]).toFixed(0)}–${(100 * precision.ci95[1]).toFixed(0)}%)`);
    }
    if (miss) {
      estimates.push(`${(100 * miss.estimate).toFixed(1)}% miss rate (95% CI ${(100 * miss.ci95[0]).toFixed(0)}–${(100 * miss.ci95[1]).toFixed(0)}%)`);
    }
    $("progress").textContent = `${stats.labeled} / ${stats.total} reviewed · ${stats.remaining} left${estimates.length ? " · " + estimates.join(" · ") : ""}`;
  }
  $("empty").hidden = Boolean(item);
  $("item").hidden = !item;
  if (!item) {
    $("description").textContent = "";
    return;
  }
  $("auditBadge").textContent = item.audit_type === "positive" ? "Parser found salary" : "Parser found none";
  $("auditBadge").className = `badge ${item.audit_type}`;
  $("source").textContent = item.ranges[0]?.source_type || "negative audit";
  $("weight").textContent = `weight ${Number(item.sampling_weight).toFixed(1)}`;
  $("title").textContent = item.title || "Untitled job";
  $("meta").textContent = [item.company, item.ats, item.location, item.publishedAt].filter(Boolean).join(" · ");
  $("ranges").innerHTML = item.ranges.length
    ? item.ranges.map(rangeMarkup).join("")
    : `<article class="range-card"><strong>No extracted salary.</strong><p class="range-detail">Read the description and check whether the rules missed salaried pay.</p></article>`;
  const options = item.audit_type === "positive"
    ? [["correct", "Correct"], ["incorrect", "Incorrect"], ["unclear", "Unclear"], ["non_salaried_pay", "Hourly / other pay"]]
    : [["no_salary", "No salary"], ["missed_salary", "Missed salary"], ["unclear", "Unclear"], ["non_salaried_pay", "Hourly / other pay"]];
  $("actions").innerHTML = options.map(([value, label], index) =>
    `<button data-verdict="${value}">${label} <kbd>${index + 1}</kbd></button>`
  ).join("");
  $("actions").querySelectorAll("button").forEach((button) => {
    button.addEventListener("click", () => submit(button.dataset.verdict));
  });
  $("note").value = item.note || "";
  $("description").textContent = item.description || "No stored description.";
  $("description").scrollTop = 0;
  $("original").href = item.jobUrl || "#";
  $("original").hidden = !item.jobUrl;
  $("error").textContent = "";
}

async function api(path, options) {
  const response = await fetch(path, options);
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `Request failed (${response.status})`);
  return body;
}

async function submit(verdict) {
  if (state.busy || !state.item) return;
  state.busy = true;
  try {
    const body = await api("/api/label", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ queue_id: state.item.id, verdict, note: $("note").value })
    });
    render(body.item, body.stats);
  } catch (error) {
    $("error").textContent = error.message;
  } finally { state.busy = false; }
}

async function undo() {
  if (state.busy) return;
  state.busy = true;
  try {
    const body = await api("/api/undo", { method: "POST", body: "{}" });
    render(body.item, body.stats);
  } catch (error) {
    $("error").textContent = error.message;
  } finally { state.busy = false; }
}

window.addEventListener("keydown", (event) => {
  const target = event.target instanceof Element ? event.target : null;
  const isTyping = target?.matches("textarea, input, select") || target?.isContentEditable;
  if (isTyping || event.ctrlKey || event.metaKey || event.altKey || event.repeat) return;

  // `event.key` varies with keyboard layout and Shift; `event.code` reliably
  // distinguishes both the number row and the numeric keypad.
  const shortcuts = {
    Digit1: 0, Numpad1: 0,
    Digit2: 1, Numpad2: 1,
    Digit3: 2, Numpad3: 2,
    Digit4: 3, Numpad4: 3,
  };
  const index = shortcuts[event.code] ?? ({ "1": 0, "2": 1, "3": 2, "4": 3 }[event.key]);
  if (index !== undefined) {
    const button = $("actions").querySelectorAll("button")[index];
    if (button) {
      event.preventDefault();
      button.click();
    }
  } else if (event.code === "KeyZ" || event.key.toLowerCase() === "z") {
    event.preventDefault();
    undo();
  }
}, true);
$("undo").addEventListener("click", undo);

Promise.all([api("/api/item"), api("/api/stats")])
  .then(([itemBody, statsBody]) => render(itemBody.item, statsBody))
  .catch((error) => { $("progress").textContent = "Unable to load"; $("error").textContent = error.message; });
