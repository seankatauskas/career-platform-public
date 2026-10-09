"""Shared mail orchestration: prepare once, checkpoint, then resume projections."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
from typing import Any

from ..contracts import ContractError, ConflictError, MutationContext, canonical_json, parse_utc, payload_sha256, utc_now
from ..db import connect
from ..inference.usage import InvocationReconciliationRequired, UsagePolicy, current_scope, invocation_scope
from ..inference.contracts import InferenceResponseRejected
from .model import ModelExecutionError
from .understanding_evaluation import load_report


class MailUnderstandingRuntime:
    def __init__(self, ledger, archive, analyzer, *, mode='shared', source_scope='current',
                 allow_attachments=True, evaluation_report=None, usage_limits=None):
        from .understanding_store import MailUnderstandingService
        if mode not in {'legacy','shadow','shared','paused'}:
            raise ContractError('invalid mail understanding mode')
        if source_scope not in {'current', 'thread', 'thread_attachments'}:
            raise ContractError('invalid mail understanding source scope')
        self.ledger, self.archive, self.analyzer = ledger, archive, analyzer
        self.mode, self.source_scope = mode, source_scope
        self.allow_attachments = allow_attachments
        self.report = evaluation_report
        self.policy = UsagePolicy.from_mapping(usage_limits)
        self.service = MailUnderstandingService(ledger, archive)

    def _context_sources(self, observation):
        prior, attachments, coverage = [], [], []
        with connect(self.ledger.store.db_path) as con:
            rows = con.execute(
                "SELECT m.*,a.truncated AS archive_truncated FROM lifecycle_mail_observations m "
                "LEFT JOIN mail_archive a ON a.archive_id=m.archive_id "
                "WHERE m.account_id=? AND m.conversation_ref=? "
                "AND m.observation_id<>? AND m.source_at<=? AND m.direction IN ('inbound','outbound') "
                "ORDER BY m.source_at DESC,m.observation_id DESC LIMIT 7",
                (observation['account_id'], observation['conversation_ref'], observation['observation_id'], observation['source_at'])
            ).fetchall() if observation.get('conversation_ref') else []
            attachment_rows = con.execute(
                'SELECT attachment_record_id,archive_id,extracted_sha256 FROM mail_archive_attachments WHERE archive_id=? ORDER BY attachment_record_id LIMIT 5',
                (observation.get('archive_id'),)).fetchall()
        if self.source_scope in {'thread','thread_attachments'}:
            if len(rows) >= 7:
                coverage.append(dict(source_id='history',reason='history_context_limit'))
            for row in rows:
                item = dict(row)
                source_id = 'history:' + item['observation_id']
                if not item.get('archive_id'):
                    coverage.append(dict(source_id=source_id,reason='history_archive_missing'))
                    continue
                try:
                    text = self.archive.read_message(item['archive_id'])
                except ContractError:
                    coverage.append(dict(source_id=source_id,reason='history_archive_unavailable'))
                    continue
                prior.append(dict(source_id=source_id,kind='prior_' + item['direction'],text=text,
                                  source_at=item['source_at'],archive_id=item['archive_id'],truncated=bool(item['archive_truncated'])))
        elif rows:
            coverage.append(dict(source_id='history',reason='source_scope_disabled'))
        if self.source_scope == 'thread_attachments' and self.allow_attachments:
            for row in attachment_rows:
                source_id = 'attachment:' + row['attachment_record_id']
                try:
                    text = self.archive.read_attachment_text(row['attachment_record_id'])
                except ContractError:
                    coverage.append(dict(source_id=source_id,reason='attachment_archive_unavailable'))
                    continue
                attachments.append(dict(source_id=source_id,kind='attachment',text=text,source_at=observation['source_at'],
                                        attachment_record_id=row['attachment_record_id'],archive_id=row['archive_id']))
        elif attachment_rows:
            coverage.append(dict(source_id='attachments',reason='source_scope_disabled'))
        return prior, attachments, coverage

    def process(self, observation, *, subject, body, body_kind='text', candidates=(),
                candidate_context_complete=True, coverage=(), heartbeat=None, replay_id=None):
        if self.mode in {'paused','legacy'} and replay_id is None:
            return {'state':'paused'}
        mode = 'replay' if replay_id else self.mode
        saved = self.service.find_for_message(observation['account_id'],observation['immutable_message_id'],mode=mode,replay_id=replay_id)
        if saved:
            if saved['evidence_id'] != observation['evidence_id']:
                raise ConflictError('saved mail analysis belongs to different evidence')
            if heartbeat is not None and not heartbeat():
                raise RuntimeError('mail understanding worker lease lost')
            return self.service.project(saved['analysis_id'],self._ctx('project',saved['analysis_id']),evaluation_report=self.report)
        if self.analyzer is None:
            raise ContractError('shared mail analyzer unavailable; message remains pending')
        from .understanding_sources import build_request
        prior, attachments, gaps = self._context_sources(observation)
        request = build_request(
            account_id=observation['account_id'], immutable_message_id=observation['immutable_message_id'],
            observation_id=observation['observation_id'],evidence_id=observation['evidence_id'],
            received_at=observation['source_at'],subject=subject,body=body,body_kind=body_kind,
            candidates=candidates,candidate_context_complete=candidate_context_complete,
            producer_version=self.analyzer.producer_version,prior_messages=prior,attachments=attachments,
            coverage=[*coverage,*gaps], archive_id=observation.get('archive_id'), replay_id=replay_id,
        )
        request = self.analyzer.prepare(request)
        claimed = self.service.claim(request,self._ctx('claim',payload_sha256(request)),mode=mode,lease_seconds=self._lease_seconds())
        if claimed['state'] == 'uncertain':
            raise InvocationReconciliationRequired()
        if claimed['state'] == 'busy':
            return claimed
        analysis_id = claimed['analysis_id']
        if claimed['state'] != 'saved':
            try:
                # A safely failed attempt can be reclaimed after local context has
                # changed. The claimed encrypted snapshot is still its authority;
                # never analyze the newly prepared but unclaimed request instead.
                request = self.service.get_request(analysis_id)
                if request['producer_version'] != self.analyzer.producer_version:
                    raise ContractError('changed mail analyzer requires an explicit replay')
                if heartbeat is not None and not heartbeat():
                    raise RuntimeError('mail understanding worker lease lost')
                with self._inference_scope(analysis_id,claimed['claim_token'],heartbeat) as attempt:
                    scope = current_scope()
                    self.service.bind_inference_work(
                        analysis_id,claimed['claim_token'],scope.work_id,scope.revision,
                        self._ctx('bind',analysis_id + ':' + claimed['claim_token']),
                    )
                    try:
                        try:
                            raw = self.analyzer.analyze(request)
                        except (ModelExecutionError, ContractError, InferenceResponseRejected) as exc:
                            # Validation rejected a received answer. This is a known
                            # local failure, unlike losing a valid answer before save.
                            attempt['output_rejected'] = exc
                            raise
                        if heartbeat is not None and not heartbeat():
                            raise RuntimeError('mail understanding worker lease lost')
                        self.service.save(analysis_id,claimed['claim_token'],raw,self._ctx('save',analysis_id + ':' + claimed['claim_token']))
                        attempt['checkpointed'] = True
                    finally:
                        # Seal the exact invocation set before another message in
                        # this mailbox work can invoke the provider. Future sibling
                        # calls must never be attributed to a safely failed attempt.
                        with connect(self.ledger.store.db_path) as con:
                            invoked = [row['invocation_id'] for row in con.execute(
                                'SELECT invocation_id,state FROM inference_invocations WHERE work_id=?', (scope.work_id,)
                            ) if row['invocation_id'] not in attempt['before']
                                or row['state'] != attempt['before'][row['invocation_id']]
                                or attempt['before'][row['invocation_id']] in {'reserved','submitting','accepted','unknown'}]
                        self.service.finish_inference_work(
                            analysis_id,claimed['claim_token'],scope.work_id,invoked,
                            self._ctx('finish',analysis_id + ':' + claimed['claim_token']),
                        )
            except Exception as exc:
                reason = ('usage_reconciliation_required' if getattr(exc,'outcome_unknown',False)
                          else 'usage_deferred' if getattr(exc,'defer_without_attempt',False)
                          else type(exc).__name__)
                try:
                    self.service.fail(analysis_id,claimed['claim_token'],reason,self._ctx('fail',analysis_id + ':' + claimed['claim_token']))
                except ConflictError:
                    # A lost claim cannot be used to overwrite its new owner's state.
                    pass
                raise
        return self.service.project(analysis_id,self._ctx('project',analysis_id),evaluation_report=self.report)

    def _lease_seconds(self):
        provider = getattr(self.analyzer, '_provider', None)
        config = getattr(provider, 'config', None)
        timeout = getattr(config, 'timeout_seconds', getattr(self.analyzer, 'timeout_seconds', 120))
        return min(3600, max(300, int(timeout) + 60))

    @contextmanager
    def _inference_scope(self, analysis_id, claim_token, heartbeat):
        """Reuse worker ownership, or create a real fenced owner for a CLI replay.

        A synthetic invocation scope alone is insufficient: managed providers check
        the corresponding running work row before reserving any budget or sending.
        Standalone owners finish here and are never deliberately queued for a worker.
        """
        inherited = current_scope()
        standalone = inherited is None
        work_id = 'mail-understanding:' + analysis_id if standalone else inherited.work_id
        path = self.ledger.store.db_path
        if inherited is not None and inherited.db_path.resolve() != path.resolve():
            raise ConflictError('mail understanding cannot inherit another database scope')
        stamp = utc_now()
        lease_seconds = self._lease_seconds()
        def expiry(now):
            return (parse_utc(now) + timedelta(seconds=lease_seconds)).isoformat(timespec='seconds').replace('+00:00','Z')
        with connect(path) as con:
            con.execute('BEGIN IMMEDIATE')
            if standalone:
                row = con.execute('SELECT * FROM work_items WHERE work_id=?',(work_id,)).fetchone()
                if row and row['status']=='running' and row['lease_expires_at'] and row['lease_expires_at']>stamp and row['lease_token']!=claim_token:
                    raise ConflictError('mail analysis inference already has an active work lease')
                if row:
                    revision = row['recovery_revision']
                    con.execute("UPDATE work_items SET status='running',attempts=attempts+1,lease_owner='mail_understanding',lease_token=?,lease_expires_at=?,started_at=?,completed_at=NULL WHERE work_id=?",(claim_token,expiry(stamp),stamp,work_id))
                else:
                    revision = 0
                    con.execute("INSERT INTO work_items(work_id,task_kind,dedupe_key,payload_json,status,due_at,attempts,max_attempts,lease_owner,lease_token,lease_expires_at,created_at,started_at) VALUES(?,'mail.understanding',?,?,'running',?,1,3,'mail_understanding',?,?,?,?)",(work_id,work_id,canonical_json({'analysis_id':analysis_id}),stamp,claim_token,expiry(stamp),stamp,stamp))
            else:
                revision = inherited.revision
            before = {row['invocation_id']:row['state'] for row in con.execute('SELECT invocation_id,state FROM inference_invocations WHERE work_id=?',(work_id,))}
        def owned_heartbeat():
            if heartbeat is not None and not heartbeat():
                return False
            if not standalone:
                return inherited.heartbeat() if inherited.heartbeat is not None else True
            now = utc_now()
            with connect(path) as con:
                return con.execute("UPDATE work_items SET lease_expires_at=? WHERE work_id=? AND status='running' AND lease_token=? AND lease_expires_at>?",(expiry(now),work_id,claim_token,now)).rowcount == 1
        attempt = {'before':before, 'checkpointed':False, 'output_rejected':False}
        try:
            policy = self.policy if standalone else inherited.policy
            clock = None if standalone else inherited.clock
            with invocation_scope(path,work_id,revision,policy=policy,clock=clock,heartbeat=owned_heartbeat):
                scope = current_scope()
                yield attempt
        except Exception as exc:
            uncertain = bool(getattr(exc,'outcome_unknown',False))
            with connect(path) as con:
                con.execute('BEGIN IMMEDIATE')
                from ..inference.usage import _assert_owned, _update_work_outcome
                # A stale analyzer must not release another worker's reservation.
                _assert_owned(con, scope)
                if standalone and con.execute('SELECT lease_token FROM work_items WHERE work_id=?', (work_id,)).fetchone()[0] != claim_token:
                    raise ConflictError('mail understanding work lease changed')
                # A successful synchronous response whose analysis was not saved
                # has no retrievable result. Preserve the usage ledger's existing
                # reconciliation boundary rather than issuing the same POST again.
                for row in con.execute('SELECT * FROM inference_invocations WHERE work_id=?',(work_id,)).fetchall():
                    if row['state']=='completed' and not row['provider_job_id'] and before.get(row['invocation_id'])!='completed' and not attempt['checkpointed']:
                        if attempt['output_rejected'] is exc:
                            con.execute("UPDATE inference_invocations SET state='failed',reconciliation_reason='inference_output_rejected',updated_at=? WHERE invocation_id=?",(utc_now(),row['invocation_id']))
                        else:
                            con.execute("UPDATE inference_invocations SET state='unknown',reconciliation_reason='inference_result_not_checkpointed',updated_at=? WHERE invocation_id=?",(utc_now(),row['invocation_id']))
                            uncertain = True
                    elif row['state'] in {'submitting','unknown'}:
                        uncertain = True
                _update_work_outcome(con, work_id)
                if uncertain:
                    con.execute("UPDATE work_items SET external_outcome='unknown' WHERE work_id=?",(work_id,))
                if standalone:
                    deferred = bool(getattr(exc,'defer_without_attempt',False))
                    con.execute("UPDATE work_items SET status='dead',completed_at=?,lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,last_error=?,failure_kind=?,failure_retryable=?,attempts=MAX(0,attempts-?),recovery_revision=recovery_revision+1 WHERE work_id=? AND lease_token=?",(utc_now(),'inference_reconciliation_required' if uncertain else type(exc).__name__,'external_reconciliation' if uncertain else 'usage_deferred' if deferred else 'retryable',int(not uncertain),int(deferred),work_id,claim_token))
            if uncertain and not getattr(exc,'outcome_unknown',False):
                raise InvocationReconciliationRequired() from exc
            raise
        else:
            if standalone:
                with connect(path) as con:
                    con.execute("UPDATE work_items SET status='succeeded',completed_at=?,lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,last_error='',failure_kind='',failure_retryable=NULL WHERE work_id=? AND lease_token=?",(utc_now(),work_id,claim_token))

    @staticmethod
    def _ctx(operation, identity):
        return MutationContext('understanding:' + operation + ':' + identity,'system','mail_understanding',identity[:200])


def build_understanding_runtime(config, ledger, archive, environment=None):
    """Only the trusted mail lane constructs providers or decrypts source material."""
    import os
    from pathlib import Path
    from .model import load_classifier_config
    from .understanding_adapters import RemoteMailUnderstandingAnalyzer
    environment = os.environ if environment is None else environment
    analyzer = None
    local = config.mail_classifier_config or environment.get('JOB_SEARCH_MAIL_CLASSIFIER_CONFIG')
    if config.mail_understanding_mode in {'shadow','shared'}:
        if local:
            local_config = load_classifier_config(Path(local))
            analyzer = local_config.build_understanding()
        elif config.remote_mail_inference_enabled:
            from ..runtime import _configured_remote_mail_profile
            from ..inference import build_structured_provider
            analyzer = RemoteMailUnderstandingAnalyzer(build_structured_provider(_configured_remote_mail_profile(config,environment)))
        else:
            raise ContractError('shared mail requires a configured version2 local analyzer or remote opt-in')
    return MailUnderstandingRuntime(ledger,archive,analyzer,mode=config.mail_understanding_mode,
        source_scope=config.mail_understanding_source_scope or 'current',
        allow_attachments=bool(local) or config.remote_mail_temporal_enabled,
        evaluation_report=load_report(config.mail_understanding_evaluation_report),usage_limits=config.inference_usage_limits)
