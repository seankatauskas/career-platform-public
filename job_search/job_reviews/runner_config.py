"""Explicit configuration for the trusted isolated-review coordinator."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, fields
from pathlib import Path

from ..contracts import ContractError


@dataclass(frozen=True)
class RunnerConfig:
    application_config: Path
    state_dir: Path
    runtime_dir: Path
    auth_home: Path
    model_image: str
    codex_executable: str = 'codex'
    model: str = 'gpt-6-astra'
    reasoning_effort: str = 'high'
    check_model: str | None = None
    check_reasoning_effort: str | None = None
    codex_version: str = '0.160.0'
    concurrency: int = 2
    batch_size: int = 20
    screening_enabled: bool = False
    screening_model: str | None = None
    screening_reasoning_effort: str | None = None
    screening_batch_size: int = 200
    benchmark_check_all: bool = False
    assignment_timeout_seconds: int = 1200
    preload_enabled: bool = True
    invocation_timeout_seconds: int = 3600
    transient_retries: int = 2
    max_collection_age_seconds: int = 43200
    worker_uid: int = 10001
    worker_gid: int = 10001
    schedule_enabled: bool = False
    schedule_calendar: str | None = None
    version: int = 1

    def validate(self):
        if self.version != 1 or type(self.version) is not int:
            raise ContractError('unsupported review runner configuration version')
        for name in ('application_config', 'state_dir', 'runtime_dir', 'auth_home'):
            if not isinstance(getattr(self, name), Path) or not getattr(self, name).is_absolute():
                raise ContractError(f'{name} must be an absolute path')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[a-f0-9]{64}', self.model_image):
            raise ContractError('review model_image must be pinned by sha256 digest')
        for name in ('codex_executable', 'model', 'reasoning_effort', 'codex_version'):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or '\x00' in value or '\n' in value:
                raise ContractError(f'{name} must be explicit nonempty text')
        for name, low, high in (
            ('concurrency', 1, 32), ('batch_size', 1, 20), ('screening_batch_size', 1, 200),
            ('assignment_timeout_seconds', 30, 1200),
            ('invocation_timeout_seconds', 10, 86400), ('transient_retries', 0, 2),
            ('max_collection_age_seconds', 60, 604800),
            ('worker_uid', 1, 65535), ('worker_gid', 1, 65535),
        ):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ContractError(f'{name} must be an integer between {low} and {high}')
        if self.check_model is not None and (not isinstance(self.check_model, str)
                or not self.check_model.strip() or '\x00' in self.check_model or '\n' in self.check_model):
            raise ContractError('check_model must be explicit nonempty text when set')
        if self.check_reasoning_effort is not None and self.check_reasoning_effort not in ('low', 'medium', 'high'):
            raise ContractError('check reasoning effort must be low, medium or high')
        if type(self.schedule_enabled) is not bool:
            raise ContractError('schedule_enabled must be boolean')
        if type(self.preload_enabled) is not bool:
            raise ContractError('preload_enabled must be boolean')
        if type(self.screening_enabled) is not bool:
            raise ContractError('screening_enabled must be boolean')
        if type(self.benchmark_check_all) is not bool:
            raise ContractError('benchmark_check_all must be boolean')
        if self.screening_enabled:
            if not self.preload_enabled:
                raise ContractError('screening requires complete preloaded evidence')
            for name in ('screening_model', 'screening_reasoning_effort'):
                value = getattr(self, name)
                if not isinstance(value, str) or not value.strip() or '\x00' in value or '\n' in value:
                    raise ContractError(f'{name} must be explicit nonempty text when screening is enabled')
            if self.screening_reasoning_effort not in ('low', 'medium', 'high'):
                raise ContractError('screening reasoning effort must be low, medium or high')
        elif self.screening_model is not None or self.screening_reasoning_effort is not None:
            raise ContractError('screening model and effort require screening_enabled')
        if self.schedule_calendar is not None and (
            not isinstance(self.schedule_calendar, str) or not self.schedule_calendar.strip()
            or '\n' in self.schedule_calendar or '\x00' in self.schedule_calendar
        ):
            raise ContractError('schedule_calendar must be a single nonempty calendar expression')
        if self.schedule_enabled and not self.schedule_calendar:
            raise ContractError('scheduled reviews require an explicitly selected calendar')
        return self

    def check_profile(self):
        return {'model': self.model if self.check_model is None else self.check_model,
                'reasoning_effort': self.reasoning_effort if self.check_reasoning_effort is None else self.check_reasoning_effort}

    def execution_policy(self):
        policy = {'version': 1,
                'detailed': {'model': self.model, 'reasoning_effort': self.reasoning_effort},
                'check': self.check_profile(),
                'screening': {'model': self.screening_model, 'reasoning_effort': self.screening_reasoning_effort}
                    if self.screening_enabled else None,
                'concurrency': self.concurrency, 'batch_size': self.batch_size,
                'screening_batch_size': self.screening_batch_size}
        if self.benchmark_check_all:
            policy['extra_check_all'] = True
        return policy


def load_runner_config(path):
    path = Path(path).expanduser().resolve()
    try:
        raw = path.read_bytes()
        if len(raw) > 65536:
            raise ContractError('review runner configuration exceeds 64 KiB')
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) - {f.name for f in fields(RunnerConfig)}:
            raise ContractError('unknown review runner configuration fields')
        for key in ('application_config', 'state_dir', 'runtime_dir', 'auth_home'):
            if key in value:
                if not isinstance(value[key], str):
                    raise ContractError(f'{key} must be a path')
                value[key] = Path(value[key]).expanduser()
        return RunnerConfig(**value).validate()
    except (OSError, ValueError, TypeError) as exc:
        if isinstance(exc, ContractError):
            raise
        raise ContractError('invalid or unreadable review runner configuration') from None
