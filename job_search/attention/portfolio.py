"""Whole-portfolio counts, slot-specific facts, and bounded grounded rendering."""
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from .policy import text_time


_ROUTINE_EVENTS = frozenset(('submission_observed', 'submission_confirmed'))
_EVENT_LABELS = {
    'submission_observed': 'Application submitted',
    'submission_confirmed': 'Application confirmation',
    'recruiter_contact': 'Recruiter response',
    'assessment_requested': 'Assessment requested',
    'assessment_completed': 'Assessment completed',
    'interview_requested': 'Interview invitation',
    'interview_scheduled': 'Interview confirmed',
    'interview_completed': 'Interview completed',
    'offer_received': 'Offer received',
    'offer_accepted': 'Offer accepted',
    'rejection_received': 'Rejection received',
    'withdrawn': 'Application withdrawn',
}


def _mail_analyses(con):
    # Older schemas remain inspectable during release checks. Read through the
    # mail service's projection rather than reconstructing semantic decisions.
    if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='mail_understanding_analyses'").fetchone():
        return []
    from job_search.mail.understanding_store import briefing_analyses
    return briefing_analyses(con)


def _application_activity(con, since, through, excluded_events=()):
    """Count each application's first submission/receipt, using ingestion time.

    Routine activity belongs only to this delivered-briefing interval. It never
    becomes a backlog of individually unpresented facts. Keep source event IDs
    in the snapshot so the aggregate is inspectable without retaining mail text.
    """
    result = {'since': since, 'through': through, 'submitted': 0, 'confirmed': 0,
              'source_event_ids': []}
    rows = con.execute("""
        SELECT e.event_id,e.event_type FROM application_events e
        JOIN (SELECT MIN(event_seq) AS event_seq FROM application_events
              WHERE event_type IN ('submission_observed','submission_confirmed')
              GROUP BY application_id,event_type) first USING(event_seq)
        WHERE e.recorded_at>? AND e.recorded_at<=?
        ORDER BY e.event_seq
    """, (since, through))
    for row in rows:
        if row['event_id'] in excluded_events:
            continue
        key = 'submitted' if row['event_type'] == 'submission_observed' else 'confirmed'
        result[key] += 1
        result['source_event_ids'].append(row['event_id'])
    return result


