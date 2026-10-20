"""Model citations address immutable text blocks; Python owns source offsets."""
import hashlib

from job_search.commands import DomainError


class EvidenceError(DomainError):
    def __init__(self, reason):
        super().__init__('evidence_mismatch', 'The citation does not identify exact saved evidence.')
        self.reason = reason


def source_blocks(context):
    result = []
    for source in context.sources:
        # IDs remain stable when other sources are added or reordered.
        prefix = hashlib.sha256((source.source_id + '\0' + source.revision + '\0' + source.sha256).encode()).hexdigest()[:20]
        blocks, offset = [], 0
        for line in source.text.splitlines(keepends=True):
            # Include every character, including whitespace, without normalization.
            for relative in range(0, len(line), 512):
                text = line[relative:relative + 512]
                blocks.append({'block_id': prefix + ':' + str(offset + relative), 'text': text})
            offset += len(line)
        if not blocks:
            blocks.append({'block_id': prefix + ':0', 'text': ''})
        result.append({'source_id': source.source_id, 'revision': source.revision,
                       'sha256': source.sha256, 'direction': source.direction,
                       'role': source.role, 'blocks': blocks})
    return result


def resolve_evidence(citations, context):
    index = {}
    for source in source_blocks(context):
        offset = 0
        for block in source['blocks']:
            index[block['block_id']] = (source, block['text'], offset)
            offset += len(block['text'])
    if not isinstance(citations, list) or not 1 <= len(citations) <= 5:
        raise EvidenceError('invalid_span')
    result = []
    for citation in citations:
        if not isinstance(citation, dict) or set(citation) not in ({'block_id'}, {'block_id', 'quote'}):
            raise EvidenceError('invalid_span')
        block_id = citation['block_id']
        if not isinstance(block_id, str) or block_id not in index:
            raise EvidenceError('unknown_block')
        source, text, offset = index[block_id]
        # New provider contracts select immutable blocks. Python supplies the
        # exact text, including repeated wording, Unicode and whitespace.
        # Legacy explicit quotes remain readable for archived transport tests.
        quote = citation.get('quote', text)
        if not isinstance(quote, str) or not 1 <= len(quote) <= 512:
            raise EvidenceError('invalid_span')
        start = text.find(quote)
        if start < 0:
            raise EvidenceError('quote_not_found')
        if text.find(quote, start + 1) >= 0:
            raise EvidenceError('quote_not_unique')
        result.append({'source_id': source['source_id'], 'revision': source['revision'],
                       'start': offset + start, 'end': offset + start + len(quote), 'quote': quote})
    return result
