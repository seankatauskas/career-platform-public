"""Isolated real dashboard and owner services for catalog email review."""
import json
import sqlite3
from pathlib import Path

from job_search.application_gateway import ApplicationGateway
from job_search.dashboard import DashboardController, make_server
from job_search.service import JobSearchLedger
from tests.test_job_search_dashboard import FakePreferences
from tests.test_review_application_discovery import DiscoveryTest


def main():
    fixture = DiscoveryTest()
    fixture.setUp()
    try:
        with sqlite3.connect(fixture.catalog.jobs_db) as con:
            con.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?)', ('ashby', 'catalog-platform', 'Northwind',
                'Platform Engineer', 'https://northwind.example/jobs/platform', None))
        source, _ = fixture.message(text='Thank you for applying for the Platform Engineer position at Northwind.')
        ledger = JobSearchLedger(Path(fixture.tmp.name) / 'operations.db')
        gateway = ApplicationGateway(fixture.runtime, ledger, catalog=fixture.catalog)
        controller = DashboardController(ledger, FakePreferences(), jobs=fixture.catalog, application_gateway=gateway, demo_mode=True)
        server = make_server(controller, port=0)
        print(json.dumps({'url':'http://127.0.0.1:' + str(server.server_port), 'group_id':'email:' + source['id']}), flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    finally:
        fixture.tmp.cleanup()


if __name__ == '__main__':
    main()
