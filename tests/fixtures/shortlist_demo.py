"""Isolated shortlist preview with a 30-second, entirely local generation demo."""
import argparse
import json
import signal
import threading
from datetime import timedelta
from contextlib import closing

from job_search.dashboard import DashboardController, make_server
from job_search.db import connect
from job_search.job_reviews.contracts import stamp, timestamp
from tests.test_list_generator import GeneratorTests
from tests.test_job_search_dashboard import FakePreferences
from tests.test_old_method import assessment


def prepare_result(fixture, rid, examples):
    review = fixture.workspace(rid)
    by_id = {e[0]:e for e in examples}
    order = []
    for n, job in enumerate(review.jobs, 1):
        e = by_id[job['id'].split('--')[0]]
        value = assessment(e[4]); value['explanation'] = e[5]
        value['priority'] = next(i for i, item in enumerate(examples, 1) if item[0] == e[0])
        review.sources([n]); review.save([{'ordinal': n, 'assessment': value}])
        order.append((value['priority'], n))
    review.finish([n for _, n in sorted(order)])
    fixture.store.checkpoint(rid, review.state, sealed=True)


class DemoGeneration:
    """Drive real local publication with scripted assessments, never a model/host."""

    def __init__(self, fixture, examples):
        self.fixture = fixture
        self.examples = examples
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.worker = None

    def __getattr__(self, name):
        return getattr(self.fixture.service, name)

    def call(self, action, args=None):
        with self.lock:
            if action == 'generate':
                status = self.fixture.service.call('generation-status')
                if status.get('status') != 'active':
                    end = timestamp(status['covered_through']) + timedelta(hours=1)
                    self.fixture.service.now = lambda: stamp(end)
            result = self.fixture.service.call(action, args)
            if action in ('generate', 'generation-resume') and result.get('status') == 'active':
                if self.worker is None or not self.worker.is_alive():
                    self.worker = threading.Thread(target=self.run, args=(result,), daemon=True)
                    self.worker.start()
            return result

    def run(self, run):
        fixture, rid = self.fixture, run['review_id']
        end = timestamp(run['window_end'])
        try:
            # Frontend polls every ten seconds: queued now, reviewing at 10s,
            # validating at 20s, then saved lists at 30s.
            if self.stop.wait(5):
                return
            fixture.service.now = lambda: stamp(end + timedelta(seconds=2))
            if not fixture.store.metadata(rid).get('inputs_ready'):
                for jid, company, title, location, decision, explanation in self.examples:
                    fixture.insert(jid + '--' + rid, company=company, title=title, location=location,
                        jobUrl='https://example.com/jobs/' + jid,
                        posted_at=stamp(end - timedelta(minutes=10)),
                        first_seen=stamp(end - timedelta(minutes=1)),
                        last_seen=stamp(end - timedelta(minutes=1)), source_updated_at=None)
                if not fixture.store.freeze_generation(rid, execution=fixture.execution):
                    raise RuntimeError('Demo inputs could not be frozen')
            fixture.store.update(rid, phase='running')
            if self.stop.wait(10):
                return
            prepare_result(fixture, rid, self.examples)
            fixture.store.update(rid, phase='validating')
            if self.stop.wait(10):
                return
            fixture.store.publish(rid, fixture.observations(rid))
        except Exception as error:
            fixture.store.update(rid, phase='failed', error=str(error))

    def close(self):
        self.stop.set()
        if self.worker:
            self.worker.join()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=0)
    args = parser.parse_args()
    fixture = GeneratorTests(); fixture.setUp()
    try:
        with closing(connect(fixture.jobs)) as con, con:
            con.execute('DELETE FROM jobs')
        examples = [
            ('northstar', 'Northstar', 'Software Engineer, Backend', 'Chicago, IL · Hybrid', 'close',
             'Build Python APIs and the services behind a growing logistics product. Your integration work is a strong match; the main gap is operating these systems at larger production scale.'),
            ('fieldnote', 'Fieldnote', 'Software Engineer, Integrations', 'Remote · United States', 'close',
             'Own partner integrations and improve developer tooling. Your Agoda API experience maps directly to the work, with room to grow into more independent system design.'),
            ('harbor', 'Harbor', 'Full Stack Engineer', 'New York, NY · Hybrid', 'slight_stretch',
             'Build customer-facing features across React and Python services. The backend work fits well; stronger frontend ownership and testing experience would help you compete.'),
            ('juniper', 'Juniper', 'Platform Engineer', 'Remote · United States', 'slight_stretch',
             'Improve deployment tooling and internal services for a small engineering team. Your software background is relevant, though the role expects hands-on cloud infrastructure experience.'),
            ('relay', 'Relay', 'Software Engineer, Data Products', 'Austin, TX · Hybrid', 'bigger_stretch',
             'Turn operational data into reliable APIs and internal tools. Python and integration skills transfer well; production data pipelines are the largest experience gap.'),
            ('clearpath', 'Clearpath', 'Solutions Engineer', 'Remote · United States', 'broad_only',
             'Help customers connect their systems through APIs and lightweight implementation work. A credible adjacent option if you want more customer interaction alongside engineering.'),
        ]
        for jid, company, title, location, decision, explanation in examples:
            fixture.insert(jid, company=company, title=title, location=location,
                jobUrl='https://example.com/jobs/'+jid, posted_at='2026-10-10T11:00:00Z', first_seen='2026-10-10T16:00:00Z', source_updated_at=None, last_seen='2026-10-10T16:00:00Z')
        fixture.service.now = lambda: '2026-10-10T17:00:00Z'
        rid = fixture.generate(initial_start='2026-10-10T06:00:00Z')['review_id']
        fixture.service.now = lambda: '2026-10-10T17:02:00Z'
        fixture.store.freeze_generation(rid, execution=fixture.execution)
        prepare_result(fixture, rid, examples)
        fixture.store.publish(rid, fixture.observations(rid))
        controller = DashboardController(fixture.ledger, FakePreferences(), jobs=fixture.service.catalog, demo_mode=True)
        demo = DemoGeneration(fixture, examples)
        controller.job_reviews = demo
        server = make_server(controller, port=args.port)
        signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
        print(json.dumps({'url': f'http://127.0.0.1:{server.server_address[1]}/#shortlist'}), flush=True)
        try:
            server.serve_forever(poll_interval=.1)
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
            demo.close()
    finally:
        fixture.doCleanups()


if __name__ == '__main__':
    main()
