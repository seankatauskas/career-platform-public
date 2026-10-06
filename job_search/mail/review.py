"""Previewed, user-authorized resolutions for legacy email review, singly or in batches.

Interpretation belongs to the reviewer. This boundary validates exact evidence,
previews effects, and applies the entire batch under one ledger transaction.
"""
from dataclasses import asdict
import json
import uuid

from ..contracts import (ApplicationEventType, ContractError, ConflictError,
                         JobSnapshot, RecommendationProvenance, canonical_json,
                         parse_utc, payload_sha256, validate_event_payload)
from ..db import connect
from ..lifecycle.core import TASK_KINDS
from ..lifecycle.mail import link_accepted_evidence
from ..reducer import reduce_events, EVENT_OUTCOME
from .archive_source import _parts


EVENT_TYPES = tuple(e.value for e in ApplicationEventType if e.value not in
                    {'application_started', 'manual_correction', 'submission_observed'})
MAX_BATCH = 50


def _text(value, name, maximum=1000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ContractError(f'{name} must be 1 to {maximum} characters')
    return value.strip()


class MailReviewService:
    def __init__(self, ledger):
        self.ledger, self.store = ledger, ledger.store

    def applications(self, query='', limit=50, after=''):
        if not isinstance(query, str) or len(query) > 300 or type(limit) is not int or not 1 <= limit <= 100 or not isinstance(after, str):
            raise ContractError('invalid application search')
        with connect(self.store.db_path) as con:
            rows = con.execute("SELECT application_id,employer_snapshot,title_snapshot,current_phase,terminal_outcome,submitted_at "
                "FROM applications WHERE application_id>? AND instr(lower(employer_snapshot || ' ' || title_snapshot),lower(?))>0 "
                "ORDER BY application_id LIMIT ?", (after, query.strip(), limit+1)).fetchall()
        return {'applications': [dict(row) for row in rows[:limit]],
                'next_cursor': rows[limit-1]['application_id'] if len(rows)>limit else None}

    def list_pending(self, limit=50, after=''):
        if type(limit) is not int or not 1 <= limit <= 100 or not isinstance(after, str):
            raise ContractError('invalid pending review page')
        with connect(self.store.db_path) as con:
            rows = con.execute("SELECT p.*,e.subject,e.sender,e.received_at,e.excerpt "
                "FROM event_proposals p JOIN mail_evidence e USING(evidence_id) "
                "WHERE p.status IN ('pending','conflict') AND p.understanding_finding_id IS NULL "
                "AND NOT EXISTS(SELECT 1 FROM mail_understanding_ownership o WHERE o.evidence_id=p.evidence_id) "
                "AND p.proposal_id>? ORDER BY p.proposal_id LIMIT ?", (after, limit+1)).fetchall()
        items = []
        for row in rows[:limit]:
            item = dict(row)
            subject, body = _parts(item.pop('excerpt'))
            item.update(subject=subject or item['subject'], body=body,
                        candidate_application_ids=self.store._unassigned_mail_candidates(item['evidence_id']))
            for key in ('payload_json', 'candidate_application_ids_json'):
                item.pop(key, None)
            items.append(item)
        return {'items': items, 'event_types': EVENT_TYPES, 'task_kinds': sorted(TASK_KINDS),
                'next_cursor': rows[limit-1]['proposal_id'] if len(rows)>limit else None,
                'body_note': 'Saved evidence text; open the message to read the full archive.'}

    def _prepare(self, con, decisions):
        if not isinstance(decisions, list) or not 1 <= len(decisions) <= MAX_BATCH:
            raise ContractError(f'provide 1 to {MAX_BATCH} decisions')
        allowed = {'proposal_id', 'decision', 'application_id', 'new_application', 'event_type',
                   'evidence_quote', 'reason', 'task'}
        normalized, baseline, changes, seen, new_names, simulated = [], [], [], set(), set(), {}
        for raw in decisions:
            if not isinstance(raw, dict) or set(raw) - allowed:
                raise ContractError('unknown mail review fields')
            pid = _text(raw.get('proposal_id'), 'proposal_id', 256)
            if pid in seen:
                raise ContractError('a proposal may appear only once per batch')
            seen.add(pid)
            row = con.execute('SELECT * FROM event_proposals WHERE proposal_id=?', (pid,)).fetchone()
            if row is None or row['status'] not in ('pending', 'conflict'):
                raise ConflictError('email review changed or was already resolved; refresh before deciding')
            proposal = dict(row)
            from .understanding_store import owns_evidence
            if proposal.get('understanding_finding_id') or owns_evidence(con, proposal['evidence_id']):
                raise ContractError('use the grouped email finding review for this proposal')
            evidence = dict(con.execute('SELECT * FROM mail_evidence WHERE evidence_id=?',
                                        (proposal['evidence_id'],)).fetchone())
            observation = con.execute('SELECT * FROM lifecycle_mail_observations WHERE evidence_id=?',
                                      (proposal['evidence_id'],)).fetchone()
            decision = raw.get('decision')
            if decision not in ('record', 'keep', 'dismiss'):
                raise ContractError('decision must be record, keep, or dismiss')
            reason = _text(raw.get('reason'), 'reason')
            choice = {'proposal_id': pid, 'decision': decision, 'reason': reason}
            app, app_id, new_app = None, raw.get('application_id'), raw.get('new_application')
            previous_projection = None
            if decision == 'dismiss':
                if any(raw.get(k) for k in ('application_id', 'new_application', 'event_type', 'task', 'evidence_quote')):
                    raise ContractError('dismiss does not change an application or create a task')
            else:
                if bool(app_id) == bool(new_app):
                    raise ContractError('choose an existing application or create one')
                if observation and observation['direction'] in ('outbound', 'draft'):
                    raise ContractError('outbound mail cannot be recorded as an employer update')
                if app_id:
                    app_id = _text(app_id, 'application_id', 256)
                    app = dict(self.store._application(con, app_id))
                    if app_id in simulated:
                        previous_projection = reduce_events(simulated[app_id])
                    choice['application_id'] = app_id
                else:
                    if not isinstance(new_app, dict) or set(new_app) != {'employer', 'title'}:
                        raise ContractError('new_application requires employer and title')
                    new_app = {k: _text(new_app[k], k, 300 if k == 'employer' else 500) for k in new_app}
                    identity = tuple(new_app[k].casefold() for k in ('employer', 'title'))
                    if identity in new_names or con.execute('SELECT 1 FROM applications WHERE lower(trim(employer_snapshot))=? '
                            'AND lower(trim(title_snapshot))=?', identity).fetchone():
                        raise ConflictError('this employer and role already have an application; select it instead')
                    new_names.add(identity)
                    choice['new_application'] = new_app
                if observation:
                    links = [r[0] for r in con.execute('SELECT application_id FROM lifecycle_mail_links WHERE observation_id=?',
                                                      (observation['observation_id'],))]
                    if links and set(links) != {app_id}:
                        raise ConflictError('this message is already linked elsewhere; use the association correction workflow')
            event_type, quote = None, None
            if decision == 'record':
                event_type = raw.get('event_type') or proposal['event_type']
                if event_type not in EVENT_TYPES:
                    raise ContractError('unsupported application update')
                quote = raw.get('evidence_quote')
                if quote is None and event_type == proposal['event_type']:
                    quote = proposal['evidence_quote']
                # Preserve the exact substring, including whitespace, for provenance.
                if not isinstance(quote, str) or not quote.strip() or len(quote) > 512 or quote not in evidence['excerpt']:
                    raise ContractError('provide an exact supporting quote from the saved email evidence (up to 512 characters)')
                payload = json.loads(proposal['payload_json']) if event_type == proposal['event_type'] else {}
                validate_event_payload(ApplicationEventType(event_type), payload)
                outcome = EVENT_OUTCOME.get(ApplicationEventType(event_type))
                phase = previous_projection.current_phase.value if previous_projection else app['current_phase'] if app else None
                prior_outcome = previous_projection.terminal_outcome.value if previous_projection and previous_projection.terminal_outcome else app.get('terminal_outcome') if app else None
                if phase == 'terminal' and outcome and prior_outcome != outcome.value:
                    raise ConflictError('this outcome conflicts with the closed application; use lifecycle correction')
                choice.update(event_type=event_type, evidence_quote=quote)
            elif raw.get('event_type') or raw.get('evidence_quote'):
                raise ContractError('only record decisions may specify an event and quote')
            tasks = [dict(r) for r in con.execute('SELECT * FROM lifecycle_tasks WHERE application_id=? ORDER BY task_id',
                                                 (app_id,))] if app_id else []
            task = raw.get('task')
            if task:
                if decision == 'dismiss' or not isinstance(task, dict) or set(task) - {'kind', 'note', 'due_at'}:
                    raise ContractError('invalid next step')
                if task.get('kind') not in TASK_KINDS:
                    raise ContractError('unsupported next step')
                task = {'kind': task['kind'], 'note': _text(task.get('note'), 'task note', 2000),
                        **({'due_at': task['due_at']} if task.get('due_at') else {})}
                if task.get('due_at'):
                    parse_utc(task['due_at'])
                if any(t['evidence_id'] == evidence['evidence_id'] and t['kind'] == task['kind'] for t in tasks):
                    raise ConflictError('this message already has that next step; review the existing task')
                choice['task'] = task
            before = app['current_phase'] if app else 'preparing' if new_app else None
            after, after_outcome = before, app.get('terminal_outcome') if app else None
            if previous_projection:
                before = after = previous_projection.current_phase.value
                after_outcome = previous_projection.terminal_outcome.value if previous_projection.terminal_outcome else None
            if decision == 'record':
                virtual_id = app_id or pid
                events = simulated.setdefault(virtual_id, list(self.store._events(con, app_id)) if app_id else [
                    {'event_type': 'application_started', 'event_seq': 1, 'occurred_at': evidence['received_at'],
                     'recorded_at': evidence['received_at'], 'payload_json': '{}'}])
                events.append({'event_type': event_type, 'event_seq': len(events) + 1, 'occurred_at': evidence['received_at'],
                               'recorded_at': evidence['received_at'], 'payload_json': canonical_json(payload)})
                projected = reduce_events(events)
                after, after_outcome = projected.current_phase.value, projected.terminal_outcome.value if projected.terminal_outcome else None
            if task and after == 'terminal':
                raise ContractError('a closed application cannot have a new next step')
            change = {'proposal_id': pid, 'subject': evidence['subject'], 'decision': decision,
                      'application': f"{app['employer_snapshot']} · {app['title_snapshot']}" if app else
                          f"{new_app['employer']} · {new_app['title']}" if new_app else None,
                      'creates_application': bool(new_app), 'event_type': event_type,
                      'from_phase': before, 'to_phase': after, 'terminal_outcome': after_outcome,
                      'next_step': task, 'reason': reason,
                      'closes_application_work': after == 'terminal' and before != 'terminal'}
            baseline.append({'proposal': proposal, 'evidence': evidence, 'application': app, 'tasks': tasks,
                             'observation': dict(observation) if observation else None})
            normalized.append(choice)
            changes.append(change)
        # Avoid creating a task that another choice in this batch immediately closes.
        terminal_apps = {c.get('application_id') or c['proposal_id'] for c in normalized if c.get('event_type') in
                         {'rejection_received', 'offer_accepted', 'withdrawn'}}
        if any(c.get('task') and (c.get('application_id') or c['proposal_id']) in terminal_apps for c in normalized):
            raise ContractError('the batch closes an application that also has a new next step')
        return {'decisions': normalized, 'changes': changes,
                'preview_hash': payload_sha256({'decisions': normalized, 'baseline': baseline}),
                'notice': 'Records application updates and your chosen next steps. Does not send email or change calendars.'}

    def preview(self, decisions):
        with connect(self.store.db_path) as con:
            con.execute('BEGIN')
            return self._prepare(con, decisions)

    def apply(self, decisions, preview_hash, context):
        context.validate()
        if context.actor_kind != 'user':
            raise ContractError('mail review resolution requires a user-authorized decision')
        def operation(con, stamp):
            plan = self._prepare(con, decisions)
            if plan['preview_hash'] != preview_hash:
                raise ConflictError('review or application changed; preview this batch again before saving')
            results = []
            for choice in plan['decisions']:
                pid, decision = choice['proposal_id'], choice['decision']
                original = dict(con.execute('SELECT * FROM event_proposals WHERE proposal_id=?', (pid,)).fetchone())
                app_id = choice.get('application_id')
                if choice.get('new_application'):
                    new = choice['new_application']
                    app_id = uuid.uuid4().hex
                    snapshot = JobSnapshot('external', uuid.uuid4().hex, '', new['title'], new['employer'], '', '')
                    con.execute("INSERT INTO applications (application_id,ats,job_id,title_snapshot,employer_snapshot,job_url_snapshot,current_phase,started_at,last_activity_at,last_event_seq,projection_sha256,updated_at) VALUES (?,'external',?,?,?,'','preparing',?,?,0,'',?)",
                                (app_id, snapshot.job_id, new['title'], new['employer'], stamp, stamp, stamp))
                    self.store._append_event(con, app_id, ApplicationEventType.APPLICATION_STARTED, stamp,
                        {'snapshot': asdict(snapshot), 'provenance': asdict(RecommendationProvenance())}, 'mail-review-start:'+pid, context, stamp)
                    self.store._project(con, app_id)
                replacement = None
                if decision == 'record':
                    replacement = uuid.uuid4().hex
                    evidence = con.execute('SELECT excerpt FROM mail_evidence WHERE evidence_id=?', (original['evidence_id'],)).fetchone()
                    quote = choice['evidence_quote']; start = evidence['excerpt'].index(quote)
                    payload = original['payload_json'] if choice['event_type'] == original['event_type'] else '{}'
                    con.execute("INSERT INTO event_proposals (proposal_id,dedupe_key,evidence_id,proposed_application_id,event_type,producer_kind,producer_version,confidence,candidate_application_ids_json,evidence_quote,span_start,span_end,payload_json,status,created_at) VALUES (?,?,?,?,?,'rule','user-reviewed-v1',1,?,?,?,?,?,'pending',?)",
                        (replacement, 'mail-review:'+pid, original['evidence_id'], app_id, choice['event_type'],
                         canonical_json([app_id]), quote, start, start+len(quote), payload, stamp))
                    result = self.store._decide_event_proposal(con, stamp, replacement, 'accepted', app_id,
                        choice['reason']+'; reviewed replacement of '+pid, context, create_tasks=False)
                    if result['decision'] != 'accepted':
                        raise ConflictError('review conflicts with current application state')
                elif decision == 'keep':
                    link_accepted_evidence(con, original['evidence_id'], app_id, context, stamp)
                if choice.get('task'):
                    self.ledger.lifecycle._create_task(con, app_id,
                        {**choice['task'], 'owner': 'applicant', 'evidence_id': original['evidence_id']}, context, stamp)
                reason = f"{decision}: {choice['reason']}" + (f'; replacement {replacement}' if replacement else '')
                con.execute("UPDATE event_proposals SET status='rejected',decided_at=? WHERE proposal_id=?", (stamp, pid))
                con.execute('INSERT INTO event_proposal_decisions VALUES (?,?,?,?,?,?,?)',
                    (uuid.uuid4().hex, pid, 'rejected', app_id, 'user', reason[:1000], stamp))
                results.append({'proposal_id': pid, 'decision': decision, 'application_id': app_id,
                                'replacement_proposal_id': replacement})
            return {'resolved': results, 'preview_hash': preview_hash}
        return self.store._idempotent('mail_review.resolve', context,
            {'decisions': decisions, 'preview_hash': preview_hash, 'actor_kind': context.actor_kind,
             'source_kind': context.source_kind, 'source_ref': context.source_ref}, operation)
