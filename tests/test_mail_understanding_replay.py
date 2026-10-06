"""Archive replay membership, pause fencing and uncertain requests stay bounded."""
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from job_search.contracts import ContractError, MutationContext
from job_search.db import connect
from job_search.mail.understanding_replay import UnderstandingReplay
from tests.test_job_search_career_actions import setup


STAMP = '2026-10-04T12:00:00Z'


def replay_fixture(directory, *, with_archive=True):
    path, ledger, _, _, app, eid = setup(directory, body='Please reply')
    with connect(path) as con:
        observation = dict(con.execute('SELECT * FROM lifecycle_mail_observations WHERE evidence_id=?', (eid,)).fetchone())
        if with_archive:
            con.execute('INSERT INTO mail_archive VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                        ('archive', 'account', observation['immutable_message_id'], 'fixture-key', b'0'*12, b'ciphertext'*3, 'a'*64, 'b'*64, 12, 0, STAMP, STAMP))
            con.execute('UPDATE lifecycle_mail_observations SET archive_id=? WHERE observation_id=?', ('archive', observation['observation_id']))
    runtime = SimpleNamespace(mode='shared', analyzer=object(), calls=[], archive=SimpleNamespace(read_message=lambda _: 'Subject: Interview\n\nPlease reply'))
    def process(observation, **kwargs):
        runtime.calls.append((observation, kwargs))
        return {'analysis_id': 'analysis-' + str(len(runtime.calls)), 'state': 'projected'}
    runtime.process = process
    runtime.service = SimpleNamespace(can_retry_message=lambda *args: False)
    return path, ledger, runtime, observation, app


def add_message(ledger, app, identity):
    stamp = '2030-01-02T12:00:00Z'
    context = MutationContext(identity, 'system', 'replay_test')
    evidence = ledger.record_mail_evidence(dict(account_id='account', immutable_message_id=identity, sender='r@example.test',
        subject='Interview', received_at=stamp, body_sha256='d'*64, excerpt='Please reply'), context)['evidence']
    observation = ledger.lifecycle.observe_mail(dict(account_id='account', immutable_message_id=identity, direction='inbound',
        subject='Interview', source_at=stamp, modified_at=stamp, evidence_id=evidence['evidence_id']), context)['observation']
    ledger.lifecycle.link_mail(dict(observation_id=observation['observation_id'], application_id=app), MutationContext(identity+'-link', 'user', 'replay_test'))
    return observation


class ReplaySafetyTests(unittest.TestCase):
    def test_fixed_membership_excludes_new_mail_and_uses_current_app_context(self):
        with TemporaryDirectory() as directory:
            path, ledger, runtime, _, app = replay_fixture(directory)
            replay = UnderstandingReplay(ledger, runtime)
            job = replay.start('account', MutationContext('start', 'user', 'test'))
            add_message(ledger, app, 'new-after-start')
            with connect(path) as con:
                con.execute("UPDATE applications SET current_phase='interviewing' WHERE application_id=?", (app,))
            result = replay.run_batch(job['replay_id'])
            self.assertEqual(result['counts'], {'done': 1})
            self.assertEqual(len(runtime.calls), 1)
            self.assertEqual(runtime.calls[0][1]['candidates'][0].phase, 'interviewing')
            self.assertEqual(replay.preview('account')['messages'], 2)

    def test_missing_or_unreadable_archive_stays_visible_without_model_call(self):
        for present in (False, True):
            with self.subTest(present=present), TemporaryDirectory() as directory:
                _, ledger, runtime, _, _ = replay_fixture(directory, with_archive=present)
                def unavailable(_): raise ContractError('fixture archive cannot be decrypted')
                runtime.archive.read_message = unavailable
                replay = UnderstandingReplay(ledger, runtime)
                job = replay.start('account', MutationContext('start', 'user', 'test'))
                result = replay.run_batch(job['replay_id'])
                self.assertEqual(result['counts'], {'unavailable': 1})
                self.assertEqual(result['issues'][0]['reason'], 'archive_unavailable' if present else 'archive_missing')
                self.assertEqual(runtime.calls, [])

    def test_changed_source_is_held_before_archive_or_provider_access(self):
        with TemporaryDirectory() as directory:
            path, ledger, runtime, observation, _ = replay_fixture(directory)
            replay = UnderstandingReplay(ledger, runtime)
            job = replay.start('account', MutationContext('start', 'user', 'test'))
            with connect(path) as con:
                con.execute('UPDATE lifecycle_mail_observations SET archive_id=NULL WHERE observation_id=?', (observation['observation_id'],))
            result = replay.run_batch(job['replay_id'])
            self.assertEqual(result['issues'][0]['reason'], 'message_source_changed')
            self.assertEqual(runtime.calls, [])

    def test_uncertain_provider_outcome_is_not_blindly_retried(self):
        class Uncertain(RuntimeError): outcome_unknown = True
        with TemporaryDirectory() as directory:
            _, ledger, runtime, _, _ = replay_fixture(directory)
            def process(*args, **kwargs):
                runtime.calls.append('attempt')
                raise Uncertain('provider outcome is unknown')
            runtime.process = process
            replay = UnderstandingReplay(ledger, runtime)
            job = replay.start('account', MutationContext('start', 'user', 'test'))
            self.assertEqual(replay.run_batch(job['replay_id'])['counts'], {'reconciliation': 1})
            self.assertEqual(replay.run_batch(job['replay_id'], retry_failed=True)['counts'], {'reconciliation': 1})
            self.assertEqual(runtime.calls, ['attempt'])
            runtime.service.can_retry_message = lambda *args: True
            runtime.process = lambda *args, **kwargs: {'analysis_id': 'recovered', 'state': 'projected'}
            self.assertEqual(replay.run_batch(job['replay_id'], retry_failed=True)['counts'], {'done': 1})

    def test_pause_between_items_leaves_remaining_members_pending(self):
        with TemporaryDirectory() as directory:
            _, ledger, runtime, _, app = replay_fixture(directory)
            add_message(ledger, app, 'second')
            replay = UnderstandingReplay(ledger, runtime)
            job = replay.start('account', MutationContext('start', 'user', 'test'))
            process = runtime.process
            def pause_after(*args, **kwargs):
                result = process(*args, **kwargs)
                runtime.mode = 'paused'
                return result
            runtime.process = pause_after
            result = replay.run_batch(job['replay_id'])
            self.assertEqual(result['counts'], {'done': 1, 'pending': 1})
            self.assertEqual(len(runtime.calls), 1)
            with self.assertRaises(ContractError): replay.run_batch(job['replay_id'])


if __name__ == '__main__':
    unittest.main()
