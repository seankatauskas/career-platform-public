"""Score versioned analysis snapshots against independently reviewed expectations.

This measures proposed findings, never authorizes their acceptance. Fixtures and
live providers use the same scorer. Reports contain no quotes or message bodies.
"""
from collections import Counter
from copy import deepcopy

from job_search.commands import DomainError, digest
from job_search.inference import InferenceTransportError
from .contracts import AnalysisInput, SourceText, KINDS, validate_analysis
from .recipes import TERMINAL_OUTCOMES
from .validation import validate_diagnostic


def load_case(case):
    if (not isinstance(case, dict) or not isinstance(case.get('name'), str)
            or not 1 <= len(case['name']) <= 120 or not isinstance(case.get('context'), dict)
            or not isinstance(case.get('expected'), list) or len(case['expected']) > 80):
        raise ValueError('A case needs a name, exact analysis context, and bounded expected findings')
    raw = deepcopy(case['context'])
    raw['sources'] = tuple(SourceText(**s) for s in raw['sources'])
    raw['candidates'] = tuple(raw.get('candidates', ()))
    context = AnalysisInput(**raw)  # Checks exact source hashes and bounds.
    targets = {c['id'] for c in context.candidates} | {
        c['id'] for c in context.context.get('catalog_candidates', [])}
    for expected in case['expected']:
        if (not isinstance(expected, dict) or not {'category', 'kind', 'target_id'} <= set(expected)
                or set(expected) - {'category', 'kind', 'target_id', 'value', 'requirement', 'responsible_party', 'channel'}
                or not isinstance(expected['category'], str) or expected['category'] not in KINDS
                or not isinstance(expected['kind'], str) or expected['kind'] not in KINDS[expected['category']]
                or (expected['target_id'] is not None and (
                    not isinstance(expected['target_id'], str) or expected['target_id'] not in targets))):
            raise ValueError('Invalid reviewed expectation or target outside supplied context')
    if case.get('expected_relevance', 'career_related') not in {'career_related', 'unrelated', 'uncertain'}:
        raise ValueError('Invalid expected relevance')
    return context


def _contains(actual, expected):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and _contains(actual[k], v) for k, v in expected.items())
    return type(actual) is type(expected) and actual == expected


def _signature(group, finding):
    # No evidence quotes, request descriptions, or arbitrary model fields.
    result = {'category': group, 'kind': finding['kind'], 'target_id': finding.get('target_id')}
    if group == 'facts' and finding['kind'] in {'submission', 'outcome'} and isinstance(finding['value'], dict):
        field = 'status' if finding['kind'] == 'submission' else 'outcome'
        allowed = TERMINAL_OUTCOMES if field == 'outcome' else {
            'attempted', 'submitted', 'received', 'confirmed', 'failed', 'retracted', 'pending_verification'}
        value = finding['value'].get(field)
        result['value'] = {field: value if isinstance(value, str) and value in allowed else None}
    return result


def _match(expected, predicted):
    """Maximum one-to-one match; broad expectations cannot steal specific ones."""
    assigned = {}
    def visit(index, seen):
        for j, finding in enumerate(predicted):
            if j in seen or not _contains(finding, expected[index]):
                continue
            seen.add(j)
            if j not in assigned or visit(assigned[j], seen):
                assigned[j] = index
                return True
        return False
    for index in range(len(expected)):
        visit(index, set())
    return set(assigned.values()), set(assigned)


def evaluate_case(case, analyzer):
    context = load_case(case)
    fingerprint = digest({'context': context.descriptor(), 'expected': case['expected'],
                          'relevance': case.get('expected_relevance', 'career_related')})
    report = {'name': case['name'], 'fingerprint': fingerprint,
              'message_fingerprint': digest(context.descriptor()['sources']),
              'prompt': analyzer.prompt_version, 'scope': case.get('scope', 'saved AnalysisInput'),
              'expected_findings': len(case['expected']), 'matched_findings': 0,
              'missed_findings': len(case['expected']), 'unexpected_findings': 0,
              'wrong_targets': 0, 'passed': False}
    try:
        result = analyzer.analyze(context)
        validate_analysis(result, context)
        diagnostic = getattr(result, 'validation_diagnostic', None)
        if diagnostic is not None:
            validate_diagnostic(diagnostic)
        predicted = [dict(f, category=group) for group in KINDS for f in result[group]]
        expected_matches, predicted_matches = _match(case['expected'], predicted)
        unmatched = [p for j, p in enumerate(predicted) if j not in predicted_matches]
        # Count erroneous identity proposals separately, including unexpected
        # links when the reviewed answer requires abstention.
        known_targets = {e['target_id'] for e in case['expected'] if e['target_id'] is not None}
        wrong = 0
        for finding in unmatched:
            peers = [e for e in case['expected'] if e['category'] == finding['category'] and e['kind'] == finding['kind']]
            allowed_targets = {e['target_id'] for e in peers} if peers else known_targets
            # An extra unsupported fact about the correct application is an
            # unexpected finding, not an incorrect application identity.
            wrong += finding.get('target_id') is not None and finding['target_id'] not in allowed_targets
        report.update(matched_findings=len(expected_matches),
            missed_findings=len(case['expected']) - len(expected_matches),
            unexpected_findings=len(unmatched), wrong_targets=wrong,
            relevance_correct=result['relevance'] == case.get('expected_relevance', 'career_related'),
            predicted=[_signature(g, f) for g in KINDS for f in result[g]], diagnostics=diagnostic)
        report['passed'] = (not report['missed_findings'] and not report['unexpected_findings']
            and report['relevance_correct'] and not (diagnostic and diagnostic.get('issues', [diagnostic])))
    except DomainError as exc:
        diagnostic = getattr(exc, 'validation_diagnostic', None)
        if diagnostic is not None:
            validate_diagnostic(diagnostic)
        report.update(failure='analysis_failed', diagnostics=diagnostic)
    except InferenceTransportError:
        report.update(failure='provider_failed')
    return report


def summarize(reports):
    if not reports:
        raise ValueError('Evaluation requires at least one observation')
    counts = Counter(r['message_fingerprint'] for r in reports)
    matched = sum(r['matched_findings'] for r in reports)
    expected = sum(r['expected_findings'] for r in reports)
    predicted = matched + sum(r['unexpected_findings'] for r in reports)
    return {'observations': len(reports), 'independent_messages': len(counts),
            'passed': sum(r['passed'] for r in reports),
            'all_passed': all(r['passed'] for r in reports),
            'failures': dict(Counter(r['failure'] for r in reports if 'failure' in r)),
            'finding_precision': matched / predicted if predicted else None,
            'finding_recall': matched / expected if expected else None,
            **{key: sum(r[key] for r in reports) for key in (
                'expected_findings', 'matched_findings', 'missed_findings', 'unexpected_findings', 'wrong_targets')},
            'scope': 'Observed proposed findings; repeated runs are not independent messages. No auto-approval qualification.'}
