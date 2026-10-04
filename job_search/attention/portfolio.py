"""Whole-portfolio counts, slot-specific facts, and bounded grounded rendering."""
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from .policy import text_time


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
    presented=set()
    for saved in con.execute("SELECT b.selected_refs_json,b.snapshot_json FROM attention_briefings b JOIN notification_outbox n USING(notification_id) WHERE n.status='delivered'"):
        selected=set(json.loads(saved['selected_refs_json']))
        for fact in json.loads(saved['snapshot_json']).get('facts',[]):
            if fact['ref'] in selected: presented.add((fact['ref'],fact['source_revision']))
    applications={r['application_id']:dict(r) for r in con.execute('SELECT * FROM applications')}
    tasks=[dict(r) for r in con.execute("SELECT * FROM lifecycle_tasks WHERE status='open'")]
    rounds=[dict(r) for r in con.execute("SELECT * FROM interview_rounds WHERE status IN ('confirmed','rescheduled','proposed')")]
    counts={'applications':len(applications),'active_applications':sum(a['current_phase']!='terminal' for a in applications.values()),
        'open_tasks':len(tasks),'applicant_tasks':sum(t['owner']=='applicant' for t in tasks),
        'employer_tasks':sum(t['owner']=='employer' for t in tasks),'unknown_owner_tasks':sum(t['owner']=='unknown' for t in tasks),
        'overdue_tasks':sum(bool(t['due_at']) and t['due_at']<text_time(now) for t in tasks),
        'interviews':len(rounds),'events_last_week':con.execute('SELECT COUNT(*) FROM application_events WHERE occurred_at>=?',(text_time(now-timedelta(days=7)),)).fetchone()[0],
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
        if row.get('task_id'): represented_tasks.add(row['task_id'])
    for task in tasks:
        if applications.get(task['application_id'],{}).get('current_phase')=='terminal' or task['task_id'] in represented_tasks:
            continue
        due=task['due_at']
        if due and due>=text_time(forward_end): continue
        shown=('task:'+task['task_id'],str(task['revision_no'])) in presented
        if shown and not due and (task['owner']!='applicant' or slot=='evening'): continue
        snoozed=bool(task['snoozed_until'] and task['snoozed_until']>text_time(now))
        priority=(95 if due and due<=text_time(itinerary_end) else 70) if task['owner']=='applicant' else 20
        add('task:'+task['task_id'],task['kind'],task['application_id'],task['note'],due,task['revision_no'],task['owner'],task['evidence_id'],priority if not snoozed else 5,
            section='next_steps' if task['owner']=='applicant' else 'waiting')
    for table,key in (('event_proposals','proposal_id'),('temporal_proposals','temporal_proposal_id'),('lifecycle_correction_proposals','proposal_id'),('interview_revisions','revision_id'),('lifecycle_discoveries','discovery_id')):
        rows=con.execute(f"SELECT * FROM {table} WHERE status='pending'").fetchall();counts['pending_reviews']+=len(rows)
        for row in rows:
            value=dict(row)
            add('review:'+table+':'+value[key],'review',value.get('application_id') or value.get('proposed_application_id'),
                'Review '+str(value.get('kind') or value.get('event_type') or table.replace('_',' ')),value['created_at'],priority=55,section='decisions')
    for event in con.execute('SELECT * FROM application_events WHERE recorded_at>? ORDER BY event_seq',(min(recap_since,baseline),)):
        if variant!='week_recap' and ('event:'+event['event_id'],str(event['event_seq'])) in presented: continue
        if event['event_type'] not in ('application_started','manual_correction'):
            add('event:'+event['event_id'],'event',event['application_id'],event['event_type'].replace('_',' '),event['occurred_at'],event['event_seq'],priority=60,
                section='recap' if variant=='week_recap' else 'changes',observed_at=event['recorded_at'])
    # Ingestion is the delta watermark. An old message newly discovered now must
    # appear without pretending its source date is recent or that silence is news.
    for row in con.execute("SELECT o.*,l.application_id FROM lifecycle_mail_observations o JOIN lifecycle_mail_links l USING(observation_id) WHERE o.direction='inbound' AND o.updated_at>? ORDER BY o.updated_at,o.observation_id",(min(delta_since,baseline),)):
        ref='mail:'+row['observation_id']+':'+row['application_id']
        if (ref,str(row['modified_at'])) in presented: continue
        counts['new_messages']+=1
        add('mail:'+row['observation_id']+':'+row['application_id'],'mail',row['application_id'],row['subject'] or 'New linked message',row['source_at'],row['modified_at'],evidence_id=row['evidence_id'],priority=75,section='changes',observed_at=row['updated_at'])
    # Completion is an explicit ledger revision, never passage of a due date.
    for row in con.execute("SELECT r.*,t.application_id,t.note FROM lifecycle_task_revisions r JOIN lifecycle_tasks t USING(task_id) WHERE r.operation='complete' AND r.created_at>? ORDER BY r.created_at,r.revision_id",(min(recap_since,baseline),)):
        ref='completed:'+row['task_id']+':'+str(row['revision_no'])
        if variant!='week_recap' and (ref,str(row['revision_no'])) in presented: continue
        counts['completed_tasks']+=1
        add('completed:'+row['task_id']+':'+str(row['revision_no']),'completed',row['application_id'],'Completed: '+row['note'],row['created_at'],row['revision_no'],priority=65,section='recap' if variant=='week_recap' else 'completed')
    for row in con.execute("SELECT * FROM attention_candidates WHERE status IN ('active','snoozed') ORDER BY candidate_seq"):
        value=dict(row);payload=json.loads(value['payload_json'])
        matching=next((f for f in facts if f['ref']==value['source_kind']+':'+value['source_id'] and f['source_revision']==value['source_revision']),None)
        if matching:
            matching['candidate_id']=value['candidate_id'];matching['candidate_revision']=value['revision_no']
        elif value['source_kind'] not in ('task','interview','event') and value['observed_at']>delta_since and (not value['expires_at'] or value['expires_at']>text_time(now)):
            add('candidate:'+value['candidate_id'],'development',value['application_id'],payload['title'],value['due_at'] or value['source_at'],value['source_revision'],payload.get('owner'),priority=45,section='changes')
            facts[-1].update(candidate_id=value['candidate_id'],candidate_revision=value['revision_no'])
    coverage={'portfolio_complete':True,'mail_complete':False,'agenda':{'complete':False,'reason':'not_configured'}}
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
        'coverage':coverage,'ready_replies':[]}


