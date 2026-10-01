"""Strict Graph attachment acquisition and isolated text extraction."""

from __future__ import annotations

import hashlib
import importlib.util
import os
import resource
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence

from job_search.contracts import ContractError

from .sanitizer import sanitize_mail


PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
ICS_MIME = "text/calendar"
ALLOWED_MIME_EXTENSIONS = {PDF_MIME: ".pdf", DOCX_MIME: ".docx", ICS_MIME: ".ics"}
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024
MAX_ATTACHMENT_COUNT = 20
MAX_EXTRACTED_CHARS = 256_000


class AttachmentRejected(ContractError):
    pass


@dataclass(frozen=True)
class AttachmentDescriptor:
    attachment_id: str
    name: str
    mime_type: str
    size: int


@dataclass(frozen=True)
class ExtractedAttachment:
    attachment_id: str
    mime_type: str
    source_size: int
    source_sha256: str
    sanitized_text: str
    truncated: bool


def validate_attachment_descriptor(value: Mapping[str, Any]) -> AttachmentDescriptor:
    if not isinstance(value, Mapping):
        raise AttachmentRejected("attachment descriptor must be an object")
    if value.get("@odata.type") != "#microsoft.graph.fileAttachment":
        raise AttachmentRejected("only Graph file attachments are supported")
    attachment_id = value.get("id")
    name = value.get("name")
    mime = value.get("contentType")
    size = value.get("size")
    if not isinstance(attachment_id, str) or not attachment_id or len(attachment_id) > 2048:
        raise AttachmentRejected("attachment id is invalid")
    if not isinstance(name, str) or not name or len(name) > 512:
        raise AttachmentRejected("attachment name is invalid")
    if PurePosixPath(name.replace("\\", "/")).name != name.replace("\\", "/"):
        raise AttachmentRejected("attachment name must not contain a path")
    if mime not in ALLOWED_MIME_EXTENSIONS:
        raise AttachmentRejected("attachment MIME is not allowed")
    if Path(name).suffix.casefold() != ALLOWED_MIME_EXTENSIONS[str(mime)]:
        raise AttachmentRejected("attachment extension does not match MIME")
    if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= MAX_ATTACHMENT_BYTES:
        raise AttachmentRejected("attachment size exceeds the safe limit")
    if value.get("isInline") is not False:
        raise AttachmentRejected("inline attachments are not acquired")
    return AttachmentDescriptor(attachment_id, name, str(mime), size)


def validate_attachment_bytes(descriptor: AttachmentDescriptor, value: bytes) -> None:
    if not isinstance(value, bytes) or not value or len(value) > MAX_ATTACHMENT_BYTES:
        raise AttachmentRejected("attachment body exceeds the safe limit")
    if len(value) != descriptor.size:
        raise AttachmentRejected("attachment body size differs from Graph metadata")
    if descriptor.mime_type == PDF_MIME:
        if not value.startswith(b"%PDF-") or b"%%EOF" not in value[-2048:]:
            raise AttachmentRejected("PDF signature is invalid")
    elif descriptor.mime_type == DOCX_MIME:
        if not value.startswith(b"PK\x03\x04"):
            raise AttachmentRejected("DOCX ZIP signature is invalid")
        try:
            with zipfile.ZipFile(__import__("io").BytesIO(value)) as archive:
                names = set(archive.namelist())
                if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                    raise AttachmentRejected("DOCX package is incomplete")
        except (zipfile.BadZipFile, OSError) as exc:
            raise AttachmentRejected("DOCX ZIP is invalid") from exc
    elif descriptor.mime_type == ICS_MIME:
        try:
            text = value.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise AttachmentRejected("ICS must be UTF-8") from exc
        if "BEGIN:VCALENDAR" not in text[:256].upper() or "END:VCALENDAR" not in text[-512:].upper():
            raise AttachmentRejected("ICS signature is invalid")


def _sandbox_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def macos_attachment_sandbox(command: Sequence[str], directory: Path) -> tuple[str, ...]:
    sandbox = Path("/usr/bin/sandbox-exec")
    if sys.platform != "darwin" or not sandbox.exists():
        raise AttachmentRejected("attachment extraction sandbox is unavailable")
    executable = shutil.which(command[0]) if not Path(command[0]).is_absolute() else command[0]
    if not executable:
        raise AttachmentRejected("attachment extraction interpreter is unavailable")
    worker = Path(__file__).with_name("attachment_worker.py").resolve()
    clauses = [
        "(version 1)",
        "(deny default)",
        "(deny network*)",
        "(allow process-info*)",
        f'(allow process-exec (literal "{_sandbox_literal(str(executable))}"))',
        f'(allow file-read* file-write* (subpath "{_sandbox_literal(str(directory))}"))',
        f'(allow file-read* (literal "{_sandbox_literal(str(worker))}"))',
        '(allow file-read* (subpath "/System") (subpath "/usr") (subpath "/Library"))',
        '(allow file-read* (literal "/dev/null") (literal "/dev/urandom"))',
    ]
    # A user-site install is commonly outside /Library. Permit only the package
    # directory containing the explicitly selected PDF parser and its peers.
    pdf_spec = importlib.util.find_spec("pypdf")
    if pdf_spec and pdf_spec.submodule_search_locations:
        for location in pdf_spec.submodule_search_locations:
            site_packages = Path(location).resolve().parent
            clauses.append(
                f'(allow file-read* (subpath "{_sandbox_literal(str(site_packages))}"))'
            )
    return (str(sandbox), "-p", "\n".join(clauses), "--", *command)


