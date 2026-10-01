#!/usr/bin/env python3
"""Build deterministic opportunity families and leakage-safe template groups.

This is an offline, dependency-free processor over the scraper's SQLite database.
It never changes ``jobs``: all output is derived and can be regenerated safely.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
from html.parser import HTMLParser
import json
import math
from pathlib import Path
import re
import sqlite3
import unicodedata
from typing import Iterable, Sequence
from urllib.parse import quote


NORMALIZATION_VERSION = 1
TEMPLATE_SIMILARITY_THRESHOLD = 0.985
MIN_DESCRIPTION_CHARS = 200
AUDIT_EXCERPT_CHARS = 400
AUDIT_MEMBER_IDS = 20


_BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "br", "dd", "div", "dl",
    "dt", "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2",
    "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol",
    "p", "pre", "section", "table", "tbody", "td", "tfoot", "th", "thead",
    "tr", "ul",
}
_WORD_RE = re.compile(r"\w+", re.UNICODE)


class _TextExtractor(HTMLParser):
    """Small HTML-to-text converter that does not concatenate block boundaries."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        tag = tag.casefold()
        if tag in {"script", "style"}:
            self._ignored_depth += 1
        elif not self._ignored_depth and tag in _BLOCK_TAGS:
            self.parts.append(" ")

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)
        if tag.casefold() in {"script", "style"}:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in {"script", "style"} and self._ignored_depth:
            self._ignored_depth -= 1
        elif not self._ignored_depth and tag in _BLOCK_TAGS:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.parts.append(data)


def html_to_text(value: object) -> str:
    parser = _TextExtractor()
    parser.feed("" if value is None else str(value))
    parser.close()
    return "".join(parser.parts)


def normalize_text(value: object) -> str:
    """Canonical text used by every family and grouping fingerprint."""
    plain = html_to_text(value)
    canonical = unicodedata.normalize("NFKC", plain).casefold()
    return " ".join(canonical.split())


