"""Opt-in live semantic evaluation of private, versioned AnalysisInput snapshots.

Cases: name, context (sources including text/hash, candidates, versions, coverage,
context), expected (category, kind, target_id and optional value/request fields),
and optional expected_relevance/scope. Expectations must be independently reviewed.
This command never connects to the production application or accepts proposals.
"""
import argparse
import json
from pathlib import Path
import time

from job_search.applications.understanding import SharedAnalyzer
from job_search.applications.understanding.evaluation import evaluate_case, load_case, summarize
from job_search.inference import build_structured_provider, load_inference_config


def replay(cases, provider, *, repeat=1):
    if (not isinstance(cases, list) or not 1 <= len(cases) <= 200
            or type(repeat) is not int or not 1 <= repeat <= 10):
        raise ValueError('Use 1..200 cases and 1..10 repetitions')
    for case in cases:
        load_case(case)  # Validate the entire dataset before spending on inference.
    if len({case['name'] for case in cases}) != len(cases):
        raise ValueError('Case names must be unique')
    reports = []
    class ObservedProvider:
        max_input_tokens = provider.max_input_tokens
        count_tokens_upper_bound = staticmethod(provider.count_tokens_upper_bound)
        def __init__(self):
            self.calls = []
        def generate(self, *args, **kwargs):
            started = time.monotonic()
            call = {'stage': kwargs['schema_name']}
            self.calls.append(call)
            try:
                response = provider.generate(*args, **kwargs)
                usage = getattr(response, 'usage', {})
                # Provider-specific usage can contain arbitrary data; retain
                # only standard numeric token counters.
                call['tokens'] = {key: value for key, value in usage.items()
                    if key in {'input_tokens', 'output_tokens', 'prompt_tokens', 'completion_tokens', 'total_tokens'}
                    and type(value) is int and value >= 0}
                return response
            finally:
                call['seconds'] = round(time.monotonic() - started, 3)
    for repetition in range(repeat):
        for case in cases:
            observed = ObservedProvider()
            report = evaluate_case(case, SharedAnalyzer(observed))
            report.update(repetition=repetition + 1, calls=observed.calls)
            reports.append(report)
    return {'summary': summarize(reports), 'cases': reports}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases', type=Path, required=True)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repeat', type=int, default=1)
    args = parser.parse_args()
    # Reserve a new private report before making any billable calls.
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x') as destination:
        args.output.chmod(0o600)
        profile = load_inference_config(args.config)
        report = replay(json.loads(args.cases.read_text()), build_structured_provider(profile), repeat=args.repeat)
        report.update(model=profile.structured_generation.generation_identity,
                      dataset_kind='development_replay',
                      qualification='A reviewed independent holdout is still required before claiming reliability.')
        json.dump(report, destination, indent=2)
        destination.write('\n')
    print(json.dumps(report['summary']))
    return 0 if report['summary']['all_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
