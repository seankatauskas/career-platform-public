"""Exact job identity and durable, evidence-based browser application tracking.

This module receives bounded observations from a registered extension. It never
requests an employer URL or submits a form. Browser job discoveries live in the
private ledger, independently of collector coverage and freshness assertions.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time
import uuid
from urllib.parse import parse_qs, unquote, urlsplit

from .autofill import ATS_HOSTS, validate_extension_origin, validate_descriptors, validate_captured_answers
from .contracts import (ApplicationEventType, ContractError, ConflictError, JobSnapshot,
                        MutationContext, RecommendationProvenance, canonical_json,
                        parse_utc, payload_sha256, utc_now)
from .db import connect

KINDS = frozenset({'attempted', 'request_sent', 'request_completed', 'site_acknowledged', 'failed'})
LABELS = {'attempted': 'Submission attempted', 'request_sent': 'Request sent · unconfirmed',
          'site_acknowledged': 'Submitted · awaiting email', 'email_confirmed': 'Email confirmed',
          'failed': 'Submission failed', 'unresolved': 'Submission needs review'}


def identify_job(page_url: str) -> dict:
    """Parse only supported ATS URL grammars, never arbitrary path substrings."""
    if not isinstance(page_url, str) or len(page_url) > 4096:
        raise ContractError('invalid application URL')
    u = urlsplit(page_url)
    if u.scheme != 'https' or u.username or u.password or u.port not in (None, 443):
        raise ContractError('application URL must be an HTTPS ATS page')
    host = (u.hostname or '').lower()
    ats = next((key for key, hosts in ATS_HOSTS.items() if host in hosts), '')
    if not ats:
        raise ContractError('unsupported application site')
    parts = [unquote(p) for p in u.path.split('/') if p]
    board = job_id = ''
    if ats in {'ashby', 'lever'} and len(parts) in (2, 3):
        board, job_id = parts[:2]
        if len(parts) == 3 and parts[2] not in {'application', 'apply', 'thanks'}:
            raise ContractError('unsupported application path')
        if not re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', job_id):
            raise ContractError('invalid ATS posting ID')
        job_id = job_id.lower()
    elif ats == 'greenhouse':
        if (len(parts) == 3 or (len(parts) == 4 and parts[3] == 'confirmation')) and parts[1] == 'jobs':
            board, job_id = parts[0], parts[2]
        elif parts in (['embed', 'job_app'], ['embed', 'job_board', 'job']):
            q = parse_qs(u.query)
            board = (q.get('for') or [''])[0]
            job_id = (q.get('token') or q.get('gh_jid') or [''])[0]
        if not re.fullmatch(r'\d+', job_id):
            raise ContractError('invalid Greenhouse posting ID')
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,200}', board) or not job_id:
        raise ContractError('application URL lacks a board and job ID')
    canonical = f'https://{host}/{board}/jobs/{job_id}' if ats == 'greenhouse' else f'https://{host}/{board}/{job_id}'
    return {'ats': ats, 'job_id': job_id, 'board': board, 'canonical_url': canonical}


def _text(value, maximum=500):
    if not isinstance(value, str) or len(value) > maximum:
        raise ContractError('invalid browser metadata')
    return ' '.join(value.split())


class BrowserTracking:
    def __init__(self, ledger, catalog, autofill, resume_lab=None):
        self.ledger, self.catalog, self.autofill, self.resume_lab = ledger, catalog, autofill, resume_lab
        self.path = ledger.store.db_path

    def issue_pairing(self, audience):
        code = secrets.token_urlsafe(32)
        with connect(self.path) as con:
            con.execute('DELETE FROM browser_pairings WHERE expires_at<?', (time.time(),))
            con.execute('INSERT INTO browser_pairings VALUES (?,?,?)',
                        (hashlib.sha256(code.encode()).hexdigest(), audience, time.time()+300))
        return {'pairing_code': code, 'expires_in_seconds': 300}

    def enroll(self, code, origin, audience):
        origin = validate_extension_origin(origin)
        token, device = secrets.token_urlsafe(32), uuid.uuid4().hex
        with connect(self.path) as con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('SELECT * FROM browser_pairings WHERE code_hash=?',
                              (hashlib.sha256(str(code).encode()).hexdigest(),)).fetchone()
            if not row or row['expires_at'] <= time.time() or row['audience'] != audience:
                raise ContractError('browser connection code is invalid or expired')
            con.execute('DELETE FROM browser_pairings WHERE code_hash=?', (row['code_hash'],))
            con.execute('INSERT INTO browser_devices VALUES (?,?,?,?,?,NULL)',
                        (device, hashlib.sha256(token.encode()).hexdigest(), origin, audience, utc_now()))
        return {'device_id': device, 'device_token': token, 'connected': True}

    def authenticate(self, token, origin, audience):
        validate_extension_origin(origin)
        with connect(self.path) as con:
            row = con.execute('SELECT * FROM browser_devices WHERE token_hash=?',
                              (hashlib.sha256(str(token).encode()).hexdigest(),)).fetchone()
        if not row or row['revoked_at'] or row['extension_origin'] != origin or row['audience'] != audience:
            raise ContractError('browser connection is invalid or revoked; reconnect in Settings')
        return row['device_id']

    def devices(self):
        with connect(self.path) as con:
            return [dict(r) for r in con.execute('SELECT device_id,extension_origin,created_at,revoked_at FROM browser_devices ORDER BY created_at')]

    def revoke(self, device):
        with connect(self.path) as con:
            con.execute('UPDATE browser_devices SET revoked_at=? WHERE device_id=?', (utc_now(), device))
        return {'revoked': True}

    def resolve(self, page_url):
        identity = identify_job(page_url)
        with connect(self.path) as con:
            app = con.execute('SELECT * FROM applications WHERE ats=? AND job_id=?',
                              (identity['ats'], identity['job_id'])).fetchone()
            discovered = con.execute('SELECT snapshot_json FROM browser_jobs WHERE ats=? AND job_id=?',
                                    (identity['ats'], identity['job_id'])).fetchone()
        job = None
        if self.catalog is not None:
            try:
                job = self.catalog.get_job(identity['ats'], identity['job_id'])
            except ContractError as exc:
                if str(exc) != 'job was not found':
                    raise
        # Greenhouse can store a custom company URL. A canonical ATS URL, when
        # present, must agree with the observed board as well as posting ID.
        reference = (job or {}).get('jobUrl') or (app['job_url_snapshot'] if app else '')
        if reference:
            try:
                known = identify_job(reference)
            except ContractError:
                known = None
            if known and known['board'].casefold() != identity['board'].casefold():
                raise ConflictError('posting board does not match the database job')
        snapshot = json.loads(discovered['snapshot_json']) if discovered else None
        if job:
            snapshot = {'title': str(job['title']), 'employer': str(job['company']),
                        'company_slug': str(job['company']), 'job_url': str(job.get('jobUrl') or identity['canonical_url']),
                        'family_id': ''}
        if app:
            snapshot = {'title': app['title_snapshot'], 'employer': app['employer_snapshot'],
                        'company_slug': app['company_slug_snapshot'], 'job_url': app['job_url_snapshot'],
                        'family_id': app['family_id']}
        return {**identity, 'known': bool(job or app or discovered), 'snapshot': snapshot,
                'application_id': app['application_id'] if app else None,
                'status': self.application_status(app['application_id']) if app else None}

    def assignments(self, page_url, fields):
        identity = identify_job(page_url)
        fields = validate_descriptors(fields)
        return {'assignments': [*self.autofill._profile.assignments(identity['ats'], fields),
                                *self.autofill._vault.assignments(identity['ats'], fields)]}

    def resume_attachment(self, page_url):
        resolved = self.resolve(page_url)
        if self.resume_lab is None:
            return {'resume': None}
        artifact = self.resume_lab.get_autofill_resume(resolved['application_id'])
        if artifact is None:
            return {'resume': None}
        if (artifact.content_type != 'application/pdf' or not artifact.content.startswith(b'%PDF-')
                or not 0 < len(artifact.content) <= 20 * 1024 * 1024
                or hashlib.sha256(artifact.content).hexdigest() != artifact.sha256):
            raise ContractError('saved resume PDF failed verification')
        contact = self.autofill._profile.contact
        name = contact.get('full_name') or ' '.join(
            contact.get(key, '') for key in ('first_name', 'last_name'))
        name = re.sub(r'[^\w-]+', '_', name, flags=re.UNICODE).strip('_-')[:120]
        filename = f'{name}_Resume.pdf' if name else 'Resume.pdf'
        return {'resume': {'filename': filename, 'sha256': artifact.sha256,
                           'content_base64': base64.b64encode(artifact.content).decode('ascii')}}

    def observe(self, device, body):
        allowed = {'device_token', 'observation_id', 'attempt_id', 'page_url', 'kind', 'occurred_at',
                   'title', 'employer', 'metadata', 'resume_sha256'}
        if set(body) - allowed:
            raise ContractError('unsupported observation fields')
        oid, aid = body.get('observation_id'), body.get('attempt_id')
        for value in (oid, aid):
            if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{16,80}', value):
                raise ContractError('invalid browser observation identity')
        kind = body.get('kind')
        if kind not in KINDS:
            raise ContractError('unsupported observation kind')
        stamp = str(body.get('occurred_at') or '')
        parsed = parse_utc(stamp)
        if parsed.timestamp() > time.time()+300:
            raise ContractError('observation is in the future')
        metadata = body.get('metadata') or {}
        if not isinstance(metadata, dict) or set(metadata) - {'signal', 'request_status', 'adapter_version'}:
            raise ContractError('invalid observation metadata')
        metadata = {k: _text(v, 120) for k, v in metadata.items()}
        if kind == 'site_acknowledged' and metadata.get('signal') not in {'success_dom', 'success_route'}:
            raise ContractError('site acknowledgement requires a supported success signal')
        digest = str(body.get('resume_sha256') or '')
        if digest and not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise ContractError('invalid resume fingerprint')
        identity = self.resolve(body.get('page_url'))
        normalized = {k: body[k] for k in body if k != 'device_token'}
        request_hash = payload_sha256(normalized)
        with connect(self.path) as con:
            prior = con.execute('SELECT o.*,a.device_id FROM browser_observations o JOIN browser_attempts a USING(attempt_id) WHERE observation_id=?', (oid,)).fetchone()
            if prior:
                if prior['device_id'] != device or prior['payload_sha256'] != request_hash:
                    raise ConflictError('observation ID was reused with different data')
                return self.attempt_status(device, aid)
            attempt = con.execute('SELECT * FROM browser_attempts WHERE attempt_id=?', (aid,)).fetchone()
        if attempt and attempt['device_id'] != device:
            raise ConflictError('attempt belongs to another browser')
        if not attempt and kind != 'attempted':
            raise ConflictError('record the submission attempt before its outcome')
        app_id = identity['application_id']
        if not app_id:
            snapshot = identity['snapshot'] or {'title': _text(body.get('title') or 'Job application'),
                'employer': _text(body.get('employer') or identity['board']),
                'company_slug': identity['board'], 'job_url': identity['canonical_url'], 'family_id': ''}
            result = self.ledger.start_application(JobSnapshot(ats=identity['ats'], job_id=identity['job_id'], **snapshot),
                RecommendationProvenance(), MutationContext('browser-start:'+aid, 'system', 'browser_extension', device))
            app_id = result['application']['application_id']
            if not identity['known']:
                with connect(self.path) as con:
                    con.execute('INSERT OR IGNORE INTO browser_jobs VALUES (?,?,?,?)',
                        (identity['ats'], identity['job_id'], canonical_json(snapshot), utc_now()))
        if attempt and attempt['application_id'] != app_id:
            raise ConflictError('attempt cannot move to another job')
        try:
            resume = self.resume_fingerprint(digest, app_id) if digest else {'decision': 'not_tracked', 'reason': 'upload_not_observed'}
        except Exception:
            resume = {'decision': 'not_tracked', 'reason': 'resume_lookup_unavailable', 'sha256': digest}
        with connect(self.path) as con:
            con.execute('BEGIN IMMEDIATE')
            con.execute('INSERT OR IGNORE INTO browser_attempts VALUES (?,?,?,?,?,?,?,?)',
                (aid, device, app_id, 'attempted', stamp, stamp, digest, canonical_json(resume)))
            row = con.execute('SELECT device_id,application_id FROM browser_attempts WHERE attempt_id=?', (aid,)).fetchone()
            if row['device_id'] != device or row['application_id'] != app_id:
                raise ConflictError('attempt identity conflict')
            prior = con.execute('SELECT * FROM browser_observations WHERE observation_id=?', (oid,)).fetchone()
            if prior:
                if prior['payload_sha256'] != request_hash:
                    raise ConflictError('observation ID conflict')
            else:
                con.execute('INSERT INTO browser_observations VALUES (?,?,?,?,?,?,?)',
                    (oid, aid, kind, stamp, utc_now(), request_hash, canonical_json(metadata)))
            row = con.execute('SELECT * FROM browser_attempts WHERE attempt_id=?', (aid,)).fetchone()
            status = row['status']
            if status != 'site_acknowledged':
                if kind == 'site_acknowledged' or kind == 'failed':
                    status = kind
                elif status != 'failed' and kind in {'request_sent', 'request_completed'}:
                    status = 'request_sent'
            con.execute('UPDATE browser_attempts SET status=?,updated_at=? WHERE attempt_id=?', (status, stamp, aid))
            if kind == 'site_acknowledged':
                self._finalize(con, app_id, aid, stamp, json.loads(row['resume_json']))
        # Answer-capture failures must never prevent acknowledging a committed observation.
        try:
            self.maintain_captures()
        except Exception:
            pass
        return self.attempt_status(device, aid)

    def _finalize(self, con, app_id, source, stamp, resume):
        app = self.ledger.store._application(con, app_id)
        if app['current_phase'] == 'terminal':
            return
        fresh = con.execute('INSERT OR IGNORE INTO browser_finalizations VALUES (?,?,?)', (app_id, source, stamp)).rowcount
        if not fresh or app['submitted_at']:
            return
        event, created = self.ledger.store._append_event(con, app_id, ApplicationEventType.SUBMISSION_OBSERVED,
            stamp, {'observed_by': 'automatic_extension', 'resume': resume}, 'browser-submission:'+app_id,
            MutationContext('browser-finalize:'+app_id, 'system', 'browser_extension', source), utc_now())
        if created:
            self.ledger.store._project(con, app_id)
            self.ledger.store._insert_feedback_outbox(con, self.ledger.store._application(con, app_id), event, utc_now())

    def resume_fingerprint(self, digest, app_id):
        if self.resume_lab and self.autofill._submission_context:
            try:
                selection = self.resume_lab.get_selection(app_id).get('selection')
                if selection and self.resume_lab.get_artifact(selection['artifact_id']).sha256 == digest:
                    return self.autofill._submission_context(app_id, 'selected')['resume']
            except Exception:
                pass
        if self.resume_lab and hasattr(self.resume_lab, 'match_uploaded_resume'):
            result = self.resume_lab.match_uploaded_resume(digest)
            if result:
                return result
        return {'decision': 'not_tracked', 'reason': 'unknown_upload', 'sha256': digest}

    def attempt_status(self, device, attempt):
        with connect(self.path) as con:
            row = con.execute('SELECT * FROM browser_attempts WHERE attempt_id=? AND device_id=?', (attempt, device)).fetchone()
        if not row:
            raise ContractError('unknown browser attempt')
        return {**self.application_status(row['application_id']),
                'attempt_id': attempt, 'application_id': row['application_id']}

    def evidence(self, app_id):
        with connect(self.path) as con:
            return [dict(row) for row in con.execute(
                "SELECT o.kind,o.occurred_at,o.metadata_json FROM browser_observations o JOIN browser_attempts a USING(attempt_id) WHERE a.application_id=? ORDER BY o.occurred_at DESC,o.rowid DESC LIMIT 100", (app_id,))]

    def application_status(self, app_id):
        with connect(self.path) as con:
            app = con.execute('SELECT confirmed_at,submitted_at,current_phase FROM applications WHERE application_id=?', (app_id,)).fetchone()
            row = con.execute('SELECT * FROM browser_attempts WHERE application_id=? ORDER BY created_at DESC LIMIT 1', (app_id,)).fetchone()
        if not row or not app:
            return None
        status = 'email_confirmed' if app['confirmed_at'] else 'site_acknowledged' if app['submitted_at'] else row['status']
        if status in {'attempted', 'request_sent'} and time.time()-parse_utc(row['updated_at']).timestamp() >= 600:
            status = 'unresolved'
        return {'status': status, 'label': LABELS[status], 'attempt_id': row['attempt_id']}

    def stage_capture(self, device, body):
        status = self.attempt_status(device, str(body.get('attempt_id') or ''))
        fields = validate_descriptors(body.get('fields') or [])
        answers = validate_captured_answers(body.get('answers') or [], {f['field_id']: f for f in fields})
        app = self.ledger.get_application_timeline(status['application_id'])['application']
        vault = self.autofill._vault
        if not hasattr(vault, 'stage_browser_capture'):
            return {'staged': False}
        vault.stage_browser_capture(status['attempt_id'], app['application_id'], app['ats'], fields, answers)
        self.maintain_captures()
        return {'staged': True}

    def maintain_captures(self):
        vault = self.autofill._vault
        if hasattr(vault, 'finish_browser_captures'):
            with connect(self.path) as con:
                confirmed = {r[0] for r in con.execute('SELECT application_id FROM applications WHERE submitted_at IS NOT NULL')}
            vault.finish_browser_captures(confirmed)
