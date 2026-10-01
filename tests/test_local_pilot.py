"""Offline regressions for hybrid inference and truthful local enrollment."""
import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from job_search.inference import build_structured_provider, load_inference_config
from job_search.inference.providers import OpenAICompatibleStructuredGenerator
from job_search.ranking.model import encoder_for_recorded_revision, PreferenceModelError
from job_search.runtime import load_runtime_config
from job_search.runtime_readiness import runtime_readiness
from job_search.setup import initialize
from job_search.verification import record_verification, verification_capabilities

ROOT = Path(__file__).resolve().parents[1]


def profile(root):
    (root / 'key').write_text('test-token')
    (root / 'key').chmod(0o600)
    path = root / 'inference.json'
    path.write_text(json.dumps({'version': 1, 'profile_id': 'test-hybrid',
        'structured_generation': {'kind': 'openrouter', 'model': 'example/model',
            'credential_file': 'key', 'max_input_tokens': 65536,
            'timeout_seconds': 60, 'max_response_bytes': 1048576,
            'default_max_output_tokens': 4096}, 'embeddings': None}))
    path.chmod(0o600)
    return path


class LocalPilotTests(unittest.TestCase):
    def test_graph_odata_folder_pagination_preserves_link_and_read_boundary(self):
        from job_search.outlook.transport import RetryClass, GraphLinkError
        from job_search.outlook.auth import BASE_SCOPES, DRAFT_SCOPES
        from tests.test_job_search_outlook import session_with, response
        url = "https://graph.microsoft.com/v1.0/me/mailFolders('AQFk-test_01==')/messages/delta?$skiptoken=opaque%2Bfixture"
        session, tokens, http = session_with(response(200, {'value': []}))
        self.assertEqual(session.request_json('GET', url, scopes=BASE_SCOPES, retry_class=RetryClass.READ), {'value': []})
        self.assertEqual(http.requests[0][1], url)
        for method, target, scopes in [
            ('POST', url, BASE_SCOPES), ('GET', url, DRAFT_SCOPES),
            ('GET', url.replace('/me/', '/users/other/'), BASE_SCOPES),
            ('GET', url.replace('/messages/delta', '/sendMail'), BASE_SCOPES),
        ]:
            with self.assertRaises(GraphLinkError):
                session.request_json(method, target, scopes=scopes, retry_class=RetryClass.READ)
        self.assertEqual(len(http.requests), 1)

    def test_private_board_registry_overrides_inherited_cache(self):
        from job_search.runtime import RuntimeConfigV1
        config = replace(RuntimeConfigV1.defaults(ROOT), board_registry_path=Path('/private/boards.json'))
        self.assertEqual(config.environment({'JOB_BOARDS_CACHE': '/legacy/boards.json'})['JOB_BOARDS_CACHE'],
                         '/private/boards.json')

    def test_launchd_refuses_protected_worktree_before_service_mutation(self):
        from job_search.launchd import manage_launch_agents
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize(root/'config.json', root, ROOT)
            config = load_runtime_config(root/'config.json', required=True)
            config = replace(config, project_root=Path.home()/'Documents'/'example-project')
            runner = Mock()
            with self.assertRaisesRegex(ValueError, 'outside Documents'):
                manage_launch_agents(config, action='install', apply=True,
                                    output_dir=root/'agents', runner=runner)
            runner.assert_not_called()
            self.assertFalse((root/'agents').exists())

    def test_initial_catalog_is_not_a_notification_backlog(self):
        from job_search.integration import ShortlistNotificationEvaluator
        gateway, ledger, publisher = Mock(), Mock(), Mock()
        ledger.get_shortlist_notification_state.return_value = {}
        ledger.application_keys.return_value = set()
        gateway.notification_exposed_job_keys.return_value = set()
        gateway.preview_shortlist.return_value = {'recommendations': [
            {'ats': 'ashby', 'id': 'old', 'first_seen': '2026-09-01T00:00:00+00:00'},
            {'ats': 'ashby', 'id': 'unknown'},
        ]}
        evaluator = ShortlistNotificationEvaluator(gateway, ledger, publisher, options={},
            enabled=True, first_seen_since='2026-09-27T00:00:00Z')
        result = evaluator({}, Mock(workflow_id='pilot'))
        self.assertTrue(result['suppressed'])
        publisher.publish.assert_not_called()
        gateway.preview_shortlist.return_value['recommendations'].append(
            {'ats': 'ashby', 'id': 'new', 'first_seen': '2026-09-28T00:00:00+00:00'})
        publisher.publish.return_value = {'created': True, 'suppressed': False}
        gateway.record_notification_shortlist.return_value = {'session_id': 'test-session'}
        self.assertEqual(evaluator({}, Mock(workflow_id='pilot'))['jobs'], 1)
        recorded = gateway.record_notification_shortlist.call_args.args[0]['recommendations']
        self.assertEqual([r['id'] for r in recorded], ['new'])

    def test_quote_alignment_never_guesses_missing_or_ambiguous_evidence(self):
        from job_search.mail.remote import _align_unique_evidence
        row = {'evidence_quote': 'Interview requested', 'span_start': 0, 'span_end': 3}
        source = 'Hello. Interview requested. Thank you.'
        fixed = _align_unique_evidence(row, source)
        self.assertEqual(source[fixed['span_start']:fixed['span_end']], row['evidence_quote'])
        self.assertEqual(_align_unique_evidence(row, source + source), row)
        self.assertEqual(_align_unique_evidence(row, 'unrelated email'), row)
        malformed = {**row, 'span_start': True}
        self.assertEqual(_align_unique_evidence(malformed, source), malformed)

    def test_openrouter_requires_private_structured_output_and_attests_hosted_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            config = load_inference_config(profile(Path(directory)))
            transport = Mock(return_value={'model': 'example/model', 'provider': 'test-provider',
                'choices': [{'message': {'content': '{"ok":true}'}, 'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}})
            provider = OpenAICompatibleStructuredGenerator(config.structured_generation, transport)
            result = provider.generate([{'role': 'user', 'content': 'Return JSON'}],
                json_schema={'type': 'object', 'properties': {'ok': {'type': 'boolean'}},
                    'required': ['ok'], 'additionalProperties': False},
                schema_name='test', max_output_tokens=128, temperature=0)
            request = json.loads(transport.call_args.args[2])
            self.assertEqual(request['provider'], {'data_collection': 'deny', 'require_parameters': True})
            self.assertEqual(request['reasoning'], {'enabled': False})
            self.assertEqual(request['response_format']['type'], 'json_schema')
            self.assertNotIn('models', request)
            self.assertIsNone(provider.provenance['weights_revision'])
            self.assertEqual(result.usage['response_provider'], 'test-provider')

    def test_generation_only_profile_keeps_exact_local_encoder(self):
        with tempfile.TemporaryDirectory() as directory:
            path = profile(Path(directory))
            revision = 'BAAI/bge-base-en-v1.5@' + 'a' * 40
            encoder = Mock(model_revision=revision)
            with patch('job_search.ranking.model.SentenceTransformerEncoder', return_value=encoder) as local:
                self.assertIs(encoder_for_recorded_revision(revision, 'cpu', path), encoder)
                local.assert_called_once_with('BAAI/bge-base-en-v1.5', 'a' * 40, 'cpu')
            with patch('job_search.ranking.model._remote_encoder', return_value=Mock(model_revision='different')):
                with self.assertRaises(PreferenceModelError):
                    encoder_for_recorded_revision(revision, remote_provider=Mock())

    def test_paused_installation_is_not_overdue_and_receipts_are_bound_to_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize(root / 'config.json', root, ROOT)
            config = load_runtime_config(root / 'config.json', required=True)
            config = replace(config, outlook_client_id='11111111-1111-1111-1111-111111111111')
            report = runtime_readiness(config, dependencies={})
            caps = {c['id']: c for c in report['capabilities']}
            self.assertEqual(caps['outlook']['status'], 'paused')
            self.assertTrue(caps['outlook']['configured'])
            self.assertFalse(caps['outlook']['enabled'])
            record_verification(config, 'outlook_read', succeeded=True)
            self.assertEqual(verification_capabilities(config)[0]['status'], 'ready')
            changed = replace(config, outlook_client_id='22222222-2222-2222-2222-222222222222')
            self.assertEqual(verification_capabilities(changed)[0]['status'], 'configured_unverified')
            receipt = config.log_dir / 'connection-verifications.json'
            self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(config.outlook_client_id, receipt.read_text())

    def test_failed_board_cannot_close_previously_open_posting(self):
        from job_search.collection import boards
        from tests.test_job_boards import BASE
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); db = root / 'jobs.db'; registry = root / 'boards.json'
            registry.write_text(json.dumps({'greenhouse': ['good', 'failed']}))
            rows = [{**BASE, 'ats': 'greenhouse', 'company': company, 'id': company,
                     'title': 'Software Engineer'} for company in ('good', 'failed')]
            boards.save(rows, db, '2026-09-01T00:00:00Z')
            def scan(ats, slug, *args, **kwargs):
                if slug == 'failed':
                    raise RuntimeError('synthetic unavailable board')
                return []
            argv = ['collector', '--all', '--ats', 'greenhouse', '--boards-from', str(registry),
                    '--db', str(db), '--out', str(root / 'output')]
            with patch('sys.argv', argv), patch.object(boards, 'scan_board', side_effect=scan), \
                 contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                boards.main()
            with sqlite3.connect(db) as con:
                closed = dict(con.execute('SELECT id,closed_at FROM jobs'))
            self.assertIsNone(closed['failed'])
            self.assertTrue(closed['good'])
            receipt = json.loads((root / 'output.collection.json').read_text())
            self.assertEqual(receipt['status'], 'partial')
            self.assertEqual(receipt['boards_failed'], 1)


if __name__ == '__main__':
    unittest.main()
