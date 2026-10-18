"""Transactional source journal and indexed blocks for family preparation.

SQL triggers cover arbitrary writers. A changed tracker, derived table mutation,
normalizer, or implementation invalidates the seal and requires a full rebuild.
No description corpus is retained in the block index.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3

PREFIX = 'family_preparation_'
DERIVED = ('job_families', 'job_family_members', 'job_template_clusters', 'job_template_cluster_lineage')


def signature():
    from . import dedupe
    return hashlib.sha256(Path(__file__).read_bytes() + Path(dedupe.__file__).read_bytes()
                          + str(dedupe.NORMALIZATION_VERSION).encode()).hexdigest()


def schema():
    result = {
        PREFIX + 'state': 'CREATE TABLE family_preparation_state (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL, sealed_revision INTEGER, signature TEXT, source_revision INTEGER NOT NULL, prepared_source_revision INTEGER NOT NULL)',
        PREFIX + 'dirty': 'CREATE TABLE family_preparation_dirty (revision INTEGER PRIMARY KEY, ats TEXT NOT NULL, job_id TEXT NOT NULL)',
        PREFIX + 'blocks': 'CREATE TABLE family_preparation_blocks (ats TEXT NOT NULL, job_id TEXT NOT NULL, block_ats TEXT NOT NULL, block_company TEXT NOT NULL, block_title TEXT NOT NULL, PRIMARY KEY(ats,job_id))',
        PREFIX + 'block_lookup': 'CREATE INDEX family_preparation_block_lookup ON family_preparation_blocks(block_ats,block_company,block_title)',
    }
    result[PREFIX + 'dirty_update'] = ('CREATE TRIGGER family_preparation_dirty_update AFTER UPDATE ON family_preparation_dirty BEGIN UPDATE family_preparation_state SET revision=revision+1 WHERE id=1; END')
    for table in ('jobs', *DERIVED, PREFIX + 'blocks'):
        for event in ('INSERT', 'UPDATE', 'DELETE'):
            name = PREFIX + table + '_' + event.lower()
            condition = ''
            if table == 'jobs':
                if event == 'UPDATE':
                    condition = ' WHEN ' + ' OR '.join('OLD.' + c + ' IS NOT NEW.' + c for c in ('ats', 'id', 'company', 'title', 'description'))
                refs = ('OLD', 'NEW') if event == 'UPDATE' else (('OLD',) if event == 'DELETE' else ('NEW',))
                statements = ' '.join('UPDATE family_preparation_state SET source_revision=source_revision+1 WHERE id=1; INSERT INTO family_preparation_dirty SELECT source_revision,' + ref + '.ats,' + ref + '.id FROM family_preparation_state WHERE id=1;' for ref in refs)
            else:
                statements = 'UPDATE family_preparation_state SET revision=revision+1 WHERE id=1;'
            result[name] = f'CREATE TRIGGER {name} AFTER {event} ON {table}{condition} BEGIN {statements} END'
    return result


def valid(con):
    actual = {r[0]: r[1] for r in con.execute("SELECT name,sql FROM sqlite_master WHERE name GLOB ?", (PREFIX + '*',))}
    return actual == schema()


def trusted(con):
    if not valid(con):
        return False
    row = con.execute('SELECT revision,sealed_revision,signature,source_revision,prepared_source_revision FROM family_preparation_state WHERE id=1').fetchone()
    if not (row and type(row[0]) is int and row[0] >= 0 and row[0] == row[1] and row[2] == signature()
            and type(row[3]) is int and type(row[4]) is int and row[3] >= row[4] >= 0):
        return False
    count, low, high = con.execute('SELECT COUNT(*),MIN(revision),MAX(revision) FROM family_preparation_dirty').fetchone()
    return count == row[3] - row[4] and (not count or (low == row[4] + 1 and high == row[3]))


def current(con):
    return trusted(con) and not con.execute('SELECT 1 FROM family_preparation_dirty LIMIT 1').fetchone()


def check_schema(con):
    if not valid(con):
        raise RuntimeError("Schema migration required: family preparation tracking mismatch")
    if not con.execute('SELECT 1 FROM family_preparation_state WHERE id=1').fetchone():
        raise RuntimeError("Initialization required: family preparation state is missing")


def install(con):
    """Repair only module-owned tracking, inside the full rebuild transaction."""
    from job_search.schema_lifecycle import require_current
    if require_current():
        check_schema(con)
        return
    if valid(con):
        con.execute('INSERT OR IGNORE INTO family_preparation_state VALUES (1,0,NULL,NULL,0,0)')
        return
    objects = list(con.execute("SELECT type,name FROM sqlite_master WHERE name GLOB ? ORDER BY CASE type WHEN 'trigger' THEN 0 WHEN 'index' THEN 1 ELSE 2 END", (PREFIX + '*',)))
    for kind, name in objects:
        con.execute('DROP ' + kind.upper() + ' IF EXISTS "' + name.replace('"', '""') + '"')
    for statement in schema().values():
        con.execute(statement)
    con.execute('INSERT INTO family_preparation_state VALUES (1,0,NULL,NULL,0,0)')


def seal(con):
    con.execute('DELETE FROM family_preparation_dirty')
    con.execute('UPDATE family_preparation_state SET sealed_revision=revision,prepared_source_revision=source_revision,signature=? WHERE id=1', (signature(),))


def seal_full(con):
    install(con)
    con.execute('DELETE FROM family_preparation_blocks')
    con.execute('INSERT INTO family_preparation_blocks SELECT ats,id,block_ats,block_company,block_title FROM preparation.source')
    if con.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] != con.execute('SELECT COUNT(*) FROM family_preparation_blocks').fetchone()[0]:
        con.execute('UPDATE family_preparation_state SET signature=NULL WHERE id=1')
        return
    seal(con)


def counts(con):
    jobs = con.execute('SELECT COUNT(*) FROM job_family_members').fetchone()[0]
    families = con.execute('SELECT COUNT(*) FROM job_families').fetchone()[0]
    return {'jobs': jobs, 'families': families, 'collapsed_jobs': jobs - families,
            'template_clusters': con.execute('SELECT COUNT(DISTINCT template_cluster_id) FROM job_template_clusters').fetchone()[0],
            'leakage_groups': con.execute('SELECT COUNT(DISTINCT leakage_group_id) FROM job_template_clusters').fetchone()[0]}


def prepare(path):
    """Return None when a full pass is required; publish changed blocks atomically."""
    from . import dedupe as d
    with closing(sqlite3.connect(path)) as con, con:
        con.row_factory = sqlite3.Row
        con.execute('PRAGMA foreign_keys=ON')
        con.execute('BEGIN')
        if not trusted(con):
            return None
        dirty = list(con.execute('SELECT DISTINCT ats,job_id FROM family_preparation_dirty'))
        if not dirty:
            return {**counts(con), 'incremental': True, 'changed_blocks': 0, 'prepared_families': 0, 'examined_jobs': 0}
        blocks = set()
        con.execute('CREATE TEMP TABLE changed_job_keys (ats TEXT, job_id TEXT, PRIMARY KEY(ats,job_id))')
        con.executemany('INSERT INTO changed_job_keys VALUES (?,?)', dirty)
        con.execute('CREATE TEMP TABLE changed_job_blocks (ats TEXT, job_id TEXT, block_ats TEXT, block_company TEXT, block_title TEXT, PRIMARY KEY(ats,job_id))')
        con.execute('CREATE INDEX changed_job_block_lookup ON changed_job_blocks(block_ats,block_company,block_title)')
        for ats, job_id in dirty:
            old = con.execute('SELECT block_ats,block_company,block_title FROM family_preparation_blocks WHERE ats=? AND job_id=?', (ats, job_id)).fetchone()
            if old:
                blocks.add(tuple(old))
            row = con.execute('SELECT ats,company,title FROM jobs WHERE ats=? AND id=?', (ats, job_id)).fetchone()
            if row:
                block = tuple(d.normalize_text(value) for value in row)
                blocks.add(block)
                con.execute('INSERT INTO changed_job_blocks VALUES (?,?,?,?,?)', (ats, job_id, *block))
        con.execute('CREATE TEMP TABLE affected_families (family_id TEXT PRIMARY KEY)')
        families, members, provisional = [], [], {}
        for block in sorted(blocks):
            con.execute('INSERT OR IGNORE INTO affected_families SELECT family_id FROM job_families WHERE ats=? AND company_normalized=? AND title_normalized=?', block)
            rows = con.execute(
                'SELECT j.ats,j.id,j.company,j.title,j.description FROM family_preparation_blocks b '
                'JOIN jobs j ON j.ats=b.ats AND j.id=b.job_id '
                'WHERE b.block_ats=? AND b.block_company=? AND b.block_title=? '
                'AND NOT EXISTS (SELECT 1 FROM changed_job_keys k WHERE k.ats=b.ats AND k.job_id=b.job_id) '
                'UNION ALL SELECT j.ats,j.id,j.company,j.title,j.description FROM changed_job_blocks b '
                'JOIN jobs j ON j.ats=b.ats AND j.id=b.job_id '
                'WHERE b.block_ats=? AND b.block_company=? AND b.block_title=?', (*block, *block))
            block_families, block_members = d._build_rows(rows)
            provisional.update(d._template_components(block_families))
            for family in block_families:
                description = family.pop('description_normalized')
                family['leakage_group_id'] = d._identifier('leak', 'exact-description', [description]) if description else d._identifier('leak', 'empty-singleton', [family['family_id']])
            families.extend(block_families)
            members.extend(block_members)
        # Lineages may bridge blocks after historical moves. Resolve globally so
        # merges redirect every historical alias exactly as a full pass does.
        assigned = d._stable_template_lineages(con, provisional, members)
        templates = [(f['family_id'], assigned[f['family_id']], f['leakage_group_id'], d.NORMALIZATION_VERSION) for f in families]
        d._upsert_derived_rows(con, families, members, templates, scoped=True)
        # A merge can redirect an unaffected block's prior lineage too.
        con.execute('UPDATE job_template_clusters SET template_cluster_id=(SELECT lineage_id FROM job_template_cluster_lineage l WHERE l.template_cluster_id=job_template_clusters.template_cluster_id) WHERE EXISTS (SELECT 1 FROM job_template_cluster_lineage l WHERE l.template_cluster_id=job_template_clusters.template_cluster_id AND l.lineage_id IS NOT job_template_clusters.template_cluster_id)')
        con.executemany('DELETE FROM family_preparation_blocks WHERE ats=? AND job_id=?', dirty)
        con.execute('INSERT INTO family_preparation_blocks SELECT * FROM changed_job_blocks')
        seal(con)
        return {**counts(con), 'incremental': True, 'changed_blocks': len(blocks),
                'prepared_families': len(families), 'examined_jobs': len(members)}
