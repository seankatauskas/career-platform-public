"""Fictional persisted state exercised by both release images, with no network."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

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


def encoded(value):
    return {'blob': value.hex()} if isinstance(value, bytes) else value


def snapshot(root):
    result = {'databases': {}, 'files': {}}
    for path in sorted((root / 'data').rglob('*')):
        if not path.is_file() or path.name.endswith(('-wal', '-shm')):
            continue
        name = str(path.relative_to(root))
        if path.suffix != '.db':
            result['files'][name] = digest(path)
            continue
        with sqlite3.connect(path) as con:
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


def assert_preserved(root, before):
    after = snapshot(root)
    for name, expected in before['files'].items():
        assert after['files'].get(name) == expected, 'artifact/config changed: ' + name
    for name, tables in before['databases'].items():
        for table, expected in tables.items():
            found = after['databases'][name][table]
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


def main():
    action, path = sys.argv[1:]
    root = Path(path)
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
        assert_preserved(root, json.loads(expected.read_text()))
        archive = EncryptedMailArchive(ledger, PortableArchiveKeyProvider(root / 'fixture-key'))
        assert archive.read_message((root / 'mail-id').read_text()) == 'Fictional recruiter message'
        if action == 'upgrade':
            start(ledger, job_id='after-upgrade', key='after-upgrade')
            create_standard(resumes, name='After upgrade', rank=2, marker='two')
            install_ranking_trackers(config)
            (root / 'after.json').write_text(json.dumps(snapshot(root)))
        elif action == 'rollback':
            verify_predecessor_ranking_writes(config)
    print(json.dumps({'passed': True, 'action': action, 'state_sha256': hashlib.sha256(json.dumps(snapshot(root), sort_keys=True).encode()).hexdigest()}))


if __name__ == '__main__':
    main()
