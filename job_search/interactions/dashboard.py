"""Chief-of-staff dashboard commands, behind existing session/Origin/CSRF checks."""
from ..contracts import ContractError, MutationContext


def preview(attention,slot='morning',local_date=None):
    if slot in ('week_ahead','week_recap'):
        from datetime import datetime,timedelta
        from zoneinfo import ZoneInfo
        today=datetime.now(ZoneInfo(attention.preferences()['timezone'])).date()
        weekday=0 if slot=='week_ahead' else 4
        local_date=local_date or (today+timedelta(days=(weekday-today.weekday())%7)).isoformat()
        slot='morning' if slot=='week_ahead' else 'evening'
    return attention.preview(slot,local_date)


def read(ledger,path,query):
    if any(len(v)!=1 for v in query.values()):
        raise ContractError('repeated query parameter')
    def integer(name,default,maximum):
        value = query.get(name,[str(default)])[0]
        if not value.isdigit() or not 0 <= int(value) <= maximum:
            raise ContractError('invalid '+name)
        return int(value)
    if path == 'preferences':
        return ledger.attention.preferences()
    if path == 'delivery-recovery':
        return ledger.interactions.list_delivery_recovery(limit=max(1,integer('limit',25,100)),offset=integer('offset',0,100000))
    if path == 'preview':
        if set(query)-{'slot','local_date'}:
            raise ContractError('invalid preview query')
        return preview(ledger.attention,query.get('slot',['morning'])[0],query.get('local_date',[None])[0])
    if path == 'history':
        return ledger.attention.history(limit=max(1,integer('limit',20,100)),offset=integer('offset',0,100000))
    if path.startswith('briefing/'):
        return ledger.attention.get_briefing(path.split('/',1)[1])
    if path == 'candidates':
        return ledger.attention.list_candidates(limit=max(1,integer('limit',50,100)),offset=integer('offset',0,100000))
    if path == 'actions':
        return ledger.career_actions.list_proposals(status=query.get('status',['pending'])[0],limit=max(1,integer('limit',50,100)))
    if path.startswith('actions/'):
        return ledger.career_actions.get_proposal(path.split('/',1)[1])
    if path == 'commitments':
        result=ledger.career_actions.list_commitments(limit=max(1,integer('limit',50,100)))
        rows=[]
        for value in result.get('commitments',[]):
            row=dict(value)
            row['offered_slots']=ledger.career_actions.get_proposal(row['proposal_id']).get('offered_slots',[])
            rows.append(row)
        return {'commitments':rows}
    raise ContractError('unknown chief-of-staff view')


FIELDS = {
    'preferences':({'changes','expected_revision'},{'changes','expected_revision'}),
    'acknowledge':({'candidate_id','expected_revision'},{'candidate_id','expected_revision'}),
    'snooze':({'candidate_id','until','expected_revision'},{'candidate_id','until','expected_revision'}),
    'actions/decide':({'proposal_id','decision','payload_hash','source_hash'},{'proposal_id','decision','payload_hash','source_hash'}),
    'actions/review':({'proposal_id'},{'proposal_id'}),
    'actions/edit':({'proposal_id','payload_hash','source_hash','body'},{'proposal_id','payload_hash','source_hash','body'}),
    'commitments/review':({'commitment_id','decision','expected_version','starts_at','ends_at'},{'commitment_id','decision','expected_version'}),
    'delivery-recovery':({'ticket_id','decision','expected_revision','payload_sha256','source_version','identity'},{'ticket_id','decision','expected_revision','payload_sha256','source_version','identity'}),
}


def mutate(ledger,path,body,context):
    if context.actor_kind != 'user' or path not in FIELDS:
        raise ContractError('trusted user command required')
    values = {k:v for k,v in body.items() if k!='idempotency_key'}
    allowed,required = FIELDS[path]
    if set(values)-allowed or required-set(values):
        raise ContractError('command does not match its contract')
    if path == 'preferences':
        return ledger.attention.update_preferences(values['changes'],values['expected_revision'],context)
    if path == 'delivery-recovery':
        return ledger.interactions.reconcile_delivery(values,context)
    if path == 'acknowledge':
        return ledger.attention.acknowledge(values['candidate_id'],context,expected_revision=values['expected_revision'])
    if path == 'snooze':
        return ledger.attention.snooze(values['candidate_id'],values['until'],context,expected_revision=values['expected_revision'])
    if path == 'actions/decide':
        return ledger.career_actions.decide_proposal(values['proposal_id'],values['decision'],values['payload_hash'],context,expected_source_hash=values['source_hash'])
    if path == 'commitments/review':
        return ledger.career_actions.review_commitment(values['commitment_id'],values['decision'],context,expected_version=values['expected_version'],starts_at=values.get('starts_at'),ends_at=values.get('ends_at'))
    if path == 'actions/review':
        return ledger.interactions.review_action(values['proposal_id'],context)
    if path == 'actions/edit':
        original = ledger.career_actions.get_proposal(values['proposal_id'])
        # Cancel first so an old Telegram ticket cannot race the edited draft.
        # Retrying a partial edit is safe through the deterministic command keys.
        cancel = MutationContext(context.idempotency_key+':cancel','user',context.source_kind,context.source_ref)
        ledger.career_actions.decide_proposal(values['proposal_id'],'rejected',values['payload_hash'],cancel,expected_source_hash=values['source_hash'])
        create = MutationContext(context.idempotency_key+':replacement','user',context.source_kind,context.source_ref)
        return ledger.career_actions.propose_reply(original['application_id'],original['evidence_id'],values['body'],create,offered_slots=original.get('offered_slots',[]))
    raise ContractError('unknown chief-of-staff command')
