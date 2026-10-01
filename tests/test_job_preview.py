"""Offline checks for read-only previews and untrusted ATS formatting."""
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from job_search.collection import boards
from job_search.contracts import JobSnapshot, MutationContext, RecommendationProvenance
from job_search.integration import LocalJobCatalog
from job_search.job_preview import render_description
from tests.test_job_search_dashboard import dashboard, request


class PreviewTests(unittest.TestCase):
    def test_preserves_html_and_encoded_html_without_active_content(self):
        value = '<h2 onclick="bad()">Role</h2><p>Build <strong>systems</strong>.</p><ul><li>Python</li></ul>'
        safe = render_description(value + '<script>bad()</script><style>body{display:none}</style><img src="https://tracker.test/pixel"><iframe src="https://bad.test">hidden</iframe><a href="javascript:bad()">bad link</a><a href="https://example.test/?x=1&amp;y=2" onclick="bad()">Details</a>')
        self.assertIn('<h2>Role</h2>', safe)
        self.assertIn('<ul><li>Python</li></ul>', safe)
        for forbidden in ('onclick', '<script', '<style', '<img', '<iframe', 'javascript:', 'bad()', 'hidden'):
            self.assertNotIn(forbidden, safe)
        self.assertIn('rel="noopener noreferrer"', safe)
        self.assertIn('href="https://example.test/?x=1&amp;y=2"', safe)
        self.assertEqual(render_description('&lt;p&gt;Hello &amp;amp; welcome&lt;/p&gt;'), '<p>Hello &amp; welcome</p>')
        self.assertEqual(render_description('<script>only script</script>'), '')
        self.assertEqual(render_description('3 < 5 & 9 > 2\nNext paragraph'), '<p>3 &lt; 5 &amp; 9 &gt; 2<br>Next paragraph</p>')

    def test_malformed_markup_is_inert_and_bounded(self):
        for value in ('<svg><script>alert(1)</script></svg><p>Visible</p>', '<math><mtext><table><mglyph><style><!--</style><img title="--><img src=x onerror=alert(1)>">', '<a href="java&#10;script:alert(1)">text</a>', '<a href="https://[invalid">text</a>'):
            output = render_description(value)
            self.assertNotIn('onerror=', output.replace('&quot;', ''))
            self.assertNotIn('<img', output)
            self.assertNotIn('href=', output)
        output = render_description('<div>' * 2000 + 'hello' + '</div>' * 2000)
        self.assertLess(output.count('<div>'), 65)
        self.assertIn('hello', output)

    def test_collection_preserves_layout_without_changing_model_text(self):
        payloads = {
            'ashby': {'jobs': [{'id':'1', 'isListed':True, 'title':'Engineer', 'descriptionPlain':'Build systems. Python', 'descriptionHtml':'<p>Build systems.</p><ul><li>Python</li></ul>'}]},
            'greenhouse': {'jobs': [{'id':1, 'title':'Engineer', 'content':'&lt;h2&gt;About&lt;/h2&gt;&lt;p&gt;Build systems.&lt;/p&gt;'}]},
            'lever': [{'id':'1','text':'Engineer','descriptionPlain':'Build systems.', 'description':'<p>Build systems.</p>', 'lists':[{'text':'Requirements','content':'<li>Python</li>'}], 'additional':'<p>Benefits</p>'}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'jobs.db'
            with closing(sqlite3.connect(db)) as con:
                # Exercise the additive migration with a real pre-preview schema.
                con.execute('CREATE TABLE jobs(ats TEXT,id TEXT,description TEXT,first_seen TEXT,last_seen TEXT,closed_at TEXT,PRIMARY KEY(ats,id))')
            for ats, payload in payloads.items():
                with patch.object(boards, 'fetch', return_value=json.dumps(payload).encode()):
                    rows = boards.scan_board(ats, 'example', None, False, 'contains')
                self.assertNotIn('<', rows[0]['description'])
                self.assertIn('<', render_description(rows[0]['description_html']))
                boards.save(rows, db, '2026-09-29T00:00:00Z')
                job = LocalJobCatalog(db).get_job(ats, '1')
                self.assertEqual(job['description_html'], rows[0]['description_html'])
                self.assertEqual(job['description'], rows[0]['description'])
            lever = LocalJobCatalog(db).get_job('lever','1')['description_html']
            self.assertEqual(lever.count('Build systems.'), 1)
            self.assertIn('<h3>Requirements</h3><ul><li>Python</li></ul>', lever)
            self.assertIn('Benefits', lever)
            with closing(sqlite3.connect(db)) as con, con:
                boards._prepare_etags(con)
                con.execute("INSERT INTO board_etag VALUES ('ashby','example','old','date',1,2)")
            self.assertEqual(boards.load_etags(db), {})

    def test_preview_route_needs_no_shortlist_and_does_not_write(self):
        with dashboard() as (server, controller, ledger, preferences):
            class Catalog:
                def get_job(self, ats, job_id):
                    return {'ats':ats,'id':job_id,'title':'Engineer','description':'Flat text', 'description_html':'<h2>Responsibilities</h2><ul><li>Build systems</li></ul>'}
            controller.jobs = Catalog()
            before = ledger.list_applications()
            status, _, data = request(server, 'GET', '/api/v1/jobs/preview?ats=ashby&id=1')
            self.assertEqual(status, 200)
            result = json.loads(data)
            self.assertIn('<h2>Responsibilities</h2>', result['description_html'])
            self.assertNotIn('description_html', result['job'])
            self.assertNotIn('description', result['job'])
            self.assertEqual(ledger.list_applications(), before)
            self.assertEqual(preferences.calls, [])
            self.assertEqual(request(server, 'GET', '/api/v1/jobs/preview?ats=ashby')[0], 400)
            self.assertEqual(request(server, 'GET', '/api/v1/jobs/preview?ats=ashby&id=1&id=2')[0], 400)
            controller.jobs = None
            self.assertFalse(json.loads(request(server, 'GET', '/api/v1/jobs/preview?ats=ashby&id=1')[2])['available'])

    def test_application_cards_get_catalog_details_without_description_reads(self):
        with tempfile.TemporaryDirectory() as tmp, dashboard() as (server, controller, ledger, preferences):
            db = Path(tmp) / "jobs.db"
            boards.save([{"ats":"ashby", "id":"1", "title":"Current title", "company":"example",
                "location":"Chicago, IL", "employmentType":"FullTime", "description":"x" * 300000,
                "jobUrl":"https://example.test/current", "posted_at":"2026-09-01T00:00:00Z"},
                {"ats":"lever", "id":"1", "title":"Other job", "company":"other", "location":"London"}],
                db, "2026-09-29T00:00:00Z")
            catalog = LocalJobCatalog(db)
            controller.jobs = catalog
            started = ledger.start_application(JobSnapshot(ats="ashby", job_id="1", family_id="", title="Saved title",
                employer="Example", company_slug="example", job_url="https://example.test/1"),
                RecommendationProvenance(), MutationContext("summary-start", "user", "dashboard"))
            before = ledger.get_application_timeline(started["application"]["application_id"])
            # The card query must not load a full description for every application.
            with patch.object(catalog, "get_job", side_effect=AssertionError("no per-job description reads")):
                status, _, data = request(server, "GET", "/api/v1/applications")
            self.assertEqual(status, 200)
            app = json.loads(data)["applications"][0]
            self.assertEqual(app["title_snapshot"], "Saved title")
            self.assertEqual(app["job_posting"]["location"], "Chicago, IL")
            self.assertEqual(app["job_posting"]["employmentType"], "FullTime")
            self.assertEqual(app["job_posting"]["posted_at"], "2026-09-01T00:00:00Z")
            self.assertNotIn("description", app["job_posting"])
            self.assertNotIn("description_html", app["job_posting"])
            self.assertEqual(ledger.get_application_timeline(app["application_id"]), before)
            self.assertNotIn("location", catalog.posting_dates([("ashby", "1")])[("ashby", "1")])

    def test_large_card_batch_uses_bounded_catalog_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'jobs.db'
            with closing(sqlite3.connect(db)) as con, con:
                con.execute('CREATE TABLE jobs(ats TEXT,id TEXT,title TEXT,posted_at TEXT,PRIMARY KEY(ats,id))')
                con.executemany('INSERT INTO jobs VALUES (?,?,?,?)', [
                    ('ashby', str(i), f'Role {i}', '2026-09-30T00:00:00Z') for i in range(10000)])
                con.execute("INSERT INTO jobs VALUES ('lever','9000','Different platform','2026-09-29T00:00:00Z')")
            original_connect = sqlite3.connect
            def bounded_connect(*args, **kwargs):
                con = original_connect(*args, **kwargs)
                steps = 0
                def limit_work():
                    nonlocal steps
                    steps += 1000
                    return steps > 20000
                con.set_progress_handler(limit_work, 1000)
                return con
            catalog = LocalJobCatalog(db)
            identities = [('ashby', str(i)) for i in range(9000, 9321)]
            with patch('job_search.integration.sqlite3.connect', side_effect=bounded_connect):
                rows = catalog.posting_summaries(identities + [('lever','9000'), ('ashby','absent'), identities[0]])
                self.assertEqual(len(rows), 322)
                self.assertEqual(rows[('ashby','9000')]['title'], 'Role 9000')
                self.assertEqual(rows[('lever','9000')]['title'], 'Different platform')
                self.assertEqual(catalog.posting_summaries([]), {})

    def test_application_snapshot_survives_missing_catalog(self):
        with dashboard() as (server, controller, ledger, preferences):
            result = ledger.start_application(JobSnapshot(ats='ashby', job_id='1', family_id='', title='Engineer', employer='Example', company_slug='example', job_url='https://example.test/1'), RecommendationProvenance(session_id='preview',policy_id='curated',rank=1), MutationContext('preview-start','user','dashboard'))
            application_id = result['application']['application_id']
            before = ledger.get_application_timeline(application_id)
            status, _, data = request(server, 'GET', f'/api/v1/jobs/preview?application_id={application_id}')
            self.assertEqual(status, 200)
            result = json.loads(data)
            self.assertEqual(result['job']['title'], 'Engineer')
            self.assertEqual(result['job']['jobUrl'], 'https://example.test/1')
            self.assertIn('No saved job description', result['description_note'])
            self.assertEqual(ledger.get_application_timeline(application_id), before)


if __name__ == '__main__':
    unittest.main()
