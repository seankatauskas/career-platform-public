"""Fictional production dashboard backed by the application owners; no workers."""
import argparse
import json
from pathlib import Path

from job_search.application_gateway import ApplicationGateway
from job_search.application_runtime import ApplicationRuntime
from job_search.application_transport import AgentApplicationAdapter, HumanApplicationAdapter
from job_search.commands import CommandContext, Principal
from job_search.dashboard import DashboardController, make_server
from job_search.service import JobSearchLedger
from tests.test_browser_tracking import Catalog
from tests.test_job_search_dashboard import FakePreferences


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--state-dir', type=Path, required=True)
    args = parser.parse_args()
    ledger = JobSearchLedger(args.state_dir / 'operations.db')
    runtime = ApplicationRuntime(args.state_dir / 'owners.db')
    human = HumanApplicationAdapter(runtime, 'fixture-owner')
    application = human.command('save_job', {'job_source': {'source': 'greenhouse', 'source_id': '12345',
        'employer': 'Example Labs', 'title': 'Platform Engineer'}}, 'save')
    second = human.command('save_job', {'job_source': {'source': 'fixture', 'source_id': 'second',
        'employer': 'Other Company', 'title': 'Product Engineer'}}, 'second')
    human.command('add_note', {'application_id': application['id'], 'text': 'Discuss the platform team.'}, 'note')
    human.command('record_submission', {'application_id': application['id'], 'status': 'confirmed',
        'occurred_at': '2026-10-01T12:00:00Z'}, 'submission')
    human.command('create_task', {'application_id': application['id'], 'kind': 'other',
        'description': 'Prepare portfolio'}, 'task')
    AgentApplicationAdapter(runtime).call('propose_changes', {'operation': 'create_task',
        'application_id': application['id'], 'input': {'application_id': application['id'],
        'kind': 'complete_assessment', 'description': 'Complete the design exercise'}}, idempotency_key='proposal')
    AgentApplicationAdapter(runtime).call('propose_changes', {'operation': 'add_note',
        'application_id': second['id'], 'input': {'application_id': second['id'],
        'text': 'Review the product team.'}}, idempotency_key='second-proposal')
    principal = Principal('fixture', 'human', frozenset({'*'}))
    message = runtime.executor.run(CommandContext(principal, 'message'), 'record_message', {},
        lambda tx: runtime.correspondence.record_message(tx, account_id='fixture-account',
            provider_message_id='fixture-message', source_version='v1', direction='incoming',
            authored_text='Please bring your portfolio. <script>unsafe()</script>', archive_ref='fixture-archive'))
    runtime.executor.run(CommandContext(principal, 'link'), 'link_message', {},
        lambda tx: runtime.correspondence.link_message(tx, message_id=message['id'], application_id=application['id']))

    class FictionalMailReader:
        def read_message(self, application_id, message_id):
            return {'text': 'Please bring your portfolio. <script>unsafe()</script>',
                'revision': 1, 'coverage': {'complete': True}}
    runtime.mail_reader = FictionalMailReader()
    class DatedCatalog(Catalog):
        def get_job(self, ats, job_id):
            if job_id == 'shortlisted-role':
                return {**super().get_job(ats, '12345'), 'id': job_id, 'title': 'Another Engineer',
                    'company': 'Shortlist Company', 'company_slug': 'shortlist-company',
                    'jobUrl': 'https://job-boards.greenhouse.io/shortlist-company/jobs/67890'}
            return super().get_job(ats, job_id)
        def posting_summaries(self, identities):
            return {identity: {'ats': identity[0], 'posted_at': '2026-09-15T12:00:00Z'}
                    for identity in identities if identity == ('greenhouse', '12345')}
    gateway = ApplicationGateway(runtime, ledger, catalog=DatedCatalog())
    controller = DashboardController(ledger, FakePreferences(), jobs=DatedCatalog(), application_gateway=gateway, demo_mode=True)
    shortlist = controller.curated.publish({'title': 'Fictional shortlist', 'idempotency_key': 'shortlist',
        'jobs': [{'ats': 'greenhouse', 'job_id': 'shortlisted-role', 'explanation': 'A role to explore.'}]})
    server = make_server(controller, port=0)
    print(json.dumps({'url': 'http://127.0.0.1:' + str(server.server_port),
        'application_id': application['id'], 'second_id': second['id'], 'shortlist': shortlist['dashboard_path']}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
