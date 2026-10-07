"""Source-preserving mail observations and explicitly reviewed mail discovery."""
from __future__ import annotations

from dataclasses import asdict
import json
import sqlite3
import uuid
from typing import Any, Mapping

from ..contracts import (ApplicationEventType, ContractError, ConflictError, JobSnapshot,
                         MutationContext, RecommendationProvenance, canonical_json,
                         parse_utc, payload_sha256, validate_identifier)
from ..db import connect


def _time(value):
    if value in (None, ''):
        return None
    return parse_utc(value).isoformat(timespec='seconds').replace('+00:00', 'Z')


def _text(value, name, limit=512, required=False):
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ContractError(f'invalid {name}')
    return value.strip()


def _bound(limit):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ContractError('limit must be between 1 and 100')


def _public(row):
    result = dict(row)
    for field in ('account_id', 'immutable_message_id', 'folder_ref'):
        result.pop(field, None)
    if 'recipients_json' in result:
        result['recipients'] = json.loads(result.pop('recipients_json'))
    return result


def supersede_waiting_reply(con, application_id, source_at, context, stamp):
    """A newly linked incoming response resolves only older waiting obligations."""
    if not con.execute("SELECT 1 FROM sqlite_master WHERE name='lifecycle_tasks'").fetchone():
        return
    from .core import CoreMixin
    core = CoreMixin()
    for task in con.execute("SELECT task_id FROM lifecycle_tasks WHERE application_id=? AND status='open' AND owner='employer' AND policy_version='observed-reply-v1' AND julianday(source_time)<julianday(?)", (application_id, source_at)).fetchall():
        core._transition_task(con, task['task_id'], 'supersede', {}, context, stamp)


def link_accepted_evidence(con, evidence_id, application_id, context, stamp):
    """Link reviewed evidence inside the caller's event transaction."""
    evidence = con.execute('SELECT * FROM mail_evidence WHERE evidence_id=?', (evidence_id,)).fetchone()
    if not evidence:
        raise ContractError('mail evidence not found')
    observed = con.execute('SELECT observation_id FROM lifecycle_mail_observations WHERE evidence_id=?', (evidence_id,)).fetchone()
    if observed:
        observation_id = observed['observation_id']
    else:
        # Legacy evidence predates direction tracking. Preserve the unknown value.
        observation_id = uuid.uuid4().hex
        archived = con.execute('SELECT archive_id FROM mail_archive WHERE account_id=? AND immutable_message_id=?', (evidence['account_id'], evidence['immutable_message_id'])).fetchone()
        thread = payload_sha256({'account': evidence['account_id'], 'conversation': evidence['conversation_id']}) if evidence['conversation_id'] else ''
        con.execute("INSERT INTO lifecycle_mail_observations (observation_id,account_id,immutable_message_id,conversation_ref,direction,sender,subject,received_at,source_at,modified_at,evidence_id,archive_id,created_at,updated_at) VALUES (?,?,?,?,'unknown',?,?,?,?,?,?,?,?,?)", (observation_id, evidence['account_id'], evidence['immutable_message_id'], thread, evidence['sender'], evidence['subject'], evidence['received_at'], evidence['received_at'], evidence['received_at'], evidence_id, archived['archive_id'] if archived else None, stamp, stamp))
        snapshot = dict(con.execute('SELECT * FROM lifecycle_mail_observations WHERE observation_id=?', (observation_id,)).fetchone())
        con.execute('INSERT INTO lifecycle_mail_revisions VALUES (?,?,?,?)', (payload_sha256(snapshot), observation_id, canonical_json(snapshot), stamp))
    previous = con.execute('SELECT 1 FROM lifecycle_mail_links WHERE observation_id=? AND application_id=?', (observation_id, application_id)).fetchone()
    con.execute("INSERT OR IGNORE INTO lifecycle_mail_links VALUES (?,?,1.0,'accepted_event',?)", (observation_id, application_id, stamp))
    if not previous:
        con.execute("INSERT INTO lifecycle_mail_link_history (observation_id,to_application_id,actor_kind,reason,created_at) VALUES (?,?,?,'accepted_event',?)", (observation_id, application_id, context.actor_kind, stamp))
    observation = con.execute('SELECT direction,source_at FROM lifecycle_mail_observations WHERE observation_id=?', (observation_id,)).fetchone()
    if observation['direction'] == 'inbound':
        supersede_waiting_reply(con, application_id, observation['source_at'], context, stamp)
    return observation_id


