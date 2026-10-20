"""Reviewed creation for unassigned email, using public owner participants."""
from dataclasses import dataclass
import sqlite3

from job_search.commands import DomainError, digest
from job_search.contracts import ContractError
from job_search.mail.discovery import explicit_identity, related_employer, related_role
from .review import ReviewService


@dataclass(frozen=True)
class CreateApplicationFromReview:
    group_id: str
    expected_digest: str
    expected_match_digest: str
    employer: str
    title: str
    catalog_id: str = None
    search_employer: str = None
    search_title: str = None


class ApplicationDiscovery:
    """A preview is read-only; the exact human command creates and links atomically."""
    def __init__(self, runtime):
        self.runtime = runtime
        self.review = ReviewService(runtime)

    def _source(self, con, group_id, expected_digest):
        group = self.review._checked(con, group_id, expected_digest)
        if group['source_type'] != 'email' or group.get('association') or group['applications']:
            raise DomainError('dependency_unresolved', 'Choose an unassigned email; existing associations use application correction')
        source = self.runtime.correspondence.get(con, group_id[6:])
        if source['direction'] != 'incoming':
            raise DomainError('dependency_unresolved', 'Only received correspondence can suggest an application')
        if any('source_revision_changed' in p['blockers'] for p in group['proposals']):
            raise DomainError('version_conflict', 'Refresh analysis for the current email revision')
        reader = getattr(self.runtime, 'mail_reader', None)
        if reader is None or not reader.can_review_source(source['id'], source['revision'], source['source_sha256']):
            raise DomainError('not_authorized', 'An authorized email reader is required')
        return group, source

    @staticmethod
    def _identity(employer, title):
        if any(not isinstance(v, str) or not 1 <= len(v.strip()) <= 160 for v in (employer, title)):
            raise DomainError('invalid_input', 'Confirm the employer and role from the email (up to 160 characters each)')
        return {'employer': employer.strip(), 'title': title.strip()}

    def _matches(self, con, fields):
        applications, after, scanned = [], None, 0
        while True:
            page = self.runtime.applications.list_applications(con, limit=200, after_id=after)
            for app in page['items']:
                job = self.runtime.applications.get_job(con, app['job_id'])
                names = [job.get('employer') or '']
                titles = [job.get('title') or '']
                for saved in job.get('recorded_snapshots', []):
                    snapshot = saved['snapshot']
                    names.extend([snapshot.get('employer') or '', snapshot.get('company_slug') or ''])
                    titles.append(snapshot.get('title') or '')
                if (any(related_employer(fields['employer'], name) for name in names)
                        and any(related_role(fields['title'], title) for title in titles)):
                    applications.append({'id': app['id'], 'version': app['version'],
                                         'employer': job.get('employer'), 'title': job.get('title')})
            scanned += len(page['items'])
            after = page['next_cursor']
            if not after or scanned >= 10000 or len(applications) > 20:
                break
        complete = not after and len(applications) <= 20
        jobs = {'items': [], 'complete': False}
        catalog = getattr(self.runtime, 'job_catalog', None)
        if catalog is not None:
            try:
                jobs = catalog.review_role_matches(fields['employer'], fields['title'])
            except (ContractError, sqlite3.Error, OSError):
                pass
        return {'applications': applications[:20], 'jobs': jobs['items'], 'complete': complete and jobs['complete']}

    def _checked_identity(self, con, group, source, fields):
        matches = self._matches(con, fields)
        result = {**fields, **matches, 'can_create': matches['complete'] and not matches['applications'] and not matches['jobs']}
        result['match_digest'] = digest({'group': group['preview_digest'], 'source': group['source'], **result})
        result['catalog_choices'] = [self._catalog_choice(group, fields, job) for job in matches['jobs']]
        if result['can_create']:
            result['reason'] = 'No plausible existing role found. Create a Tracking application and attach this email. Proposed status changes and tasks remain in Review.'
        elif matches['applications'] or matches['jobs']:
            result['reason'] = 'Possible existing roles need review before creating another application.'
        else:
            result['reason'] = 'The application or jobs catalog check is incomplete. Creation is unavailable until both can be checked.'
        return result

    @staticmethod
    def _catalog_choice(group, fields, row):
        choice = {'id': f"catalog:{row['ats']}:{row['id']}", 'job': {
            'ats': row['ats'], 'source_id': row['id'], 'employer': row['company'],
            'title': row['title'], 'location': row.get('location'),
            'job_url': row.get('jobUrl'), 'closed_at': row.get('closed_at')}}
        choice['match_digest'] = digest({'group': group['preview_digest'], 'search': fields, 'choice': choice})
        return choice

    def preview(self, *, group_id, expected_digest, employer=None, title=None):
        with self.runtime.executor.read() as con:
            group, source = self._source(con, group_id, expected_digest)
        # Authorized archive reads and interpretation stay outside write transactions.
        content = self.runtime.mail_reader.read_review_source(source['id'], source['revision'], source['source_sha256'])
        fields = self._identity(employer, title) if employer is not None or title is not None else explicit_identity('', content.get('text') or '')
        if not content.get('coverage', {}).get('complete') or not all(fields.values()):
            return {**fields, 'can_create': False, 'complete': False, 'applications': [], 'jobs': [], 'catalog_choices': [],
                    'reason': 'Read the email and confirm its employer and role. Complete source evidence is required before suggesting creation.'}
        fields = self._identity(**fields)
        with self.runtime.executor.read() as con:
            group, current = self._source(con, group_id, expected_digest)
            if current['source_sha256'] != source['source_sha256'] or current['revision'] != source['revision']:
                raise DomainError('version_conflict', 'The email changed while checking its identity')
            return self._checked_identity(con, group, current, fields)

    def create(self, tx, value: CreateApplicationFromReview):
        if tx.context.principal.kind != 'human' or tx.context.origin != 'direct':
            raise DomainError('not_authorized', 'Application discovery requires an exact human decision')
        group, source = self._source(tx.connection, value.group_id, value.expected_digest)
        fields = self._identity(value.employer, value.title)
        if value.catalog_id is None and (value.search_employer is not None or value.search_title is not None):
            raise DomainError('invalid_input', 'Catalog search identity requires a selected role')
        if value.catalog_id is not None:
            if value.search_employer is not None or value.search_title is not None:
                search = self._identity(value.search_employer, value.search_title)
                matches = self._matches(tx.connection, search)
                choices = [self._catalog_choice(group, search, row) for row in matches['jobs']]
            else:
                choices = group.get('catalog_candidates', [])
            candidate = next((c for c in choices if c['id'] == value.catalog_id), None)
            if candidate is None or candidate['match_digest'] != value.expected_match_digest:
                raise DomainError('version_conflict', 'The catalog choice changed. Refresh this review.')
            job = candidate['job']
            if fields != {k: job[k] for k in ('employer', 'title')}:
                raise DomainError('invalid_input', 'The selected catalog identity does not match the reviewed role')
            try:
                current = self.runtime.job_catalog.posting_summaries([(job['ats'], job['source_id'])]).get((job['ats'], job['source_id']))
            except (AttributeError, ContractError, sqlite3.Error, OSError):
                current = None
            if not current or any(current.get(column) != job.get(field) for column, field in
                                  (('company', 'employer'), ('title', 'title'), ('location', 'location'), ('jobUrl', 'job_url'))):
                raise DomainError('version_conflict', 'The catalog role changed or is unavailable. Refresh analysis before choosing it.')
            # Exact ATS identity reuses an application created since analysis.
            # This saves a role, never a browser attempt or accepted receipt.
            app = self.runtime.applications.save_job(tx, {'job_source': {
                'source': job['ats'], 'source_id': job['source_id'], **fields},
                'job_snapshot': {'ats': job['ats'], 'job_id': job['source_id'], **fields,
                                 'location': job.get('location'), 'job_url': job.get('job_url')}})
        else:
            checked = self._checked_identity(tx.connection, group, source, fields)
            if not checked['can_create'] or checked['match_digest'] != value.expected_match_digest:
                raise DomainError('version_conflict', 'The matching review changed. Refresh before creating an application.')
            app = self.runtime.applications.ensure_application(tx, {'job_source': {
                'discovery_id': 'mail:' + source['id'], **fields}})
        # Replace unresolved targets with attributed proposals, without accepting
        # their semantic findings. Only the explicitly previewed email link is accepted.
        replacements = self.review.correct_review_target(tx, group_id=value.group_id,
            expected_digest=value.expected_digest, application_id=app['id'],
            reason='Created a tracking application from reviewed correspondence', receipt_scope='application')
        link = replacements['association_proposal']
        self.runtime.workflows.review_changes(tx, [{'proposal_id': link['id'], 'expected_version': link['version'],
            'decision': 'accept', 'reason': 'Attach the reviewed email to the new tracking application'}])
        return {'status': 'applied', 'application_id': app['id'], 'job_id': app['job_id'],
                'association_proposal_id': link['id'], 'pending_proposals': [p['id'] for p in replacements['proposals']]}
