"""User-confirmed review scope, saved independently of any review's progress."""
from __future__ import annotations

import json

from ..contracts import ContractError, canonical_json
from .contracts import exact, integer, text, timestamp


SCHEMA = r"""
CREATE TABLE job_review_search_briefs (
 revision INTEGER PRIMARY KEY AUTOINCREMENT,
 brief_json TEXT NOT NULL,
 saved_at TEXT NOT NULL
);
CREATE TRIGGER job_review_search_briefs_no_update BEFORE UPDATE ON job_review_search_briefs
BEGIN SELECT RAISE(ABORT, 'search brief revisions are immutable'); END;
CREATE TRIGGER job_review_search_briefs_no_delete BEFORE DELETE ON job_review_search_briefs
BEGIN SELECT RAISE(ABORT, 'search brief revisions are immutable'); END;
"""

BRIEF_OPTIONS = {
    'broad_geography': ('us', 'worldwide'),
    'targeted_geography': ('us', 'worldwide'),
    'targeted_scope': ('software_building', 'all_technical'),
    'adjacent_roles': ('broad_only', 'targeted', 'exclude'),
    'conditional_order': ('technical_fit', 'after_actionable'),
}
BRIEF_FIELDS = (*BRIEF_OPTIONS, 'stretch_policy', 'eligibility_facts', 'notes')


def suggested_search_brief():
    """Editable suggestions, never treated as user preferences until saved."""
    return {
        'broad_geography': 'us',
        'targeted_geography': 'us',
        'targeted_scope': 'software_building',
        'adjacent_roles': 'broad_only',
        'conditional_order': 'technical_fit',
        'stretch_policy': 'Consider evidence-backed stretches and explain material experience or specialization gaps.',
        'eligibility_facts': [],
        'notes': [],
    }


def validate_search_brief(value):
    exact(value, BRIEF_FIELDS, BRIEF_FIELDS)
    out = {}
    for field, choices in BRIEF_OPTIONS.items():
        if not isinstance(value[field], str) or value[field] not in choices:
            raise ContractError(f'{field} must be one of: {", ".join(choices)}')
        out[field] = value[field]
    out['stretch_policy'] = text(value['stretch_policy'], 'stretch_policy', 1000)
    for field in ('eligibility_facts', 'notes'):
        values = value[field]
        if not isinstance(values, list) or len(values) > 20:
            raise ContractError(f'{field} must be a list of up to twenty user-confirmed statements')
        out[field] = [text(item, field, 500) for item in values]
    return out


def project_search_brief(value):
    """Project the frozen public evidence shape without unrelated diagnostics."""
    if not isinstance(value, dict) or not isinstance(value.get('brief'), dict):
        raise ContractError('invalid search brief snapshot')
    revision = integer(value.get('revision'), 'search brief revision')
    brief = validate_search_brief({key: value['brief'][key] for key in BRIEF_FIELDS if key in value['brief']})
    saved_at = value.get('saved_at')
    if revision:
        timestamp(saved_at)
    elif saved_at is not None:
        raise ContractError('unsaved search brief cannot have a saved date')
    return {'revision': revision, 'brief': brief, 'saved_at': saved_at}


def current_search_brief(con):
    row = con.execute('SELECT revision,brief_json,saved_at FROM job_review_search_briefs ORDER BY revision DESC LIMIT 1').fetchone()
    if row is None:
        return {'revision': 0, 'brief': suggested_search_brief(), 'saved_at': None}
    return project_search_brief({'revision': row[0], 'brief': json.loads(row[1]), 'saved_at': row[2]})


class SearchBriefMixin:
    """Methods use the shared review dispatcher's transaction and idempotency."""

    def _brief(self, con, args):
        return current_search_brief(con)

    def _save_brief(self, con, args):
        expected = integer(args.get('expected_revision'), 'expected_revision')
        brief = validate_search_brief(args.get('brief'))
        current = current_search_brief(con)
        if current['revision'] != expected:
            raise ContractError('search brief changed; reload its current revision before saving')
        con.execute('INSERT INTO job_review_search_briefs(revision,brief_json,saved_at) VALUES(?,?,?)',
                    (expected + 1, canonical_json(brief), self.now()))
        return current_search_brief(con)
