(function (root) {
  "use strict";

  const DEFAULT_DASHBOARD = "http://127.0.0.1:8766";
  const label = "[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?";
  const privateOrigin = new RegExp(`^https://${label}\\.${label}\\.ts\\.net(?::443)?/?$`);
  const localOrigin = /^http:\/\/(?:127\.0\.0\.1|localhost):[0-9]+\/?$/;

  function dashboardBase(value) {
    const raw = String(value || DEFAULT_DASHBOARD).trim();
    // Validate the original spelling before URL normalizes credentials, paths,
    // case, escape sequences, or alternate representations of loopback hosts.
    if (!localOrigin.test(raw) && !privateOrigin.test(raw)) {
      throw new Error("Use a loopback HTTP URL with a port, or your exact HTTPS machine.tailnet.ts.net dashboard address.");
    }
    let parsed;
    try { parsed = new URL(raw); } catch (_error) {
      throw new Error("The dashboard address is invalid.");
    }
    if (parsed.protocol === "http:" && (!parsed.port || Number(parsed.port) === 0)) {
      throw new Error("The local dashboard address needs a valid nonzero port.");
    }
    return parsed.origin;
  }

  function permissionOrigin(base) { return `${dashboardBase(base)}/*`; }

  const api = { DEFAULT_DASHBOARD, dashboardBase, permissionOrigin };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.JobDashboardConnection = api;
})(globalThis);
