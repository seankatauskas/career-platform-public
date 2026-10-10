"""Read message evidence for a specific dashboard review item."""

import json

from .contracts import ContractError
from .db import connect
from .mail.archive_source import _parts


def review_message(ledger, mail_source, query):
    """Resolve through the review record, retaining mailbox scope for failures."""
    kind, identity = query.get('kind'), query.get('id')
    sources = {
        'mail_classification_review': ('mail_classification_reviews', 'review_id'),
        'event_proposal': ('event_proposals', 'proposal_id'),
        'mail_analysis': ('mail_understanding_analyses', 'analysis_id'),
        'temporal_proposal': ('temporal_proposals', 'temporal_proposal_id'),
        'lifecycle_correction': ('lifecycle_correction_proposals', 'proposal_id'),
        'interview_revision': ('interview_revisions', 'revision_id'),
        'mail_discovery': ('lifecycle_discoveries', 'discovery_id'),
        'browser_submission': ('browser_attempts', 'attempt_id'),
        'reply_request': ('lifecycle_tasks', 'task_id'),
    }
    with connect(ledger.store.db_path) as con:
        if kind == 'mail_processing_failure':
            saved = con.execute(
                'SELECT * FROM outlook_message_stage WHERE immutable_message_id=? '
                'AND account_id=? AND folder_ref=? AND query_version=?',
                (identity, query.get('account_id'), query.get('folder_ref'), query.get('query_version')),
            ).fetchone()
        elif kind in sources:
            table, key = sources[kind]
            saved = con.execute(f'SELECT * FROM {table} WHERE {key}=?', (identity,)).fetchone()
        else:
            raise ContractError('unknown review kind')
        if saved is None:
            raise ContractError('review item not found')
        record = dict(saved)
        if kind == 'mail_discovery':
            record = dict(con.execute('SELECT * FROM lifecycle_mail_observations WHERE observation_id=?',
                                      (record['observation_id'],)).fetchone())
        if kind == 'interview_revision':
            record = json.loads(record['details_json'])
        evidence_id = record.get('evidence_id')
        if evidence_id:
            record = dict(con.execute('SELECT * FROM mail_evidence WHERE evidence_id=?', (evidence_id,)).fetchone())
        archive_id = record.get('archive_id')
        if not archive_id and record.get('account_id') and record.get('immutable_message_id'):
            archive = con.execute('SELECT archive_id FROM mail_archive WHERE account_id=? AND immutable_message_id=?',
                                  (record['account_id'], record['immutable_message_id'])).fetchone()
            archive_id = archive['archive_id'] if archive else None
        saved_subject, saved_body = _parts(record.get('excerpt', ''))
        result = {'subject': saved_subject or record.get('subject', ''), 'body': saved_body,
                  'available': False, 'truncated': False}
    if archive_id and mail_source is not None:
        try:
            content = mail_source.get_review_message(archive_id)
            result.update(subject=content['subject'], body=content['body'],
                          available=True, truncated=content.get('truncated', False))
        except Exception:
            pass  # Locked archives must not disclose provider errors.
    return result
