"""Offline metadata-only email matching against the jobs catalog."""
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

from job_search.contracts import ContractError
from job_search.integration import LocalJobCatalog
from job_search.mail.context import CandidateApplication
from job_search.mail.identity import review_supported_candidates, supported_candidates


def catalog(directory, jobs):
    path = Path(directory) / 'jobs.db'
    with sqlite3.connect(path) as con:
        con.executescript('''
            CREATE TABLE jobs (ats TEXT, id TEXT, company TEXT, title TEXT,
                jobUrl TEXT, description TEXT, description_html TEXT,
                closed_at TEXT, PRIMARY KEY (ats,id));
            CREATE INDEX jobs_company ON jobs(ats,company);
        ''')
        con.executemany('INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?)', [
            (ats, identity, company, title, 'https://example.test/' + identity,
             'Private description text', '<p>Private description HTML</p>', closed)
            for ats, identity, company, title, closed in jobs
        ])
    return LocalJobCatalog(path)


def test_full_body_names_slug_and_role_including_closed_job():
    with tempfile.TemporaryDirectory() as directory:
        source = catalog(directory, [
            ('ashby', 'platform-001', 'exampleco', 'Platform Engineer', '2026-09-01'),
            ('ashby', 'designer-002', 'exampleco', 'Product Designer', None),
            ('lever', 'platform-003', 'Other Co', 'Platform Engineer', None),
        ])
        rows = source.review_candidates('Application update', 'Background. ' * 500 +
            '\nYour application to Example Co. Platform Engineer update.')
        assert [row['id'] for row in rows] == ['platform-001', 'designer-002']
        assert rows[0]['match_unique'] and rows[0]['match_confidence'] == 'high'
        assert rows[0]['closed_at'] == '2026-09-01'
        assert 'role title in message' in rows[0]['match_reason']
        assert rows[0]['jobUrl'] == 'https://example.test/platform-001'


def test_role_alone_never_selects_unrelated_employers():
    with tempfile.TemporaryDirectory() as directory:
        source = catalog(directory, [('ashby', 'platform-001', 'Example Co', 'Platform Engineer', None)])
        assert source.review_candidates('Platform Engineer', 'Thank you for your interest.') == ()
        assert source.review_candidates('New Example Company', 'Platform Engineer') == ()


def test_many_similar_roles_disambiguate_once_for_the_whole_result_set():
    with tempfile.TemporaryDirectory() as directory:
        source = catalog(directory, [
            ('ashby', f'platform-{number:03}', 'Example Co', 'Platform Engineer', None)
            for number in range(100)
        ])
        # A long email with many equally plausible roles previously caused one
        # full identity pass per result row, repeatedly scanning the same body.
        body = 'Background information. ' * 200 + '\nExample Co\nPlatform Engineer'
        with patch('job_search.mail.identity.supported_candidates', wraps=supported_candidates) as disambiguate:
            rows = source.review_candidates('Application update', body)
        assert len(rows) == 20 and not any(row['match_unique'] for row in rows)
        assert disambiguate.call_count == 1


def test_posting_id_lookup_and_employer_conflict():
    with tempfile.TemporaryDirectory() as directory:
        source = catalog(directory, [
            ('ashby', 'ref-1234', 'Example Co', 'Engineer', None),
            ('lever', 'ref-5678', 'Other Co', 'Engineer', None),
        ])
        result = source.review_candidates('Regarding REF-1234', 'Recruiting update.')
        assert len(result) == 1 and result[0]['id'] == 'ref-1234'
        assert result[0]['match_unique']
        conflict = source.review_candidates('Your application to Other Co.', 'Reference ref-1234')
        assert [row['id'] for row in conflict] == ['ref-5678']


def test_matching_reads_metadata_only_and_preserves_database():
    with tempfile.TemporaryDirectory() as directory:
        source = catalog(directory, [('ashby', 'platform-001', 'Example Co', 'Platform Engineer', None)])
        before = source.jobs_db.read_bytes()
        connect = sqlite3.connect
        reads = []

        def readonly_connection(*args, **kwargs):
            con = connect(*args, **kwargs)
            def authorize(action, first, second, *_):
                if action == sqlite3.SQLITE_READ:
                    reads.append((first, second))
                    if second in ('description', 'description_html'):
                        return sqlite3.SQLITE_DENY
                if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            con.set_authorizer(authorize)
            return con

        with patch('job_search.integration.sqlite3.connect', side_effect=readonly_connection):
            rows = source.review_candidates('Example Co', 'Platform Engineer')
        assert len(rows) == 1
        assert 'description' not in rows[0] and 'description_html' not in rows[0]
        assert ('jobs', 'company') in reads
        assert source.jobs_db.read_bytes() == before


