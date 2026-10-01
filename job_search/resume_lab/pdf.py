"""Bounded, isolated pypdf adapter for generated resume artifacts."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import resource
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from job_search.contracts import ContractError

from .process import ProcessOutputLimitError, run_bounded_process


MAX_PDF_BYTES = 5 * 1024 * 1024
MAX_RESULT_BYTES = 1024 * 1024
MAX_EXTRACTOR_OUTPUT_BYTES = 64 * 1024


class PdfExtractionError(RuntimeError):
    """The PDF could not be safely parsed into visible text."""


@dataclass(frozen=True)
class PdfExtraction:
    pdf_sha256: str
    parser: str
    parser_version: str
    pages: int
    content_stream_bytes: int
    logical_text: str
    layout_text: str


def _sandbox_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _executable_targets(invoked: Path, resolved: Path) -> set[Path]:
    """Include the helper binary used by Apple's framework Python launcher."""

    targets = {invoked, resolved}
    # uv's virtualenv symlink can pass through a second, directory symlink.
    # Keep its lexical target so sandbox traversal can stat those ancestors.
    if invoked.is_symlink():
        target = Path(os.readlink(invoked))
        targets.add(target if target.is_absolute() else invoked.parent / target)
    for parent in resolved.parents:
        helper = parent / "Resources" / "Python.app" / "Contents" / "MacOS" / "Python"
        if helper.is_file():
            targets.add(helper.resolve())
            break
    return targets


def macos_pdf_sandbox(command: Sequence[str], directory: Path) -> tuple[str, ...]:
    sandbox = Path("/usr/bin/sandbox-exec")
    if sys.platform != "darwin" or not sandbox.exists():
        raise PdfExtractionError("PDF extraction sandbox is unavailable on this host")
    invoked_executable = Path(command[0]).expanduser().absolute()
    try:
        resolved_executable = invoked_executable.resolve(strict=True)
    except OSError as exc:
        raise PdfExtractionError("PDF extraction executable was not found") from exc
    logical_directory = Path(directory).expanduser().absolute()
    directory = logical_directory.resolve()
    write_directories = {logical_directory, directory}
    worker = Path(__file__).with_name("pdf_worker.py").resolve()
    executable_targets = _executable_targets(
        invoked_executable, resolved_executable
    )
    read_paths = {
        Path("/System"),
        Path("/usr/bin"),
        Path("/usr/lib"),
        Path("/usr/share"),
        Path(sys.prefix).resolve(),
        Path(sys.base_prefix).resolve(),
        *executable_targets,
        worker,
    }
    spec = importlib.util.find_spec("pypdf")
    if spec and spec.submodule_search_locations:
        read_paths.update(
            Path(value).resolve().parent for value in spec.submodule_search_locations
        )
    executable_literals = " ".join(
        f'(literal "{_sandbox_literal(str(path))}")'
        for path in sorted(executable_targets, key=str)
    )
    clauses = [
        "(version 1)",
        "(deny default)",
        "(deny network*)",
        "(allow process-info*)",
        '(allow file-read* (literal "/"))',
        f"(allow process-exec {executable_literals})",
        '(allow file-read* (literal "/dev/null") (literal "/dev/urandom"))',
    ]
    for writable in sorted(write_directories, key=str):
        clauses.append(
            f'(allow file-read* file-write* (subpath "{_sandbox_literal(str(writable))}"))'
        )
    resolved_paths = {path.resolve() for path in read_paths} | executable_targets
    ancestors = {
        parent
        for path in (*resolved_paths, *write_directories)
        for parent in path.parents
        if parent != Path("/")
    }
    for ancestor in sorted(ancestors, key=str):
        clauses.append(
            f'(allow file-read* (literal "{_sandbox_literal(str(ancestor))}"))'
        )
    for path in sorted(resolved_paths, key=str):
        kind = "subpath" if path.is_dir() else "literal"
        clauses.append(f'(allow file-read* ({kind} "{_sandbox_literal(str(path))}"))')
    return (
        str(sandbox),
        "-p",
        "\n".join(clauses),
        "--",
        str(invoked_executable),
        *command[1:],
    )


def _extractor_limits() -> None:
    for name, requested in (
        (resource.RLIMIT_CPU, 12),
        (resource.RLIMIT_FSIZE, MAX_RESULT_BYTES + MAX_PDF_BYTES),
        (resource.RLIMIT_NOFILE, 32),
    ):
        _soft, hard = resource.getrlimit(name)
        value = requested if hard == resource.RLIM_INFINITY else min(requested, hard)
        resource.setrlimit(name, (value, hard))


