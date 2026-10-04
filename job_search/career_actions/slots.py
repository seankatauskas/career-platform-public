"""Bounded recognition against already approved slots, never open-ended date parsing."""
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from ..contracts import ContractError,parse_utc

ZONE_NAMES={'UTC':'UTC','GMT':'UTC','CT':'America/Chicago','CST':'America/Chicago','CDT':'America/Chicago','ET':'America/New_York','EST':'America/New_York','EDT':'America/New_York','PT':'America/Los_Angeles','PST':'America/Los_Angeles','PDT':'America/Los_Angeles','MT':'America/Denver','MST':'America/Denver','MDT':'America/Denver'}

def zone(slot):
    name=slot.get('time_zone') or 'UTC'
    try:return ZoneInfo(ZONE_NAMES.get(name.upper(),name))
    except (ValueError,ZoneInfoNotFoundError) as exc:raise ContractError('offered slot requires a known IANA time zone') from exc


def label(slot):
    local=parse_utc(slot['starts_at']).astimezone(zone(slot));end=parse_utc(slot['ends_at']).astimezone(zone(slot))
    return local.strftime('%A, %B %d, %Y at %I:%M %p')+' to '+end.strftime('%I:%M %p')+' '+local.strftime('%Z')


def authored_text(text):
    if text.startswith('BEGIN UNTRUSTED EMAIL\nSUBJECT\n') and '\nBODY\n' in text:
        text=text.split('\nBODY\n',1)[1].rsplit('\nEND UNTRUSTED EMAIL',1)[0]
    # Forwarded content and reply quotes cannot confirm a new appointment.
    if re.search(r'(?im)^\s*(begin forwarded|[- ]*forwarded message|from:)',text):
        return re.split(r'(?im)^\s*(begin forwarded|[- ]*forwarded message|from:)',text,maxsplit=1)[0].strip()
    return re.split(r'(?im)^\s*>|^On .+wrote:|^[-_]{3,}',text,maxsplit=1)[0].strip()


def matched_slots(text, slots):
    text=authored_text(text)
    text=re.sub(r'(?im)^(?:location|join(?: meeting)?|meeting (?:link|url)):\s*[^\n]*','',text)
    lower=text.lower()
    if re.search(r'(?i)\band\s+(?:at\s+)?\d|\b(different|another)\s+(?:time|day|slot)\b',text):return []
    if re.search(r'(?i)\b(or|instead|rather|today|tomorrow|next)\b',text):return []
    if re.search(r'\b\d{1,2}/\d{1,2}\b|\b\d{1,2}(?:st|nd|rd|th)\b',text,re.I):return []
    named_days=set(re.findall(r'(?i)\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b',text))
    if len(named_days)>1:return []
    explicit_zones=re.findall(r'\b(?:UTC|GMT|[CEPMA][SD]?T|CET|CEST|BST|IST|[A-Za-z_]+/[A-Za-z_]+)\b',text)
    time_mentions=list(re.finditer(r'(?i)(?:\bat\s*|\b)(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?\b',text))
    # Ignore bare date/year numbers; time needs at, colon, or meridiem.
    times=[m for m in time_mentions if ':' in m.group() or re.search(r'(?i)\bat|[ap]\.?m',m.group())]
    day_mentioned=bool(re.search(r'(?i)\b(mon|tue|wed|thu|fri|sat|sun)(day|sday|nesday|rsday|urday)?\b|\b(january|february|march|april|may|june|july|august|september|october|november|december)\b|\d{4}-\d{2}-\d{2}',text))
    results=[]
    for slot in slots:
        if slot['starts_at'] in text:
            results.append(slot);continue
        local=parse_utc(slot['starts_at']).astimezone(zone(slot))
        if explicit_zones:
            allowed={local.strftime('%Z').upper(),str(zone(slot)).upper()}
            if str(zone(slot))=='America/Chicago':allowed.add('CT')
            if str(zone(slot))=='America/New_York':allowed.add('ET')
            if str(zone(slot))=='America/Los_Angeles':allowed.add('PT')
            if str(zone(slot))=='America/Denver':allowed.add('MT')
            if any(z.upper() not in allowed for z in explicit_zones):continue
        day=bool(re.search(r'\b'+local.strftime('%A').lower()+r'\b|\b'+local.strftime('%a').lower()+r'\b',lower))
        day=day or local.strftime('%Y-%m-%d') in text
        day=day or bool(re.search(r'\b'+local.strftime('%B').lower()+r'\s+0?'+str(local.day)+r'\b',lower))
        matching_time=False
        for m in times:
            hour,minute=int(m[1]),int(m[2] or 0);meridiem=(m[3] or '').lower().replace('.','')
            if meridiem:hour=hour%12+(12 if meridiem=='pm' else 0)
            if minute==local.minute and (hour==local.hour or (not meridiem and hour==local.hour%12)):
                matching_time=True
        if times and matching_time and (day or not day_mentioned):results.append(slot)
        elif not times and not day_mentioned and not explicit_zones and not re.search(r'\d',text) and len(slots)==1:results.append(slot)
    return results
