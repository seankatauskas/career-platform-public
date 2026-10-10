"""Explicit user-only lifecycle commands for the authenticated dashboard."""
from collections.abc import Mapping
from job_search.contracts import ContractError

FIELDS = {
    'follow-up/configure': ({'application_id','after_days'}, {'application_id','after_days'}),
    'tasks/create': ({'application_id','values'}, {'application_id','values'}),
    'tasks/transition': ({'task_id','operation','values'}, {'task_id','operation'}),
    'tasks/prepare-reply': ({'task_id','expected_revision'}, {'task_id','expected_revision'}),
    'details/record': ({'application_id','kind','values','detail_id'}, {'application_id','kind','values'}),
    'corrections/decide': ({'proposal_id','decision'}, {'proposal_id','decision'}),
    'interviews/import': ({'schedule_id'}, {'schedule_id'}),
    'interviews/propose': ({'application_id','details'}, {'application_id','details'}),
    'interviews/decide': ({'proposal_id','decision','reason'}, {'proposal_id','decision'}),
    'discoveries/decide': ({'discovery_id','decision','application_id','employer','title','job_url'}, {'discovery_id','decision'}),
    'mail/direction': ({'observation_id','direction','reason','expected_updated_at'}, {'observation_id','direction','reason','expected_updated_at'}),
    'mail/link': ({'observation_id','application_id','confidence','source'}, {'observation_id','application_id'}),
    'replay/transition': ({'replay_id','operation'}, {'replay_id','operation'}),
    'replay/start': ({'account_id','since_at','until_at'}, {'account_id','since_at','until_at'}),
    'reminders/cancel': ({'reminder_id'}, {'reminder_id'}),
}


def mutate(service, operation, body, context):
    if context.actor_kind != 'user':
        raise ContractError('lifecycle decisions require a user')
    if operation not in FIELDS or not isinstance(body, Mapping):
        raise ContractError('unknown lifecycle command')
    values = {k:v for k,v in body.items() if k != 'idempotency_key'}
    allowed, required = FIELDS[operation]
    if set(values)-allowed or required-set(values):
        raise ContractError('lifecycle command fields do not match its contract')
    if operation == 'follow-up/configure':
        return service.configure_follow_up(values['application_id'], values['after_days'], context)
    if operation == 'tasks/create':
        return service.create_task(values['application_id'], values['values'], context)
    if operation == 'tasks/transition':
        return service.transition_task(values['task_id'], values['operation'], values.get('values',{}), context)
    if operation == 'tasks/prepare-reply':
        return service.prepare_task_reply(values['task_id'], values['expected_revision'], context)
    if operation == 'details/record':
        return service.record_detail(values['application_id'], values['kind'], values['values'], context, detail_id=values.get('detail_id'))
    if operation == 'corrections/decide':
        return service.decide_correction(values['proposal_id'], values['decision'], context)
    if operation == 'interviews/import':
        return service.import_accepted_schedule(values['schedule_id'], context)
    if operation == 'interviews/propose':
        return service.propose_interview_revision(values['application_id'], values['details'], context)
    if operation == 'interviews/decide':
        return service.decide_interview_revision(values['proposal_id'], values['decision'], values.get('reason','Reviewed in dashboard'), context)
    if operation == 'discoveries/decide':
        return service.decide_discovery(values, context)
    if operation == 'mail/direction':
        return service.review_mail_direction(values, context)
    if operation == 'mail/link':
        return service.link_mail(values, context)
    if operation == 'replay/transition':
        return service.transition_mail_replay(values['replay_id'], values['operation'], context)
    if operation == 'replay/start':
        return service.start_mail_replay(values, context)
    if operation == 'reminders/cancel':
        return service.cancel_unified_reminder(values['reminder_id'], context)
    raise ContractError('unknown lifecycle command')
