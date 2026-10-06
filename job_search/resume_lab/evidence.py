"""Read persisted review evidence without opening PDF or mutation services."""
import json
import os
import sqlite3
import stat
import subprocess
import sys
from contextlib import closing
from pathlib import Path

def read_review_evidence(db_path):
    """Return approved career content and active resume text from one snapshot.

    The host review coordinator may run under a different UID from the application
    that owns the PDFs. It needs database evidence only, and must neither initialize
    the artifact repository nor migrate or repair the application's private state.
    """
    from .contracts import ResumeBoundaryError
    path = Path(db_path).expanduser().absolute()
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ResumeBoundaryError('resume-lab database must be a regular file')
    if os.geteuid() != info.st_uid:
        if os.geteuid() != 0:
            raise ResumeBoundaryError('review evidence requires the database owner')
        # Even mode=ro may create WAL/SHM sidecars. Only this bounded text reader
        # runs as the database owner; the multithreaded coordinator never changes
        # identity. Pass trusted source on stdin because the owner may not traverse
        # the root-only host installation. No application imports are needed.
        result = subprocess.run(
            [sys.executable, '-I', '-', str(path)],
            input=Path(__file__).read_text(encoding='utf-8'), text=True,
            capture_output=True, timeout=30, check=False, cwd='/',
            user=info.st_uid, group=info.st_gid, extra_groups=(), umask=0o077,
            env={},
        )
        if result.returncode:
            raise ResumeBoundaryError('approved review evidence could not be read')
        return json.loads(result.stdout)
    return _read_snapshot(path)


def _read_snapshot(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
        raise ValueError('review evidence requires a regular database owned by the reader')
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as con:
        con.row_factory = sqlite3.Row
        con.execute('BEGIN')
        career = {'approved': None, 'draft_revision_id': None}
        # Resume-only installations may predate the optional career fact bank.
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='career_profile_state'").fetchone():
            state = con.execute('SELECT draft_revision_id, approved_revision_id FROM career_profile_state WHERE singleton=1').fetchone()
            if state:
                career['draft_revision_id'] = state['draft_revision_id']
                if state['approved_revision_id']:
                    approved = con.execute(
                        'SELECT revision_id, content_json FROM career_profile_revisions WHERE revision_id=?',
                        (state['approved_revision_id'],),
                    ).fetchone()
                    if approved is None:
                        raise ValueError('approved career revision is unavailable')
                    career['approved'] = {'revision_id': approved['revision_id'],
                                          'content': json.loads(approved['content_json'])}
        versions = [dict(row) for row in con.execute(
            'SELECT v.version_id, v.plain_text FROM resume_standards s '
            'LEFT JOIN resume_standard_versions v ON v.version_id=s.active_version_id '
            'WHERE s.active=1 AND s.active_version_id IS NOT NULL ORDER BY s.manual_rank, s.standard_id'
        )]
        if any(version['version_id'] is None for version in versions):
            raise ValueError('active resume version is unavailable')
    return career, versions


if __name__ == '__main__':
    print(json.dumps(_read_snapshot(Path(sys.argv[1]))))
