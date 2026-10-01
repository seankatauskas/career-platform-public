"use strict";

importScripts("dashboard_connection.js");
const { dashboardBase, permissionOrigin } = JobDashboardConnection;
const pendingCaptures = new Map();

async function requirePermission(base) {
  if (!await chrome.permissions.contains({ origins: [permissionOrigin(base)] })) {
    throw new Error("Dashboard access was denied or removed. Click Fill application to grant access and pair again.");
  }
}

async function post(base, path, body) {
  const origin = dashboardBase(base);
  await requirePermission(origin);
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 15000);
  try {
    let response;
    try {
      response = await fetch(`${origin}${path}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        cache: "no-store",
        credentials: "omit",
        redirect: "error",
        referrerPolicy: "no-referrer",
        signal: controller.signal
      });
    } catch (_error) {
      throw new Error(origin.startsWith("https:")
        ? "Cannot reach the private dashboard. Check Tailscale and open this address in your browser. Redirects are not allowed."
        : "Cannot reach the local dashboard. Start it and check its address and port.");
    }
    let payload;
    try { payload = await response.json(); } catch (_error) {
      throw new Error("This address did not return a dashboard response. Check the dashboard address and access.");
    }
    if (!response.ok) {
      const error = String(payload.error || "");
      if ([401, 403].includes(response.status) || error === "invalid Host header") {
        throw new Error("The dashboard refused access. Check the authorized Tailscale account and extension access.");
      }
      if (/pairing code is invalid or expired/.test(error)) {
        throw new Error("The pairing code expired or was already used. Get a new code from the dashboard.");
      }
      if (/submission token is invalid or expired/.test(error)) {
        throw new Error("The application handoff expired. Get a new dashboard code and pair again before recording submission.");
      }
      throw new Error(error || `Dashboard request failed (${response.status})`);
    }
    return payload;
  } finally { clearTimeout(timeout); }
}

function receiptKey(tabId) { return `submission-${tabId}`; }

function pageIdentity(value) {
  const parsed = new URL(String(value));
  return `${parsed.origin}${parsed.pathname}`;
}

async function exchange(message) {
  const result = await post(message.dashboard_base, "/api/v1/autofill/exchange", {
    pairing_code: message.pairing_code,
    ats: message.form.ats,
    page_url: message.form.page_url,
    fields: message.form.fields
  });
  const applied = await chrome.tabs.sendMessage(message.tab_id, {
    type: "applyAssignments",
    assignments: result.assignments
  });
  await chrome.storage.session.set({
    [receiptKey(message.tab_id)]: {
      dashboard_base: dashboardBase(message.dashboard_base),
      submission_token: result.submission_token,
      idempotency_key: `mark-${crypto.randomUUID()}`,
      application: result.application,
      resume: result.resume,
      ats: message.form.ats,
      page_url: message.form.page_url
    }
  });
  await chrome.storage.local.set({ dashboard_base: dashboardBase(message.dashboard_base) });
  return {
    ok: true,
    filled: applied.filled,
    application: result.application,
    resume: result.resume
  };
}

async function stageCapture(message, sender) {
  const tabId = message.tab_id !== undefined
    ? message.tab_id
    : sender && sender.tab && sender.tab.id;
  if (tabId === undefined) return { ok: true, staged: false };
  const key = receiptKey(tabId);
  const stored = await chrome.storage.session.get(key);
  const receipt = stored[key];
  if (!receipt) return { ok: true, staged: false };
  const form = message.form || {};
  if (!form.supported || form.ats !== receipt.ats || !Array.isArray(form.answers)) {
    return { ok: true, staged: false };
  }
  const result = await post(receipt.dashboard_base, "/api/v1/autofill/capture", {
    submission_token: receipt.submission_token,
    ats: form.ats,
    page_url: form.page_url,
    answers: form.answers
  });
  return { ok: true, staged: result.staged, answer_count: result.answer_count };
}

function queueStageCapture(message, sender) {
  const tabId = message.tab_id !== undefined
    ? message.tab_id
    : sender && sender.tab && sender.tab.id;
  const work = stageCapture(message, sender);
  if (tabId !== undefined) {
    pendingCaptures.set(tabId, work);
    const clearPending = () => {
      if (pendingCaptures.get(tabId) === work) pendingCaptures.delete(tabId);
    };
    // Register both outcomes without creating an unobserved rejected Promise.
    work.then(clearPending, clearPending);
  }
  return work;
}

async function markSubmitted(message) {
  if (!["selected", "not_tracked"].includes(message.resume_decision)) {
    throw new Error("Choose whether this submission uses the selected resume.");
  }
  const key = receiptKey(message.tab_id);
  const stored = await chrome.storage.session.get(key);
  const receipt = stored[key];
  if (!receipt) throw new Error("No active autofill handoff for this tab.");
  const pending = pendingCaptures.get(message.tab_id);
  if (pending) await pending;
  // Once submission may have reached the server, retry the same idempotent
  // operation without attempting to stage another capture on a used receipt.
  if (!receipt.submission_started) {
    let liveCapture = null;
    try {
      liveCapture = await chrome.tabs.sendMessage(message.tab_id, { type: "captureForm" });
    } catch (_error) {
      liveCapture = null;
    }
    if (liveCapture && liveCapture.supported && liveCapture.answers.length) {
      await stageCapture({ tab_id: message.tab_id, form: liveCapture });
    }
  }
  const currentForm = await chrome.tabs.sendMessage(message.tab_id, { type: "describeForm" });
  if (!currentForm || !currentForm.supported) {
    throw new Error("Open the original supported ATS application page.");
  }
  await chrome.storage.session.set({ [key]: { ...receipt, submission_started: true } });
  const result = await post(receipt.dashboard_base, "/api/v1/autofill/submitted", {
    submission_token: receipt.submission_token,
    idempotency_key: receipt.idempotency_key,
    ats: currentForm.ats,
    page_url: currentForm.page_url,
    resume_decision: message.resume_decision
  });
  await chrome.storage.session.remove(key);
  return {
    ok: true,
    application: result.application,
    capture: result.autofill_capture || { private_answers: 0, custom_answers: 0 }
  };
}

async function receiptStatus(tabId) {
  const key = receiptKey(tabId);
  const stored = await chrome.storage.session.get(key);
  let currentForm = null;
  try {
    currentForm = await chrome.tabs.sendMessage(tabId, { type: "describeForm" });
  } catch (_error) {
    currentForm = null;
  }
  const receipt = stored[key];
  const active = Boolean(
    receipt && currentForm && currentForm.supported &&
    receipt.ats === currentForm.ats &&
    pageIdentity(receipt.page_url) === pageIdentity(currentForm.page_url)
  );
  return {
    ok: true,
    active,
    application: active && receipt.application,
    resume: active && receipt.resume
  };
}

chrome.runtime.onMessage.addListener((message, sender, respond) => {
  if (!message || !message.type) return false;
  let work;
  if (message.type === "exchangeHandoff") work = exchange(message);
  else if (message.type === "markSubmitted") work = markSubmitted(message);
  else if (message.type === "receiptStatus") work = receiptStatus(message.tab_id);
  else if (message.type === "stageCaptureFromPage") work = queueStageCapture(message, sender);
  else return false;
  work.then(respond).catch((error) => respond({ ok: false, error: error.message }));
  return true;
});

// Removing site permission also discards its short-lived handoffs. A fresh user
// gesture and pairing code are required before this extension can reconnect.
chrome.permissions.onRemoved.addListener(() => {
  (async () => {
    const stored = await chrome.storage.session.get(null);
    for (const [key, receipt] of Object.entries(stored)) {
      if (!key.startsWith("submission-") || !receipt.dashboard_base) continue;
      if (!await chrome.permissions.contains({ origins: [permissionOrigin(receipt.dashboard_base)] })) {
        await chrome.storage.session.remove(key);
      }
    }
    const saved = await chrome.storage.local.get("dashboard_base");
    if (saved.dashboard_base && !await chrome.permissions.contains({ origins: [permissionOrigin(saved.dashboard_base)] })) {
      await chrome.storage.local.remove("dashboard_base");
    }
  })().catch(() => {});
});

importScripts("tracking_worker.js");
