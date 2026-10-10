"""Durable manual review of received but unusable classifier answers.

These records contain no inferred event, application assignment, or model quote.
The immutable mail evidence is the authority for a subsequent human decision.
"""
from contextlib import nullcontext

from ..contracts import ContractError, payload_sha256, validate_identifier


SCHEMA = """
CREATE TABLE mail_classification_reviews (
    review_id TEXT PRIMARY KEY,
    evidence_id TEXT NOT NULL UNIQUE REFERENCES mail_evidence(evidence_id),
    producer_version TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','resolved')),
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolution_json TEXT
);
"""


class ClassificationRejected(ContractError):
    """A received answer cannot support any automatic application interpretation."""

    def __init__(self):
        super().__init__('Email classification needs manual review; the model answer was not usable')


def assert_reviewable_outcome(connection=None):
    """Never turn unknown provider outcomes or a lost worker lease into success."""
    from ..db import connect
    from ..inference.usage import current_scope, InvocationReconciliationRequired, _assert_owned
    scope = current_scope()
    if scope is None:
        return
    with nullcontext(connection) if connection is not None else connect(scope.db_path) as con:
        _assert_owned(con, scope)
        if con.execute("SELECT 1 FROM inference_invocations WHERE work_id=? "
                       "AND state NOT IN ('completed','failed','cancelled')", (scope.work_id,)).fetchone():
            raise InvocationReconciliationRequired()


def record_review(con, stamp, evidence_id, producer_version):
    assert_reviewable_outcome(con)
    validate_identifier(producer_version, 'producer_version')
    review_id = 'unclassified:' + payload_sha256({'evidence_id': evidence_id})
    existing = con.execute('SELECT * FROM mail_classification_reviews WHERE evidence_id=?',
                           (evidence_id,)).fetchone()
    if existing:
        return dict(review_id=existing['review_id'], status=existing['status'], created=False)
    con.execute('INSERT INTO mail_classification_reviews '
                '(review_id,evidence_id,producer_version,reason_code,created_at) VALUES (?,?,?,?,?)',
                (review_id, evidence_id, producer_version, 'classification_output_rejected', stamp))
    return dict(review_id=review_id, status='pending', created=True)


def save_review(store, evidence_id, producer_version, context):
    assert_reviewable_outcome()
    return store._idempotent('mail_classification_review', context,
        {'evidence_id': evidence_id, 'producer_version': producer_version},
        lambda con, stamp: record_review(con, stamp, evidence_id, producer_version))


def review_record(con, identity):
    """Common review shape; an unclassified record supplies no default decision."""
    if identity.startswith('unclassified:'):
        row = con.execute('SELECT * FROM mail_classification_reviews WHERE review_id=?', (identity,)).fetchone()
        return None if row is None else dict(row, proposal_id=identity, event_type=None,
                                            evidence_quote='', payload_json='{}')
    row = con.execute('SELECT * FROM event_proposals WHERE proposal_id=?', (identity,)).fetchone()
    return dict(row) if row else None
