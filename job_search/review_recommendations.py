"""Read-only, explainable next actions for email reviews.

Suggestions never create events or associations. Decisions still pass through the
existing user-reviewed commands, including their candidate and evidence checks.
"""
from __future__ import annotations

from .contracts import ContractError, JobSnapshot, payload_sha256
from .db import connect
from .mail.context import CandidateApplication
from .mail.identity import identity_conflicts, review_supported_candidates, unique_supported_application
from .mail.matching import rank_mail_candidates
from .review_messages import review_message


EVENT_LABELS = {
    'submission_confirmed': 'Confirm application received',
    'recruiter_contact': 'Record recruiter contact',
    'assessment_requested': 'Record assessment request',
    'assessment_completed': 'Record assessment completed',
    'interview_requested': 'Record interview request',
    'interview_scheduled': 'Record interview scheduled',
    'interview_completed': 'Record interview completed',
    'offer_received': 'Record offer',
    'offer_accepted': 'Record offer accepted',
    'rejection_received': 'Record rejection',
    'withdrawn': 'Record withdrawal',
}


def _suggestion(action, label, application_id=None, confidence='low',
                explanation='', requires_selection=False):
    return dict(action=action, label=label, application_id=application_id,
                confidence=confidence, explanation=explanation,
                requires_selection=requires_selection)


def _context(con, item):
    """Resolve trusted thread metadata without returning mailbox identifiers."""
    if item['kind'] == 'event_proposal':
        row = con.execute('SELECT e.* FROM mail_evidence e JOIN event_proposals p '
                          'USING(evidence_id) WHERE p.proposal_id=?', (item['id'],)).fetchone()
        if not row:
            return {}
        record = dict(row)
        record['conversation_ref'] = payload_sha256({
            'account': record['account_id'], 'conversation': record['conversation_id'],
        }) if record.get('conversation_id') else ''
        return record
    row = con.execute('SELECT m.* FROM lifecycle_mail_observations m '
                      'JOIN lifecycle_discoveries d USING(observation_id) '
                      'WHERE d.discovery_id=?', (item['id'],)).fetchone()
    return dict(row) if row else {}


def _matches(con, rows, item, content):
    context = _context(con, item)
    linked = set()
    if context.get('conversation_ref'):
        linked = {row[0] for row in con.execute(
            'SELECT DISTINCT l.application_id FROM lifecycle_mail_links l '
            'JOIN lifecycle_mail_observations m USING(observation_id) '
            'WHERE m.account_id=? AND m.conversation_ref=?',
            (context['account_id'], context['conversation_ref']),
        )}
    eligible = rows
    if item['kind'] == 'event_proposal':
        # These choices use the same server-loaded evidence and validation as
        # decide_event_proposal. Browser-supplied text cannot expand this set.
        allowed = set(item.get('candidate_application_ids', ()))
        if item.get('application_id'):
            allowed.add(item['application_id'])
        eligible = [row for row in rows if row['application_id'] in allowed]
    ranked = rank_mail_candidates([
        {**row, 'same_conversation': row['application_id'] in linked}
        for row in eligible
    ], content['subject'] + '\n' + content['body'])
    candidates = [CandidateApplication(
        application_id=row['application_id'], ats=row['ats'], job_id=row['job_id'],
        employer=row['employer_snapshot'], title=row['title_snapshot'],
        company_slug=row['company_slug_snapshot'], phase=row['current_phase'],
        match_context=row['mail_match_context'],
    ) for row in ranked]
    supported = review_supported_candidates(candidates, content['subject'], content['body'])
    supported_ids = {candidate.application_id for candidate in supported}
    ranked = [row for row in ranked if row['application_id'] in supported_ids]
    if not ranked:
        return [], None
    # Exact identity evidence wins when available. Otherwise the most relevant
    # supported role is still a useful, editable default for explicit review.
    unique = unique_supported_application(supported, content['subject'], content['body'])
    selected = unique or ranked[0]['application_id']
    margin = ranked[0]['mail_match_score'] - ranked[1]['mail_match_score'] if len(ranked) > 1 else 0
    matches = []
    for row in ranked:
        identity = row['application_id']
        confidence = 'low'
        explanation = row['mail_match_context']
        if identity == selected:
            if unique:
                confidence = 'high' if row['mail_match_score'] >= 90 else 'medium'
            elif len(ranked) == 1 or margin >= 3:
                confidence = 'medium'
                explanation = 'Closest matching role in the email; ' + explanation
            else:
                explanation = 'Suggested match; check the application because several roles have similar evidence. ' + explanation
        matches.append(dict(application_id=identity, employer=row['employer_snapshot'],
                            title=row['title_snapshot'], confidence=confidence,
                            explanation=explanation))
    matches.sort(key=lambda row: row['application_id'] != selected)
    return matches, selected


