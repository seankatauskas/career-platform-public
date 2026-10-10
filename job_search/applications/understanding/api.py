"""Analysis receipts and proposed operations are this module's only write surface."""
from datetime import datetime, timedelta
import json
import hashlib
import uuid

from job_search.commands import DomainError, digest, encode
from .contracts import validate_analysis
from .recipes import fact_recipe
from . import processing


SCHEMA = """
CREATE TABLE IF NOT EXISTS understand_claims (
 input_key TEXT PRIMARY KEY, token TEXT NOT NULL, lease_until TEXT NOT NULL,
 generation INTEGER NOT NULL, status TEXT NOT NULL, analysis_id TEXT);
CREATE TABLE IF NOT EXISTS understand_analyses (
 id TEXT PRIMARY KEY, input_key TEXT NOT NULL, context_fingerprint TEXT NOT NULL,
 descriptor TEXT NOT NULL, schema_version TEXT NOT NULL, prompt_version TEXT NOT NULL,
 model_version TEXT NOT NULL, status TEXT NOT NULL, output TEXT, failure_code TEXT,
 recorded_at TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS understand_successful_input ON understand_analyses(input_key) WHERE status='succeeded';
CREATE TABLE IF NOT EXISTS understand_findings (
 id TEXT PRIMARY KEY, analysis_id TEXT NOT NULL REFERENCES understand_analyses(id),
 category TEXT NOT NULL, position INTEGER NOT NULL, content TEXT NOT NULL,
 lineage_key TEXT NOT NULL, comparison_key TEXT NOT NULL,
 UNIQUE(analysis_id,category,position));
CREATE TABLE IF NOT EXISTS understand_proposals (
 id TEXT PRIMARY KEY, version INTEGER NOT NULL, status TEXT NOT NULL,
 operation TEXT NOT NULL, input TEXT NOT NULL, application_id TEXT, evidence TEXT NOT NULL,
 expected_versions TEXT NOT NULL, dependencies TEXT NOT NULL, blockers TEXT NOT NULL,
 analysis_id TEXT, finding_id TEXT, lineage_key TEXT NOT NULL,
 recorded_at TEXT NOT NULL, reason TEXT,
 UNIQUE(lineage_key,operation));
"""

_PREVIOUS_SCHEMA = SCHEMA
SCHEMA += processing.SCHEMA
SCHEMA_MIGRATIONS = {("understanding", hashlib.sha256(_PREVIOUS_SCHEMA.encode()).hexdigest(),
                      hashlib.sha256(SCHEMA.encode()).hexdigest()): (processing.SCHEMA, processing.BACKFILL)}


OPERATIONS = frozenset({"link_message", "correct_association", "confirm_submission", "record_request",
    "create_task", "record_assessment", "update_assessment", "record_offer", "decide_offer",
    "schedule_interview", "reschedule_interview", "cancel_interview", "close_application",
    "complete_task", "cancel_task", "snooze_task", "correct_progress", "combine_jobs", "add_note", "record_progress",
    "record_submission", "attach_submission_answers", "save_job", "reopen_application", "create_reminder", "cancel_reminder"})


def _human(tx):
    if tx.context.principal.kind != "human" or tx.context.origin == "inferred":
        raise DomainError("not_authorized", "Proposal decisions require human review")


def _proposal(row):
    if row is None:
        raise DomainError("not_found", "Proposal not found")
    result = dict(row)
    for key in ("input", "evidence", "expected_versions", "dependencies", "blockers"):
        result[key] = json.loads(result[key])
    return result


