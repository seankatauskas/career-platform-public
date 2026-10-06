"""Version-bound evaluation gates for shared mail; no production accuracy by fiat."""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
from typing import Any, Mapping

from ..contracts import ContractError, MODEL_AUTO_APPLY_EVENT_TYPES, payload_sha256, validate_identifier

SCHEMA_VERSION = 'mail_understanding_v1'
PROMPT_VERSION = 'mail-understanding-prompt-v1'
VALIDATOR_VERSION = 'mail-understanding-validator-v1'
POLICY_VERSION = 'mail-understanding-policy-v1'
AUTO_ACTIONS = frozenset({'reply', 'send_availability', 'complete_assessment', 'offer_decision'})


def provenance(producer_version: str) -> dict:
    return dict(schema_version=SCHEMA_VERSION, producer_version=producer_version,
                prompt_version=PROMPT_VERSION, validator_version=VALIDATOR_VERSION,
                policy_version=POLICY_VERSION)


@dataclass(frozen=True)
class UnderstandingEvaluationReport:
    data: Mapping[str, Any]

    @property
    def policy_id(self):
        return 'mail-understanding:' + payload_sha256(self.data)

    def allows(self, finding_type, finding, request_or_provenance):
        expected = provenance(request_or_provenance.get('producer_version', ''))
        if self.data['provenance'] != expected:
            return False
        if request_or_provenance.get('schema_version', SCHEMA_VERSION) != SCHEMA_VERSION:
            return False
        if request_or_provenance.get('coverage') or request_or_provenance.get('candidate_context_complete') is False:
            return False
        if not finding.get('application_id'):
            return False
        if finding_type == 'event':
            kind = finding.get('event_type')
            if kind not in {v.value for v in MODEL_AUTO_APPLY_EVENT_TYPES}:
                return False
        elif finding_type == 'action':
            kind = finding.get('kind')
            if kind not in AUTO_ACTIONS or finding.get('actor') != 'applicant' or finding.get('obligation') != 'required':
                return False
        else:
            return False
        metric = self.data['classes'].get(finding_type + ':' + kind)
        if metric is None:
            return False
        score = finding.get('confidence')
        return (type(score) in (int, float) and math.isfinite(score)
                and score >= self.data['threshold']
                and metric['high_confidence_predictions'] >= 50
                and metric['high_confidence_cases'] >= 50
                and metric['correct_predictions'] / max(1, metric['high_confidence_predictions']) >= .99
                and metric['wrong_application_matches'] == 0)

    def event_policy(self, event_type):
        metric = self.data['classes'].get('event:' + event_type, {})
        count = metric.get('high_confidence_predictions', 0)
        return dict(policy_id=self.policy_id + ':' + event_type,
                    producer_version=self.data['provenance']['producer_version'],
                    event_type=event_type, threshold=self.data['threshold'],
                    example_count=metric.get('high_confidence_cases', 0), observed_precision=metric.get('correct_predictions', 0) / max(1, count),
                    wrong_application_matches=metric.get('wrong_application_matches', 0),
                    evaluation_sha256=self.data['dataset_fingerprint'], enabled=True)


def validate_report(data):
    fields = {'version', 'provenance', 'dataset_fingerprint', 'dataset_kind', 'threshold', 'classes'}
    if not isinstance(data, dict) or set(data) != fields or type(data['version']) is not int or data['version'] != 1:
        raise ContractError('invalid shared mail evaluation report')
    p = data['provenance']
    if not isinstance(p, dict) or set(p) != set(provenance('')) or any(not isinstance(v, str) or not v for v in p.values()):
        raise ContractError('invalid evaluation provenance')
    fingerprint = data['dataset_fingerprint']
    if not isinstance(fingerprint, str) or len(fingerprint) != 64 or any(c not in '0123456789abcdef' for c in fingerprint):
        raise ContractError('invalid evaluation dataset fingerprint')
    if data['dataset_kind'] != 'reviewed_private_holdout':
        raise ContractError('automatic mail policy requires a reviewed private holdout')
    t = data['threshold']
    if type(t) not in (int, float) or not math.isfinite(t) or not .9 <= t <= 1:
        raise ContractError('invalid evaluation threshold')
    if not isinstance(data['classes'], dict) or len(data['classes']) > 32:
        raise ContractError('invalid evaluation classes')
    names = {'event:' + e.value for e in MODEL_AUTO_APPLY_EVENT_TYPES} | {'action:' + k for k in AUTO_ACTIONS}
    for name, metric in data['classes'].items():
        if name not in names or not isinstance(metric, dict) or set(metric) != {'high_confidence_predictions','high_confidence_cases','correct_predictions','wrong_application_matches','expected_findings','missed_findings'}:
            raise ContractError('invalid evaluation class metric')
        if any(type(n) is not int or n < 0 for n in metric.values()):
            raise ContractError('invalid evaluation counts')
        if (metric['correct_predictions'] > metric['high_confidence_predictions']
            or metric['correct_predictions'] > metric['expected_findings']
            or metric['high_confidence_cases'] > metric['high_confidence_predictions']
            or metric['wrong_application_matches'] > metric['high_confidence_predictions'] - metric['correct_predictions']
            or metric['missed_findings'] != metric['expected_findings'] - metric['correct_predictions']):
            raise ContractError('inconsistent evaluation counts')
    return UnderstandingEvaluationReport(data)


