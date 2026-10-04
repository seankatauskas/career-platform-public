"""Project externally confirmed career commitments using the existing round ledger."""
import json
from ..contracts import ConflictError,MutationContext,canonical_json
from ..db import connect


def project(service, commitment_id):
    lifecycle=service.ledger.lifecycle
    with connect(service.store.db_path) as con:
        saved=con.execute("SELECT c.*,p.application_id,p.status AS send_status,p.payload_json,m.source_at FROM career_commitments c JOIN career_send_proposals p USING(proposal_id) JOIN lifecycle_mail_observations m ON m.evidence_id=c.confirmation_evidence_id AND m.direction='inbound' WHERE commitment_id=?",(commitment_id,)).fetchone()
        if not saved or saved['send_status']!='observed_sent' or saved['status'] not in {'pending','created','linked_invite','cancelled','pending_cancel'}:return
        item=dict(saved)
        lifecycle._evidence(con,item['application_id'],item['confirmation_evidence_id'])
        current=con.execute('SELECT * FROM interview_rounds WHERE round_id=?',(item['round_id'],)).fetchone() if item['round_id'] else None
        if not current:
            prior=con.execute("SELECT DISTINCT r.round_id FROM career_commitments c JOIN interview_rounds r USING(round_id) WHERE c.proposal_id=? AND c.commitment_id<>?",(item['proposal_id'],commitment_id)).fetchall()
            if len(prior)>1:raise ConflictError('ambiguous prior confirmed round')
            if prior:current=con.execute('SELECT * FROM interview_rounds WHERE round_id=?',(prior[0]['round_id'],)).fetchone()
        if current and current['status'] in {'cancelled','completed'} and item['status'] not in {'cancelled','pending_cancel'}:
            raise ConflictError('closed interview requires explicit review')
        if not current:
            possible=con.execute("SELECT * FROM interview_rounds WHERE application_id=? AND starts_at=? AND ends_at=? AND status IN ('proposed','confirmed','rescheduled')",(item['application_id'],item['starts_at'],item['ends_at'])).fetchall()
            if len(possible)>1:raise ConflictError('ambiguous existing interview round')
            current=possible[0] if possible else None
        status='cancelled' if item['status'] in {'cancelled','pending_cancel'} else 'rescheduled' if current and current['current_revision_id'] and (current['starts_at']!=item['starts_at'] or current['ends_at']!=item['ends_at'] or current['status']=='rescheduled') else 'confirmed'
        if current:
            details=json.loads(current['details_json'])
            if current['status']==status and current['starts_at']==item['starts_at'] and current['ends_at']==item['ends_at'] and details.get('evidence_id')==item['confirmation_evidence_id'] and (item['status']!='linked_invite' or current['calendar_event_id']==item['remote_id']):
                if not item['round_id']:con.execute('UPDATE career_commitments SET round_id=? WHERE commitment_id=?',(current['round_id'],commitment_id))
                return
    slots=json.loads(item['payload_json'])['offered_slots']
    matching=next((s for s in slots if s['starts_at']==item['starts_at'] and s['ends_at']==item['ends_at']),None)
    if not matching:raise ConflictError('commitment no longer matches an observed sent offer')
    conflicts=service._calendar_conflicts(item,exclude={item['remote_id']}) if service.outlook and status!='cancelled' else []
    details={'status':status,'starts_at':item['starts_at'],'ends_at':item['ends_at'],'time_zone':matching.get('time_zone','UTC'),
        'evidence_id':item['confirmation_evidence_id'],'organizer':item['organizer'],'employer_confirmed':True,
        'source_at':item['source_at'],'availability_conflicts':conflicts,
        'location':json.loads(item['details_json']).get('location',''),'join_url':json.loads(item['details_json']).get('join_url',''),
        'note':'Recruiter confirmation of an approved reply observed in Sent mail.'}
    if current:details['round_id']=current['round_id']
    if item['status']=='linked_invite':
        details.update(calendar_account_id=service.account_id,calendar_event_id=item['remote_id'],calendar_change_key=item['etag'],calendar_modified_at=json.loads(item['details_json']).get('calendar_modified_at',''))
    key='career-round-'+commitment_id+'-'+str(item['version'])+'-'+status
    ctx=MutationContext(key,'system','career_confirmed_mail',item['confirmation_evidence_id'])
    proposal=lifecycle.propose_interview_revision(item['application_id'],details,ctx)
    revision=proposal['revision']['revision_id']
    def apply(con,stamp):
        latest=con.execute('SELECT * FROM career_commitments WHERE commitment_id=?',(commitment_id,)).fetchone()
        if latest['version']!=item['version']:raise ConflictError('commitment changed before interview projection')
        incoming=con.execute('SELECT * FROM interview_revisions WHERE revision_id=?',(revision,)).fetchone()
        round_row=con.execute('SELECT * FROM interview_rounds WHERE round_id=?',(incoming['round_id'],)).fetchone()
        stale=incoming['base_revision_id']!=round_row['current_revision_id'] or incoming['base_round_status']!=round_row['status']
        result=lifecycle._decide_interview_revision(con,revision,'accepted','Verified recruiter confirmation of observed Sent offer',ctx,service._now(),
            {'status':'checked' if service.outlook else 'not_checked','conflicts':conflicts},version_conflict=stale,record_confirmed_conflicts=True)
        if result.get('applied'):
            con.execute('UPDATE career_commitments SET round_id=? WHERE commitment_id=?',(incoming['round_id'],commitment_id))
        return result
    return service.store._idempotent('career.project_interview',ctx,{'revision_id':revision},apply)
