#!/usr/bin/env python3
"""Offline tests for deterministic job opportunity families."""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

from job_search.collection.dedupe import AUDIT_EXCERPT_CHARS, audit_clusters, main, normalize_text, prepare_families


def _long(sentence: str, repeats: int = 10) -> str:
    return " ".join(sentence for _ in range(repeats))


def _make_db(directory: str, jobs: list[tuple[str, str, str, str, str]]) -> Path:
    path = Path(directory) / "jobs.db"
    with sqlite3.connect(path) as con:
        con.execute(
            "CREATE TABLE jobs (ats TEXT NOT NULL,id TEXT NOT NULL,company TEXT,title TEXT,"
            "description TEXT,first_seen TEXT,last_seen TEXT,PRIMARY KEY(ats,id))"
        )
        con.executemany(
            "INSERT INTO jobs (ats,id,company,title,description,first_seen,last_seen) "
            "VALUES (?,?,?,?,?,'first','last')",
            jobs,
        )
    return path


def _derived_snapshot(path: Path) -> dict[str, list[tuple]]:
    with sqlite3.connect(path) as con:
        return {
            table: con.execute(f"SELECT * FROM {table} ORDER BY 1,2").fetchall()
            for table in ("job_families", "job_family_members", "job_template_clusters")
        }


def test_normalization_collapses_unicode_html_entities_and_whitespace() -> None:
    assert normalize_text(" <p>Ｓenior&nbsp;R&amp;D</p><script>bad()</script> ") == "senior r&d"


def test_exact_variants_collapse_and_preserve_source_jobs() -> None:
    plain = _long("Build reliable systems & services for customers.")
    html = "<div>" + "<p>Build reliable systems &amp; services for customers.</p>" * 10 + "</div>"
    with tempfile.TemporaryDirectory() as directory:
        db = _make_db(directory, [
            ("ashby", "2", "Ａcme", "Senior Engineer", html),
            ("ashby", "1", "acme", "  senior   engineer ", plain),
            ("lever", "3", "acme", "Senior Engineer", plain),
        ])
        before = sqlite3.connect(db).execute("SELECT * FROM jobs ORDER BY ats,id").fetchall()
        result = prepare_families(db)
        after = sqlite3.connect(db).execute("SELECT * FROM jobs ORDER BY ats,id").fetchall()
        assert before == after
        assert result == {
            "jobs": 3, "families": 2, "collapsed_jobs": 1,
            "template_clusters": 2, "leakage_groups": 1,
        }
        with sqlite3.connect(db) as con:
            family = con.execute(
                "SELECT canonical_ats,canonical_job_id,member_count FROM job_families "
                "WHERE member_count=2"
            ).fetchone()
            assert family == ("ashby", "1", 2)


def test_identical_short_descriptions_are_family_singletons_but_share_leakage() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db = _make_db(directory, [
            ("ashby", "1", "acme", "Engineer", "Same thin posting"),
            ("ashby", "2", "acme", "Engineer", "Same thin posting"),
        ])
        result = prepare_families(db)
        assert result["families"] == 2 and result["collapsed_jobs"] == 0
        with sqlite3.connect(db) as con:
            rows = con.execute(
                "SELECT template_cluster_id,leakage_group_id FROM job_template_clusters"
            ).fetchall()
        assert len({row[0] for row in rows}) == 2
        assert len({row[1] for row in rows}) == 1


def test_empty_descriptions_remain_leakage_singletons() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db = _make_db(directory, [
            ("ashby", "1", "acme", "Engineer", ""),
            ("lever", "2", "beta", "Engineer", ""),
        ])
        prepare_families(db)
        with sqlite3.connect(db) as con:
            rows = con.execute(
                "SELECT leakage_group_id FROM job_template_clusters"
            ).fetchall()
        assert len({row[0] for row in rows}) == 2


