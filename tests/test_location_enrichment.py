#!/usr/bin/env python3

import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

from job_search.collection import locations
from job_search.collection.locations import backfill, compatibility_score, normalize_location


def _row(location: str, remote: object = "", workplace: str = "") -> dict[str, object]:
    return {
        "ats": "ashby", "location": location,
        "isRemote": remote, "workplaceType": workplace,
    }


def test_preferred_metros_and_remote_policy() -> None:
    city = normalize_location(_row("New York, NY (Hybrid)", False, "Hybrid"))
    assert city["us_eligibility"] == "eligible"
    assert city["preferred_metro_code"] == "new_york_city"
    assert city["arrangement"] == "hybrid"
    assert compatibility_score(city) == 1.0

    remote = normalize_location(_row("Remote - United States", True, "Remote"))
    assert remote["us_eligibility"] == "eligible"
    assert remote["preferred_metro_code"] is None
    assert compatibility_score(remote) == 0.8
    assert normalize_location(_row("Remote, U.S", True, "Remote"))["us_eligibility"] == "eligible"

    other = normalize_location(_row("Raleigh, NC", False, "On-site"))
    assert other["us_eligibility"] == "eligible"
    assert compatibility_score(other) == 0.55


def test_unknown_remote_and_non_us_are_not_eligible() -> None:
    unknown = normalize_location(_row("Remote", True, "Remote"))
    assert unknown["us_eligibility"] == "unknown"
    assert compatibility_score(unknown) is None
    pronoun = normalize_location(_row("Join us remotely", True, "Remote"))
    assert pronoun["us_eligibility"] == "unknown"
    canada = normalize_location(_row("Toronto, Canada", False, "Onsite"))
    assert canada["us_eligibility"] == "ineligible"
    assert compatibility_score(canada) is None


def test_mixed_locations_retain_us_option() -> None:
    mixed = normalize_location(_row("Toronto, Canada; Chicago, IL", False, "Hybrid"))
    assert mixed["us_eligibility"] == "eligible"
    assert mixed["status"] == "partial"
    assert mixed["preferred_metro_code"] == "chicago"


def test_backfill_is_idempotent_and_preserves_raw_jobs() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "jobs.db"
        with sqlite3.connect(db) as con:
            con.execute(
                "CREATE TABLE jobs (ats TEXT,id TEXT,location TEXT,isRemote TEXT,"
                "workplaceType TEXT,PRIMARY KEY(ats,id))"
            )
            con.executemany("INSERT INTO jobs VALUES (?,?,?,?,?)", [
                ("ashby", "1", "San Francisco, CA", "0", "Onsite"),
                ("lever", "2", "Remote - US", "1", "remote"),
            ])
        first = backfill(db)
        second = backfill(db)
        assert first["changed"] == 2 and second["changed"] == 0
        with sqlite3.connect(db) as con:
            assert con.execute("SELECT location FROM jobs ORDER BY id").fetchall() == [
                ("San Francisco, CA",), ("Remote - US",),
            ]
            assert con.execute(
                "SELECT COUNT(*) FROM job_location_enrichment"
            ).fetchone()[0] == 2


def test_unchanged_backfill_skips_normalization_but_invalidates_each_input() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "jobs.db"
        with sqlite3.connect(db) as con:
            con.execute("CREATE TABLE jobs (ats TEXT,id TEXT,location TEXT,isRemote TEXT,workplaceType TEXT,PRIMARY KEY(ats,id))")
            con.executemany("INSERT INTO jobs VALUES (?,?,?,?,?)", [
                ("ashby", "1", "Chicago, IL", "0", "onsite"),
                ("lever", "2", "Remote", "1", "remote"),
                ("lever", "3", "", "", ""),
            ])
        initial = backfill(db)
        with sqlite3.connect(db) as con:
            before = con.execute("SELECT * FROM job_location_enrichment ORDER BY ats,job_id").fetchall()
            targets = con.execute("SELECT * FROM job_location_targets ORDER BY ats,job_id,ordinal").fetchall()
        with patch.object(locations, "normalize_location", side_effect=AssertionError("unchanged input normalized")):
            repeat = backfill(db)
            limited = backfill(db, limit=1)
        assert repeat == {**initial, "changed": 0}
        assert limited["processed"] == 1 and limited["changed"] == 0
        with sqlite3.connect(db) as con:
            assert con.execute("SELECT * FROM job_location_enrichment ORDER BY ats,job_id").fetchall() == before
            assert con.execute("SELECT * FROM job_location_targets ORDER BY ats,job_id,ordinal").fetchall() == targets
        for column, value in (("location", "Toronto, Canada"), ("isRemote", "1"), ("workplaceType", "hybrid")):
            with sqlite3.connect(db) as con:
                con.execute(f"UPDATE jobs SET {column}=? WHERE ats='ashby' AND id='1'", (value,))
            with patch.object(locations, "normalize_location", wraps=normalize_location) as normalize:
                assert backfill(db)["changed"] == 1
                assert normalize.call_count == 1
        with patch.object(locations, "NORMALIZER_VERSION", "location-fixture-v2"):
            with patch.object(locations, "normalize_location", wraps=normalize_location) as normalize:
                assert backfill(db)["changed"] == 3
                assert normalize.call_count == 3


def main() -> None:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"ok ({len(tests)} location-enrichment tests)")


if __name__ == "__main__":
    main()
