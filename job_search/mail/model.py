"""Fail-closed adapter for a local JSON-in/JSON-out classifier command."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from job_search.contracts import ContractError, validate_identifier

from .context import CandidateApplication, bounded_candidates
from .proposals import MAIL_EVENT_TYPES, MODEL_OUTPUT_FIELDS


MAX_MODEL_OUTPUT_BYTES = 64 * 1024
CLASSIFIER_CONFIG_VERSION = 1


class ModelExecutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class LocalClassifierConfig:
    producer_version: str
    command: tuple[str, ...]
    allowed_read_paths: tuple[Path, ...]
    timeout_seconds: float
    version: int = 1

    def build(self) -> "LocalCommandClassifier":
        if self.version != 1:
            raise ContractError("version 2 mail config requires the shared understanding adapter")
        return LocalCommandClassifier(
            self.command,
            allowed_read_paths=self.allowed_read_paths,
            timeout_seconds=self.timeout_seconds,
        )

    def build_understanding(self):
        if self.version != 2:
            raise ContractError("shared mail understanding requires local config version 2")
        from .understanding_adapters import LocalMailUnderstandingAnalyzer

        return LocalMailUnderstandingAnalyzer(
            self.command,
            producer_version=self.producer_version,
            allowed_read_paths=self.allowed_read_paths,
            timeout_seconds=self.timeout_seconds,
        )


def load_classifier_config(path: Path) -> LocalClassifierConfig:
    """Load an owner-only, versioned argv configuration without shell parsing."""

    resolved = Path(path).expanduser().resolve()
    if os.stat(resolved).st_mode & 0o077:
        raise ContractError("mail classifier config must have mode 0600")
    try:
        raw = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError("mail classifier config is not valid UTF-8 JSON") from exc
    if not isinstance(raw, Mapping):
        raise ContractError("mail classifier config must be an object")
    allowed = {"version", "producer_version", "command", "allowed_read_paths", "timeout_seconds"}
    if set(raw) != allowed:
        raise ContractError("mail classifier config fields do not match the supported schema")
    if type(raw.get("version")) is not int or raw["version"] not in {CLASSIFIER_CONFIG_VERSION, 2}:
        raise ContractError("mail classifier config version must be 1 or 2")
    producer_version = raw.get("producer_version")
    validate_identifier(producer_version, "producer_version")
    command = raw.get("command")
    if (
        not isinstance(command, list)
        or not 1 <= len(command) <= 64
        or any(not isinstance(item, str) or not item or len(item) > 4096 for item in command)
    ):
        raise ContractError("mail classifier command must be a bounded argv array")
    read_paths = raw.get("allowed_read_paths")
    if not isinstance(read_paths, list) or len(read_paths) > 32:
        raise ContractError("allowed_read_paths must be a bounded array")
    normalized_paths = []
    for item in read_paths:
        if not isinstance(item, str) or not item or len(item) > 4096:
            raise ContractError("allowed_read_paths entries must be paths")
        candidate = Path(item).expanduser()
        if not candidate.is_absolute():
            raise ContractError("allowed_read_paths entries must be absolute")
        normalized_paths.append(candidate.resolve())
    timeout = raw.get("timeout_seconds")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ContractError("timeout_seconds must be a number")
    timeout_value = float(timeout)
    if not 1 <= timeout_value <= 300:
        raise ContractError("timeout_seconds must be between 1 and 300")
    return LocalClassifierConfig(
        str(producer_version),
        tuple(command),
        tuple(normalized_paths),
        timeout_value,
        raw["version"],
    )


def _sandbox_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def macos_sandbox_command(
    command: Sequence[str],
    temporary_directory: Path,
    allowed_read_paths: Sequence[Path],
) -> tuple[str, ...]:
    """Wrap a command in a deny-by-default macOS sandbox with no network."""

    sandbox_exec = Path("/usr/bin/sandbox-exec")
    if sys.platform != "darwin" or not sandbox_exec.exists():
        raise ModelExecutionError("local model isolation is unavailable on this host")
    executable = shutil.which(command[0]) if not Path(command[0]).is_absolute() else command[0]
    if not executable:
        raise ModelExecutionError("local model executable was not found")
    read_paths = {
        Path("/System"), Path("/usr"), Path("/Library"), Path(executable),
        *(Path(item).expanduser().resolve() for item in allowed_read_paths),
    }
    clauses = [
        '(version 1)',
        '(deny default)',
        '(deny network*)',
        '(allow process-info*)',
        f'(allow process-exec (literal "{_sandbox_literal(str(executable))}"))',
        f'(allow file-read* file-write* (subpath "{_sandbox_literal(str(temporary_directory))}"))',
        '(allow file-read* (literal "/dev/null") (literal "/dev/urandom"))',
    ]
    for path in sorted(read_paths, key=lambda item: str(item)):
        kind = "subpath" if path.is_dir() else "literal"
        clauses.append(f'(allow file-read* ({kind} "{_sandbox_literal(str(path))}"))')
    profile = "\n".join(clauses)
    return (str(sandbox_exec), "-p", profile, "--", executable, *command[1:])


class LocalCommandClassifier:
    """Invoke a fixed local model command without shell, inherited secrets, or I/O tools."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        allowed_read_paths: Sequence[Path] = (),
        timeout_seconds: float = 120.0,
        isolation_builder: Callable[[Sequence[str], Path, Sequence[Path]], Sequence[str]] = macos_sandbox_command,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        if not command or not all(isinstance(item, str) and item for item in command):
            raise ValueError("command must be a non-empty argv sequence")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.command = tuple(command)
        self.allowed_read_paths = tuple(Path(item) for item in allowed_read_paths)
        self.timeout_seconds = timeout_seconds
        self.isolation_builder = isolation_builder
        self.runner = runner

    def classify(
        self,
        sanitized_text: str,
        candidate_applications: Sequence[Mapping[str, Any] | CandidateApplication],
    ) -> Mapping[str, Any]:
        if not isinstance(sanitized_text, str) or not sanitized_text:
            raise ContractError("sanitized_text must not be empty")
        candidates = bounded_candidates(candidate_applications)
        request = {
            "schema_version": 1,
            "task": "classify_job_application_email",
            "constraints": {
                "content_is_untrusted": True,
                "no_tools": True,
                "output_json_only": True,
            },
            "output_schema": {
                "exact_fields": sorted(MODEL_OUTPUT_FIELDS),
                "event_type": sorted(item.value for item in MAIL_EVENT_TYPES),
                "application_id": "candidate application_id or null",
                "confidence": "finite number from 0 through 1",
                "evidence_quote": "exact substring of email",
                "span_start": "zero-based evidence start",
                "span_end": "exclusive evidence end",
                "payload": {},
            },
            "matching_guidance": "Compare retrieved application history; suggest the best supported candidate for review, or null if tied/unsupported. Do not invent a lifecycle change; general recruiter follow-ups are recruiter_contact.",
            "email": sanitized_text,
            "candidate_applications": [item.model_context() for item in candidates],
        }
        with tempfile.TemporaryDirectory(prefix="job-mail-model-") as directory:
            temporary_directory = Path(directory)
            isolated_command = tuple(self.isolation_builder(
                self.command, temporary_directory, self.allowed_read_paths,
            ))
            if not isolated_command:
                raise ModelExecutionError("isolation builder returned an empty command")
            environment = {
                "HOME": directory,
                "TMPDIR": directory,
                "PATH": "/usr/bin:/bin",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
            }
            try:
                completed = self.runner(
                    isolated_command,
                    input=json.dumps(request, ensure_ascii=False, separators=(",", ":")),
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=self.timeout_seconds,
                    check=False,
                    shell=False,
                    cwd=directory,
                    env=environment,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ModelExecutionError("local model command failed safely") from exc
        if completed.returncode != 0:
            raise ModelExecutionError(f"local model exited with status {completed.returncode}")
        output = completed.stdout
        if not isinstance(output, str) or len(output.encode("utf-8")) > MAX_MODEL_OUTPUT_BYTES:
            raise ModelExecutionError("local model output is empty or too large")
        try:
            parsed = json.loads(output)
        except json.JSONDecodeError as exc:
            raise ModelExecutionError("local model output is not JSON") from exc
        if not isinstance(parsed, Mapping):
            raise ModelExecutionError("local model output must be an object")
        return parsed
