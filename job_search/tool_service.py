"""Networkless Unix-socket boundary for cloud resume and attachment tools.

The macOS runtime isolates Tectonic and document parsers with ``sandbox-exec``.
Linux containers use a stronger deployment boundary instead: this small service runs
with no network namespace, a read-only root filesystem, no capabilities, and only a
temporary directory plus the pinned Tectonic toolchain.  Application containers send
bounded requests over an owner-only Unix socket and still validate every result.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import signal
import socket
import socketserver
import stat
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Any

from .contracts import ContractError

PROTOCOL_VERSION = 1
SERVICE_REVISION = "cloud-document-tools-v1"
MAX_FRAME_BYTES = 8 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 75.0
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class ToolServiceError(RuntimeError):
    """The isolated document-tool service was unavailable or rejected a request."""


def _canonical(value: Any) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
        raise ToolServiceError("document-tool message is not finite JSON") from exc
    if not encoded or len(encoded) > MAX_FRAME_BYTES:
        raise ToolServiceError("document-tool message exceeds its size limit")
    return encoded + b"\n"


def _parse_frame(raw: bytes) -> Mapping[str, Any]:
    if not raw or len(raw) > MAX_FRAME_BYTES + 1 or not raw.endswith(b"\n"):
        raise ToolServiceError("document-tool message is incomplete or too large")
    try:
        value = json.loads(raw[:-1].decode("utf-8"), parse_constant=lambda _v: (_ for _ in ()).throw(ValueError()))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise ToolServiceError("document-tool message is invalid JSON") from exc
    if not isinstance(value, Mapping):
        raise ToolServiceError("document-tool message must be an object")
    return value


def _safe_socket(path: Path, *, require_exists: bool) -> Path:
    target = Path(path).expanduser()
    if not target.is_absolute():
        raise ToolServiceError("document-tool socket path must be absolute")
    try:
        parent = target.parent.resolve(strict=True)
        parent_info = parent.stat()
    except OSError as exc:
        raise ToolServiceError("document-tool socket directory is unavailable") from exc
    current_uid = getattr(os, "geteuid", lambda: parent_info.st_uid)()
    if (
        not stat.S_ISDIR(parent_info.st_mode)
        or parent_info.st_uid != current_uid
        or parent_info.st_mode & 0o077
    ):
        raise ToolServiceError("document-tool socket directory must be owner-only")
    normalized = parent / target.name
    try:
        info = os.lstat(normalized)
    except FileNotFoundError:
        if require_exists:
            raise ToolServiceError("document-tool socket is unavailable") from None
        return normalized
    if (
        not stat.S_ISSOCK(info.st_mode)
        or info.st_uid != current_uid
        or info.st_mode & 0o077
        or info.st_nlink != 1
    ):
        raise ToolServiceError("document-tool socket is unsafe")
    return normalized


class ToolServiceClient:
    """Bounded one-request-per-connection client for the private Unix socket."""

    def __init__(self, socket_path: Path, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        if not 1 <= float(timeout_seconds) <= 180:
            raise ContractError("document-tool timeout must be between 1 and 180 seconds")
        self.socket_path = Path(socket_path).expanduser()
        self.timeout_seconds = float(timeout_seconds)

    def call(self, operation: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if operation not in {"ping", "compile_tex", "extract_pdf", "extract_attachment"}:
            raise ToolServiceError("document-tool operation is invalid")
        target = _safe_socket(self.socket_path, require_exists=True)
        request = _canonical(
            {
                "version": PROTOCOL_VERSION,
                "operation": operation,
                "payload": dict(payload),
            }
        )
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout_seconds)
        try:
            connection.connect(str(target))
            connection.sendall(request)
            # The newline is the request-frame terminator.  Do not half-close the
            # socket here: a fast one-request server can send its response and close
            # before ``shutdown(SHUT_WR)`` runs, which macOS reports as ENOTCONN even
            # though the complete response is already waiting to be read.
            chunks: list[bytes] = []
            remaining = MAX_FRAME_BYTES + 2
            while remaining:
                chunk = connection.recv(min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
                if b"\n" in chunk:
                    break
        except (OSError, TimeoutError) as exc:
            raise ToolServiceError("document-tool service is unavailable") from exc
        finally:
            connection.close()
        raw = b"".join(chunks)
        response = _parse_frame(raw)
        if set(response) != {"version", "ok", "result", "error"}:
            raise ToolServiceError("document-tool response has an invalid shape")
        if response.get("version") != PROTOCOL_VERSION or not isinstance(response.get("ok"), bool):
            raise ToolServiceError("document-tool response has an invalid version")
        if not response["ok"]:
            raise ToolServiceError("document-tool service rejected the request")
        if response.get("error") is not None or not isinstance(response.get("result"), Mapping):
            raise ToolServiceError("document-tool response has an invalid result")
        return response["result"]


def _decode_bounded_binary(value: Any) -> bytes:
    if not isinstance(value, str):
        raise ToolServiceError("document-tool PDF result is invalid")
    try:
        result = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise ToolServiceError("document-tool PDF result is invalid") from exc
    if not result or len(result) > 5 * 1024 * 1024:
        raise ToolServiceError("document-tool PDF result is invalid")
    return result


class RemoteTectonicCompiler:
    """Drop-in compiler whose untrusted process lives in the networkless service."""

    def __init__(self, socket_path: Path, expected_engine_version: str) -> None:
        if not isinstance(expected_engine_version, str) or not expected_engine_version.strip():
            raise ContractError("remote Tectonic engine version is required")
        self.client = ToolServiceClient(socket_path)
        self.engine_version = expected_engine_version.strip()

    def compile(self, source: str) -> Any:
        from .resume_lab.tex import (
            MAX_PDF_BYTES,
            CompiledPdf,
            TexToolchainError,
            preflight_tex,
        )

        report = preflight_tex(source)
        if not report.safe:
            raise TexToolchainError("TeX preflight failed: " + "; ".join(report.issues))
        try:
            value = self.client.call("compile_tex", {"tex_source": source})
            expected = {
                "pdf_base64", "pdf_sha256", "source_sha256", "engine",
                "engine_version", "bundle_sha256", "log_excerpt",
            }
            if set(value) != expected:
                raise ToolServiceError("remote Tectonic result has an invalid shape")
            pdf = _decode_bounded_binary(value["pdf_base64"])
            if len(pdf) > MAX_PDF_BYTES or not pdf.startswith(b"%PDF-") or b"%%EOF" not in pdf[-4096:]:
                raise ToolServiceError("remote Tectonic PDF is incomplete")
            pdf_sha = hashlib.sha256(pdf).hexdigest()
            if (
                value["pdf_sha256"] != pdf_sha
                or value["source_sha256"] != report.source_sha256
                or value["engine"] != "tectonic"
                or value["engine_version"] != self.engine_version
                or not isinstance(value["bundle_sha256"], str)
                or not _SHA256.fullmatch(value["bundle_sha256"])
                or not isinstance(value["log_excerpt"], str)
                or len(value["log_excerpt"].encode("utf-8")) > 32 * 1024
            ):
                raise ToolServiceError("remote Tectonic result failed validation")
            return CompiledPdf(
                pdf,
                pdf_sha,
                report.source_sha256,
                "tectonic",
                self.engine_version,
                value["bundle_sha256"],
                value["log_excerpt"],
            )
        except ToolServiceError as exc:
            raise TexToolchainError("remote Tectonic compilation failed safely") from exc


class RemotePypdfExtractor:
    """Drop-in PDF extractor backed by the networkless service."""

    def __init__(self, socket_path: Path) -> None:
        self.client = ToolServiceClient(socket_path)

    def extract(self, pdf: bytes) -> Any:
        from .resume_lab.pdf import (
            MAX_PDF_BYTES,
            PdfExtraction,
            PdfExtractionError,
            _validate_result,
        )

        if not isinstance(pdf, bytes) or not pdf or len(pdf) > MAX_PDF_BYTES:
            raise ContractError("resume PDF must be bounded bytes")
        digest = hashlib.sha256(pdf).hexdigest()
        try:
            value = self.client.call(
                "extract_pdf", {"pdf_base64": base64.b64encode(pdf).decode("ascii")}
            )
            expected = {
                "pdf_sha256", "parser", "parser_version", "pages",
                "content_stream_bytes", "logical_text", "layout_text",
            }
            if set(value) != expected or value["pdf_sha256"] != digest:
                raise ToolServiceError("remote PDF result has an invalid shape")
            checked = _validate_result({"schema_version": 1, **{key: value[key] for key in expected - {"pdf_sha256"}}})
            return PdfExtraction(
                digest,
                str(checked["parser"]),
                str(checked["parser_version"]),
                int(checked["pages"]),
                int(checked["content_stream_bytes"]),
                str(checked["logical_text"]),
                str(checked["layout_text"]),
            )
        except ToolServiceError as exc:
            raise PdfExtractionError("remote PDF extraction failed safely") from exc


class RemoteAttachmentExtractor:
    """Drop-in Outlook attachment extractor backed by the networkless service."""

    def __init__(self, socket_path: Path) -> None:
        self.client = ToolServiceClient(socket_path)

    def extract(self, descriptor: Any, content: bytes) -> Any:
        from .mail.attachments import (
            MAX_EXTRACTED_CHARS,
            AttachmentDescriptor,
            AttachmentRejected,
            ExtractedAttachment,
            validate_attachment_bytes,
        )

        if not isinstance(descriptor, AttachmentDescriptor):
            raise AttachmentRejected("attachment descriptor is invalid")
        validate_attachment_bytes(descriptor, content)
        try:
            value = self.client.call(
                "extract_attachment",
                {
                    "descriptor": asdict(descriptor),
                    "content_base64": base64.b64encode(content).decode("ascii"),
                },
            )
            expected = {
                "attachment_id", "mime_type", "source_size", "source_sha256",
                "sanitized_text", "truncated",
            }
            if set(value) != expected:
                raise ToolServiceError("remote attachment result has an invalid shape")
            digest = hashlib.sha256(content).hexdigest()
            if (
                value["attachment_id"] != descriptor.attachment_id
                or value["mime_type"] != descriptor.mime_type
                or value["source_size"] != len(content)
                or value["source_sha256"] != digest
                or not isinstance(value["sanitized_text"], str)
                or not value["sanitized_text"].strip()
                or len(value["sanitized_text"]) > MAX_EXTRACTED_CHARS
                or not isinstance(value["truncated"], bool)
            ):
                raise ToolServiceError("remote attachment result failed validation")
            return ExtractedAttachment(
                descriptor.attachment_id,
                descriptor.mime_type,
                len(content),
                digest,
                value["sanitized_text"],
                value["truncated"],
            )
        except ToolServiceError as exc:
            raise AttachmentRejected("remote attachment extraction failed safely") from exc


def _identity_command(command: Sequence[str], *_unused: Any) -> tuple[str, ...]:
    """Rely on the dedicated container boundary; never use in an app container."""

    return tuple(command)


def _attest_network_namespace(
    interfaces_path: Path = Path("/sys/class/net"),
    status_path: Path = Path("/proc/self/status"),
    route_path: Path = Path("/proc/net/route"),
    ipv6_route_path: Path = Path("/proc/net/ipv6_route"),
) -> None:
    """Accept inert kernel netdevs while rejecting network access or privilege.

    A Docker `network_mode: none` namespace can contain DOWN tunnel devices and
    non-interface sysfs entries. Names alone do not prove connectivity. IFF_UP,
    routing tables and inability to gain namespace/network administration are
    checked together before untrusted document tools are allowed to run.
    """
    try:
        found_loopback = False
        for interface in interfaces_path.iterdir():
            if not interface.is_dir():
                continue
            flags = int((interface / "flags").read_text().strip(), 16)
            if interface.name == "lo":
                if not flags & 0x8:  # IFF_LOOPBACK
                    raise ValueError("invalid loopback")
                found_loopback = True
            elif flags & 0x1:  # IFF_UP
                raise ValueError("active external interface")
        if not found_loopback:
            raise ValueError("missing loopback")
        status = dict(line.split(":", 1) for line in status_path.read_text().splitlines() if ":" in line)
        privileged = (1 << 12) | (1 << 21)  # CAP_NET_ADMIN | CAP_SYS_ADMIN
        if any(int(status[key].strip(), 16) & privileged for key in ("CapEff", "CapPrm", "CapBnd", "CapAmb")):
            raise ValueError("network administration remains available")
        if status["NoNewPrivs"].strip() != "1":
            raise ValueError("privilege escalation is not disabled")
        for line in route_path.read_text().splitlines()[1:]:
            fields = line.split()
            if len(fields) < 4 or fields[0] != "lo":
                raise ValueError("external IPv4 route")
        for line in ipv6_route_path.read_text().splitlines():
            fields = line.split()
            if len(fields) != 10 or fields[-1] != "lo":
                raise ValueError("external IPv6 route")
    except (OSError, ValueError, KeyError) as exc:
        raise ToolServiceError("cannot attest a networkless, unprivileged tool container") from exc


class ToolDispatcher:
    def __init__(self, executable: Path, bundle: Path, engine_version: str) -> None:
        if sys.platform != "linux":
            raise ToolServiceError("container document tools require Linux")
        if os.environ.get("JOB_SEARCH_NETWORKLESS_TOOL_CONTAINER") != "1":
            raise ToolServiceError("networkless tool-container attestation is missing")
        _attest_network_namespace()
        from .mail.attachments import SandboxedAttachmentExtractor
        from .resume_lab.pdf import PypdfExtractor
        from .resume_lab.tex import TectonicCompiler

        self.compiler = TectonicCompiler(
            executable, bundle, engine_version, isolation_builder=_identity_command
        )
        self.pdf = PypdfExtractor(isolation_builder=_identity_command)
        self.attachment = SandboxedAttachmentExtractor(isolation_builder=_identity_command)

    def _ping(self) -> Mapping[str, Any]:
        try:
            pypdf_version = package_version("pypdf")
        except PackageNotFoundError:
            pypdf_version = "unavailable"
        return {
            "service_revision": SERVICE_REVISION,
            "engine": "tectonic",
            "engine_version": self.compiler.engine_version,
            "bundle_sha256": self.compiler.bundle_sha256,
            "pypdf_version": pypdf_version,
        }

    def dispatch(self, operation: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if operation == "ping":
            if payload:
                raise ContractError("ping payload must be empty")
            return self._ping()
        if operation == "compile_tex":
            if set(payload) != {"tex_source"} or not isinstance(payload["tex_source"], str):
                raise ContractError("compile_tex payload is invalid")
            compiled = self.compiler.compile(payload["tex_source"])
            return {
                "pdf_base64": base64.b64encode(compiled.pdf_bytes).decode("ascii"),
                "pdf_sha256": compiled.pdf_sha256,
                "source_sha256": compiled.source_sha256,
                "engine": compiled.engine,
                "engine_version": compiled.engine_version,
                "bundle_sha256": compiled.bundle_sha256,
                "log_excerpt": compiled.log_excerpt,
            }
        if operation == "extract_pdf":
            if set(payload) != {"pdf_base64"}:
                raise ContractError("extract_pdf payload is invalid")
            pdf = _decode_bounded_binary(payload["pdf_base64"])
            extracted = self.pdf.extract(pdf)
            return asdict(extracted)
        if operation == "extract_attachment":
            if set(payload) != {"descriptor", "content_base64"} or not isinstance(payload["descriptor"], Mapping):
                raise ContractError("extract_attachment payload is invalid")
            from .mail.attachments import validate_attachment_descriptor

            raw = payload["descriptor"]
            if set(raw) != {"attachment_id", "name", "mime_type", "size"}:
                raise ContractError("attachment descriptor is invalid")
            descriptor = validate_attachment_descriptor(
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "id": raw["attachment_id"],
                    "name": raw["name"],
                    "contentType": raw["mime_type"],
                    "size": raw["size"],
                    "isInline": False,
                }
            )
            content = _decode_bounded_binary(payload["content_base64"])
            # The shared decoder applies the same five-MiB binary bound; the
            # attachment extractor performs MIME-specific validation immediately.
            return asdict(self.attachment.extract(descriptor, content))
        raise ContractError("document-tool operation is unsupported")


class _ToolHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        self.connection.settimeout(10)
        try:
            request = _parse_frame(self.rfile.readline(MAX_FRAME_BYTES + 2))
            if set(request) != {"version", "operation", "payload"}:
                raise ToolServiceError("document-tool request has an invalid shape")
            if request.get("version") != PROTOCOL_VERSION:
                raise ToolServiceError("document-tool protocol version is unsupported")
            operation = request.get("operation")
            payload = request.get("payload")
            if not isinstance(operation, str) or not isinstance(payload, Mapping):
                raise ToolServiceError("document-tool request fields are invalid")
            result = self.server.dispatcher.dispatch(operation, payload)  # type: ignore[attr-defined]
            response = {"version": PROTOCOL_VERSION, "ok": True, "result": result, "error": None}
        # This is the outer trust boundary: parser/compiler/library failures must
        # become one pathless response and must never terminate the service.
        except Exception:  # noqa: BLE001
            response = {
                "version": PROTOCOL_VERSION,
                "ok": False,
                "result": None,
                "error": "request_rejected",
            }
        try:
            self.wfile.write(_canonical(response))
        except (OSError, ToolServiceError):
            return


class _ToolServer(socketserver.UnixStreamServer):
    allow_reuse_address = False

    def __init__(self, path: Path, dispatcher: ToolDispatcher) -> None:
        self.dispatcher = dispatcher
        super().__init__(str(path), _ToolHandler)


def serve(socket_path: Path, dispatcher: ToolDispatcher) -> int:
    target = _safe_socket(socket_path, require_exists=False)
    if target.exists():
        # A clean shutdown removes the socket. Refuse to steal an active endpoint,
        # but safely clear an owner-only stale socket left by a killed container.
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.2)
        try:
            probe.connect(str(target))
        except OSError:
            target.unlink()
        else:
            raise ToolServiceError("document-tool socket is already active")
        finally:
            probe.close()
    server = _ToolServer(target, dispatcher)
    os.chmod(target, 0o600)
    stopping = False

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True

    previous = {}
    for name in ("SIGTERM", "SIGINT"):
        signum = getattr(signal, name, None)
        if signum is not None:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, request_stop)
    server.timeout = 0.5
    try:
        while not stopping:
            server.handle_request()
    finally:
        server.server_close()
        try:
            if _safe_socket(target, require_exists=True) == target:
                target.unlink()
        except ToolServiceError:
            pass
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Networkless cloud document-tool service")
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("serve")
    start.add_argument("--socket", type=Path, required=True)
    start.add_argument("--tectonic-executable", type=Path, required=True)
    start.add_argument("--tectonic-bundle", type=Path, required=True)
    start.add_argument("--tectonic-version", required=True)
    health = commands.add_parser("healthcheck")
    health.add_argument("--socket", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "healthcheck":
            result = ToolServiceClient(args.socket, timeout_seconds=3).call("ping", {})
            return 0 if result.get("service_revision") == SERVICE_REVISION else 1
        dispatcher = ToolDispatcher(
            args.tectonic_executable, args.tectonic_bundle, args.tectonic_version
        )
        return serve(args.socket, dispatcher)
    except (ContractError, OSError, RuntimeError, ValueError) as exc:
        print(f"job-search document tools: {type(exc).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "SERVICE_REVISION",
    "RemoteAttachmentExtractor",
    "RemotePypdfExtractor",
    "RemoteTectonicCompiler",
    "ToolServiceClient",
    "ToolServiceError",
]
