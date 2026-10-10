"""Current processing issues and exact human recovery, separate from attempt history."""
import json

from job_search.commands import DomainError


FAILURE_CODES = frozenset({"context_budget_exceeded", "invalid_json", "evidence_mismatch",
    "output_truncated", "output_too_large", "invalid_output", "provider_failed",
    "incomplete_coverage", "source_unavailable", "projection_failed"})
SCHEMA = """
CREATE TABLE IF NOT EXISTS understand_processing_issues (
 issue_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, revision TEXT NOT NULL,
 sha256 TEXT NOT NULL, analysis_id TEXT NOT NULL REFERENCES understand_analyses(id),
 version INTEGER NOT NULL, status TEXT NOT NULL, failure_code TEXT,
 resolution_reason TEXT, updated_at TEXT NOT NULL,
 UNIQUE(source_id,revision)
);
"""
# Exact prior schemas have no current-issue record. Preserve every old attempt and
# derive the initial current record without declaring pending projection complete.
BACKFILL = """WITH sources AS (
 SELECT a.rowid AS attempt_order,a.*,json_extract(s.value,'$.source_id') AS source_id,
 json_extract(s.value,'$.revision') AS revision,json_extract(s.value,'$.sha256') AS sha256
 FROM understand_analyses a,json_each(a.descriptor,'$.sources') s
 WHERE COALESCE(json_extract(s.value,'$.direction'),'incoming')='incoming'
 AND COALESCE(json_extract(s.value,'$.role'),'authored')='authored'
), latest AS (
 SELECT *,ROW_NUMBER() OVER (PARTITION BY source_id,revision ORDER BY attempt_order DESC) AS rank FROM sources
)
INSERT INTO understand_processing_issues
SELECT lower(hex(source_id))||'.'||lower(hex(revision)),source_id,revision,sha256,id,1,
 CASE WHEN status='succeeded' AND (json_extract(descriptor,'$.coverage.complete')=1 OR json_extract(output,'$.relevance')='unrelated')
 AND COALESCE(json_extract(output,'$.relevance'),'')!='uncertain'
 AND NOT EXISTS(SELECT 1 FROM command_work w WHERE w.owner='understanding' AND w.kind='project_analysis'
 AND w.dedupe_key=latest.id AND w.status!='done') THEN 'succeeded' ELSE 'open' END,
 CASE WHEN status='failed' THEN failure_code
 WHEN EXISTS(SELECT 1 FROM command_work w WHERE w.owner='understanding' AND w.kind='project_analysis'
 AND w.dedupe_key=latest.id AND w.status!='done') THEN 'projection_pending'
 WHEN json_extract(output,'$.relevance')='uncertain' THEN 'relevance_uncertain'
 WHEN json_extract(descriptor,'$.coverage.complete')!=1 AND COALESCE(json_extract(output,'$.relevance'),'')!='unrelated' THEN 'incomplete_coverage'
 ELSE NULL END,
 NULL,recorded_at FROM latest WHERE rank=1"""


def identity(source_id, revision):
    return source_id.encode().hex() + "." + revision.encode().hex()


def get(connection, issue_id):
    row = connection.execute("SELECT * FROM understand_processing_issues WHERE issue_id=?", (issue_id,)).fetchone()
    if row is None:
        raise DomainError("not_found", "Processing issue not found")
    result = dict(row)
    analysis = connection.execute("SELECT descriptor,model_version,recorded_at FROM understand_analyses WHERE id=?", (result["analysis_id"],)).fetchone()
    descriptor = json.loads(analysis["descriptor"])
    attempts = connection.execute("""SELECT COUNT(*) FROM understand_analyses a WHERE EXISTS (
        SELECT 1 FROM json_each(a.descriptor,'$.sources') s WHERE json_extract(s.value,'$.source_id')=?
        AND json_extract(s.value,'$.revision')=?)""", (result["source_id"], result["revision"])).fetchone()[0]
    return {**result, "id": issue_id, "attempt_count": attempts,
            "sources": [{"source_id": result["source_id"], "revision": result["revision"], "sha256": result["sha256"], "owner": "correspondence"}],
            "candidate_ids": [item["id"] for item in descriptor["candidates"]],
            "coverage": descriptor["coverage"], "model_version": analysis["model_version"], "recorded_at": analysis["recorded_at"]}


