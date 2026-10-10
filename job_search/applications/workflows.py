"""Explicit coordination through public owners; no business-table SQL lives here."""
from dataclasses import replace

from job_search.commands import DomainError,digest


INTERNAL_OPERATIONS = frozenset({"save_job", "add_note", "create_task", "record_request",
    "complete_task", "cancel_task", "snooze_task", "record_submission", "confirm_submission", "attach_submission_answers",
    "schedule_interview", "reschedule_interview", "cancel_interview", "record_assessment",
    "update_assessment", "record_offer", "decide_offer", "record_progress", "correct_progress",
    "close_application", "reopen_application", "create_reminder", "cancel_reminder"})
RECORD_KINDS = {"task":"tasks", "interview":"interviews", "assessment":"assessments",
    "offer":"offers", "submission":"submissions", "progress":"progress", "reminder":"reminders"}


class ApplicationWorkflows:
    def __init__(self, applications, correspondence, understanding, actions):
        self.applications = applications
        self.correspondence = correspondence
        self.understanding = understanding
        self.actions = actions

    def recover_processing(self, tx, operation, value):
        if set(value) != {"issue_id", "expected_version", "expected_analysis_id", "reason"}:
            raise DomainError("invalid_input", "Processing recovery requires the exact reviewed issue")
        issue = self.understanding.get_processing_issue(tx.connection, value["issue_id"])
        source = self.correspondence.get(tx.connection, issue["source_id"])
        if source["revision"] != issue["revision"] or source["source_sha256"] != issue["sha256"]:
            raise DomainError("version_conflict", "A newer source revision requires review")
        if source["direction"] != "incoming":
            raise DomainError("invalid_input", "Only incoming evidence has an Understanding recovery")
        return getattr(self.understanding, operation)(tx, **value)

    def check_versions(self, connection, versions):
        for reference, expected in versions.items():
            kind, separator, record_id = reference.partition(":")
            if not separator or not isinstance(expected,int) or isinstance(expected,bool):
                raise DomainError("invalid_input", "Version references require kind:id and an integer version")
            if kind == "application":
                record = self.applications.get_application(connection, record_id)
            elif kind in RECORD_KINDS:
                record = self.applications.get_record(connection, RECORD_KINDS[kind], record_id)
            elif kind == "message":
                record = self.correspondence.get(connection, record_id)
            elif kind == "association":
                record = self.correspondence.get_association(connection, record_id)
            else:
                raise DomainError("invalid_input", "Unsupported version reference")
            if record["version"] != expected:
                raise DomainError("version_conflict", "A relevant record changed after review")

    def reconcile_projected_changes(self, tx, proposals):
        """Retire exact restatements, using authoritative owner reads in this transaction.

        Conflicts, uncertain associations, new timestamps, and other operations
        remain reviewable. Evidence stays attached to the retained proposal.
        """
        result = []
        for proposal in proposals:
            reason = self._already_recorded(tx.connection, proposal)
            result.append(self.understanding.supersede_restatement(tx,
                proposal_id=proposal["id"], expected_version=proposal["version"], reason=reason)
                if reason else proposal)
        return result

    def _already_recorded(self, connection, proposal):
        if (proposal["status"] != "pending" or proposal["blockers"] or proposal["dependencies"]
                or not proposal.get("application_id")):
            return None
        value = proposal["input"]
        application_id = proposal["application_id"]
        if value.get("application_id") != application_id:
            return None
        versions = proposal["expected_versions"]
        if "application:" + application_id not in versions:
            return None
        try:
            self.check_versions(connection, versions)
            app = self.applications.get_application(connection, application_id)
        except DomainError:
            return None
        if app["id"] != application_id:
            return None
        if proposal["operation"] == "close_application":
            if (app["disposition"] == "closed" and app["outcome"] == value.get("outcome")
                    and value.get("expected_version") == app["version"]
                    and not value.get("expected_records")
                    and set(value) <= {"application_id", "expected_version", "expected_records", "outcome", "reason"}):
                return "Already recorded: application is closed with this outcome."
        elif proposal["operation"] in {"confirm_submission", "record_submission"}:
            if set(value) - {"application_id", "submission_id", "expected_version", "status", "occurred_at", "evidence"}:
                return None
            record_id = value.get("submission_id")
            if not record_id or "submission:" + record_id not in versions:
                return None
            record = self.applications.get_record(connection, "submissions", record_id)
            if (record["application_id"] == app["id"] and record["pursuit_no"] == app["pursuit_no"]
                    and record["version"] == value.get("expected_version")
                    and record["status"] == value.get("status")
                    and record.get("occurred_at") == value.get("occurred_at")):
                return "Already recorded: this submission has the same status and timestamp."
        return None

    def applicability(self, tx, envelope):
        app = self.applications.get_application(tx.connection,envelope["application_id"])
        if app["id"] != envelope["application_id"] or app["pursuit_no"] != envelope["pursuit_no"]:
            raise DomainError("version_conflict", "Action belongs to an earlier application context")
        if app["disposition"] == "closed" and not envelope.get("allow_closed"):
            raise DomainError("version_conflict", "Application is closed")
        required = "application:"+app["id"]
        if required not in envelope["context_versions"]:
            raise DomainError("invalid_input", "Action approval must bind the application version")
        self.check_versions(tx.connection,envelope["context_versions"])
        consequence=envelope.get("consequence")
        if consequence:
            task=self.applications.get_record(tx.connection,"tasks",consequence["task_id"])
            if (task["application_id"] != app["id"] or task["pursuit_no"] != app["pursuit_no"] or
                    task["status"] != "open" or task["version"] != consequence["expected_version"] or
                    task["completion_rule"] != "verified_send"):
                raise DomainError("version_conflict", "The authorized task consequence no longer applies")
        if envelope["kind"] in {"send_reply","create_reply_draft"}:
            target=envelope["target"]
            message=self.correspondence.get(tx.connection,target["message_id"])
            if (message["account_id"] != envelope["account_id"] or message["source_sha256"] != target["source_hash"] or
                    message["provider_message_id"] != target.get("provider_message_id")):
                raise DomainError("version_conflict", "Reply source identity or content changed")
            association=self.correspondence.association(tx.connection,message["id"])
            if not association or association["application_id"]!=app["id"]:
                raise DomainError("version_conflict","Reply source is not linked to this application")
            for reference in ("message:"+message["id"],"association:"+association["id"]):
                if reference not in envelope["context_versions"]:
                    raise DomainError("invalid_input","Reply approval must bind its message and association versions")
        return True

    def apply_internal(self, tx, operation, value):
        if operation=="correct_association":
            return self.correct_association(tx,value)
        if operation=="combine_jobs":
            return self.combine_jobs(tx,value)
        if operation == "link_message":
            self.applications.get_application(tx.connection,value["application_id"])
            return self.correspondence.link_message(tx,**value)
        if operation not in INTERNAL_OPERATIONS:
            raise DomainError("invalid_input", "Proposal does not name a reviewable internal operation")
        if operation == "close_application":
            result=self.applications.close_application(tx,value)
            self.actions.invalidate_context(tx,value["application_id"],"application_closed")
            self.understanding.cancel_pending(tx,application_id=value["application_id"],reason="application_closed")
            return result
        result=getattr(self.applications,operation)(tx,value)
        if operation in {"record_submission","confirm_submission","attach_submission_answers"}:
            attempts={(e["device_id"],e["attempt_ref"]) for e in result.get("evidence",[]) if e.get("device_id") and e.get("attempt_ref")}
            for device,attempt_id in attempts:
                attempt=self.applications.get_browser_attempt(tx.connection,device,attempt_id,limit=200)
                if attempt is None:
                    continue
                captures=[o for o in attempt["observations"] if o["activity"]=="answer_capture"]
                latest=attempt.get("latest_answer_observation_id")
                if latest and latest not in {o["id"] for o in captures}:
                    captures.append(self.applications.get_record(tx.connection,"observations",latest))
                for observation in captures:
                    self.understanding.project_browser(tx,observation=observation,submissions=[result])
        if operation in {"reschedule_interview","cancel_interview","correct_progress"}:
            app_id=result.get("application_id") or value.get("application_id")
            targets=[]
            if value.get("interview_id"):
                targets=[value["interview_id"],"interview:"+value["interview_id"]]
            for item in value.get("corrections",[]):
                targets.extend((item["id"],item["kind"].rstrip("s")+":"+item["id"]))
            self.actions.invalidate_context(tx,app_id,"context_changed",target_ids=targets or None)
        return result

    def review_changes(self, tx, decisions):
        if tx.context.principal.kind != "human" or not isinstance(decisions,list) or not 1<=len(decisions)<=50:
            raise DomainError("not_authorized", "A bounded human review bundle is required")
        selected={}
        for decision in decisions:
            if set(decision)-{"proposal_id","expected_version","decision","reason"}:
                raise DomainError("invalid_input","Unknown review field")
            proposal=self.understanding.get(tx.connection,decision["proposal_id"])
            if proposal["id"] in selected or proposal["status"]!="pending" or proposal["version"]!=decision["expected_version"]:
                raise DomainError("version_conflict","A selected proposal changed")
            if decision["decision"] not in {"accept","reject"}:
                raise DomainError("invalid_input","Review decision must be accept or reject")
            if decision["decision"]=="accept":
                if proposal["blockers"]:
                    raise DomainError("dependency_unresolved","Resolve the proposal's blockers first")
                self.check_versions(tx.connection,proposal["expected_versions"])
                if proposal.get("application_id") and proposal["input"].get("application_id",proposal["application_id"]) != proposal["application_id"]:
                    raise DomainError("invalid_input","Proposal target differs from operation target")
            selected[proposal["id"]]=(proposal,decision)
        results=[]
        remaining=dict(selected)
        applied=set()
        original=tx.context
        try:
            while remaining:
                progressed=False
                for proposal_id,(proposal,decision) in list(remaining.items()):
                    accepting=decision["decision"]=="accept"
                    if accepting:
                        unresolved=False
                        for dependency in proposal["dependencies"]:
                            if dependency in applied:
                                continue
                            if dependency in remaining and remaining[dependency][1]["decision"]=="accept":
                                unresolved=True
                                continue
                            if self.understanding.get(tx.connection,dependency)["status"]!="applied":
                                raise DomainError("dependency_unresolved","A prerequisite was not accepted")
                        if unresolved:
                            continue
                    tx.context=replace(original,causation_id=proposal_id)
                    result=self.apply_internal(tx,proposal["operation"],proposal["input"]) if accepting else None
                    resolved=self.understanding.resolve(tx,proposal_id=proposal_id,
                        expected_version=proposal["version"],decision="applied" if accepting else "rejected",
                        reason=decision.get("reason"))
                    results.append({"proposal":resolved,"result":result})
                    if accepting:
                        applied.add(proposal_id)
                    del remaining[proposal_id]
                    progressed=True
                if not progressed:
                    raise DomainError("dependency_unresolved","Proposal dependencies contain a cycle")
        finally:
            tx.context=original
        return {"status":"applied","decisions":results}

    def apply_action_result(self, tx, result_id):
        result=self.actions.get_result(tx.connection,result_id)
        if result["delivery"]!="pending":
            return result
        envelope=result["envelope"]
        consequence=envelope.get("consequence")
        if not consequence:
            return self.actions.acknowledge_result(tx,result_id,"applied")
        try:
            # External success stays true even if the application has since changed.
            self.check_versions(tx.connection,envelope["context_versions"])
            app=self.applications.get_application(tx.connection,envelope["application_id"])
            task=self.applications.get_record(tx.connection,"tasks",consequence["task_id"])
            if (app["disposition"]!="open" or app["pursuit_no"]!=envelope["pursuit_no"] or
                    task["application_id"]!=app["id"] or task["pursuit_no"]!=app["pursuit_no"] or
                    task["version"]!=consequence["expected_version"] or task["status"]!="open" or
                    task["completion_rule"]!="verified_send"):
                raise DomainError("version_conflict","Authorized consequence changed")
        except DomainError as exc:
            return self.actions.acknowledge_result(tx,result_id,"conflict",reason=exc.code)
        original=tx.context
        try:
            tx.context=replace(original,causation_id=result_id)
            self.applications.complete_task(tx,{"task_id":task["id"],"expected_version":task["version"],
                "reason":"Verified authorized send: "+result_id,"result_id":result_id})
            return self.actions.acknowledge_result(tx,result_id,"applied")
        finally:
            tx.context=original

    def combine_jobs(self, tx, value):
        source,target=value["source_application_id"],value["target_application_id"]
        if self.actions.has_unresolved(tx.connection,source) or self.actions.has_unresolved(tx.connection,target):
            raise DomainError("needs_reconciliation","Resolve external executions before combining jobs")
        result=self.applications.combine_jobs(tx,value)
        for application_id in (source,target):
            self.actions.invalidate_context(tx,application_id,"jobs_combined")
            self.understanding.cancel_pending(tx,application_id=application_id,reason="jobs_combined")
        after=None
        while True:
            page=self.correspondence.list_associations(tx.connection,source,after=after)
            for link in page["items"]:
                self.correspondence.correct_association(tx,association_id=link["id"],application_id=target,
                    expected_version=link["version"],reason=value["reason"])
            after=page["next_cursor"]
            if after is None:
                break
        return result

    def preview_association_correction(self,connection,association_id):
        association=self.correspondence.get_association(connection,association_id)
        source_id=association["message_id"]
        proposals=self.understanding.find_proposals_by_evidence(connection,source_id,association["application_id"],limit=200)
        if proposals.get("truncated") or proposals.get("next_cursor"):
            raise DomainError("dependency_unresolved","Correction exceeds the bounded review size")
        by_source=self.applications.affected_by_evidence(connection,association["application_id"],source_id)
        by_cause=self.applications.affected_by_causation(connection,association["application_id"],[p["id"] for p in proposals["items"]])
        if by_source["truncated"] or by_cause["truncated"]:
            raise DomainError("dependency_unresolved","Correction exceeds the bounded review size")
        affected={(item["kind"],item["id"]):item for item in by_source["items"]+by_cause["items"]}
        value={"association":association,"affected":[affected[key] for key in sorted(affected)],
               "proposals":proposals["items"]}
        return {**value,"preview_digest":digest(value)}

    def correct_association(self,tx,value):
        required={"association_id","application_id","expected_version","preview_digest","corrections","reason"}
        if set(value)!=required:
            raise DomainError("invalid_input","Correction requires the complete reviewed preview and resolutions")
        preview=self.preview_association_correction(tx.connection,value["association_id"])
        if preview["preview_digest"]!=value["preview_digest"] or preview["association"]["version"]!=value["expected_version"]:
            raise DomainError("version_conflict","Association correction preview changed")
        target=self.applications.get_application(tx.connection,value["application_id"])
        old_app=preview["association"]["application_id"]
        if target["id"]==old_app:
            raise DomainError("invalid_input","Choose a different application")
        wanted={(item["kind"],item["id"]):item["expected_version"] for item in preview["affected"]}
        supplied={(item["kind"],item["id"]):item["expected_version"] for item in value["corrections"]}
        if wanted!=supplied or len(supplied)!=len(value["corrections"]):
            raise DomainError("version_conflict","Resolve every affected record using the displayed versions")
        corrected=None
        if value["corrections"]:
            corrected=self.applications.correct_progress(tx,{"application_id":old_app,
                "corrections":value["corrections"],"reason":value["reason"]})
        association=self.correspondence.correct_association(tx,association_id=value["association_id"],
            application_id=target["id"],expected_version=value["expected_version"],reason=value["reason"])
        targets={"association:"+association["id"],"message:"+association["message_id"]}
        for item in preview["affected"]:
            targets.update((item["id"],item["kind"].rstrip("s")+":"+item["id"]))
        self.actions.invalidate_context(tx,old_app,"association_corrected",target_ids=targets)
        replacements=[]
        for proposal in preview["proposals"]:
            if proposal["status"]=="pending":
                self.understanding.resolve(tx,proposal_id=proposal["id"],expected_version=proposal["version"],
                    decision="superseded",reason=value["reason"])
            if proposal["status"]!="applied" or proposal["operation"]=="link_message":
                continue
            # Reassignment creates suggestions; it does not accept facts at the new target.
            payload={**proposal["input"],"application_id":target["id"]}
            blockers=[]
            if any(key in payload for key in ("task_id","submission_id","interview_id","assessment_id","offer_id")):
                blockers=["requires_target_record_resolution"]
            replacements.append(self.understanding.propose(tx,operation=proposal["operation"],input=payload,
                application_id=target["id"],evidence=proposal["evidence"],blockers=blockers,
                expected_versions={"application:"+target["id"]:target["version"],"association:"+association["id"]:association["version"]},
                lineage_key=digest({"correction":value["preview_digest"],"proposal":proposal["id"],"target":target["id"]})))
        return {"association":association,"correction":corrected,"proposals":replacements}
