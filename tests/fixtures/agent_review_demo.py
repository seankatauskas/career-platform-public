"""Fictional, loopback-only dashboard fixture for agent review browser acceptance."""
import json
import signal

from job_search.dashboard import DashboardController, make_server
from tests.test_agent_job_reviews import ReviewTests
from tests.test_job_search_dashboard import FakePreferences


def main():
    fixture = ReviewTests()
    fixture.setUp()
    fixture.insert('b', title='Backend Engineer')
    review = fixture.start()
    fixture.assess(review, ordinal=1)
    fixture.assess(review, ordinal=1, kind='check')
    fixture.assess(review, ordinal=2, value=fixture.assessment('broad_only'))
    receipt = fixture.finish(review)
    active = fixture.start()
    controller = DashboardController(fixture.ledger, FakePreferences(), jobs=fixture.catalog)
    controller.job_reviews = fixture.service
    server = make_server(controller, port=0)
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    print(json.dumps({'url': f'http://127.0.0.1:{server.server_address[1]}',
                      'review_id': review, 'active_review_id': active, 'lists': receipt['lists']}), flush=True)
    try:
        server.serve_forever(poll_interval=.05)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        fixture.doCleanups()


if __name__ == '__main__':
    main()
