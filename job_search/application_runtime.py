"""Composition for the isolated application candidate; never configures live dispatch."""
import uuid
from contextlib import contextmanager

from .commands import CommandContext, CommandExecutor, DomainError, Principal, digest
from .applications.api import ApplicationOperations, SCHEMA as APPLICATION_SCHEMA, SCHEMA_MIGRATIONS as APPLICATION_MIGRATIONS
from .applications.workflows import ApplicationWorkflows, INTERNAL_OPERATIONS
from .applications.queries import ApplicationQueries
from .correspondence.api import CorrespondenceOperations, SCHEMA as CORRESPONDENCE_SCHEMA
from .applications.understanding.api import UnderstandingOperations, SCHEMA as UNDERSTANDING_SCHEMA, SCHEMA_MIGRATIONS as UNDERSTANDING_MIGRATIONS
from .external_actions.api import ExternalActionOperations, SCHEMA as ACTION_SCHEMA, SCHEMA_MIGRATIONS as ACTION_MIGRATIONS


class ApplicationRuntime:
    """Explicit candidate composition. Existing hosts must opt in by construction."""
    def __init__(self,path,*,clock=None,extra_schemas=None,mail_source=None,reply_provider_resolver=None):
        if set(extra_schemas or {}) & {"commands","applications","correspondence","understanding","external_actions"}:
            raise DomainError("invalid_input","Extra schemas cannot replace an existing owner")
        self.executor=CommandExecutor(path,{"applications":APPLICATION_SCHEMA,
            "correspondence":CORRESPONDENCE_SCHEMA,"understanding":UNDERSTANDING_SCHEMA,
            "external_actions":ACTION_SCHEMA,**(extra_schemas or {})},clock=clock, schema_migrations={**APPLICATION_MIGRATIONS, **ACTION_MIGRATIONS, **UNDERSTANDING_MIGRATIONS})
        self.applications=ApplicationOperations()
        self.correspondence=CorrespondenceOperations()
        self.understanding=UnderstandingOperations()
        self.actions=ExternalActionOperations()
        from .application_preparation import ReplyPreparationService
        self.reply_preparation=ReplyPreparationService(self,reply_provider_resolver)
        self.workflows=ApplicationWorkflows(self.applications,self.correspondence,self.understanding,self.actions)
        self.queries=ApplicationQueries(self.executor,self.applications,self.correspondence,self.understanding,self.actions,mail_source=mail_source)

    def command(self,context,operation,payload):
        if not isinstance(payload,dict):
            raise DomainError("invalid_input","Operation input must be an object")
        if set(payload)&{"actor_kind","principal","capabilities","origin","delegation"}:
            raise DomainError("not_authorized","Authority cannot be supplied in operation input")
        return self.executor.run(context,operation,payload,lambda tx:self._dispatch(tx,operation,payload))

    def _dispatch(self,tx,operation,payload):
        if operation in INTERNAL_OPERATIONS:
            return self.workflows.apply_internal(tx,operation,payload)
        if operation in {"retry_processing", "resolve_processing"}:
            return self.workflows.recover_processing(tx, operation, payload)
        if operation=="review_changes":
            return self.workflows.review_changes(tx,payload["decisions"])
        if operation=="replace_proposal":
            return self.understanding.replace(tx,**payload)
        if operation=="combine_jobs":
            return self.workflows.combine_jobs(tx,payload)
        if operation=="correct_association":
            return self.workflows.correct_association(tx,payload)
        if operation=="link_message":
            return self.workflows.apply_internal(tx,operation,payload)
        if operation in {"schedule_operation","cancel_schedule","reenable_reminder"}:
            return getattr(self.applications,operation)(tx,payload)
        if operation=="run_scheduled":
            return self.applications.run_schedule(tx,payload["schedule_id"])
        if operation=="queue_due_reminders":
            return self.applications.queue_due_reminders(tx,**payload)
        if operation=="record_delivery":
            return self.applications.record_delivery(tx,payload)
        if operation=="record_message":
            source=self.correspondence.record_message(tx,**payload)
            self.understanding.supersede_processing_source(tx,source_id=source["id"],latest_revision=source["latest_revision"])
            return source
        if operation=="record_browser_observation":
            observation=self.applications.record_browser_observation(tx,payload)
            submissions=self.applications.list_records(tx.connection,observation["application_id"],"submissions",current_only=True)
            attempt=self.applications.get_browser_attempt(tx.connection,observation["device_id"],observation["attempt_ref"],limit=200)
            proposals=self.understanding.project_browser(tx,observation=observation,submissions=submissions,attempt=attempt)
            self._finish_work(tx,"applications","analyze_browser",observation["id"])
            return {"observation":observation,"review":proposals}
        if operation=="propose_changes":
            return self._propose(tx,payload)
        if operation in {"prepare_reply","prepare_calendar_change"}:
            if payload.get("envelope") is not None:
                self.workflows.applicability(tx,payload["envelope"])
            return getattr(self.actions,operation)(tx,**payload)
        if operation=="authorize_action":
            return self.actions.authorize_action(tx,**payload,applicability=self.workflows.applicability)
        if operation in {"reject_action","revoke_action"}:
            return getattr(self.actions,operation)(tx,**payload)
        if operation=="apply_action_result":
            result=self.workflows.apply_action_result(tx,payload["result_id"])
            self._finish_work(tx,"external_actions","apply_action_result",payload["result_id"])
            return result
        raise DomainError("invalid_input","Unknown public operation")

    def _propose(self,tx,payload):
        if payload.get("operation") not in INTERNAL_OPERATIONS|{"link_message","correct_association","combine_jobs"}:
            raise DomainError("invalid_input","Unsupported proposed operation")
        value=dict(payload)
        requested=dict(value["input"])
        if tx.context.origin=="direct" and requested.get("job_source") and not requested.get("application_id"):
            app=self.applications.ensure_application(tx,{"job_source":requested.pop("job_source")})
            requested["application_id"]=app.get("application_id",app.get("id"))
            value["application_id"]=requested["application_id"]
        value["input"]=requested
        return self.understanding.propose(tx,**value)

    def result_context(self,operation,key):
        return CommandContext(Principal("candidate-result-worker","worker",frozenset({operation})),key,"result")

    def _finish_work(self,tx,owner,kind,key):
        work=self.executor.find_work(tx.connection,owner,kind,key)
        if work:
            tx.finish_work(work["id"])

    def process_results(self,*,limit=100):
        with self.executor.read() as con:
            pending=self.actions.list_results(con,delivery="pending",limit=limit)["items"]
        return {"items":[self.command(self.result_context("apply_action_result",item["result_id"]),
            "apply_action_result",{"result_id":item["result_id"]}) for item in pending]}

    def process_scheduled(self,*,limit=100):
        """Explicit offline tick; queues owner notifications without sending them."""
        now=self.executor.clock()
        with self.executor.read() as con:
            due=self.applications.due_schedules(con,now,limit=limit)
        def context(operation,key):
            return CommandContext(Principal("candidate-scheduler","worker",frozenset({operation})),key,"scheduled")
        results=[self.command(context("run_scheduled",item["id"]),"run_scheduled",{"schedule_id":item["id"]}) for item in due["items"]]
        notifications=self.command(context("queue_due_reminders",uuid.uuid4().hex),"queue_due_reminders",{"limit":limit})
        return {"schedules":results,"truncated":due["truncated"],"notifications":notifications}

    def test_action_worker(self,provider):
        from .external_actions.worker import ExternalActionWorker
        return ExternalActionWorker(self.executor,self.actions,provider,self.result_context,
            self.workflows.applicability,allow_test_dispatch=True)

    def analyze_message(self,message_id,*,archive,allowed_accounts,analyzer,model_version,
                        candidate_ids=(),target_application_id=None,revision=None,candidate_coverage=None,heartbeat=None,
                        processing_retry=None):
        """Capture context, infer outside transactions, persist, then project separately."""
        from .applications.understanding.contracts import AnalysisInput,SourceText
        from .inference.usage import UsageDeferred
        if len(candidate_ids)>20:
            raise DomainError("invalid_input","At most twenty candidate applications are allowed")
        def context_for(operation,key):
            return CommandContext(Principal("understanding-worker","worker",frozenset({operation})),key,"inferred")
        prompt_version=getattr(analyzer,"prompt_version","1")
        with self.executor.read() as con:
            source=self.correspondence.evidence(con,message_id,revision=revision,archive=archive,allowed_accounts=allowed_accounts)
        import hashlib
        truncated=source["text"] is not None and hashlib.sha256(source["text"].encode("utf-8")).hexdigest()!=source["source_sha256"]
        if source["text"] is None or truncated:
            refs=[{"source_id":message_id,"revision":source["revision"],"sha256":source["source_sha256"]}]
            failure=self.executor.run(context_for("record_analysis","unavailable:"+digest({"sources":refs,"candidates":list(candidate_ids),"truncated":truncated,"retry":processing_retry})),"record_analysis",
                {"source_refs":refs,"candidate_ids":list(candidate_ids)},lambda tx:self.understanding.record_source_failure(
                    tx,source_refs=refs,failure_code="incomplete_coverage" if truncated else "source_unavailable",candidate_ids=list(candidate_ids),retry=processing_retry))
            return {"status":"source_unavailable","coverage":source["coverage"],"analysis_id":failure["id"],"proposals":[]}
        coverage=dict(source["coverage"])
        reasons=list(coverage.get("reasons", []))
        if source["revision"] != source["latest_revision"]:
            reasons.append("historical_source_revision")
        if candidate_coverage is not None and candidate_coverage.get("complete") is not True:
            reasons.append("incomplete_candidate_coverage")
        with self.executor.read() as con:
            if source["direction"]!="incoming":
                return {"status":"not_incoming","proposals":[]}
            association=self.correspondence.association(con,message_id)
            identities=list(dict.fromkeys(candidate_ids))
            if association and association["application_id"] not in identities:
                identities.insert(0,association["application_id"])
                if len(identities)>20:
                    reasons.append("incomplete_candidate_coverage")
                identities=identities[:20]
            apps=[self.applications.get_application(con,identity) for identity in identities]
            versions={"application:"+app["id"]:app["version"] for app in apps}
            versions["message:"+message_id]=source["version"]
            association=self.correspondence.association(con,message_id)
            if association:
                versions["association:"+association["id"]]=association["version"]
                target_application_id=association["application_id"]
            previews={"closure_previews":{},"interview_previews":{},"records":{},
                      "mail_projection":{"message_id":message_id,"revision":source["revision"],
                          "target_application_id":target_application_id,"association_required":association is None}}
            if processing_retry is not None:
                # A human retry is a new analysis attempt even when the previous
                # valid response was uncertain. Deliveries of this same queued
                # attempt still share one fingerprint and successful result.
                previews["processing_attempt"]={"issue_id":processing_retry["issue_id"],
                    "version":processing_retry["expected_version"],"analysis_id":processing_retry["analysis_id"]}
            for app in apps:
                closure=self.applications.preview_closure(con,app["id"])
                previews["closure_previews"][app["id"]]={"expected_version":closure["expected_version"],"expected_records":closure["expected_records"]}
                for kind in ("interviews","assessments","offers","submissions"):
                    records=self.applications.query_records(con,kind,application_id=app["id"],current_only=True,limit=100)
                    if records["truncated"]:
                        reasons.append("incomplete_"+kind+"_coverage")
                    previews["records"].setdefault(app["id"], {})[kind]=records["items"]
                    for record in records["items"]:
                        versions[kind[:-1]+":"+record["id"]]=record["version"]
                        if kind=="interviews":
                            preview=self.applications.preview_interview_change(con,record["id"])
                            previews["interview_previews"][record["id"]]={"expected_related_versions":preview["expected_related_versions"]}
            if reasons:
                coverage.update(complete=False,reasons=list(dict.fromkeys(reasons)))
            if heartbeat is not None and not heartbeat():
                raise RuntimeError("Understanding worker lease was lost")
            context=AnalysisInput((SourceText(message_id,source["revision"],source["source_sha256"],source["text"]),),
                tuple({"id":app["id"],"job":self.applications.get_job(con,app["job_id"]),
                       "lifecycle": {key: app[key] for key in ("disposition", "outcome", "pursuit_no")}} for app in apps),
                versions,coverage,previews)
        claim=self.executor.run(context_for("analyze_observation",uuid.uuid4().hex),"analyze_observation",
            {"context":context.descriptor(),"model":model_version},lambda tx:self.understanding.claim_analysis(
                tx,context=context,model_version=model_version,prompt_version=prompt_version))
        if claim["status"]=="busy":
            return claim
        if claim["status"]=="persisted":
            analysis_id=claim["analysis_id"]
        else:
            output,failure,provider_error=None,None,None
            try:
                def renew_claim():
                    return self.executor.run(context_for("analyze_observation",uuid.uuid4().hex),"analyze_observation",
                        {"claim":claim},lambda tx:self.understanding.renew_analysis(tx,claim=claim))["renewed"]
                with _analysis_heartbeat(renew_claim, heartbeat):
                    output=analyzer.analyze(context)
                from .applications.understanding.contracts import validate_analysis
                validate_analysis(output,context)
            except UsageDeferred as exc:
                # Allowance waits and polling an accepted job are not failed
                # analyses. Leave the exact processing retry and outbox pending;
                # the operational worker owns the next due time and provider ID.
                self.executor.run(context_for("analyze_observation",claim["token"]+":deferred"),
                    "analyze_observation",{"claim":claim,"reason":exc.reason_code,"retry_at":exc.retry_at},
                    lambda tx:self.understanding.defer_analysis(tx,claim=claim,
                        reason_code=exc.reason_code,retry_at=exc.retry_at))
                raise
            except DomainError as exc:
                output=None
                failure=exc.code if exc.code in {"incomplete_coverage", "context_budget_exceeded", "invalid_json",
                    "evidence_mismatch", "output_truncated", "output_too_large", "invalid_output"} else "invalid_output"
            except Exception as exc:
                output=None
                failure="provider_failed"
                provider_error=exc
            if heartbeat is not None and not heartbeat():
                output=None
                failure="provider_failed"
                provider_error=RuntimeError("Understanding worker lease was lost")
            try:
                analysis=self.executor.run(context_for("record_analysis",claim["token"]),"record_analysis",
                    {"claim":claim,"output":output,"failure":failure},lambda tx:self.understanding.store_analysis(
                        tx,context=context,model_version=model_version,output=output,failure_code=failure,claim=claim,retry=processing_retry,prompt_version=prompt_version))
            except DomainError as exc:
                if exc.code!="invalid_input":
                    raise
                analysis=self.executor.run(context_for("record_analysis",claim["token"]+":invalid"),"record_analysis",
                    {"claim":claim,"failure":"invalid_output"},lambda tx:self.understanding.store_analysis(
                        tx,context=context,model_version=model_version,failure_code="invalid_output",claim=claim,retry=processing_retry,prompt_version=prompt_version))
            if provider_error is not None:
                # Release the analysis claim with a durable failure receipt, then
                # retain the provider's retry/defer/ambiguity semantics for the worker.
                from .inference import InferenceTransportError
                if isinstance(provider_error, InferenceTransportError):
                    raise provider_error
                raise RuntimeError("Understanding provider failed") from None
            if analysis["status"]!="succeeded":
                return {"status":"failed","analysis_id":analysis["id"],"failure":analysis["failure_code"]}
            analysis_id=analysis["id"]
        return self.project_message_analysis(analysis_id)

    def project_message_analysis(self,analysis_id):
        """Projection and its current processing outcome commit atomically."""
        with self.executor.read() as con:
            analysis=self.understanding.get_analysis(con,analysis_id)
            issues=[self.understanding.processing_for_source(con,source["source_id"],source["revision"])
                    for source in analysis["descriptor"]["sources"]]
        projection=analysis["descriptor"].get("context",{}).get("mail_projection")
        principal=Principal("understanding-worker","worker",frozenset({"project_analysis"}))
        attempt=digest({"analysis_id":analysis_id,"issues":[(i["issue_id"],i["version"]) for i in issues if i]})
        def project(tx):
            if not self.understanding.processing_projection_applicable(tx.connection,analysis_id):
                self._finish_work(tx,"understanding","project_analysis",analysis_id)
                return {"status":"superseded","analysis_id":analysis_id,"proposals":[]}
            if analysis["status"]!="succeeded":
                raise DomainError("invalid_input","Only successful analysis can be projected")
            if not projection:
                raise DomainError("dependency_unresolved","Analysis has no captured mail projection context")
            current=self.correspondence.get(tx.connection,projection["message_id"])
            if current["revision"]!=projection["revision"]:
                self.understanding.supersede_processing_source(tx,source_id=current["id"],latest_revision=current["revision"])
                self._finish_work(tx,"understanding","project_analysis",analysis_id)
                return {"status":"superseded","analysis_id":analysis_id,"proposals":[]}
            result=self.understanding.project_requests(tx,analysis_id=analysis_id,
                application_id=projection["target_application_id"],message_id=projection["message_id"],
                association_required=projection["association_required"])
            result["proposals"] = self.workflows.reconcile_projected_changes(tx, result["proposals"])
            self.understanding.record_projection(tx,analysis_id=analysis_id,status="succeeded")
            self._finish_work(tx,"understanding","project_analysis",analysis_id)
            self._finish_work(tx,"correspondence","understand_message",projection["revision"])
            return result
        context=CommandContext(principal,"projection:"+attempt,"inferred")
        try:
            return self.executor.run(context,"project_analysis",{"analysis_id":analysis_id},project)
        except Exception:
            # Preserve a safe diagnostic only; model/provider text is never history.
            failure=CommandContext(principal,"projection-failure:"+attempt,"inferred")
            self.executor.run(failure,"project_analysis",{"analysis_id":analysis_id,"failure_code":"projection_failed"},
                lambda tx:self.understanding.record_projection(tx,analysis_id=analysis_id,status="failed",failure_code="projection_failed"))
            raise


