"""Review contracts, independent of model/provider and transport."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Mapping

from ..contracts import ContractError, canonical_json, payload_sha256, validate_identifier

LEGACY_RUBRIC_VERSION = 'job-review-v1'
RUBRIC_VERSION = 'job-review-v2'
RUBRIC_VERSIONS = (LEGACY_RUBRIC_VERSION, RUBRIC_VERSION)
V2_FIELDS = ('eligibility', 'eligibility_condition', 'next_step', 'category')
SELECTED = {'close', 'slight_stretch', 'bigger_stretch', 'broad_only'}
TARGETED = SELECTED - {'broad_only'}
DECISIONS = SELECTED | {'exclude', 'needs_info'}
MATERIAL_FIELDS = ('ats', 'id', 'title', 'company', 'location', 'department', 'team',
                   'employmentType', 'isRemote', 'workplaceType', 'jobUrl', 'posted_at', 'description')
# Keep this independent of dashboard/catalog presentation fields: adding model
# diagnostics there must never expose them to reviewers or change review order.
REVIEW_JOB_FIELDS = (*MATERIAL_FIELDS, 'publishedAt', 'source_updated_at',
                     'first_seen', 'last_seen', 'closed_at')
REVIEW_SUMMARY_FIELDS = tuple(f for f in REVIEW_JOB_FIELDS if f not in
                             ('description', 'department', 'team', 'isRemote', 'workplaceType', 'last_seen'))
MAX_RESPONSE_BYTES = 56 * 1024


def stamp(value=None):
    if value is None:
        value = datetime.now(timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def timestamp(value):
    if not isinstance(value, str):
        raise ContractError('timestamp must be an ISO timestamp with timezone')
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None:
            raise ValueError()
        return result.astimezone(timezone.utc)
    except ValueError:
        raise ContractError('timestamp must be an ISO timestamp with timezone') from None


def text(value, name, maximum=2000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or '\x00' in value:
        raise ContractError(f'{name} must contain 1 to {maximum} characters')
    return value.strip()


def integer(value, name, low=0, high=1000000):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ContractError(f'{name} must be an integer between {low} and {high}')
    return value


def exact(value, allowed, required=()):
    if not isinstance(value, Mapping) or set(value) - set(allowed) or set(required) - set(value):
        raise ContractError('missing or unknown review fields')


def identifier(value, name):
    if not isinstance(value, str):
        raise ContractError(f'{name} must be an identifier')
    validate_identifier(value, name)
    return value


def fingerprint(job):
    return payload_sha256({key: job.get(key) or '' for key in MATERIAL_FIELDS})


def bounded(value):
    # Unlike Hermes' legacy presentation sanitizer, never silently truncate evidence.
    if len(canonical_json(value).encode('utf-8')) > MAX_RESPONSE_BYTES:
        raise ContractError('review response exceeds limit; request a smaller page')
    return value


def validate_assessment(value, job, facts, rubric_version=LEGACY_RUBRIC_VERSION):
    required = ('stage', 'decision', 'family', 'alignment', 'reason_code', 'explanation',
                'evidence', 'strengths', 'gaps', 'unknowns', 'borderline')
    if rubric_version not in RUBRIC_VERSIONS:
        raise ContractError('unsupported review rubric version')
    if rubric_version == RUBRIC_VERSION:
        required += V2_FIELDS
    exact(value, (*required, 'priority', 'duplicate_of', 'model'), required)
    out = dict(value)
    for field in ('decision', 'stage', 'alignment'):
        if not isinstance(out[field], str):
            raise ContractError(f'{field} must be text')
    if out['decision'] not in DECISIONS or out['stage'] not in ('screening', 'detailed'):
        raise ContractError('invalid assessment stage or decision')
    if out['alignment'] not in ('core', 'adjacent', 'unrelated', 'unknown'):
        raise ContractError('invalid career alignment')
    for field in ('family', 'reason_code', 'explanation'):
        out[field] = text(out[field], field, 2000 if field == 'explanation' else 100)
    if type(out['borderline']) is not bool:
        raise ContractError('borderline must be boolean')
    for field in ('strengths', 'gaps', 'unknowns'):
        if not isinstance(out[field], list) or len(out[field]) > 10:
            raise ContractError(f'{field} must be a list of up to ten observations')
        out[field] = [text(s, field, 500) for s in out[field]]
    if not isinstance(out['evidence'], list) or not 1 <= len(out['evidence']) <= 12:
        raise ContractError('assessment needs one to twelve source evidence entries')
    matched_description_profile = False
    for entry in out['evidence']:
        exact(entry, ('field', 'quote', 'fact_id'), ('field', 'quote'))
        field = entry['field']
        quote = text(entry['quote'], 'quote', 1000)
        if field not in MATERIAL_FIELDS or quote not in str(job.get(field) or ''):
            raise ContractError('evidence quote must occur in the frozen job field')
        if entry.get('fact_id'):
            if entry['fact_id'] not in facts:
                raise ContractError('evidence references an unknown profile fact')
            if field == 'description':
                matched_description_profile = True
    if out['decision'] in SELECTED:
        if out['stage'] != 'detailed' or not job.get('description') or not matched_description_profile:
            raise ContractError('recommendations require detailed review and linked profile evidence')
        if not any(e['field'] == 'description' for e in out['evidence']):
            raise ContractError('recommendations require description evidence')
        out['priority'] = integer(out.get('priority'), 'priority', 1)
    elif 'priority' in out:
        raise ContractError('priority is only used for selected roles')
    if out['stage'] == 'screening' and out['decision'] not in ('exclude', 'needs_info'):
        raise ContractError('screening cannot select recommendations')
    if out['stage'] == 'screening' and out['decision'] == 'exclude' and out['reason_code'] not in ('non_technical', 'location', 'duplicate'):
        raise ContractError('qualification exclusions require detailed review')
    if 'duplicate_of' in out:
        integer(out['duplicate_of'], 'duplicate_of', 1)
        if out['decision'] != 'exclude' or out['reason_code'] != 'duplicate':
            raise ContractError('duplicate references require a duplicate exclusion')
    if out['reason_code'] == 'duplicate' and 'duplicate_of' not in out:
        raise ContractError('duplicate exclusions require the retained posting ordinal')
    if 'model' in out:
        out['model'] = text(out['model'], 'model', 100)
    if rubric_version == RUBRIC_VERSION:
        if out['eligibility'] not in ('no_known_barrier', 'unresolved', 'ineligible'):
            raise ContractError('invalid eligibility status')
        if not isinstance(out['eligibility_condition'], str):
            raise ContractError('eligibility_condition must be text')
        if out['eligibility_condition'] or out['eligibility'] != 'no_known_barrier':
            out['eligibility_condition'] = text(out['eligibility_condition'], 'eligibility_condition', 500)
        if out['next_step'] not in ('apply', 'clarify', 'explore'):
            raise ContractError('invalid recommendation next step')
        if out['category'] not in ('core', 'alternative'):
            raise ContractError('invalid recommendation category')
        if out['decision'] in TARGETED:
            if out['eligibility'] == 'ineligible':
                raise ContractError('targeted recommendations cannot have confirmed ineligibility')
        if out['eligibility'] == 'unresolved' and out['next_step'] == 'apply':
            raise ContractError('unresolved eligibility requires clarify or explore as the next step')
        if out['eligibility'] == 'ineligible' and out['next_step'] != 'explore':
            raise ContractError('confirmed ineligibility cannot suggest applying or clarification')
    return out
