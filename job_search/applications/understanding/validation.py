"""Validate the provider-schema subset with bounded, content-free diagnostics."""
import math

from job_search.commands import DomainError
from .recipes import FACT_FIELDS


class OutputError(DomainError):
    def __init__(self, reason, path=()):
        super().__init__('invalid_output', 'The model response did not satisfy the analysis contract.')
        self.reason = reason
        self.path = tuple(path)


def check(value, schema, path=()):
    if 'anyOf' in schema:
        if isinstance(value, dict) and isinstance(value.get('kind'), str):
            selected = [c for c in schema['anyOf'] if c.get('properties', {}).get('kind', {}).get('enum') == [value['kind']]]
            if len(selected) == 1:
                return check(value, selected[0], path)
        errors = []
        for choice in schema['anyOf']:
            try:
                check(value, choice, path)
                return
            except OutputError as exc:
                errors.append(exc)
        # Nullable fields should explain the actual value's failure, rather
        # than hide it behind the null alternative's type error.
        substantive = [exc for exc in errors if exc.reason != 'wrong_type']
        if len(substantive) == 1:
            raise substantive[0]
        raise OutputError('invalid_variant', path)
    types = schema.get('type', [])
    types = [types] if isinstance(types, str) else types
    actual = ('null' if value is None else 'boolean' if type(value) is bool else
              'integer' if type(value) is int else 'number' if type(value) is float else
              'string' if isinstance(value, str) else 'array' if isinstance(value, list) else
              'object' if isinstance(value, dict) else 'unknown')
    if actual not in types and not (actual == 'integer' and 'number' in types):
        raise OutputError('wrong_type', path)
    if 'enum' in schema and value not in schema['enum']:
        raise OutputError('invalid_choice', path)
    if actual == 'object':
        properties = schema.get('properties', {})
        if set(value) - set(properties) and schema.get('additionalProperties') is False:
            # Never copy unknown model field names into stored diagnostics.
            raise OutputError('unknown_field', path)
        missing = set(schema.get('required', [])) - set(value)
        if missing:
            raise OutputError('missing_field', (*path, sorted(missing)[0]))
        for key, child in value.items():
            if key in properties:
                check(child, properties[key], (*path, key))
    if actual in {'array', 'string'}:
        minimum, maximum = ('minItems', 'maxItems') if actual == 'array' else ('minLength', 'maxLength')
        if len(value) < schema.get(minimum, 0) or len(value) > schema.get(maximum, float('inf')):
            raise OutputError('invalid_length', path)
        if actual == 'array':
            for index, child in enumerate(value):
                check(child, schema['items'], (*path, index))
    if actual in {'integer', 'number'}:
        if (type(value) is float and not math.isfinite(value)) or value < schema.get('minimum', -float('inf')) or value > schema.get('maximum', float('inf')):
            raise OutputError('invalid_number', path)


REASONS = frozenset({'invalid_span', 'invalid_identity', 'invalid_source_or_bounds', 'quote_not_unique',
    'quote_not_found', 'unknown_block', 'invalid_variant', 'wrong_type', 'invalid_choice', 'unknown_field',
    'missing_field', 'invalid_length', 'invalid_number', 'invalid_contract', 'invalid_json',
    'output_truncated', 'output_too_large', 'context_budget_exceeded', 'duplicate_binding',
    'missing_binding', 'record_target_mismatch', 'catalog_target', 'incomplete_retrieval', 'provider_failed',
    'unexpected_binding', 'incomplete_temporal', 'possibly_missing_findings', 'uncertain_semantics',
    'unsupported_finding_removed', 'multiple_authored_sources', 'conflicting_identity', 'existing_association_conflict'})


# Only schema vocabulary and bounded array indexes may enter an error path.
# Unknown field names, candidate IDs and model values never enter diagnostics.
PATH_FIELDS = frozenset({'kind', 'value', 'target_id', 'evidence', 'confidence', 'block_id', 'quote',
    'status', 'submission_id', 'occurred_at', 'outcome', 'requirement', 'responsible_party', 'channel',
    'wording', 'normalized', 'timezone', 'missing', 'finding_id', 'record_id', 'bindings', 'records', 'resolution',
    'support', 'topic', 'classification',
    'relevance', 'uncertainties', 'associations', 'facts', 'requests', 'temporal_facts',
    'assessment_id', 'interview_id', 'offer_id', 'terms', 'participants', 'employer_confirmed',
    'description', 'due_at', 'starts_at', 'ends_at', 'location', 'format', 'title', 'url',
    'contact_name', 'contact_email', 'name', 'email', 'reason', 'scheduled_at', 'received_at'}).union(
        *(fields for fields in FACT_FIELDS.values()))


def safe_path(path):
    return list(path[:12]) if all((type(p) is int and 0 <= p <= 100) or
        (isinstance(p, str) and p in PATH_FIELDS) for p in path[:12]) else []


def validate_diagnostic(value):
    if not isinstance(value, dict):
        raise OutputError('invalid_contract')
    # Older persisted diagnostics remain readable and valid.
    if set(value) == {'category', 'position', 'reason'}:
        issues = [dict(value, stage='extraction')]
    elif ((set(value) == {'version', 'issues', 'repairs'} and value['version'] == 2) or
          (set(value) == {'version', 'issues', 'repairs', 'corrections'} and value['version'] == 3)) and type(value['repairs']) is int and 0 <= value['repairs'] <= (3 if value['version'] == 3 else 2):
        issues = value['issues']
    else:
        raise OutputError('invalid_contract')
    if not isinstance(issues, list) or not 0 <= len(issues) <= 80:
        raise OutputError('invalid_contract')
    corrections = value.get('corrections', [])
    if not isinstance(corrections, list) or len(corrections) > 80:
        raise OutputError('invalid_contract')
    if any(not isinstance(c, dict) or c.get('reason') != 'unsupported_finding_removed' or c.get('stage') != 'verification' for c in corrections):
        raise OutputError('invalid_contract')
    for issue in issues + corrections:
        if (not isinstance(issue, dict) or set(issue) not in (
                {'stage', 'category', 'position', 'reason'}, {'stage', 'category', 'position', 'reason', 'path'})
                or issue['stage'] not in {'extraction', 'verification', 'identity'}
                or issue['category'] not in {'analysis', 'associations', 'facts', 'requests', 'temporal_facts'}
                or type(issue['position']) is not int or not 0 <= issue['position'] < 80
                or issue['reason'] not in REASONS):
            raise OutputError('invalid_contract')
        if 'path' in issue and (not isinstance(issue['path'], list) or len(issue['path']) > 12
                or safe_path(issue['path']) != issue['path']):
            raise OutputError('invalid_contract')
