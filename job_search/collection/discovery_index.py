"""An additive, covering date index for read-only discovery-window consumers.

Date strings remain untouched. Extended ISO dates can be narrowed by local day;
other representations stay in an explicit fallback bucket for exact Python parsing.
"""
from contextlib import closing
from datetime import date, timedelta
import sqlite3


DATE_KEY = """CASE WHEN first_seen GLOB
    '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'
    THEN substr(first_seen,1,10) ELSE '' END"""
INDEX_SQL = f"""CREATE INDEX IF NOT EXISTS jobs_discovery_candidates
    ON jobs(({DATE_KEY}), ats, id, posted_at, first_seen, closed_at);"""
CANDIDATE_SQL = f"""SELECT ats,id,posted_at,first_seen,closed_at FROM jobs
    WHERE ({DATE_KEY}) BETWEEN ? AND ?
    UNION ALL
    SELECT ats,id,posted_at,first_seen,closed_at FROM jobs
    WHERE ({DATE_KEY}) = ''"""


def candidate_bounds(start, end):
    # ISO offsets are strictly less than 24 hours. Expand both UTC boundary days
    # so local dates on either side cannot be dropped. Exact microsecond and age
    # checks remain the caller's responsibility; SQLite date functions round and
    # do not support every representation accepted by datetime.fromisoformat.
    lower, upper = start.date(), end.date()
    if lower > date.min:
        lower -= timedelta(days=1)
    if upper < date.max:
        upper += timedelta(days=1)
    return lower.isoformat(), upper.isoformat()


def prepare_existing(path):
    """Install during writable initialization, never during review/health reads.

    Fresh or pre-provenance catalogs are left to the collector's schema migration.
    An index build on existing data is paid once, inside release maintenance.
    """
    if not path.is_file():
        return
    with closing(sqlite3.connect(path, timeout=60)) as con, con:
        columns = {r[1] for r in con.execute('PRAGMA table_info(jobs)')}
        if {'ats', 'id', 'posted_at', 'first_seen', 'closed_at'} <= columns:
            con.execute(INDEX_SQL)
