"""Offline company submission history on saved and model shortlists."""

import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from job_search.contracts import ApplicationEventType, EventInput, JobSnapshot, MutationContext, RecommendationProvenance
from job_search.dashboard import DashboardController
from job_search.integration import LocalJobCatalog
from job_search.service import JobSearchLedger
from tests.test_job_search_dashboard import FakePreferences


NOW = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)


def stamp(days=0, seconds=0):
    return (NOW + timedelta(days=days, seconds=seconds)).isoformat().replace('+00:00', 'Z')


class RecentCompanyApplicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.clock = patch('job_search.store.utc_now', return_value=stamp())
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.ledger = JobSearchLedger(self.root / 'ledger.db')
        self.preferences = FakePreferences()
        self.controller = DashboardController(self.ledger, self.preferences)
        self.sequence = 0

    def application(self, employer='Acme', slug='acme-board', ats='ashby', submitted=None):
        self.sequence += 1
        key = str(self.sequence)
        app = self.ledger.start_application(
            JobSnapshot(ats, 'prior-' + key, '', 'Earlier role', employer, slug,
                        'https://example.test/prior-' + key),
            RecommendationProvenance(), MutationContext('start-' + key, 'user', 'dashboard'),
        )['application']
        if submitted:
            self.submit(app['application_id'], submitted)
        return app['application_id']

    def submit(self, app_id, when):
        self.ledger.record_submission(app_id, when,
            MutationContext('submit-' + app_id, 'user', 'dashboard'))

    def event(self, app_id, event_type, when):
        key = event_type.value + '-' + app_id
        self.ledger.record_event(EventInput(app_id, event_type, when, {}, key,
            MutationContext(key, 'user', 'dashboard')))

    def badge(self, company='Acme', ats='ashby'):
        return self.controller.with_recent_company_applications([
            {'company': company, 'ats': ats},
        ])[0]['recent_company_application']

    def test_inclusive_window_excludes_old_future_and_preparing(self):
        self.application(employer='Old', submitted=stamp(days=-180, seconds=-1))
        self.application(employer='Future', submitted=stamp(seconds=1))
        self.application(employer='Preparing')
        boundary = self.application(employer='Boundary', submitted=stamp(days=-180))
        today = self.application(employer='Today', submitted=stamp())
        for company in ('Old', 'Future', 'Preparing', ''):
            self.assertIsNone(self.badge(company))
        self.assertEqual(self.badge('Boundary'), {
            'application_id': boundary, 'applied_at': stamp(days=-180), 'window_days': 180,
        })
        self.assertEqual(self.badge('Today')['application_id'], today)

    def test_company_names_cross_ats_but_board_aliases_are_scoped(self):
        application = self.application(employer='  Acme   Labs ', slug='acme-board',
                                       submitted=stamp(days=-1))
        for company, ats in [('ACME labs', 'lever'), ('Ａｃｍｅ Labs', 'greenhouse'),
                             ('acme-board', 'ashby')]:
            self.assertEqual(self.badge(company, ats)['application_id'], application)
        for company, ats in [('Acme Labs Europe', 'ashby'), ('Acme', 'ashby'),
                             ('acme-board', 'lever'), ('Other Company', 'ashby')]:
            self.assertIsNone(self.badge(company, ats))
        board = self.application(employer='sharedslug', slug='sharedslug',
                                 ats='greenhouse', submitted=stamp(days=-1))
        self.assertEqual(self.badge('SHAREDSLUG', 'greenhouse')['application_id'], board)
        self.assertIsNone(self.badge('sharedslug', 'lever'))

    def test_latest_submission_terminal_and_confirmation_semantics(self):
        older = self.application(submitted=stamp(days=-5))
        newest = self.application(submitted=stamp(days=-1))
        self.event(newest, ApplicationEventType.REJECTION_RECEIVED, stamp())
        self.assertEqual(self.badge()['application_id'], newest)
        self.event(older, ApplicationEventType.SUBMISSION_CONFIRMED, stamp())
        self.assertEqual(self.badge()['application_id'], newest)
        expired = self.application(employer='Expired', submitted=stamp(days=-181))
        self.event(expired, ApplicationEventType.SUBMISSION_CONFIRMED, stamp())
        self.assertIsNone(self.badge('Expired'))
        confirmed = self.application(employer='Confirmed')
        self.event(confirmed, ApplicationEventType.SUBMISSION_CONFIRMED, stamp(days=-2))
        self.assertEqual(self.badge('Confirmed')['applied_at'], stamp(days=-2))
        self.assertEqual(self.ledger.verify_projections(), [])

    def test_cached_model_shortlist_updates_without_reranking_and_expires(self):
        application = self.application()
        initial = self.controller.create_shortlist('browser', {}, 'shortlist-first')
        self.assertIsNone(initial['recommendations'][0]['recent_company_application'])
        self.submit(application, stamp())
        cached = self.controller.cached_shortlist('browser')
        self.assertEqual(cached['recommendations'][0]['recent_company_application']['application_id'], application)
        self.assertIsNone(initial['recommendations'][0]['recent_company_application'])
        refreshed = self.controller.create_shortlist('other-browser', {}, 'shortlist-second')
        self.assertEqual(refreshed['recommendations'][0]['recent_company_application']['application_id'], application)
        with patch('job_search.store.utc_now', return_value=stamp(days=180, seconds=1)):
            self.assertIsNone(self.controller.cached_shortlist('browser')['recommendations'][0]['recent_company_application'])
        self.assertEqual(len(self.preferences.calls), 2)

    def test_saved_shortlist_uses_displayed_company_and_refreshes_after_submission(self):
        jobs_path = self.root / 'jobs.db'
        with closing(sqlite3.connect(jobs_path)) as con, con:
            con.execute('CREATE TABLE jobs(ats TEXT,id TEXT,title TEXT,company TEXT,description TEXT,jobUrl TEXT,PRIMARY KEY(ats,id))')
            con.execute("INSERT INTO jobs VALUES ('ashby','new-role','New role','Old Name','Role description','https://example.test/new-role')")
        catalog = LocalJobCatalog(jobs_path)
        controller = DashboardController(self.ledger, self.preferences, jobs=catalog)
        list_id = controller.curated.publish({'title': 'Saved picks', 'idempotency_key': 'publish',
            'jobs': [{'ats': 'ashby', 'job_id': 'new-role'}]})['list_id']
        with closing(sqlite3.connect(jobs_path)) as con, con:
            con.execute("UPDATE jobs SET company='Acme'")
        self.assertIsNone(controller.curated_list(list_id)['recommendations'][0]['recent_company_application'])
        application = self.application(ats='lever', submitted=stamp(days=-3))
        for instance in (controller, DashboardController(self.ledger, self.preferences, jobs=catalog)):
            row = instance.curated_list(list_id)['recommendations'][0]
            self.assertEqual(row['company'], 'Old Name')
            self.assertEqual(row['job_posting']['company'], 'Acme')
            self.assertEqual(row['recent_company_application']['application_id'], application)
        self.assertNotIn('recent_company_application', controller.curated.get(list_id)['recommendations'][0])

    def test_lookup_does_not_use_paginated_application_history(self):
        application = self.application(submitted=stamp(days=-90))
        # The dashboard history defaults to 200 rows; company lookup has no such cap.
        with patch('job_search.store.utc_now', return_value=stamp(seconds=1)):
            for _ in range(201):
                self.application(employer='Other Company')
        self.assertNotIn(application, [row['application_id'] for row in self.ledger.list_applications()])
        self.assertEqual(self.badge()['application_id'], application)


if __name__ == '__main__':
    unittest.main()
