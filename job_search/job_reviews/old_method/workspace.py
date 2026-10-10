"""Worker-local inputs, checkpointing and validated output. No database access."""
from collections import Counter
import json
import os
from pathlib import Path

from ...contracts import ContractError, canonical_json, payload_sha256
from ..contracts import RUBRIC_VERSION, SELECTED, fingerprint, validate_assessment
from ..cards import visible_explanation
from . import WORKFLOW
from .screen import VERSION, screen


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as stream:
        stream.write(canonical_json(value) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(0o600)
    temporary.replace(path)


def validate_packet(packet):
    if (packet.get('workflow') != WORKFLOW or packet.get('screen_version') != VERSION
            or payload_sha256(packet['context']) != packet['context_sha256']
            or not packet['context'].get('facts')):
        raise ContractError('invalid old-method context or workflow')
    from ..contracts import timestamp
    start, end = timestamp(packet['window_start']), timestamp(packet['window_end'])
    keys = set()
    for row in packet['jobs']:
        key = (row['ats'], str(row['id']))
        if key in keys or row.get('closed_at') or not start < timestamp(row['posted_at']) <= end:
            raise ContractError('invalid frozen posting membership')
        keys.add(key)
    if packet['sources_sha256'] != payload_sha256([fingerprint(j) for j in packet['jobs']]):
        raise ContractError('frozen postings changed')


def validate_result(packet, result, *, require_sealed=True):
    validate_packet(packet)
    if (result.get('input_sha256') != payload_sha256(packet)
            or require_sealed and result.get('sealed') is not True):
        raise ContractError('result is incomplete or belongs to different inputs')
    facts = {f['fact_id'] for f in packet['context']['facts']}
    saved = result.get('assessments')
    if not isinstance(saved, dict):
        raise ContractError('missing saved assessments')
    for key, value in saved.items():
        if not key.isdigit() or str(int(key)) != key or not 1 <= int(key) <= len(packet['jobs']):
            raise ContractError('assessment outside frozen inputs')
        validate_assessment(value, packet['jobs'][int(key)-1], facts, RUBRIC_VERSION)
        if value['decision'] in SELECTED:
            if int(key) not in result.get('sources_delivered', []):
                raise ContractError('selected source was not delivered in full')
            visible_explanation(value)
    selected = {int(k) for k, v in saved.items() if v['decision'] in SELECTED}
    order = result.get('order', [])
    if require_sealed and (any(type(n) is not int for n in order)
            or len(order) != len(set(order)) or set(order) != selected):
        raise ContractError('order must contain every selected posting exactly once')
    return result


class Review:
    """Same helper for the isolated AWS worker and local Codex sessions."""
    def __init__(self, inputs='/evidence/input.json', output='/output'):
        self.packet = json.loads(Path(inputs).read_text())
        validate_packet(self.packet)
        self.jobs, self.context = self.packet['jobs'], self.packet['context']
        self.index = screen(self.jobs)
        self.directory = Path(output)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / 'progress.json'
        self.state = (json.loads(self.path.read_text()) if self.path.exists() else
                      {'input_sha256': payload_sha256(self.packet), 'sealed': False,
                       'assessments': {}, 'sources_delivered': [], 'searches': [],
                       'restored': [], 'order': []})
        validate_result(self.packet, self.state, require_sealed=False)

    def _save(self):
        write_json(self.path, self.state)

    def _numbers(self, ordinals):
        values = list(ordinals)
        if any(type(n) is not int or not 1 <= n <= len(self.jobs) for n in values):
            raise ContractError('posting outside this input')
        return values

    def sources(self, ordinals):
        values = self._numbers(ordinals)
        self.state['sources_delivered'] = sorted(set(self.state['sources_delivered']) | set(values))
        self._save()
        return [{'ordinal': n, **self.jobs[n-1]} for n in values]

    def search(self, pattern, *, rejected_only=False):
        import re
        expression = re.compile(pattern, re.I)
        matches = [i['ordinal'] for i, j in zip(self.index, self.jobs)
                   if (not rejected_only or not i['candidate']) and
                   expression.search(j['title'] + '\n' + (j.get('description') or ''))]
        self.state['searches'].append({'pattern': pattern, 'rejected_only': rejected_only, 'matches': matches})
        self._save()
        return [{'ordinal': n, **{k: self.jobs[n-1].get(k) for k in ('title', 'company', 'location')}} for n in matches]

    def restore(self, ordinals):
        self.state['restored'] = sorted(set(self.state['restored']) | set(self._numbers(ordinals)))
        self.state['sealed'] = False
        self._save()

    def save(self, entries):
        if not isinstance(entries, list):
            raise ContractError('save requires a list of ordinal/assessment entries')
        candidate = json.loads(canonical_json(self.state))
        for entry in entries:
            n = self._numbers([entry['ordinal']])[0]
            candidate['assessments'][str(n)] = entry['assessment']
        candidate['sealed'] = False
        validate_result(self.packet, candidate, require_sealed=False)
        self.state = candidate
        self._save()
        return self.progress()

    def remove(self, ordinals):
        for n in self._numbers(ordinals):
            self.state['assessments'].pop(str(n), None)
        self.state['sealed'] = False
        self._save()

    def progress(self):
        counts = Counter(v['decision'] for v in self.state['assessments'].values())
        return {'total': len(self.jobs), 'initial_candidates': sum(i['candidate'] for i in self.index),
                'screened_out': sum(not i['candidate'] for i in self.index),
                'assessed': len(self.state['assessments']), 'decisions': dict(counts),
                'without_saved_assessment': len(self.jobs)-len(self.state['assessments']),
                'restored': self.state['restored'], 'sealed': self.state['sealed']}

    def finish(self, ordinals):
        self.state.update(order=self._numbers(ordinals), sealed=True)
        try:
            validate_result(self.packet, self.state)
        except Exception:
            self.state['sealed'] = False
            raise
        self._save()
        write_json(self.directory / 'result.json', self.state)
        return self.progress()