def test_bounded_result_prioritizes_role_before_board_limit_and_marks_truncation():
    with tempfile.TemporaryDirectory() as directory:
        jobs = [('ashby', f'job-{i:04}', 'Example Co', 'Other role', None) for i in range(125)]
        jobs.append(('ashby', 'zzz-target', 'Example Co', 'Platform Engineer', None))
        source = catalog(directory, jobs)
        rows = source.review_candidates('Example Co recruiting update', 'Platform Engineer', limit=2)
        assert len(rows) == 2 and rows[0]['id'] == 'zzz-target'
        assert rows[0]['candidates_truncated']
        assert not rows[0]['match_unique']


def test_same_company_without_role_remains_ambiguous():
    with tempfile.TemporaryDirectory() as directory:
        source = catalog(directory, [
            ('ashby', 'engineer-01', 'Example Co', 'Engineer', None),
            ('ashby', 'designer-02', 'Example Co', 'Designer', None),
        ])
        rows = source.review_candidates('Your application to Example Co.', 'Recruiting update.')
        assert len(rows) == 2 and not any(row['match_unique'] for row in rows)
        assert all(row['match_confidence'] == 'medium' for row in rows)


def test_review_role_variants_keep_employer_identity_and_automatic_gates_strict():
    with tempfile.TemporaryDirectory() as directory:
        source = catalog(directory, [
            ('ashby', 'platform-01', 'Example Co', 'Platform Engineer', None),
            ('ashby', 'designer-02', 'Example Co', 'Product Designer', None),
            ('lever', 'platform-03', 'Other Co', 'Platform Engineer', None),
        ])
        message = 'Your application for the Senior Platform Engineer role at Example Co.'
        rows = source.review_candidates('Application update', message)
        assert [row['id'] for row in rows] == ['platform-01']
        assert rows[0]['match_confidence'] == 'medium'
        assert not rows[0]['match_unique']
        candidates = [CandidateApplication('app-1', 'ashby', 'platform-01', 'Example Co', 'Platform Engineer'),
                      CandidateApplication('app-2', 'ashby', 'designer-02', 'Example Co', 'Product Designer'),
                      CandidateApplication('app-3', 'lever', 'platform-03', 'Other Co', 'Platform Engineer')]
        assert supported_candidates(candidates, '', message) == ()
        assert review_supported_candidates(candidates, '', message) == (candidates[0],)
        assert review_supported_candidates(candidates, '',
            'Your application for the Senior Platform Engineer role.') == ()
        assert review_supported_candidates(candidates, '',
            'Your application for the Platform Software Engineer role at Example Co.') == (candidates[0],)
        assert review_supported_candidates(candidates, '',
            'Your application for the Data Engineer role at Example Co.') == ()
        assert review_supported_candidates(candidates, '',
            'Your application for the Platform Engineer II remote role at Example Co.') == (candidates[0],)


def test_missing_empty_and_invalid_catalog_inputs():
    with tempfile.TemporaryDirectory() as directory:
        source = LocalJobCatalog(Path(directory) / 'missing.db')
        assert source.review_candidates('Example Co', 'Engineer') == ()
        assert not source.jobs_db.exists()
        for limit in (0, 51, True, '2'):
            try:
                source.review_candidates('Example Co', 'Engineer', limit=limit)
            except ContractError:
                pass
            else:
                raise AssertionError('Invalid candidate limit accepted')
        source = catalog(directory, [])
        assert source.review_candidates('', '') == ()
        assert source.review_candidates('Example Co', 'Engineer') == ()


if __name__ == '__main__':
    tests = [value for name, value in globals().copy().items() if name.startswith('test_') and callable(value)]
    for test in tests:
        test()
    print(f'ok ({len(tests)} review catalog tests)')
