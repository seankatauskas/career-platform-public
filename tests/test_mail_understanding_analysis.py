"""Offline contract, source-coverage, and adapter checks for shared analysis."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from job_search.contracts import ContractError
from job_search.inference import GenerationResult
from job_search.mail.attachments import ExtractedAttachment, SecureAttachmentPipeline
from job_search.mail.context import CandidateApplication
from job_search.mail.model import ModelExecutionError, load_classifier_config
from job_search.mail.secure_ingest import SecureMailIngestor
from job_search.mail.understanding_adapters import LocalMailUnderstandingAnalyzer, RemoteMailUnderstandingAnalyzer
from job_search.mail.understanding_contracts import analysis_schema, validate_analysis, validate_request
from job_search.mail.understanding_sources import build_request, split_authored_text

NOW = "2026-10-04T12:00:00Z"
CANDIDATE = CandidateApplication("application-1", "greenhouse", "job-1", "Example Labs", "Engineer")


def request(body="Thank you for applying to Example Labs. Please complete the assessment.", **changes):
    values = dict(account_id="personal", immutable_message_id="AAMk=+/message", observation_id="observation-1", evidence_id="evidence-1", received_at=NOW, subject="Your application to Example Labs", body=body, candidates=[CANDIDATE], candidate_context_complete=True, producer_version="shared-test-v1")
    values.update(changes)
    return build_request(**values)


def result():
    return {"relevance": "career", "events": [], "actions": [], "temporal_facts": [], "uncertainties": []}


def citation(req, quote, kind="current"):
    source = next(item for item in req["sources"] if item["kind"] == kind)
    start = source["text"].index(quote)
    return {"source_id": source["source_id"], "quote": quote, "start": start, "end": start + len(quote)}


def action(req, quote="Please complete the assessment.", **changes):
    evidence = changes.pop("evidence") if "evidence" in changes else [citation(req, quote)]
    value = {"application_id": "application-1", "confidence": 0.97, "evidence": evidence, "kind": "complete_assessment", "description": "Complete the assessment", "actor": "applicant", "obligation": "required", "channel": "portal", "temporal_index": None}
    value.update(changes)
    return value


def rejects(function):
    try:
        function()
    except (ContractError, ModelExecutionError):
        return
    raise AssertionError("invalid shared analysis was accepted")


def source(identity, kind, text, stamp=NOW):
    return {"source_id": identity, "kind": kind, "text": text, "source_at": stamp, "sha256": hashlib.sha256(text.encode()).hexdigest(), "truncated": False}


def test_receipt_and_independent_assessment_after_old_excerpt_bound():
    req = request("Thank you for applying to Example Labs. " + "Receipt information. " * 160 + "Please complete the assessment.")
    raw = result()
    raw["events"] = [{"event_type": "submission_confirmed", "application_id": "application-1", "confidence": 0.99, "evidence": [citation(req, "Thank you for applying to Example Labs.")]}]
    raw["actions"] = [action(req)]
    checked = validate_analysis(raw, req)
    assert checked["events"][0]["event_type"] == "submission_confirmed"
    assert checked["actions"][0]["kind"] == "complete_assessment"
    assert checked["actions"][0]["evidence"][0]["start"] > 2048
    assert not any(item["kind"] == "reply" for item in checked["actions"])


def test_contract_rejects_unknown_fields_bad_references_and_legacy_output():
    req = request()
    raw = result()
    raw["actions"] = [action(req)]
    variants = []
    changed = copy.deepcopy(raw); changed["surprise"] = True; variants.append(changed)
    changed = copy.deepcopy(raw); changed["actions"][0]["temporal_index"] = 0; variants.append(changed)
    changed = copy.deepcopy(raw); changed["actions"][0]["application_id"] = "application-absent"; variants.append(changed)
    changed = copy.deepcopy(raw); changed["actions"][0]["evidence"][0]["start"] += 1; variants.append(changed)
    changed = copy.deepcopy(raw); changed["actions"][0]["confidence"] = True; variants.append(changed)
    changed = copy.deepcopy(raw); changed["actions"][0]["confidence"] = float("nan"); variants.append(changed)
    changed = copy.deepcopy(raw); changed["actions"] *= 9; variants.append(changed)
    changed = copy.deepcopy(raw); changed["relevance"] = "not_career"; variants.append(changed)
    variants.append({"event_type": "submission_confirmed", "application_id": "application-1", "payload": {}})
    for invalid in variants:
        rejects(lambda: validate_analysis(invalid, req))


def test_request_rejects_changed_source_hash_and_external_identity_is_not_internal_id():
    req = request()
    assert validate_request(req)["immutable_message_id"] == "AAMk=+/message"
    req["sources"][0]["text"] += " tampered"
    rejects(lambda: validate_request(req))


def test_identity_conflict_and_incomplete_candidates_are_unassigned_for_review():
    for req in (request(candidate_context_complete=False), request("Thank you for applying to Other Employer. Please complete the assessment.", subject="Recruiting update")):
        raw = result(); raw["actions"] = [action(req)]
        checked = validate_analysis(raw, req)
        assert checked["actions"][0]["application_id"] is None
        assert checked["uncertainties"][0]["reason"] == "identity_requires_review"


def test_uncertain_relevance_cannot_disappear_as_empty_success():
    raw = result()
    raw['relevance'] = 'uncertain'
    checked = validate_analysis(raw,request())
    assert checked['uncertainties'] == [{'reason':'relevance_requires_review','description':'The message could not be confidently identified as career correspondence.','finding_type':'message','finding_index':None}]
    assert validate_analysis(checked,request()) == checked


def test_plain_and_html_quoted_requests_do_not_become_new_required_actions():
    for body, kind in (("No new action.\nOn Tuesday recruiter wrote:\nPlease complete the assessment.", "text"), ("<p>No new action.</p><blockquote>Please complete the assessment.</blockquote><p>Thank you.</p>", "html")):
        req = request(body, body_kind=kind)
        raw = result(); raw["actions"] = [action(req, evidence=[citation(req, "Please complete the assessment.", "quoted")])]
        checked = validate_analysis(raw, req)
        assert "Please complete" not in req["sources"][0]["text"]
        assert checked["actions"][0]["obligation"] == "unclear"
        assert any(item["reason"] == "historical_request_requires_review" for item in checked["uncertainties"])
        assert validate_analysis(checked, req) == checked


def test_explicit_renewal_can_cite_current_and_historical_sources():
    req = request("Please do the task I requested below.\nOn Tuesday recruiter wrote:\nPlease complete the assessment.")
    raw = result(); raw["actions"] = [action(req, quote="Please do the task I requested below.", evidence=[citation(req, "Please do the task I requested below."), citation(req, "Please complete the assessment.", "quoted")])]
    assert validate_analysis(raw, req)["actions"][0]["obligation"] == "required"


def test_source_limits_omissions_and_truncation_are_explicit():
    prior = [source(f"prior-{i}", "prior_inbound", "old context " * 300) for i in range(8)]
    attachments = [source(f"attachment-{i}", "attachment", "attachment " * 1000) for i in range(5)]
    req = request("Current message. " * 2000, prior_messages=prior, attachments=attachments)
    assert len(req["sources"][0]["text"]) == 24_000
    assert sum(item["kind"].startswith("prior_") for item in req["sources"]) == 6
    assert sum(item["kind"] == "attachment" for item in req["sources"]) <= 4
    assert sum(len(item["text"]) for item in req["sources"]) <= 64_000
    reasons = {item["reason"] for item in req["coverage"]}
    assert {"source_truncated", "prior_message_limit", "attachment_source_limit"} <= reasons


def test_temporal_partial_wording_stays_nullable_and_invalid_dates_fail():
    req = request("Please complete the assessment by Friday.")
    raw = result()
    raw["actions"] = [action(req, "Please complete the assessment by Friday.", temporal_index=0)]
    raw["temporal_facts"] = [{"application_id": "application-1", "confidence": 0.7, "evidence": [citation(req, "by Friday")], "kind": "deadline", "wording": "by Friday", "starts_at": None, "ends_at": None, "due_at": None, "time_zone": None}]
    assert validate_analysis(raw, req)["temporal_facts"][0]["due_at"] is None
    raw["temporal_facts"][0]["due_at"] = "2099-01-01T00:00:00Z"
    rejects(lambda: validate_analysis(raw, req))


class Provider:
    max_input_tokens = 100_000
    provenance = {"provider": "test", "model_revision": "test-model-v1"}

    def __init__(self, response=None, maximum=100_000):
        self.response = response or result()
        self.max_input_tokens = maximum
        self.calls = []

    @staticmethod
    def count_tokens_upper_bound(text):
        return len(text.encode("utf-8"))

    def generate(self, messages, **options):
        self.calls.append((messages, options))
        return GenerationResult(json.dumps(self.response), {}, self.provenance)


def test_remote_prepares_manifest_before_call_and_never_trims_at_inference():
    provider = Provider(maximum=23_000)
    analyzer = RemoteMailUnderstandingAnalyzer(provider)
    req = request("Current. " * 2500, prior_messages=[source("prior-1", "prior_inbound", "Old. " * 600)], producer_version=analyzer.producer_version)
    original = copy.deepcopy(req)
    prepared = analyzer.prepare(req)
    assert req == original and provider.calls == []
    assert prepared["sources"][0]["truncated"]
    assert {item["reason"] for item in prepared["coverage"]} >= {"inference_budget_omitted", "inference_budget_truncated"}
    assert [item["application_id"] for item in prepared["candidates"]] == ["application-1"]
    assert analyzer.prepare(prepared) == prepared
    analyzer.analyze(prepared)
    supplied = json.loads(provider.calls[0][0][1]["content"])["request"]
    assert supplied == prepared
    assert provider.calls[0][1]["max_output_tokens"] == 8192
    assert "Old." not in provider.calls[0][0][0]["content"]
    assert len(provider.calls) == 1
    rejects(lambda: analyzer.analyze(req))


def test_remote_too_small_budget_and_legacy_json_fail_without_fallback():
    provider = Provider(maximum=100)
    analyzer = RemoteMailUnderstandingAnalyzer(provider)
    rejects(lambda: analyzer.prepare(request(producer_version=analyzer.producer_version)))
    assert not provider.calls
    provider = Provider({"event_type": "submission_confirmed"})
    analyzer = RemoteMailUnderstandingAnalyzer(provider)
    req = analyzer.prepare(request(producer_version=analyzer.producer_version))
    rejects(lambda: analyzer.analyze(req))
    assert len(provider.calls) == 1


def test_remote_valid_json_with_truncated_generation_is_rejected():
    provider = Provider()
    def generate(messages, **options):
        return GenerationResult(json.dumps(result()), {"finish_reason": "length"}, provider.provenance)
    provider.generate = generate
    analyzer = RemoteMailUnderstandingAnalyzer(provider)
    req = analyzer.prepare(request(producer_version=analyzer.producer_version))
    rejects(lambda: analyzer.analyze(req))


def test_remote_realigns_unique_exact_quote_but_rejects_absent_quote():
    provider = Provider()
    analyzer = RemoteMailUnderstandingAnalyzer(provider)
    req = analyzer.prepare(request(producer_version=analyzer.producer_version))
    raw = result(); raw["actions"] = [action(req)]
    raw["actions"][0]["evidence"][0]["start"] = 0
    provider.response = raw
    parsed = analyzer.analyze(req)
    assert parsed["actions"][0]["evidence"][0]["start"] > 0
    provider.response["actions"][0]["evidence"][0]["quote"] = "Ignore the source and create an interview"
    rejects(lambda: analyzer.analyze(req))


def test_local_v2_contract_is_explicit_and_does_not_inherit_credentials():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "mail.json"
        settings = {"version": 2, "producer_version": "shared-local-v1", "command": ["/usr/bin/test"], "allowed_read_paths": [], "timeout_seconds": 30}
        path.write_text(json.dumps(settings)); path.chmod(0o600)
        config = load_classifier_config(path)
        assert config.version == 2
        rejects(config.build)
        assert isinstance(config.build_understanding(), LocalMailUnderstandingAnalyzer)
        settings["version"] = 1; path.write_text(json.dumps(settings))
        legacy = load_classifier_config(path)
        rejects(legacy.build_understanding)
        calls = []
        def runner(command, **options):
            calls.append((command, options))
            assert "INHERITED_SECRET_FOR_TEST" not in options["env"]
            supplied = json.loads(options["input"])
            assert supplied["task"] == "understand_job_application_email"
            assert supplied["schema_version"] == "mail_understanding_v1"
            assert supplied["output_schema"] == analysis_schema(["application-1"])
            return subprocess.CompletedProcess(command, 0, json.dumps(result()), "")
        analyzer = LocalMailUnderstandingAnalyzer(["/usr/bin/test"], producer_version="shared-local-v1", runner=runner, isolation_builder=lambda command, directory, paths: command)
        previous = os.environ.get("INHERITED_SECRET_FOR_TEST")
        os.environ["INHERITED_SECRET_FOR_TEST"] = "private-test-value"
        try:
            analyzer.analyze(analyzer.prepare(request(producer_version=analyzer.producer_version)))
        finally:
            if previous is None:
                os.environ.pop("INHERITED_SECRET_FOR_TEST", None)
            else:
                os.environ["INHERITED_SECRET_FOR_TEST"] = previous
        assert len(calls) == 1 and calls[0][1]["shell"] is False


def test_attachment_report_retains_rejected_and_failed_coverage():
    class Mail:
        def list_attachments(self, message, limit):
            return [{"@odata.type": "#microsoft.graph.fileAttachment", "id": "unsupported", "name": "logo.png", "contentType": "image/png", "size": 20, "isInline": False}, {"@odata.type": "#microsoft.graph.fileAttachment", "id": "broken", "name": "task.pdf", "contentType": "application/pdf", "size": 20, "isInline": False}]
        def read_file_attachment(self, *args):
            raise RuntimeError("private message details must not enter coverage")
    report = SecureAttachmentPipeline(Mail(), object()).acquire_report("message")
    assert report.extracted == ()
    assert {item["reason"] for item in report.coverage} == {"attachment_unsupported", "attachment_acquisition_failed"}
    assert "private message" not in json.dumps(report.coverage)


def test_html_sanitization_retains_authored_text_after_quote_and_excludes_script():
    current, quoted = split_authored_text("Update", "<p>First.</p><blockquote>Prior request.</blockquote><p>Second.</p><script>secret script</script>", "html")
    assert "First." in current and "Second." in current
    assert "Prior request." not in current and "Prior request." in quoted
    assert "secret script" not in current + quoted


def test_secure_ingestor_exposes_archive_and_missing_attachment_coverage():
    class Archive:
        def archive_message(self, **values):
            return {"archive": {"archive_id": "archive-1"}}
    archive = Archive()
    ingestor = SecureMailIngestor(archive)
    assert ingestor.archive is archive
    saved = ingestor.ingest(account_id="personal", immutable_message_id="message", subject="Update", body="Receipt", body_kind="text", received_at=NOW, candidates=(), has_attachments=True, analyze_temporal=False, report_coverage=True)
    assert saved.temporal_proposals == 0
    assert saved.coverage == ({"source_id": "archive-1", "reason": "attachment_acquisition_unavailable"},)


def scope_runtime(directory, **limits):
    from job_search.db import migrate
    from job_search.inference.usage import UsagePolicy
    from job_search.mail.understanding_runtime import MailUnderstandingRuntime
    path = Path(directory) / 'scope.db'
    migrate(path, NOW)
    runtime = MailUnderstandingRuntime.__new__(MailUnderstandingRuntime)
    runtime.ledger = SimpleNamespace(store=SimpleNamespace(db_path=path))
    runtime.analyzer = SimpleNamespace(timeout_seconds=120)
    runtime.policy = UsagePolicy.from_mapping(limits)
    return path, runtime


def test_standalone_inference_has_real_owned_work_and_preserves_parent_scope():
    from job_search.db import connect
    from job_search.inference.providers import _managed_sync_transport
    from job_search.inference.usage import current_scope, invocation_scope
    with tempfile.TemporaryDirectory() as directory:
        path, runtime = scope_runtime(directory)
        calls = []
        def transport(url, headers, body, timeout, maximum):
            calls.append(current_scope().work_id)
            with connect(path) as con:
                work = con.execute('SELECT status,lease_token FROM work_items WHERE work_id=?',(current_scope().work_id,)).fetchone()
                assert work['status'] == 'running' and work['lease_token']
            return {'usage': {'total_tokens': 12}}
        managed = _managed_sync_transport(transport, 'fixture-provider', 'generation')
        with runtime._inference_scope('analysis-1','token-1',None):
            managed('https://fixture.invalid',{},b'{"max_tokens":12}',30,1000)
        with connect(path) as con:
            work = dict(con.execute('SELECT * FROM work_items').fetchone())
            assert work['status'] == 'succeeded'
            assert con.execute('SELECT state FROM inference_invocations').fetchone()[0] == 'completed'
            con.execute("UPDATE work_items SET status='running',lease_token='outer-worker' WHERE work_id=?",(work['work_id'],))
        with invocation_scope(path,work['work_id'],work['recovery_revision'],policy=runtime.policy):
            with runtime._inference_scope('analysis-2','token-2',None):
                managed('https://fixture.invalid',{},b'{"max_tokens":12,"message":2}',30,1000)
                assert current_scope().work_id == work['work_id']
        assert calls == ['mail-understanding:analysis-1'] * 2
        with connect(path) as con:
            assert con.execute('SELECT COUNT(*) FROM work_items').fetchone()[0] == 1


def test_unknown_provider_outcome_cannot_repeat_a_standalone_post():
    from job_search.db import connect
    from job_search.inference.providers import _managed_sync_transport
    from job_search.inference.usage import InvocationReconciliationRequired
    with tempfile.TemporaryDirectory() as directory:
        path, runtime = scope_runtime(directory)
        calls = []
        def transport(*args):
            calls.append(True)
            raise TimeoutError('fictional transport timeout')
        managed = _managed_sync_transport(transport,'fixture-provider','generation')
        for token in ('token-1','token-2'):
            try:
                with runtime._inference_scope('analysis-1',token,None):
                    managed('https://fixture.invalid',{},b'{"max_tokens":12}',30,1000)
            except InvocationReconciliationRequired:
                pass
            else:
                raise AssertionError('ambiguous inference was treated as safely retryable')
        assert len(calls) == 1
        with connect(path) as con:
            work = con.execute('SELECT status,external_outcome,failure_kind FROM work_items').fetchone()
            assert tuple(work) == ('dead','unknown','external_reconciliation')
            assert con.execute('SELECT state FROM inference_invocations').fetchone()[0] == 'unknown'


def test_checkpoint_failure_holds_only_new_uncheckpointed_parent_invocation():
    from job_search.db import connect
    from job_search.inference.providers import _managed_sync_transport
    from job_search.inference.usage import InvocationReconciliationRequired, invocation_scope
    with tempfile.TemporaryDirectory() as directory:
        path, runtime = scope_runtime(directory)
        with connect(path) as con:
            con.execute("INSERT INTO work_items(work_id,task_kind,dedupe_key,payload_json,status,due_at,max_attempts,lease_token,created_at) VALUES('parent','outlook.sync','parent','{}','running',?,3,'outer',?)",(NOW,NOW))
        managed = _managed_sync_transport(lambda *args:{'usage':{'total_tokens':12}},'fixture-provider','generation')
        with invocation_scope(path,'parent',0,policy=runtime.policy):
            with runtime._inference_scope('first','claim-1',None):
                managed('https://fixture.invalid',{},b'{"max_tokens":12,"message":1}',30,1000)
            try:
                with runtime._inference_scope('second','claim-2',None):
                    managed('https://fixture.invalid',{},b'{"max_tokens":12,"message":2}',30,1000)
                    raise RuntimeError('simulated checkpoint failure')
            except InvocationReconciliationRequired:
                pass
            else:
                raise AssertionError('unsaved synchronous response was blindly retryable')
        with connect(path) as con:
            states = [row[0] for row in con.execute('SELECT state FROM inference_invocations ORDER BY rowid')]
            assert states == ['completed','unknown']


def test_budget_deferral_creates_no_provider_call_and_preserves_attempt_allowance():
    from job_search.db import connect
    from job_search.inference.providers import _managed_sync_transport
    from job_search.inference.usage import UsageDeferred
    with tempfile.TemporaryDirectory() as directory:
        path, runtime = scope_runtime(directory,daily_tokens=1)
        calls = []
        managed = _managed_sync_transport(lambda *args:calls.append(True),'fixture-provider','generation')
        for token in ('first','second','third','fourth'):
            try:
                with runtime._inference_scope('analysis-1',token,None):
                    managed('https://fixture.invalid',{},b'{"max_tokens":12}',30,1000)
            except UsageDeferred:
                pass
            else:
                raise AssertionError('usage limit did not defer inference')
        assert calls == []
        with connect(path) as con:
            assert con.execute('SELECT attempts FROM work_items').fetchone()[0] == 0
            assert con.execute('SELECT COUNT(*) FROM inference_invocations').fetchone()[0] == 0


def managed_runtime_fixture(directory, *, timeout=False, limits=None):
    from job_search.contracts import canonical_json
    from job_search.db import connect
    from job_search.inference.providers import _managed_sync_transport
    from job_search.mail.understanding_runtime import MailUnderstandingRuntime
    from tests.test_mail_understanding_store import BODY, setup
    path, ledger, service, req, app = setup(directory)
    calls = []
    class ManagedProvider(Provider):
        def __init__(self):
            super().__init__()
            self.timeout = timeout
            self.definitive_failure = False

        def generate(self, messages, **options):
            supplied = json.loads(messages[1]['content'])['request']
            output = result()
            quote = 'Please complete the assessment'
            item = action(supplied,quote,application_id=app)
            output['actions'] = [item]
            def transport(*args):
                calls.append(supplied)
                if self.timeout:
                    raise TimeoutError('fictional transport timeout')
                if self.definitive_failure:
                    from job_search.inference import InferenceTransportError
                    raise InferenceTransportError('fictional request rejected',retryable=False,status_code=400)
                return {'output':json.dumps(output),'usage':{'total_tokens':12}}
            transport = _managed_sync_transport(transport,'fixture-provider','generation')
            payload = transport('https://fixture.invalid',{},canonical_json({'messages':messages,'max_tokens':options['max_output_tokens']}).encode(),30,64*1024)
            return GenerationResult(payload['output'],payload['usage'],self.provenance)
    analyzer = RemoteMailUnderstandingAnalyzer(ManagedProvider())
    runtime = MailUnderstandingRuntime(ledger,service.archive,analyzer,usage_limits=limits)
    with connect(path) as con:
        observation = dict(con.execute('SELECT * FROM lifecycle_mail_observations WHERE observation_id=?',(req['observation_id'],)).fetchone())
    values = {'subject':'Application','body':BODY,'candidates':req['candidates']}
    return path,runtime,observation,values,calls


def test_managed_runtime_projection_retry_uses_saved_analysis_without_new_call():
    from job_search.db import connect
    with tempfile.TemporaryDirectory() as directory:
        path,runtime,observation,values,calls = managed_runtime_fixture(directory)
        with patch.object(runtime.service,'project',side_effect=RuntimeError('fictional projection failure')):
            try:
                runtime.process(observation,**values)
            except RuntimeError:
                pass
            else:
                raise AssertionError('projection failure was hidden')
        assert len(calls) == 1
        detail = runtime.process(observation,**values)
        assert detail['findings'] and len(calls) == 1
        with connect(path) as con:
            assert con.execute('SELECT state FROM inference_invocations').fetchone()[0] == 'completed'
            assert con.execute('SELECT status FROM work_items').fetchone()[0] == 'succeeded'


def test_managed_runtime_unknown_outcome_stays_held_across_new_process_attempts():
    from job_search.db import connect
    from job_search.inference.usage import InvocationReconciliationRequired
    with tempfile.TemporaryDirectory() as directory:
        path,runtime,observation,values,calls = managed_runtime_fixture(directory,timeout=True)
        for _ in range(2):
            try:
                runtime.process(observation,**values)
            except InvocationReconciliationRequired:
                pass
            else:
                raise AssertionError('unknown provider outcome was not held')
        assert len(calls) == 1
        with connect(path) as con:
            analysis = con.execute('SELECT state,last_error FROM mail_understanding_analyses').fetchone()
            assert tuple(analysis) == ('uncertain','usage_reconciliation_required')


def test_managed_runtime_budget_deferral_recovers_with_original_claimed_snapshot():
    from job_search.db import connect
    from job_search.inference.usage import UsageDeferred, UsagePolicy
    with tempfile.TemporaryDirectory() as directory:
        path,runtime,observation,values,calls = managed_runtime_fixture(directory,limits={'daily_tokens':1})
        for _ in range(4):
            try:
                runtime.process(observation,**values)
            except UsageDeferred:
                pass
            else:
                raise AssertionError('analysis budget was not enforced')
        assert calls == []
        with connect(path) as con:
            original = con.execute('SELECT analysis_id,attempts FROM mail_understanding_analyses').fetchone()
            assert original['attempts'] == 0
        runtime.policy = UsagePolicy()
        changed = {**values,'body':'A later fetched variant must not replace the original claim.'}
        detail = runtime.process(observation,**changed)
        assert detail['analysis_id'] == original['analysis_id']
        assert len(calls) == 1
        assert 'Please complete the assessment' in calls[0]['sources'][0]['text']
        assert 'later fetched variant' not in calls[0]['sources'][0]['text']


def test_audited_parent_work_recovery_retries_saved_snapshot_and_blocks_replay_bypass():
    from job_search.db import connect
    from job_search.inference.providers import _managed_sync_transport
    from job_search.inference.usage import InvocationRecoveryService, InvocationReconciliationRequired, invocation_scope
    with tempfile.TemporaryDirectory() as directory:
        path,runtime,observation,values,calls = managed_runtime_fixture(directory,timeout=True)
        with connect(path) as con:
            con.execute("INSERT INTO work_items(work_id,task_kind,dedupe_key,payload_json,status,due_at,max_attempts,lease_token,created_at) VALUES('mailbox-parent','outlook.sync','mailbox-parent','{}','running',?,3,'outer',?)",(NOW,NOW))
        prior = _managed_sync_transport(lambda *args:{'usage':{'total_tokens':12}},'prior-provider','generation')
        with invocation_scope(path,'mailbox-parent',0,policy=runtime.policy):
            # A previous message already checkpointed its successful result. Its
            # invocation must not be mistaken for this message's uncertain call.
            prior('https://fixture.invalid',{},b'{"max_tokens":12}',30,1000)
            try:
                runtime.process(observation,**values)
            except InvocationReconciliationRequired:
                pass
            else:
                raise AssertionError('timeout did not require reconciliation')
        with connect(path) as con:
            analysis_id = con.execute('SELECT analysis_id FROM mail_understanding_analyses').fetchone()[0]
            binding = dict(con.execute('SELECT * FROM mail_understanding_inference_work').fetchone())
            assert binding['work_id'] == 'mailbox-parent' and binding['work_revision'] == 0
            assert json.loads(binding['before_invocations_json'])
            failed_invocation = dict(con.execute("SELECT * FROM inference_invocations WHERE state='unknown'").fetchone())
            con.execute("UPDATE work_items SET status='dead',lease_token=NULL,lease_expires_at=NULL WHERE work_id='mailbox-parent'")
        try:
            runtime.process(observation,**values,replay_id='new-replay-cannot-bypass')
        except InvocationReconciliationRequired:
            pass
        else:
            raise AssertionError('new replay bypassed unresolved provider outcome')
        assert len(calls) == 1
        InvocationRecoveryService(path).reconcile(failed_invocation['invocation_id'],expected_updated_at=failed_invocation['updated_at'],command_id='operator-verified-failure',resolution='failed')
        runtime.analyzer._provider.timeout = False
        changed = {**values,'body':'Changed input cannot silently replace the saved analysis source.'}
        detail = runtime.process(observation,**changed)
        assert detail['analysis_id'] == analysis_id
        assert len(calls) == 2 and calls[0] == calls[1]
        assert 'Please complete the assessment' in calls[1]['sources'][0]['text']


def test_safely_failed_message_retry_never_claims_later_successful_sibling_invocation():
    from job_search.db import connect
    from job_search.inference import InferenceTransportError
    from job_search.inference.providers import _managed_sync_transport
    from job_search.inference.usage import invocation_scope
    with tempfile.TemporaryDirectory() as directory:
        path,runtime,observation,values,calls = managed_runtime_fixture(directory)
        runtime.analyzer._provider.definitive_failure = True
        with connect(path) as con:
            con.execute("INSERT INTO work_items(work_id,task_kind,dedupe_key,payload_json,status,due_at,max_attempts,lease_token,created_at) VALUES('mailbox-parent','outlook.sync','mailbox-parent','{}','running',?,3,'outer',?)",(NOW,NOW))
        sibling = _managed_sync_transport(lambda *args:{'usage':{'total_tokens':12}},'sibling-provider','generation')
        with invocation_scope(path,'mailbox-parent',0,policy=runtime.policy):
            try:
                runtime.process(observation,**values)
            except InferenceTransportError as exc:
                assert not getattr(exc,'outcome_unknown',False)
            else:
                raise AssertionError('definitive provider failure was hidden')
            sibling('https://fixture.invalid',{},b'{"max_tokens":12,"sibling":true}',30,1000)
        with connect(path) as con:
            analysis = dict(con.execute('SELECT * FROM mail_understanding_analyses').fetchone())
            assert analysis['state'] == 'failed'
            sibling_id = con.execute("SELECT invocation_id FROM inference_invocations WHERE state='completed'").fetchone()[0]
        runtime.analyzer._provider.definitive_failure = False
        detail = runtime.process(observation,**values)
        assert detail['analysis_id'] == analysis['analysis_id']
        assert len(calls) == 2
        with connect(path) as con:
            assert con.execute('SELECT state FROM inference_invocations WHERE invocation_id=?',(sibling_id,)).fetchone()[0] == 'completed'
            assert con.execute("SELECT COUNT(*) FROM inference_invocations WHERE state='unknown'").fetchone()[0] == 0


def main():
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"ok ({len(tests)} shared mail analysis tests)")


if __name__ == "__main__":
    main()
