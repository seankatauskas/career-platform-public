"""Private shared-mail review routes preserve exact findings and batch authority."""
import json
from unittest.mock import PropertyMock, patch

from job_search.contracts import ConflictError
from job_search.service import JobSearchLedger
from tests.test_job_search_dashboard import dashboard, request, session, post


class MailReviews:
    def __init__(self):
        self.calls = []
        self.detail = {'analysis_id': 'mail-1', 'revision': 'revision-1', 'mode': 'shared',
                       'subject': '<script>Untrusted mail</script>', 'findings': []}

    def get(self, analysis_id):
        self.calls.append(('get', analysis_id))
        return self.detail

    def list_reviews(self, *, history=False, limit=100):
        self.calls.append(('list', history, limit))
        return [self.detail]

    def decide(self, analysis_id, revision, decisions, context):
        if revision != self.detail['revision']:
            raise ConflictError('mail analysis revision changed')
        self.calls.append(('decide', analysis_id, revision, decisions, context))
        return {**self.detail, 'revision': 'revision-2'}


def test_mail_review_reads_are_private_bounded_and_preserve_exact_quotes():
    service = MailReviews()
    with patch.object(JobSearchLedger, 'mail_understanding', new_callable=PropertyMock, create=True, return_value=service):
        with dashboard() as (server, *_):
            cookie, _ = session(server)
            headers = {'Cookie': cookie}
            status, _, body = request(server, 'GET', '/api/v1/mail-analyses/mail-1', headers=headers)
            assert status == 200 and json.loads(body) == service.detail
            status, _, body = request(server, 'GET', '/api/v1/mail-analyses?history=true&limit=12', headers=headers)
            assert status == 200 and json.loads(body)['analyses'] == [service.detail]
            assert service.calls[-1] == ('list', True, 12)
            for query in ('history=yes', 'history=true&history=false', 'limit=101', 'limit=0', 'extra=true'):
                assert request(server, 'GET', '/api/v1/mail-analyses?' + query, headers=headers)[0] == 400
            assert request(server, 'GET', '/api/v1/mail-analyses/mail-1?raw=true', headers=headers)[0] == 400


def test_batch_decisions_require_csrf_exact_revision_and_user_context():
    service = MailReviews()
    decisions = [{'finding_id': 'receipt', 'decision': 'accepted', 'application_id': 'app-1', 'reason': 'confirmed'},
                 {'finding_id': 'reply', 'decision': 'rejected', 'application_id': None, 'reason': 'not a request'}]
    with patch.object(JobSearchLedger, 'mail_understanding', new_callable=PropertyMock, create=True, return_value=service):
        with dashboard() as (server, *_):
            cookie, csrf = session(server)
            path = '/api/v1/mail-analyses/mail-1/decisions'
            body = {'revision': 'revision-1', 'decisions': decisions}
            assert request(server, 'POST', path, body, {'Cookie': cookie})[0] == 403
            status, _, response = post(server, path, body, cookie, csrf)
            assert status == 200 and json.loads(response)['revision'] == 'revision-2'
            call = service.calls[-1]
            assert call[:4] == ('decide', 'mail-1', 'revision-1', decisions)
            assert call[4].actor_kind == 'user' and call[4].source_kind == 'dashboard_mail_review'
            assert call[4].idempotency_key.startswith('mail-review:')
            calls_before = len(service.calls)
            assert post(server, path, {**body, 'revision': 'old'}, cookie, csrf)[0] == 409
            assert len(service.calls) == calls_before
            for invalid in ({'decisions': decisions}, {**body, 'revision': 1}, {**body, 'decisions': {}}, {**body, 'approve_all': True}):
                assert post(server, path, invalid, cookie, csrf)[0] == 400


if __name__ == '__main__':
    tests = [value for name, value in list(globals().items()) if name.startswith('test_') and callable(value)]
    for test in tests:
        test()
    print(f'ok ({len(tests)} shared mail dashboard tests)')
