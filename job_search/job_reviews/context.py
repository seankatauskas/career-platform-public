"""Read user-approved facts and active resume text without invoking inference."""
from collections import Counter

from ..contracts import ContractError, payload_sha256
from .brief import project_search_brief
from .contracts import RUBRIC_VERSION, integer


FACT_SOURCES = ('summary', 'education', 'experience', 'projects', 'skills', 'resume')


def _source_inventory(value):
    if not isinstance(value, dict) or not isinstance(value.get('facts_by_source'), dict):
        raise ContractError('invalid review source inventory')
    result = {key: integer(value.get(key), key) for key in
              ('career_fact_count', 'resume_fact_count', 'total_fact_count')}
    if type(value.get('pending_career_draft')) is not bool:
        raise ContractError('pending_career_draft must be boolean')
    result['pending_career_draft'] = value['pending_career_draft']
    result['facts_by_source'] = {key: integer(value['facts_by_source'][key], key)
                               for key in FACT_SOURCES if key in value['facts_by_source']}
    return result


def review_context(context):
    """Project approved evidence, never model diagnostics, into review context.

    Free-text facts, preferences and explicit feedback remain verbatim. This is a
    structured boundary, not a text filter for words like 'score' or 'ranking'.
    """
    result = {k: context[k] for k in ('rubric_version', 'profile_revision',
              'resume_versions', 'fingerprint', 'preferences') if k in context}
    if 'search_brief' in context:
        result['search_brief'] = project_search_brief(context['search_brief'])
    if 'source_inventory' in context:
        result['source_inventory'] = _source_inventory(context['source_inventory'])
    heading_fields = ('entry_id', 'institution', 'degree', 'location', 'dates',
                      'details', 'company', 'role', 'name', 'context', 'url', 'category')
    result['facts'] = []
    for fact in context.get('facts', []):
        approved = {k: fact[k] for k in ('fact_id', 'source', 'entry_id', 'text') if k in fact}
        if isinstance(approved.get('text'), dict):
            approved['text'] = {k: approved['text'][k] for k in heading_fields if k in approved['text']}
        result['facts'].append(approved)
    if 'feedback' in context:
        result['feedback'] = [{k: item[k] for k in ('sequence', 'note', 'created_at') if k in item}
                              for item in context['feedback']]
    return result


def profile_context(resume):
    if resume is None:
        raise ContractError('a current resume or approved career profile is required')
    career = resume.get_career_profile()
    standards = resume.service.list_active_standards()
    versions = [resume.service.store.get_standard_version(standard['active_version_id'])
                for standard in sorted(standards, key=lambda s: (s['manual_rank'], s['standard_id']))]
    return _evidence_context(career, versions)


def stored_profile_context(db_path):
    """Host-side review evidence without PDF ownership or mutation dependencies."""
    from ..resume_lab.evidence import read_review_evidence
    if db_path is None:
        raise ContractError('a current resume or approved career profile is required')
    return _evidence_context(*read_review_evidence(db_path))


def _evidence_context(career, resume_versions):
    profile = career.get('approved')
    facts = []
    if profile:
        content = profile['content']
        if content.get('summary', '').strip():
            facts.append({'fact_id': profile['revision_id'] + '.summary', 'source': 'summary',
                          'text': content['summary']})
        for section in ('education', 'experience', 'projects', 'skills'):
            for entry in content.get(section, []):
                if entry.get('retired'):
                    continue
                heading = {k: v for k, v in entry.items() if k not in ('bullets', 'items', 'retired')}
                facts.append({'fact_id': entry['entry_id'], 'source': section, 'text': heading})
                for fact in entry.get('bullets', entry.get('items', [])):
                    if not fact.get('retired'):
                        facts.append({'fact_id': fact['fact_id'], 'source': section,
                                      'entry_id': entry['entry_id'], 'text': fact['text']})
    versions = []
    for version in resume_versions:
        versions.append(version['version_id'])
        for i, line in enumerate(str(version.get('plain_text') or '').splitlines()):
            if line.strip():
                # PDF extraction may produce a single very long line. Keep each fact retrievable.
                cleaned = line.strip()
                for offset in range(0, len(cleaned), 2000):
                    suffix = f'.{offset}' if len(cleaned) > 2000 else ''
                    facts.append({'fact_id': f"{version['version_id']}.{i}{suffix}",
                                  'source': 'resume', 'text': cleaned[offset:offset + 2000]})
    if not facts:
        raise ContractError('no approved profile facts or active resume text are available')
    counts = Counter(fact['source'] for fact in facts)
    draft_revision = career.get('draft_revision_id') or (career.get('draft') or {}).get('revision_id')
    approved_revision = profile['revision_id'] if profile else None
    inventory = {'career_fact_count': len(facts) - counts['resume'],
                 'resume_fact_count': counts['resume'], 'total_fact_count': len(facts),
                 'facts_by_source': {key: counts[key] for key in FACT_SOURCES},
                 'pending_career_draft': bool(draft_revision and draft_revision != approved_revision)}
    return {'rubric_version': RUBRIC_VERSION, 'profile_revision': profile['revision_id'] if profile else None,
            'resume_versions': versions, 'facts': facts, 'fingerprint': payload_sha256(facts),
            'source_inventory': inventory}
