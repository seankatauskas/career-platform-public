"""Recover failed incoming mail analysis from its private sanitized archive.

This explicit review command uses the configured classifier and creates a pending
proposal only. It never retries Graph, applies an event, or links an application.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import replace
import fcntl
import os
from pathlib import Path
import uuid

from .contracts import (ContractError, ConflictError, MAIL_EXCERPT_LIMIT,
                        MutationContext, payload_sha256, utc_now, validate_identifier)
from .db import connect
from .mail.context import CandidateApplication
from .mail.identity import review_supported_candidates
from .mail.matching import rank_mail_candidates
from .mail.proposals import validate_model_output
from .mail.sanitizer import sanitize_mail
from .mail.understanding_runtime import MailUnderstandingRuntime
from .inference.usage import UsageDeferred, InvocationReconciliationRequired
from .review_messages import review_message


def _identity(query):
    message = query.get('message_id') or query.get('id')
    if query.get('message_id') and query.get('id') and query['message_id'] != query['id']:
        raise ContractError('review message identity does not match')
    account, folder, version = query.get('account_id'), query.get('folder_ref'), query.get('query_version')
    if any(not isinstance(value, str) or not value or len(value) > 2048 for value in (account, folder, message)):
        raise ContractError('invalid archived review identity')
    if type(version) is not int or version < 1:
        raise ContractError('invalid archived review query version')
    return account, folder, version, message


@contextmanager
def _recovery_lock(db_path, digest):
    # All processes sharing this database serialize recovery of the same immutable
    # message. Lock files contain no mail data and their names contain only hashes.
    directory = Path(db_path).parent / '.review-recovery-locks'
    directory.mkdir(mode=0o700, exist_ok=True)
    descriptor = os.open(str(directory / digest), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _records(ledger, identity, transaction=None):
    account, folder, version, message = identity
    with nullcontext(transaction) if transaction is not None else connect(ledger.store.db_path) as con:
        stage = con.execute('SELECT * FROM outlook_message_stage WHERE account_id=? '
                            'AND folder_ref=? AND query_version=? AND immutable_message_id=?', identity).fetchone()
        if stage is None:
            raise ContractError('mail review item was not found')
        observation = con.execute('SELECT direction FROM lifecycle_mail_observations '
                                  'WHERE account_id=? AND immutable_message_id=?', (account, message)).fetchone()
        folder_role = con.execute('SELECT role FROM lifecycle_mail_folders WHERE account_id=? AND folder_id=?',
                                  (account, folder)).fetchone()
        if (folder.casefold() in {'sentitems', 'sent', 'drafts'}
                or folder_role and folder_role['role'] in {'sentitems', 'drafts'}
                or observation and observation['direction'] in {'draft', 'outbound'}):
            raise ContractError('only received mail can be analyzed for review')
        evidence = con.execute('SELECT * FROM mail_evidence WHERE account_id=? AND immutable_message_id=?',
                               (account, message)).fetchone()
        proposal = con.execute('SELECT p.proposal_id,p.status FROM event_proposals p JOIN mail_evidence e '
                               'USING(evidence_id) WHERE e.account_id=? AND e.immutable_message_id=? '
                               'ORDER BY p.created_at DESC,p.proposal_id DESC LIMIT 1', (account, message)).fetchone()
    return dict(stage), dict(evidence) if evidence else None, dict(proposal) if proposal else None


def _mark_processed(ledger, identity):
    with connect(ledger.store.db_path) as con:
        updated = con.execute("UPDATE outlook_message_stage SET processing_status='processed',last_error='',updated_at=? "
                              "WHERE account_id=? AND folder_ref=? AND query_version=? AND immutable_message_id=? "
                              "AND processing_status='failed'", (utc_now(), *identity)).rowcount
        if not updated:
            current = con.execute('SELECT processing_status FROM outlook_message_stage WHERE account_id=? '
                                  'AND folder_ref=? AND query_version=? AND immutable_message_id=?', identity).fetchone()
            if not current or current['processing_status'] != 'processed':
                raise ConflictError('email review changed during analysis; refresh the page')


def recover_review(ledger, mail_source, query, classifier, model_version, *, usage_limits=None):
    """Reanalyze one scoped failed message; retries reuse its saved proposal."""
    identity = _identity(query)
    account, folder, version, message = identity
    digest = payload_sha256({'account_id': account, 'message_id': message})
    with _recovery_lock(ledger.store.db_path, digest):
        stage, existing_evidence, existing_proposal = _records(ledger, identity)
        if existing_proposal and stage['processing_status'] in {'failed', 'processed'}:
            if stage['processing_status'] == 'failed':
                _mark_processed(ledger, identity)
            return {**existing_proposal, 'created': False}
        if stage['processing_status'] != 'failed':
            raise ConflictError('email is no longer awaiting failure review')
        if classifier is None:
            raise ContractError('email analysis is not configured')
        validate_identifier(model_version, 'model_version')
        content = review_message(ledger, mail_source, dict(
            kind='mail_processing_failure', id=message, account_id=account,
            folder_ref=folder, query_version=version,
        ))
        if not content['available']:
            raise ContractError('the archived email is not available for analysis')
        mail = sanitize_mail(content['subject'], content['body'], max_chars=MAIL_EXCERPT_LIMIT)
        rows = rank_mail_candidates(ledger.store.list_mail_candidates(
            received_at=stage['received_at'] or '', account_id=account,
            conversation_id=stage['conversation_id'], sender=stage['sender'], include_unsubmitted=True,
        ), mail.subject + '\n' + mail.body)
        candidates = review_supported_candidates([CandidateApplication(
            application_id=row['application_id'], ats=row['ats'], job_id=row['job_id'],
            employer=row['employer_snapshot'], title=row['title_snapshot'],
            company_slug=row['company_slug_snapshot'], phase=row['current_phase'],
            match_context=row['mail_match_context'],
        ) for row in rows], mail.subject, mail.body)[:20]
        owner = MailUnderstandingRuntime(ledger, None, classifier, mode='legacy', usage_limits=usage_limits)
        with owner._inference_scope('review-recovery:' + digest, uuid.uuid4().hex, None) as attempt:
            try:
                output = classifier.classify(mail.text, [item.model_context() for item in candidates])
            except (UsageDeferred, InvocationReconciliationRequired):
                raise
            except Exception:
                raise ContractError('archived email analysis failed; the original review is unchanged') from None
            validated = validate_model_output(output,
                evidence_id=existing_evidence['evidence_id'] if existing_evidence else 'review-recovery',
                mail=mail, candidates=candidates, producer_version=model_version)
            if existing_evidence:
                # Retained evidence is immutable. Preserve the validated quote while
                # relocating its offset into the exact excerpt already on record.
                start = existing_evidence['excerpt'].find(validated.evidence_quote)
                if start < 0:
                    raise ContractError('analysis evidence is absent from the retained email excerpt')
                validated = replace(validated, span_start=start, span_end=start + len(validated.evidence_quote))
            def persist(con, stamp):
                # Direction, status, evidence, proposal, and stage completion share
                # one write transaction so a concurrent dismissal cannot be lost.
                current, evidence, competing = _records(ledger, identity, con)
                if current['processing_status'] != 'failed':
                    raise ConflictError('email review changed during analysis; refresh the page')
                if competing:
                    result = {**competing, 'created': False}
                else:
                    reviewed = validated
                    if evidence:
                        start = evidence['excerpt'].find(reviewed.evidence_quote)
                        if start < 0:
                            raise ContractError('analysis evidence is absent from the retained email excerpt')
                        reviewed = replace(reviewed, span_start=start, span_end=start + len(reviewed.evidence_quote))
                    else:
                        evidence = ledger.store.record_mail_evidence(dict(
                            account_id=account, immutable_message_id=message,
                            conversation_id=stage['conversation_id'], sender=stage['sender'],
                            subject=mail.subject, received_at=stage['received_at'],
                            body_sha256=mail.content_sha256, excerpt=mail.text,
                        ), MutationContext('review-evidence:' + digest, 'system', 'review_recovery'),
                            _transaction=(con, stamp))['evidence']
                    proposal = replace(reviewed, evidence_id=evidence['evidence_id'],
                                       proposed_application_id=None, dedupe_key='review-recovery:' + digest)
                    saved = ledger.store.create_event_proposal(proposal, MutationContext(
                        'review-proposal:' + digest, 'model', 'review_recovery'), _transaction=(con, stamp))
                    result = {'proposal_id': saved['proposal']['proposal_id'], 'status': saved['proposal']['status'],
                              'created': saved['created']}
                updated = con.execute("UPDATE outlook_message_stage SET processing_status='processed',last_error='',updated_at=? "
                                      "WHERE account_id=? AND folder_ref=? AND query_version=? AND immutable_message_id=? "
                                      "AND processing_status='failed'", (stamp, *identity)).rowcount
                if updated != 1:
                    raise ConflictError('email review changed during analysis; refresh the page')
                return result
            result = ledger.store._idempotent('recover_mail_review',
                MutationContext('review-recover:' + digest, 'system', 'review_recovery'),
                {'account_id': account, 'folder_ref': folder, 'query_version': version, 'message_id': message}, persist)
            attempt['checkpointed'] = True
            return result