def test_exact_description_leakage_crosses_ats_titles_and_companies() -> None:
    description = _long("Design resilient distributed data services with careful operations.")
    with tempfile.TemporaryDirectory() as directory:
        db = _make_db(directory, [
            ("ashby", "1", "alpha", "Platform Engineer", description),
            ("lever", "2", "beta", "Site Reliability Engineer", description),
        ])
        prepare_families(db)
        with sqlite3.connect(db) as con:
            rows = con.execute(
                "SELECT template_cluster_id,leakage_group_id FROM job_template_clusters"
            ).fetchall()
        assert len({row[0] for row in rows}) == 2
        assert len({row[1] for row in rows}) == 1


def test_near_templates_cluster_without_becoming_duplicates() -> None:
    base = _long("Build operate and improve reliable cloud systems for global customers.", 30)
    changed = base.replace("global", "enterprise", 1)
    with tempfile.TemporaryDirectory() as directory:
        db = _make_db(directory, [
            ("greenhouse", "1", "acme", "Software Engineer", base),
            ("greenhouse", "2", "acme", "Software Engineer", changed),
        ])
        result = prepare_families(db)
        assert result["families"] == 2 and result["collapsed_jobs"] == 0
        with sqlite3.connect(db) as con:
            mappings = con.execute(
                "SELECT template_cluster_id,leakage_group_id FROM job_template_clusters"
            ).fetchall()
        assert len({row[0] for row in mappings}) == 1
        assert len({row[1] for row in mappings}) == 2


def test_unrelated_descriptions_with_same_block_remain_separate() -> None:
    platform = _long("Build distributed databases APIs and observability for cloud infrastructure.")
    design = _long("Create brand illustrations typography campaigns and visual design systems.")
    with tempfile.TemporaryDirectory() as directory:
        db = _make_db(directory, [
            ("ashby", "1", "acme", "Lead", platform),
            ("ashby", "2", "acme", "Lead", design),
        ])
        prepare_families(db)
        with sqlite3.connect(db) as con:
            cluster_count = con.execute(
                "SELECT COUNT(DISTINCT template_cluster_id) FROM job_template_clusters"
            ).fetchone()[0]
        assert cluster_count == 2


def test_schema_exposes_stable_ids_versions_and_group_indexes() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db = _make_db(directory, [
            ("ashby", "1", "acme", "Engineer", _long("Build useful reliable software.")),
        ])
        prepare_families(db)
        with sqlite3.connect(db) as con:
            family_columns = {row[1] for row in con.execute("PRAGMA table_info(job_families)")}
            template_columns = {
                row[1] for row in con.execute("PRAGMA table_info(job_template_clusters)")
            }
            indexes = {
                row[0] for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'job_%'"
                )
            }
        assert {
            "family_id", "family_fingerprint", "normalization_version",
            "canonical_ats", "canonical_job_id", "description_fingerprint",
        } <= family_columns
        assert {
            "family_id", "template_cluster_id", "leakage_group_id",
            "normalization_version",
        } <= template_columns
        assert {
            "job_family_members_family", "job_template_clusters_template",
            "job_template_clusters_leakage",
        } <= indexes