def for_source(connection, source_id, revision):
    row = connection.execute("SELECT issue_id FROM understand_processing_issues WHERE source_id=? AND revision=?", (source_id, revision)).fetchone()
    return get(connection, row[0]) if row else None


def page(connection, *, application_id=None, limit=25, after=None):
    if type(limit) is not int or not 1 <= limit <= 100 or after is not None and not isinstance(after, str):
        raise DomainError("invalid_input", "Invalid processing issue page")
    rows = connection.execute("""SELECT i.issue_id FROM understand_processing_issues i
        JOIN understand_analyses a ON a.id=i.analysis_id
        WHERE i.issue_id>? AND i.status IN ('open','retry_queued') AND (? IS NULL OR EXISTS
        (SELECT 1 FROM json_each(a.descriptor,'$.candidates') c WHERE json_extract(c.value,'$.id')=?))
        ORDER BY i.issue_id LIMIT ?""", (after or "", application_id, application_id, limit + 1)).fetchall()
    items = [get(connection, row[0]) for row in rows[:limit]]
    return {"items": items, "next_cursor": items[-1]["issue_id"] if len(rows) > limit else None, "truncated": len(rows) > limit}


def retry_applicable(connection, payload):
    try:
        issue = get(connection, payload["issue_id"])
    except DomainError as exc:
        if exc.code == "not_found": return False
        raise
    return (issue["status"] == "retry_queued" and issue["version"] == payload["expected_version"]
            and issue["analysis_id"] == payload["analysis_id"] and issue["source_id"] == payload["message_id"]
            and issue["revision"] == payload["revision"] and issue["sha256"] == payload["sha256"])


def check_retry(connection, retry):
    if retry is not None and not retry_applicable(connection, retry):
        raise DomainError("version_conflict", "Processing retry was resolved or replaced")


def record_attempt(tx, analysis):
    with tx.scope("understanding"):
        for source in analysis["descriptor"]["sources"]:
            if source.get("direction", "incoming") != "incoming" or source.get("role", "authored") != "authored":
                continue
            issue_id = identity(source["source_id"], source["revision"])
            before = for_source(tx.connection, source["source_id"], source["revision"])
            # A delayed ordinary worker may preserve its attempt, but cannot undo
            # an explicit human resolution or schedule a business projection.
            if before and before["status"] in {"resolved_manually", "superseded"}:
                continue
            tx.connection.execute("""INSERT INTO understand_processing_issues VALUES(?,?,?,?,?,?,?, ?,NULL,?)
                ON CONFLICT(issue_id) DO UPDATE SET sha256=excluded.sha256,analysis_id=excluded.analysis_id,
                version=excluded.version,status=excluded.status,failure_code=excluded.failure_code,
                resolution_reason=NULL,updated_at=excluded.updated_at""",
                (issue_id, source["source_id"], source["revision"], source["sha256"], analysis["id"],
                 before["version"] + 1 if before else 1, "open",
                 analysis["failure_code"] if analysis["status"] == "failed" else "projection_pending", tx.now))
            tx.record("understanding", issue_id, "record_processing_attempt", before, get(tx.connection, issue_id))


def projection_applicable(connection, analysis):
    for source in analysis["descriptor"]["sources"]:
        if source.get("direction", "incoming") != "incoming" or source.get("role", "authored") != "authored":
            continue
        issue = for_source(connection, source["source_id"], source["revision"])
        if issue and (issue["analysis_id"] != analysis["id"] or issue["status"] in {"resolved_manually", "superseded"}):
            return False
    return True


