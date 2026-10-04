"""Fixed, lineage-preserving handlers for the opportunity refresh DAG."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .worker import (
    FollowUpTask,
    PermanentTaskError,
    RetryableTaskError,
    TaskContext,
    TaskResult,
    run_command_with_heartbeat,
)


LOCATION_TASK = "opportunity.location_refresh"
PREFERENCE_TASK = "opportunity.preference_refresh"
SALARY_TASK = "opportunity.salary_drain"
NOTIFICATION_TASK = "notification.shortlist_evaluate"
SALARY_BATCH_SIZE = 25


class OpportunityDAG:
    """The only supported post-scrape dependency graph."""

    @staticmethod
    def _task(
        context: TaskContext,
        task_kind: str,
        *,
        lane: str,
        priority: int,
        reason: str,
        delay_seconds: int = 0,
    ) -> FollowUpTask:
        return FollowUpTask(
            task_kind=task_kind,
            payload={
                "workflow_id": context.workflow_id,
                "parent_work_id": context.work_id,
                "source_task": context.task_kind,
                "reason": reason,
            },
            priority=priority,
            max_attempts=5,
            delay_seconds=delay_seconds,
            lane=lane,
            workflow_id=context.workflow_id,
            parent_work_id=context.work_id,
        )

    def after_ats(self, context: TaskContext) -> Sequence[FollowUpTask]:
        if context.task_kind not in {"ats.authoritative", "ats.new_only"}:
            return ()
        return (
            self._task(
                context,
                LOCATION_TASK,
                lane="core",
                priority=60,
                reason="ats_ingested",
            ),
            self._task(
                context,
                SALARY_TASK,
                lane="model",
                priority=-20,
                reason="ats_ingested_salary_side_branch",
            ),
        )

    def after_location(self, context: TaskContext) -> Sequence[FollowUpTask]:
        return (
            self._task(
                context,
                PREFERENCE_TASK,
                lane="model",
                priority=60,
                reason="locations_ready",
            ),
        )

    def after_preference(self, context: TaskContext) -> Sequence[FollowUpTask]:
        return (
            self._task(
                context,
                NOTIFICATION_TASK,
                lane="core",
                priority=20,
                reason="recommendations_stable",
            ),
        )

    def after_salary(
        self, context: TaskContext, *, queue_empty: bool
    ) -> Sequence[FollowUpTask]:
        if queue_empty:
            return (
                self._task(
                    context,
                    NOTIFICATION_TASK,
                    lane="core",
                    priority=10,
                    reason="salary_queue_drained",
                ),
            )
        return (
            self._task(
                context,
                SALARY_TASK,
                lane="model",
                priority=-20,
                reason="salary_batch_remaining",
                delay_seconds=5 * 60,
            ),
        )


class FixedCommand:
    """Execute one code-owned argv; task payload can never inject arguments."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        project_root: Path,
        environment_provider: Callable[[], Mapping[str, str]],
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        timeout_seconds: int = 60 * 60,
    ) -> None:
        if not command or timeout_seconds < 1:
            raise ValueError("fixed command and positive timeout are required")
        self.command = tuple(str(value) for value in command)
        self.project_root = Path(project_root)
        self.environment_provider = environment_provider
        self.runner = runner
        self.timeout_seconds = timeout_seconds

    def run(self, context: TaskContext) -> Mapping[str, Any]:
        environment = dict(self.environment_provider())
        from .inference.usage import scope_environment, UsageDeferred
        environment.update(scope_environment())
        if self.runner is None:
            completed = run_command_with_heartbeat(
                self.command,
                context,
                cwd=str(self.project_root),
                env=environment,
                timeout_seconds=self.timeout_seconds,
            )
        else:
            completed = self.runner(
                self.command,
                cwd=str(self.project_root),
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.timeout_seconds,
                check=False,
                shell=False,
            )
        if completed.returncode:
            if completed.returncode == 76:
                try:
                    value = json.loads(str(completed.stderr or completed.stdout))
                    if set(value) != {"type", "reason", "retry_at"} or value["type"] != "inference_usage_deferred":
                        raise ValueError()
                    raise UsageDeferred(str(value["reason"]), str(value["retry_at"]))
                except (ValueError, TypeError):
                    raise PermanentTaskError("invalid inference deferral result") from None
            error = " ".join(
                str(completed.stderr or completed.stdout or "command failed").split()
            )[:1000]
            if completed.returncode in {2, 64, 78}:
                raise PermanentTaskError(error)
            raise RetryableTaskError(error)
        stdout = str(completed.stdout or "").strip()
        try:
            output: Any = json.loads(stdout) if stdout else {}
        except json.JSONDecodeError:
            output = {"stdout_tail": stdout[-4096:]}
        return {
            "returncode": int(completed.returncode),
            "output": output,
            "stderr_tail": str(completed.stderr or "")[-4096:],
        }


class LocationRefreshHandler:
    def __init__(self, command: FixedCommand, dag: OpportunityDAG) -> None:
        self.command = command
        self.dag = dag

    def __call__(self, payload: Mapping[str, Any], context: TaskContext) -> TaskResult:
        del payload
        result = {"stage": "location_refresh", **self.command.run(context)}
        return TaskResult(result, self.dag.after_location(context))


