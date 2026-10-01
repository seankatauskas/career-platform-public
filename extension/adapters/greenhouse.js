(function (root) {
  "use strict";
  const common = root.JobAutofillCommon || (typeof require === "function" ? require("./common.js") : null);
  const HOSTS = new Set(["boards.greenhouse.io", "job-boards.greenhouse.io", "job-boards.eu.greenhouse.io"]);
  function supports(hostname) { return HOSTS.has(String(hostname || "").toLowerCase()); }
  function override(element) {
    const name = common.normalize(element.getAttribute ? element.getAttribute("name") : "");
    const exact = {
      "job application first name": "first_name",
      "job application last name": "last_name",
      "job application email": "email",
      "job application phone": "phone"
    };
    return exact[name] || null;
  }
  function describe(documentNode) { return common.describe(documentNode, "greenhouse", override); }
  const api = { supports, describe };
  root.JobAutofillGreenhouse = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
