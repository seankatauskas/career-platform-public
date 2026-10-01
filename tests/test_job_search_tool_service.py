#!/usr/bin/env python3
"""Offline checks for the networkless cloud document-tool boundary."""

from __future__ import annotations

import base64
import hashlib
import os
import tempfile
import threading
from pathlib import Path

from job_search.mail.attachments import (
    ICS_MIME,
    AttachmentDescriptor,
    AttachmentRejected,
)
from job_search.resume_lab.pdf import PdfExtractionError
from job_search.resume_lab.gateway import build_resume_lab_gateway, resume_lab_status
from job_search.resume_lab.tex import TexToolchainError, preflight_tex
from job_search.runtime import RuntimeConfigV1
from job_search.tool_service import (
    RemoteAttachmentExtractor,
    RemotePypdfExtractor,
    RemoteTectonicCompiler,
    ToolServiceClient,
    ToolServiceError,
    _ToolServer,
    _attest_network_namespace,
)


class FakeClient:
    def __init__(self, callback):
        self.callback = callback

    def call(self, operation, payload):
        return self.callback(operation, payload)


class FakeDispatcher:
    def dispatch(self, operation, payload):
        assert operation == "ping" and payload == {}
        return {
            "service_revision": "cloud-document-tools-v1",
            "engine": "tectonic",
            "engine_version": "0.15.0",
            "bundle_sha256": "a" * 64,
            "pypdf_version": "6.16.2",
        }


def expect(error_type, callback, text=""):
    try:
        callback()
    except error_type as exc:
        assert text in str(exc)
        return exc
    raise AssertionError(f"expected {error_type.__name__}")


def _source() -> str:
    return "\\documentclass{article}\n\\begin{document}\nResume\\end{document}\n"


def test_unix_protocol_is_owner_only_bounded_and_round_trips() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        os.chmod(root, 0o700)
        path = root / "tools.sock"
        server = _ToolServer(path, FakeDispatcher())
        os.chmod(path, 0o600)
        thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.01},
        )
        thread.start()
        try:
            result = ToolServiceClient(path, timeout_seconds=2).call("ping", {})
            assert result["service_revision"] == "cloud-document-tools-v1"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            path.unlink(missing_ok=True)
        assert not thread.is_alive()

        unsafe = root / "unsafe.sock"
        unsafe.touch()
        expect(ToolServiceError, lambda: ToolServiceClient(unsafe).call("ping", {}), "unsafe")


def test_resume_gateway_selects_remote_tools_and_doctor_checks_service() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        os.chmod(root, 0o700)
        socket_path = root / "tools.sock"
        server = _ToolServer(socket_path, FakeDispatcher())
        os.chmod(socket_path, 0o600)
        thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.01},
        )
        thread.start()
        config = RuntimeConfigV1.from_mapping(
            {
                "version": 1,
                "project_root": str(root),
                "resume_lab_db": "resume.db",
                "resume_artifact_root": "artifacts",
                "tool_service_socket": str(socket_path),
                "resume_tectonic_version": "0.15.0",
            },
            default_root=root,
        )
        try:
            report = resume_lab_status(config)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            socket_path.unlink(missing_ok=True)
        assert not thread.is_alive()
        assert report["tool_service_ready"] is True
        assert report["ready_for_import"] is True
        gateway = build_resume_lab_gateway(config)
        assert isinstance(gateway.toolchain.compiler, RemoteTectonicCompiler)
        assert isinstance(gateway.toolchain.extractor, RemotePypdfExtractor)


def test_remote_compiler_revalidates_pdf_hash_source_and_engine() -> None:
    source = _source()
    report = preflight_tex(source)
    pdf = b"%PDF-1.7\ncloud-test\n%%EOF\n"
    digest = hashlib.sha256(pdf).hexdigest()
    compiler = RemoteTectonicCompiler(Path("/private/tools.sock"), "0.15.0")
    compiler.client = FakeClient(
        lambda _operation, _payload: {
            "pdf_base64": base64.b64encode(pdf).decode("ascii"),
            "pdf_sha256": digest,
            "source_sha256": report.source_sha256,
            "engine": "tectonic",
            "engine_version": "0.15.0",
            "bundle_sha256": "a" * 64,
            "log_excerpt": "",
        }
    )
    compiled = compiler.compile(source)
    assert compiled.pdf_bytes == pdf and compiled.pdf_sha256 == digest

    compiler.client = FakeClient(
        lambda _operation, _payload: {
            "pdf_base64": base64.b64encode(pdf).decode("ascii"),
            "pdf_sha256": "0" * 64,
            "source_sha256": report.source_sha256,
            "engine": "tectonic",
            "engine_version": "0.15.0",
            "bundle_sha256": "a" * 64,
            "log_excerpt": "",
        }
    )
    expect(TexToolchainError, lambda: compiler.compile(source), "failed safely")