class PreferenceRefreshHandler:
    def __init__(self, command: FixedCommand, dag: OpportunityDAG) -> None:
        self.command = command
        self.dag = dag

    def __call__(self, payload: Mapping[str, Any], context: TaskContext) -> TaskResult:
        del payload
        result = {"stage": "preference_refresh", **self.command.run(context)}
        return TaskResult(result, self.dag.after_preference(context))


class SalaryDrainHandler:
    """Drain at most 25 model jobs and reschedule the side branch if needed."""

    def __init__(
        self,
        command: FixedCommand,
        dag: OpportunityDAG,
        jobs_db: Path,
        status_provider: Callable[[Path], Mapping[str, Any]] | None = None,
    ) -> None:
        self.command = command
        self.dag = dag
        self.jobs_db = Path(jobs_db)
        self.status_provider = status_provider or self._salary_status

    @staticmethod
    def _salary_status(path: Path) -> Mapping[str, Any]:
        from job_search.salary.llm import status

        return status(path)

    def __call__(self, payload: Mapping[str, Any], context: TaskContext) -> TaskResult:
        del payload
        before = dict(self.status_provider(self.jobs_db))
        if not before.get("activated_at"):
            return TaskResult(
                {
                    "stage": "salary_drain",
                    "activated": False,
                    "processed": 0,
                    "queue_empty": True,
                    "status": before,
                },
                self.dag.after_salary(context, queue_empty=True),
            )
        if int(before.get("actionable") or 0) < 1:
            return TaskResult(
                {
                    "stage": "salary_drain",
                    "activated": True,
                    "processed": 0,
                    "queue_empty": True,
                    "status": before,
                },
                self.dag.after_salary(context, queue_empty=True),
            )
        command_result = self.command.run(context)
        after = dict(self.status_provider(self.jobs_db))
        remaining = int(after.get("actionable") or 0)
        result = {
            "stage": "salary_drain",
            "activated": True,
            "batch_size": SALARY_BATCH_SIZE,
            "queue_empty": remaining == 0,
            "status": after,
            **command_result,
        }
        return TaskResult(
            result,
            self.dag.after_salary(context, queue_empty=remaining == 0),
        )


def model_python(project_root: Path) -> str:
    candidate = Path(project_root) / ".venv-local-mlx" / "bin" / "python"
    return str(candidate) if candidate.is_file() else sys.executable


def build_opportunity_handlers(
    *,
    project_root: Path,
    jobs_db: Path,
    preference_db: Path,
    proxy_db: Path | None = None,
    policy_refresh: bool = False,
    ranking_refresh_mode: str = "full",
    environment_provider: Callable[[], Mapping[str, str]],
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    salary_status_provider: Callable[[Path], Mapping[str, Any]] | None = None,
) -> tuple[OpportunityDAG, Mapping[str, Callable[..., Any]]]:
    root = Path(project_root)
    if ranking_refresh_mode not in {"full", "broad_cpu", "sparse_cpu"}:
        raise ValueError("ranking_refresh_mode must be full, broad_cpu, or sparse_cpu")
    if ranking_refresh_mode in {"broad_cpu", "sparse_cpu"} and not policy_refresh:
        raise ValueError(f"{ranking_refresh_mode} requires named policy refresh")
    refresh_options = ()
    if ranking_refresh_mode == "broad_cpu":
        refresh_options = ("--policy", "broad", "--active-components-only", "--no-embeddings")
    elif ranking_refresh_mode == "sparse_cpu":
        refresh_options = ("--policy", "broad", "--policy", "selective",
                           "--active-components-only", "--no-embeddings")
    python = sys.executable
    salary_python = model_python(root)
    dag = OpportunityDAG()
    location = FixedCommand(
        (
            python,
            "-m", "job_search.collection.locations",
            "backfill",
            "--db",
            str(jobs_db),
        ),
        project_root=root,
        environment_provider=environment_provider,
        runner=runner,
    )
    preference = FixedCommand(
        (
            python,
            "-m", *( ("job_search.ranking.refresh",) if policy_refresh else ("job_search.ranking.model", "refresh") ),
            "--db",
            str(jobs_db),
            "--state-db",
            str(preference_db),
            *(("--proxy-db", str(proxy_db)) if policy_refresh and proxy_db else ()),
            *refresh_options,
        ),
        project_root=root,
        environment_provider=environment_provider,
        runner=runner,
        # Large initial backfills can exceed the collector's one-hour deadline.
        # Ranking checkpoints each batch and continues renewing its work lease.
        timeout_seconds=2 * 60 * 60,
    )
    salary = FixedCommand(
        (
            salary_python,
            "-m", "job_search.salary.llm",
            "--db",
            str(jobs_db),
            "run",
            "--limit",
            str(SALARY_BATCH_SIZE),
        ),
        project_root=root,
        environment_provider=environment_provider,
        runner=runner,
    )
    handlers = {
        LOCATION_TASK: LocationRefreshHandler(location, dag),
        PREFERENCE_TASK: PreferenceRefreshHandler(preference, dag),
        SALARY_TASK: SalaryDrainHandler(
            salary,
            dag,
            jobs_db,
            status_provider=salary_status_provider,
        ),
    }
    return dag, handlers