def _fingerprint(domain: str, parts: Sequence[object]) -> str:
    payload = json.dumps(
        [NORMALIZATION_VERSION, domain, *parts],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _identifier(prefix: str, domain: str, parts: Sequence[object]) -> str:
    return f"{prefix}_{_fingerprint(domain, parts)}"


def _features(description: str) -> Counter[str]:
    tokens = _WORD_RE.findall(description)
    features: Counter[str] = Counter(f"w:{token}" for token in tokens)
    features.update(
        f"b:{left}\x1f{right}" for left, right in zip(tokens, tokens[1:])
    )
    return features


def _tfidf_vectors(
    descriptions: Sequence[str],
) -> list[tuple[dict[str, float], float]]:
    counts = [_features(description) for description in descriptions]
    document_frequency: Counter[str] = Counter()
    for values in counts:
        document_frequency.update(values.keys())
    total = len(counts)
    vectors: list[tuple[dict[str, float], float]] = []
    for values in counts:
        vector = {
            feature: (1.0 + math.log(count))
            * (math.log((1.0 + total) / (1.0 + document_frequency[feature])) + 1.0)
            for feature, count in values.items()
        }
        norm = math.sqrt(sum(weight * weight for weight in vector.values()))
        vectors.append((vector, norm))
    return vectors


def _cosine(
    left: tuple[dict[str, float], float],
    right: tuple[dict[str, float], float],
) -> float:
    left_values, left_norm = left
    right_values, right_norm = right
    if not left_norm or not right_norm:
        return 0.0
    if len(left_values) > len(right_values):
        left_values, right_values = right_values, left_values
    dot = sum(weight * right_values.get(feature, 0.0) for feature, weight in left_values.items())
    return dot / (left_norm * right_norm)


def _template_components(
    family_rows: Sequence[dict[str, object]],
) -> dict[str, str]:
    """Return family -> deterministic cluster id without collapsing families."""
    by_block: dict[tuple[str, str, str], list[dict[str, object]]] = defaultdict(list)
    for family in family_rows:
        block = (
            str(family["ats"]),
            str(family["company_normalized"]),
            str(family["title_normalized"]),
        )
        by_block[block].append(family)

    cluster_by_family: dict[str, str] = {}
    for block_families in by_block.values():
        ordered = sorted(block_families, key=lambda item: str(item["family_id"]))
        parent = list(range(len(ordered)))

        def find(index: int) -> int:
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        def union(left: int, right: int) -> None:
            left_root, right_root = find(left), find(right)
            if left_root != right_root:
                parent[max(left_root, right_root)] = min(left_root, right_root)

        eligible = [
            index for index, family in enumerate(ordered)
            if len(str(family["description_normalized"])) >= MIN_DESCRIPTION_CHARS
        ]
        # A block with fewer than two eligible families has no comparisons.
        # Most catalog blocks are singletons; avoid tokenizing their descriptions.
        vectors = _tfidf_vectors(
            [str(ordered[index]["description_normalized"]) for index in eligible]
        ) if len(eligible) > 1 else []
        for left_position, left_index in enumerate(eligible):
            for right_position in range(left_position + 1, len(eligible)):
                # An edge inside an already-connected component cannot change it.
                if find(left_index) == find(eligible[right_position]):
                    continue
                if _cosine(vectors[left_position], vectors[right_position]) \
                        >= TEMPLATE_SIMILARITY_THRESHOLD:
                    union(left_index, eligible[right_position])

        components: dict[int, list[str]] = defaultdict(list)
        for index, family in enumerate(ordered):
            components[find(index)].append(str(family["family_id"]))
        for members in components.values():
            cluster_id = _identifier("tpl", "template-cluster", sorted(members))
            for family_id in members:
                cluster_by_family[family_id] = cluster_id
    return cluster_by_family


_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS job_families (
        family_id              TEXT PRIMARY KEY,
        family_fingerprint     TEXT NOT NULL UNIQUE,
        normalization_version  INTEGER NOT NULL,
        ats                    TEXT NOT NULL,
        company_normalized     TEXT NOT NULL,
        title_normalized       TEXT NOT NULL,
        description_fingerprint TEXT NOT NULL,
        canonical_ats          TEXT NOT NULL,
        canonical_job_id       TEXT NOT NULL,
        member_count           INTEGER NOT NULL CHECK (member_count > 0)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS job_family_members (
        ats                TEXT NOT NULL,
        job_id             TEXT NOT NULL,
        family_id          TEXT NOT NULL REFERENCES job_families(family_id)
                            ON DELETE CASCADE,
        source_fingerprint TEXT NOT NULL,
        PRIMARY KEY (ats, job_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS job_template_clusters (
        family_id             TEXT PRIMARY KEY REFERENCES job_families(family_id)
                               ON DELETE CASCADE,
        template_cluster_id   TEXT NOT NULL,
        leakage_group_id      TEXT NOT NULL,
        normalization_version INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS job_template_cluster_lineage (
        template_cluster_id   TEXT PRIMARY KEY,
        lineage_id            TEXT NOT NULL,
        normalization_version INTEGER NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS job_family_members_family ON job_family_members(family_id)",
    "CREATE INDEX IF NOT EXISTS job_template_clusters_template ON job_template_clusters(template_cluster_id)",
    "CREATE INDEX IF NOT EXISTS job_template_clusters_leakage ON job_template_clusters(leakage_group_id)",
    "CREATE INDEX IF NOT EXISTS job_template_cluster_lineage_root ON job_template_cluster_lineage(lineage_id)",
)


def _stable_template_lineages(
    con: sqlite3.Connection,
    provisional_by_family: dict[str, str],
    member_rows: Sequence[tuple[str, str, str, str]],
) -> dict[str, str]:
    """Keep template groups conservative and stable as their membership evolves.

    A content-derived component hash changes whenever a family joins or leaves. Labels
    retain the cluster ID seen at judgment time, so exposing that changing hash would
    let the same evolving template cross a later model-validation fold. This lineage
    table retains aliases forever. Splits intentionally keep their old shared lineage;
    merges collapse every prior lineage to one deterministic root.
    """
    con.execute(_SCHEMA[3])
    aliases = {
        str(row[0]): str(row[1])
        for row in con.execute(
            "SELECT template_cluster_id,lineage_id FROM job_template_cluster_lineage"
        )
    }

    def resolve(cluster_id: str) -> str:
        path: list[str] = []
        current = cluster_id
        while aliases.get(current, current) != current:
            if current in path:
                raise ValueError("template lineage contains a cycle")
            path.append(current)
            current = aliases[current]
        for value in path:
            aliases[value] = current
        return current

    tables = {
        str(row[0])
        for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    old_by_family: dict[str, str] = {}
    old_by_member: dict[tuple[str, str], str] = {}
    if {"job_template_clusters", "job_family_members"} <= tables:
        rows = con.execute(
            "SELECT tc.family_id,tc.template_cluster_id,m.ats,m.job_id "
            "FROM job_template_clusters tc LEFT JOIN job_family_members m "
            "ON m.family_id=tc.family_id"
        ).fetchall()
        for family_id, cluster_id, ats, job_id in rows:
            cluster = str(cluster_id)
            aliases.setdefault(cluster, cluster)
            old_by_family[str(family_id)] = cluster
            if ats is not None and job_id is not None:
                old_by_member[(str(ats), str(job_id))] = cluster

    members_by_family: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for ats, job_id, family_id, _ in member_rows:
        members_by_family[str(family_id)].append((str(ats), str(job_id)))
    components: dict[str, list[str]] = defaultdict(list)
    for family_id, provisional in provisional_by_family.items():
        components[provisional].append(family_id)

    lineage_parent = {
        resolve(lineage): resolve(lineage) for lineage in aliases.values()
    }

    def find_lineage(lineage: str) -> str:
        lineage_parent.setdefault(lineage, lineage)
        current = lineage
        while lineage_parent[current] != current:
            lineage_parent[current] = lineage_parent[lineage_parent[current]]
            current = lineage_parent[current]
        return current

    def merge_lineages(lineages: set[str]) -> str:
        roots = {find_lineage(lineage) for lineage in lineages}
        root = min(roots)
        for value in roots:
            lineage_parent[value] = root
        return root

    assigned: dict[str, str] = {}
    for provisional, families in sorted(components.items()):
        prior_roots: set[str] = set()
        if provisional in aliases:
            prior_roots.add(find_lineage(resolve(provisional)))
        for family_id in families:
            previous = old_by_family.get(family_id)
            if previous:
                prior_roots.add(find_lineage(resolve(previous)))
            for member in members_by_family.get(family_id, []):
                previous = old_by_member.get(member)
                if previous:
                    prior_roots.add(find_lineage(resolve(previous)))
        if prior_roots:
            root = merge_lineages(prior_roots)
        else:
            root = provisional
            lineage_parent.setdefault(root, root)
        for family_id in families:
            assigned[family_id] = root

    # A merge processed after a split may have redirected an earlier assignment.
    assigned = {
        family_id: find_lineage(lineage) for family_id, lineage in assigned.items()
    }
    aliases = {
        alias: find_lineage(resolve(lineage)) for alias, lineage in aliases.items()
    }
    for lineage in assigned.values():
        aliases.setdefault(lineage, lineage)
    con.executemany(
        "INSERT INTO job_template_cluster_lineage "
        "(template_cluster_id,lineage_id,normalization_version) VALUES (?,?,?) "
        "ON CONFLICT(template_cluster_id) DO UPDATE SET "
        "lineage_id=excluded.lineage_id,"
        "normalization_version=excluded.normalization_version",
        [
            (alias, lineage, NORMALIZATION_VERSION)
            for alias, lineage in sorted(aliases.items())
        ],
    )
    return assigned


def _require_jobs_schema(con: sqlite3.Connection) -> None:
    exists = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'"
    ).fetchone()
    if not exists:
        raise ValueError("database does not contain a jobs table")
    columns = {row[1] for row in con.execute("PRAGMA table_info(jobs)")}
    required = {"ats", "id", "company", "title", "description"}
    missing = sorted(required - columns)
    if missing:
        raise ValueError(f"jobs table is missing required columns: {', '.join(missing)}")


def _build_rows(
    rows: Iterable[sqlite3.Row],
) -> tuple[list[dict[str, object]], list[tuple[str, str, str, str]]]:
    # Keep one normalized description per exact key, not another full description
    # for every member. On a full database this avoids retaining several copies of
    # hundreds of thousands of multi-kilobyte descriptions.
    grouped: dict[tuple[str, ...], list[tuple[str, str, str]]] = defaultdict(list)
    for row in rows:
        raw = {
            "ats": "" if row["ats"] is None else str(row["ats"]),
            "id": "" if row["id"] is None else str(row["id"]),
            "company": "" if row["company"] is None else str(row["company"]),
            "title": "" if row["title"] is None else str(row["title"]),
            "description": "" if row["description"] is None else str(row["description"]),
        }
        if not raw["ats"] or not raw["id"]:
            continue
        normalized = {
            "ats": normalize_text(raw["ats"]),
            "company": normalize_text(raw["company"]),
            "title": normalize_text(raw["title"]),
            "description": normalize_text(raw["description"]),
        }
        base_key = (
            normalized["ats"], normalized["company"], normalized["title"],
            normalized["description"],
        )
        # Thin/error postings are deliberately never collapsed or grouped for leakage.
        if len(normalized["description"]) < MIN_DESCRIPTION_CHARS:
            key = (*base_key, "singleton", raw["ats"], raw["id"])
        else:
            key = base_key
        source_fingerprint = _fingerprint(
            "source-job",
            [raw["ats"], raw["id"], raw["company"], raw["title"], raw["description"]],
        )
        grouped[key].append((raw["ats"], raw["id"], source_fingerprint))

    family_rows: list[dict[str, object]] = []
    member_rows: list[tuple[str, str, str, str]] = []
    for key, members in sorted(grouped.items()):
        ordered_members = sorted(members)
        canonical_ats, canonical_job_id, _ = ordered_members[0]
        ats, company, title, description = key[:4]
        family_fingerprint = _fingerprint("exact-family", key)
        family_id = f"fam_{family_fingerprint}"
        description_fingerprint = _fingerprint("description", [description])
        family_rows.append({
            "family_id": family_id,
            "family_fingerprint": family_fingerprint,
            "normalization_version": NORMALIZATION_VERSION,
            "ats": ats,
            "company_normalized": company,
            "title_normalized": title,
            "description_fingerprint": description_fingerprint,
            "description_normalized": description,
            "canonical_ats": canonical_ats,
            "canonical_job_id": canonical_job_id,
            "member_count": len(ordered_members),
        })
        member_rows.extend(
            (member_ats, job_id, family_id, source_fingerprint)
            for member_ats, job_id, source_fingerprint in ordered_members
        )
    return family_rows, member_rows


def _upsert_derived_rows(
    con: sqlite3.Connection,
    family_rows: Sequence[dict[str, object]],
    member_rows: Sequence[tuple[str, str, str, str]],
    template_rows: Sequence[tuple[str, str, str, int]],
) -> None:
    for statement in _SCHEMA:
        con.execute(statement)

    con.execute("CREATE TEMP TABLE next_family_ids (family_id TEXT PRIMARY KEY)")
    con.execute(
        "CREATE TEMP TABLE next_member_ids (ats TEXT, job_id TEXT, PRIMARY KEY(ats,job_id))"
    )
    con.executemany(
        "INSERT INTO next_family_ids VALUES (?)",
        [(row["family_id"],) for row in family_rows],
    )
    con.executemany(
        "INSERT INTO next_member_ids VALUES (?,?)",
        [(ats, job_id) for ats, job_id, _, _ in member_rows],
    )

    con.executemany(
        "INSERT INTO job_families "
        "(family_id,family_fingerprint,normalization_version,ats,company_normalized,"
        "title_normalized,description_fingerprint,canonical_ats,canonical_job_id,member_count) "
        "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(family_id) DO UPDATE SET "
        "family_fingerprint=excluded.family_fingerprint,"
        "normalization_version=excluded.normalization_version,ats=excluded.ats,"
        "company_normalized=excluded.company_normalized,title_normalized=excluded.title_normalized,"
        "description_fingerprint=excluded.description_fingerprint,canonical_ats=excluded.canonical_ats,"
        "canonical_job_id=excluded.canonical_job_id,member_count=excluded.member_count",
        [
            (
                row["family_id"], row["family_fingerprint"], row["normalization_version"],
                row["ats"], row["company_normalized"], row["title_normalized"],
                row["description_fingerprint"], row["canonical_ats"],
                row["canonical_job_id"], row["member_count"],
            )
            for row in family_rows
        ],
    )
    con.executemany(
        "INSERT INTO job_family_members (ats,job_id,family_id,source_fingerprint) "
        "VALUES (?,?,?,?) ON CONFLICT(ats,job_id) DO UPDATE SET "
        "family_id=excluded.family_id,source_fingerprint=excluded.source_fingerprint",
        member_rows,
    )
    con.executemany(
        "INSERT INTO job_template_clusters "
        "(family_id,template_cluster_id,leakage_group_id,normalization_version) "
        "VALUES (?,?,?,?) ON CONFLICT(family_id) DO UPDATE SET "
        "template_cluster_id=excluded.template_cluster_id,"
        "leakage_group_id=excluded.leakage_group_id,"
        "normalization_version=excluded.normalization_version",
        template_rows,
    )

    con.execute(
        "DELETE FROM job_template_clusters WHERE family_id NOT IN "
        "(SELECT family_id FROM next_family_ids)"
    )
    con.execute(
        "DELETE FROM job_family_members WHERE NOT EXISTS "
        "(SELECT 1 FROM next_member_ids n WHERE n.ats=job_family_members.ats "
        "AND n.job_id=job_family_members.job_id)"
    )
    con.execute(
        "DELETE FROM job_families WHERE family_id NOT IN "
        "(SELECT family_id FROM next_family_ids)"
    )


def prepared_families_are_current(db_path: str | Path) -> bool:
    """Verify raw source fingerprints before reusing expensive derived groups.

    This streams descriptions once without normalizing or comparing templates.
    New, edited, deleted, or reassigned jobs and old normalization versions force
    a rebuild, even if a caller changed content without updating its timestamp.
    """
    from contextlib import closing
    with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=ro', uri=True)) as con:
        con.execute('BEGIN')
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'jobs', 'job_families', 'job_family_members', 'job_template_clusters'} <= tables:
            return False
        if con.execute('SELECT 1 FROM job_families WHERE normalization_version IS NOT ? LIMIT 1', (NORMALIZATION_VERSION,)).fetchone():
            return False
        if con.execute('SELECT 1 FROM job_template_clusters WHERE normalization_version IS NOT ? LIMIT 1', (NORMALIZATION_VERSION,)).fetchone():
            return False
        jobs = con.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]
        members = con.execute('SELECT COUNT(*) FROM job_family_members').fetchone()[0]
        families = con.execute('SELECT COUNT(*) FROM job_families').fetchone()[0]
        if jobs != members or families != con.execute('SELECT COUNT(DISTINCT family_id) FROM job_family_members').fetchone()[0]:
            return False
        if families != con.execute('SELECT COUNT(*) FROM job_template_clusters').fetchone()[0]:
            return False
        if con.execute('SELECT 1 FROM job_families f LEFT JOIN job_family_members m '
                       'ON m.ats=f.canonical_ats AND m.job_id=f.canonical_job_id '
                       'LEFT JOIN job_template_clusters t ON t.family_id=f.family_id '
                       'WHERE m.family_id IS NULL OR m.family_id<>f.family_id OR t.family_id IS NULL LIMIT 1').fetchone():
            return False
        rows = con.execute('SELECT j.ats,j.id,j.company,j.title,j.description,m.source_fingerprint '
                           'FROM jobs j LEFT JOIN job_family_members m ON m.ats=j.ats AND m.job_id=j.id')
        for row in rows:
            raw = ['' if value is None else str(value) for value in row[:5]]
            if row[5] != _fingerprint('source-job', raw):
                return False
        return True


def prepare_families(db_path: str | Path) -> dict[str, int]:
    """Regenerate current opportunity-family metadata and return summary counts."""
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(path)
    with sqlite3.connect(path) as con:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        _require_jobs_schema(con)
        jobs = con.execute(
            "SELECT ats,id,company,title,description FROM jobs ORDER BY ats,id"
        )
        family_rows, member_rows = _build_rows(jobs)
        provisional_clusters = _template_components(family_rows)
        cluster_by_family = _stable_template_lineages(
            con, provisional_clusters, member_rows,
        )
        template_rows = []
        for family in family_rows:
            family_id = str(family["family_id"])
            description = str(family["description_normalized"])
            if description:
                leakage_id = _identifier("leak", "exact-description", [description])
            else:
                leakage_id = _identifier("leak", "empty-singleton", [family_id])
            template_rows.append(
                (family_id, cluster_by_family[family_id], leakage_id, NORMALIZATION_VERSION)
            )
        _upsert_derived_rows(con, family_rows, member_rows, template_rows)

    return {
        "jobs": len(member_rows),
        "families": len(family_rows),
        "collapsed_jobs": len(member_rows) - len(family_rows),
        "template_clusters": len({row[1] for row in template_rows}),
        "leakage_groups": len({row[2] for row in template_rows}),
    }


def audit_clusters(db_path: str | Path, limit: int = 200) -> dict[str, object]:
    """Return a deterministic, bounded review of the largest fuzzy clusters.

    The source database is opened in SQLite read-only mode. Exact opportunity
    families remain represented separately inside each template cluster so a
    reviewer can spot false fuzzy merges without exporting complete descriptions.
    """
    if limit < 1:
        raise ValueError("audit limit must be positive")
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(path)
    uri = "file:" + quote(str(path.resolve())) + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as con:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA query_only=ON")
        tables = {
            str(row[0])
            for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        required = {"jobs", "job_families", "job_family_members", "job_template_clusters"}
        missing = sorted(required - tables)
        if missing:
            raise ValueError(
                "database is missing derived dedupe tables; run `job_search/collection/dedupe.py "
                "--db DB prepare` first"
            )
        eligible_clusters = int(con.execute(
            "SELECT COUNT(*) FROM ("
            "SELECT template_cluster_id FROM job_template_clusters "
            "GROUP BY template_cluster_id HAVING COUNT(*)>1)"
        ).fetchone()[0])
        cluster_rows = con.execute(
            "SELECT tc.template_cluster_id,COUNT(*) AS family_count,"
            "SUM(f.member_count) AS job_count "
            "FROM job_template_clusters tc JOIN job_families f "
            "ON f.family_id=tc.family_id "
            "GROUP BY tc.template_cluster_id HAVING COUNT(*)>1 "
            "ORDER BY family_count DESC,job_count DESC,tc.template_cluster_id LIMIT ?",
            (limit,),
        ).fetchall()
        cluster_ids = [str(row["template_cluster_id"]) for row in cluster_rows]
        families_by_cluster: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for start in range(0, len(cluster_ids), 400):
            group = cluster_ids[start:start + 400]
            marks = ",".join("?" for _ in group)
            rows = con.execute(
                "SELECT tc.template_cluster_id,f.family_id,f.company_normalized,"
                "f.title_normalized,f.canonical_ats,f.canonical_job_id,f.member_count,"
                "j.company,j.title,j.description "
                "FROM job_template_clusters tc JOIN job_families f "
                "ON f.family_id=tc.family_id JOIN jobs j "
                "ON j.ats=f.canonical_ats AND j.id=f.canonical_job_id "
                f"WHERE tc.template_cluster_id IN ({marks}) "
                "ORDER BY tc.template_cluster_id,f.family_id",
                group,
            ).fetchall()
            for row in rows:
                families_by_cluster[str(row["template_cluster_id"])].append(row)

        family_ids = [
            str(family["family_id"])
            for cluster_id in cluster_ids
            for family in families_by_cluster[cluster_id]
        ]
        members_by_family: dict[str, list[dict[str, str]]] = defaultdict(list)
        for start in range(0, len(family_ids), 400):
            group = family_ids[start:start + 400]
            marks = ",".join("?" for _ in group)
            rows = con.execute(
                "SELECT family_id,ats,job_id FROM job_family_members "
                f"WHERE family_id IN ({marks}) ORDER BY family_id,ats,job_id",
                group,
            ).fetchall()
            for row in rows:
                family_id = str(row["family_id"])
                if len(members_by_family[family_id]) < AUDIT_MEMBER_IDS:
                    members_by_family[family_id].append({
                        "ats": str(row["ats"]), "id": str(row["job_id"]),
                    })

    clusters = []
    for cluster_row in cluster_rows:
        cluster_id = str(cluster_row["template_cluster_id"])
        families = []
        for row in families_by_cluster[cluster_id]:
            description = " ".join(str(row["description"] or "").split())
            family_id = str(row["family_id"])
            member_count = int(row["member_count"])
            member_ids = members_by_family[family_id]
            families.append({
                "family_id": family_id,
                "canonical_ats": str(row["canonical_ats"]),
                "canonical_job_id": str(row["canonical_job_id"]),
                "canonical_company": str(row["company"] or ""),
                "canonical_title": str(row["title"] or ""),
                "company_normalized": str(row["company_normalized"]),
                "title_normalized": str(row["title_normalized"]),
                "description_chars": len(description),
                "description_excerpt": description[:AUDIT_EXCERPT_CHARS],
                "description_excerpt_truncated": len(description) > AUDIT_EXCERPT_CHARS,
                "member_count": member_count,
                "member_ids": member_ids,
                "member_ids_truncated": member_count > len(member_ids),
            })
        clusters.append({
            "template_cluster_id": cluster_id,
            "family_count": int(cluster_row["family_count"]),
            "job_count": int(cluster_row["job_count"]),
            "families": families,
        })
    return {
        "format_version": 1,
        "normalization_version": NORMALIZATION_VERSION,
        "limit": limit,
        "eligible_cluster_count": eligible_clusters,
        "returned_cluster_count": len(clusters),
        "returned_family_count": sum(cluster["family_count"] for cluster in clusters),
        "returned_job_count": sum(cluster["job_count"] for cluster in clusters),
        "clusters": clusters,
    }


def main(argv: Sequence[str] | None = None) -> None:
    def positive_int(value: str) -> int:
        parsed = int(value)
        if parsed < 1:
            raise argparse.ArgumentTypeError("must be a positive integer")
        return parsed

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="SQLite jobs database")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("prepare", help="build or refresh derived family tables")
    audit = subparsers.add_parser(
        "audit", help="export the largest multi-family fuzzy clusters for review",
    )
    audit.add_argument(
        "--limit", type=positive_int, default=200,
        help="maximum clusters (default: 200)",
    )
    audit.add_argument("--output", type=Path, help="write JSON to this path instead of stdout")
    args = parser.parse_args(argv)
    if args.command == "prepare":
        print(json.dumps(prepare_families(args.db), sort_keys=True))
    else:
        if args.output and args.output.resolve() == args.db.resolve():
            parser.error("--output must not overwrite the source database")
        result = audit_clusters(args.db, args.limit)
        payload = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        if args.output:
            args.output.write_text(payload, encoding="utf-8")
        else:
            print(payload, end="")


if __name__ == "__main__":
    main()
