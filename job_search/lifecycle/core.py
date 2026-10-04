"""Transactional application obligations, detail revisions, and reviewed repairs.

Reminder delivery is deliberately independent of task completion. The methods with
leading underscores accept an existing ledger transaction for mail/interview callers.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any, Mapping

from ..contracts import (ApplicationEventType, ContractError, ConflictError,
    MutationContext, canonical_json, parse_utc, utc_now, validate_event_payload)
from ..db import connect
from ..notifications import NotificationIntent, NotificationPolicy

TASK_KINDS = frozenset(('reply','send_availability','complete_assessment','attend_interview','send_document','offer_decision','follow_up'))
TASK_STATUSES = frozenset(('open','completed','cancelled','superseded'))
OWNERS = frozenset(('applicant','employer','unknown'))


def _id():
    return uuid.uuid4().hex


def _user(context):
    context.validate()
    if context.actor_kind != 'user':
        raise ContractError('lifecycle decisions require actor_kind=user')


def _limit(limit):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise ContractError('limit must be between 1 and 500')


def _text(value, name, limit=2000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ContractError(f'{name} must be 1 to {limit} characters')
    if any(ord(c) < 32 and c not in '\n\r\t' for c in value):
        raise ContractError(f'{name} contains control characters')
    return value.strip()


def _time(value, name, optional=False):
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise ContractError(f'{name} must be a UTC timestamp')
    parse_utc(value)
    return value


def _known(values, allowed):
    if not isinstance(values, Mapping) or set(values) - set(allowed):
        raise ContractError('unknown lifecycle fields')


def _task(con, task_id):
    row = con.execute('SELECT * FROM lifecycle_tasks WHERE task_id=?', (task_id,)).fetchone()
    if not row:
        raise ContractError('task not found')
    return dict(row)


def _revision(con, task, operation, context, stamp):
    con.execute('INSERT INTO lifecycle_task_revisions VALUES (?,?,?,?,?,?,?,?,?,?)',
        (_id(), task['task_id'], task['revision_no'], operation, canonical_json(task),
         context.actor_kind, context.source_kind, context.source_ref, task['source_time'], stamp))


def _cancel_task_notifications(con, task_id):
    con.execute("UPDATE notification_outbox SET status='cancelled',lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL "
        "WHERE dedupe_key LIKE ? AND status IN ('pending','delivering')", ('lifecycle-task:'+task_id+':%',))


def close_application_work(con, application_id, stamp):
    """Called by ledger projection under its write lock; never sends notifications."""
    context = MutationContext('terminal:'+application_id, 'system', 'lifecycle_terminal')
    for row in con.execute("SELECT * FROM lifecycle_tasks WHERE application_id=? AND status='open'", (application_id,)).fetchall():
        task = dict(row)
        task.update(status='cancelled', revision_no=task['revision_no']+1, updated_at=stamp)
        con.execute("UPDATE lifecycle_tasks SET status='cancelled',revision_no=?,updated_at=? WHERE task_id=?",
                    (task['revision_no'],stamp,task['task_id']))
        _revision(con, task, 'terminal_closure', context, stamp)
        _cancel_task_notifications(con, task['task_id'])
    con.execute("UPDATE notification_outbox SET status='cancelled',lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL WHERE application_id=? AND topic='reminder.due' AND status IN ('pending','delivering')", (application_id,))
    con.execute("UPDATE accepted_interview_schedules SET status='cancelled' WHERE application_id=? AND status='active'", (application_id,))
    if con.execute("SELECT 1 FROM sqlite_master WHERE name='interview_rounds'").fetchone():
        con.execute("UPDATE interview_rounds SET status='cancelled',updated_at=? WHERE application_id=? AND status IN ('proposed','confirmed','rescheduled')", (stamp,application_id))
    con.execute("UPDATE reminders SET status='cancelled',cancelled_at=? WHERE application_id=? AND status='scheduled'", (stamp,application_id))
    for table in ('local_reminders','interview_reminders'):
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            con.execute(f"UPDATE {table} SET status='dismissed',completed_at=? WHERE application_id=? AND status='pending'", (stamp,application_id))


class CoreMixin:
    def _core_command(self, name, request, context, operation):
        return self.store._idempotent('lifecycle.'+name, context, {
            **request, 'actor_kind':context.actor_kind, 'source_kind':context.source_kind,
            'source_ref':context.source_ref}, operation)

    def _evidence(self, con, application_id, evidence_id):
        if not evidence_id:
            return None
        if not con.execute('SELECT 1 FROM mail_evidence WHERE evidence_id=?', (evidence_id,)).fetchone():
            raise ContractError('mail evidence not found')
        linked = None
        has_observation = False
        if con.execute("SELECT 1 FROM sqlite_master WHERE name='lifecycle_mail_links'").fetchone():
            has_observation = bool(con.execute('SELECT 1 FROM lifecycle_mail_observations WHERE evidence_id=?',(evidence_id,)).fetchone())
            linked = con.execute('SELECT 1 FROM lifecycle_mail_links l JOIN lifecycle_mail_observations o USING(observation_id) WHERE o.evidence_id=? AND l.application_id=?', (evidence_id,application_id)).fetchone()
        if not has_observation:
            linked = con.execute('SELECT 1 FROM event_proposals WHERE evidence_id=? AND proposed_application_id=?', (evidence_id,application_id)).fetchone()
        if not linked:
            raise ContractError('evidence is not linked to this application')
        return evidence_id

    def _create_task(self, con, application_id, values, context, stamp):
        _known(values, ('kind','owner','note','due_at','source_time','evidence_id','supersedes_task_id','action_id','policy_version'))
        application = self.store._application(con, application_id)
        if application['current_phase'] == 'terminal':
            raise ConflictError('terminal applications cannot acquire open tasks')
        kind, owner = values.get('kind'), values.get('owner','unknown')
        if kind not in TASK_KINDS or owner not in OWNERS:
            raise ContractError('invalid task kind or owner')
        evidence_id = self._evidence(con, application_id, values.get('evidence_id'))
        action_id = values.get('action_id')
        if action_id and not con.execute('SELECT 1 FROM action_proposals WHERE action_id=? AND application_id=?', (action_id,application_id)).fetchone():
            raise ContractError('action is not linked to this application')
        supersedes = values.get('supersedes_task_id')
        if supersedes:
            old = _task(con, supersedes)
            if old['application_id'] != application_id:
                raise ContractError('cannot supersede a task in another application')
            self._transition_task(con, supersedes, 'supersede', {}, context, stamp)
        task = dict(task_id=_id(), application_id=application_id, kind=kind, owner=owner,
            status='open', note=_text(values.get('note',kind.replace('_',' ')), 'note'),
            due_at=_time(values.get('due_at'),'due_at',True), snoozed_until=None,
            source_time=_time(values.get('source_time',stamp),'source_time'), evidence_id=evidence_id,
            completed_evidence_id=None, supersedes_task_id=supersedes, action_id=action_id,
            policy_version=_text(values.get('policy_version','lifecycle-v1'),'policy_version',100),
            revision_no=1, created_at=stamp, updated_at=stamp)
        con.execute('INSERT INTO lifecycle_tasks ('+','.join(task)+') VALUES ('+','.join('?' for _ in task)+')', tuple(task.values()))
        _revision(con,task,'created',context,stamp)
        return task

    def create_task(self, application_id, values, context):
        _user(context)
        return self._core_command('create_task',dict(application_id=application_id,values=values), context,
            lambda con,stamp: {'task':self._create_task(con,application_id,values,context,stamp)})

    def _transition_task(self, con, task_id, operation, values, context, stamp):
        _known(values, ('evidence_id','source_time','snoozed_until','reason'))
        task = _task(con,task_id)
        if operation not in ('complete','cancel','supersede','snooze'):
            raise ContractError('invalid task operation')
        target = {'complete':'completed','cancel':'cancelled','supersede':'superseded','snooze':'open'}[operation]
        if task['status'] != 'open':
            if task['status'] == target and operation != 'snooze':
                return task
            raise ConflictError('only open tasks can transition')
        if operation == 'snooze':
            until = _time(values.get('snoozed_until'),'snoozed_until')
            if until <= stamp:
                raise ContractError('snooze must end in the future')
            task['snoozed_until'] = until
        if values.get('evidence_id'):
            evidence = self._evidence(con,task['application_id'],values['evidence_id'])
            if operation == 'complete':
                task['completed_evidence_id'] = evidence
        task.update(status=target,revision_no=task['revision_no']+1,updated_at=stamp)
        if values.get('source_time'):
            task['source_time'] = _time(values['source_time'],'source_time')
        con.execute('UPDATE lifecycle_tasks SET status=?,revision_no=?,updated_at=?,snoozed_until=?,completed_evidence_id=?,source_time=? WHERE task_id=?',
            (task['status'],task['revision_no'],stamp,task['snoozed_until'],task['completed_evidence_id'],task['source_time'],task_id))
        _revision(con,task,operation,context,stamp)
        _cancel_task_notifications(con,task_id)
        return task

    def transition_task(self, task_id, operation, values, context):
        _user(context)
        return self._core_command('transition_task',dict(task_id=task_id,operation=operation,values=values),context,
            lambda con,stamp:{'task':self._transition_task(con,task_id,operation,values,context,stamp)})

    def complete_task_from_evidence(self, con, task_id, evidence_id, source_time, context, stamp):
        task = _task(con,task_id)
        if task['kind'] not in ('reply','send_availability'):
            raise ContractError('sent evidence requires a communication task')
        observed = con.execute("SELECT 1 FROM lifecycle_mail_observations o JOIN lifecycle_mail_links l USING(observation_id) WHERE o.evidence_id=? AND l.application_id=? AND o.direction='outbound'", (evidence_id,task['application_id'])).fetchone()
        if not observed:
            raise ContractError('completion requires observed outbound mail')
        if task['status'] == 'completed':
            return task
        completed = self._transition_task(con,task_id,'complete',dict(evidence_id=evidence_id,source_time=source_time),context,stamp)
        self._create_task(con,task['application_id'],dict(kind='reply',owner='employer',
            note='Waiting for a response to your sent reply.',source_time=source_time,
            evidence_id=evidence_id,policy_version='observed-reply-v1'),context,stamp)
        return completed

    def list_tasks(self, application_id=None, status=None, limit=100, offset=0):
        _limit(limit)
        if status is not None and status not in TASK_STATUSES:
            raise ContractError('invalid task status')
        if isinstance(offset,bool) or not isinstance(offset,int) or offset < 0:
            raise ContractError('invalid offset')
        clauses, args = [], []
        for column, value in (('application_id',application_id),('status',status)):
            if value is not None:
                clauses.append(column+'=?'); args.append(value)
        with connect(self.store.db_path) as con:
            return [dict(row) for row in con.execute('SELECT * FROM lifecycle_tasks'+(' WHERE '+' AND '.join(clauses) if clauses else '')+' ORDER BY due_at IS NULL,due_at,task_id LIMIT ? OFFSET ?',(*args,limit,offset))]

    def task_history(self, task_id):
        with connect(self.store.db_path) as con:
            _task(con,task_id)
            return [{**dict(row),'state':json.loads(row['state_json'])} for row in con.execute('SELECT * FROM lifecycle_task_revisions WHERE task_id=? ORDER BY revision_no',(task_id,))]

    def _record_detail(self, con, application_id, kind, values, context, stamp, detail_id=None):
        _known(values, ('status','title','due_at','source_time','evidence_id','document_ref','terms','submission_evidence_id','note','expected_revision_no'))
        values = dict(values)
        expected_revision = values.pop('expected_revision_no',None)
        if expected_revision is not None and (isinstance(expected_revision,bool) or not isinstance(expected_revision,int) or expected_revision < 1):
            raise ContractError('expected_revision_no must be a positive integer')
        if expected_revision is not None and not detail_id:
            raise ContractError('expected_revision_no requires an existing detail')
        statuses = {'assessment':{'requested','submitted','completed','cancelled'},
                    'offer':{'offered','negotiating','accepted','declined','expired','employer_withdrawn'}}
        if kind not in statuses or values.get('status') not in statuses[kind]:
            raise ContractError('invalid lifecycle detail kind or status')
        self.store._application(con,application_id)
        old = None
        if detail_id:
            old = con.execute('SELECT * FROM lifecycle_details WHERE detail_id=?',(detail_id,)).fetchone()
            if not old or old['application_id'] != application_id or old['kind'] != kind:
                raise ContractError('detail does not belong to application and kind')
            if expected_revision is not None and old['revision_no'] != expected_revision:
                raise ConflictError('detail changed since it was read; reload before updating')
            if old['status'] in {'accepted','declined','expired','employer_withdrawn','completed','cancelled'} and values['status'] != old['status']:
                raise ConflictError('closed detail cannot transition; create a new reviewed record')
        detail_id = detail_id or _id()
        detail = json.loads(old['details_json']) if old else {}
        detail.update(values)
        _text(detail.get('title',kind),'title',300)
        due_at = _time(detail.get('due_at'),'due_at',True)
        source_time = _time(values.get('source_time',stamp),'source_time')
        evidence_id = self._evidence(con,application_id,detail.get('evidence_id'))
        if detail.get('submission_evidence_id'):
            self._evidence(con,application_id,detail['submission_evidence_id'])
        if len(canonical_json(detail).encode()) > 16000:
            raise ContractError('detail exceeds 16000 bytes')
        revision_no = old['revision_no']+1 if old else 1
        task_id = old['task_id'] if old else None
        active = detail['status'] in {'requested','offered','negotiating'}
        if task_id:
            existing = _task(con,task_id)
            if existing['status'] == 'open':
                completion = detail['status'] in {'submitted','completed','accepted','declined'}
                self._transition_task(con,task_id,'supersede' if active else ('complete' if completion else 'cancel'),
                    {'evidence_id':evidence_id} if evidence_id else {},context,stamp)
        if active and evidence_id:
            task_kind = 'complete_assessment' if kind == 'assessment' else 'offer_decision'
            for fact_task in con.execute("SELECT task_id FROM lifecycle_tasks WHERE application_id=? AND evidence_id=? AND kind=? AND status='open'",(application_id,evidence_id,task_kind)).fetchall():
                self._transition_task(con,fact_task['task_id'],'supersede',{},context,stamp)
        if active:
            task = self._create_task(con,application_id,dict(
                kind='complete_assessment' if kind == 'assessment' else 'offer_decision',
                owner='applicant',note=detail.get('title',kind.replace('_',' ')),due_at=due_at,
                source_time=source_time,evidence_id=evidence_id),context,stamp)
            task_id = task['task_id']
        state = dict(detail_id=detail_id,application_id=application_id,kind=kind,status=detail['status'],
            details_json=canonical_json(detail),evidence_id=evidence_id,source_time=source_time,
            task_id=task_id,revision_no=revision_no,created_at=old['created_at'] if old else stamp,updated_at=stamp)
        if old:
            con.execute('UPDATE lifecycle_details SET status=?,details_json=?,evidence_id=?,source_time=?,task_id=?,revision_no=?,updated_at=? WHERE detail_id=?',
                (state['status'],state['details_json'],evidence_id,source_time,task_id,revision_no,stamp,detail_id))
        else:
            con.execute('INSERT INTO lifecycle_details ('+','.join(state)+') VALUES ('+','.join('?' for _ in state)+')',tuple(state.values()))
        con.execute('INSERT INTO lifecycle_detail_revisions VALUES (?,?,?,?,?,?,?,?,?)',
            (_id(),detail_id,revision_no,canonical_json(state),context.actor_kind,context.source_kind,context.source_ref,source_time,stamp))
        if kind == 'offer':
            event_type = ApplicationEventType.OFFER_RECEIVED
            payload = {'detail_id':detail_id,'detail_revision':revision_no,'offer_outcome':detail['status']}
            if detail['status'] in {'accepted','declined','expired','employer_withdrawn'}:
                # The detail preserves distinct outcomes; the existing ledger contract
                # intentionally retains its three coarse terminal outcomes.
                event_type = ApplicationEventType.MANUAL_CORRECTION
                payload.update(reason='Reviewed offer '+detail['status'], target_phase='terminal',
                    target_outcome={'accepted':'accepted','declined':'withdrawn','expired':'rejected','employer_withdrawn':'rejected'}[detail['status']])
        else:
            event_type = ApplicationEventType.ASSESSMENT_COMPLETED if detail['status'] in {'submitted','completed'} else ApplicationEventType.ASSESSMENT_REQUESTED
            payload = {'detail_id':detail_id,'detail_revision':revision_no,'assessment_status':detail['status']}
        if not (kind == 'assessment' and detail['status'] == 'cancelled'):
            self.store._append_event(con,application_id,event_type,source_time,payload,
                f'lifecycle-detail:{detail_id}:{revision_no}',context,stamp)
            self.store._project(con,application_id)
        return {**state,'details':detail}

    def record_detail(self, application_id, kind, values, context, detail_id=None):
        _user(context)
        if detail_id and (not isinstance(values,Mapping) or values.get('expected_revision_no') is None):
            raise ContractError('updating a detail requires expected_revision_no')
        return self._core_command('record_detail',dict(application_id=application_id,kind=kind,values=values,detail_id=detail_id),context,
            lambda con,stamp:{'detail':self._record_detail(con,application_id,kind,values,context,stamp,detail_id)})

    def list_details(self, application_id, *, kind=None, limit=100, offset=0):
        _limit(limit)
        if isinstance(offset,bool) or not isinstance(offset,int) or offset < 0:
            raise ContractError('invalid offset')
        if kind is not None and kind not in ('assessment','offer'):
            raise ContractError('invalid detail kind')
        with connect(self.store.db_path) as con:
            rows = con.execute('SELECT * FROM lifecycle_details WHERE application_id=?'+(' AND kind=?' if kind else '')+' ORDER BY updated_at DESC,detail_id LIMIT ? OFFSET ?',
                (application_id,kind,limit,offset) if kind else (application_id,limit,offset))
            return [{**dict(row),'details':json.loads(row['details_json'])} for row in rows]

    def detail_history(self, detail_id):
        with connect(self.store.db_path) as con:
            return [{**dict(row),'state':json.loads(row['state_json'])} for row in con.execute('SELECT * FROM lifecycle_detail_revisions WHERE detail_id=? ORDER BY revision_no',(detail_id,))]

    def propose_correction(self, application_id, kind, payload, context, evidence_id=None):
        if context.actor_kind not in ('user','hermes','system','model'):
            raise ContractError('unsupported correction proposer')
        if kind not in ('phase','reopen','supersede_fact','association','duplicate','task','task_transition','detail'):
            raise ContractError('invalid correction kind')
        if not isinstance(payload,Mapping) or len(canonical_json(payload).encode()) > 20000:
            raise ContractError('correction payload must be a bounded object')
        def operation(con,stamp):
            self.store._application(con,application_id)
            self._evidence(con,application_id,evidence_id)
            proposal_id = _id()
            base_state = self._correction_base(con,application_id,kind,payload)
            con.execute('INSERT INTO lifecycle_correction_proposals VALUES (?,?,?,?,?,\'pending\',?,?,?,?,NULL,?)',
                (proposal_id,application_id,kind,canonical_json(payload),evidence_id,context.actor_kind,
                 context.source_kind,context.source_ref,stamp,canonical_json(base_state)))
            return {'proposal':self._correction(con,proposal_id)}
        return self._core_command('propose_correction',dict(application_id=application_id,kind=kind,payload=payload,evidence_id=evidence_id),context,operation)

    def _correction_base(self,con,application_id,kind,payload):
        app = self.store._application(con,application_id)
        base = {'last_event_seq':app['last_event_seq']}
        if kind == 'task_transition':
            task = _task(con,payload.get('task_id',''))
            if task['application_id'] != application_id:
                raise ContractError('task does not belong to application')
            base['task_revision'] = task['revision_no']
        if kind == 'detail' and payload.get('detail_id'):
            row = con.execute('SELECT application_id,revision_no FROM lifecycle_details WHERE detail_id=?',(payload['detail_id'],)).fetchone()
            if not row or row['application_id'] != application_id:
                raise ContractError('detail does not belong to application')
            base['detail_revision'] = row['revision_no']
        if kind == 'association':
            self._evidence(con,application_id,payload.get('evidence_id'))
        return base

    @staticmethod
    def _correction(con,proposal_id):
        row = con.execute('SELECT * FROM lifecycle_correction_proposals WHERE proposal_id=?',(proposal_id,)).fetchone()
        if not row:
            raise ContractError('correction proposal not found')
        return {**dict(row),'payload':json.loads(row['payload_json'])}

    def list_corrections(self, application_id=None, *, status='pending', limit=100):
        _limit(limit)
        if status not in (None,'pending','accepted','rejected'):
            raise ContractError('invalid correction status')
        where,args = [],[]
        for column,value in (('application_id',application_id),('status',status)):
            if value is not None:
                where.append(column+'=?'); args.append(value)
        with connect(self.store.db_path) as con:
            rows = con.execute('SELECT * FROM lifecycle_correction_proposals'+(' WHERE '+' AND '.join(where) if where else '')+' ORDER BY created_at,proposal_id LIMIT ?',(*args,limit))
            return [{**dict(row),'payload':json.loads(row['payload_json'])} for row in rows]

    def decide_correction(self, proposal_id, decision, context, reason=''):
        _user(context)
        if decision not in ('accepted','rejected'):
            raise ContractError('correction decision must be accepted or rejected')
        def operation(con,stamp):
            proposal = self._correction(con,proposal_id)
            if proposal['status'] != 'pending':
                raise ConflictError('correction has already been decided')
            result = {}
            if decision == 'accepted':
                app,kind,payload = proposal['application_id'],proposal['kind'],proposal['payload']
                if self._correction_base(con,app,kind,payload) != json.loads(proposal['base_state_json']):
                    raise ConflictError('application changed since proposal; review a new proposal')
                if kind == 'task':
                    _known(payload,('values',))
                    result['task'] = self._create_task(con,app,payload['values'],context,stamp)
                elif kind == 'task_transition':
                    _known(payload,('task_id','operation','values'))
                    if _task(con,payload['task_id'])['application_id'] != app:
                        raise ContractError('task does not belong to application')
                    result['task'] = self._transition_task(con,payload['task_id'],payload['operation'],payload.get('values',{}),context,stamp)
                elif kind == 'detail':
                    _known(payload,('kind','values','detail_id'))
                    result['detail'] = self._record_detail(con,app,payload['kind'],payload['values'],context,stamp,payload.get('detail_id'))
                elif kind == 'association':
                    _known(payload,('evidence_id','target_application_id','reason','target_phase','target_outcome'))
                    self._evidence(con,app,payload['evidence_id'])
                    applied = con.execute("SELECT 1 FROM event_proposals WHERE evidence_id=? AND proposed_application_id=? AND applied_event_id IS NOT NULL LIMIT 1",(payload['evidence_id'],app)).fetchone()
                    correction = None
                    if applied or 'target_phase' in payload:
                        correction = {key:payload[key] for key in ('target_phase','target_outcome') if key in payload}
                        correction['reason'] = _text(payload.get('reason',reason),'reason',1000)
                        correction['reassigned_evidence_id'] = payload['evidence_id']
                        correction['target_application_id'] = payload['target_application_id']
                        validate_event_payload(ApplicationEventType.MANUAL_CORRECTION,correction)
                    result['association'] = self.reassign_evidence(con,payload['evidence_id'],app,payload['target_application_id'],context,stamp)
                    for task in con.execute("SELECT task_id FROM lifecycle_tasks WHERE application_id=? AND evidence_id=? AND status='open'",(app,payload['evidence_id'])).fetchall():
                        self._transition_task(con,task['task_id'],'cancel',{},context,stamp)
                    if correction:
                        event,_ = self.store._append_event(con,app,ApplicationEventType.MANUAL_CORRECTION,stamp,correction,'association-correction:'+proposal_id,context,stamp)
                        self.store._project(con,app)
                        result['event'] = event
                    # Old schedules remain historical evidence; their future prompts
                    # must not survive an explicitly reviewed association repair.
                    temporal_ids = [row[0] for row in con.execute(
                        'SELECT t.temporal_proposal_id FROM temporal_proposals t JOIN mail_archive a USING(archive_id) JOIN mail_evidence e ON e.account_id=a.account_id AND e.immutable_message_id=a.immutable_message_id WHERE e.evidence_id=? AND t.application_id=?',
                        (payload['evidence_id'],app))]
                    for temporal_id in temporal_ids:
                        con.execute("UPDATE local_reminders SET status='dismissed',completed_at=? WHERE temporal_proposal_id=? AND status='pending'",(stamp,temporal_id))
                        con.execute("UPDATE accepted_interview_schedules SET status='cancelled' WHERE temporal_proposal_id=? AND status='active'",(temporal_id,))
                    if con.execute("SELECT 1 FROM sqlite_master WHERE name='interview_rounds'").fetchone():
                        rounds = con.execute("SELECT round_id FROM interview_rounds WHERE application_id=? AND (json_extract(details_json,'$.evidence_id')=? OR legacy_schedule_id IN (SELECT interview_schedule_id FROM accepted_interview_schedules WHERE status='cancelled' AND temporal_proposal_id IN (SELECT t.temporal_proposal_id FROM temporal_proposals t JOIN mail_archive a USING(archive_id) JOIN mail_evidence e ON e.account_id=a.account_id AND e.immutable_message_id=a.immutable_message_id WHERE e.evidence_id=?)))",(app,payload['evidence_id'],payload['evidence_id'])).fetchall()
                        for round_ in rounds:
                            con.execute("UPDATE interview_rounds SET status='cancelled',updated_at=? WHERE round_id=?",(stamp,round_['round_id']))
                            con.execute("UPDATE interview_reminders SET status='dismissed',completed_at=? WHERE round_id=? AND status='pending'",(stamp,round_['round_id']))
                else:
                    _known(payload,('reason','target_phase','target_outcome','occurred_at','event_id','target_application_id'))
                    if kind == 'supersede_fact':
                        event = self.store._event(con,payload.get('event_id',''))
                        if event['application_id'] != app:
                            raise ContractError('fact does not belong to application')
                        for task in con.execute("SELECT t.task_id FROM lifecycle_tasks t JOIN lifecycle_event_tasks e USING(task_id) WHERE e.event_id=? AND t.status='open'",(event['event_id'],)).fetchall():
                            self._transition_task(con,task['task_id'],'supersede',{},context,stamp)
                    if kind == 'duplicate':
                        target = payload.get('target_application_id')
                        if target == app:
                            raise ContractError('application cannot duplicate itself')
                        self.store._application(con,target)
                    corrected = dict(payload)
                    corrected['reason'] = _text(payload.get('reason',reason),'reason',1000)
                    if kind == 'reopen' and payload.get('target_phase') == 'terminal':
                        raise ContractError('reopen requires a nonterminal phase')
                    validate_event_payload(ApplicationEventType.MANUAL_CORRECTION,corrected)
                    occurred = _time(payload.get('occurred_at',stamp),'occurred_at')
                    event,_ = self.store._append_event(con,app,ApplicationEventType.MANUAL_CORRECTION,occurred,corrected,'lifecycle-correction:'+proposal_id,context,stamp)
                    self.store._project(con,app)
                    result['event'] = event
            con.execute('UPDATE lifecycle_correction_proposals SET status=?,decided_at=? WHERE proposal_id=?',(decision,stamp,proposal_id))
            con.execute('INSERT INTO lifecycle_correction_decisions VALUES (?,?,?,?,?,?,?)',(_id(),proposal_id,decision,context.actor_kind,reason[:1000],canonical_json(result),stamp))
            return {'proposal':self._correction(con,proposal_id),**result}
        return self._core_command('decide_correction',dict(proposal_id=proposal_id,decision=decision,reason=reason),context,operation)

    def list_unified_reminders(self, application_id=None, *, statuses=None, limit=100, offset=0):
        _limit(limit)
        normalized = tuple(statuses or ())
        if any(value not in ('scheduled','completed','cancelled') for value in normalized):
            raise ContractError('invalid reminder status')
        if isinstance(offset,bool) or not isinstance(offset,int) or offset < 0:
            raise ContractError('invalid offset')
        with connect(self.store.db_path) as con:
            queries = ["SELECT 'general:'||reminder_id reminder_id,reminder_id native_id,'general' source,application_id,note,due_at,status,status source_status FROM reminders"]
            for table,source in (('local_reminders','local'),('interview_reminders','interview')):
                if con.execute('SELECT 1 FROM sqlite_master WHERE name=?',(table,)).fetchone():
                    queries.append(f"SELECT '{source}:'||reminder_id,reminder_id,'{source}',application_id,kind,due_at,CASE status WHEN 'pending' THEN 'scheduled' WHEN 'dismissed' THEN 'cancelled' ELSE status END,status FROM {table}")
            clauses,args = [],[]
            if application_id:
                clauses.append('application_id=?'); args.append(application_id)
            if normalized:
                clauses.append('status IN ('+','.join('?' for _ in normalized)+')'); args.extend(normalized)
            # Source completion records durable publication, not external delivery.
            # Read the bounded reminder page first, then resolve its latest outbox
            # attempt using both legacy and source-qualified reminder references.
            page = 'SELECT * FROM ('+' UNION ALL '.join(queries)+')'+(' WHERE '+' AND '.join(clauses) if clauses else '')+' ORDER BY due_at,reminder_id LIMIT ? OFFSET ?'
            result = []
            for row in con.execute(page,(*args,limit,offset)).fetchall():
                reminder = dict(row)
                notification = con.execute(
                    "SELECT status FROM notification_outbox WHERE topic='reminder.due' "
                    "AND application_id=? AND json_extract(context_json,'$.reminder_id') IN (?,?) "
                    "ORDER BY created_at DESC,notification_id DESC LIMIT 1",
                    (reminder['application_id'],reminder['native_id'],reminder['reminder_id']),
                ).fetchone()
                reminder['delivery_status'] = (
                    {'delivered':'sent','dead':'failed'}.get(notification['status'],notification['status'])
                    if notification else ('unknown' if reminder['source_status'] == 'completed' else 'never_published')
                )
                result.append(reminder)
            return result

    def cancel_unified_reminder(self, reminder_id, context):
        _user(context)
        source,separator,native_id = reminder_id.partition(':')
        if not separator or source not in ('general','local','interview'):
            raise ContractError('source-qualified reminder ID required')
        if source == 'interview':
            return self.complete_interview_reminder(native_id,'dismissed',context)
        table = 'reminders' if source == 'general' else 'local_reminders'
        target_status = 'cancelled' if source == 'general' else 'dismissed'
        def operation(con,stamp):
            reminder = con.execute(f'SELECT * FROM {table} WHERE reminder_id=?',(native_id,)).fetchone()
            if not reminder:
                raise ContractError('reminder not found')
            queued = con.execute("SELECT notification_id FROM notification_outbox WHERE topic='reminder.due' AND application_id=? AND json_extract(context_json,'$.reminder_id') IN (?,?) AND status IN ('pending','delivering')",(reminder['application_id'],native_id,reminder_id)).fetchall()
            if reminder['status'] == 'completed' and not queued:
                raise ConflictError('delivered reminder cannot be cancelled')
            for notification in queued:
                con.execute("UPDATE notification_outbox SET status='cancelled',lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL WHERE notification_id=?",(notification['notification_id'],))
            if reminder['status'] != target_status:
                date_column = 'cancelled_at' if source == 'general' else 'completed_at'
                con.execute(f'UPDATE {table} SET status=?,{date_column}=? WHERE reminder_id=?',(target_status,stamp,native_id))
            result = dict(con.execute(f'SELECT * FROM {table} WHERE reminder_id=?',(native_id,)).fetchone())
            return {'cancelled':reminder['status'] != target_status,'reminder':result}
        return self._core_command('cancel_unified_reminder',dict(reminder_id=reminder_id),context,operation)

    def publish_due_tasks(self, now, context, *, limit=100, policy=None):
        context.validate()
        if context.actor_kind != 'system':
            raise ContractError('task publication requires system actor')
        _time(now,'now'); _limit(limit)
        policy = policy or NotificationPolicy()
        def operation(con,stamp):
            tasks = con.execute("SELECT t.* FROM lifecycle_tasks t JOIN applications a USING(application_id) WHERE t.status='open' AND a.current_phase!='terminal' AND t.due_at<=? AND (t.snoozed_until IS NULL OR t.snoozed_until<=?) AND (t.due_at>=t.created_at OR t.revision_no>1) AND NOT EXISTS (SELECT 1 FROM lifecycle_task_revisions r WHERE r.task_id=t.task_id AND r.revision_no=t.revision_no AND r.operation='reviewed_deadline' AND t.due_at<t.updated_at) AND NOT EXISTS (SELECT 1 FROM notification_outbox n WHERE n.dedupe_key='lifecycle-task:'||t.task_id||':'||t.revision_no) ORDER BY t.due_at,t.task_id LIMIT ?",(now,now,limit)).fetchall()
            published = 0
            for row in tasks:
                task = dict(row)
                # Old discoveries remain visible, but never flood notification delivery.
                if task['due_at'] < task['created_at'] and task['revision_no'] == 1:
                    continue
                intent = NotificationIntent(topic='reminder.due',source_id=task['task_id']+':'+str(task['revision_no']),
                    title='Application task due',body=task['note'],application_id=task['application_id'],context={'application_id':task['application_id'],'reminder_id':'task:'+task['task_id']})
                notification = policy.evaluate(intent,available_at=now)
                if notification is None:
                    continue
                if policy.policy_id == 'chief-of-staff-v1':
                    result = self.ledger.attention.from_notification(intent, context=context, now=now, con=con)
                    published += int(result.get('created', False))
                    continue
                published += con.execute("INSERT OR IGNORE INTO notification_outbox (notification_id,dedupe_key,topic,policy_id,application_id,title,body,context_json,status,max_attempts,available_at,created_at) VALUES (?,?,?,?,?,?,?,?,'pending',?,?,?)",
                    (_id(),'lifecycle-task:'+task['task_id']+':'+str(task['revision_no']),notification['topic'],notification['policy_id'],task['application_id'],notification['title'],notification['body'],canonical_json(notification['context']),notification['max_attempts'],now,stamp)).rowcount
            return {'published':published,'examined':len(tasks)}
        return self._core_command('publish_due_tasks',dict(now=now,limit=limit,policy_id=policy.policy_id,enabled_topics=sorted(policy.enabled_topics)),context,operation)

    def publish_due_interview_reminders(self, now, context, *, limit=100, policy=None):
        if context.actor_kind != 'system':
            raise ContractError('reminder publication requires system actor')
        _time(now,'now'); _limit(limit)
        policy = policy or NotificationPolicy()
        def operation(con,stamp):
            if not con.execute("SELECT 1 FROM sqlite_master WHERE name='interview_reminders'").fetchone():
                return {'published':0}
            rows = con.execute("SELECT r.* FROM interview_reminders r JOIN applications a USING(application_id) WHERE r.status='pending' AND r.due_at<=? AND a.current_phase!='terminal' ORDER BY r.due_at,r.reminder_id LIMIT ?",(now,limit)).fetchall()
            count = 0
            for reminder in rows:
                intent = NotificationIntent(topic='reminder.due',source_id='interview:'+reminder['reminder_id'],title='Interview reminder',body='An upcoming interview needs your attention.',application_id=reminder['application_id'],context={'reminder_id':'interview:'+reminder['reminder_id']})
                notification = policy.evaluate(intent,available_at=now)
                if not notification:
                    continue
                if policy.policy_id == 'chief-of-staff-v1':
                    result = self.ledger.attention.from_notification(intent, context=context, now=now, con=con)
                    count += int(result.get('created', False))
                    con.execute("UPDATE interview_reminders SET status='completed',completed_at=? WHERE reminder_id=?",(stamp,reminder['reminder_id']))
                    continue
                count += con.execute("INSERT OR IGNORE INTO notification_outbox (notification_id,dedupe_key,topic,policy_id,application_id,title,body,context_json,status,max_attempts,available_at,created_at) VALUES (?,?,?,?,?,?,?,?,'pending',?,?,?)",
                    (_id(),notification['dedupe_key'],notification['topic'],notification['policy_id'],reminder['application_id'],notification['title'],notification['body'],canonical_json(notification['context']),notification['max_attempts'],now,stamp)).rowcount
                con.execute("UPDATE interview_reminders SET status='completed',completed_at=? WHERE reminder_id=?",(stamp,reminder['reminder_id']))
            return {'published':count}
        return self._core_command('publish_due_interview_reminders',dict(now=now,limit=limit,policy_id=policy.policy_id,enabled_topics=sorted(policy.enabled_topics)),context,operation)

    def configure_follow_up(self, application_id, after_days, context):
        _user(context)
        if after_days is not None and (isinstance(after_days,bool) or not isinstance(after_days,int) or not 1 <= after_days <= 90):
            raise ContractError('follow-up delay must be 1 to 90 days or null to disable')
        def operation(con,stamp):
            self.store._application(con,application_id)
            row = con.execute('SELECT * FROM lifecycle_follow_up_policies WHERE application_id=?',(application_id,)).fetchone()
            version = row['policy_version']+1 if row else 1
            con.execute('INSERT INTO lifecycle_follow_up_policies VALUES (?,?,?,?) ON CONFLICT(application_id) DO UPDATE SET after_days=excluded.after_days,policy_version=excluded.policy_version,updated_at=excluded.updated_at',(application_id,after_days,version,stamp))
            con.execute('INSERT INTO lifecycle_follow_up_policy_revisions VALUES (?,?,?,?,?,?)',(_id(),application_id,after_days,version,context.actor_kind,stamp))
            for task in con.execute("SELECT task_id FROM lifecycle_tasks WHERE application_id=? AND kind='follow_up' AND status='open' AND policy_version LIKE 'follow-up:%'",(application_id,)).fetchall():
                self._transition_task(con,task['task_id'],'cancel',{},context,stamp)
            return {'application_id':application_id,'after_days':after_days,'policy_version':version}
        return self._core_command('configure_follow_up',dict(application_id=application_id,after_days=after_days),context,operation)

    def evaluate_follow_ups(self, now, context, *, limit=100):
        from datetime import timedelta
        if context.actor_kind != 'system':
            raise ContractError('follow-up evaluation requires system actor')
        _time(now,'now'); _limit(limit)
        def operation(con,stamp):
            if not con.execute("SELECT 1 FROM sqlite_master WHERE name='lifecycle_mail_observations'").fetchone():
                return {'created':0,'cancelled':0}
            policies = con.execute("SELECT p.* FROM lifecycle_follow_up_policies p JOIN applications a USING(application_id) WHERE p.after_days IS NOT NULL AND a.current_phase!='terminal' ORDER BY p.application_id").fetchall()
            created = cancelled = 0
            for policy in policies:
                # Observation schema exposes source time independently of ingest time.
                rows = con.execute("SELECT o.* FROM lifecycle_mail_observations o JOIN lifecycle_mail_links l USING(observation_id) WHERE l.application_id=? AND o.direction IN ('inbound','outbound') ORDER BY o.source_at DESC,o.observation_id DESC LIMIT 1",(policy['application_id'],)).fetchall()
                if not rows:
                    continue
                latest = dict(rows[0])
                for task in con.execute("SELECT * FROM lifecycle_tasks WHERE application_id=? AND kind='follow_up' AND status='open' AND policy_version LIKE 'follow-up:%'",(policy['application_id'],)).fetchall():
                    if task['source_time'] < latest['source_at']:
                        self._transition_task(con,task['task_id'],'cancel',{},context,stamp); cancelled += 1
                if latest['direction'] != 'outbound':
                    continue
                existing = con.execute('SELECT 1 FROM lifecycle_follow_up_observations WHERE observation_id=? AND policy_version=?',(latest['observation_id'],policy['policy_version'])).fetchone()
                if existing:
                    continue
                due = (parse_utc(latest['source_at'])+timedelta(days=policy['after_days'])).isoformat(timespec='seconds').replace('+00:00','Z')
                # Create upcoming obligations, suppress historical reminder flood in publisher.
                task = self._create_task(con,policy['application_id'],dict(kind='follow_up',owner='applicant',note='Consider following up on your last message.',due_at=due,source_time=latest['source_at'],evidence_id=latest['evidence_id'],policy_version='follow-up:'+str(policy['policy_version'])),context,stamp)
                con.execute('INSERT INTO lifecycle_follow_up_observations VALUES (?,?,?)',(latest['observation_id'],policy['policy_version'],task['task_id']))
                created += 1
                if created >= limit:
                    break
            return {'created':created,'cancelled':cancelled}
        return self._core_command('evaluate_follow_ups',dict(now=now,limit=limit),context,operation)


def ensure_event_task(con, store, event, evidence_id, stamp):
    """Create one conservative obligation per applied lifecycle fact under its lock."""
    mapping = {'assessment_requested':('complete_assessment','applicant'),
               'interview_requested':('send_availability','applicant'),
               'offer_received':('offer_decision','applicant')}
    if event['event_type'] not in mapping or event.get('payload',{}).get('detail_id'):
        return
    if con.execute('SELECT 1 FROM lifecycle_event_tasks WHERE event_id=?',(event['event_id'],)).fetchone():
        return
    if store._application(con,event['application_id'])['current_phase'] == 'terminal':
        return
    service = CoreMixin()
    service.store = store
    kind,owner = mapping[event['event_type']]
    prior = con.execute("SELECT task_id FROM lifecycle_tasks WHERE application_id=? AND evidence_id=? AND kind=? AND status='open' ORDER BY created_at,task_id LIMIT 1",(event['application_id'],evidence_id,kind)).fetchone() if evidence_id else None
    if prior:
        con.execute('INSERT INTO lifecycle_event_tasks VALUES (?,?)',(event['event_id'],prior['task_id']))
        return
    task = service._create_task(con,event['application_id'],dict(kind=kind,owner=owner,
        note=kind.replace('_',' '),source_time=event['occurred_at'],evidence_id=evidence_id),
        MutationContext('event-task:'+event['event_id'],'system','lifecycle_event',event['event_id']),stamp)
    con.execute('INSERT INTO lifecycle_event_tasks VALUES (?,?)',(event['event_id'],task['task_id']))


def ensure_deadline_task(con, store, proposal, context, stamp):
    """Turn a reviewed deadline into an obligation without inventing its meaning."""
    application_id = proposal['application_id']
    evidence = con.execute('SELECT e.evidence_id,e.received_at FROM mail_evidence e JOIN mail_archive a ON a.account_id=e.account_id AND a.immutable_message_id=e.immutable_message_id WHERE a.archive_id=?',(proposal['archive_id'],)).fetchone()
    evidence_id = evidence['evidence_id'] if evidence else None
    source_time = evidence['received_at'] if evidence else stamp
    if evidence_id:
        from .mail import link_accepted_evidence
        link_accepted_evidence(con,evidence_id,application_id,context,stamp)
    existing = con.execute("SELECT * FROM lifecycle_tasks WHERE application_id=? AND evidence_id=? AND status='open' ORDER BY created_at,task_id",(application_id,evidence_id)).fetchall() if evidence_id else []
    # If several requests share a message the date cannot safely pick one.
    if len(existing) == 1:
        task = dict(existing[0])
        task.update(due_at=proposal['due_at'],revision_no=task['revision_no']+1,updated_at=stamp)
        con.execute('UPDATE lifecycle_tasks SET due_at=?,revision_no=?,updated_at=? WHERE task_id=?',(task['due_at'],task['revision_no'],stamp,task['task_id']))
        _revision(con,task,'reviewed_deadline',context,stamp)
        _cancel_task_notifications(con,task['task_id'])
        return task
    service = CoreMixin()
    service.store = store
    return service._create_task(con,application_id,dict(kind='follow_up',owner='unknown',
        note='Review recorded deadline',due_at=proposal['due_at'],source_time=source_time,
        evidence_id=evidence_id,policy_version='reviewed-deadline-v1'),context,stamp)
