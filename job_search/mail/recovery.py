"""Resolve obsolete archived-review work without replaying mail or inference."""
from __future__ import annotations

import json
import re
import sqlite3

from ..contracts import payload_sha256


PREFIX = 'mail-understanding:review-recovery:'
RESOLUTION = 'mail_review_already_processed'


def resolution_evidence(con: sqlite3.Connection, item: dict) -> tuple[str, dict]:
    """Return only durable, message-scoped evidence; never inspect email text."""
    work_id = item['work_id']
    if (item['task_kind'] != 'mail.understanding' or not work_id.startswith(PREFIX)
            or not re.fullmatch(r'[a-f0-9]{64}', work_id[len(PREFIX):])):
        return 'not_archived_review_work', {}
    if item['status'] != 'dead':
        return 'work_not_failed', {}
    if (item.get('lease_token') or item.get('lease_owner') or item.get('lease_expires_at')
            or item.get('schedule_key') or item.get('workflow_id')):
        return 'work_still_owned', {}
    try:
        payload = json.loads(item['payload_json'])
    except (TypeError, ValueError):
        return 'invalid_review_work_identity', {}
    if payload != {'analysis_id': work_id[len('mail-understanding:'):]}:
        return 'invalid_review_work_identity', {}
    invocations = [dict(row) for row in con.execute(
        'SELECT invocation_id,state FROM inference_invocations WHERE work_id=? ORDER BY invocation_id',
        (work_id,))]
    if (item['external_outcome'] not in {'none', 'terminal'}
            or any(row['state'] not in {'completed', 'failed'} for row in invocations)):
        return 'external_reconciliation_required', {}
    # The original standalone owner stores a digest, not raw mailbox identifiers.
    # Resolve that digest against retained evidence, then check every staged copy.
    for evidence in con.execute('SELECT evidence_id,account_id,immutable_message_id FROM mail_evidence'):
        digest = payload_sha256({'account_id': evidence['account_id'],
                                 'message_id': evidence['immutable_message_id']})
        if digest != work_id[len(PREFIX):]:
            continue
        stages = con.execute(
            'SELECT processing_status FROM outlook_message_stage WHERE account_id=? AND immutable_message_id=?',
            (evidence['account_id'], evidence['immutable_message_id'])).fetchall()
        if (not any(row[0] == 'processed' for row in stages)
                or any(row[0] not in {'processed', 'ignored'} for row in stages)):
            return 'mail_review_not_processed', {}
        proposal = con.execute(
            'SELECT proposal_id,status FROM event_proposals WHERE evidence_id=? AND created_at>=? '
            'ORDER BY created_at DESC,proposal_id DESC LIMIT 1',
            (evidence['evidence_id'], item['created_at'])).fetchone()
        if not proposal:
            return 'mail_review_proposal_missing', {}
        return RESOLUTION, {'evidence_id': evidence['evidence_id'],
                            'proposal_id': proposal['proposal_id'], 'proposal_status': proposal['status'],
                            'invocations': invocations}
    return 'mail_review_evidence_missing', {}
