"""Frozen, score-blind evidence packets for a separately isolated reviewer.

Export and import run in the trusted coordinator. A packet is data, not proof that
its reader has been sandboxed. The runtime must separately deny the reviewer access
to the application, databases, other tools, and the coordinator's conversation.
"""
from __future__ import annotations

import json
import re
from ..contracts import ContractError, canonical_json, payload_sha256
from .context import review_context
from .contracts import REVIEW_JOB_FIELDS, exact, identifier, integer, validate_assessment

PACKET_VERSION = 1
MAX_PACKET_JOBS = 20
MAX_PACKET_BYTES = 8 * 1024 * 1024
MAX_RESULT_BYTES = 1024 * 1024
_CONTEXT_META = ('rubric_version', 'profile_revision', 'resume_versions', 'fingerprint')
_CONTEXT_SECTIONS = ('facts', 'preferences', 'feedback')
_DIGEST = re.compile(r'[0-9a-f]{64}\Z')


def _copy_bounded(value, maximum, name):
    try:
        encoded = canonical_json(value)
        if len(encoded.encode('utf-8')) > maximum:
            raise ContractError(f'{name} exceeds its size limit; export fewer jobs')
        return json.loads(encoded)
    except (ValueError, TypeError, RecursionError, UnicodeError) as exc:
        if isinstance(exc, ContractError):
            raise
        raise ContractError(f'{name} must contain finite JSON data') from None


def _digest(value, name):
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ContractError(f'{name} must be a SHA-256 digest')
    return value


def _context(service, review_id):
    result = {}
    for section in _CONTEXT_SECTIONS:
        offset = 0
        values = []
        while True:
            page = service.call('context', {
                'review_id': review_id, 'section': section, 'offset': offset, 'limit': 20,
            })
            meta = {key: page[key] for key in _CONTEXT_META if key in page}
            if result and meta != {key: result[key] for key in _CONTEXT_META if key in result}:
                raise ContractError('frozen review context changed during export')
            result.update(meta)
            projected = review_context(page)
            chunk = projected.get(section, [])
            if not isinstance(chunk, list):
                raise ContractError('invalid review context page')
            values.extend(chunk)
            result[section] = values
            _copy_bounded(result, MAX_PACKET_BYTES, 'review context')
            next_offset = page.get('next_offset')
            if next_offset is None:
                if len(values) != page.get('total'):
                    raise ContractError('review context pagination is incomplete')
                break
            if next_offset != offset + len(chunk) or next_offset <= offset:
                raise ContractError('review context pagination did not advance')
            offset = next_offset
    if not isinstance(result.get('fingerprint'), str) or not result['fingerprint']:
        raise ContractError('frozen review context requires a fingerprint')
    return result


def _job(service, review_id, actor, ordinal):
    offset = 0
    job = None
    parts = []
    identity = None
    while True:
        page = service.call('job', {
            'review_id': review_id, 'actor': actor, 'ordinal': ordinal,
            'offset': offset, 'limit': 6000, 'blind': True,
        })
        current = (page.get('ordinal'), page.get('revision'), page.get('snapshot_sha256'))
        if current[0] != ordinal or (identity is not None and identity != current):
            raise ContractError('review job changed during packet export')
        identity = current
        projected = {key: page['job'][key] for key in REVIEW_JOB_FIELDS
                     if key != 'description' and key in page['job']}
        if job is not None and job != projected:
            raise ContractError('review job evidence changed during packet export')
        job = projected
        part = page.get('description')
        if not isinstance(part, str) or page.get('offset') != offset:
            raise ContractError('invalid review description page')
        parts.append(part)
        offset += len(part)
        if offset > MAX_PACKET_BYTES:
            raise ContractError('review description exceeds packet size limit')
        if page.get('next_offset') is None:
            if offset != page.get('description_chars'):
                raise ContractError('review description pagination is incomplete')
            break
        if not part or page['next_offset'] != offset:
            raise ContractError('review description pagination did not advance')
    job['description'] = ''.join(parts)
    return {'ordinal': ordinal, 'expected_revision': identity[1],
            'snapshot_sha256': identity[2], 'job': job}