_ANALYSIS_HEARTBEAT_SECONDS = 30


@contextmanager
def _analysis_heartbeat(renew_claim, worker_heartbeat):
    """Keep both leases alive during polling and blocking model transports.

    The renewal thread owns no model authority. It can extend only the persisted
    claim token, and a lost lease makes the model result unusable.
    """
    from contextlib import nullcontext
    from threading import Event, Lock, Thread
    from .inference.usage import current_scope, invocation_scope
    parent = current_scope()
    worker_heartbeat = worker_heartbeat or (parent.heartbeat if parent else None)
    stopped, lost, lock = Event(), Event(), Lock()

    def pulse():
        with lock:
            if lost.is_set(): return False
            try:
                if worker_heartbeat is not None and not worker_heartbeat():
                    lost.set()
                    return False
                if not renew_claim():
                    lost.set()
                    return False
            except Exception:
                lost.set()
                return False
            return True

    def keep_alive():
        while not stopped.wait(_ANALYSIS_HEARTBEAT_SECONDS):
            if not pulse(): return

    if not pulse():
        raise RuntimeError("Understanding analysis lease was lost")
    thread = Thread(target=keep_alive, name="understanding-lease", daemon=True)
    thread.start()
    scope = invocation_scope(parent.db_path,parent.work_id,parent.revision,policy=parent.policy,
        clock=parent.clock,heartbeat=pulse) if parent else nullcontext()
    try:
        with scope:
            yield
            if not pulse():
                raise RuntimeError("Understanding analysis lease was lost")
    finally:
        stopped.set()
        thread.join()
