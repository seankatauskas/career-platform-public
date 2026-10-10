"""Canonical OLD METHOD screen; labels allocate attention, not model judgments."""
import re
from ..contracts import fingerprint
from ...collection.locations import normalize_location

VERSION = "old-method-screen-v1"
patterns=[
 ('FDE',r'forward[ -]deployed|\bdeployment (?:software )?engineer\b'),
 ('AI/ML',r'\b(?:ai|ml|machine learning|artificial intelligence|llm|deep learning|applied ai|generative ai|genai)\b.*\b(?:engineer|scientist|researcher|developer)\b|\b(?:engineer|scientist|researcher|developer)\b.*\b(?:ai|ml|machine learning|llm|deep learning|generative ai|genai)\b|\b(?:research engineer|applied scientist|mlops)\b'),
 ('Full stack',r'full[ -]?stack'),
 ('Frontend',r'front[ -]?end|\b(?:web developer|web engineer|ui engineer|ui developer|ux engineer|javascript developer|react developer)\b'),
 ('Backend',r'back[ -]?end|\b(?:api|java|python|node\.?js|kotlin|golang|ruby) (?:software )?(?:engineer|developer)\b'),
 ('Mobile',r'\b(?:ios|android|mobile|react native)\b.*\b(?:engineer|developer)\b|\b(?:engineer|developer)\b.*\b(?:ios|android|mobile|react native)\b'),
 ('Platform/infrastructure',r'\b(?:platform engineer|infrastructure engineer|devops|site reliability|developer (?:tools|experience) engineer|production engineer|cloud engineer)\b'),
 ('General software',r'\b(?:software|application|applications|product|founding)\b.*\b(?:engineer|developer)\b|\b(?:member of technical staff|programmer|swe|sde)\b'),
 ('Data engineering',r'\b(?:data|analytics|database) engineer'),
 ('Test/QA automation',r'\b(?:sdet|test automation|automation test|qa engineer|quality assurance engineer|software test)\b'),
 ('Solutions/integrations',r'\b(?:solutions? engineer|integrations? engineer|customer engineer|implementation engineer|solutions? architect)\b'),
 ('Other engineering leadership',r'\bengineering (?:manager|director)|\b(?:head|vp|vice president|director) of (?:software|engineering)|\b(?:software|systems?) architect\b')]
sw=re.compile(r'\b(?:software|typescript|javascript|python|react|java|sql|programming|api|backend|frontend|full.stack|kubernetes|cloud|distributed systems)\b',re.I)
# Explicit software roles do not need description-keyword corroboration.
# Ambiguous product, infrastructure, platform and solutions titles retain the gate.
unambiguous_software_title=re.compile(r'\b(?:software[ -]+(?:engineer|developer)|devops[ -]+engineer|site[ -]+reliability[ -]+engineer)\b',re.I)

def family(j):
 t=j['title'];d=j['description'] or ''
 if re.search(r'\b(?:product|program|project|technical program) manager|\bproduct owner|\brecruiter\b|\bdesigner\b',t,re.I):return 'Other occupations'
 for name,pat in patterns:
  if re.search(pat,t,re.I):
   if name in ('General software','Platform/infrastructure','Solutions/integrations') and not unambiguous_software_title.search(t) and len(sw.findall(d))<2:return 'Other occupations'
   return name
 return 'Other occupations'
def seniority(t):
 t=re.sub(r'(?i)member of technical staff','software engineer',t)
 if re.search(r'\b(?:intern|internship|co[ -]?op|apprentice|apprenticeship|working student)\b',t,re.I):return 'Student/intern/apprentice'
 if re.search(r'\b(?:staff|principal|architect|lead|manager|director|head|vp|vice president|distinguished|chief)\b',t,re.I):return 'Leadership/staff/principal'
 if re.search(r'\b(?:senior|sr\.?|iii|iv|sde.?3|swe.?3)\b',t,re.I):return 'Senior'
 return 'Early/mid or unspecified'

def screen(jobs):
    output = []
    for ordinal, job in enumerate(jobs, 1):
        row = dict(ordinal=ordinal, ats=job['ats'], job_id=str(job['id']),
                   snapshot_sha256=fingerprint(job), family=family(job),
                   seniority=seniority(job['title']))
        row['candidate'] = row['family'] != 'Other occupations'
        row['geo'] = normalize_location(job) if row['candidate'] else None
        output.append(row)
    return output
