"""Deterministic complete-evidence bounds for trusted coordinator dispatch."""
from ..contracts import ContractError, canonical_json
from .contracts import MAX_RESPONSE_BYTES, REVIEW_JOB_FIELDS, REVIEW_SUMMARY_FIELDS
from .service import unpack

# UTF-8 bytes conservatively upper-bound input tokens without a tokenizer.
# Leave at least 92K of a 272K context for instructions, tools and output. The
# independent runtime limit remains one MiB; this packing limit is stricter.
MAX_PACKET_BYTES = 180_000
ASSIGNMENT_HEADER_BYTES = 1024
PACKET_HEADER_BYTES = 2048


def pack_assignment(authority, review_id, ordinals, *, purpose='detailed', kind='primary'):
    """Return a fitting stable prefix, or route one oversized screen to detail.

    Nothing is truncated or ranked. An oversized detailed singleton retains the
    existing paged evidence workflow. All source reads use the frozen ledger.
    """
    if purpose not in ('detailed', 'screening') or not ordinals:
        raise ContractError('packing requires an explicit nonempty assignment')
    packed, oversized, preload = [], None, True
    with authority._transaction() as con:
        run = authority._managed_run(con, review_id)
        context = unpack(run['context_json'])
        packet_bytes = len(canonical_json(context).encode('utf-8')) + PACKET_HEADER_BYTES
        assignment_bytes = ASSIGNMENT_HEADER_BYTES
        for ordinal in ordinals:
            item = authority.service._item(con, {'review_id': review_id, 'ordinal': ordinal})
            source = unpack(item['snapshot_json'])
            slot = {'ordinal': ordinal, 'expected_revision': item['revision'],
                    'snapshot_sha256': item['snapshot_sha256'], 'submitted': False,
                    'job': {key: source.get(key) for key in REVIEW_SUMMARY_FIELDS}}
            entry = {'ordinal': ordinal, 'expected_revision': item['revision'],
                     'snapshot_sha256': item['snapshot_sha256'],
                     'job': {key: source[key] for key in REVIEW_JOB_FIELDS if key in source}}
            if kind == 'adjudicator':
                from .adjudication import differences, resolution_basis
                primary, check = unpack(item['assessment_json']), unpack(item['check_json'])
                entry['disagreement'] = {'ordinal': ordinal, 'basis_sha256': resolution_basis(run, item),
                    'primary': primary, 'check': check, 'differing_dimensions': differences(primary, check)}
            slot_bytes = len(canonical_json(slot).encode('utf-8')) + 1
            entry_bytes = len(canonical_json(entry).encode('utf-8')) + 1
            if assignment_bytes + slot_bytes > MAX_RESPONSE_BYTES:
                if not packed:
                    raise ContractError('single job metadata exceeds bounded assignment response')
                break
            if packet_bytes + slot_bytes + entry_bytes > MAX_PACKET_BYTES:
                if not packed:
                    if purpose == 'screening':
                        oversized = ordinal
                    else:
                        packed.append(ordinal)
                        preload = False
                break
            packed.append(ordinal)
            assignment_bytes += slot_bytes
            packet_bytes += slot_bytes + entry_bytes
    if oversized is not None:
        authority.route_oversize(review_id, oversized)
    return {'ordinals': packed, 'preload_enabled': preload}
