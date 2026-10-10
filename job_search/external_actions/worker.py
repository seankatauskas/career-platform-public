"""Fenced dispatch with explicit test or operator-activated production composition."""
from dataclasses import asdict
import uuid

from job_search.commands import DomainError
from .api import PreEffectTransientError, ProviderOutcome


class ExternalActionWorker:
    """Drive one persisted action without holding a transaction across provider I/O.

    context_factory(operation, unique_key) must mint trusted worker capabilities.
    allow_test_dispatch accepts only providers explicitly marked test_only=True;
    Production requires injected persistent activation status and a transactional
    revision guard. Neither authority can be supplied through operation payloads.
    """

    def __init__(self, executor, operations, provider, context_factory, applicability,
                 *, allow_test_dispatch=False, activation_status=None,
                 activation_guard=None, worker_id="external-action-worker", lease_seconds=60, heartbeat=None):
        self.executor = executor
        self.operations = operations
        self.provider = provider
        self.context_factory = context_factory
        self.applicability = applicability
        if (activation_status is None) != (activation_guard is None):
            raise ValueError("Production dispatch requires both activation interfaces")
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 300:
            raise ValueError("Invalid provider lease duration")
        self.test_dispatch = allow_test_dispatch and getattr(provider, "test_only", False) is True
        self.activation_status, self.activation_guard = activation_status, activation_guard
        self.worker_id, self.lease_seconds = worker_id, lease_seconds
        self.heartbeat = heartbeat
        self.enabled = self.test_dispatch or activation_status is not None

    def _activation(self):
        if self.activation_status is not None:
            value = self.activation_status()
            if not isinstance(value, dict) or value.get("paused") is not False or type(value.get("revision")) is not int:
                return None
            return value["revision"]
        return -1 if self.test_dispatch else None

    def _run(self, operation, payload, callback):
        return self.executor.run(self.context_factory(operation, uuid.uuid4().hex), operation, payload, callback)

    def _get(self, action_id):
        with self.executor.read() as con:
            return self.operations.get(con, action_id)

    def execute(self, action_id):
        revision = self._activation()
        if revision is None:
            return {"action_id": action_id, "status": "paused"}
        with self.executor.read() as con:
            if self.executor.restore_quarantined(con, "external_action", action_id):
                return {"action_id": action_id, "status": "restore_quarantined"}
        action = self._run("claim_action", {"action_id": action_id},
                           lambda tx: self.operations.claim_action(tx, action_id, self.worker_id,
                               lease_seconds=self.lease_seconds, activation_revision=revision if revision >= 0 else None,
                               activation_guard=self.activation_guard))
        if action["execution"] != "executing":
            return action
        fence = action["fence"]
        for _ in range(4):
            if self.heartbeat is not None and not self.heartbeat():
                return self._finish(action_id, fence, ProviderOutcome("pre_effect_failure", error_code="worker_lease_lost"))
            action = self._renew(action_id, fence)
            if action["execution"] != "executing":
                return action
            try:
                self.provider.preflight(action)
            except PreEffectTransientError:
                return self._finish(action_id, fence, ProviderOutcome("pre_effect_failure", error_code="preflight_transient"))
            except Exception:
                # Preflight has no effects. Do not leak provider content or tokens.
                return self._finish(action_id, fence, ProviderOutcome("failed", error_code="preflight_failed"))
            if self.heartbeat is not None and not self.heartbeat():
                return self._finish(action_id, fence, ProviderOutcome("pre_effect_failure", error_code="worker_lease_lost"))
            action = self._renew(action_id, fence)
            if action["execution"] != "executing":
                return action
            try:
                action = self._run("begin_write", {"action_id": action_id, "fence": fence, "checkpoint": action["checkpoint"]},
                                   lambda tx: self.operations.begin_write(tx, action_id, fence, applicability=self.applicability,
                                       activation_guard=self.activation_guard))
            except DomainError as exc:
                if exc.code != "version_conflict":
                    raise
                return self._expire(action_id)
            if action["execution"] != "executing":
                return action
            try:
                outcome = self.provider.perform(action)
            except Exception:
                # Even a nominal transient exception cannot prove nonexecution
                # once durable intent exists. Reconciliation must establish that.
                outcome = ProviderOutcome("uncertain", error_code="provider_outcome_unknown")
            action = self._finish(action_id, fence, outcome)
            if action["execution"] != "executing":
                return action
        return self._finish(action_id, fence, ProviderOutcome("uncertain", error_code="provider_step_limit"))

    def _renew(self, action_id, fence):
        try:
            return self._run("claim_action", {"action_id": action_id, "fence": fence, "renew": True},
                lambda tx: self.operations.renew_claim(tx, action_id, fence,
                    lease_seconds=self.lease_seconds, activation_guard=self.activation_guard))
        except DomainError as exc:
            if exc.code != "version_conflict":
                raise
            return self._expire(action_id)

    def _expire(self, action_id):
        self._run("expire_actions", {"action_id": action_id}, self.operations.expire_leases)
        return self._get(action_id)

    def _finish(self, action_id, fence, outcome):
        try:
            return self._run("finish_attempt", {"action_id": action_id, "fence": fence, "outcome": asdict(outcome)},
                             lambda tx: self.operations.finish_attempt(tx, action_id, fence, outcome))
        except DomainError as exc:
            if exc.code != "version_conflict":
                raise
            # The durable intent is enough to quarantine the abandoned attempt.
            # A stale writer cannot apply its receipt under a replacement lease.
            self._run("expire_actions", {"action_id": action_id}, self.operations.expire_leases)
            return self._get(action_id)

    def reconcile(self, action_id):
        if self._activation() is None:
            return {"action_id": action_id, "status": "paused"}
        action = self._get(action_id)
        if action["execution"] not in {"uncertain", "awaiting_confirmation"}:
            return action
        try:
            outcome = self.provider.reconcile(action)
        except Exception:
            outcome = ProviderOutcome("uncertain", error_code="reconciliation_unavailable")
        return self._run("reconcile_result", {"action_id": action_id, "outcome": asdict(outcome)},
                         lambda tx: self.operations.reconcile_result(tx, action_id, outcome, expected_fence=action["fence"]))
