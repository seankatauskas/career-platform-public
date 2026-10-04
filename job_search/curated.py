"""Durable agent-selected lists. Selection and catalog queries belong to the caller."""
from __future__ import annotations

import json
import secrets
from contextlib import closing
from typing import Any, Mapping

from .contracts import ContractError, canonical_json, parse_utc, payload_sha256, utc_now, validate_identifier
from .db import connect

POLICY = "curated"
SNAPSHOT_FIELDS = ("ats", "id", "title", "company", "location", "employmentType", "jobUrl",
                   "family_id", "posted_at", "source_updated_at", "publishedAt", "first_seen", "closed_at")


def validate_publication(value: Mapping[str, Any]) -> dict:
    if not isinstance(value, Mapping) or set(value) - {"title", "window_start", "window_end", "idempotency_key", "jobs"}:
        raise ContractError("invalid shortlist fields")
    result = dict(value)
    for name, maximum in (("title", 200), ("idempotency_key", 255)):
        raw = result.get(name)
        if not isinstance(raw, str) or not raw.strip() or len(raw) > maximum:
            raise ContractError(f"{name} must be nonempty text of at most {maximum} characters")
        result[name] = raw.strip()
    validate_identifier(result["idempotency_key"], "idempotency_key")
    for name in ("window_start", "window_end"):
        raw = result.get(name)
        if raw is not None:
            if not isinstance(raw, str):
                raise ContractError(f"{name} must be a UTC timestamp")
            parse_utc(raw)
    if bool(result.get("window_start")) != bool(result.get("window_end")):
        raise ContractError("provide both window_start and window_end")
    if result.get("window_start") and parse_utc(result["window_start"]) >= parse_utc(result["window_end"]):
        raise ContractError("window_start must precede window_end")
    jobs = result.get("jobs")
    if not isinstance(jobs, list) or len(jobs) > 500:
        raise ContractError("jobs must be an ordered list of up to 500 entries")
    seen = set()
    for entry in jobs:
        if not isinstance(entry, dict) or set(entry) - {"ats", "job_id", "explanation"}:
            raise ContractError("invalid job entry fields")
        if not isinstance(entry.get("ats"), str) or entry["ats"] not in {"ashby", "greenhouse", "lever"}:
            raise ContractError("unknown ats")
        if not isinstance(entry.get("job_id"), str):
            raise ContractError("job_id must be text")
        validate_identifier(entry["job_id"], "job_id")
        reason = entry.get("explanation", "")
        if not isinstance(reason, str) or len(reason) > 2000:
            raise ContractError("explanation must be text of at most 2000 characters")
        identity = (entry["ats"], entry["job_id"])
        if identity in seen:
            raise ContractError("duplicate job in shortlist")
        seen.add(identity)
    return result


class CuratedShortlists:
    def __init__(self, db_path, catalog):
        self.db_path, self.catalog = db_path, catalog

    def publish(self, supplied: Mapping[str, Any]) -> dict:
        request = validate_publication(supplied)
        with closing(connect(self.db_path)) as con, con:
            con.execute("BEGIN IMMEDIATE")
            return self.publish_in_transaction(con, request)

    def publish_in_transaction(self, con, supplied, *, snapshots=None):
        """Share a caller-owned transaction for atomic multi-list review publication."""
        request = validate_publication(supplied)
        digest = payload_sha256(request)
        prior = con.execute("SELECT * FROM curated_shortlists WHERE idempotency_key=?", (request['idempotency_key'],)).fetchone()
        if prior:
            if prior['request_sha256'] != digest:
                raise ContractError("idempotency key was reused for a different shortlist")
            return self._receipt(prior)
        if snapshots is None:
            snapshots = []
            for entry in request['jobs']:
                job = self.catalog.get_job(entry['ats'], entry['job_id'])
                snapshots.append({key: job.get(key) for key in SNAPSHOT_FIELDS})
        if len(snapshots) != len(request['jobs']):
            raise ContractError('publication snapshots do not match jobs')
        list_id = 'curated_' + secrets.token_hex(16)
        con.execute("INSERT INTO curated_shortlists (list_id,title,window_start,window_end,idempotency_key,request_sha256,created_at,job_count) VALUES (?,?,?,?,?,?,?,?)",
                    (list_id, request['title'], request.get('window_start'), request.get('window_end'), request['idempotency_key'], digest, utc_now(), len(snapshots)))
        for rank, (entry, snapshot) in enumerate(zip(request['jobs'], snapshots), 1):
            con.execute("INSERT INTO curated_shortlist_items VALUES (?,?,?,?,?,?)",
                        (list_id, rank, entry['ats'], entry['job_id'], entry.get('explanation', ''), canonical_json(snapshot)))
        return self._receipt(con.execute("SELECT * FROM curated_shortlists WHERE list_id=?", (list_id,)).fetchone())
    @staticmethod
    def _receipt(row) -> dict:
        return {key: row[key] for key in ('list_id', 'title', 'created_at', 'job_count')} | {'dashboard_path': '#shortlist/' + row['list_id']}

    def lists(self, before: int | None = None) -> dict:
        with closing(connect(self.db_path)) as con:
            rows = con.execute("SELECT sequence,list_id,title,created_at,job_count,window_start,window_end FROM curated_shortlists "
                               + ("WHERE sequence<? " if before is not None else "") + "ORDER BY sequence DESC LIMIT 101", (() if before is None else (before,))).fetchall()
        return {'lists': [dict(row) for row in rows[:100]], 'next_before': rows[99]['sequence'] if len(rows) > 100 else None}

    def get(self, list_id: str) -> dict:
        validate_identifier(list_id, 'list_id')
        with closing(connect(self.db_path)) as con:
            row = con.execute("SELECT list_id,title,created_at,job_count,window_start,window_end FROM curated_shortlists WHERE list_id=?", (list_id,)).fetchone()
            if row is None:
                raise ContractError('saved shortlist was not found')
            items = con.execute("SELECT * FROM curated_shortlist_items WHERE list_id=? ORDER BY rank", (list_id,)).fetchall()
            jobs = []
            for item in items:
                job = json.loads(item['snapshot_json'])
                job.update(rank=item['rank'], explanation=item['explanation'], curated_list_id=list_id, policy_id=POLICY)
                application = con.execute("SELECT application_id,current_phase FROM applications WHERE ats=? AND job_id=?", (item['ats'], item['job_id'])).fetchone()
                job['application_id'] = application['application_id'] if application else None
                job['application_phase'] = application['current_phase'] if application else None
                jobs.append(job)
        return {**dict(row), 'source': 'curated', 'recommendations': jobs}

    def item(self, list_id: str, ats: str, job_id: str) -> dict:
        for job in self.get(list_id)['recommendations']:
            if job['ats'] == ats and job['id'] == job_id:
                return job
        raise ContractError('job is not in this saved shortlist')