class MailMixin:
    def observe_mail(self, payload: Mapping[str, Any], context: MutationContext):
        if context.actor_kind != 'system':
            raise ContractError('mail observations require the authenticated sync service')
        account = _text(payload.get('account_id'), 'account_id', 2048, True)
        message = _text(payload.get('immutable_message_id'), 'message_id', 2048, True)
        direction = payload.get('direction', 'unknown')
        if direction not in {'inbound', 'outbound', 'draft', 'unknown'}:
            raise ContractError('invalid mail direction')
        received, sent = _time(payload.get('received_at')), _time(payload.get('sent_at'))
        source = sent if direction == 'outbound' else received
        source = source or _time(payload.get('source_at'))
        if not source or (direction == 'outbound' and not sent):
            raise ContractError('mail observation requires a valid source time; sent mail requires sent_at')
        recipients = payload.get('recipients', [])
        if not isinstance(recipients, (list, tuple)) or len(recipients) > 100:
            raise ContractError('recipients must be a bounded list')
        values = dict(account_id=account, immutable_message_id=message,
                      conversation_ref=payload_sha256({'account': account, 'conversation': payload['conversation_id']}) if payload.get('conversation_id') else '',
                      folder_ref=_text(payload.get('folder_ref', ''), 'folder_ref', 2048), direction=direction,
                      sender=_text(payload.get('sender', ''), 'sender'),
                      recipients_json=canonical_json([_text(x, 'recipient') for x in recipients]),
                      subject=_text(payload.get('subject', ''), 'subject'), received_at=received, sent_at=sent,
                      source_at=source, modified_at=_time(payload.get('modified_at')) or source,
                      evidence_id=payload.get('evidence_id'), archive_id=payload.get('archive_id'))
        revision = payload_sha256(values)
        def operation(con, stamp):
            existing = con.execute('SELECT * FROM lifecycle_mail_observations WHERE account_id=? AND immutable_message_id=?', (account, message)).fetchone()
            observation_id = existing['observation_id'] if existing else uuid.uuid4().hex
            for table, field in (('mail_evidence', 'evidence_id'), ('mail_archive', 'archive_id')):
                if values[field] and not con.execute(f'SELECT 1 FROM {table} WHERE {field}=? AND account_id=? AND immutable_message_id=?', (values[field], account, message)).fetchone():
                    raise ContractError('mail source identity mismatch')
            if existing and values['modified_at'] == existing['modified_at'] and existing['direction'] == direction:
                immutable_fields = ('sender', 'recipients_json', 'subject', 'received_at', 'sent_at', 'source_at')
                if any(values[key] != existing[key] for key in immutable_fields):
                    raise ConflictError('same source version has contradictory mail facts')
            if not existing:
                columns = list(values)
                con.execute('INSERT INTO lifecycle_mail_observations (observation_id,' + ','.join(columns) + ',created_at,updated_at) VALUES (' + ','.join('?' for _ in range(len(columns)+3)) + ')', [observation_id, *values.values(), stamp, stamp])
            elif parse_utc(values['modified_at']) >= parse_utc(existing['modified_at']) and not (existing['direction'] == 'outbound' and direction == 'draft') and not (existing['direction'] in {'inbound','outbound'} and direction == 'unknown') and not con.execute('SELECT 1 FROM lifecycle_mail_direction_decisions WHERE observation_id=? AND direction<>?', (observation_id,direction)).fetchone():
                con.execute('UPDATE lifecycle_mail_observations SET ' + ','.join(k+'=?' for k in values) + ',updated_at=? WHERE observation_id=?', [*values.values(), stamp, observation_id])
            con.execute('INSERT OR IGNORE INTO lifecycle_mail_revisions VALUES (?,?,?,?)', (revision, observation_id, canonical_json(values), stamp))
            current = con.execute('SELECT * FROM lifecycle_mail_observations WHERE observation_id=?', (observation_id,)).fetchone()
            # Immutable Graph IDs survive sending. A draft worker success alone is
            # insufficient; require a subsequent sent observation for that exact ID.
            if current['direction'] == 'outbound':
                actions = con.execute("SELECT DISTINCT p.action_id,p.application_id,p.payload_json FROM action_executions x JOIN action_proposals p USING(action_id) WHERE x.remote_id=? AND p.kind='outlook_reply_draft' AND p.account_id=?", (message, account)).fetchall()
                if len(actions) == 1:
                    action = actions[0]
                    con.execute('UPDATE lifecycle_mail_observations SET action_id=? WHERE observation_id=?', (action['action_id'], observation_id))
                    self._link_mail(con, observation_id, action['application_id'], 1.0, 'observed_sent_draft', context, stamp)
                    if current['evidence_id'] and hasattr(self, 'complete_task_from_evidence'):
                        original_id = json.loads(action['payload_json']).get('message_id')
                        original = con.execute('SELECT evidence_id FROM mail_evidence WHERE account_id=? AND immutable_message_id=?', (account, original_id)).fetchone() if original_id else None
                        original_evidence_id = original['evidence_id'] if original else None
                        tasks = con.execute("SELECT task_id FROM lifecycle_tasks WHERE application_id=? AND status='open' AND kind IN ('reply','send_availability') AND (action_id=? OR (action_id IS NULL AND evidence_id=?))", (action['application_id'], action['action_id'], original_evidence_id)).fetchall()
                        for task in tasks:
                            self.complete_task_from_evidence(con, task['task_id'], current['evidence_id'], current['sent_at'], context, stamp)
            return {'observation': _public(con.execute('SELECT * FROM lifecycle_mail_observations WHERE observation_id=?', (observation_id,)).fetchone()), 'created': existing is None}
        return self.store._idempotent('observe_mail', context, values, operation)

    def _link_mail(self, con, observation_id, application_id, confidence, source, context, stamp):
        self.store._application(con, application_id)
        if not con.execute('SELECT 1 FROM lifecycle_mail_observations WHERE observation_id=?', (observation_id,)).fetchone():
            raise ContractError('mail observation not found')
        observation = con.execute('SELECT direction,source_at FROM lifecycle_mail_observations WHERE observation_id=?', (observation_id,)).fetchone()
        if observation['direction'] == 'inbound':
            supersede_waiting_reply(con, application_id, observation['source_at'], context, stamp)
        prior = con.execute('SELECT 1 FROM lifecycle_mail_links WHERE observation_id=? AND application_id=?', (observation_id, application_id)).fetchone()
        con.execute('INSERT OR IGNORE INTO lifecycle_mail_links VALUES (?,?,?,?,?)', (observation_id, application_id, confidence, source, stamp))
        if not prior:
            con.execute('INSERT INTO lifecycle_mail_link_history (observation_id,to_application_id,actor_kind,reason,created_at) VALUES (?,?,?,?,?)', (observation_id, application_id, context.actor_kind, source, stamp))

    def link_mail(self, payload, context):
        if context.actor_kind not in {'system', 'user'}:
            raise ContractError('mail associations require user review or verified sync evidence')
        observation_id, application_id = payload['observation_id'], payload['application_id']
        confidence = payload.get('confidence', 1.0)
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise ContractError('invalid linkage confidence')
        def operation(con, stamp):
            if context.actor_kind == 'system':
                source = payload.get('source')
                if source not in {'accepted_conversation', 'lifecycle_proposal'}:
                    raise ContractError('unverified system mail association')
                observed = con.execute('SELECT * FROM lifecycle_mail_observations WHERE observation_id=?', (observation_id,)).fetchone()
                if not observed:
                    raise ContractError('mail observation not found')
                if source == 'lifecycle_proposal':
                    valid = con.execute("SELECT 1 FROM event_proposals WHERE evidence_id=? AND proposed_application_id=? AND status IN ('accepted','auto_applied')", (observed['evidence_id'], application_id)).fetchone()
                else:
                    applications = {r[0] for r in con.execute("SELECT DISTINCT l.application_id FROM lifecycle_mail_links l JOIN lifecycle_mail_observations m USING(observation_id) WHERE m.account_id=? AND m.conversation_ref=? AND m.conversation_ref<>''", (observed['account_id'], observed['conversation_ref']))}
                    # Accepted ledger evidence supports legacy threads predating observations.
                    if not applications and observed['evidence_id']:
                        applications = {r[0] for r in con.execute("SELECT DISTINCT ae.application_id FROM event_proposals p JOIN mail_evidence e USING(evidence_id) JOIN application_events ae ON ae.event_id=p.applied_event_id WHERE p.status IN ('accepted','auto_applied') AND NOT EXISTS(SELECT 1 FROM lifecycle_mail_observations o WHERE o.evidence_id=e.evidence_id) AND e.account_id=? AND e.conversation_id=(SELECT conversation_id FROM mail_evidence WHERE evidence_id=?)", (observed['account_id'], observed['evidence_id']))}
                    valid = applications == {application_id}
                if not valid:
                    raise ContractError('mail association is ambiguous or unreviewed')
            self._link_mail(con, observation_id, application_id, confidence, payload.get('source', 'reviewed'), context, stamp)
            return {'observation_id': observation_id, 'application_id': application_id}
        return self.store._idempotent('link_mail', context, payload, operation)

    def reassign_evidence(self, con, evidence_id, from_application_id, to_application_id, context, stamp):
        self.store._application(con, to_application_id)
        rows = con.execute('SELECT m.observation_id FROM lifecycle_mail_observations m JOIN lifecycle_mail_links l USING(observation_id) WHERE m.evidence_id=? AND l.application_id=?', (evidence_id, from_application_id)).fetchall()
        if not rows:
            raise ContractError('evidence has no matching application association')
        for row in rows:
            con.execute('DELETE FROM lifecycle_mail_links WHERE observation_id=? AND application_id=?', (row[0], from_application_id))
            self._link_mail(con, row[0], to_application_id, 1.0, 'reviewed_correction', context, stamp)
            con.execute('INSERT INTO lifecycle_mail_link_history (observation_id,from_application_id,to_application_id,actor_kind,reason,created_at) VALUES (?,?,?,?,?,?)', (row[0], from_application_id, to_application_id, context.actor_kind, 'reviewed_correction', stamp))

    def list_application_conversation(self, application_id, *, limit=50, cursor=None):
        _bound(limit)
        validate_identifier(application_id, 'application_id')
        with connect(self.store.db_path) as con:
            self.store._application(con, application_id)
            clauses, parameters = ['l.application_id=?'], [application_id]
            if cursor:
                validate_identifier(cursor, 'cursor')
                anchor = con.execute('SELECT source_at FROM lifecycle_mail_observations m JOIN lifecycle_mail_links l USING(observation_id) WHERE observation_id=? AND application_id=?', (cursor, application_id)).fetchone()
                if not anchor:
                    raise ContractError('conversation cursor is not linked to this application')
                clauses.append('(m.source_at<? OR (m.source_at=? AND m.observation_id<?))')
                parameters.extend([anchor['source_at'], anchor['source_at'], cursor])
            rows = con.execute('SELECT m.*,l.confidence,l.source FROM lifecycle_mail_links l JOIN lifecycle_mail_observations m USING(observation_id) WHERE ' + ' AND '.join(clauses) + ' ORDER BY m.source_at DESC,m.observation_id DESC LIMIT ?', (*parameters,limit+1)).fetchall()
        items = [_public(row) for row in rows[:limit]]
        return {'items': items, 'next_cursor': items[-1]['observation_id'] if len(rows)>limit else None, 'complete': len(rows)<=limit,
                'coverage': 'linked_observations_only; unprocessed or unlinked mailbox messages may exist'}

    def propose_discovery(self, payload, context):
        observation_id = payload['observation_id']
        employer = _text(payload.get('employer', ''), 'employer')
        title = _text(payload.get('title', ''), 'title')
        def operation(con, stamp):
            observation = con.execute('SELECT * FROM lifecycle_mail_observations WHERE observation_id=?', (observation_id,)).fetchone()
            if not observation or observation['direction'] in {'draft','outbound'}:
                raise ContractError('discovery requires received mail evidence')
            if con.execute('SELECT 1 FROM lifecycle_mail_links WHERE observation_id=?', (observation_id,)).fetchone():
                return {'created': False, 'discovery': None}
            existing = con.execute('SELECT * FROM lifecycle_discoveries WHERE observation_id=?', (observation_id,)).fetchone()
            if existing:
                return {'created': False, 'discovery': dict(existing)}
            discovery_id = uuid.uuid4().hex
            con.execute("INSERT INTO lifecycle_discoveries VALUES (?,?,'pending',?,?,NULL,'',?,?)", (discovery_id, observation_id, employer, title, stamp, stamp))
            return {'created': True, 'discovery': dict(con.execute('SELECT * FROM lifecycle_discoveries WHERE discovery_id=?', (discovery_id,)).fetchone())}
        return self.store._idempotent('propose_discovery', context, payload, operation)

    def list_discoveries(self, *, status='pending', limit=50, cursor=None):
        _bound(limit)
        if status not in {'pending','linked','created','dismissed'}:
            raise ContractError('invalid discovery status')
        with connect(self.store.db_path) as con:
            rows = con.execute('SELECT d.*,m.subject,m.evidence_id,m.archive_id,m.direction,m.received_at,m.sent_at,m.updated_at AS observation_updated_at FROM lifecycle_discoveries d JOIN lifecycle_mail_observations m USING(observation_id) WHERE d.status=? AND d.discovery_id>? ORDER BY d.discovery_id LIMIT ?', (status, cursor or '', limit+1)).fetchall()
        return {'items': [dict(r) for r in rows[:limit]], 'next_cursor': rows[limit-1]['discovery_id'] if len(rows)>limit else None, 'complete': len(rows)<=limit}

    def decide_discovery(self, payload, context, *, review_job_snapshot=None):
        if context.actor_kind != 'user':
            raise ContractError('discovery decisions require user review')
        decision = payload.get('decision')
        if decision not in {'link','create','dismiss','link_job'}:
            raise ContractError('invalid discovery decision')
        if decision == 'link_job':
            if review_job_snapshot is None:
                raise ContractError('catalog discovery requires a server-validated job')
            review_job_snapshot.validate()
            if payload.get('selected_job') != {'ats': review_job_snapshot.ats, 'id': review_job_snapshot.job_id}:
                raise ContractError('selected job does not match the validated catalog job')
        elif review_job_snapshot is not None:
            raise ContractError('catalog selection requires a link_job decision')
        def operation(con, stamp):
            row = con.execute('SELECT * FROM lifecycle_discoveries WHERE discovery_id=?', (payload['discovery_id'],)).fetchone()
            if not row or row['status'] != 'pending':
                raise ConflictError('discovery is not pending')
            application_id = payload.get('application_id') if decision == 'link' else None
            if decision == 'link' and not application_id:
                raise ContractError('link decision requires application_id')
            if decision == 'link_job':
                application_id = self.store._start_application(con, review_job_snapshot,
                    RecommendationProvenance(), context, stamp)['application']['application_id']
            if decision == 'create':
                employer = _text(payload.get('employer', row['employer']), 'employer', required=True)
                title = _text(payload.get('title', row['title']), 'title', required=True)
                application_id = uuid.uuid4().hex
                snapshot = JobSnapshot('external', uuid.uuid4().hex, '', title, employer, '', '')
                snapshot.validate()
                con.execute("INSERT INTO applications (application_id,ats,job_id,title_snapshot,employer_snapshot,job_url_snapshot,current_phase,started_at,last_activity_at,last_event_seq,projection_sha256,updated_at) VALUES (?,'external',?,?,?,'','preparing',?,?,0,'',?)", (application_id, snapshot.job_id, title, employer, stamp, stamp, stamp))
                self.store._append_event(con, application_id, ApplicationEventType.APPLICATION_STARTED, stamp, {'snapshot': asdict(snapshot), 'provenance': asdict(RecommendationProvenance())}, 'mail-discovery:' + row['discovery_id'], context, stamp)
                self.store._project(con, application_id)
            if application_id:
                self._link_mail(con, row['observation_id'], application_id, 1.0, 'reviewed_discovery', context, stamp)
            con.execute('UPDATE lifecycle_discoveries SET status=?,application_id=?,reviewed_by=?,updated_at=? WHERE discovery_id=?', ({'link':'linked','link_job':'linked','create':'created','dismiss':'dismissed'}[decision], application_id, context.actor_kind, stamp, row['discovery_id']))
            return {'discovery': dict(con.execute('SELECT * FROM lifecycle_discoveries WHERE discovery_id=?', (row['discovery_id'],)).fetchone()), 'application_id': application_id}
        return self.store._idempotent('decide_discovery', context, payload, operation)

    def start_mail_replay(self, payload, context):
        if context.actor_kind != 'user':
            raise ContractError('historical replay requires an explicit user request')
        account = _text(payload.get('account_id'), 'account_id', 2048, True)
        since, until = _time(payload.get('since_at')), _time(payload.get('until_at'))
        if not since or not until or parse_utc(since) >= parse_utc(until) or (parse_utc(until)-parse_utc(since)).total_seconds() > 366 * 86400:
            raise ContractError('replay requires an ordered window of at most 366 days')
        query_version = payload.get('query_version', 2)
        if isinstance(query_version, bool) or not isinstance(query_version, int) or query_version < 1:
            raise ContractError('invalid replay query version')
        def operation(con, stamp):
            high = con.execute('SELECT COALESCE(MAX(rowid),0) FROM outlook_message_stage WHERE account_id=? AND query_version=?', (account, query_version)).fetchone()[0]
            replay_id = uuid.uuid4().hex
            con.execute("INSERT INTO lifecycle_mail_replays (replay_id,account_id,since_at,until_at,query_version,max_stage_rowid,status,created_at,updated_at) VALUES (?,?,?,?,?,?,'pending',?,?)", (replay_id, account, since, until, query_version, high, stamp, stamp))
            return {'replay': dict(con.execute('SELECT * FROM lifecycle_mail_replays WHERE replay_id=?', (replay_id,)).fetchone())}
        return self.store._idempotent('start_mail_replay', context, payload, operation)

    def get_mail_replay(self, replay_id):
        validate_identifier(replay_id, 'replay_id')
        with connect(self.store.db_path) as con:
            row = con.execute('SELECT * FROM lifecycle_mail_replays WHERE replay_id=?', (replay_id,)).fetchone()
        if not row:
            raise ContractError('mail replay not found')
        result = dict(row)
        result['coverage'] = 'staged messages in requested window at creation; no mailbox traversal implied'
        result['notifications_suppressed'] = True
        return result

    def mail_coverage(self, application_id=None):
        with connect(self.store.db_path) as con:
            if application_id:
                self.store._application(con, application_id)
            counts = {row['processing_status']: row['n'] for row in con.execute('SELECT processing_status,COUNT(*) n FROM outlook_message_stage WHERE removed=0 GROUP BY processing_status')}
            health = [dict(row) for row in con.execute("SELECT status,last_success_at,last_attempt_at FROM connector_health WHERE connector_key LIKE 'outlook:%'")]
            cutoffs = [dict(row) for row in con.execute('SELECT started_at FROM outlook_activation')]
            cursor_count = con.execute('SELECT COUNT(*) FROM outlook_sync_cursors WHERE needs_backfill=1 OR in_flight_next_link IS NOT NULL').fetchone()[0]
        return {'processing_counts': counts, 'connectors': health, 'incomplete_cursors': cursor_count,
                'complete': False, 'scope': 'observed linked messages only',
                'processing_cutoffs': cutoffs, 'cutoff_known': bool(cutoffs),
                'note': 'Mailbox traversal and analysis have independent coverage; absence is not an employer outcome.'}

    def list_pending_replays(self, *, limit=10, account_id=None):
        _bound(limit)
        if account_id is not None:
            account_id = _text(account_id, 'account_id', 2048, True)
        with connect(self.store.db_path) as con:
            return [dict(row) for row in con.execute("SELECT * FROM lifecycle_mail_replays WHERE status IN ('pending','running') AND (? IS NULL OR account_id=?) ORDER BY created_at,replay_id LIMIT ?", (account_id,account_id,limit))]

    def get_mail_observation(self, observation_id):
        validate_identifier(observation_id, 'observation_id')
        with connect(self.store.db_path) as con:
            row = con.execute('SELECT * FROM lifecycle_mail_observations WHERE observation_id=?', (observation_id,)).fetchone()
            if not row:
                raise ContractError('mail observation not found')
            result = _public(row)
            result['application_ids'] = [r[0] for r in con.execute('SELECT application_id FROM lifecycle_mail_links WHERE observation_id=? ORDER BY application_id', (observation_id,))]
            result['direction_decisions'] = [dict(r) for r in con.execute('SELECT decision_id,direction,reason,source_at,decided_at FROM lifecycle_mail_direction_decisions WHERE observation_id=? ORDER BY decided_at,decision_id LIMIT 20', (observation_id,))]
        return result

    def review_mail_direction(self, payload, context):
        """Resolve unknown direction explicitly; never guess from candidate wording."""
        if context.actor_kind != 'user':
            raise ContractError('mail direction requires user review')
        observation_id = payload['observation_id']
        validate_identifier(observation_id, 'observation_id')
        direction = payload.get('direction')
        if direction not in {'inbound','outbound'}:
            raise ContractError('reviewed direction must be inbound or outbound')
        reason = _text(payload.get('reason'), 'reason', required=True)
        expected = _time(payload.get('expected_updated_at'))
        if not expected:
            raise ContractError('direction review requires expected_updated_at')
        def operation(con, stamp):
            row = con.execute('SELECT * FROM lifecycle_mail_observations WHERE observation_id=?', (observation_id,)).fetchone()
            if not row or row['direction'] != 'unknown' or _time(row['updated_at']) != expected:
                raise ConflictError('mail observation changed or direction is already known')
            source_at = row['sent_at'] if direction == 'outbound' else row['received_at']
            if not source_at:
                raise ContractError('reviewed direction requires its observed source timestamp')
            decision_id = uuid.uuid4().hex
            con.execute('INSERT INTO lifecycle_mail_direction_decisions VALUES (?,?,?,?,?,?,?)', (decision_id, observation_id, direction, reason, context.actor_kind, source_at, stamp))
            con.execute('UPDATE lifecycle_mail_observations SET direction=?,source_at=?,updated_at=? WHERE observation_id=?', (direction, source_at, stamp, observation_id))
            revised = dict(con.execute('SELECT * FROM lifecycle_mail_observations WHERE observation_id=?', (observation_id,)).fetchone())
            con.execute('INSERT INTO lifecycle_mail_revisions VALUES (?,?,?,?)', (decision_id, observation_id, canonical_json({'observation':revised,'direction_review':decision_id}), stamp))
            if direction == 'inbound':
                for linked in con.execute('SELECT application_id FROM lifecycle_mail_links WHERE observation_id=?', (observation_id,)).fetchall():
                    supersede_waiting_reply(con, linked['application_id'], source_at, context, stamp)
            return {'observation': _public(revised), 'decision_id': decision_id,
                    'reprocessing': 'Run an explicit historical replay to classify previously skipped historical messages.'}
        return self.store._idempotent('review_mail_direction', context, payload, operation)

    def list_mail_replays(self, *, account_id=None, application_id=None, status=None, limit=25, cursor=None):
        """Read bounded replay progress without mailbox transport IDs or bodies."""
        _bound(limit)
        clauses, parameters = [], []
        if account_id is not None:
            clauses.append('r.account_id=?')
            parameters.append(_text(account_id, 'account_id', 2048, True))
        if status is not None:
            if status not in {'pending','running','completed','failed','cancelled'}:
                raise ContractError('invalid replay status')
            clauses.append('r.status=?')
            parameters.append(status)
        with connect(self.store.db_path) as con:
            if application_id is not None:
                validate_identifier(application_id, 'application_id')
                self.store._application(con, application_id)
                clauses.append('EXISTS (SELECT 1 FROM lifecycle_mail_observations m JOIN lifecycle_mail_links l USING(observation_id) WHERE l.application_id=? AND m.account_id=r.account_id AND julianday(m.received_at)>=julianday(r.since_at) AND julianday(m.received_at)<julianday(r.until_at))')
                parameters.append(application_id)
            if cursor:
                validate_identifier(cursor, 'cursor')
                anchor = con.execute('SELECT r.created_at FROM lifecycle_mail_replays r WHERE ' + ' AND '.join([*clauses,'r.replay_id=?']), (*parameters,cursor)).fetchone()
                if not anchor:
                    raise ContractError('replay cursor does not match the current filter')
                clauses.append('(r.created_at<? OR (r.created_at=? AND r.replay_id<?))')
                parameters.extend([anchor['created_at'],anchor['created_at'],cursor])
            where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
            records = con.execute('SELECT r.replay_id,r.account_id,r.since_at,r.until_at,r.query_version,r.processed,r.status,r.last_error,r.failure_count,r.created_at,r.updated_at FROM lifecycle_mail_replays r' + where + ' ORDER BY r.created_at DESC,r.replay_id DESC LIMIT ?', (*parameters,limit+1)).fetchall()
        items = [dict(row) for row in records[:limit]]
        return {'items':items,'next_cursor':items[-1]['replay_id'] if len(records)>limit else None,'complete':len(records)<=limit,
                'coverage':'Replay jobs analyze staged history only; application filtering finds accounts and windows with linked observations.'}

    def transition_mail_replay(self, replay_id, operation, context):
        if context.actor_kind != 'user':
            raise ContractError('replay retry and cancellation require a user')
        validate_identifier(replay_id, 'replay_id')
        if operation not in {'retry','cancel'}:
            raise ContractError('invalid replay operation')
        def mutate(con, stamp):
            replay = con.execute('SELECT * FROM lifecycle_mail_replays WHERE replay_id=?', (replay_id,)).fetchone()
            if not replay:
                raise ContractError('mail replay not found')
            if operation == 'retry' and replay['status'] != 'failed':
                raise ConflictError('only a failed replay can be retried')
            if operation == 'cancel' and replay['status'] not in {'pending','running','failed'}:
                raise ConflictError('replay is already terminal')
            target = 'pending' if operation == 'retry' else 'cancelled'
            con.execute('UPDATE lifecycle_mail_replays SET status=?,last_error=?,updated_at=? WHERE replay_id=?', (target,'',stamp,replay_id))
            con.execute('INSERT INTO lifecycle_mail_replay_decisions VALUES (?,?,?,?,?,?)', (uuid.uuid4().hex,replay_id,operation,context.actor_kind,replay['after_stage_rowid'],stamp))
            result = dict(con.execute('SELECT replay_id,status,processed,failure_count,updated_at FROM lifecycle_mail_replays WHERE replay_id=?', (replay_id,)).fetchone())
            return {'replay':result}
        return self.store._idempotent('transition_mail_replay', context, {'replay_id':replay_id,'operation':operation}, mutate)

    def list_mail_replay_accounts(self, *, limit=100):
        """Configured or previously staged account aliases for explicit replay."""
        _bound(limit)
        with connect(self.store.db_path) as con:
            return [row[0] for row in con.execute('SELECT account_id FROM outlook_activation UNION SELECT account_id FROM outlook_message_stage UNION SELECT account_id FROM outlook_sync_cursors ORDER BY account_id LIMIT ?', (limit,))]