def record_projection(tx, analysis, *, status, failure_code=None):
    if status not in {"succeeded", "failed"} or status == "failed" and failure_code not in FAILURE_CODES:
        raise DomainError("invalid_input", "Invalid processing projection outcome")
    if not projection_applicable(tx.connection, analysis):
        return {"status": "superseded", "analysis_id": analysis["id"]}
    # An unrelated conclusion creates no business inference, including no claim
    # that a missing attachment contains no request. Preserve incomplete coverage.
    unrelated = analysis["output"] and analysis["output"].get("relevance") == "unrelated"
    complete = analysis["descriptor"]["coverage"].get("complete") is True
    unresolved = analysis["output"] and analysis["output"].get("relevance") == "uncertain" and not tx.connection.execute(
        "SELECT 1 FROM understand_proposals WHERE analysis_id=? AND status IN ('pending','applied') AND json_array_length(blockers)=0 LIMIT 1", (analysis["id"],)).fetchone()
    state = "succeeded" if status == "succeeded" and (complete or unrelated) and not unresolved else "open"
    code = failure_code if status == "failed" else "relevance_uncertain" if unresolved else None if state == "succeeded" else "incomplete_coverage"
    with tx.scope("understanding"):
        for source in analysis["descriptor"]["sources"]:
            issue = for_source(tx.connection, source["source_id"], source["revision"])
            if not issue or issue["analysis_id"] != analysis["id"]:
                continue
            if issue["status"] == state and issue["failure_code"] == code:
                continue
            tx.connection.execute("UPDATE understand_processing_issues SET status=?,failure_code=?,version=version+1,updated_at=? WHERE issue_id=?",
                                  (state, code, tx.now, issue["issue_id"]))
            tx.record("understanding", issue["issue_id"], "record_processing_projection", issue, get(tx.connection, issue["issue_id"]))
    return {"status": state, "analysis_id": analysis["id"]}


def decide(tx, *, issue_id, expected_version, expected_analysis_id, reason, retry=False):
    if tx.context.principal.kind != "human" or tx.context.origin == "inferred":
        raise DomainError("not_authorized", "Processing recovery requires human review")
    if type(expected_version) is not int or not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 2000:
        raise DomainError("invalid_input", "A current version and explicit resolution or retry reason are required")
    with tx.scope("understanding"):
        before = get(tx.connection, issue_id)
        if before["version"] != expected_version or before["analysis_id"] != expected_analysis_id:
            raise DomainError("version_conflict", "The current processing attempt changed")
        if before["status"] not in ({"open"} if retry else {"open", "retry_queued"}):
            raise DomainError("version_conflict", "Processing issue no longer needs this decision")
        tx.connection.execute("UPDATE understand_processing_issues SET status=?,version=version+1,resolution_reason=?,updated_at=? WHERE issue_id=?",
            ("retry_queued" if retry else "resolved_manually", reason, tx.now, issue_id))
        result = get(tx.connection, issue_id)
        if retry:
            payload = {"issue_id": issue_id, "expected_version": result["version"], "analysis_id": result["analysis_id"],
                       "message_id": result["source_id"], "revision": result["revision"], "sha256": result["sha256"]}
            tx.enqueue("understanding", "retry_processing", issue_id + ":" + str(result["version"]), payload)
        tx.record("understanding", issue_id, "retry_processing" if retry else "resolve_processing", before, result)
        return result


def supersede_source(tx, *, source_id, latest_revision):
    with tx.scope("understanding"):
        rows = tx.connection.execute("SELECT issue_id FROM understand_processing_issues WHERE source_id=? AND revision!=? AND status!='superseded'", (source_id, latest_revision)).fetchall()
        for row in rows:
            before = get(tx.connection, row[0])
            tx.connection.execute("UPDATE understand_processing_issues SET status='superseded',version=version+1,updated_at=? WHERE issue_id=?", (tx.now, row[0]))
            tx.record("understanding", row[0], "supersede_processing_revision", before, get(tx.connection, row[0]))
        return {"superseded": len(rows)}
