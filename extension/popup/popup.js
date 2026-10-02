"use strict";

let activeTabId = null;
let form = null;
let resumeStatus = null;
let browserConnected = false;

const $ = (selector) => document.querySelector(selector);

function show(message, good = false) {
  const notice = $("#notice");
  notice.textContent = message;
  notice.style.color = good ? "#bad49d" : "#e7a762";
}

function showResumeStatus(value) {
  resumeStatus = value && typeof value === "object" ? value : { status: "unavailable" };
  const target = $("#resume-status");
  if (resumeStatus.status === "selected") {
    const name = String(resumeStatus.name || "Selected resume");
    const kind = resumeStatus.comparison_kind === "grounded_rewrite"
      ? "approved job-tailored rewrite"
      : "hand-written standard";
    target.textContent = `Resume: ${name} (${kind}) selected.`;
  } else if (resumeStatus.status === "not_selected") {
    target.textContent = "Resume: no tracked resume selected.";
  } else {
    target.textContent = "Resume selection status unavailable; check the dashboard.";
  }
}

async function send(message) {
  const result = await chrome.runtime.sendMessage(message);
  if (!result || !result.ok) throw new Error((result && result.error) || "Extension request failed.");
  return result;
}

async function initialize() {
  const saved = await chrome.storage.local.get("dashboard_base");
  if (saved.dashboard_base) $("#dashboard-base").value = saved.dashboard_base;
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || tab.id === undefined) throw new Error("No active browser tab.");
  activeTabId = tab.id;
  const tracking = await send({type: "trackingStatus", tab_id: activeTabId});
  browserConnected = tracking.connected;
  $("#disconnect-browser").hidden = !browserConnected;
  $("#connect-browser").hidden = browserConnected;
  $("#pairing-code").parentElement.hidden = browserConnected;
  if (browserConnected) {
    $("#tracking-status").textContent = tracking.error || tracking.result?.label || (tracking.supported ? "Job recognized. Submit normally; tracking is automatic." : "Connected. Open an Ashby, Greenhouse, or Lever application.");
    if (tracking.queued) $("#tracking-status").textContent += ` ${tracking.queued} observations waiting to sync.`;
    if (tracking.answer_error) $("#tracking-status").textContent += ` ${tracking.answer_error}`;
    else if (tracking.answers_queued) $("#tracking-status").textContent += ` ${tracking.answers_queued} answer snapshots waiting to sync.`;
    else if (tracking.answer_status?.saved) $("#tracking-status").textContent += ` ${tracking.answer_status.field_count} application fields saved.${tracking.answer_status.incomplete ? ' Some fields exceeded capture limits; check Answers in the dashboard.' : ''}`;
    $("#form-status").textContent = tracking.job ? `${tracking.job.ats} · ${tracking.job.board}` : "Browser connected";
    $("#fill").disabled = !tracking.supported;
    $("#mark-submitted").disabled = true;
    return;
  }
  try {
    form = await chrome.tabs.sendMessage(activeTabId, { type: "describeForm" });
  } catch (_error) {
    throw new Error("Open a supported Ashby, Greenhouse, or Lever application page.");
  }
  if (!form.supported) throw new Error("This page is not a supported ATS application.");
  $("#form-status").textContent = `${form.ats} form · ${form.fields.length} supported fields`;
  $("#fill").disabled = false;
  const receipt = await send({ type: "receiptStatus", tab_id: activeTabId });
  showResumeStatus(receipt.resume);
  $("#mark-submitted").disabled = !receipt.active;
  if (receipt.active && receipt.application) {
    $("#mark-submitted").textContent = `Mark ${receipt.application.title} submitted`;
  }
}

$("#fill").addEventListener("click", async () => {
  const button = $("#fill");
  if (browserConnected) {
    button.disabled = true;
    try {
      const result = await send({type:"trackingFill", tab_id:activeTabId});
      $("#resume-status").textContent = result.resume?.message || "Attach your resume manually.";
      show(`Filled ${result.filled} fields. ${result.resume?.message || ''} Review before submitting.`, result.resume?.status !== 'manual');
    }
    catch(error) { show(error.message); }
    finally { button.disabled = false; }
    return;
  }
  const code = $("#pairing-code").value.trim();
  if (!code) { show("Paste the one-time dashboard code."); return; }
  button.disabled = true;
  try {
    const dashboard = JobDashboardConnection.dashboardBase($("#dashboard-base").value);
    // Request from the click handler before awaiting anything: Chromium requires
    // a user gesture, and only this exact machine origin is requested.
    if (dashboard.startsWith("https:") && !await chrome.permissions.request({
      origins: [JobDashboardConnection.permissionOrigin(dashboard)]
    })) {
      throw new Error("Dashboard access was denied. Allow this private dashboard when you are ready to pair.");
    }
    const result = await send({
      type: "exchangeHandoff",
      dashboard_base: dashboard,
      pairing_code: code,
      tab_id: activeTabId,
      form
    });
    $("#pairing-code").value = "";
    $("#mark-submitted").disabled = false;
    $("#mark-submitted").textContent = `Mark ${result.application.title} submitted`;
    showResumeStatus(result.resume);
    show(`Filled ${result.filled} stored fields. Review the application before submitting.`, true);
  } catch (error) {
    show(error.message);
    button.disabled = false;
  } finally {
    $("#pairing-code").value = "";
  }
});

$("#mark-submitted").addEventListener("click", async () => {
  const button = $("#mark-submitted");
  const resumeDecision = resumeStatus && resumeStatus.status === "selected"
    ? "selected"
    : "not_tracked";
  if (
    resumeDecision === "not_tracked" &&
    !window.confirm(
      "No tracked resume is selected in this handoff. Record this submission without a tracked resume?"
    )
  ) return;
  button.disabled = true;
  try {
    const result = await send({
      type: "markSubmitted",
      tab_id: activeTabId,
      resume_decision: resumeDecision
    });
    const count = Number(result.capture.private_answers || 0) + Number(result.capture.custom_answers || 0);
    show(`Submission recorded; ${count} eligible answers securely captured. Email confirmation remains separate.`, true);
  } catch (error) {
    show(error.message);
    button.disabled = false;
  }
});

initialize().catch((error) => {
  $("#form-status").textContent = error.message;
  show(error.message);
});

$("#connect-browser").addEventListener("click", async () => {
  try {
    const base = JobDashboardConnection.dashboardBase($("#dashboard-base").value);
    if(base.startsWith("https:") && !await chrome.permissions.request({origins:[JobDashboardConnection.permissionOrigin(base)]})) throw new Error("Dashboard access was denied.");
    await send({type:"trackingConnect", dashboard_base:base, pairing_code:$("#pairing-code").value.trim()});
    $("#pairing-code").value="";
    const tabs=await chrome.tabs.query({});
    for(const tab of tabs) chrome.tabs.sendMessage(tab.id,{type:"trackingRefresh"}).catch(()=>{});
    await initialize();
    show("Browser connected. No per-job pairing or submission button needed.",true);
  } catch(error) { show(error.message); }
});
$("#disconnect-browser").addEventListener("click", async()=>{
  await send({type:"trackingDisconnect"}); await initialize();
});
setInterval(()=>{if(browserConnected) initialize().catch(()=>{});},3000);
