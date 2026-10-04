"""Privacy-bounded lifecycle views that retain usable page continuations.

The generic Hermes node budget applies to each record, not an entire composed
workspace. Pages are trimmed before their continuation is calculated, so a size
limit never advances past an unseen record.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import json

MAX_BYTES = 64 * 1024
PAGE_BYTES = 48 * 1024
PAGE_ROWS = 10
RECORD_BYTES = 3000

_PRIORITY = (
    'observation_id', 'message_id', 'revision_no', 'revision_id', 'task_id',
    'detail_id', 'round_id', 'proposal_id', 'id', 'event_id', 'application_id',
    'status', 'direction', 'kind', 'owner', 'current_phase', 'terminal_outcome',
    'employer_snapshot', 'title_snapshot', 'subject', 'note', 'reason', 'source_at',
    'source_time', 'due_at', 'starts_at', 'ends_at', 'evidence_id', 'archive_id',
    'completed_evidence_id', 'action_id', 'confidence', 'source', 'created_at',
    'updated_at', 'state', 'payload', 'proposal', 'details',
)
_RANK = {key: index for index, key in enumerate(_PRIORITY)}


def _size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))


def _text(value, limit):
    raw = value.encode('utf-8')
    if len(raw) <= limit:
        return value, False
    suffix = '…[truncated]'
    return raw[:max(0, limit-len(suffix.encode('utf-8')))].decode('utf-8', 'ignore') + suffix, True


def _compact(value, key='', depth=0):
    if isinstance(value, str):
        # Identifiers must remain usable in a later tool call. Their contracts
        # already bound length; source references are removed by bounded_output.
        if key.endswith('_id') or key in {'id', 'next_cursor'}:
            return value, False
        return _text(value, 768 if key == 'excerpt' else 512 if key in {'note', 'reason'} else 256)
    if isinstance(value, Mapping):
        result, clipped = {}, False
        entries = sorted(value.items(), key=lambda item:_RANK.get(item[0], len(_RANK)))
        cap = 24 if depth < 2 else 8
        for child_key, child in entries[:cap]:
            if depth >= 4 and isinstance(child, (Mapping, list, tuple)):
                result[child_key], was_clipped = '[truncated]', True
            else:
                result[child_key], was_clipped = _compact(child, child_key, depth+1)
            clipped |= was_clipped
        clipped |= len(entries) > cap
        if clipped:
            result['content_truncated'] = True
        return result, clipped
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        result, clipped = [], len(value) > 5
        for child in value[:5]:
            item, was_clipped = _compact(child, key, depth+1)
            result.append(item)
            clipped |= was_clipped
        return result, clipped
    return value, False


def _record(value, *, max_bytes=RECORD_BYTES):
    # Import lazily: Hermes imports lifecycle tool declarations at module load.
    from ..hermes import bounded_output
    prioritized, clipped = _compact(value)
    compact = bounded_output(prioritized, string_limit=2048)
    clipped |= compact != prioritized
    if not isinstance(compact, Mapping):
        return compact
    compact = dict(compact)
    # Keep priority identity/status fields first; dropped detail is explicit and
    # the ID remains available to retrieve a narrower record or evidence view.
    while _size(compact) > max_bytes:
        candidates = [key for key in compact if key != 'content_truncated']
        if not candidates:
            break
        compact.pop(candidates[-1])
        clipped = True
    if clipped or compact != value:
        compact['content_truncated'] = True
    return compact


def _rows(value):
    return list(value) if isinstance(value, (list, tuple)) else []


def _page(name, source, args, *, row_limit=PAGE_ROWS, record_bytes=RECORD_BYTES):
    key = 'rounds' if name == 'list_interview_rounds' else 'items'
    originals = _rows(source.get(key))
    items, used = [], 0
    for row in originals[:row_limit]:
        item = _record(row, max_bytes=record_bytes)
        size = _size(item)
        if items and used + size > PAGE_BYTES:
            break
        items.append(item)
        used += size
    shortened = len(items) < len(originals)
    result = {'complete':bool(source.get('complete', source.get('next_offset') is None)) and not shortened}
    if name == 'list_application_conversation':
        result['next_cursor'] = originals[len(items)-1]['observation_id'] if shortened and items else source.get('next_cursor')
    elif name == 'get_application_record_history':
        result['next_revision'] = originals[len(items)-1]['revision_no'] if shortened and items else source.get('next_revision')
    else:
        offset = args.get('offset', 0)
        result['next_offset'] = offset + len(items) if shortened else source.get('next_offset')
    if source.get('coverage'):
        result['coverage'] = _record(source['coverage'])
    result['truncated'] = shortened or any(isinstance(item, Mapping) and item.get('content_truncated') for item in items)
    result[key] = items
    return result


def _archive_page(source):
    # Archive cursors encode how many ciphertext records were scanned, including
    # nonmatches. Never drop a match while retaining that opaque continuation.
    from ..hermes import bounded_output
    items = []
    for original in _rows(source.get('items')):
        fields = {key:original[key] for key in ('message_id', 'subject', 'excerpt', 'archive_truncated') if key in original}
        safe = bounded_output(fields, string_limit=2048)
        item, clipped = {}, False
        for key, value in safe.items():
            if key in {'subject', 'excerpt'}:
                item[key], was_clipped = _text(value, 256 if key == 'subject' else 768)
                clipped |= was_clipped
            else:
                item[key] = value
        if clipped:
            item['content_truncated'] = True
        items.append(item)
    result = {key:source[key] for key in ('next_cursor', 'complete', 'scanned', 'scan_limit') if key in source}
    result['coverage'] = _record(source.get('coverage', 'Sanitized encrypted archive only.'))
    result['items'] = items
    result['truncated'] = any(item.get('content_truncated', False) for item in items)
    return result


def _briefing(source):
    result = {
        'application':_record(source.get('application', {}), max_bytes=1800),
        'explanation':_text(str(source.get('explanation', '')), 1500)[0],
        'coverage':_record(source.get('coverage', {}), max_bytes=2400),
        'as_of':source.get('as_of'),
        'truncated':bool(source.get('truncated')),
    }
    for key, count in (
        ('next_obligations', 5), ('tasks', 5), ('reminders', 3), ('details', 3),
        ('pending_reviews', 3), ('actions', 3), ('evidence', 3), ('attention', 3),
        ('legacy_interviews', 3),
    ):
        rows = _rows(source.get(key))
        result[key] = [_record(row, max_bytes=1000) for row in rows[:count]]
        result['truncated'] |= len(rows)>count or any(isinstance(row, Mapping) and row.get('content_truncated') for row in result[key])
    conversation = _page('list_application_conversation', source.get('conversation', {}), {}, row_limit=3, record_bytes=1000)
    result['conversation'] = conversation
    result['interviews'] = _page('list_interview_rounds', source.get('interviews', {}), {}, row_limit=3, record_bytes=1000)
    result['follow_up'] = _record(source.get('follow_up', {}), max_bytes=800)
    result['last_messages_in_returned_page'] = {
        direction:next((item for item in conversation['items'] if item.get('direction') == direction), None)
        for direction in ('inbound', 'outbound', 'draft')
    }
    result['truncated'] |= conversation['truncated'] or result['interviews']['truncated']
    result['read_more'] = 'Use the dedicated conversation, task, detail, interview, reminder, review, and record-history tools for further pages or evidence.'
    return result


def bounded_lifecycle(name, result, args):
    """Bound lifecycle output without discarding control metadata or page rows."""
    if not isinstance(result, Mapping):
        return _record(result)
    if name == 'search_mail_history':
        output = _archive_page(result)
    elif name in {'list_application_conversation', 'get_application_record_history', 'list_interview_rounds'}:
        output = _page(name, result, args)
    elif name == 'get_application_briefing':
        output = _briefing(result)
    elif name in {'list_application_tasks', 'list_application_details', 'list_lifecycle_reviews', 'list_application_reminders'}:
        key = {'list_application_tasks':'tasks', 'list_application_details':'details', 'list_lifecycle_reviews':'items', 'list_application_reminders':'reminders'}[name]
        rows = _rows(result.get(key))
        items = [_record(row) for row in rows[:PAGE_ROWS]]
        more = len(items)<len(rows) or len(rows)>=args.get('limit', 25)
        output = {'complete':not more, 'next_offset':args.get('offset',0)+len(items) if more else None,
                  'offset':args.get('offset',0), 'returned':len(items), key:items,
                  'truncated':len(items)<len(rows) or any(isinstance(item, Mapping) and item.get('content_truncated') for item in items)}
    else:
        output = _record(result, max_bytes=8000)
    if _size(output) > MAX_BYTES:
        # Supported service contracts fit by construction. Fail explicitly if a
        # future shape violates them; never return an advanced unusable cursor.
        from ..contracts import ContractError
        raise ContractError('lifecycle output exceeds its bounded response contract')
    return output
