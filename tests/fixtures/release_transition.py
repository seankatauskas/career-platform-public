"""Fictional persisted state exercised by both release images, with no network."""
from datetime import datetime, timezone
from contextlib import closing, redirect_stdout
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import types

from job_search.collection import boards
from job_search.collection.dedupe import prepare_families
from job_search.ranking.labeler import prepare_preferences
from job_search.ranking.model import prepare_state
from job_search.ranking.proxy import prepare_schema
from job_search.resume_lab.service import ResumeLabService
from job_search.resume_lab.career_store import CareerStore
from job_search.runtime import load_runtime_config
from job_search.service import JobSearchLedger
from job_search.mail.archive import EncryptedMailArchive
from job_search.secure_persistence import PortableArchiveKeyProvider, initialize_portable_master_key
from job_search.contracts import MutationContext
from tests.test_job_search_ledger import start
from tests.test_resume_lab_core import create_standard
from tests.test_job_search_automation import enqueue_work


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def initialize(root):
    config = load_runtime_config(root / 'config.json', required=True)
    ledger = JobSearchLedger(config.application_db)
    with sqlite3.connect(config.jobs_db) as con:
        boards._prepare(con)
    prepare_families(config.jobs_db)
    prepare_preferences(config.jobs_db)
    prepare_state(config.preference_db)
    prepare_schema(config.proxy_db)
    resumes = ResumeLabService(config.resume_lab_db)
    CareerStore(config.resume_lab_db)
    return config, ledger, resumes


def startup_probe(action, root):
    """Exercise real CLI migration/check commands and worker constructors offline."""
    from job_search.cli import main as cli
    from job_search.runtime import build_runtime
    from job_search.schema_lifecycle import STARTUP_POLICY
    config = load_runtime_config(root / 'config.json', required=True)
    def command(name):
        with redirect_stdout(io.StringIO()):
            assert cli(['--config', str(root / 'config.json'), name]) == 0
    if action == 'startup-prepare':
        command('migrate')
        # Bootstrap optional subsystem state once, as on an installed predecessor.
        for lane in ('core', 'model'):
            build_runtime(config, lane=lane, base_environment={})
    before = startup_snapshot(root)
    original = os.environ.get('JOB_SEARCH_SCHEMA_MODE')
    os.environ['JOB_SEARCH_SCHEMA_MODE'] = 'require_current'
    try:
        command('check-schema')
        for lane in ('core', 'model'):
            build_runtime(config, lane=lane, base_environment={})
        assert startup_snapshot(root) == before, 'production startup changed persisted state'
        ledger = JobSearchLedger(config.application_db)
        if action == 'startup-write':
            start(ledger, job_id='strict-startup-write', key='strict-startup-write')
            (root / 'startup-after.json').write_text(json.dumps(snapshot(root)))
        elif action == 'startup-rollback':
            assert_preserved(root, json.loads((root / 'startup-after.json').read_text()))
            start(ledger, job_id='strict-rollback-write', key='strict-rollback-write')
    finally:
        if original is None:
            os.environ.pop('JOB_SEARCH_SCHEMA_MODE', None)
        else:
            os.environ['JOB_SEARCH_SCHEMA_MODE'] = original
    return {'passed': True, 'action': action, 'startup_schema_policy': STARTUP_POLICY,
            'startup_read_only': True}