def snapshot(con, now, slot, local_date, preferences, agenda=None):
    zone=ZoneInfo(preferences['timezone'])
    day=datetime.strptime(local_date,'%Y-%m-%d').replace(tzinfo=zone)
    variant='week_ahead' if slot=='morning' and day.weekday()==0 else ('week_recap' if slot=='evening' and day.weekday()==4 else slot)
    itinerary_start=day if slot=='morning' else day+timedelta(days=1)
    itinerary_end=itinerary_start+timedelta(days=1)
    forward_end=day+timedelta(days=7) if variant=='week_ahead' else itinerary_end
    week_start=day-timedelta(days=day.weekday())
    prior=con.execute("SELECT json_extract(b.snapshot_json,'$.as_of') as_of FROM attention_briefings b JOIN notification_outbox n USING(notification_id) WHERE n.status='delivered' AND b.scheduled_for<? ORDER BY b.scheduled_for DESC LIMIT 1",(text_time(now),)).fetchone()
    delta_since=prior['as_of'] if prior else text_time(now-timedelta(days=1))
    recap_since=text_time(week_start) if variant=='week_recap' else delta_since
    baseline=preferences.get('activated_at') or text_time(now-timedelta(days=7))
    analyses = _mail_analyses(con)
    live_analyses = [a for a in analyses if a['mode'] == 'shared' and not a.get('replay_id')]
    replay_analyses = [a for a in analyses if a.get('replay_id') or a['mode'] == 'replay']
    live_evidence = {a['evidence_id'] for a in live_analyses if a.get('evidence_id')}
    replay_evidence = {a['evidence_id'] for a in replay_analyses if a.get('evidence_id')}
    projected = {}
    for analysis in [*live_analyses, *replay_analyses]:
        for finding in analysis.get('findings', []):
            projection = finding.get('projection')
            if projection:
                projected[(projection['kind'], projection['id'])] = (analysis, finding)
    replay_tasks = {identity for (kind, identity), (a, _) in projected.items()
                    if kind == 'task' and (a.get('replay_id') or a['mode'] == 'replay')}
    replay_temporal = {identity for (kind, identity), (a, _) in projected.items()
                       if kind == 'temporal_proposal' and (a.get('replay_id') or a['mode'] == 'replay')}
    event_sources = {r['applied_event_id']: projected[('event_proposal', r['proposal_id'])]
                     for r in con.execute("SELECT proposal_id,applied_event_id FROM event_proposals WHERE applied_event_id IS NOT NULL")
                     if ('event_proposal', r['proposal_id']) in projected}
    replay_events = {event_id for event_id, (a, _) in event_sources.items() if a.get('replay_id') or a['mode'] == 'replay'}
    replay_schedules = set()
    schedule_sources = {}
    for schedule in con.execute('SELECT * FROM accepted_interview_schedules'):
        source = projected.get(('temporal_proposal', schedule['temporal_proposal_id']))
        if source:
            schedule_sources[schedule['interview_schedule_id']] = source
            event_sources[schedule['application_event_id']] = source
        if schedule['temporal_proposal_id'] in replay_temporal:
            replay_schedules.add(schedule['interview_schedule_id'])
            replay_events.add(schedule['application_event_id'])
    activity = _application_activity(con, max(recap_since, baseline), text_time(now), replay_events)
    presented=set()
    for saved in con.execute("SELECT b.selected_refs_json,b.snapshot_json FROM attention_briefings b JOIN notification_outbox n USING(notification_id) WHERE n.status='delivered'"):
        selected=set(json.loads(saved['selected_refs_json']))
        for fact in json.loads(saved['snapshot_json']).get('facts',[]):
            if fact['ref'] in selected: presented.add((fact['ref'],fact['source_revision']))
    applications={r['application_id']:dict(r) for r in con.execute('SELECT * FROM applications')}
    tasks=[dict(r) for r in con.execute("SELECT * FROM lifecycle_tasks WHERE status='open'") if r['task_id'] not in replay_tasks]
    rounds=[dict(r) for r in con.execute("SELECT * FROM interview_rounds WHERE status IN ('confirmed','rescheduled','proposed')") if r['legacy_schedule_id'] not in replay_schedules]
    counts={'applications':len(applications),'active_applications':sum(a['current_phase']!='terminal' for a in applications.values()),
        'open_tasks':len(tasks),'applicant_tasks':sum(t['owner']=='applicant' for t in tasks),
        'employer_tasks':sum(t['owner']=='employer' for t in tasks),'unknown_owner_tasks':sum(t['owner']=='unknown' for t in tasks),
        'overdue_tasks':sum(bool(t['due_at']) and t['due_at']<text_time(now) for t in tasks),
        'interviews':len(rounds),'events_last_week':sum(r['event_id'] not in replay_events for r in con.execute('SELECT event_id FROM application_events WHERE occurred_at>=?',(text_time(now-timedelta(days=7)),))),
        'pending_reviews':0,'new_messages':0,'completed_tasks':0}
    facts=[]
    def add(ref,kind,application_id,label,when=None,revision='',owner=None,evidence_id=None,priority=0,section='next_steps',**extra):
        app=applications.get(application_id,{})
        facts.append(dict(ref=ref,kind=kind,application_id=application_id,employer=app.get('employer_snapshot',''),role=app.get('title_snapshot',''),
            label=str(label)[:400],at=when,source_revision=str(revision),owner=owner,evidence_id=evidence_id,priority=priority,section=section,**extra))
    # Counts above describe the entire portfolio. Selection below is specific to
    # this slot: today's itinerary, tomorrow's preparation, or Monday's week ahead.
    represented_tasks=set()
    for row in rounds:
        if not row['starts_at'] or not text_time(itinerary_start)<=row['starts_at']<text_time(forward_end):
            continue
        add('interview:'+row['round_id'],'interview',row['application_id'],row['round_kind']+' ('+row['status']+')',row['starts_at'],row['current_revision_id'],priority=100,
            section='itinerary' if row['starts_at']<text_time(itinerary_end) else 'week_ahead',calendar_event_id=row['calendar_event_id'])
        if row['legacy_schedule_id'] in schedule_sources:
            analysis, finding = schedule_sources[row['legacy_schedule_id']]
            facts[-1].update(analysis_id=analysis['analysis_id'], finding_id=finding['finding_id'], evidence_id=analysis.get('evidence_id'))
        if row.get('task_id'): represented_tasks.add(row['task_id'])
    for task in tasks:
        if applications.get(task['application_id'],{}).get('current_phase')=='terminal' or task['task_id'] in represented_tasks:
            continue
        due=task['due_at']
        if due and due>=text_time(forward_end): continue
        shown=('task:'+task['task_id'],str(task['revision_no'])) in presented
        if shown and not due and (task['owner']!='applicant' or slot=='evening'): continue
        snoozed=bool(task['snoozed_until'] and task['snoozed_until']>text_time(now))
        priority=(95 if due and due<=text_time(itinerary_end) else 90) if task['owner']=='applicant' else 20
        add('task:'+task['task_id'],task['kind'],task['application_id'],task['note'],due,task['revision_no'],task['owner'],task['evidence_id'],priority if not snoozed else 5,
            section='next_steps' if task['owner']=='applicant' else 'waiting')
        if ('task', task['task_id']) in projected:
            analysis, finding = projected[('task', task['task_id'])]
            facts[-1].update(analysis_id=analysis['analysis_id'], finding_id=finding['finding_id'])
    represented_action_evidence = {f['evidence_id'] for f in facts if f.get('evidence_id') and f.get('owner') == 'applicant'}
    represented_analyses = {f['analysis_id'] for f in facts if f.get('analysis_id')}
    reviewed_messages = set()
    grouped_evidence = set()
    for analysis in live_analyses:
        if not analysis.get('current', True):
            continue
        pending = [f for f in analysis.get('findings', []) if f['status'] in ('pending', 'held')]
        if not pending:
            continue
        counts['pending_reviews'] += 1
        grouped_evidence.add(analysis.get('evidence_id'))
        # A single review can contain several independent findings. Its source
        # remains available in the dashboard; the briefing never endorses them.
        label = 'Review email: ' + (analysis.get('subject') or 'Application correspondence')
        add('review:mail_analysis:' + analysis['analysis_id'], 'review', analysis.get('application_id'), label,
            analysis['created_at'], analysis['revision'], priority=85, section='decisions', evidence_id=analysis.get('evidence_id'),
            analysis_id=analysis['analysis_id'], finding_ids=[f['finding_id'] for f in pending],
            review_path='#review/mail_analysis/' + analysis['analysis_id'])
    for table,key in (('event_proposals','proposal_id'),('temporal_proposals','temporal_proposal_id'),('lifecycle_correction_proposals','proposal_id'),('interview_revisions','revision_id'),('lifecycle_discoveries','discovery_id')):
        rows=con.execute(f"SELECT * FROM {table} WHERE status='pending'").fetchall()
        for row in rows:
            value=dict(row)
            projection_kind = {'event_proposals': 'event_proposal', 'temporal_proposals': 'temporal_proposal'}.get(table)
            if (projection_kind, value[key]) in projected:
                continue
            if value.get('evidence_id') in replay_evidence - live_evidence:
                continue
            counts['pending_reviews'] += 1
            application_id=value.get('application_id') or value.get('proposed_application_id')
            if value.get('evidence_id'):
                reviewed_messages.add((value['evidence_id'],application_id))
            label=str(value.get('kind') or value.get('event_type') or table.replace('_',' '))
            add('review:'+table+':'+value[key],'review',value.get('application_id') or value.get('proposed_application_id'),
                _EVENT_LABELS.get(label,label.replace('_',' ')),value['created_at'],priority=85,section='decisions',evidence_id=value.get('evidence_id'))
    classified_messages = {}
    event_evidence = {}
    for row in con.execute("SELECT p.evidence_id,e.* FROM event_proposals p JOIN application_events e ON e.event_id=p.applied_event_id WHERE p.status IN ('accepted','auto_applied') ORDER BY e.event_seq"):
        classified_messages[(row['evidence_id'],row['application_id'])] = dict(row)
        event_evidence[row['event_id']] = row['evidence_id']
    represented_events = set()
    for event in con.execute('SELECT * FROM application_events WHERE recorded_at>? ORDER BY event_seq',(min(recap_since,baseline),)):
        evidence_id = event_evidence.get(event['event_id'])
        analysis_source = event_sources.get(event['event_id'])
        if (event['event_id'] in replay_events or evidence_id in grouped_evidence
            or (analysis_source and (analysis_source[0]['analysis_id'] in represented_analyses or analysis_source[0].get('evidence_id') in grouped_evidence))
            or (evidence_id in live_evidence and evidence_id in represented_action_evidence)):
            continue
        if variant!='week_recap' and ('event:'+event['event_id'],str(event['event_seq'])) in presented: continue
        if event['event_type'] not in _ROUTINE_EVENTS | {'application_started','manual_correction'}:
            represented_events.add(event['event_id'])
            add('event:'+event['event_id'],'event',event['application_id'],_EVENT_LABELS.get(event['event_type'],event['event_type'].replace('_',' ')),event['occurred_at'],event['event_seq'],priority=80,
                section='recap' if variant=='week_recap' else 'changes',observed_at=event['recorded_at'],event_type=event['event_type'],evidence_id=event_evidence.get(event['event_id']))
            if analysis_source:
                analysis, finding = analysis_source
                facts[-1].update(analysis_id=analysis['analysis_id'], finding_id=finding['finding_id'], evidence_id=analysis.get('evidence_id'))
    # Ingestion is the delta watermark. An old message newly discovered now must
    # appear without pretending its source date is recent or that silence is news.
    for row in con.execute("SELECT o.*,l.application_id FROM lifecycle_mail_observations o JOIN lifecycle_mail_links l USING(observation_id) WHERE o.direction='inbound' AND o.updated_at>? ORDER BY o.updated_at,o.observation_id",(min(delta_since,baseline),)):
        if row['evidence_id'] in replay_evidence - live_evidence:
            continue
        ref='mail:'+row['observation_id']+':'+row['application_id']
        if (ref,str(row['modified_at'])) in presented: continue
        counts['new_messages']+=1
        if row['evidence_id'] in live_evidence:
            # The shared result is represented by its accepted projections or a
            # grouped review. No second interpretation comes from the subject.
            continue
        classification=classified_messages.get((row['evidence_id'],row['application_id']))
        if classification:
            # Accepted receipt mail is covered by the activity count. Other
            # accepted mail is represented by its meaningful lifecycle event.
            event_ref=('event:'+classification['event_id'],str(classification['event_seq']))
            if (classification['event_type'] in _ROUTINE_EVENTS
                or classification['event_id'] in represented_events or event_ref in presented):
                continue
        if (row['evidence_id'],row['application_id']) in reviewed_messages:
            continue
        add('mail:'+row['observation_id']+':'+row['application_id'],'mail',row['application_id'],row['subject'] or 'New linked message',row['source_at'],row['modified_at'],evidence_id=row['evidence_id'],priority=75,section='changes',observed_at=row['updated_at'])
    # Completion is an explicit ledger revision, never passage of a due date.
    for row in con.execute("SELECT r.*,t.application_id,t.note FROM lifecycle_task_revisions r JOIN lifecycle_tasks t USING(task_id) WHERE r.operation='complete' AND r.created_at>? ORDER BY r.created_at,r.revision_id",(min(recap_since,baseline),)):
        if row['task_id'] in replay_tasks:
            continue
        ref='completed:'+row['task_id']+':'+str(row['revision_no'])
        if variant!='week_recap' and (ref,str(row['revision_no'])) in presented: continue
        counts['completed_tasks']+=1
        add('completed:'+row['task_id']+':'+str(row['revision_no']),'completed',row['application_id'],'Completed: '+row['note'],row['created_at'],row['revision_no'],priority=65,section='recap' if variant=='week_recap' else 'completed')
    for row in con.execute("SELECT * FROM attention_candidates WHERE status IN ('active','snoozed') ORDER BY candidate_seq"):
        value=dict(row);payload=json.loads(value['payload_json'])
        if (value['source_kind'] == 'task' and value['source_id'] in replay_tasks
            or value['source_kind'] == 'event' and value['source_id'] in replay_events):
            continue
        matching=next((f for f in facts if f['ref']==value['source_kind']+':'+value['source_id'] and f['source_revision']==value['source_revision']),None)
        if matching:
            matching['candidate_id']=value['candidate_id'];matching['candidate_revision']=value['revision_no']
        elif value['source_kind'] not in ('task','interview','event') and value['observed_at']>delta_since and (not value['expires_at'] or value['expires_at']>text_time(now)):
            add('candidate:'+value['candidate_id'],'development',value['application_id'],payload['title'],value['due_at'] or value['source_at'],value['source_revision'],payload.get('owner'),priority=45,section='changes')
            facts[-1].update(candidate_id=value['candidate_id'],candidate_revision=value['revision_no'])
    coverage={'portfolio_complete':True,'mail_complete':False,'agenda':{'complete':False,'reason':'not_configured'}}
    current_analyses = [a for a in live_analyses if a.get('current', True)]
    coverage['mail_understanding'] = {'analyzed_messages': len(current_analyses),
                                      'incomplete_messages': sum(bool(a.get('coverage')) for a in current_analyses)}
    coverage['mail_processing']={r['processing_status']:r['n'] for r in con.execute('SELECT processing_status,COUNT(*) n FROM outlook_message_stage GROUP BY processing_status')}
    coverage['connectors']=[dict(r) for r in con.execute("SELECT status,last_success_at FROM connector_health WHERE connector_key LIKE 'outlook:%'")]
    if agenda is not None:
        coverage['agenda']=dict(agenda.get('coverage') or {})
        represented_rounds={f['ref'].split(':',1)[1] for f in facts if f['kind']=='interview'}
        represented_calendar={f.get('calendar_event_id') for f in facts if f.get('calendar_event_id')}
        for item in agenda.get('items',[]):
            starts=item.get('starts_at');ends=item.get('ends_at')
            if not starts or not ends or ends<=text_time(itinerary_start) or starts>=text_time(forward_end): continue
            if item.get('status') in ('cancelled','declined','draft'): continue
            if item.get('round_id') in represented_rounds or item['id'] in represented_calendar: continue
            add('agenda:'+str(item['id']),'calendar',None,item.get('title') or 'Calendar commitment',starts,item.get('source_ref',''),priority=100,
                section='itinerary' if starts<text_time(itinerary_end) else 'week_ahead')
    facts.sort(key=lambda f:(-f['priority'],f['at'] or '9999',f['ref']))
    total=len(facts);selected=facts[:80]
    return {'as_of':text_time(now),'slot':slot,'local_date':local_date,'variant':variant,'delta_since':delta_since,
        'itinerary_start':text_time(itinerary_start),'itinerary_end':text_time(itinerary_end),'recap_since':recap_since,
        'counts':counts,'facts':selected,'fact_count':total,'omitted_facts':max(0,total-len(selected)),
        'coverage':coverage,'ready_replies':[],'application_activity':activity,'timezone':preferences['timezone']}


