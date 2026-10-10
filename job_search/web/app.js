"use strict";

const state = {
  csrf: "", shortlist: null, applications: [], resumeStandards: [],
  careerProfile: null, careerContent: null, careerDirty: false,
  careerEpoch: 0, careerImportTimer: null,
};

const RESUME_BLOCK_REASONS = {
  no_standard_resume: "Import at least one hand-written standard resume.",
  job_description_too_complex: "This job description contains too many distinct screening criteria for a safe comparison.",
  no_parse_safe_standard: "No hand-written resume passed PDF parsing and fidelity checks.",
  needs_normalization: "The recommended resume still needs a successful structured normalization pass.",
  generation_not_configured: "Configure the local resume model, PDF toolchain, and model worker.",
  local_model_failed: "The configured local resume model failed safely.",
  runpod_reconciliation_required: "Runpod may still have an accepted GPU job. Inspect that endpoint's job list before acknowledging a retry.",
  application_not_preparing: "This application has left preparation, so generation is closed.",
  career_page_overflow: "Your required content does not fit one page. Unpin some facts or shorten the profile, then regenerate.",
  career_one_page_overflow: "Your required content does not fit one page. Unpin some facts or shorten the profile, then regenerate.",
  career_setup_required: "Configure the resume model and document tools, or enter your information in the profile editor.",
  career_import_rejected: "This document could not be imported. Try text or an exported career profile, then review the draft.",
  page_overflow: "Your required content does not fit one page. Unpin some facts or shorten the profile, then regenerate.",
};

const $ = (selector) => document.querySelector(selector);
const node = (tag, className, text) => {
  const value = document.createElement(tag);
  if (className) value.className = className;
  if (text !== undefined) value.textContent = String(text);
  return value;
};
const key = (prefix) => `${prefix}-${crypto.randomUUID()}`;
const RESUME_COMMAND_STORAGE_PREFIX = "job-search:resume-command:v1:";
const resumeCommandEnvelopes = new Map();

function resumeCommandStorageKey(commandId) {
  return `${RESUME_COMMAND_STORAGE_PREFIX}${encodeURIComponent(commandId)}`;
}

function resumeCommandEnvelope(commandId, prefix, stablePayload = {}) {
  if (resumeCommandEnvelopes.has(commandId)) {
    return resumeCommandEnvelopes.get(commandId);
  }
  const storageKey = resumeCommandStorageKey(commandId);
  let stored = "";
  try {
    stored = sessionStorage.getItem(storageKey) || "";
  } catch (_error) {
    // In-memory retention still protects retries when browser storage is disabled.
  }
  let storedEnvelope = {};
  if (stored) {
    try {
      const parsed = JSON.parse(stored);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        storedEnvelope = parsed;
      }
    } catch (_error) {
      // Older dashboard versions stored only the idempotency key as plain text.
      if (stored.length <= 200) storedEnvelope = { idempotency_key: stored };
    }
  }
  const envelope = {};
  if (
    typeof storedEnvelope.idempotency_key === "string"
    && storedEnvelope.idempotency_key
    && storedEnvelope.idempotency_key.length <= 200
  ) {
    envelope.idempotency_key = storedEnvelope.idempotency_key;
  } else {
    envelope.idempotency_key = key(prefix);
  }
  Object.entries(stablePayload).forEach(([name, value]) => {
    envelope[name] = Object.hasOwn(storedEnvelope, name)
      ? storedEnvelope[name]
      : value;
  });
  resumeCommandEnvelopes.set(commandId, envelope);
  try {
    sessionStorage.setItem(storageKey, JSON.stringify(envelope));
  } catch (_error) {
    // See the in-memory fallback above.
  }
  return envelope;
}

function clearResumeCommandEnvelope(commandId) {
  resumeCommandEnvelopes.delete(commandId);
  try {
    sessionStorage.removeItem(resumeCommandStorageKey(commandId));
  } catch (_error) {
    // Storage may be unavailable; the in-memory entry is already gone.
  }
}