def review_job_snapshot(jobs, selection, content):
    """Resolve a browser-selected identity against local catalog and mail evidence."""
    if jobs is None or not isinstance(selection, dict) or set(selection) != {'ats', 'id'}:
        raise ContractError('selected job requires an available catalog and ats/id')
    if not all(isinstance(value, str) and value for value in selection.values()):
        raise ContractError('invalid selected job identity')
    job = jobs.get_job(selection['ats'], selection['id'])
    snapshot = JobSnapshot(ats=job['ats'], job_id=job['id'], family_id=job.get('family_id') or '',
        employer=job['company'], title=job['title'], company_slug=job['company'], job_url=job.get('jobUrl') or '')
    snapshot.validate()
    candidate = CandidateApplication(application_id='catalog-review', ats=snapshot.ats,
        job_id=snapshot.job_id, employer=snapshot.employer, title=snapshot.title,
        company_slug=snapshot.company_slug)
    if not review_supported_candidates([candidate], content.get('subject', ''), content.get('body', '')):
        raise ContractError('selected job has no supporting mail identity')
    return snapshot


def _catalog_matches(jobs, content):
    search = getattr(jobs, 'review_candidates', None)
    if search is None:
        return []
    try:
        rows = search(content['subject'], content['body'], limit=20)
    except ContractError:
        return []
    return [{key: row.get(key) for key in ('ats', 'id', 'company', 'title', 'jobUrl', 'closed_at')}
            | {'confidence': row.get('match_confidence', 'low') if row.get('match_unique') else 'low',
               'explanation': row.get('match_reason', 'Closest supported job in the local catalog.')
                 + ('' if row.get('match_unique') else '. Suggested match; check the role before recording the action.')}
            for row in rows]


def enrich_review_items(ledger, mail_source, items, jobs=None, *, can_analyze_archives=False):
    """Enrich dashboard rows using sanitized archived subject/body when available.

    Archived plaintext is neither persisted nor returned in this response. A
    locked or unavailable archive falls back to the stored evidence excerpt.
    """
    result = []
    with connect(ledger.store.db_path) as con:
        rows = [dict(row) for row in con.execute(
            'SELECT application_id,ats,job_id,employer_snapshot,title_snapshot,'
            'company_slug_snapshot,current_phase FROM applications ORDER BY application_id')]
        applications = {row['application_id']: row for row in rows}
        for original in items:
            item = dict(original)
            kind = item.get('kind')
            if kind in {'event_proposal', 'mail_discovery'}:
                try:
                    content = review_message(ledger, mail_source, item)
                except ContractError:
                    # A queue item can disappear between the list and evidence read.
                    content = dict(subject='', body='')
                if kind == 'event_proposal' and not item.get('application_id'):
                    evidence = _context(con, item)
                    if evidence:
                        item['candidate_application_ids'] = ledger.store._unassigned_mail_candidates(
                            evidence['evidence_id'], review_mail_content=content,
                        )
                matches, selected = _matches(con, rows, item, content)
                item['application_matches'] = matches
                assigned = item.get('application_id')
                conflicting = False
                if assigned in applications:
                    row = applications[assigned]
                    conflicting = identity_conflicts(CandidateApplication(
                        application_id=assigned, ats=row['ats'], job_id=row['job_id'],
                        employer=row['employer_snapshot'], title=row['title_snapshot'],
                        company_slug=row['company_slug_snapshot'],
                    ), content['subject'], content['body'])
                    if not selected and not conflicting:
                        selected = assigned
                match = next((row for row in matches if row['application_id'] == selected), None)
                if selected:
                    explanation = match['explanation'] if match else 'Application already selected by the saved proposal.'
                    confidence = match['confidence'] if match else 'medium'
                else:
                    explanation = ('The email names a different employer or role than the saved selection. Review the application match.' if conflicting
                                   else 'Several applications match this email. Choose the correct application.' if matches
                                   else 'No application has enough matching evidence. Choose or create an application after reviewing the email.')
                    confidence = 'low'
                if kind == 'event_proposal':
                    item['suggested_resolution'] = _suggestion(
                        'accept', EVENT_LABELS.get(item.get('detail'), 'Confirm update'),
                        selected, confidence, explanation, not bool(selected))
                else:
                    item['suggested_resolution'] = _suggestion(
                        'link' if selected else 'review',
                        'Link application' if selected else 'Choose application',
                        selected, confidence, explanation, not bool(selected))
                if not selected:
                    item['job_matches'] = _catalog_matches(jobs, content)
                    if item['job_matches']:
                        job = item['job_matches'][0]
                        item['suggested_resolution'].update(job=job, confidence=job['confidence'],
                            explanation=job['explanation'], requires_selection=False)
                        if kind == 'mail_discovery':
                            item['suggested_resolution'].update(action='link_job', label='Link job and create record')
            elif kind == 'mail_processing_failure':
                item['can_analyze_archive'] = bool(can_analyze_archives and mail_source is not None and con.execute(
                    'SELECT 1 FROM mail_archive WHERE account_id=? AND immutable_message_id=?',
                    (item.get('account_id'), item.get('id'))).fetchone())
                retry = item.get('can_retry') is not False
                item['suggested_resolution'] = _suggestion(
                    'retry' if retry else 'dismiss', 'Retry processing' if retry else 'Dismiss',
                    explanation='Retry email analysis.' if retry else 'This email is no longer in the synced folder.')
            elif kind == 'temporal_proposal':
                item['suggested_resolution'] = _suggestion(
                    'accept', 'Save proposed time' if item.get('detail') == 'interview' else 'Save deadline',
                    item.get('application_id'), explanation='Review and save the proposed time.')
            result.append(item)
    return result
