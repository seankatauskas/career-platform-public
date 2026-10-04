"""One evidence-backed briefing shared by the dashboard and agent tools."""
import json
from job_search.contracts import ContractError, utc_now, validate_identifier
from job_search.db import connect


class BriefingMixin:
    def list_lifecycle_reviews(self, application_id=None, *, limit=100, offset=0):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ContractError('review limit must be between 1 and 100')
        if type(offset) is not int or offset < 0:
            raise ContractError('invalid review offset')
        if application_id:
            validate_identifier(application_id, 'application_id')
        result = []
        with connect(self.store.db_path) as con:
            sources = {
                'lifecycle_correction':('lifecycle_correction_proposals','proposal_id'),
                'interview_revision':('interview_revisions','revision_id'),
                'mail_discovery':('lifecycle_discoveries','discovery_id'),
            }
            queries, parameters = [], []
            for kind, (table, key) in sources.items():
                where = "status IN ('pending','conflict')" if kind == 'interview_revision' else "status='pending'"
                if application_id:
                    where += ' AND application_id=?'
                    parameters.append(application_id)
                queries.append(f"SELECT {key} id,created_at,'{kind}' kind FROM {table} WHERE {where}")
            page = con.execute('SELECT * FROM ('+' UNION ALL '.join(queries)+') ORDER BY created_at,id LIMIT ? OFFSET ?',(*parameters,limit,offset)).fetchall()
            for selected in page:
                kind = selected['kind']
                table, key = sources[kind]
                item = dict(con.execute(f'SELECT * FROM {table} WHERE {key}=?',(selected['id'],)).fetchone())
                for field in ('payload_json','details_json'):
                    if field in item:
                        item[field.removesuffix('_json')] = json.loads(item.pop(field))
                result.append({'id':item[key], 'kind':kind, 'status':item['status'], 'application_id':item.get('application_id'), 'created_at':item['created_at'], 'detail':item.get('kind') or kind, 'proposal':item})
        for item in result:
            if item['kind'] == 'mail_discovery':
                observation = self.get_mail_observation(item['proposal']['observation_id'])
                item['proposal']['observation'] = observation
                if observation.get('evidence_id'):
                    item['evidence_quote'] = self.ledger.get_sanitized_evidence(observation['evidence_id'])['excerpt']
        return sorted(result, key=lambda item:(item['created_at'],item['id']))[:limit]

    def get_record_history(self, kind, record_id, *, limit=25, after_revision=0):
        """Read immutable task/detail revisions with bounded stable continuation."""
        validate_identifier(record_id, 'record_id')
        if type(limit) is not int or not 1 <= limit <= 100 or type(after_revision) is not int or after_revision < 0:
            raise ContractError('invalid history page')
        tables = {'task': ('lifecycle_task_revisions', 'task_id'), 'detail': ('lifecycle_detail_revisions', 'detail_id')}
        if kind not in tables:
            raise ContractError('invalid history kind')
        table, key = tables[kind]
        with connect(self.store.db_path) as con:
            rows = con.execute(f'SELECT * FROM {table} WHERE {key}=? AND revision_no>? ORDER BY revision_no LIMIT ?', (record_id,after_revision,limit+1)).fetchall()
        items = []
        for row in rows[:limit]:
            value = dict(row)
            value['state'] = json.loads(value.pop('state_json'))
            if 'details_json' in value['state']:
                value['state']['details'] = json.loads(value['state'].pop('details_json'))
            items.append(value)
        return {'items':items, 'complete':len(rows)<=limit, 'next_revision':items[-1]['revision_no'] if len(rows)>limit else None}

    def get_application_briefing(self, application_id):
        validate_identifier(application_id, 'application_id')
        timeline = self.ledger.get_application_timeline(application_id)
        app = timeline['application']
        with connect(self.store.db_path) as con:
            policy = con.execute('SELECT after_days,policy_version,updated_at FROM lifecycle_follow_up_policies WHERE application_id=?',(application_id,)).fetchone()
            follow_up = dict(policy) if policy else {'after_days':None}
        tasks = list(self.list_tasks(application_id, limit=100))
        obligations = list(self.list_tasks(application_id, status='open', limit=100))
        open_ids = {task['task_id'] for task in obligations}
        tasks = [*obligations, *(task for task in tasks if task['task_id'] not in open_ids)]
        obligations.sort(key=lambda t:(t.get('snoozed_until') or t.get('due_at') or '9999',t['task_id']))
        conversation = self.list_application_conversation(application_id, limit=20)
        messages = conversation.get('items', [])
        reminders = list(self.list_unified_reminders(application_id, limit=50))
        details = list(self.list_details(application_id, limit=50))
        rounds = self.list_interview_rounds(application_id=application_id, statuses=['confirmed','rescheduled','proposed'], limit=50)
        reviews = self.list_lifecycle_reviews(application_id, limit=30)
        attention = [item for item in self.ledger.list_attention_items() if item.get('application_id') == application_id or application_id in item.get('candidate_application_ids',())]
        actions = [a for a in self.ledger.list_actions() if a.get('application_id') == application_id]
        evidence = []
        for event in timeline['events'][-50:]:
            if event.get('email_evidence'):
                evidence.append({'event_id':event['event_id'],'event_type':event['event_type'],'occurred_at':event['occurred_at'],**event['email_evidence']})
        last = {}
        for direction in ('inbound','outbound','draft'):
            candidates = [m for m in messages if m.get('direction') == direction]
            last[direction] = max(candidates, key=lambda m:m.get('source_at') or m.get('updated_at') or '', default=None)
        if app['current_phase'] == 'terminal':
            explanation = 'This application is closed: ' + str(app.get('terminal_outcome') or 'outcome recorded') + '.'
        elif obligations:
            owners = sorted(set(t['owner'] for t in obligations))
            explanation = ('Next steps are assigned to ' + ', '.join(owners) + '. ' + (obligations[0].get('note') or obligations[0]['kind'].replace('_',' ')))
        else:
            explanation = 'Stage: ' + app['current_phase'].replace('_',' ') + '. The next responsible party has not been recorded.'
        return {
            'application':app, 'explanation':explanation, 'follow_up':follow_up,
            'next_obligations':obligations[:20], 'tasks':tasks[:50],
            'conversation':conversation, 'last_messages_in_returned_page':last,
            'reminders':reminders, 'details':details, 'interviews':rounds,
            'legacy_interviews':[r for r in self.ledger.list_interview_schedules(limit=1000, application_id=application_id) if r['application_id']==application_id and r['interview_schedule_id'] not in {x.get('legacy_schedule_id') for x in rounds['rounds']}],
            'pending_reviews':reviews, 'attention':attention[:30],
            'actions':[{'action_id':a['action_id'],'kind':a['kind'],'status':a['status'],'created_at':a['created_at']} for a in actions[:20]],
            'evidence':evidence[-20:], 'coverage':self.mail_coverage(application_id),
            'as_of':utc_now(), 'limits':{'tasks':50,'messages':20,'details':50,'interviews':50,'evidence':20,'reviews':30},
            'truncated':bool(rounds.get('next_offset')) or len(tasks)>50 or not conversation.get('complete',False) or len(details)>=50 or len(reviews)>=30 or len(evidence)>20 or len(actions)>20,
        }

    def list_upcoming_interviews(self, limit=100):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ContractError('interview limit must be between 1 and 100')
        now = utc_now()
        rounds = self.list_interview_rounds(statuses=['confirmed','rescheduled'], starts_after=now, limit=limit)['rounds']
        with connect(self.store.db_path) as con:
            legacy = [dict(row) for row in con.execute(
                "SELECT * FROM accepted_interview_schedules s WHERE status='active' AND julianday(starts_at)>=julianday(?) AND NOT EXISTS (SELECT 1 FROM interview_rounds r WHERE r.legacy_schedule_id=s.interview_schedule_id) ORDER BY starts_at,interview_schedule_id LIMIT ?", (now,limit))]
        for row in rounds:
            row['employer_confirmed'] = row.get('details',{}).get('employer_confirmed')
        for row in legacy:
            row['employer_confirmed'] = None
        return sorted([*rounds,*legacy], key=lambda row:(row['starts_at'],row.get('round_id') or row.get('interview_schedule_id')))[:limit]