def test_prepare_is_idempotent_and_content_edits_remove_stale_rows() -> None:
    first = _long("Build secure infrastructure services and automation for customers.")
    second = _long("Analyze product experiments metrics and customer behavior for teams.")
    with tempfile.TemporaryDirectory() as directory:
        db = _make_db(directory, [
            ("ashby", "1", "acme", "Engineer", first),
            ("lever", "2", "beta", "Analyst", second),
        ])
        prepare_families(db)
        initial = _derived_snapshot(db)
        old_source_fingerprint = next(
            row[3] for row in initial["job_family_members"] if row[:2] == ("ashby", "1")
        )
        prepare_families(db)
        assert _derived_snapshot(db) == initial
        old_family = next(
            row[2] for row in initial["job_family_members"] if row[:2] == ("ashby", "1")
        )
        unchanged_family = next(
            row[2] for row in initial["job_family_members"] if row[:2] == ("lever", "2")
        )
        edited = _long("Develop machine learning ranking systems and data pipelines for search.")
        with sqlite3.connect(db) as con:
            con.execute(
                "UPDATE jobs SET description=? WHERE ats='ashby' AND id='1'", (edited,)
            )
        prepare_families(db)
        with sqlite3.connect(db) as con:
            current = dict(
                ((ats, job_id), family_id) for ats, job_id, family_id in con.execute(
                    "SELECT ats,job_id,family_id FROM job_family_members"
                )
            )
            assert current[("ashby", "1")] != old_family
            assert current[("lever", "2")] == unchanged_family
            assert not con.execute(
                "SELECT 1 FROM job_families WHERE family_id=?", (old_family,)
            ).fetchone()
            assert con.execute(
                "SELECT source_fingerprint FROM job_family_members "
                "WHERE ats='ashby' AND job_id='1'"
            ).fetchone()[0] != old_source_fingerprint
            assert con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
        with sqlite3.connect(db) as con:
            con.execute("DELETE FROM jobs WHERE ats='lever' AND id='2'")
        prepare_families(db)
        with sqlite3.connect(db) as con:
            assert not con.execute(
                "SELECT 1 FROM job_family_members WHERE ats='lever' AND job_id='2'"
            ).fetchone()
            assert not con.execute(
                "SELECT 1 FROM job_families WHERE family_id=?", (unchanged_family,)
            ).fetchone()
            assert con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_audit_is_read_only_deterministic_and_bounded() -> None:
    base = _long("Build operate and improve reliable cloud systems for global customers.", 30)
    changed = base.replace("global", "enterprise", 1)
    unrelated = _long("Create illustrations typography campaigns and visual design systems.", 30)
    with tempfile.TemporaryDirectory() as directory:
        db = _make_db(directory, [
            ("greenhouse", "1", "acme", "Software Engineer", base),
            ("greenhouse", "2", "acme", "Software Engineer", changed),
            ("greenhouse", "3", "acme", "Software Engineer", unrelated),
        ])
        prepare_families(db)
        before = _derived_snapshot(db)
        first = audit_clusters(db, limit=1)
        second = audit_clusters(db, limit=1)
        assert first == second
        assert _derived_snapshot(db) == before
        assert first["eligible_cluster_count"] == 1
        assert first["returned_cluster_count"] == 1
        assert first["returned_family_count"] == 2
        assert first["returned_job_count"] == 2
        cluster = first["clusters"][0]
        assert cluster["family_count"] == 2 and cluster["job_count"] == 2
        assert len(cluster["families"]) == 2
        for family in cluster["families"]:
            assert family["canonical_company"] == "acme"
            assert family["canonical_title"] == "Software Engineer"
            assert family["member_ids"] in (
                [{"ats": "greenhouse", "id": "1"}],
                [{"ats": "greenhouse", "id": "2"}],
            )
            assert len(family["description_excerpt"]) <= AUDIT_EXCERPT_CHARS
            assert family["description_chars"] > len(family["description_excerpt"])
            assert family["description_excerpt_truncated"]


def test_audit_cli_writes_json_or_stdout() -> None:
    base = _long("Build operate and improve reliable cloud systems for customers.", 30)
    changed = base.replace("customers", "enterprises", 1)
    with tempfile.TemporaryDirectory() as directory:
        db = _make_db(directory, [
            ("ashby", "1", "acme", "Engineer", base),
            ("ashby", "2", "acme", "Engineer", changed),
        ])
        prepare_families(db)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            main(["--db", str(db), "audit", "--limit", "1"])
        assert json.loads(stdout.getvalue())["returned_cluster_count"] == 1

        output = Path(directory) / "cluster-audit.json"
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            main([
                "--db", str(db), "audit", "--limit", "1", "--output", str(output),
            ])
        assert stdout.getvalue() == ""
        assert json.loads(output.read_text(encoding="utf-8"))[
            "clusters"
        ][0]["family_count"] == 2

        errors = io.StringIO()
        try:
            with contextlib.redirect_stderr(errors):
                main(["--db", str(db), "audit", "--output", str(db)])
        except SystemExit as exc:
            assert exc.code == 2
        else:
            raise AssertionError("audit allowed --output to overwrite its source database")
        assert "must not overwrite" in errors.getvalue()
        with sqlite3.connect(db) as con:
            assert con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2


