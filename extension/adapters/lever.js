(function (root) {
  "use strict";
  const common = root.JobAutofillCommon || (typeof require === "function" ? require("./common.js") : null);
  const HOSTS = new Set(["jobs.lever.co", "jobs.eu.lever.co"]);
  function supports(hostname) { return HOSTS.has(String(hostname || "").toLowerCase()); }
  function override(element) {
    const name = common.normalize(element.getAttribute ? element.getAttribute("name") : "");
    if (name === "name") return "full_name";
    if (name === "email") return "email";
    if (name === "phone") return "phone";
    if (name === "org" || name === "organization") return "work_employer";
    if (/linkedin/.test(name)) return "linkedin_url";
    if (/portfolio|website/.test(name)) return "portfolio_url";
    return null;
  }
  function describe(documentNode) { return common.describe(documentNode, "lever", override); }
  const api = { supports, describe };
  root.JobAutofillLever = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
