"""Opt-in live-provider replay of private receipt cases in disposable application state."""
import argparse
import hashlib
import json
from pathlib import Path
import tempfile
import time

from job_search.application_runtime import ApplicationRuntime
from job_search.application_transport import HumanApplicationAdapter
from job_search.applications.understanding import SharedAnalyzer
from job_search.commands import CommandContext, Principal
from job_search.inference import build_structured_provider, load_inference_config


def replay(cases, profile):
    if not isinstance(cases, list) or not 1 <= len(cases) <= 10:
        raise ValueError('Supply between one and ten private receipt cases')
    provider = build_structured_provider(profile)
    reports = []
    with tempfile.TemporaryDirectory(prefix='receipt-replay-') as temporary:
        for index, case in enumerate(cases):
            body = case['source']['text']
            if hashlib.sha256(body.encode()).hexdigest() != case['source']['sha256']:
                raise ValueError('Source hash does not match the saved email')
            runtime = ApplicationRuntime(Path(temporary) / (str(index) + '.db'))
            human = HumanApplicationAdapter(runtime, 'isolated-fixture-setup')
            ids, employers = [], {}
            for i, candidate in enumerate(case['candidates']):
                job = candidate['job']; snapshot = job.get('recorded_snapshot') or {}
                employer = job.get('employer') or snapshot.get('employer')
                app = human.command('save_job', {'job_source': {'source': 'fixture', 'source_id': str(i),
                    'employer': employer, 'title': job.get('title') or snapshot.get('title')}}, 'seed-' + str(i))
                ids.append(app['id']); employers[app['id']] = employer
            message = runtime.command(CommandContext(Principal('fixture-mail', 'worker', {'record_message'}), 'message', 'inferred'),
                'record_message', {'account_id': 'replay', 'provider_message_id': case['source']['source_id'],
                'source_version': 1, 'direction': 'incoming', 'authored_text': body, 'archive_ref': 'replay-source'})
            class Archive:
                def read_message(self, ref): return body
            class ObservedProvider:
                max_input_tokens = provider.max_input_tokens
                count_tokens_upper_bound = staticmethod(provider.count_tokens_upper_bound)
                def __init__(self): self.calls = []
                def generate(self, *args, **kwargs):
                    start = time.monotonic()
                    response = provider.generate(*args, **kwargs)
                    self.calls.append({'stage': kwargs['schema_name'], 'seconds': round(time.monotonic() - start, 2), 'usage': response.usage})
                    return response
            observed = ObservedProvider()
            analyzer = SharedAnalyzer(observed)
            result = runtime.analyze_message(message['id'], archive=Archive(), allowed_accounts=('replay',),
                candidate_ids=ids, analyzer=analyzer, model_version=profile.structured_generation.generation_identity)
            with runtime.executor.read() as connection:
                issue = runtime.understanding.processing_for_source(connection, message['id'], message['revision'])
                accepted_receipts = sum(len(runtime.applications.list_records(connection, app, 'receipts')['items']) for app in ids)
                accepted_link = runtime.correspondence.association(connection, message['id'])
            proposals = result.get('proposals', [])
            reports.append({'name': case['name'], 'expected_employer': case['expected_employer'], 'model': profile.structured_generation.generation_identity,
                'prompt': analyzer.prompt_version, 'source_sha256': case['source']['sha256'], 'calls': observed.calls,
                'processing': issue['status'], 'failure_code': issue['failure_code'], 'diagnostics': issue['validation_diagnostic'],
                'proposals': [{'operation': p['operation'], 'status': p['status'], 'employer': employers.get(p['application_id']),
                               'blockers': p['blockers']} for p in proposals],
                'accepted_receipts': accepted_receipts, 'accepted_link': accepted_link is not None,
                'scope': 'Saved email and candidate employer/title set; isolated fresh application records; no catalog/thread search replay.'})
    return reports


def passed(report):
    return (report['processing'] == 'succeeded' and not report['accepted_receipts'] and not report['accepted_link']
            and {p['operation'] for p in report['proposals']} == {'link_message', 'record_submission_receipt'}
            and all(p['status'] == 'pending' and not p['blockers'] and p['employer'] == report['expected_employer'] for p in report['proposals']))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases', type=Path, required=True, help='Private JSON list with name, expected_employer, source (text, sha256, source_id), and candidate job records')
    parser.add_argument('--config', type=Path, required=True, help='Configured structured-generation profile; invokes its live provider')
    parser.add_argument('--output', type=Path, required=True, help='Private JSON report; email text and credentials are excluded')
    args = parser.parse_args()
    reports = replay(json.loads(args.cases.read_text()), load_inference_config(args.config))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.touch(mode=0o600, exist_ok=True); args.output.chmod(0o600)
    args.output.write_text(json.dumps(reports, indent=2) + '\n')
    print(json.dumps({'cases': len(reports), 'passed': sum(passed(r) for r in reports), 'report': str(args.output)}))
    return 0 if all(passed(r) for r in reports) else 1


if __name__ == '__main__':
    raise SystemExit(main())