def render(snapshot, ordered_refs=None, generation=None, dashboard_url=None):
    facts={f['ref']:f for f in snapshot['facts']}
    critical=[f['ref'] for f in snapshot['facts'] if f.get('priority',0)>=95 and f.get('at')]
    generation=generation or {}
    # AI can select/order facts, but cannot demote obligations below routine
    # messages or repeat a fact as a summary, suggestion and separate bullet.
    requested=list(facts) if not ordered_refs else list(ordered_refs)
    requested.extend(ref for statement in generation.get('summary_statements',[]) for ref in statement['fact_refs'])
    requested.extend(step['fact_ref'] for step in generation.get('suggested_next_steps',[]))
    attention=[f['ref'] for f in snapshot['facts'] if f.get('section') in ('next_steps','decisions') and f.get('priority',0)>=85]
    refs=list(dict.fromkeys(ref for ref in critical+attention+requested if ref in facts))
    refs.sort(key=lambda ref:-facts[ref].get('priority',0))
    refs=refs[:12]
    title={'week_ahead':'Your week ahead','week_recap':'Your week in review','morning':'Your morning briefing','evening':'Your evening briefing'}[snapshot['variant']]
    counts=snapshot['counts']
    active,tasks,reviews=counts['active_applications'],counts['applicant_tasks'],counts['pending_reviews']
    lines=[f"{active} active application{'s' if active!=1 else ''}; {tasks} next step{'s' if tasks!=1 else ''} for you; {reviews} review{'s' if reviews!=1 else ''} waiting."]
    footer=[]
    activity=snapshot.get('application_activity',{})
    submitted,confirmed=activity.get('submitted',0),activity.get('confirmed',0)
    if submitted or confirmed:
        label='Application activity this week' if snapshot['variant']=='week_recap' else 'Application activity'
        footer.append(f"{label}: {submitted} application{'s' if submitted!=1 else ''} submitted; {confirmed} confirmation{'s' if confirmed!=1 else ''} received.")
    if not snapshot['coverage'].get('mail_complete') or not snapshot['coverage'].get('agenda',{}).get('complete'):
        footer.append('Coverage is incomplete; missing updates do not imply an employer decision.')
    if dashboard_url: footer.append('Open briefing: '+dashboard_url)
    # Reserve the single activity line and the link even for a crowded briefing.
    reserved=sum(len(line)+1 for line in footer)+1
    budget=1950-reserved;used=[]
    def append(line,line_refs=()):
        if sum(len(item)+1 for item in lines)+len(line)+1>budget:
            return False
        lines.append(line);used.extend(line_refs)
        return True
    reply_results={(reply.get('application_id'),reply.get('evidence_id')):reply for reply in snapshot.get('ready_replies',[])}
    for ref in refs:
        fact=facts[ref]
        message=(fact.get('application_id'),fact.get('evidence_id'))
        subject=' — '.join(x for x in (fact.get('employer'),fact.get('role')) if x)
        section={'itinerary':'Today' if snapshot['slot']=='morning' else 'Tomorrow','week_ahead':'This week','next_steps':'Needs you','waiting':'Waiting','decisions':'Review','changes':'Employer update','completed':'Completed','recap':'This week'}.get(fact.get('section'),'Update')
        line=section+': '+(subject+': ' if subject else '')+fact['label']
        if fact.get('review_path') and dashboard_url:
            line+=' '+dashboard_url.split('#',1)[0].rstrip('/')+'/'+fact['review_path']
        timed = ref in critical or fact.get('section') in ('next_steps','waiting') or fact['kind'] in ('interview','calendar')
        if fact.get('at') and (timed or fact['kind'] in ('event','mail')):
            try:
                local=datetime.fromisoformat(fact['at'].replace('Z','+00:00')).astimezone(ZoneInfo(snapshot.get('timezone','America/Chicago')))
                line+=' ('+local.strftime('%a %b %d, %I:%M %p %Z' if timed else '%b %d, %Y')+')'
            except ValueError: pass
        reply=reply_results.get(message) if fact.get('owner')=='applicant' and fact['kind'] in ('reply','send_availability') else None
        if reply:
            if reply.get('status')=='READY':
                line+=' Reply ready for review in the dashboard.'
            elif reply.get('missing_information'):
                labels={'availability':'your available times','reply_body':'the reply you want to send','source_context':'the original message','source':'the original message','answer':'your answer','personal_answers':'your answer','user_availability':'your available times'}
                needs=[labels.get(str(item),'your input') for item in reply['missing_information'][:3]]
                line+=' To prepare a reply, please provide '+', '.join(dict.fromkeys(needs))+'.'
        append('• '+line,[ref])
    if not used and not counts['applicant_tasks'] and not counts['pending_reviews']:
        append('No recorded action needs attention in this snapshot.')
    lines.extend(footer)
    return title,'\n'.join(lines),list(dict.fromkeys(used))