def load_report(path: Path | None):
    """An absent/invalid artifact disables automation, without disabling review."""
    if path is None:
        return None
    try:
        info = os.stat(path)
        if info.st_mode & 0o077 or info.st_uid != os.getuid() or info.st_size > 131072:
            return None
        return validate_report(json.loads(Path(path).read_text()))
    except (OSError, ValueError, TypeError, ContractError):
        return None


def evaluate_cases(cases, *, producer_version, dataset_kind='synthetic', threshold=.9):
    """Score independently reviewed expected findings against recorded predictions.

    Inputs contain kind/application_id/label for each expected and predicted finding,
    confidence for predictions, and optional operational counters. Bodies need not be
    exported. Exact multiset matching counts duplicate predictions as false positives.
    Synthetic reports are diagnostic only and cannot pass load_report.
    """
    from collections import Counter
    from .proposals import MAIL_EVENT_TYPES
    if type(threshold) not in (int, float) or not math.isfinite(threshold) or not .9 <= threshold <= 1:
        raise ContractError('invalid evaluation threshold')
    if not isinstance(cases, list) or not 1 <= len(cases) <= 10000:
        raise ContractError('evaluation requires 1..10000 cases')
    if dataset_kind not in {'synthetic', 'reviewed_private_holdout'}:
        raise ContractError('invalid evaluation dataset kind')
    if not isinstance(producer_version, str) or not producer_version or len(producer_version) > 200:
        raise ContractError('invalid evaluation producer version')
    classes = {}
    totals = dict(cases=0, invalid_outputs=0, coverage_abstentions=0, duplicate_predictions=0,
                  review_items=0, latency_ms=0, cost_microusd=0)
    ids = set()
    labels = {'event': {e.value for e in MAIL_EVENT_TYPES}, 'action': AUTO_ACTIONS | {'other'}, 'temporal': {'deadline', 'interview'}}
    def findings(values, *, prediction=False):
        if not isinstance(values, list) or len(values) > 24:
            raise ContractError('evaluation findings must be a bounded array')
        seen_types = Counter()
        for finding in values:
            fields = {'kind', 'application_id', 'label'} | ({'confidence'} if prediction else set())
            if not isinstance(finding, dict) or set(finding) != fields:
                raise ContractError('invalid evaluation finding fields')
            kind, label = finding['kind'], finding['label']
            if not isinstance(kind, str) or kind not in labels or not isinstance(label, str) or label not in labels[kind]:
                raise ContractError('invalid evaluation finding kind')
            if finding['application_id'] is not None:
                validate_identifier(finding['application_id'], 'application_id')
            seen_types[kind] += 1
            if seen_types[kind] > 8:
                raise ContractError('evaluation finding count exceeds the analysis contract')
            if prediction:
                score = finding['confidence']
                if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1:
                    raise ContractError('invalid evaluation confidence')
        return values

    def metric_for(class_id):
        return classes.setdefault(class_id, dict(high_confidence_predictions=0,high_confidence_cases=0,
            correct_predictions=0,wrong_application_matches=0,expected_findings=0,missed_findings=0))

    for case in cases:
        if not isinstance(case, dict) or not {'case_id', 'expected', 'predicted'} <= set(case) or set(case) - ({'case_id', 'expected', 'predicted'} | set(totals) - {'cases', 'duplicate_predictions'}):
            raise ContractError('invalid evaluation case fields')
        case_id = case['case_id']
        validate_identifier(case_id, 'case_id')
        if case_id in ids:
            raise ContractError('duplicate evaluation case')
        ids.add(case_id)
        totals['cases'] += 1
        for key in totals.keys() - {'cases', 'duplicate_predictions'}:
            value = case.get(key, 0)
            if type(value) is not int or value < 0:
                raise ContractError('invalid evaluation counter')
            totals[key] += value
        if case.get('invalid_outputs', 0) not in (0, 1):
            raise ContractError('each evaluation case represents one analysis output')
        expected_values = findings(case['expected'])
        predicted_values = findings(case['predicted'], prediction=True)
        expected = Counter((f['kind'], f['application_id'], f['label']) for f in expected_values)
        seen = Counter()
        for key, count in expected.items():
            metric = metric_for(key[0] + ':' + key[2])
            metric['expected_findings'] += count
        case_classes = set()
        for f in ([] if case.get('invalid_outputs') else predicted_values):
            if f['confidence'] < threshold:
                continue
            key = (f['kind'], f['application_id'], f['label'])
            class_id = key[0] + ':' + key[2]
            metric = metric_for(class_id)
            case_classes.add(class_id)
            metric['high_confidence_predictions'] += 1
            seen[key] += 1
            if seen[key] > 1:
                totals['duplicate_predictions'] += 1
            if expected[key]:
                metric['correct_predictions'] += 1
                expected[key] -= 1
            elif f['application_id'] is not None and not any(e['kind'] == key[0] and e['label'] == key[2] and e['application_id'] == key[1] for e in expected_values):
                metric['wrong_application_matches'] += 1
        for class_id in case_classes:
            classes[class_id]['high_confidence_cases'] += 1
        for key, count in expected.items():
            classes[key[0] + ':' + key[2]]['missed_findings'] += count
    eligible = {'event:' + e.value for e in MODEL_AUTO_APPLY_EVENT_TYPES} | {'action:' + k for k in AUTO_ACTIONS}
    report = dict(version=1, provenance=provenance(producer_version),dataset_fingerprint=payload_sha256(cases),
                  dataset_kind=dataset_kind, threshold=threshold, classes={k:v for k,v in classes.items() if k in eligible})
    return {'report': report, 'metrics': totals, 'all_classes': classes}