def diagnostics_probe(action, root):
    """Exercise reviewed diagnostics against legacy and converted owner state.

    Check both ordinary snapshot-backed requests and explicit ranking inspection;
    supply fictional dependency observations so this never starts provider work.
    Full source review must still cover unexercised inputs and new call paths.
    """
    from dataclasses import replace
    from job_search.runtime_readiness import runtime_readiness
    config = load_runtime_config(root / 'config.json', required=True)
    if action == 'diagnostics-owners-check':
        from job_search.application_migration import CANDIDATE_NAME
        config = replace(config, application_backend='owners',
                         application_owner_db=root / 'data' / 'application-owners' / CANDIDATE_NAME)
    from unittest.mock import patch
    with patch.dict(os.environ, {'JOB_SEARCH_SCHEMA_MODE': 'require_current'}):
        before = startup_snapshot(root)
        reports = []
        from job_search.schema_lifecycle import check_config
        from job_search.runtime import build_runtime
        check_config(config)
        for lane in ('core', 'model'):
            # Advancing time makes any accidental timestamp refresh observable even
            # when fixture setup and startup happen within the same second.
            build_runtime(config, lane=lane, base_environment={},
                          now_provider=lambda: datetime(2035, 1, 1, tzinfo=timezone.utc))
        after = startup_snapshot(root)
        changed = [name + '/' + table for name, tables in before['databases'].items()
                   for table, rows in tables.items() if after['databases'].get(name, {}).get(table) != rows]
        changed += [group + '/' + name for group in ('files', 'schemas')
                    for name, value in before[group].items() if after[group].get(name) != value]
        assert after == before, 'diagnostics startup changed persisted state: ' + ','.join(changed)
        for detailed in (False, True):
            for automation in (False, True):
                for observations in ('provided', 'snapshot', 'local'):
                    snapshot_backed = observations == 'snapshot'
                    report = runtime_readiness(config, dependencies={} if observations == 'provided' else None,
                        use_snapshot=snapshot_backed, include_ranking_details=detailed,
                        automation_enabled=automation)
                    assert isinstance(report['capabilities'], list)
                    assert report['external_services_verified'] is False
                    if config.application_backend == 'owners':
                        assert report['application_owners'].get('available') is not False
                    assert startup_snapshot(root) == before, 'diagnostics changed persisted state'
                    reports.append({'detailed': detailed, 'automation': automation,
                                    'observations': observations})
    return {'passed': True, 'action': action, 'diagnostics_read_only': True,
            'backend': config.application_backend, 'scenarios': reports}

def encoded(value):
    return {'blob': value.hex()} if isinstance(value, bytes) else value