def test_remote_pdf_and_attachment_results_are_bound_to_inputs() -> None:
    pdf = b"%PDF-1.7\ncloud-test\n%%EOF\n"
    digest = hashlib.sha256(pdf).hexdigest()
    extractor = RemotePypdfExtractor(Path("/private/tools.sock"))
    extractor.client = FakeClient(
        lambda _operation, _payload: {
            "pdf_sha256": digest,
            "parser": "pypdf",
            "parser_version": "6.16.2",
            "pages": 1,
            "content_stream_bytes": 20,
            "logical_text": "Resume",
            "layout_text": "Resume",
        }
    )
    result = extractor.extract(pdf)
    assert result.pdf_sha256 == digest and result.logical_text == "Resume"
    extractor.client = FakeClient(
        lambda _operation, _payload: {
            "pdf_sha256": "0" * 64,
            "parser": "pypdf",
            "parser_version": "6.16.2",
            "pages": 1,
            "content_stream_bytes": 20,
            "logical_text": "Resume",
            "layout_text": "Resume",
        }
    )
    expect(PdfExtractionError, lambda: extractor.extract(pdf), "failed safely")

    content = b"BEGIN:VCALENDAR\nEND:VCALENDAR\n"
    descriptor = AttachmentDescriptor("attachment-1", "invite.ics", ICS_MIME, len(content))
    remote = RemoteAttachmentExtractor(Path("/private/tools.sock"))
    remote.client = FakeClient(
        lambda _operation, _payload: {
            "attachment_id": descriptor.attachment_id,
            "mime_type": descriptor.mime_type,
            "source_size": len(content),
            "source_sha256": hashlib.sha256(content).hexdigest(),
            "sanitized_text": "BEGIN:VCALENDAR\nEND:VCALENDAR",
            "truncated": False,
        }
    )
    extracted = remote.extract(descriptor, content)
    assert extracted.attachment_id == descriptor.attachment_id
    remote.client = FakeClient(lambda _operation, _payload: {})
    expect(AttachmentRejected, lambda: remote.extract(descriptor, content), "failed safely")


def test_networkless_attestation_accepts_inert_kernel_devices_and_rejects_escape_paths():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        interfaces = root / "net"
        interfaces.mkdir()
        for name, flags in (("lo", "0x9"), ("tunl0", "0x80"), ("gre0", "0x80")):
            (interfaces / name).mkdir()
            (interfaces / name / "flags").write_text(flags)
        (interfaces / "bonding_masters").write_text("")
        status, route, ipv6 = root / "status", root / "route", root / "ipv6"
        baseline = "CapEff: 0000000000000000\nCapPrm: 0000000000000000\nCapBnd: 0000000000000000\nCapAmb: 0000000000000000\nNoNewPrivs: 1\n"
        status.write_text(baseline)
        route.write_text("Iface Destination Gateway Flags\n")
        ipv6.write_text(" ".join(["0"] * 9 + ["lo"]) + "\n")
        check = lambda: _attest_network_namespace(interfaces, status, route, ipv6)
        check()
        (interfaces / "gre0" / "flags").write_text("0x81")
        expect(ToolServiceError, check)
        (interfaces / "gre0" / "flags").write_text("0x80")
        for key in ("CapEff", "CapPrm", "CapBnd", "CapAmb"):
            for bit in (12, 21):
                status.write_text(baseline.replace(f"{key}: 0000000000000000", f"{key}: {1 << bit:016x}"))
                expect(ToolServiceError, check)
        status.write_text(baseline.replace("NoNewPrivs: 1", "NoNewPrivs: 0"))
        expect(ToolServiceError, check)
        status.write_text(baseline)
        route.write_text("Iface Destination Gateway Flags\ngre0 00000000 00000000 0001\n")
        expect(ToolServiceError, check)
        route.write_text("Iface Destination Gateway Flags\n")
        ipv6.write_text(" ".join(["0"] * 9 + ["gre0"]) + "\n")
        expect(ToolServiceError, check)
        ipv6.write_text("")
        (interfaces / "gre0" / "flags").unlink()
        expect(ToolServiceError, check)


def main() -> None:
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in sorted(tests, key=lambda value: value.__name__):
        test()
    print("ok")


if __name__ == "__main__":
    main()