def validate_packet(packet):
    """Validate a coordinator-owned packet; the digest is not authentication."""
    packet = _copy_bounded(packet, MAX_PACKET_BYTES, 'review packet')
    fields = ('version', 'review_id', 'actor', 'kind', 'context', 'jobs', 'packet_sha256')
    exact(packet, fields, fields)
    if type(packet['version']) is not int or packet['version'] != PACKET_VERSION:
        raise ContractError('unsupported review packet version')
    identifier(packet['review_id'], 'review_id')
    identifier(packet['actor'], 'actor')
    if packet['kind'] not in ('primary', 'check'):
        raise ContractError('kind must be primary or check')
    context = packet['context']
    exact(context, (*_CONTEXT_META, *_CONTEXT_SECTIONS), ('fingerprint', *_CONTEXT_SECTIONS))
    if not isinstance(context['fingerprint'], str) or not context['fingerprint']:
        raise ContractError('review context fingerprint is required')
    if any(not isinstance(context[section], list) for section in _CONTEXT_SECTIONS):
        raise ContractError('review context sections must be lists')
    if review_context(context) != context:
        raise ContractError('review context contains unsupported fields')
    jobs = packet['jobs']
    if not isinstance(jobs, list) or not 1 <= len(jobs) <= MAX_PACKET_JOBS:
        raise ContractError('review packet requires one to twenty jobs')
    seen = set()
    for entry in jobs:
        fields = ('ordinal', 'expected_revision', 'snapshot_sha256', 'job')
        exact(entry, fields, fields)
        ordinal = integer(entry['ordinal'], 'ordinal', 1)
        if ordinal in seen:
            raise ContractError('duplicate packet ordinal')
        seen.add(ordinal)
        integer(entry['expected_revision'], 'expected_revision')
        _digest(entry['snapshot_sha256'], 'snapshot_sha256')
        exact(entry['job'], REVIEW_JOB_FIELDS, ('ats', 'id', 'description'))
        if not isinstance(entry['job']['description'], str):
            raise ContractError('packet description must be text')
    _digest(packet['packet_sha256'], 'packet_sha256')
    if packet['packet_sha256'] != payload_sha256({k: v for k, v in packet.items() if k != 'packet_sha256'}):
        raise ContractError('review packet fingerprint does not match its content')
    return packet


def export_packet(service, *, review_id, actor, ordinals, kind='primary'):
    """Read every evidence page for an explicit batch without model diagnostics."""
    identifier(review_id, 'review_id')
    identifier(actor, 'actor')
    if kind not in ('primary', 'check'):
        raise ContractError('kind must be primary or check')
    if not isinstance(ordinals, (list, tuple)) or not 1 <= len(ordinals) <= MAX_PACKET_JOBS:
        raise ContractError('export requires one to twenty explicit ordinals')
    ordinals = [integer(value, 'ordinal', 1) for value in ordinals]
    if len(set(ordinals)) != len(ordinals):
        raise ContractError('duplicate packet ordinal')
    packet = {'version': PACKET_VERSION, 'review_id': review_id, 'actor': actor,
              'kind': kind, 'context': _context(service, review_id), 'jobs': []}
    for ordinal in sorted(ordinals):
        packet['jobs'].append(_job(service, review_id, actor, ordinal))
        _copy_bounded(packet, MAX_PACKET_BYTES, 'review packet')
    packet['packet_sha256'] = payload_sha256(packet)
    return validate_packet(packet)


def import_assessments(service, packet, result):
    """Import a complete response against the trusted original packet.

    All output shapes, membership, evidence and observed revisions are checked before
    writes. Each assessment retains the service's optimistic transaction guard. The
    batch is not atomic across concurrent writers; never present a failed import as
    completed. The caller must retain the original packet outside the reviewer.
    """
    packet = validate_packet(packet)
    result = _copy_bounded(result, MAX_RESULT_BYTES, 'review result')
    exact(result, ('packet_sha256', 'assessments'), ('packet_sha256', 'assessments'))
    if result['packet_sha256'] != packet['packet_sha256']:
        raise ContractError('review result belongs to another packet')
    rows = result['assessments']
    if not isinstance(rows, list) or len(rows) != len(packet['jobs']):
        raise ContractError('review result must assess every packet job exactly once')
    jobs = {entry['ordinal']: entry for entry in packet['jobs']}
    facts = {fact['fact_id'] for fact in packet['context']['facts']}
    assessments = {}
    for row in rows:
        exact(row, ('ordinal', 'assessment'), ('ordinal', 'assessment'))
        ordinal = integer(row['ordinal'], 'ordinal', 1)
        if ordinal not in jobs or ordinal in assessments:
            raise ContractError('foreign or duplicate result ordinal')
        assessments[ordinal] = validate_assessment(row['assessment'], jobs[ordinal]['job'], facts)
    if _context(service, packet['review_id']) != packet['context']:
        raise ContractError('review context changed since packet export')
    for ordinal, entry in jobs.items():
        current = service.call('job', {'review_id': packet['review_id'], 'actor': packet['actor'],
                                      'ordinal': ordinal, 'offset': 0, 'limit': 1, 'blind': True})
        if (current['revision'] != entry['expected_revision'] or
                current['snapshot_sha256'] != entry['snapshot_sha256']):
            raise ContractError('review job changed since packet export; export a fresh packet')
    receipts = []
    for ordinal, entry in jobs.items():
        receipts.append(service.call('assess', {
            'review_id': packet['review_id'], 'ordinal': ordinal, 'actor': packet['actor'],
            'kind': packet['kind'], 'expected_revision': entry['expected_revision'],
            'assessment': assessments[ordinal],
            'idempotency_key': f"isolated-{packet['packet_sha256']}-{ordinal}",
        }))
    return {'packet_sha256': packet['packet_sha256'], 'assessments': receipts}