def test_cli_prepare_prints_machine_readable_summary() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db = _make_db(directory, [
            ("ashby", "1", "acme", "Engineer", _long("Build useful reliable software.")),
        ])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            main(["--db", str(db), "prepare"])
        assert json.loads(output.getvalue())["families"] == 1


def test_singleton_blocks_do_not_build_unused_similarity_vectors() -> None:
    from job_search.collection import dedupe
    families = [dict(family_id=f'fam_{i}', ats='ashby', company_normalized='example',
                     title_normalized=f'engineer {i}', description_normalized=_long('Build reliable services.'))
                for i in range(100)]
    # A short/error posting in an otherwise singleton block is also ineligible.
    families.append({**families[0], 'family_id': 'fam_thin', 'description_normalized': ''})
    with patch.object(dedupe, '_tfidf_vectors', side_effect=AssertionError('unused vectorization')):
        clusters = dedupe._template_components(families)
    assert clusters == {f['family_id']: dedupe._identifier('tpl', 'template-cluster', [f['family_id']])
                        for f in families}


def test_connected_templates_skip_redundant_comparisons_without_merging_outliers() -> None:
    from job_search.collection import dedupe
    families = [dict(family_id=f'fam_{i:03}', ats='ashby', company_normalized='example',
                     title_normalized='engineer', description_normalized=_long('Build reliable services.'))
                for i in range(40)]
    outlier = dict(families[0], family_id='fam_outlier',
                   description_normalized=_long('Organize theatre productions and choreograph ballet.'))
    with patch.object(dedupe, '_cosine', wraps=dedupe._cosine) as cosine:
        clusters = dedupe._template_components([*families, outlier])
    expected = dedupe._identifier('tpl', 'template-cluster', [f['family_id'] for f in families])
    assert all(clusters[f['family_id']] == expected for f in families)
    assert clusters['fam_outlier'] != expected
    assert cosine.call_count == 79  # 39 connecting edges and 40 outlier comparisons.


def test_prepared_reuse_requires_identical_source_even_without_timestamp_changes() -> None:
    from job_search.collection.dedupe import prepared_families_are_current
    with tempfile.TemporaryDirectory() as directory:
        db = _make_db(directory, [('ashby', '1', 'example', 'Engineer', _long('Build systems.'))])
        assert not prepared_families_are_current(db)
        prepare_families(db)
        assert prepared_families_are_current(db)
        for column in ('company', 'title', 'description'):
            with sqlite3.connect(db) as con:
                con.execute(f"UPDATE jobs SET {column}={column} || ' changed'")
            assert not prepared_families_are_current(db)
            prepare_families(db)
            assert prepared_families_are_current(db)
        with sqlite3.connect(db) as con:
            con.execute("UPDATE jobs SET id='replacement'")
        assert not prepared_families_are_current(db)
        prepare_families(db)
        assert prepared_families_are_current(db)
        with sqlite3.connect(db) as con:
            con.execute('DELETE FROM jobs')
        assert not prepared_families_are_current(db)


def test_prepared_reuse_rejects_stale_normalization_or_missing_templates() -> None:
    from job_search.collection.dedupe import prepared_families_are_current
    with tempfile.TemporaryDirectory() as directory:
        db = _make_db(directory, [('ashby', '1', 'example', 'Engineer', _long('Build systems.'))])
        prepare_families(db)
        with sqlite3.connect(db) as con:
            con.execute('UPDATE job_families SET normalization_version=-1')
        assert not prepared_families_are_current(db)
        prepare_families(db)
        with sqlite3.connect(db) as con:
            con.execute('DELETE FROM job_template_clusters')
        assert not prepared_families_are_current(db)


