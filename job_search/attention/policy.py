"""Pure preferences, slot computation, and fact-grounded attention routing."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from ..contracts import ContractError, parse_utc

DEFAULTS = dict(enabled=True,shadow=True,mode='important_developments',timezone='America/Chicago',
    morning_time='07:00',evening_time='19:00',quiet_hours_enabled=False,
    quiet_start='22:00',quiet_end='07:00',overnight_enabled=True,
    final_nudge_enabled=True,risk_window_minutes=120,final_nudge_minutes=30,
    minimum_alert_gap_minutes=30,maximum_alerts_per_day=6,ai_enabled=True,
    ready_replies_enabled=True)


def text_time(value):
    return value.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')


def validate_preferences(values):
    if set(values) != set(DEFAULTS):
        raise ContractError('unknown attention preference')
    for key in ('enabled','shadow','quiet_hours_enabled','overnight_enabled','final_nudge_enabled','ai_enabled','ready_replies_enabled'):
        if type(values[key]) is not bool:
            raise ContractError(key+' must be boolean')
    if values['mode'] not in ('important_developments','risk_only','briefings_only'):
        raise ContractError('invalid attention mode')
    if values['timezone'] != 'America/Chicago':
        raise ContractError('attention timezone must be America/Chicago')
    for key in ('morning_time','evening_time','quiet_start','quiet_end'):
        value = values[key]
        if not isinstance(value,str) or len(value)!=5 or value[2]!=':' or not value[:2].isdigit() or not value[3:].isdigit() or not 0<=int(value[:2])<=23 or not 0<=int(value[3:])<=59:
            raise ContractError('invalid local clock time')
    if values['morning_time'] >= values['evening_time']:
        raise ContractError('morning must precede evening')
    for key,bounds in {'risk_window_minutes':(5,1440),'final_nudge_minutes':(1,120),'minimum_alert_gap_minutes':(0,1440),'maximum_alerts_per_day':(1,30)}.items():
        if type(values[key]) is not int or not bounds[0]<=values[key]<=bounds[1]:
            raise ContractError('invalid '+key)
    if values['final_nudge_minutes'] >= values['risk_window_minutes']:
        raise ContractError('final nudge must follow initial risk window')
    return dict(values)


def slot_time(slot, local_date, preferences):
    if slot not in ('morning','evening'):
        raise ContractError('invalid briefing slot')
    try:
        day = datetime.strptime(local_date,'%Y-%m-%d')
        hour,minute = map(int,preferences[slot+'_time'].split(':'))
    except (TypeError,ValueError) as exc:
        raise ContractError('invalid briefing date') from exc
    zone = ZoneInfo(preferences['timezone'])
    naive = day.replace(hour=hour,minute=minute)
    # Ambiguous times use the first occurrence. A spring-forward missing clock
    # advances to the next valid minute instead of dropping the day's briefing.
    for increment in range(181):
        current = naive+timedelta(minutes=increment)
        local = current.replace(tzinfo=zone,fold=0)
        if local.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)==current:
            return local.astimezone(timezone.utc)
    raise ContractError('could not resolve briefing local time')


def next_slot(now, preferences):
    local = now.astimezone(ZoneInfo(preferences['timezone']))
    values=[]
    for delta in (0,1):
        day=(local+timedelta(days=delta)).date().isoformat()
        for slot in ('morning','evening'):
            candidate=slot_time(slot,day,preferences)
            if candidate>now:
                values.append(candidate)
    return min(values)


def quiet(now, preferences):
    if not preferences['quiet_hours_enabled'] or preferences['overnight_enabled']:
        return False
    clock=now.astimezone(ZoneInfo(preferences['timezone'])).strftime('%H:%M')
    start,end=preferences['quiet_start'],preferences['quiet_end']
    return start<=clock<end if start<end else clock>=start or clock<end


def decide(candidate, preferences, now, relevant, *, active=True):
    """Return a delivery route and explanatory reason; never infer missing facts."""
    if not relevant:
        return 'suppress','source_no_longer_relevant'
    if not preferences['enabled'] or not active:
        return 'defer','notifications_paused'
    if candidate['status']=='acknowledged':
        return 'suppress','acknowledged'
    if candidate['status']=='resolved':
        return 'suppress','resolved'
    if candidate.get('snoozed_until') and parse_utc(candidate['snoozed_until'])>now:
        return 'defer','snoozed'
    if candidate.get('expires_at') and parse_utc(candidate['expires_at'])<=now:
        return 'suppress','expired'
    payload=candidate.get('payload',{})
    if payload.get('historical'):
        return 'briefing','historical_context'
    due=parse_utc(candidate['due_at']) if candidate.get('due_at') else None
    if payload.get('explicit_reminder') and due and due<=now:
        return 'urgent','explicit_reminder'
    if preferences['mode']=='briefings_only':
        return 'briefing','briefings_only'
    if quiet(now,preferences):
        return 'defer','quiet_hours'
    interview=candidate['source_kind'] in ('interview','interview_reminder') or payload.get('task_kind')=='attend_interview' or payload.get('event_type')=='interview_scheduled'
    preparation=timedelta(minutes=30 if interview else preferences['risk_window_minutes'])
    # Escalate when waiting for the next scheduled briefing would leave too little
    # time to act, even if the deadline is more than two hours away right now.
    if due and now<due and due-preparation<=next_slot(now,preferences) and payload.get('owner')=='applicant':
        return 'urgent','interview_risk' if interview else 'deadline_risk'
    important=candidate['topic'] in ('application.offer_received','application.interview_requested')
    age=now-parse_utc(candidate['source_at'])
    if preferences['mode']=='important_developments' and important and timedelta(0)<=age<=timedelta(days=2):
        return 'urgent','important_development'
    return 'briefing','routine_development'
