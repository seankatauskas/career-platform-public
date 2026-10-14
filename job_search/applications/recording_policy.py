"""Owner-authorized recording of exact paired-browser facts; no mail authority."""
from job_search.commands import DomainError


VERSION = "paired-browser-v1"
SCHEMA = """
CREATE TABLE IF NOT EXISTS app_recording_policy (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL,
 enabled INTEGER NOT NULL, policy_version TEXT NOT NULL, updated_at TEXT NOT NULL);
"""


def get(connection):
    row = connection.execute("SELECT * FROM app_recording_policy WHERE singleton=1").fetchone()
    return {"version": row["version"] if row else 0, "enabled": bool(row["enabled"]) and row["policy_version"] == VERSION if row else False,
            "policy_version": VERSION}


def configure(tx, *, enabled, expected_version):
    if tx.context.principal.kind != "human" or tx.context.origin == "inferred":
        raise DomainError("not_authorized", "Only the owner can configure browser recording")
    if type(enabled) is not bool or type(expected_version) is not int:
        raise DomainError("invalid_input", "An explicit policy decision and version are required")
    before = get(tx.connection)
    if before["version"] != expected_version:
        raise DomainError("version_conflict", "Browser recording preference changed")
    with tx.scope("applications"):
        tx.connection.execute("INSERT INTO app_recording_policy VALUES(1,?,?,?,?) ON CONFLICT(singleton) DO UPDATE SET version=excluded.version,enabled=excluded.enabled,policy_version=excluded.policy_version,updated_at=excluded.updated_at",
                              (expected_version + 1, int(enabled), VERSION, tx.now))
        after = get(tx.connection)
        tx.record("applications", "browser-recording", "configure_browser_recording", before, after)
    return after


def eligibility(applications, connection, proposal):
    if proposal["status"] != "pending" or proposal["blockers"] or proposal["dependencies"]:
        return "proposal_needs_review"
    if proposal["operation"] not in {"record_submission", "confirm_submission", "attach_submission_answers"}:
        return "unsupported_browser_operation"
    app = applications.get_application(connection, proposal["application_id"])
    if app["disposition"] != "open":
        return "application_closed"
    evidence = proposal["evidence"]
    if not evidence or any(e.get("owner") != "applications" for e in evidence):
        return "not_browser_evidence"
    pairs = {(e.get("device_id"), e.get("attempt_ref")) for e in evidence}
    if len(pairs) != 1 or not all(next(iter(pairs))):
        return "ambiguous_browser_attempt"
    device, attempt_ref = next(iter(pairs))
    attempt = applications.get_browser_attempt(connection, device, attempt_ref, limit=200)
    if not attempt or attempt["coverage"].get("truncated"):
        return "incomplete_browser_evidence"
    observations = {o["id"]: o for o in attempt["observations"]}
    for ref in evidence:
        o = observations.get(ref.get("source_id"))
        if not o or str(o["version"]) != str(ref.get("revision")) or o["source_digest"] != ref.get("sha256"):
            return "browser_evidence_changed"
        if o["application_id"] != app["id"] or o["pursuit_no"] != app["pursuit_no"]:
            return "historical_browser_attempt"
        if o.get("source", {}).get("adapter") != "paired_extension_v1":
            return "unpaired_browser_evidence"
    if proposal["operation"] == "confirm_submission" and not any(
            o["activity"] == "website_acknowledgment" and o.get("source", {}).get("metadata", {}).get("signal") in {"success_dom", "success_route"}
            for o in observations.values()):
        return "missing_website_acknowledgment"
    return None