def _validate_result(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PdfExtractionError("PDF extractor output must be an object")
    expected = {
        "schema_version", "parser", "parser_version", "pages",
        "content_stream_bytes", "logical_text", "layout_text",
    }
    if set(value) != expected or value.get("schema_version") != 1 or value.get("parser") != "pypdf":
        raise PdfExtractionError("PDF extractor output has an invalid schema")
    if not isinstance(value.get("parser_version"), str) or not value["parser_version"]:
        raise PdfExtractionError("PDF extractor version is invalid")
    if isinstance(value.get("pages"), bool) or not isinstance(value.get("pages"), int) or not 1 <= value["pages"] <= 5:
        raise PdfExtractionError("PDF extractor page count is invalid")
    streams = value.get("content_stream_bytes")
    if isinstance(streams, bool) or not isinstance(streams, int) or not 0 <= streams <= 8 * 1024 * 1024:
        raise PdfExtractionError("PDF content stream measurement is invalid")
    for key in ("logical_text", "layout_text"):
        text = value.get(key)
        if not isinstance(text, str) or not text.strip() or len(text) > 256_000:
            raise PdfExtractionError(f"PDF extractor {key} is invalid")
    return value


class PypdfExtractor:
    """Extract logical and layout views without importing pypdf in the service."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 20.0,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        isolation_builder: Callable[[Sequence[str], Path], Sequence[str]] = macos_pdf_sandbox,
    ) -> None:
        if not 1 <= float(timeout_seconds) <= 60:
            raise ContractError("PDF extraction timeout must be between 1 and 60 seconds")
        self.timeout_seconds = float(timeout_seconds)
        self.runner = runner
        self.isolation_builder = isolation_builder

    def extract(self, pdf: bytes) -> PdfExtraction:
        if not isinstance(pdf, bytes) or not pdf or len(pdf) > MAX_PDF_BYTES:
            raise ContractError("resume PDF must be bounded bytes")
        if not pdf.startswith(b"%PDF-") or b"%%EOF" not in pdf[-4096:]:
            raise ContractError("resume PDF signature is invalid")
        digest = hashlib.sha256(pdf).hexdigest()
        with tempfile.TemporaryDirectory(prefix="job-resume-pdf-") as name:
            # macOS exposes /var as a symlink to /private/var. Resolve once so
            # command arguments and the sandbox write grant name the same path.
            directory = Path(name).resolve()
            source = directory / "resume.pdf"
            destination = directory / "extracted.json"
            descriptor = os.open(source, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(pdf)
            worker = Path(__file__).with_name("pdf_worker.py").resolve()
            command = (sys.executable, "-I", str(worker), str(source), str(destination))
            isolated = tuple(self.isolation_builder(command, directory))
            if not isolated:
                raise PdfExtractionError("PDF extraction isolation returned an empty command")
            try:
                environment = {
                    "HOME": str(directory),
                    "TMPDIR": str(directory),
                    "PATH": "/usr/bin:/bin",
                    "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8",
                }
                if self.runner is None:
                    completed = run_bounded_process(
                        isolated,
                        timeout=self.timeout_seconds,
                        cwd=str(directory),
                        env=environment,
                        preexec_fn=_extractor_limits,
                        max_output_bytes=MAX_EXTRACTOR_OUTPUT_BYTES,
                    )
                else:
                    completed = self.runner(
                        isolated,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        timeout=self.timeout_seconds,
                        check=False,
                        shell=False,
                        cwd=str(directory),
                        env=environment,
                        preexec_fn=_extractor_limits,
                    )
            except (
                OSError,
                ProcessOutputLimitError,
                subprocess.TimeoutExpired,
            ) as exc:
                raise PdfExtractionError("PDF extraction failed safely") from exc
            if completed.returncode != 0:
                raise PdfExtractionError(f"PDF extractor exited with status {completed.returncode}")
            try:
                info = destination.lstat()
                raw = destination.read_bytes()
            except OSError as exc:
                raise PdfExtractionError("PDF extractor produced no result") from exc
            if not stat.S_ISREG(info.st_mode) or destination.is_symlink() or len(raw) > MAX_RESULT_BYTES:
                raise PdfExtractionError("PDF extractor result file is invalid")
            try:
                value = _validate_result(json.loads(raw.decode("utf-8")))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise PdfExtractionError("PDF extractor result is not UTF-8 JSON") from exc
        return PdfExtraction(
            digest,
            str(value["parser"]),
            str(value["parser_version"]),
            int(value["pages"]),
            int(value["content_stream_bytes"]),
            str(value["logical_text"]),
            str(value["layout_text"]),
        )
