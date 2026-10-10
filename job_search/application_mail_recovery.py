"""Exact owner/worker reconciliation; no inference, provider I/O, or history deletion."""
import json

from .commands import CommandContext, DomainError, Principal, digest
from .contracts import ConflictError, ContractError
from .recovery import RecoveryService


KINDS = {("correspondence", "understand_message"), ("understanding", "retry_processing"),
         ("understanding", "project_analysis")}


def _proof(runtime, con, reference, account_id):
    if not isinstance(reference, dict) or set(reference) != {"owner","kind","key","work_id"}:
        return None
    if (reference["owner"], reference["kind"]) not in KINDS:
        return None
    work = runtime.executor.find_work(con, reference["owner"], reference["kind"], reference["key"])
    if work is None or work["id"] != reference["work_id"]:
        return None
    payload = json.loads(work["payload"])
    if reference["kind"] == "project_analysis":
        if set(payload) != {"analysis_id"} or reference["key"] != payload["analysis_id"]:
            return None
        analysis = runtime.understanding.get_analysis(con, payload["analysis_id"])
        projection = analysis["descriptor"].get("context", {}).get("mail_projection")
        if not projection:
            return None
        message_id, revision = projection["message_id"], projection["revision"]
    else:
        message_id, revision = payload["message_id"], payload["revision"]
        if reference["kind"] == "understand_message":
            if set(payload) != {"message_id","revision"} or reference["key"] != revision:
                return None
        elif (set(payload) != {"issue_id","expected_version","analysis_id","message_id","revision","sha256"}
                or type(payload["expected_version"]) is not int
                or reference["key"] != payload["issue_id"] + ":" + str(payload["expected_version"])):
            return None
    source = runtime.correspondence.get(con, message_id, revision)
    if source["account_id"] != account_id:
        return None
    if reference["kind"] == "retry_processing" and payload["sha256"] != source["source_sha256"]:
        return None
    if reference["kind"] == "project_analysis" and not any(
            item["source_id"] == message_id and item["revision"] == revision and item["sha256"] == source["source_sha256"]
            for item in analysis["descriptor"]["sources"]):
        return None
    current_source = runtime.correspondence.get(con, message_id)
    issue = runtime.understanding.processing_for_source(con, message_id, revision)
    reason = None
    if current_source["revision"] != revision:
        reason = "new_source_revision"
    elif issue and issue["status"] in {"succeeded","resolved_manually","superseded"}:
        reason = "owner_resolved"
    elif reference["kind"] == "project_analysis" and issue and issue["analysis_id"] != payload["analysis_id"]:
        reason = "new_analysis"
    elif reference["kind"] == "retry_processing" and issue and issue["issue_id"] == payload.get("issue_id"):
        if issue["version"] > payload["expected_version"] and not runtime.understanding.processing_retry_applicable(con, payload):
            reason = "retry_finished"
    elif reference["kind"] == "understand_message" and issue:
        if issue["status"] == "retry_queued":
            reason = "explicit_retry"
        else:
            current = runtime.understanding.get_analysis(con, issue["analysis_id"])
            retry = current["descriptor"].get("context", {}).get("processing_attempt")
            if retry and retry.get("issue_id") == issue["issue_id"]:
                reason = "explicit_retry"
    if reason is None:
        return None
    return {"owner_work_id": work["id"], "owner":reference["owner"], "kind":reference["kind"], "key":reference["key"],
            "reason":reason, "source_id":message_id, "revision":revision,
            "issue_id":issue["issue_id"] if issue else None, "issue_version":issue["version"] if issue else None,
            "analysis_id":issue["analysis_id"] if issue else None}


def reconcile_reference(runtime, reference, account_id, *, operational=None, item=None):
    """Hold owner lock before operational lock; retry repairs either crash boundary."""
    try:
        with runtime.executor.read() as con:
            expected = _proof(runtime, con, reference, account_id)
        if expected is None:
            return {"reconciled":False}
        identity = {"proof":expected, "operational_work_id":item["work_id"] if item else None,
                    "operational_revision":item["recovery_revision"] if item else None}
        context = CommandContext(Principal("mail-work-reconciler","worker",frozenset({"project_analysis"})),
                                 "mail-work-recovery:" + digest(identity), "result")
        def reconcile(tx):
            proof = _proof(runtime, tx.connection, reference, account_id)
            if proof != expected:
                raise DomainError("version_conflict", "Owner processing changed during reconciliation")
            # Commit operational receipt while the owner proof cannot change. If
            # the outer commit crashes, this immutable receipt makes retry safe.
            if operational is not None and item is not None:
                operational.resolve_owner_mail_work(item["work_id"], expected_revision=item["recovery_revision"],
                    expected_payload=reference, proof=proof, now=tx.now)
            work = runtime.executor.find_work(tx.connection, reference["owner"], reference["kind"], reference["key"])
            with tx.scope(reference["owner"]):
                if work["status"] != "done":
                    tx.finish_work(work["id"])
                tx.record(reference["owner"], work["id"], "reconcile_processing_work", {"status":work["status"]},
                    {"status":"done", "proof":proof, "operational_work_id":item["work_id"] if item else None})
            return {"reconciled":True, "owner_work_id":work["id"]}
        return runtime.executor.run(context, "project_analysis", identity, reconcile)
    except (DomainError, ContractError, ConflictError, KeyError, TypeError, ValueError):
        # Missing/malformed lineage or unresolved provider outcome stays visible.
        return {"reconciled":False}


def reconcile_mail_work(runtime, account_id, *, owner_work=(), operational_db=None, after="", limit=100):
    owner_count = 0
    for work in owner_work:
        reference = {"owner":work["owner"], "kind":work["kind"], "key":work["dedupe_key"], "work_id":work["id"]}
        owner_count += reconcile_reference(runtime, reference, account_id)["reconciled"]
    count, cursor = 0, None
    if operational_db is not None:
        operational = RecoveryService(operational_db)
        page = operational.owner_mail_candidates(after=after, limit=limit)
        cursor = page["next_cursor"]
        for item in page["items"]:
            try:
                reference = json.loads(item["payload_json"])
            except (TypeError, ValueError):
                continue
            count += reconcile_reference(runtime, reference, account_id, operational=operational, item=item)["reconciled"]
    return {"owner_work_completed":owner_count,"operational_work_resolved":count,"next_cursor":cursor}