def snapshot(root):
    result = {'databases': {}, 'files': {}}
    for path in sorted((root / 'data').rglob('*')):
        if not path.is_file() or path.name.endswith(('-wal', '-shm')):
            continue
        name = str(path.relative_to(root))
        if path.suffix not in {'.db', '.sqlite'}:
            result['files'][name] = digest(path)
            continue
        with closing(sqlite3.connect(path)) as con:
            assert con.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
            tables = {}
            for (table,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
                quoted = '"' + table.replace('"', '""') + '"'
                columns = [row[1] for row in con.execute('PRAGMA table_info(' + quoted + ')')]
                rows = [[encoded(v) for v in row] for row in con.execute('SELECT * FROM ' + quoted)]
                tables[table] = {'columns': columns, 'rows': rows}
            result['databases'][name] = tables
    result['files']['config.json'] = digest(root / 'config.json')
    return result


def startup_snapshot(root):
    """Include schema objects/version, which ordinary row-preservation omits."""
    result = snapshot(root)
    result['schemas'] = {}
    for relative in result['databases']:
        con = sqlite3.connect((root / relative).resolve().as_uri() + '?mode=ro', uri=True)
        try:
            result['schemas'][relative] = {
                'user_version': con.execute('PRAGMA user_version').fetchone()[0],
                'objects': con.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name').fetchall(),
            }
        finally:
            con.close()
    return result


def preparation_reseal(root, before, after):
    """Recognize a complete, validated checkpoint publication by a seal-aware image."""
    prior = before['databases'].get('data/jobs.db', {})
    observed = after['databases'].get('data/jobs.db', {})
    if 'family_preparation_state' not in prior:
        return False
    previous, current = prior['family_preparation_state'], observed['family_preparation_state']
    assert len(previous['rows']) == len(current['rows']) == 1
    old = dict(zip(previous['columns'], previous['rows'][0]))
    new = dict(zip(current['columns'], current['rows'][0]))
    if all(new[key] == old[key] for key in ('sealed_revision', 'prepared_source_revision', 'signature')):
        return False
    for key in ('id', 'source_revision'):
        assert new[key] == old[key], 'checkpoint identity/source changed: ' + key
    for key in ('revision', 'sealed_revision', 'source_revision', 'prepared_source_revision'):
        assert type(old[key]) is int and type(new[key]) is int
        assert new[key] >= old[key] >= 0, 'checkpoint moved backwards: ' + key
    assert new['revision'] == new['sealed_revision'], 'preparation was not sealed'
    assert new['prepared_source_revision'] == new['source_revision'], 'source journal was not consumed'
    assert old['sealed_revision'] <= old['revision']
    assert old['prepared_source_revision'] <= old['source_revision']
    dirty = prior['family_preparation_dirty']
    revisions = [row[dirty['columns'].index('revision')] for row in dirty['rows']]
    assert all(type(value) is int for value in revisions)
    assert len(set(revisions)) == len(revisions) == old['source_revision'] - old['prepared_source_revision']
    assert not revisions or (min(revisions) == old['prepared_source_revision'] + 1
                             and max(revisions) == old['source_revision']), 'prior source journal has a gap'
    from job_search.collection.dedupe import _verify_all_families
    from job_search.collection.dedupe_incremental import current as preparation_current, signature
    assert new['signature'] == signature(), 'prepared snapshot signature is not trustworthy/current'
    with sqlite3.connect(root / 'data/jobs.db') as con:
        assert preparation_current(con), 'prepared checkpoint is not trustworthy/current'
        assert _verify_all_families(con), 'prepared family state differs from raw source'
    return True


def assert_preserved(root, before, *, allow_preparation=False):
    after = snapshot(root)
    resealed = allow_preparation and preparation_reseal(root, before, after)
    for name, expected in before['files'].items():
        assert after['files'].get(name) == expected, 'artifact/config changed: ' + name
    for name, tables in before['databases'].items():
        for table, expected in tables.items():
            found = after['databases'][name][table]
            if name == 'data/jobs.db' and table == 'family_preparation_state':
                # Legacy preparation advances only the derived counter. A newer
                # predecessor may also publish a validated seal and consume its
                # journal; neither case changes checkpoint identity/source input.
                assert len(expected['rows']) == len(found['rows']) == 1
                revision = expected['columns'].index('revision')
                observed_revision = found['rows'][0][found['columns'].index('revision')]
                prior_revision = expected['rows'][0][revision]
                assert type(observed_revision) is int and type(prior_revision) is int
                assert observed_revision >= prior_revision, 'preparation revision moved backwards'
                found = {'columns': found['columns'], 'rows': [list(found['rows'][0])]}
                found['rows'][0][found['columns'].index('revision')] = prior_revision
                if resealed:
                    for column in ('sealed_revision', 'prepared_source_revision', 'signature'):
                        found['rows'][0][found['columns'].index(column)] = expected['rows'][0][expected['columns'].index(column)]
            if name == 'data/jobs.db' and table == 'family_preparation_dirty' and resealed:
                # Only source events covered by the newly verified checkpoint
                # may disappear. Every other row/table retains exact checks.
                state = after['databases'][name]['family_preparation_state']
                covered = state['rows'][0][state['columns'].index('prepared_source_revision')]
                revision = expected['columns'].index('revision')
                expected = {**expected, 'rows': [row for row in expected['rows'] if row[revision] > covered]}
            indexes = [found['columns'].index(column) for column in expected['columns']]
            actual = [json.dumps([row[i] for i in indexes], sort_keys=True) for row in found['rows']]
            for row in expected['rows']:
                key = json.dumps(row, sort_keys=True)
                assert key in actual, 'persisted row lost: ' + name + '/' + table
                actual.remove(key)
    return after


def install_ranking_trackers(config):
    """Candidate-only setup; the predecessor must keep writing these databases."""
    from job_search.ranking.refresh_freshness import ensure_tracking
    boards.save([{'ats': 'ashby', 'id': 'transition-ranking', 'company': 'Fixture',
                  'title': 'Engineer', 'description': 'Fictional ranking fixture'}],
                config.jobs_db, '2026-10-03T00:00:00+00:00')
    with sqlite3.connect(config.preference_db) as con:
        con.execute('INSERT INTO preference_model_runs VALUES (?,?,?,?,?,?,?)',
                    ('transition-ranking', '2026-10-03', 'fixture', 'fixture', 1, '{}', ''))
        con.execute('INSERT INTO preference_scores VALUES (?,?,?,?,?,?,?,?,?)',
                    ('transition-ranking', 'fixture-family', 'fixture-fingerprint',
                     0, 0, .5, .5, '{}', '2026-10-03'))
    ensure_tracking(config.jobs_db, 'source')
    ensure_tracking(config.preference_db, 'scores')


def install_owner_resolution_receipt(config):
    """The predecessor must preserve a nonempty candidate-owned audit table."""
    from job_search.recovery import RecoveryService
    from job_search.db import connect
    reference = {'owner':'correspondence','kind':'understand_message','key':'fixture-revision',
                 'work_id':'fixture-owner-work'}
    enqueue_work(config.application_db, 'transition-owner-recovery', 'applications.mail.understand',
                 status='dead', attempts=1)
    with connect(config.application_db) as con:
        con.execute('UPDATE work_items SET payload_json=? WHERE work_id=?',
                    (json.dumps(reference), 'transition-owner-recovery'))
    proof = {'owner_work_id':reference['work_id'],'owner':reference['owner'],'kind':reference['kind'],
             'key':reference['key'],'reason':'owner_resolved','source_id':'fixture-source',
             'revision':'fixture-revision','issue_id':'fixture-issue','issue_version':3,'analysis_id':'fixture-analysis'}
    result = RecoveryService(config.application_db).resolve_owner_mail_work('transition-owner-recovery',
        expected_revision=0, expected_payload=reference, proof=proof, now='2026-10-10T12:00:00Z')
    assert result['status'] == 'cancelled'
    with sqlite3.connect(config.application_db) as con:
        assert con.execute('SELECT COUNT(*) FROM work_owner_resolutions').fetchone()[0] == 1
        assert con.execute('PRAGMA user_version').fetchone()[0] == 24


def verify_predecessor_ranking_writes(config):
    """Execute the old collector and its score SQL shape with new triggers installed."""
    def tracker(path, kind, scope):
        with sqlite3.connect(path) as con:
            epoch = con.execute(f'SELECT value FROM ranking_refresh_{kind}_epoch WHERE id=1').fetchone()[0]
            row = con.execute(f'SELECT revision FROM ranking_refresh_{kind}_revisions WHERE scope=?',
                              (scope,)).fetchone()
        return epoch, row[0] if row else 0

    source_before = tracker(config.jobs_db, 'source', 'catalog')
    scores_before = tracker(config.preference_db, 'scores', 'transition-ranking')
    boards.save([{'ats': 'ashby', 'id': 'transition-ranking', 'company': 'Fixture',
                  'title': 'Updated Engineer', 'description': 'Updated fictional ranking fixture'}],
                config.jobs_db, '2026-10-03T01:00:00+00:00')
    with sqlite3.connect(config.preference_db) as con:
        con.execute('UPDATE preference_scores SET sparse_score=?,final_score=? WHERE run_id=?',
                    (.6, .6, 'transition-ranking'))
    source_after = tracker(config.jobs_db, 'source', 'catalog')
    scores_after = tracker(config.preference_db, 'scores', 'transition-ranking')
    assert source_before[0] == source_after[0] and source_after[1] > source_before[1]
    assert scores_before[0] == scores_after[0] and scores_after[1] > scores_before[1]


def verify_ranking_reupgrade(root):
    """Repair legacy seals or consume supported predecessor journals without data loss."""
    from job_search.collection.dedupe import prepared_families_are_current, _verify_all_families
    from job_search.collection.dedupe_incremental import DERIVED, trusted
    config = load_runtime_config(root / 'config.json', required=True)
    before = snapshot(root)
    with sqlite3.connect(config.jobs_db) as con:
        journal_trusted = trusted(con)
    assert not prepared_families_are_current(config.jobs_db)
    result = prepare_families(config.jobs_db, incremental=True)
    assert result['incremental'] is journal_trusted, 'predecessor trust state selected the wrong repair path'
    assert prepared_families_are_current(config.jobs_db)
    with sqlite3.connect(config.jobs_db) as con:
        assert _verify_all_families(con), 'repaired families differ from raw source fingerprints'
    # These are the exact recomputable tables changed by preparation. All raw
    # jobs, preferences, score rows, model records, tracking epochs, application
    # data, and files retain the same strict preservation checks as rollback.
    rebuilt = {*DERIVED, 'family_preparation_state', 'family_preparation_dirty',
               'family_preparation_blocks', 'ranking_refresh_source_revisions',
               'ranking_refresh_source_events'}
    preserved = {'files': before['files'], 'databases': {
        name: {table: values for table, values in tables.items()
               if name != 'data/jobs.db' or table not in rebuilt}
        for name, tables in before['databases'].items()}}
    repaired = assert_preserved(root, preserved)
    prepare_families(config.jobs_db)
    full = assert_preserved(root, preserved)
    assert {table: repaired['databases']['data/jobs.db'][table] for table in DERIVED} == {
        table: full['databases']['data/jobs.db'][table] for table in DERIVED
    }, 'repaired metadata differs from a subsequent full preparation'
    return {'action': 'ranking_reupgrade', 'passed': True, 'predecessor_seal_invalidated': not journal_trusted,
            'repair_mode': 'incremental' if journal_trusted else 'full',
            'repair_verified': True, 'full_pass_parity': True, 'substantive_state_preserved': True}


def verify_application_owners(root):
    from dataclasses import replace
    from unittest import TestResult
    from tests.test_redesign_commands import CommandsTest
    from job_search.application_migration import convert_snapshot, CANDIDATE_NAME
    from job_search.application_runtime import ApplicationRuntime
    from job_search.application_installation import freeze_legacy, LEGACY_TASKS
    from job_search.runtime import build_runtime
    from job_search.commands import CommandContext, Principal
    # Exercise the candidate image's SQLite build; host-only tests do not expose
    # every build's lazy virtual-table initialization under the owner authorizer.
    result=TestResult()
    CommandsTest('test_json_table_reads_work_without_relaxing_owner_or_schema_authorization').run(result)
    assert result.testsRun == 1 and result.wasSuccessful() and not result.skipped, (result.errors,result.failures)
    config,ledger,_=initialize(root)
    destination=root/'data'/'application-owners'
    report=convert_snapshot(config.application_db,destination)
    assert report['ready_for_review'], report['issues']
    owner=ApplicationRuntime(destination/CANDIDATE_NAME)
    freeze_legacy(config.application_db,owner,operator='transition-test',report=report)
    selected=replace(config,application_backend='owners',application_owner_db=owner.executor.path)
    from job_search.schema_lifecycle import migrate_config
    migrate_config(selected)
    production=build_runtime(selected,base_environment={})
    assert not (set(production.worker.task_handlers)&LEGACY_TASKS)
    apps=owner.queries.list_applications()['items']
    assert apps
    app_id=apps[0]['id']
    context=CommandContext(Principal('transition-human','human',frozenset({'*'})),'transition-note')
    owner.command(context,'add_note',{'application_id':app_id,'text':'After owner transition'})
    try:
        ledger.record_submission(app_id,'2026-10-09T00:00:00Z',MutationContext('forbidden-old','user','transition'))
    except sqlite3.DatabaseError:
        pass
    else:
        raise AssertionError('predecessor application write was not fenced')
    owner.executor.set_activation(paused=False,expected_revision=0,operator='fixture',reason='restore test')
    stage=root/'restored'
    # Recovery runs on the host in production. Load only that package from the
    # reviewed fixture mount, without extending the image's job_search package
    # and accidentally hiding missing runtime modules in the container.
    host_package=types.ModuleType('_release_host')
    host_package.__path__=[str(Path(__file__).resolve().parents[2]/'job_search')]
    sys.modules['_release_host']=host_package
    host_ops=importlib.import_module('_release_host.aws_ops')
    host_ops.copy_snapshot(destination,stage/'state'/'application-owners')
    host_ops.prepare_application_restore(stage)
    restored=ApplicationRuntime(stage/'state'/'application-owners'/CANDIDATE_NAME)
    assert restored.executor.activation_status()['paused']
    assert restored.executor.restore_status()['required']
    assert restored.queries.workspace(app_id)['records']['notes']['items']
    assert (destination/'predecessor.sqlite').read_bytes()==(stage/'state'/'application-owners'/'predecessor.sqlite').read_bytes()
    return {'action':'application_owners','passed':True,'legacy_writes_fenced':True,
            'conversion_blockers':len(report['issues']),'restore_paused':True,
            'restore_review_required':True,'source_archive_preserved':True,
            'json_table_authorization_verified':True}


def main():
    action, path = sys.argv[1:]
    root = Path(path)
    if action.startswith('diagnostics-'):
        print(json.dumps(diagnostics_probe(action, root)))
        return
    if action.startswith('startup-'):
        print(json.dumps(startup_probe(action, root)))
        return
    if action == 'ranking_reupgrade':
        print(json.dumps(verify_ranking_reupgrade(root)))
        return
    if action == 'owners':
        print(json.dumps(verify_application_owners(root)))
        return
    if action == 'seed':
        data = root / 'data'; data.mkdir(parents=True)
        config = {'version': 1, 'project_root': str(Path.cwd()), 'timezone': 'America/Chicago',
                  **{key: str(data / name) for key, name in [('application_db','applications.db'), ('jobs_db','jobs.db'),
                     ('preference_db','preferences.db'), ('proxy_db','proxy.db'), ('resume_lab_db','resumes.db')]},
                  'resume_artifact_root': str(data / 'artifacts'), 'shortlist_notifications_enabled': False,
                  'remote_mail_inference_enabled': False, 'scraper_contact': ''}
        (root / 'config.json').write_text(json.dumps(config))
        (root / 'config.json').chmod(0o600)
        config, ledger, resumes = initialize(root)
        app = start(ledger)
        standard = create_standard(resumes)
        enqueue_work(config.application_db, 'transition-work', 'system.worker_tick', due=datetime.now(timezone.utc))
        key = initialize_portable_master_key(root / 'fixture-key')
        archive = EncryptedMailArchive(ledger, PortableArchiveKeyProvider(key))
        saved = archive.archive_message(account_id='fixture', immutable_message_id='fixture-message', sanitized_text='Fictional recruiter message', truncated=False, context=MutationContext('fixture-mail','system','mail'))
        (root / 'mail-id').write_text(saved['archive']['archive_id'])
        artifacts = data / 'artifacts'; artifacts.mkdir()
        (artifacts / 'resume.pdf').write_bytes(b'%PDF-1.4\nFictional immutable resume fixture\n%%EOF\n')
        (data / 'model-manifest.json').write_text(json.dumps({'run_id': 'fixture-ranker', 'weights': 'fixture-weights.bin'}))
        (data / 'fixture-weights.bin').write_bytes(b'fictional model bytes')
        (data / 'hermes').mkdir()
        with sqlite3.connect(data / 'hermes' / 'history.db') as con:
            con.execute('CREATE TABLE messages (id INTEGER PRIMARY KEY, body TEXT)')
            con.execute("INSERT INTO messages VALUES (1, 'Fictional conversation')")
        (root / 'before.json').write_text(json.dumps(snapshot(root)))
    else:
        config, ledger, resumes = initialize(root)
        expected = root / ('after.json' if action == 'rollback' else 'before.json')
        assert_preserved(root, json.loads(expected.read_text()), allow_preparation=action in {'upgrade', 'rollback'})
        archive = EncryptedMailArchive(ledger, PortableArchiveKeyProvider(root / 'fixture-key'))
        assert archive.read_message((root / 'mail-id').read_text()) == 'Fictional recruiter message'
        if action == 'upgrade':
            start(ledger, job_id='after-upgrade', key='after-upgrade')
            create_standard(resumes, name='After upgrade', rank=2, marker='two')
            install_ranking_trackers(config)
            install_owner_resolution_receipt(config)
            (root / 'after.json').write_text(json.dumps(snapshot(root)))
        elif action == 'rollback':
            start(ledger, job_id='after-rollback', key='after-rollback')
            verify_predecessor_ranking_writes(config)
    print(json.dumps({'passed': True, 'action': action, 'state_sha256': hashlib.sha256(json.dumps(snapshot(root), sort_keys=True).encode()).hexdigest()}))


if __name__ == '__main__':
    main()