def _reference_prepare(path: Path) -> None:
    """Original whole-catalog workflow, retained as a parity oracle for small fixtures."""
    from job_search.collection import dedupe
    with sqlite3.connect(path) as con:
        con.row_factory = sqlite3.Row
        families, members = dedupe._build_rows(con.execute(
            "SELECT ats,id,company,title,description FROM jobs ORDER BY ats,id"))
        templates = dedupe._stable_template_lineages(con, dedupe._template_components(families), members)
        rows = []
        for family in families:
            fid = family["family_id"]
            description = family["description_normalized"]
            leakage = (dedupe._identifier("leak", "exact-description", [description]) if description
                       else dedupe._identifier("leak", "empty-singleton", [fid]))
            rows.append((fid, templates[fid], leakage, dedupe.NORMALIZATION_VERSION))
        dedupe._upsert_derived_rows(con, families, members, rows)


def test_block_preparation_matches_full_catalog_through_lineage_edits_and_splits() -> None:
    from job_search.collection import dedupe
    text = _long("Build reliable systems and carefully review services.", 30)
    with tempfile.TemporaryDirectory() as left_dir, tempfile.TemporaryDirectory() as right_dir:
        jobs = [("ashby", str(i), "Ａcme" if i % 2 else "acme", "Engineer", text + (" Extra." if i % 3 else ""))
                for i in range(12)]
        jobs += [("lever", "empty", "Elsewhere", None, ""),
                 ("lever", "thin", "Elsewhere", "Design", "Thin"),
                 ("ashby", "outside", "Another", "Research", text)]
        left, right = _make_db(left_dir, jobs), _make_db(right_dir, jobs)
        edits = [None,
                 ("UPDATE jobs SET description=? WHERE id='1'", (_long("Independent research into fossils."),)),
                 ("DELETE FROM jobs WHERE id IN ('0','3','6','9')", ()),
                 ("UPDATE jobs SET company='Another' WHERE id='1'", ())]
        for edit in edits:
            if edit:
                for path in (left, right):
                    with sqlite3.connect(path) as con:
                        con.execute(*edit)
            _reference_prepare(left)
            prepare_families(right)
            assert _derived_snapshot(left) == _derived_snapshot(right)
            with sqlite3.connect(left) as a, sqlite3.connect(right) as b:
                sql = "SELECT * FROM job_template_cluster_lineage ORDER BY 1,2"
                assert a.execute(sql).fetchall() == b.execute(sql).fetchall()
                assert a.execute("SELECT * FROM jobs ORDER BY ats,id").fetchall() == b.execute("SELECT * FROM jobs ORDER BY ats,id").fetchall()
            assert dedupe.prepared_families_are_current(right)


def test_preparation_failure_rolls_back_derived_metadata_and_closes_connection() -> None:
    from job_search.collection import dedupe
    with tempfile.TemporaryDirectory() as directory:
        db = _make_db(directory, [("ashby", "1", "acme", "Engineer", _long("Build reliable systems."))])
        prepare_families(db)
        before = _derived_snapshot(db)
        with sqlite3.connect(db) as con:
            old_lineage = con.execute("SELECT * FROM job_template_cluster_lineage ORDER BY 1").fetchall()
            con.execute("UPDATE jobs SET title='Changed'")
            con.execute("INSERT INTO jobs(ats,id,company,title,description) "
                        "VALUES ('lever','new','Another','Novel','Short description')")
        with patch.object(dedupe, "_upsert_derived_rows", side_effect=RuntimeError("injected publication failure")):
            try:
                prepare_families(db)
            except RuntimeError:
                pass
            else:
                raise AssertionError("failure did not propagate")
        assert before == _derived_snapshot(db)
        assert not list(Path(directory).glob(".family-preparation-*"))
        # A fresh exclusive writer succeeds immediately after the failed call.
        with sqlite3.connect(db, timeout=0) as con:
            assert con.execute("SELECT * FROM job_template_cluster_lineage ORDER BY 1").fetchall() == old_lineage
            con.execute("BEGIN EXCLUSIVE")
            con.execute("UPDATE jobs SET title='Recovered'")
        prepare_families(db)
        assert dedupe.prepared_families_are_current(db)


