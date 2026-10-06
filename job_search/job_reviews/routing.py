"""Trusted frozen policy and conservative, independently auditable screening routes."""
from __future__ import annotations

from ..contracts import ContractError, canonical_json, payload_sha256
from .contracts import MATERIAL_FIELDS, exact, identifier, integer, text
from .service import _ReviewPrincipal, unpack

MAX_SCREENING_JOBS = 200
MAX_BULK_ITEMS = 20
FAMILIES = ('frontend', 'backend', 'full_stack', 'mobile', 'platform', 'applied_ai',
            'fde', 'data', 'security', 'quality', 'other_technical', 'non_technical')


class RoutingAuthorityMixin:
    def _validate_execution_policy(self, policy):
        exact(policy, ('version', 'detailed', 'screening', 'concurrency', 'batch_size',
                       'screening_batch_size', 'extra_check_all', 'check'), ('version', 'detailed', 'screening',
                       'concurrency', 'batch_size', 'screening_batch_size'))
        if type(policy.get('extra_check_all', False)) is not bool:
            raise ContractError('extra_check_all must be boolean')
        # Omitted and explicit False retain the original frozen policy hash.
        policy = dict(policy)
        # A historical policy without a checker profile always means its own
        # detailed profile, never a newly configured checker default.
        policy.setdefault('check', policy['detailed'])
        if not policy.get('extra_check_all'):
            policy.pop('extra_check_all', None)
        if type(policy['version']) is not int or policy['version'] != 1:
            raise ContractError('unsupported execution policy')
        integer(policy['concurrency'], 'concurrency', 1, 32)
        integer(policy['batch_size'], 'batch_size', 1, 20)
        integer(policy['screening_batch_size'], 'screening_batch_size', 1, MAX_SCREENING_JOBS)
        for purpose in ('detailed', 'screening', 'check'):
            profile = policy[purpose]
            if purpose == 'screening' and profile is None:
                continue
            exact(profile, ('model', 'reasoning_effort'), ('model', 'reasoning_effort'))
            for key in profile:
                identifier(profile[key], key)
            expected = ({'model': self.approved_check_model, 'reasoning_effort': self.approved_check_reasoning_effort}
                        if purpose == 'check' else {'model': self.approved_model, 'reasoning_effort': self.approved_reasoning_effort}
                        if purpose == 'detailed' else {'model': self.approved_screening_model,
                        'reasoning_effort': self.approved_screening_reasoning_effort})
            if profile != expected:
                raise ContractError('execution profile must match trusted runtime configuration')
        return unpack(canonical_json(policy))

    def _freeze_execution_policy(self, con, review_id, policy):
        from .authority import ReviewConflictError
        from .finalizer import ALL_GRANTS
        policy = self._validate_execution_policy(policy)
        run = self._managed_run(con, review_id)
        if policy['concurrency'] > 16 and not unpack(run['metadata_json']).get('benchmark'):
            raise ContractError('concurrency above 16 is restricted to benchmark reviews')
        if policy.get('extra_check_all') and not unpack(run['metadata_json']).get('benchmark'):
            raise ContractError('extra checks are restricted to benchmark reviews')
        digest = payload_sha256(policy)
        prior = con.execute('SELECT * FROM job_review_execution_policies WHERE review_id=?', (review_id,)).fetchone()
        if prior:
            stored_policy = unpack(prior['policy_json'])
            if (prior['policy_sha256'] != payload_sha256(stored_policy)
                    or self._validate_execution_policy(stored_policy) != policy):
                raise ReviewConflictError('review execution policy is already frozen')
            return {'policy': policy, 'policy_sha256': prior['policy_sha256']}
        grants = con.execute('SELECT * FROM ' + ALL_GRANTS + ' WHERE review_id=?', (review_id,)).fetchall()
        assessed = con.execute('SELECT 1 FROM job_review_items WHERE review_id=? AND assessment_json IS NOT NULL', (review_id,)).fetchone()
        if (grants or assessed) and policy['check'] != policy['detailed']:
            raise ReviewConflictError('legacy review work requires its original detailed checker profile')
        if policy['screening'] is not None:
            if unpack(run['context_json']).get('rubric_version') != 'job-review-v2':
                raise ContractError('screening requires a v2 review')
            if grants or assessed:
                raise ReviewConflictError('screening cannot adopt existing reviewer work')
        for grant in grants:
            runtime = unpack(grant['runtime_json'])
            if any(runtime.get(key) != value for key, value in policy['detailed'].items()):
                raise ReviewConflictError('legacy review runtime differs from execution policy')
        con.execute('INSERT INTO job_review_execution_policies VALUES(?,?,?,?)',
                    (review_id, canonical_json(policy), digest, self._now()))
        return {'policy': policy, 'policy_sha256': digest}

    def freeze_execution_policy(self, review_id, policy):
        with self._transaction() as con:
            return self._freeze_execution_policy(con, review_id, policy)

    def _ensure_execution_policy(self, con, review_id):
        prior = con.execute('SELECT policy_json FROM job_review_execution_policies WHERE review_id=?', (review_id,)).fetchone()
        if prior:
            policy = self._validate_execution_policy(unpack(prior[0]))
            if policy['concurrency'] > 16 and not unpack(self._managed_run(con, review_id)['metadata_json']).get('benchmark'):
                raise ContractError('concurrency above 16 is restricted to benchmark reviews')
            return policy
        # Legacy direct callers retain detailed-only semantics. The coordinator
        # explicitly freezes an enabled screening profile before dispatch.
        policy = {'version': 1, 'detailed': {'model': self.approved_model,
                  'reasoning_effort': self.approved_reasoning_effort}, 'screening': None,
                  'check': {'model': self.approved_check_model, 'reasoning_effort': self.approved_check_reasoning_effort},
                  'concurrency': 2, 'batch_size': 20, 'screening_batch_size': 200}
        return self._freeze_execution_policy(con, review_id, policy)['policy']

    def _route_for_item(self, con, run, item):
        return con.execute('SELECT * FROM job_review_routes WHERE review_id=? AND ordinal=? '
            'AND expected_revision=? AND snapshot_sha256=? AND context_sha256=?',
            (run['review_id'], item['ordinal'], item['revision'], item['snapshot_sha256'], run['context_sha256'])).fetchone()

    def _pending_routing(self, review_id, purpose, after, limit):
        integer(after, 'after')
        integer(limit, 'limit', 1, MAX_SCREENING_JOBS)
        with self._transaction() as con:
            run = self._managed_run(con, review_id)
            policy = self._ensure_execution_policy(con, review_id)
            found = []
            for item in con.execute('SELECT * FROM job_review_items WHERE review_id=? '
                                    'AND ordinal>? AND assessment_json IS NULL ORDER BY ordinal', (review_id, after)):
                route = self._route_for_item(con, run, item)
                wanted = ((policy['screening'] is not None and route is None) if purpose == 'screening'
                          else policy['screening'] is None or route is not None and route['route'] == 'detailed')
                if wanted:
                    found.append(item['ordinal'])
                    if len(found) > limit:
                        break
            return {'ordinals': found[:limit], 'next_after': found[limit - 1] if len(found) > limit else None}

    def pending_routes(self, review_id, after=0, limit=200):
        return self._pending_routing(review_id, 'screening', after, limit)

    def pending_detailed(self, review_id, after=0, limit=200):
        return self._pending_routing(review_id, 'detailed', after, limit)

    def _save_route(self, con, run, item, entry, grant, receipt):
        con.execute('INSERT INTO job_review_routes VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            (run['review_id'], item['ordinal'], item['revision'], item['snapshot_sha256'], run['context_sha256'],
             entry['route'], grant['grant_id'] if grant else None, grant['actor'] if grant else 'isolated-coordinator',
             payload_sha256(entry), canonical_json(entry), canonical_json(receipt), self._now()))

    def route_oversize(self, review_id, ordinal):
        """A trusted packing decision may defer work, never exclude it."""
        from .authority import ReviewConflictError
        with self._transaction() as con:
            run = self._managed_run(con, review_id)
            if self._ensure_execution_policy(con, review_id)['screening'] is None:
                raise ContractError('review has no screening stage')
            item = self.service._item(con, {'review_id': review_id, 'ordinal': ordinal})
            if item['assessment_json'] is not None:
                raise ReviewConflictError('already assessed posting cannot be routed')
            prior = self._route_for_item(con, run, item)
            if prior:
                return unpack(prior['response_json'])
            if item['claim_owner'] and item['claim_until'] and item['claim_until'] > self._now():
                raise ReviewConflictError('posting has an active reviewer lease')
            entry = {'ordinal': ordinal, 'route': 'detailed', 'reason': 'oversize_source_packet'}
            receipt = {'review_id': review_id, 'ordinal': ordinal, 'route': 'detailed', 'revision': item['revision']}
            self._save_route(con, run, item, entry, None, receipt)
            return receipt

    def _route_assessment(self, con, grant, item, entry):
        """Expand fixed exclusion metadata only; never synthesize a recommendation."""
        exact(entry, ('ordinal', 'route', 'reason_code', 'explanation', 'evidence', 'brief_revision', 'family', 'alignment'),
                    ('ordinal', 'route', 'reason_code', 'explanation', 'evidence'))
        reason = entry['reason_code']
        if reason not in ('non_technical', 'location'):
            raise ContractError('screening cannot decide qualifications or duplicates')
        explanation = text(entry['explanation'], 'explanation', 500)
        evidence = entry['evidence']
        if not isinstance(evidence, list) or not 1 <= len(evidence) <= 3:
            raise ContractError('screening needs one to three exact source quotes')
        for quote in evidence:
            exact(quote, ('field', 'quote'), ('field', 'quote'))
            if quote['field'] not in MATERIAL_FIELDS:
                raise ContractError('invalid screening evidence field')
            text(quote['quote'], 'quote', 300)
        if not any(quote['field'] == 'description' for quote in evidence):
            raise ContractError('screening exclusions require description evidence')
        job = unpack(item['snapshot_json'])
        read = con.execute('SELECT through_offset FROM job_review_reads WHERE review_id=? AND ordinal=? '
                           'AND actor=? AND snapshot_sha256=?', (grant['review_id'], item['ordinal'],
                           grant['actor'], item['snapshot_sha256'])).fetchone()
        if not job.get('description') or not read or read[0] != len(job['description']):
            raise ContractError('read the full description before excluding a posting')
        family, alignment = 'non_technical', 'unrelated'
        if reason == 'location':
            if not any(quote['field'] == 'location' for quote in evidence):
                raise ContractError('location screening requires an exact posting location quote')
            run = self._managed_run(con, grant['review_id'], active=False)
            brief = unpack(run['context_json']).get('search_brief') or {}
            revision = integer(entry.get('brief_revision'), 'brief_revision', 1)
            if (revision != brief.get('revision') or any(brief.get('brief', {}).get(key) != 'us'
                    for key in ('broad_geography', 'targeted_geography'))):
                raise ContractError('location screening requires the saved US-only broad and targeted scope')
            family, alignment = entry.get('family'), entry.get('alignment')
            if family not in FAMILIES or alignment not in ('core', 'adjacent', 'unrelated', 'unknown'):
                raise ContractError('location exclusion needs family and career alignment')
        elif any(key in entry for key in ('brief_revision', 'family', 'alignment')):
            raise ContractError('nontechnical screening metadata is fixed')
        return {'stage': 'screening', 'decision': 'exclude', 'family': family, 'alignment': alignment,
                'reason_code': reason, 'explanation': explanation, 'evidence': evidence,
                'strengths': [], 'gaps': [], 'unknowns': [], 'borderline': False,
                'eligibility': 'no_known_barrier', 'eligibility_condition': '', 'next_step': 'explore', 'category': 'core'}

    def _route_one(self, con, grant, entry):
        from .authority import ReviewConflictError, _command_key
        slot = self._slot(con, grant, entry.get('ordinal'))
        prior = con.execute('SELECT * FROM job_review_routes WHERE grant_id=? AND ordinal=?',
                            (grant['grant_id'], slot['ordinal'])).fetchone()
        if prior:
            if prior['request_sha256'] != payload_sha256(entry):
                raise ReviewConflictError('route replay conflicts with its saved receipt')
            return unpack(prior['response_json'])
        item = self._current(con, grant, slot)
        run = self._managed_run(con, grant['review_id'])
        if self._route_for_item(con, run, item):
            raise ReviewConflictError('posting already has a durable route')
        route = entry.get('route')
        if route == 'detailed':
            exact(entry, ('ordinal', 'route'), ('ordinal', 'route'))
            receipt = {'review_id': grant['review_id'], 'ordinal': slot['ordinal'], 'route': route, 'revision': item['revision']}
            con.execute('INSERT INTO job_review_commands VALUES(?,?,?,?,?)',
                        (_command_key(grant['grant_id'], slot['ordinal']), 'route', payload_sha256(entry),
                         canonical_json(receipt), self._now()))
        elif route == 'exclude':
            value = self._route_assessment(con, grant, item, entry)
            receipt = self._assess(con, grant, {'ordinal': slot['ordinal'], 'assessment': value},
                                  _ReviewPrincipal('reviewer', grant['review_id'], grant['actor'], 'primary'))
            receipt = dict(receipt, route=route)
        else:
            raise ContractError('screening can only exclude or route to detailed review')
        self._save_route(con, run, item, entry, grant, receipt)
        con.execute('UPDATE job_review_items SET claim_owner=NULL,claim_until=NULL WHERE review_id=? AND ordinal=? AND claim_owner=?',
                    (grant['review_id'], slot['ordinal'], grant['actor']))
        return receipt