def _resource_limits() -> None:
    for limit_name, requested in (
        (resource.RLIMIT_CPU, 8),
        (resource.RLIMIT_FSIZE, 2 * 1024 * 1024),
        (resource.RLIMIT_NOFILE, 32),
    ):
        _soft, hard = resource.getrlimit(limit_name)
        bounded = requested if hard == resource.RLIM_INFINITY else min(requested, hard)
        resource.setrlimit(limit_name, (bounded, hard))


class SandboxedAttachmentExtractor:
    def __init__(
        self,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        isolation_builder: Callable[[Sequence[str], Path], Sequence[str]] = macos_attachment_sandbox,
        timeout_seconds: float = 12,
    ) -> None:
        if timeout_seconds <= 0 or timeout_seconds > 60:
            raise ValueError("attachment extraction timeout must be within 60 seconds")
        self._runner = runner
        self._isolation_builder = isolation_builder
        self._timeout = timeout_seconds

    def extract(self, descriptor: AttachmentDescriptor, content: bytes) -> ExtractedAttachment:
        validate_attachment_bytes(descriptor, content)
        with tempfile.TemporaryDirectory(prefix="job-mail-attachment-") as name:
            directory = Path(name)
            source = directory / "source.bin"
            output = directory / "output.txt"
            descriptor_fd = os.open(source, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor_fd, "wb") as handle:
                handle.write(content)
            worker = Path(__file__).with_name("attachment_worker.py").resolve()
            command = (sys.executable, "-I", str(worker), descriptor.mime_type, str(source), str(output))
            isolated = tuple(self._isolation_builder(command, directory))
            if not isolated:
                raise AttachmentRejected("attachment sandbox returned an empty command")
            try:
                completed = self._runner(
                    isolated,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=self._timeout,
                    check=False,
                    shell=False,
                    cwd=str(directory),
                    env={
                        "HOME": str(directory),
                        "TMPDIR": str(directory),
                        "PATH": "/usr/bin:/bin",
                        "LANG": "C.UTF-8",
                        "LC_ALL": "C.UTF-8",
                    },
                    preexec_fn=_resource_limits,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise AttachmentRejected("attachment extraction failed safely") from exc
            if completed.returncode != 0 or not output.is_file():
                raise AttachmentRejected("attachment extractor rejected the file")
            if output.stat().st_size > 2 * 1024 * 1024:
                raise AttachmentRejected("attachment extractor output is too large")
            try:
                extracted = output.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise AttachmentRejected("attachment extractor output is not UTF-8") from exc
        if not extracted.strip():
            raise AttachmentRejected("attachment has no extractable text")
        sanitized = sanitize_mail(
            "Attachment", extracted, body_kind="text", max_chars=MAX_EXTRACTED_CHARS
        )
        return ExtractedAttachment(
            descriptor.attachment_id,
            descriptor.mime_type,
            len(content),
            hashlib.sha256(content).hexdigest(),
            sanitized.text,
            sanitized.truncated,
        )


class SecureAttachmentPipeline:
    """Fetch only pre-screened Graph file attachments and extract them locally."""

    def __init__(self, mail: Any, extractor: SandboxedAttachmentExtractor) -> None:
        self._mail = mail
        self._extractor = extractor

    def acquire(self, immutable_message_id: str) -> tuple[ExtractedAttachment, ...]:
        raw = self._mail.list_attachments(immutable_message_id, limit=MAX_ATTACHMENT_COUNT)
        results = []
        for item in raw:
            try:
                descriptor = validate_attachment_descriptor(item)
            except AttachmentRejected:
                continue
            payload = self._mail.read_file_attachment(
                immutable_message_id, descriptor.attachment_id
            )
            returned = validate_attachment_descriptor(payload)
            if returned != descriptor:
                raise AttachmentRejected("downloaded attachment metadata changed")
            content = payload.get("contentBytes")
            if not isinstance(content, bytes):
                raise AttachmentRejected("Graph attachment body was not decoded")
            results.append(self._extractor.extract(descriptor, content))
        return tuple(results)