def test_concurrent_source_commit_prevents_stale_preparation_publication() -> None:
    from job_search.collection import dedupe
    with tempfile.TemporaryDirectory() as directory:
        db = _make_db(directory, [("ashby", "1", "acme", "Engineer", _long("Build reliable systems."))])
        with sqlite3.connect(db) as con:
            con.execute("PRAGMA journal_mode=WAL")
        prepare_families(db)
        before = _derived_snapshot(db)
        original = dedupe._template_components

        def concurrent_edit(rows):
            with sqlite3.connect(db, timeout=0) as writer:
                writer.execute("UPDATE jobs SET title='Changed concurrently'")
            return original(rows)

        with patch.object(dedupe, "_template_components", side_effect=concurrent_edit):
            try:
                prepare_families(db)
            except sqlite3.OperationalError as exc:
                assert "locked" in str(exc)
            else:
                raise AssertionError("concurrent source change was silently certified")
        assert before == _derived_snapshot(db)
        assert not dedupe.prepared_families_are_current(db)
        prepare_families(db)
        assert dedupe.prepared_families_are_current(db)


def test_currentness_uses_caller_snapshot_without_ending_transaction() -> None:
    from job_search.collection import dedupe
    with tempfile.TemporaryDirectory() as directory:
        db = _make_db(directory, [("ashby", "1", "acme", "Engineer", _long("Build reliable systems."))])
        prepare_families(db)
        with sqlite3.connect(db) as con:
            con.execute("BEGIN")
            con.execute("UPDATE jobs SET description='uncommitted source change'")
            assert not dedupe.prepared_families_are_current(db, connection=con)
            assert con.in_transaction
            con.rollback()
            assert dedupe.prepared_families_are_current(db, connection=con)
            assert con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_preparation_stages_privately_on_catalog_volume_and_cleans_up() -> None:
    from job_search.collection import dedupe
    with tempfile.TemporaryDirectory() as directory:
        db = _make_db(directory, [("ashby", "1", "acme", "Engineer", _long("Build reliable systems."))])
        original = dedupe._template_components
        seen = []
        connections = []
        original_connect = sqlite3.connect

        def track_connect(*args, **kwargs):
            con = original_connect(*args, **kwargs)
            connections.append(con)
            return con

        def inspect_scratch(rows):
            scratch = list(db.parent.glob(".family-preparation-*"))
            assert len(scratch) == 1
            assert scratch[0].parent.resolve() == db.parent.resolve()
            assert scratch[0].stat().st_mode & 0o777 == 0o700
            assert (scratch[0] / "source.db").is_file()
            databases = {row[1]: row[2] for row in connections[0].execute("PRAGMA database_list")}
            assert Path(databases["preparation"]).resolve() == (scratch[0] / "source.db").resolve()
            seen.append(scratch[0])
            return original(rows)

        with patch.object(dedupe.sqlite3, "connect", side_effect=track_connect), \
                patch.object(dedupe, "_template_components", side_effect=inspect_scratch):
            prepare_families(db)
        assert seen and all(not path.exists() for path in seen)


def test_block_preparation_preserves_numeric_zero_and_skips_only_missing_ids() -> None:
    with tempfile.TemporaryDirectory() as directory:
        paths = [Path(directory) / name for name in ("reference.db", "optimized.db")]
        for db in paths:
            with sqlite3.connect(db) as con:
                con.execute("CREATE TABLE jobs(ats,id,company,title,description)")
                con.executemany("INSERT INTO jobs VALUES (?,?,?,?,?)", [
                    (0, 0, "numeric", "Role", _long("Build systems.")),
                    ("ashby", 0, "numeric", "Role", _long("Build systems.")),
                    (None, "missing ats", None, None, None),
                    ("ashby", None, None, None, None),
                    ("", "blank ats", None, None, None),
                    ("ashby", "", None, None, None),
                ])
        _reference_prepare(paths[0])
        result = prepare_families(paths[1])
        assert result["jobs"] == 2
        assert _derived_snapshot(paths[0]) == _derived_snapshot(paths[1])


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"ok ({len(tests)} dedupe tests)")