function notice(message) {
  const box = $("#notice");
  box.textContent = message;
  box.hidden = !message;
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.body !== undefined) {
    headers.set("Content-Type", "application/json");
    headers.set("X-CSRF-Token", state.csrf);
  }
  const response = await fetch(path, { ...options, headers, credentials: "same-origin" });
  let payload;
  try {
    payload = await response.json();
  } catch (_error) {
    const error = new Error(`Request failed (${response.status})`);
    error.status = response.status;
    throw error;
  }
  if (!response.ok) {
    const error = new Error(payload.message || payload.error || `Request failed (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return payload;
}

async function resumeMutation(commandId, prefix, path, payload, stablePayload = {}) {
  const envelope = resumeCommandEnvelope(commandId, prefix, stablePayload);
  try {
    const result = await api(path, {
      method: "POST",
      body: JSON.stringify({ ...payload, ...envelope }),
    });
    clearResumeCommandEnvelope(commandId);
    return result;
  } catch (error) {
    if (Number(error.status) >= 400 && Number(error.status) < 500) {
      clearResumeCommandEnvelope(commandId);
    }
    throw error;
  }
}

function clear(container) {
  container.replaceChildren();
  container.classList.remove("empty");
}

function meta(parts) {
  return node("p", "meta", parts.filter(Boolean).join(" · "));
}

function readableResumeReason(value) {
  const reason = String(value || "").trim();
  if (!reason) return "Resume comparison could not be prepared.";
  if (state.applicationBackend === "owners" && reason === "application_not_preparing") return "This application is closed or no longer matches this resume work.";
  if (reason === "runpod_reconciliation_required" || reason.startsWith("runpod_reconciliation_required:")) {
    const jobId = reason.includes(":") ? reason.slice(reason.indexOf(":") + 1) : "";
    return `${RESUME_BLOCK_REASONS.runpod_reconciliation_required}${jobId ? ` Accepted job: ${jobId}.` : ""}`;
  }
  return RESUME_BLOCK_REASONS[reason] || reason.replaceAll("_", " ");
}


let pageLoadEpoch = 0;
async function loadConsoleView() {
  const epoch = ++pageLoadEpoch;
  const view = consoleState.view;
  try {
    if (view === "applications") {
      await loadApplications();
      if (epoch === pageLoadEpoch && consoleState.restoreListScroll && !consoleState.applicationId) {
        window.scrollTo({top: consoleState.listScroll || 0, behavior: "instant"});
        consoleState.restoreListScroll = false;
      }
    }
    else if (view === "shortlist") await loadSavedShortlists();
    else if (view === "review") { await Promise.all([loadApplications(), loadReviewQueue()]); renderReviewQueue(); }
    else if (view === "ops") await loadHealth();
    else if (view === "career") await Promise.all([loadCareerProfile(), loadSavedResumes()]);
    else if (view === "settings") await loadSettingsPage(consoleState.settingsPage);
  } catch (error) {
    if (epoch !== pageLoadEpoch) return;
    notice(`Could not load this page: ${error.message}`);
    const retry = node("button", "quiet", "Try again");
    retry.onclick = () => { notice(""); loadConsoleView(); };
    $("#notice").append(document.createTextNode(" "), retry);
  }
}
async function initialize() {
  initializeConsole();
  initializeSettingsView();
  initializeDiscoveryViews();
  $("#refresh-applications").addEventListener("click", () => {
    Promise.all([loadApplications(), loadReviewQueue()]).catch(error => notice(error.message));
  });
  try {
    const session = await api("/api/v1/session");
    state.csrf = session.csrf_token;
    state.applicationBackend = session.application_backend;
    $("#demo-badge").hidden = !session.demo_mode;
    consoleState.initialized = true;
    loadConsoleView();
    // Navigation is usable while independent status sources are loading.
    if (consoleState.view !== "review") loadReviewQueue().catch(() => {
      $("#review-count").textContent = "";
      $("#review-count").title = "Review count unavailable";
    });
    if (consoleState.view !== "ops") loadHeaderHealth().catch(() => {
      renderHeaderNotification("System status could not be loaded. Open Operations to retry.", true);
    });
  } catch (error) {
    notice(`Could not connect to the app: ${error.message}`);
    const retry = node("button", "quiet", "Reconnect");
    retry.onclick = () => location.reload();
    $("#notice").append(document.createTextNode(" "), retry);
  }
}
initialize();