def render(snapshot, ordered_refs=None, generation=None, dashboard_url=None):
    facts={f['ref']:f for f in snapshot['facts']}
    critical=[f['ref'] for f in snapshot['facts'] if f.get('priority',0)>=95 and f.get('at')]
    refs=list(dict.fromkeys(critical+[r for r in (list(facts) if ordered_refs is None else ordered_refs) if r in facts]))[:12]
    title={'week_ahead':'Your week ahead','week_recap':'Your week in review','morning':'Your morning briefing','evening':'Your evening briefing'}[snapshot['variant']]
    counts=snapshot['counts'];generation=generation or {}
    lines=[f"{counts['active_applications']} active applications; {counts['applicant_tasks']} next steps for you; {counts['pending_reviews']} reviews waiting."]
    footer=[]
    if not snapshot['coverage'].get('mail_complete') or not snapshot['coverage'].get('agenda',{}).get('complete'):
        footer.append('Coverage is incomplete; missing updates do not imply an employer decision.')
    # Reserve overflow text before selecting lines; its count is finalized below.
    overflow_footer=str(snapshot['fact_count'])+' additional facts are available in the dashboard.'
    footer.append(overflow_footer)
    if dashboard_url: footer.append('Open briefing: '+dashboard_url)
    reserved=sum(len(line)+1 for line in footer)+1
    budget=1950-reserved;used=[]
    def append(line,line_refs=()):
        if sum(len(item)+1 for item in lines)+len(line)+1>budget:
            return False
        lines.append(line);used.extend(line_refs)
        return True
    def append_fact(ref):
        fact=facts[ref];subject=' — '.join(x for x in (fact.get('employer'),fact.get('role')) if x)
        section={'itinerary':'Today' if snapshot['slot']=='morning' else 'Tomorrow','week_ahead':'This week','next_steps':'Next step','waiting':'Waiting','decisions':'Review','changes':'Change','completed':'Completed','recap':'This week'}.get(fact.get('section'),'Update')
        line=section+': '+(subject+': ' if subject else '')+fact['label']
        if fact.get('at'):
            try:
                local=datetime.fromisoformat(fact['at'].replace('Z','+00:00')).astimezone(ZoneInfo('America/Chicago'))
                line+=' ('+local.strftime('%a %b %d, %I:%M %p %Z')+')'
            except ValueError: pass
        append('• '+line,[ref])
    for ref in critical[:12]: append_fact(ref)
    for statement in generation.get('summary_statements',[]):
        selected=[facts[ref] for ref in statement['fact_refs'] if ref in facts]
        labels='; '.join(f['label'] for f in selected[:3])
        prefix={'needs_you':'Your attention is needed for ','waiting':'You are waiting on ','upcoming':'Coming up: ','review':'Ready to review: ','change':'Recorded changes: '}[statement['kind']]
        if labels: append(prefix+labels+'.',[f['ref'] for f in selected[:3]])
    for step in generation.get('suggested_next_steps',[]):
        if step['fact_ref'] in facts:
            append({'review':'Suggested next step: review ','reply':'Suggested next step: review a reply for ','prepare':'Suggested next step: prepare for ','wait':'Waiting for an update on '}[step['action']]+facts[step['fact_ref']]['label']+'.',[step['fact_ref']])
    for ref in refs:
        if ref not in critical: append_fact(ref)
    for reply in snapshot.get('ready_replies',[]):
        source=next((f for f in facts.values() if f.get('application_id')==reply.get('application_id') and f.get('evidence_id')==reply.get('evidence_id')),None)
        if not source: continue
        identity=' — '.join(x for x in (source.get('employer'),source.get('role')) if x) or 'this application'
        if reply.get('status')=='READY':
            append('• Reply ready for '+identity+'. Open the briefing to review the proposal.')
        elif reply.get('missing_information'):
            labels={'availability':'your available times','reply_body':'the reply you want to send','source_context':'the original message','source':'the original message','answer':'your answer','personal_answers':'your answer','user_availability':'your available times'}
            needs=[labels.get(str(item),'your input') for item in reply['missing_information'][:3]]
            append('• To prepare a reply for '+identity+', please provide '+', '.join(dict.fromkeys(needs))+'.')
    if not used: append('No recorded action needs attention in this snapshot.')
    remaining=max(0,snapshot['fact_count']-len(set(used)))
    footer.remove(overflow_footer)
    if remaining: footer.insert(0,str(remaining)+' additional facts are available in the dashboard.')
    lines.extend(footer)
    return title,'\n'.join(lines),list(dict.fromkeys(used))
