"""Read user-approved facts and active resume text without invoking inference."""
from ..contracts import ContractError, payload_sha256
from .contracts import RUBRIC_VERSION


def review_context(context):
    """Project approved evidence, never model diagnostics, into review context.

    Free-text facts, preferences and explicit feedback remain verbatim. This is a
    structured boundary, not a text filter for words like 'score' or 'ranking'.
    """
    result = {k: context[k] for k in ('rubric_version', 'profile_revision',
              'resume_versions', 'fingerprint', 'preferences') if k in context}
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
    profile = resume.get_career_profile().get('approved')
    facts = []
    if profile:
        content = profile['content']
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
    standards = resume.service.list_active_standards()
    versions = []
    for standard in sorted(standards, key=lambda s: (s['manual_rank'], s['standard_id'])):
        version = resume.service.store.get_standard_version(standard['active_version_id'])
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
    return {'rubric_version': RUBRIC_VERSION, 'profile_revision': profile['revision_id'] if profile else None,
            'resume_versions': versions, 'facts': facts, 'fingerprint': payload_sha256(facts)}
