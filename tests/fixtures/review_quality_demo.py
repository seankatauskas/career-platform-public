"""Fictional v2 review through the real ledger, served only on loopback."""
import copy
import json
import signal

from job_search.dashboard import DashboardController, make_server
from job_search.job_reviews.brief import suggested_search_brief
from tests.test_agent_job_reviews import ReviewTests
from tests.test_job_search_dashboard import FakePreferences


def build_fixture():
    fixture = ReviewTests()
    fixture.setUp()
    fixture.insert('b', title='Software Engineer, Applied AI')
    fixture.insert('c', title='Senior Backend Engineer', company='Northstar')
    fixture.insert('d', title='Technical Customer Success', company='Harbor')
    fixture.profile['source_inventory'] = {
        'career_fact_count': 1, 'resume_fact_count': 0, 'total_fact_count': 1,
        'facts_by_source': {'experience': 1}, 'pending_career_draft': False,
    }
    fixture.service.availability_checker = lambda jobs: [
        {'ats': job['ats'], 'job_id': job['id'],
         'status': 'unknown' if job['id'] == 'c' else 'open',
         'checked_at': fixture.now, 'source': 'https://example.test/board',
         'reason': 'fictional_fixture_observation'} for job in jobs]
    brief = suggested_search_brief()
    fixture.command('save-brief', brief=brief, expected_revision=0)
    rid = fixture.command('start', mode='custom', rubric_version='job-review-v2',
                          window_start='2026-10-01T00:00:00Z')['review_id']
    common = dict(eligibility='no_known_barrier', eligibility_condition='',
                  next_step='apply', category='core', unknowns=[])
    assessments = [
        fixture.assessment(**{**common, 'eligibility': 'unresolved',
            'eligibility_condition': 'Confirm whether the role requires an active clearance.',
            'next_step': 'clarify',
            'explanation': 'The Python API and SQL work matches the core build responsibilities.',
            'gaps': ['Production ownership at the advertised scale is not established.']}),
        fixture.assessment('slight_stretch', **{**common,
            'explanation': 'Python API development transfers to the application layer of an AI product.',
            'gaps': ['No production model-evaluation experience is documented.']}),
        fixture.assessment('bigger_stretch', **{**common,
            'explanation': 'Python and SQL align with the implementation work, but the senior scope is a substantial stretch.',
            'gaps': ['Senior technical leadership and multi-team delivery are not established.']}),
        fixture.assessment('broad_only', **{**common, 'alignment': 'adjacent',
            'category': 'alternative', 'next_step': 'explore',
            'explanation': 'Technical troubleshooting could use the Python and SQL background.',
            'gaps': ['Commercial account ownership differs from the software-building direction.']}),
    ]
    for ordinal, assessment in enumerate(assessments, 1):
        fixture.assess(rid, ordinal=ordinal, value=assessment)
        fixture.assess(rid, ordinal=ordinal, kind='check', value=copy.deepcopy(assessment))
    basis = fixture.command('calibration', review_id=rid)['basis_sha256']
    for ordinal in range(1, 5):
        fixture.command('calibrate', review_id=rid, basis_sha256=basis,
                        ordinal=ordinal, position=ordinal,
                        related_group={'id': 'example-build', 'label': 'Example software roles'} if ordinal < 3 else None)
    fixture.command('finalize', review_id=rid, basis_sha256=basis)
    fixture.command('verify-availability', review_id=rid)
    receipt = fixture.finish(rid)
    return fixture, rid, receipt


def main():
    fixture, rid, receipt = build_fixture()
    controller = DashboardController(fixture.ledger, FakePreferences(), jobs=fixture.catalog)
    controller.job_reviews = fixture.service
    server = make_server(controller, port=0)
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    print(json.dumps({'url': f'http://127.0.0.1:{server.server_address[1]}',
                      'review_id': rid, 'lists': receipt['lists']}), flush=True)
    try:
        server.serve_forever(poll_interval=.05)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        fixture.doCleanups()


if __name__ == '__main__':
    main()
