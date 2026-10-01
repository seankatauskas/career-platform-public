(function (root) {
  "use strict";
  const common = root.JobAutofillCommon || (typeof require === "function" ? require("./common.js") : null);
  function supports(hostname) { return String(hostname || "").toLowerCase() === "jobs.ashbyhq.com"; }
  function override(element) {
    const name = common.normalize(element.getAttribute ? `${element.getAttribute("name") || ""} ${element.getAttribute("autocomplete") || ""}` : "");
    if (/\bfirst ?name\b/.test(name) || /\bgiven name\b/.test(name)) return "first_name";
    if (/\blast ?name\b/.test(name) || /\bfamily name\b/.test(name)) return "last_name";
    if (/\bemail\b/.test(name)) return "email";
    if (/\bphone\b/.test(name) || /\btel\b/.test(name)) return "phone";
    return null;
  }
  function describe(documentNode) { return common.describe(documentNode, "ashby", override); }
  const api = { supports, describe };
  root.JobAutofillAshby = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