class UnderstandingOperations:
    get_processing_issue = staticmethod(processing.get)
    processing_issues = staticmethod(processing.page)
    processing_for_source = staticmethod(processing.for_source)
    processing_retry_applicable = staticmethod(processing.retry_applicable)
    supersede_processing_source = staticmethod(processing.supersede_source)

    def processing_source_allowed(self, connection, message_id, revision):
        issue = processing.for_source(connection, message_id, revision)
        return issue is None or issue["status"] not in {"resolved_manually", "superseded"}

    def processing_projection_applicable(self, connection, analysis_id):
        return processing.projection_applicable(connection, self.get_analysis(connection, analysis_id))

    def record_projection(self, tx, *, analysis_id, status, failure_code=None):
        return processing.record_projection(tx, self.get_analysis(tx.connection, analysis_id),
                                           status=status, failure_code=failure_code)

    def retry_processing(self, tx, **value):
        return processing.decide(tx, **value, retry=True)

    def resolve_processing(self, tx, **value):
        return processing.decide(tx, **value)

    def record_source_failure(self, tx, *, source_refs, failure_code, candidate_ids=(), retry=None):
        """Persist missing evidence without fabricating plaintext or a valid analysis."""
        processing.check_retry(tx.connection, retry)
        if failure_code not in processing.FAILURE_CODES:
            raise DomainError("invalid_input", "Invalid source failure code")
        if not 1 <= len(source_refs) <= 50 or len(candidate_ids) > 20:
            raise DomainError("invalid_input", "Invalid failure context bounds")
        sources = []
        for ref in source_refs:
            if set(ref) != {"source_id", "revision", "sha256"} or any(not isinstance(v, str) or not v for v in ref.values()):
                raise DomainError("invalid_input", "Failure sources require exact identity, revision, and hash")
            if len(ref["sha256"]) != 64 or any(c not in "0123456789abcdef" for c in ref["sha256"]):
                raise DomainError("invalid_input", "Invalid source hash")
            sources.append(dict(ref))
        if any(not isinstance(identity, str) or not identity for identity in candidate_ids):
            raise DomainError("invalid_input", "Invalid failure candidate")
        descriptor = {"sources": sources, "candidates": [{"id": identity} for identity in candidate_ids],
                      "versions": {}, "coverage": {"complete": False, "reason": failure_code}, "context": {}}
        if retry is not None:
            if sources != [{"source_id":retry["message_id"], "revision":retry["revision"], "sha256":retry["sha256"]}]:
                raise DomainError("invalid_input", "Retry failure must identify its exact reviewed source")
            descriptor["context"]["processing_attempt"] = {"issue_id":retry["issue_id"],
                "version":retry["expected_version"], "analysis_id":retry["analysis_id"]}
        fingerprint = digest(descriptor)
        analysis_id = "analysis_" + uuid.uuid4().hex
        with tx.scope("understanding"):
            tx.connection.execute("INSERT INTO understand_analyses VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
                analysis_id, fingerprint, fingerprint, encode(descriptor), "1", "not_run", "not_run",
                "failed", None, failure_code, tx.now))
            tx.record("understanding", analysis_id, "record_source_failure", None,
                      {"id": analysis_id, "status": "failed", "failure_code": failure_code, "source_refs": sources})
        result = self.get_analysis(tx.connection, analysis_id)
        processing.record_attempt(tx, result)
        return result

    def coverage(self, connection, application_id=None, source_id=None, limit=25, after=None):
        """Analysis history is operational fact, never a new interpretation."""
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise DomainError("invalid_input", "Invalid coverage page size")
        if after is not None and (not isinstance(after, int) or isinstance(after, bool) or after < 1):
            raise DomainError("invalid_input", "Invalid coverage cursor")
        rows = connection.execute("SELECT rowid AS cursor,* FROM understand_analyses a WHERE (? IS NULL OR rowid<?) AND (? IS NULL OR EXISTS (SELECT 1 FROM json_each(json_extract(a.descriptor,'$.candidates')) c WHERE json_extract(c.value,'$.id')=?)) AND (? IS NULL OR EXISTS (SELECT 1 FROM json_each(json_extract(a.descriptor,'$.sources')) s WHERE json_extract(s.value,'$.source_id')=?)) ORDER BY rowid DESC LIMIT ?",
                                  (after, after, application_id, application_id, source_id, source_id, limit + 1)).fetchall()
        items = []
        for row in rows[:limit]:
            descriptor = json.loads(row["descriptor"])
            findings = connection.execute("SELECT id,category,content FROM understand_findings WHERE analysis_id=? ORDER BY category,position LIMIT 21", (row["id"],)).fetchall()
            items.append({"analysis_id": row["id"], "status": row["status"], "failure_code": row["failure_code"],
                "coverage": descriptor["coverage"], "sources": descriptor["sources"],
                "candidate_ids": [c["id"] for c in descriptor["candidates"]], "model_version": row["model_version"],
                "recorded_at": row["recorded_at"], "cursor": row["cursor"],
                "findings": [{"id": f["id"], "category": f["category"], "content": json.loads(f["content"])} for f in findings[:20]],
                "findings_truncated": len(findings) > 20})
        return {"items": items, "next_cursor": items[-1]["cursor"] if len(rows) > limit else None,
                "truncated": len(rows) > limit}

    def import_records(self, tx, *, records, source_snapshot):
        """Preserve native historical identities without projecting or executing."""
        if tx.context.origin != "migration" or tx.context.principal.kind != "worker":
            raise DomainError("not_authorized", "Historical import requires the migration worker")
        if not isinstance(source_snapshot, str) or not source_snapshot or len(source_snapshot) > 2000:
            raise DomainError("invalid_input", "Historical source snapshot is required")
        if set(records) - {"analyses", "findings", "proposals"}:
            raise DomainError("invalid_input", "Unsupported understanding import records")
        groups = {
            "analyses": (("id", "input_key", "context_fingerprint", "descriptor", "schema_version", "prompt_version", "model_version", "status", "output", "failure_code", "recorded_at"), {"descriptor", "output"}),
            "findings": (("id", "analysis_id", "category", "position", "content", "lineage_key", "comparison_key"), {"content"}),
            "proposals": (("id", "version", "status", "operation", "input", "application_id", "evidence", "expected_versions", "dependencies", "blockers", "analysis_id", "finding_id", "lineage_key", "recorded_at", "reason"), {"input", "evidence", "expected_versions", "dependencies", "blockers"})}
        counts = {}
        with tx.scope("understanding"):
            for group, (columns, json_fields) in groups.items():
                counts[group] = 0
                for item in records.get(group, ()):
                    item = dict(item)
                    if set(item) != set(columns):
                        raise DomainError("invalid_input", "Invalid historical understanding fields")
                    for name in json_fields:
                        if isinstance(item[name], str):
                            item[name] = json.loads(item[name])
                    if group == "proposals":
                        if item["operation"] not in OPERATIONS or item["status"] not in {"pending", "applied", "rejected", "stale", "superseded"}:
                            raise DomainError("invalid_input", "Invalid historical proposal")
                        if item["status"] in {"pending", "stale"}:
                            item["blockers"] = list(dict.fromkeys(item["blockers"] + ["migration_revalidation_required"]))
                    encoded = {k: encode(v) if k in json_fields and v is not None else v for k, v in item.items()}
                    tx.connection.execute("INSERT INTO understand_" + group + " (" + ",".join(columns) + ") VALUES (" + ",".join(":" + c for c in columns) + ")", encoded)
                    tx.record("understanding", item["id"], "import_snapshot", None,
                              {"source_snapshot": source_snapshot, "record_kind": group, "record_id": item["id"]})
                    counts[group] += 1
                    if group == "analyses":
                        processing.record_attempt(tx, self.get_analysis(tx.connection, item["id"]))
        return {"counts": counts, "source_snapshot": source_snapshot, "dispatch_created": False}

    def claim_analysis(self, tx, *, context, model_version, prompt_version="1", schema_version="1", lease_seconds=300):
        if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) or not 1 <= lease_seconds <= 900:
            raise DomainError("invalid_input", "Invalid analysis lease")
        key = digest({"input": context.fingerprint(), "model": model_version,
                      "prompt": prompt_version, "schema": schema_version})
        with tx.scope("understanding"):
            row = tx.connection.execute("SELECT * FROM understand_claims WHERE input_key=?", (key,)).fetchone()
            if row and row["status"] == "succeeded":
                return {"status": "persisted", "analysis_id": row["analysis_id"], "input_key": key}
            if row and row["status"] == "processing" and datetime.fromisoformat(row["lease_until"].replace("Z", "+00:00")) > datetime.fromisoformat(tx.now.replace("Z", "+00:00")):
                return {"status": "busy", "input_key": key}
            token = "claim_" + uuid.uuid4().hex
            until = (datetime.fromisoformat(tx.now.replace("Z", "+00:00")) + timedelta(seconds=lease_seconds)).isoformat().replace("+00:00", "Z")
            generation = row["generation"] + 1 if row else 1
            tx.connection.execute("INSERT INTO understand_claims VALUES(?,?,?,?,?,NULL) ON CONFLICT(input_key) DO UPDATE SET token=excluded.token,lease_until=excluded.lease_until,generation=excluded.generation,status=excluded.status,analysis_id=NULL",
                                  (key, token, until, generation, "processing"))
            return {"status": "claimed", "input_key": key, "token": token,
                    "generation": generation, "lease_until": until}

    def renew_analysis(self, tx, *, claim, lease_seconds=300):
        """Extend only the live claim held by this exact inference attempt."""
        if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) or not 1 <= lease_seconds <= 900:
            raise DomainError("invalid_input", "Invalid analysis lease")
        with tx.scope("understanding"):
            row = tx.connection.execute("SELECT * FROM understand_claims WHERE input_key=?", (claim.get("input_key"),)).fetchone()
            now = datetime.fromisoformat(tx.now.replace("Z", "+00:00"))
            if (not row or row["status"] != "processing" or row["token"] != claim.get("token")
                    or row["generation"] != claim.get("generation")
                    or datetime.fromisoformat(row["lease_until"].replace("Z", "+00:00")) <= now):
                raise DomainError("version_conflict", "Analysis lease is no longer owned")
            until = (now + timedelta(seconds=lease_seconds)).isoformat().replace("+00:00", "Z")
            tx.connection.execute("UPDATE understand_claims SET lease_until=? WHERE input_key=?", (until, row["input_key"]))
            return {"renewed": True, "lease_until": until}

    def defer_analysis(self, tx, *, claim, reason_code, retry_at):
        """Release an exact inference lease without recording a model outcome."""
        if reason_code not in {"inference_daily_request_limit", "inference_daily_token_limit",
                               "inference_inflight_limit", "inference_polling_pending"}:
            raise DomainError("invalid_input", "Unsupported inference deferral")
        try:
            if not isinstance(retry_at,str) or not retry_at.endswith("Z"):
                raise ValueError()
            datetime.fromisoformat(retry_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise DomainError("invalid_input", "Invalid inference retry time") from exc
        with tx.scope("understanding"):
            row = tx.connection.execute("SELECT * FROM understand_claims WHERE input_key=?", (claim.get("input_key"),)).fetchone()
            if (not row or row["status"] != "processing" or row["token"] != claim.get("token")
                    or row["generation"] != claim.get("generation")
                    or datetime.fromisoformat(row["lease_until"].replace("Z", "+00:00")) <= datetime.fromisoformat(tx.now.replace("Z", "+00:00"))):
                raise DomainError("version_conflict", "Analysis lease is no longer owned")
            tx.connection.execute("UPDATE understand_claims SET status='deferred',lease_until=? WHERE input_key=?",
                                  (tx.now,row["input_key"]))
            tx.record("understanding",row["input_key"],"defer_analysis",
                      {"status":"processing","generation":row["generation"]},
                      {"status":"deferred","generation":row["generation"],"reason_code":reason_code,"retry_at":retry_at})
            return {"status":"deferred","retry_at":retry_at}

    def store_analysis(self, tx, *, context, model_version, output=None, failure_code=None,
                       prompt_version="1", schema_version="1", claim=None, retry=None):
        processing.check_retry(tx.connection, retry)
        key = digest({"input": context.fingerprint(), "model": model_version,
                      "prompt": prompt_version, "schema": schema_version})
        if (output is None) == (failure_code is None):
            raise DomainError("invalid_input", "Store one validated result or one failure")
        validated = validate_analysis(output, context) if output is not None else None
        if failure_code is not None and failure_code not in processing.FAILURE_CODES:
            raise DomainError("invalid_input", "Unsupported analysis failure code")
        with tx.scope("understanding"):
            prior = tx.connection.execute("SELECT id FROM understand_analyses WHERE input_key=? AND status='succeeded'", (key,)).fetchone()
            if prior:
                return self.get_analysis(tx.connection, prior["id"])
            held = tx.connection.execute("SELECT * FROM understand_claims WHERE input_key=?", (key,)).fetchone()
            if held and (claim is None or claim.get("token") != held["token"] or
                         datetime.fromisoformat(held["lease_until"].replace("Z", "+00:00")) <= datetime.fromisoformat(tx.now.replace("Z", "+00:00"))):
                raise DomainError("version_conflict", "Analysis lease is no longer owned")
            if claim and not held:
                raise DomainError("version_conflict", "Analysis claim not found")
            analysis_id = "analysis_" + uuid.uuid4().hex
            status = "succeeded" if validated is not None else "failed"
            tx.connection.execute("INSERT INTO understand_analyses VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                  (analysis_id, key, context.fingerprint(), encode(context.descriptor()), schema_version,
                                   prompt_version, model_version, status, encode(validated) if validated else None, failure_code, tx.now))
            if validated:
                for category in ("associations", "facts", "requests", "temporal_facts"):
                    for position, content in enumerate(validated[category]):
                        finding_id = "finding_" + uuid.uuid4().hex
                        refs = sorted({(span["source_id"], span["revision"]) for span in content["evidence"]})
                        semantic = {k: v for k, v in content.items() if k not in {"evidence", "confidence", "actionable_source"}}
                        lineage = digest({"category": category, "sources": refs, "semantic": semantic})
                        comparison = digest({"category": category, "sources": refs, "kind": content["kind"], "target": content.get("target_id")})
                        tx.connection.execute("INSERT INTO understand_findings VALUES(?,?,?,?,?,?,?)",
                                              (finding_id, analysis_id, category, position, encode(content), lineage, comparison))
            if held:
                tx.connection.execute("UPDATE understand_claims SET status=?,analysis_id=? WHERE input_key=?", (status, analysis_id, key))
            tx.record("understanding", analysis_id, "record_analysis", None,
                      {"id": analysis_id, "status": status, "context_fingerprint": context.fingerprint(), "failure_code": failure_code})
            if validated:
                tx.enqueue("understanding", "project_analysis", analysis_id, {"analysis_id": analysis_id})
            result = self.get_analysis(tx.connection, analysis_id)
            processing.record_attempt(tx, result)
            return result

    def get_analysis(self, connection, analysis_id):
        row = connection.execute("SELECT * FROM understand_analyses WHERE id=?", (analysis_id,)).fetchone()
        if row is None:
            raise DomainError("not_found", "Analysis not found")
        result = dict(row)
        result["descriptor"] = json.loads(result["descriptor"])
        result["output"] = json.loads(result["output"]) if result["output"] else None
        findings = connection.execute("SELECT * FROM understand_findings WHERE analysis_id=? ORDER BY category,position", (analysis_id,)).fetchall()
        result["findings"] = [{**dict(f), "content": json.loads(f["content"])} for f in findings]
        return result

    def propose(self, tx, *, operation, input, application_id=None, evidence=(), expected_versions=None,
                dependencies=(), analysis_id=None, finding_id=None, lineage_key=None, blockers=()):
        if operation not in OPERATIONS or not isinstance(input, dict):
            raise DomainError("invalid_input", "Only named internal operations may be proposed")
        if len(evidence) > 20 or len(dependencies) > 20 or len(blockers) > 20 or len(encode(input)) > 32000:
            raise DomainError("invalid_input", "Proposal exceeds bounded limits")
        lineage = lineage_key or digest({"command": tx.context.idempotency_key, "actor": tx.context.principal.actor_id,
                                        "operation": operation, "input": input, "target": application_id})
        with tx.scope("understanding"):
            old = tx.connection.execute("SELECT * FROM understand_proposals WHERE lineage_key=? AND operation=?", (lineage, operation)).fetchone()
            if old:
                return _proposal(old)
            if finding_id:
                finding = tx.connection.execute("SELECT * FROM understand_findings WHERE id=? AND analysis_id=?", (finding_id, analysis_id)).fetchone()
                if finding is None:
                    raise DomainError("invalid_input", "Proposal finding is not in this analysis")
            result = {"id": "proposal_" + uuid.uuid4().hex, "version": 1, "status": "pending",
                      "operation": operation, "input": input, "application_id": application_id,
                      "evidence": list(evidence), "expected_versions": dict(expected_versions or {}),
                      "dependencies": list(dependencies), "blockers": list(blockers), "analysis_id": analysis_id,
                      "finding_id": finding_id, "lineage_key": lineage, "recorded_at": tx.now, "reason": None}
            stored = {**result, **{k: encode(result[k]) for k in ("input", "evidence", "expected_versions", "dependencies", "blockers")}}
            tx.connection.execute("INSERT INTO understand_proposals VALUES(:id,:version,:status,:operation,:input,:application_id,:evidence,:expected_versions,:dependencies,:blockers,:analysis_id,:finding_id,:lineage_key,:recorded_at,:reason)", stored)
            tx.record("understanding", result["id"], "propose_changes", None, result)
            return result

    def project_analysis(self, tx, *, analysis_id, projections):
        analysis = self.get_analysis(tx.connection, analysis_id)
        if analysis["status"] != "succeeded":
            raise DomainError("invalid_input", "Failed analysis cannot project proposals")
        if not isinstance(projections, (list, tuple)) or len(projections) > 80:
            raise DomainError("invalid_input", "Projection exceeds bounded limits")
        findings = {f["id"]: f for f in analysis["findings"]}
        result = []
        for projection in projections:
            if set(projection) - {"finding_id", "operation", "input", "application_id", "expected_versions", "dependencies"}:
                raise DomainError("invalid_input", "Invalid projection fields")
            finding = findings.get(projection.get("finding_id"))
            if finding is None:
                raise DomainError("invalid_input", "Projection references missing finding")
            content = finding["content"]
            if finding["category"] == "requests" and (
                    not content["actionable_source"] or content["requirement"] != "required" or content["responsible_party"] != "applicant"):
                continue
            blockers = []
            if not analysis["descriptor"]["coverage"]["complete"]:
                blockers.append("incomplete_evidence_coverage")
            with tx.scope("understanding"):
                # Same source/kind with changed semantics must be compared, even when
                # the model rewrites wording or offsets. New authored mail has new refs.
                prior = tx.connection.execute("SELECT p.id FROM understand_findings f JOIN understand_proposals p ON p.finding_id=f.id WHERE f.comparison_key=? AND f.lineage_key<>? LIMIT 1",
                                              (finding["comparison_key"], finding["lineage_key"])).fetchone()
                if prior:
                    blockers.append("compare_prior:" + prior["id"])
            lineage = digest({"finding": finding["lineage_key"], "target": projection.get("application_id"),
                              "operation": projection["operation"]})
            result.append(self.propose(tx, **projection, analysis_id=analysis_id,
                evidence=content["evidence"], lineage_key=lineage, blockers=blockers))
        return {"analysis_id": analysis_id, "proposals": result}

    def get(self, connection, proposal_id):
        return _proposal(connection.execute("SELECT * FROM understand_proposals WHERE id=?", (proposal_id,)).fetchone())

    def project_browser(self, tx, *, observation, submissions=(), attempt=None):
        """One pending submission decision per exact browser attempt.

        Captures remain immutable owner evidence. Attaching one is a separate
        reviewed operation after a concrete submission exists.
        """
        activity = observation.get("activity")
        if activity not in {"submission_attempt", "website_acknowledgment", "answer_capture"}:
            return {"proposals": []}
        required = {"id", "application_id", "device_id", "attempt_ref", "source_digest"}
        def validate(item):
            if not isinstance(item, dict) or not required <= set(item) or any(not item[k] for k in required):
                raise DomainError("invalid_input", "Browser observation identity is incomplete")
            if any(item[k] != observation[k] for k in ("application_id", "device_id", "attempt_ref")):
                raise DomainError("invalid_input", "Browser attempt context identifies another target")
        validate(observation)
        def ref(item):
            return {"source_id": item["id"], "owner": "applications",
                    "revision": str(item.get("version", 1)), "sha256": item["source_digest"],
                    "device_id": item["device_id"], "attempt_ref": item["attempt_ref"]}
        incomplete = False
        if isinstance(submissions, dict):
            incomplete = bool(submissions.get("truncated") or submissions.get("next_cursor") or
                              not submissions.get("coverage", {}).get("complete", True))
            submissions = submissions["items"]
        matches = [s for s in submissions if s.get("application_id", observation["application_id"]) == observation["application_id"]
                   and s.get("status") != "retracted" and any(
                       e.get("device_id") == observation["device_id"] and e.get("attempt_ref") == observation["attempt_ref"]
                       for e in s.get("evidence", ()))]
        blockers = (["ambiguous_submission_attempt"] if len(matches) > 1 else []) + (["incomplete_submission_coverage"] if incomplete else [])
        existing = matches[0] if len(matches) == 1 else None
        # Query only our owner records. Public owner DTOs supply accepted state.
        rows = tx.connection.execute("SELECT * FROM understand_proposals WHERE application_id=? AND operation IN ('record_submission','confirm_submission','attach_submission_answers') AND EXISTS (SELECT 1 FROM json_each(evidence) e WHERE json_extract(e.value,'$.device_id')=? AND json_extract(e.value,'$.attempt_ref')=?) ORDER BY rowid",
            (observation["application_id"], observation["device_id"], observation["attempt_ref"])).fetchall()
        prior = [_proposal(row) for row in rows]
        def supersede(items, replacement):
            with tx.scope("understanding"):
                for before in items:
                    if before["id"] == replacement["id"] or before["status"] != "pending":
                        continue
                    tx.connection.execute("UPDATE understand_proposals SET version=version+1,status='superseded',reason=? WHERE id=?",
                        ("Browser evidence replaced by " + replacement["id"], before["id"]))
                    tx.record("understanding", before["id"], "supersede_browser_proposal", before, self.get(tx.connection, before["id"]))
        if activity == "answer_capture":
            if "answer_snapshot" not in observation.get("source", {}):
                return {"proposals": [], "status": "no_answer_snapshot"}
            if existing is None or incomplete:
                return {"proposals": [], "status": "awaiting_submission_review", "blockers": blockers}
            if any(item.get("observation_id") == observation["id"] for item in existing.get("answer_snapshots", ())):
                return {"proposals": [], "status": "already_attached"}
            previous = [p for p in prior if p["operation"] == "attach_submission_answers" and p["input"].get("observation_id") == observation["id"]]
            decided = [p for p in previous if p["status"] in {"applied", "rejected"}]
            if decided:
                return {"proposals": [decided[-1]]}
            payload = {"submission_id": existing["id"], "expected_version": existing["version"], "observation_id": observation["id"]}
            proposal = self.propose(tx, operation="attach_submission_answers", input=payload,
                application_id=observation["application_id"], evidence=[ref(observation)],
                expected_versions={"submission:" + existing["id"]: existing["version"]},
                lineage_key=digest({"browser_capture": observation["id"], "target": payload}))
            supersede(previous, proposal)
            return {"proposals": [proposal]}
        observations = [observation]
        if attempt is not None:
            if any(attempt.get(k) != observation[k] for k in ("application_id", "device_id", "attempt_ref")):
                raise DomainError("invalid_input", "Browser attempt context identifies another target")
            observations = list(attempt.get("observations", ()))
            for item in observations:
                validate(item)
            if not any(item["id"] == observation["id"] for item in observations):
                observations.append(observation)
            if attempt.get("coverage", {}).get("truncated") or not attempt.get("coverage", {}).get("complete", True):
                blockers.append("incomplete_browser_evidence")
        acknowledgments = [item for item in observations if item.get("activity") == "website_acknowledgment"]
        selected = acknowledgments[-1] if acknowledgments else observation
        pending = [p for p in prior if p["status"] == "pending" and p["operation"] != "attach_submission_answers"]
        if selected["activity"] == "submission_attempt":
            confirmed = [p for p in pending if p["operation"] == "confirm_submission"]
            if confirmed:
                return {"proposals": [confirmed[-1]]}
            if existing and existing.get("status") == "confirmed":
                return {"proposals": [], "status": "already_confirmed"}
        operation = "confirm_submission" if selected["activity"] == "website_acknowledgment" else "record_submission"
        # A replay of a reviewed/rejected source never creates a fresh decision.
        decided = [p for p in prior if p["operation"] == operation and p["status"] in {"applied", "rejected"}
                   and any(e.get("source_id") == selected["id"] for e in p["evidence"])]
        if decided:
            return {"proposals": [decided[-1]]}
        evidence = []
        for item in observations:
            if item.get("activity") in {"submission_attempt", "website_acknowledgment"}:
                item_ref = ref(item)
                if item_ref not in evidence:
                    evidence.append(item_ref)
        for previous in pending:
            for item_ref in previous["evidence"]:
                if item_ref not in evidence:
                    evidence.append(item_ref)
        if len(evidence) > 20:
            evidence = [ref(selected)] + [e for e in evidence[-19:] if e != ref(selected)]
            blockers.append("incomplete_browser_evidence")
        evidence.sort(key=lambda item: (item.get("source_id", ""), item.get("revision", "")))
        payload = {"application_id": selected["application_id"],
            "status": "confirmed" if operation == "confirm_submission" else "attempted",
            "occurred_at": selected.get("occurred_at"), "answers": selected.get("answers", {}),
            "documents": selected.get("documents", []), "evidence": evidence}
        for field in ("answers","documents"):
            snapshots={encode(item.get(field)):item[field] for item in observations
                       if item.get("activity") in {"submission_attempt","website_acknowledgment"} and item.get(field)}
            if existing and field in existing:
                payload[field]=existing[field]
                if any(value!=payload[field] for value in snapshots.values()):
                    blockers.append("conflicting_submission_"+field)
            elif len(snapshots)==1:
                payload[field]=next(iter(snapshots.values()))
            elif len(snapshots)>1:
                blockers.append("conflicting_submission_"+field)
        expected = {}
        if existing:
            payload.update(submission_id=existing["id"], expected_version=existing["version"])
            expected = {"submission:" + existing["id"]: existing["version"]}
        lineage = digest({"browser": selected["id"], "operation": operation, "input": payload, "blockers": blockers})
        # A context can change back after a partial read or another pending edit.
        # Preserve old superseded rows instead of returning one as the active review.
        while True:
            previous = tx.connection.execute("SELECT id,status,version FROM understand_proposals WHERE lineage_key=? AND operation=?", (lineage, operation)).fetchone()
            if previous is None or previous["status"] != "superseded":
                break
            lineage = digest({"browser_projection": lineage, "superseded": previous["id"], "version": previous["version"]})
        proposal = self.propose(tx, operation=operation, input=payload, application_id=selected["application_id"],
            evidence=evidence, expected_versions=expected, lineage_key=lineage, blockers=blockers)
        supersede(pending, proposal)
        return {"proposals": [proposal]}

    def project_requests(self, tx, *, analysis_id, application_id=None, message_id=None, association_required=False):
        """Typed pending recipes for associations, lifecycle facts, and required work.

        Association context is supplied by the workflow's public owner queries.
        No private Applications state is read here. Incomplete typed facts produce
        blocked review rows alongside their preserved findings.
        """
        analysis = self.get_analysis(tx.connection, analysis_id)
        if analysis["output"] and analysis["output"].get("relevance") == "unrelated":
            return {"analysis_id": analysis_id, "proposals": [], "status": "unrelated"}
        if application_id is None:
            targets = {f["content"].get("target_id") for f in analysis["findings"]
                       if f["category"] == "associations" and f["content"]["kind"] == "application"}
            if len(targets) == 1:
                application_id = next(iter(targets))
        def relevant_versions(target, payload=None):
            # Candidate retrieval can contain many applications; changes to an
            # unrelated candidate must not invalidate this exact proposed target.
            record_refs={kind+":"+str((payload or {}).get(kind+"_id")) for kind in ("task","interview","assessment","offer","submission")}
            return {reference: version for reference, version in analysis["descriptor"]["versions"].items()
                    if reference == "application:" + str(target) or reference.startswith(("message:","association:")) or reference in record_refs}
        dependencies, result = [], []
        if association_required and application_id and message_id:
            candidates = [f for f in analysis["findings"] if f["category"] == "associations" and
                          f["content"].get("target_id") == application_id]
            if candidates:
                linked = self.project_analysis(tx, analysis_id=analysis_id, projections=[{
                    "finding_id": candidates[0]["id"], "operation": "link_message",
                    "expected_versions": relevant_versions(application_id),
                    "application_id": application_id, "input": {"message_id": message_id,
                    "application_id": application_id, "expected_version": 0}}])["proposals"]
                result.extend(linked)
                dependencies.extend(p["id"] for p in linked)
        mapping = {"reply": "reply", "book_interview": "book_interview", "send_availability": "send_availability",
                   "assessment": "complete_assessment", "documents": "send_document", "follow_up": "follow_up", "other": "other"}
        for finding in analysis["findings"]:
            content = finding["content"]
            target = content.get("target_id") or application_id
            projection = None
            recipe_blockers = []
            if finding["category"] == "requests":
                projection = {"finding_id": finding["id"], "operation": "record_request", "application_id": target,
                    "dependencies": dependencies, "expected_versions": relevant_versions(target),
                    "input": {"application_id": target, "kind": mapping[content["kind"]],
                              "description": content["outcome"], "responsible_party": "applicant",
                              "completion_rule": "verified_send" if content["kind"] == "reply" else "human_decision", "origin_ref": finding["id"],
                              "evidence": content["evidence"]}}
            elif finding["category"] == "facts" and content["actionable_source"]:
                operation, payload, recipe_blockers = fact_recipe(finding, target, analysis["descriptor"])
                projection = {"finding_id": finding["id"], "operation": operation, "application_id": target,
                    "dependencies": dependencies, "expected_versions": relevant_versions(target,payload),
                    "input": payload}
            if projection:
                projected = self.project_analysis(tx, analysis_id=analysis_id, projections=[projection])["proposals"]
                for proposal in projected:
                    additional = list(recipe_blockers)
                    if not target or association_required and not dependencies:
                        additional.append("unresolved_association")
                    if application_id and target != application_id:
                        additional.append("target_disagrees_with_selected_association")
                    if proposal["status"] == "pending" and additional:
                        with tx.scope("understanding"):
                            blockers = list(dict.fromkeys(proposal["blockers"] + additional))
                            tx.connection.execute("UPDATE understand_proposals SET blockers=? WHERE id=?", (encode(blockers), proposal["id"]))
                            proposal["blockers"] = blockers
                result.extend(projected)
        return {"analysis_id": analysis_id, "proposals": result,
                "coverage": analysis["descriptor"]["coverage"]}

    def resolve(self, tx, *, proposal_id, expected_version, decision, reason=None):
        _human(tx)
        if decision not in {"applied", "rejected", "superseded", "stale"}:
            raise DomainError("invalid_input", "Invalid proposal decision")
        with tx.scope("understanding"):
            before = self.get(tx.connection, proposal_id)
            if before["version"] != expected_version or before["status"] not in {"pending", "stale"}:
                raise DomainError("version_conflict", "Proposal changed or was already resolved")
            if decision == "applied" and before["blockers"]:
                raise DomainError("dependency_unresolved", "Proposal has unresolved blockers")
            tx.connection.execute("UPDATE understand_proposals SET version=version+1,status=?,reason=? WHERE id=?",
                                  (decision, reason, proposal_id))
            after = self.get(tx.connection, proposal_id)
            tx.record("understanding", proposal_id, "review_changes", before, after)
            return after

    def replace(self, tx, *, proposal_id, expected_version, input, expected_versions=None, reason, resolved_blockers=()):
        _human(tx)
        old = self.get(tx.connection, proposal_id)
        if not isinstance(input, dict) or input.get("application_id", old["application_id"]) != old["application_id"]:
            raise DomainError("invalid_input", "Retargeting requires the explicit association correction workflow")
        if not isinstance(resolved_blockers, (list, tuple)) or any(not isinstance(b, str) for b in resolved_blockers) or not set(resolved_blockers) <= set(old["blockers"]):
            raise DomainError("invalid_input", "Resolve only explicitly reviewed existing blockers")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise DomainError("invalid_input", "Replacement requires an attributed reason")
        self.resolve(tx, proposal_id=proposal_id, expected_version=expected_version, decision="superseded", reason=reason)
        return self.propose(tx, operation=old["operation"], input=input, application_id=old["application_id"],
            evidence=old["evidence"], expected_versions=expected_versions if expected_versions is not None else old["expected_versions"],
            dependencies=old["dependencies"], analysis_id=old["analysis_id"], finding_id=old["finding_id"],
            lineage_key=digest({"replaces": proposal_id, "version": expected_version, "input": input}),
            blockers=[b for b in old["blockers"] if b not in resolved_blockers])

    def supersede_restatement(self, tx, *, proposal_id, expected_version, reason):
        """Projection workflow receipt; never applies a lifecycle change or approval."""
        if getattr(tx, "authorized_operation", None) != "project_analysis":
            raise DomainError("not_authorized", "Restatement reconciliation requires the projection workflow")
        with tx.scope("understanding"):
            before = self.get(tx.connection, proposal_id)
            if before["version"] != expected_version or before["status"] != "pending":
                raise DomainError("version_conflict", "Proposal changed before reconciliation")
            tx.connection.execute("UPDATE understand_proposals SET version=version+1,status='superseded',reason=? WHERE id=?",
                                  (reason, proposal_id))
            after = self.get(tx.connection, proposal_id)
            tx.record("understanding", proposal_id, "reconcile_restatement", before, after)
            return after

    def cancel_pending(self, tx, *, application_id, reason):
        if getattr(tx, "authorized_operation", None) not in {"close_application", "correct_association", "combine_jobs", "review_changes"}:
            raise DomainError("not_authorized", "Proposal supersession requires an authorized connecting workflow")
        ids = [r["id"] for r in tx.connection.execute("SELECT id FROM understand_proposals WHERE application_id=? AND status IN ('pending','stale')", (application_id,))]
        items = []
        with tx.scope("understanding"):
            for proposal_id in ids:
                if proposal_id == tx.context.causation_id:
                    continue
                before = self.get(tx.connection, proposal_id)
                tx.connection.execute("UPDATE understand_proposals SET version=version+1,status='superseded',reason=? WHERE id=?",
                                      (reason, proposal_id))
                after = self.get(tx.connection, proposal_id)
                tx.record("understanding", proposal_id, "supersede_pending", before, after)
                items.append(after)
        return {"items": items}

    def find_proposals_by_evidence(self, connection, source_id, application_id, limit=100, after=None):
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
            raise DomainError("invalid_input", "Invalid evidence proposal page size")
        # SQLite's JSON table function avoids scanning other applications into memory.
        rows = connection.execute("SELECT p.* FROM understand_proposals p WHERE application_id=? AND id>? AND EXISTS (SELECT 1 FROM json_each(p.evidence) e WHERE json_extract(e.value,'$.source_id')=?) ORDER BY id LIMIT ?",
                                  (application_id, after or "", source_id, limit + 1)).fetchall()
        items = [_proposal(row) for row in rows[:limit]]
        return {"items": items, "next_cursor": items[-1]["id"] if len(rows) > limit else None, "truncated": len(rows) > limit}

    def list_pending(self, connection, application_id=None, limit=50, after=None):
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise DomainError("invalid_input", "Invalid proposal page size")
        rows = connection.execute("SELECT * FROM understand_proposals WHERE status IN ('pending','stale') AND id>? AND (? IS NULL OR application_id=?) ORDER BY id LIMIT ?",
                                  (after or "", application_id, application_id, limit + 1)).fetchall()
        items = [_proposal(row) for row in rows[:limit]]
        return {"items": items, "next_cursor": items[-1]["id"] if len(rows) > limit else None,
                "coverage": {"complete": len(rows) <= limit}}
