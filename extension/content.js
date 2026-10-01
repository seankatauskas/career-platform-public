(function () {
  "use strict";

  let activeDescription = null;

  function adapter() {
    const hostname = location.hostname.toLowerCase();
    return [JobAutofillGreenhouse, JobAutofillAshby, JobAutofillLever].find(
      (candidate) => candidate.supports(hostname)
    ) || null;
  }

  function describe() {
    const selected = adapter();
    if (!selected) return { supported: false, ats: null, fields: [], page_url: location.href };
    activeDescription = selected.describe(document);
    return {
      supported: true,
      ats: activeDescription.ats,
      fields: activeDescription.descriptors,
      page_url: location.href
    };
  }

  function capture() {
    const current = describe();
    return {
      ...current,
      answers: activeDescription ? JobAutofillCommon.snapshot(activeDescription) : []
    };
  }

  chrome.runtime.onMessage.addListener((message, _sender, respond) => {
    if (!message || message.type === undefined) return false;
    if (message.type === "inspectResumeUpload" || message.type === "attachResume") {
      if (!adapter() || location.href !== message.page_url) {
        respond({status: "manual", message: "Application changed. Reopen Autofill."});
        return false;
      }
      if (message.type === "inspectResumeUpload") {
        respond(JobResumeUpload.inspect(document));
        return false;
      }
      JobResumeUpload.attach(document, message.resume).then(respond)
        .catch(error => respond({status: "manual", message: error.message}));
      return true;
    }
    if (message.type === "describeForm") {
      respond(describe());
      return false;
    }
    if (message.type === "captureForm") {
      respond(capture());
      return false;
    }
    if (message.type === "applyAssignments") {
      if (!activeDescription) describe();
      const filled = activeDescription
        ? JobAutofillCommon.applyAssignments(activeDescription, message.assignments || [])
        : 0;
      respond({ ok: true, filled });
      return false;
    }
    return false;
  });

  // Observe a user-originated submission only long enough to preserve the final
  // eligible values. This listener never prevents, invokes, or retries submission.
  document.addEventListener("submit", () => {
    const pending = chrome.runtime.sendMessage({ type: "stageCaptureFromPage", form: capture() });
    if (pending && typeof pending.catch === "function") pending.catch(() => {});
  }, true);
})();
