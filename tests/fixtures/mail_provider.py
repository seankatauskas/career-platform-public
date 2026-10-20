"""Translate domain fixtures to the two provider responses used by mail tests."""
from copy import deepcopy
import json
from types import SimpleNamespace


class StagedMailProvider:
    max_input_tokens = 200000

    def __init__(self, output, mutate=None):
        self.output = output
        self.mutate = mutate
        self.calls = []

    def count_tokens_upper_bound(self, text):
        return len(text)

    def generate(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        payload = json.loads(messages[1]['content'])
        if kwargs['schema_name'] == 'mail_source_support_v1':
            first = next(b for s in payload['sources'] for b in s['blocks'] if b['text'])
            value = {'support': [{'topic': topic,
                'classification': 'substantive_personal' if topic == 'personal_communication' else 'actionable_commitment',
                'evidence': [{'block_id': first['block_id']}]} for topic in payload['topics']]}
        elif kwargs['schema_name'].startswith('mail_semantic_verification'):
            value = {'checks': [{'finding_id': f['finding_id'], 'verdict': 'supported'} for f in payload['findings']],
                     'coverage': 'complete'}
        elif kwargs['schema_name'] == 'mail_identity_v4':
            originals = [(item, self.output[item['category']][int(item['finding_id'].split(':')[1])]) for item in payload['findings']]
            targets = {original.get('target_id') for _, original in originals if original.get('target_id')}
            target = next(iter(targets)) if len(targets) == 1 else None
            value = {'target_id': target, 'resolution': 'matched' if target else 'conflicting' if targets else 'ambiguous',
                'evidence': self.evidence(originals[0][1]['evidence'], payload),
                'records': [{'finding_id': item['finding_id'], 'record_id': original.get('value', {}).get(original['kind'] + '_id')}
                    for item, original in originals if item['category'] == 'facts' and original['kind'] in {'interview', 'assessment', 'offer'} and isinstance(original.get('value'), dict)],
                'uncertainties': []}
        else:
            value = deepcopy(self.output)
            for group in ('associations', 'facts', 'requests', 'temporal_facts'):
                for finding in value[group]:
                    if group != 'temporal_facts':
                        finding['target_id'] = None
                    finding.setdefault('confidence', None)
                    finding['evidence'] = self.evidence(finding['evidence'], payload)
                    if group == 'facts':
                        if finding['kind'] == 'submission' and isinstance(finding['value'], str):
                            finding['value'] = {'status': 'confirmed'}
                        if isinstance(finding['value'], dict):
                            from job_search.applications.understanding.recipes import FACT_FIELDS
                            for field in FACT_FIELDS[finding['kind']]:
                                finding['value'].setdefault(field, None)
                            if isinstance(finding['value'].get('terms'), dict):
                                finding['value']['terms'] = json.dumps(finding['value']['terms'])
            if self.mutate:
                self.mutate(value, payload, kwargs['schema_name'])
        return SimpleNamespace(text=json.dumps(value), usage={})

    @staticmethod
    def evidence(spans, payload):
        result = []
        for span in spans:
            source = next(s for s in payload['sources'] if s['source_id'] == span['source_id'] and s['revision'] == span['revision'])
            offset = 0
            for block in source['blocks']:
                if offset <= span['start'] < offset + len(block['text']):
                    result.append({'block_id': block['block_id']})
                    break
                offset += len(block['text'])
        return result
