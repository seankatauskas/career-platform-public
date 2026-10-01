#!/usr/bin/env python3
"""Offline security and lifecycle tests for the full Outlook mail stream."""

from __future__ import annotations

import hashlib
import hmac
import base64
import os
import tempfile
from unittest.mock import patch
import json
import subprocess
from pathlib import Path

from job_search.contracts import (
    ContractError,
    JobSnapshot,
    MailChange,
    MailDeltaPage,
    MutationContext,
    RecommendationProvenance,
)
from job_search.db import connect
from job_search.mail import (
    AttachmentRejected,
    EncryptedMailArchive,
    KeychainArchiveKeyProvider,
    LocalTemporalExtractor,
    SandboxedAttachmentExtractor,
    SecureAttachmentPipeline,
    SecureMailIngestor,
    TemporalExtractionError,
    TemporalProposalEngine,
    TemporalSource,
    validate_temporal_output,
)
from job_search.mail.attachments import ICS_MIME, validate_attachment_descriptor
from job_search.mail.context import CandidateApplication
from job_search.outlook.mail import GraphMailClient, MailFolder
from job_search.outlook.state import SQLiteOutlookState
from job_search.service import JobSearchLedger
from job_search.secure_persistence import (
    PortableArchiveKeyProvider,
    initialize_portable_master_key,
)
from job_search.sync import ALL_HISTORY_QUERY_VERSION, OutlookMailCoordinator


NOW = "2026-09-02T12:00:00Z"


def context(key: str, actor: str = "system") -> MutationContext:
    return MutationContext(key, actor, "secure_mail_test")


def start_application(service: JobSearchLedger) -> str:
    result = service.start_application(
        JobSnapshot(
            "greenhouse", "job-1", "family-1", "Engineer", "Example Labs",
            "example", "https://example.test/job-1",
        ),
        RecommendationProvenance(),
        context("start-secure-mail", "user"),
    )
    return str(result["application"]["application_id"])


class FakeEncryptedPersistence:
    is_encrypted = True

    def __init__(self) -> None:
        self.value = ""

    def load(self):
        return self.value

    def save(self, value):
        self.value = value


class TestCipher:
    """Small authenticated test cipher; production uses lazy AES-256-GCM."""

    def __init__(self, key: bytes) -> None:
        self.key = key

    def encrypt(self, nonce: bytes, plaintext: bytes, aad: bytes) -> bytes:
        mask = hashlib.sha256(self.key + nonce).digest()
        body = bytes(value ^ mask[index % len(mask)] for index, value in enumerate(plaintext))
        return body + hmac.new(self.key, nonce + aad + body, hashlib.sha256).digest()[:16]

    def decrypt(self, nonce: bytes, ciphertext: bytes, aad: bytes) -> bytes:
        body, tag = ciphertext[:-16], ciphertext[-16:]
        expected = hmac.new(self.key, nonce + aad + body, hashlib.sha256).digest()[:16]
        if not hmac.compare_digest(tag, expected):
            raise ValueError("authentication failed")
        mask = hashlib.sha256(self.key + nonce).digest()
        return bytes(value ^ mask[index % len(mask)] for index, value in enumerate(body))


def make_archive(directory: str):
    service = JobSearchLedger(Path(directory) / "job-search.db")
    persistence = FakeEncryptedPersistence()
    provider = KeychainArchiveKeyProvider(
        Path(directory) / "key.bin", persistence=persistence
    )
    archive = EncryptedMailArchive(
        service,
        provider,
        cipher_factory=TestCipher,
        nonce_factory=lambda size: b"N" * size,
    )
    return service, archive, persistence


def test_archive_requires_encrypted_key_storage_and_authenticates_ciphertext() -> None:
    class Plain(FakeEncryptedPersistence):
        is_encrypted = False

    with tempfile.TemporaryDirectory() as directory:
        try:
            KeychainArchiveKeyProvider(Path(directory) / "plain", persistence=Plain())
        except Exception as exc:
            assert "plaintext" in str(exc)
        else:
            raise AssertionError("plaintext archive key persistence was accepted")

        service, archive, persistence = make_archive(directory)
        secret = "BEGIN UNTRUSTED EMAIL\nSUBJECT\nInterview\nBODY\nPrivate body\nEND UNTRUSTED EMAIL"
        saved = archive.archive_message(
            account_id="personal",
            immutable_message_id="message-1",
            sanitized_text=secret,
            truncated=False,
            context=context("archive-message-1"),
        )
        archive_id = saved["archive"]["archive_id"]
        assert archive.read_message(archive_id) == secret
        attachment_text = "BEGIN UNTRUSTED EMAIL\nSUBJECT\nAttachment\nBODY\nConfidential invite\nEND UNTRUSTED EMAIL"
        attached = archive.archive_attachment_text(
            archive_id=archive_id,
            immutable_attachment_id="attachment-1",
            mime_type="text/calendar",
            source_size=128,
            source_sha256=hashlib.sha256(b"calendar source").hexdigest(),
            extracted_text=attachment_text,
            context=context("archive-attachment-1"),
        )
        attachment_id = attached["attachment"]["attachment_record_id"]
        assert archive.read_attachment_text(attachment_id) == attachment_text
        # AES-GCM produces a fresh envelope before the durable command lookup.
        # Retrying the same logical write must not conflict merely because its
        # nonce and ciphertext differ.
        retry_archive = EncryptedMailArchive(
            service,
            KeychainArchiveKeyProvider(
                Path(directory) / "key.bin", persistence=persistence
            ),
            cipher_factory=TestCipher,
            nonce_factory=lambda size: b"R" * size,
        )
        replayed = retry_archive.archive_message(
            account_id="personal",
            immutable_message_id="message-1",
            sanitized_text=secret,
            truncated=False,
            context=context("archive-message-1"),
        )
        assert replayed["archive"]["archive_id"] == archive_id
        replayed_attachment = retry_archive.archive_attachment_text(
            archive_id=archive_id,
            immutable_attachment_id="attachment-1",
            mime_type="text/calendar",
            source_size=128,
            source_sha256=hashlib.sha256(b"calendar source").hexdigest(),
            extracted_text=attachment_text,
            context=context("archive-attachment-1"),
        )
        assert replayed_attachment["attachment"]["attachment_record_id"] == attachment_id
        assert persistence.value and "Private body" not in persistence.value
        assert b"Private body" not in (Path(directory) / "job-search.db").read_bytes()
        assert b"Confidential invite" not in (Path(directory) / "job-search.db").read_bytes()
        with connect(Path(directory) / "job-search.db") as con:
            ciphertext = bytearray(con.execute(
                "SELECT ciphertext FROM mail_archive WHERE archive_id=?", (archive_id,)
            ).fetchone()[0])
            ciphertext[0] ^= 1
            con.execute(
                "UPDATE mail_archive SET ciphertext=? WHERE archive_id=?",
                (bytes(ciphertext), archive_id),
            )
        try:
            archive.read_message(archive_id)
        except Exception as exc:
            assert "authentication" in str(exc)
        else:
            raise AssertionError("tampered archive ciphertext was accepted")


def test_portable_archive_revalidates_mounted_key_before_reads_and_writes() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        key_file = initialize_portable_master_key(root / "master-key")
        service = JobSearchLedger(root / "job-search.db")
        archive = EncryptedMailArchive(
            service,
            PortableArchiveKeyProvider(key_file),
            cipher_factory=TestCipher,
            nonce_factory=lambda size: b"N" * size,
        )
        saved = archive.archive_message(
            account_id="personal",
            immutable_message_id="message-1",
            sanitized_text="private state",
            truncated=False,
            context=context("portable-archive-before-key-change"),
        )
        archive_id = saved["archive"]["archive_id"]
        key_file.write_bytes(base64.b64encode(os.urandom(32)) + b"\n")
        os.chmod(key_file, 0o600)

        for operation in (
            lambda: archive.read_message(archive_id),
            lambda: archive.archive_message(
                account_id="personal",
                immutable_message_id="message-2",
                sanitized_text="must not be written",
                truncated=False,
                context=context("portable-archive-after-key-change"),
            ),
        ):
            try:
                operation()
            except ContractError as exc:
                assert "changed while process was running" in str(exc)
            else:
                raise AssertionError("changed portable archive key was accepted")


class FolderMail:
    def __init__(self) -> None:
        self.synced = []
        self.folders = (
            MailFolder("inbox-id", "root", 0, False),
            MailFolder("archive-id", "root", 0, False),
            MailFolder("junk-id", "root", 1, False),
            MailFolder("junk-child", "junk-id", 0, False),
            MailFolder("deleted-id", "root", 0, False),
        )

    def read_mail_folder(self, value):
        return next(
            item for item in self.folders
            if item.folder_id == ("junk-id" if value == "junkemail" else "deleted-id")
        )

    def list_folder_tree(self, max_folders=4096):
        assert max_folders == 4096
        return self.folders

    def initial_all_history_delta_url(self, *, folder):
        self.synced.append(folder)
        return "all:" + folder

    def read_delta_page(self, url):
        changes = ()
        if url == "all:inbox-id":
            changes = (
                MailChange(
                    "history-message", False, sender_address="updates@example.test",
                    subject="Account update", received_at=NOW, modified_at=NOW,
                ),
            )
        return MailDeltaPage(
            changes, None, "https://graph.microsoft.com/v1.0/delta/" + url[4:]
        )

    def read_message_body(self, message_id):
        assert message_id == "history-message"
        return {
            "subject": "Account update",
            "receivedDateTime": NOW,
            "sender": {"emailAddress": {"address": "updates@example.test"}},
            "body": {"contentType": "text", "content": "historical private body"},
            "hasAttachments": False,
        }


def test_all_history_sync_persists_id_only_inventory_and_excludes_subtrees() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "job-search.db"
        state = SQLiteOutlookState(path)
        service = JobSearchLedger(path)
        mail = FolderMail()
        _, archive, _ = make_archive(directory)
        coordinator = OutlookMailCoordinator(
            mail, state, service, secure_ingestor=SecureMailIngestor(archive)
        )
        result = coordinator.sync_all_history("personal")
        assert result.discovered == 5 and result.excluded == 3
        assert result.folders_synced == 2 and set(mail.synced) == {"inbox-id", "archive-id"}
        assert result.messages_staged == 1
        assert state.eligible_folders("personal") == ("archive-id", "inbox-id")
        for folder in mail.synced:
            cursor = state.load("personal", folder, ALL_HISTORY_QUERY_VERSION)
            assert not cursor.needs_backfill and cursor.committed_delta_link
        with connect(path) as con:
            columns = {row[1] for row in con.execute("PRAGMA table_info(outlook_folder_inventory)")}
            assert "display_name" not in columns
        processed = coordinator.process_pending(query_version=ALL_HISTORY_QUERY_VERSION)
        assert processed.ignored == 1 and processed.failed == 0
        with connect(path) as con:
            archive_id = con.execute("SELECT archive_id FROM mail_archive").fetchone()[0]
        assert "historical private body" in archive.read_message(archive_id)


def test_graph_folder_and_attachment_reads_are_bounded_and_all_history_has_no_cutoff() -> None:
    content = b"BEGIN:VCALENDAR\nEND:VCALENDAR\n"

    class Session:
        def __init__(self):
            self.urls = []

        def request_json(self, method, url, **kwargs):
            assert method == "GET"
            assert kwargs.get("payload") is None
            self.urls.append(url)
            if "/attachments/" in url:
                return {**ics_descriptor(content), "contentBytes": base64.b64encode(content).decode()}
            if url.startswith("/v1.0/me/mailFolders?"):
                return {"value": [{
                    "id": "parent", "parentFolderId": "root",
                    "childFolderCount": 1, "isHidden": False,
                }]}
            if "/childFolders?" in url:
                return {"value": [{
                    "id": "child", "parentFolderId": "parent",
                    "childFolderCount": 0, "isHidden": True,
                }]}
            if url.endswith("/attachments?%24select=id%2Cname%2CcontentType%2Csize%2CisInline&%24top=20"):
                return {"value": [ics_descriptor(content)]}
            raise AssertionError(url)

    session = Session()
    mail = GraphMailClient(session)
    history_url = mail.initial_all_history_delta_url(folder="folder/id")
    assert "%24filter" not in history_url and "%2F" in history_url
    assert [item.folder_id for item in mail.list_folder_tree()] == ["child", "parent"]
    assert mail.list_attachments("message-1")[0]["name"] == "invite.ics"
    downloaded = mail.read_file_attachment("message-1", "attachment-1")
    assert downloaded["contentBytes"] == content


def ics_descriptor(content: bytes):
    return {
        "@odata.type": "#microsoft.graph.fileAttachment",
        "id": "attachment-1",
        "name": "invite.ics",
        "contentType": ICS_MIME,
        "size": len(content),
        "isInline": False,
    }


def test_attachment_pipeline_filters_before_download_and_extracts_in_bounded_worker() -> None:
    content = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
        b"DTSTART:20260903T150000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )

    class Mail:
        reads = []

        def list_attachments(self, _message, limit):
            assert limit == 20
            unsafe = dict(ics_descriptor(content), id="unsafe", contentType="text/plain", name="x.txt")
            return (unsafe, ics_descriptor(content))

        def read_file_attachment(self, message, attachment):
            self.reads.append((message, attachment))
            return {**ics_descriptor(content), "contentBytes": content}

    extractor = SandboxedAttachmentExtractor(
        isolation_builder=lambda command, _directory: command,
        timeout_seconds=10,
    )
    mail = Mail()
    values = SecureAttachmentPipeline(mail, extractor).acquire("message-1")
    assert mail.reads == [("message-1", "attachment-1")]
    assert len(values) == 1 and "DTSTART:20260903T150000Z" in values[0].sanitized_text
    spoofed = dict(ics_descriptor(content), name="invite.pdf")
    try:
        validate_attachment_descriptor(spoofed)
    except AttachmentRejected:
        pass
    else:
        raise AssertionError("MIME/extension-spoofed attachment was accepted")


def test_secure_ingestion_archives_full_sanitized_body_without_widening_excerpt() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "job-search.db"
        service, archive, _ = make_archive(directory)
        state = SQLiteOutlookState(path)
        state.stage_changes(
            "personal",
            "inbox",
            [MailChange(
                "long-message", False, sender_address="recruiter@example.test",
                subject="Interview update", received_at=NOW, modified_at=NOW,
            )],
        )

        class Mail:
            def read_message_body(self, message_id):
                assert message_id == "long-message"
                return {
                    "subject": "Interview update",
                    "receivedDateTime": NOW,
                    "sender": {"emailAddress": {"address": "recruiter@example.test"}},
                    "body": {"contentType": "text", "content": "private " + "x" * 6000},
                    "hasAttachments": False,
                }

        ingestor = SecureMailIngestor(archive)
        result = OutlookMailCoordinator(
            Mail(), state, service, secure_ingestor=ingestor
        ).process_pending()
        assert result.processed == 1 and result.failed == 0
        with connect(path) as con:
            evidence = con.execute("SELECT excerpt FROM mail_evidence").fetchone()[0]
            archive_id = con.execute("SELECT archive_id FROM mail_archive").fetchone()[0]
        assert len(evidence) == 2048
        assert len(archive.read_message(archive_id)) > len(evidence)


class FixedTemporalExtractor:
    def __init__(self, quote: str) -> None:
        self.quote = quote

    def extract(self, source, candidates, default_time_zone):
        assert default_time_zone == "America/Chicago" and len(candidates) == 1
        start = source.text.index(self.quote)
        return {
            "proposals": [
                {
                    "kind": "interview",
                    "application_id": candidates[0].application_id,
                    "confidence": 0.95,
                    "evidence_quote": self.quote,
                    "span_start": start,
                    "span_end": start + len(self.quote),
                    "starts_at": "2026-09-03T15:00:00Z",
                    "ends_at": "2026-09-03T15:30:00Z",
                    "due_at": None,
                    "time_zone": "America/Chicago",
                }
            ]
        }


class FixedDeadlineExtractor:
    def extract(self, source, candidates, default_time_zone):
        del default_time_zone
        quote = "complete the assessment by Friday"
        start = source.text.index(quote)
        return {"proposals": [{
            "kind": "deadline",
            "application_id": candidates[0].application_id,
            "confidence": 0.9,
            "evidence_quote": quote,
            "span_start": start,
            "span_end": start + len(quote),
            "starts_at": None,
            "ends_at": None,
            "due_at": "2026-09-04T22:00:00Z",
            "time_zone": "America/Chicago",
        }]}


@patch("job_search.store.utc_now", lambda: NOW)
def test_temporal_proposal_is_review_only_then_acceptance_records_schedule_and_reminders() -> None:
    with tempfile.TemporaryDirectory() as directory:
        service, archive, _ = make_archive(directory)
        application_id = start_application(service)
        text = (
            "BEGIN UNTRUSTED EMAIL\nSUBJECT\nInterview\nBODY\n"
            "Can we meet September 3 at 10 AM Central?\nEND UNTRUSTED EMAIL"
        )
        archived = archive.archive_message(
            account_id="personal",
            immutable_message_id="temporal-message",
            sanitized_text=text,
            truncated=False,
            context=context("archive-temporal"),
        )
        archive_id = archived["archive"]["archive_id"]
        candidate = CandidateApplication(
            application_id, "greenhouse", "job-1", "Example Labs", "Engineer"
        )
        source = TemporalSource(archive_id, text, NOW)
        engine = TemporalProposalEngine(
            service, FixedTemporalExtractor("September 3 at 10 AM Central"), "temporal-v1"
        )
        saved = engine.propose(source, [candidate])
        proposal = saved[0]["proposal"]
        assert proposal["status"] == "pending"
        assert service.list_interview_schedules() == ()
        decided = service.decide_temporal_proposal(
            proposal["temporal_proposal_id"],
            "accepted",
            "confirmed with recruiter",
            context("accept-temporal", "user"),
        )
        assert decided["status"] == "accepted" and decided["schedule"]
        assert len(decided["reminders"]) == 2
        assert len(service.list_interview_schedules()) == 1
        timeline = service.get_application_timeline(application_id)
        assert timeline["application"]["current_phase"] == "interviewing"
        assert timeline["events"][-1]["event_type"] == "interview_scheduled"
        assert {
            item["kind"]
            for item in service.list_due_local_reminders("2026-09-04T00:00:00Z")
        } == {"interview_24h", "interview_1h"}
        completed = service.complete_local_reminder(
            decided["reminders"][0]["reminder_id"],
            "completed",
            context("complete-reminder"),
        )
        assert completed["status"] == "completed"


def test_temporal_validation_rejects_tool_fields_bad_zones_and_unbound_evidence() -> None:
    source = TemporalSource("archive-1", "Meet tomorrow at ten", NOW)
    candidate = CandidateApplication(
        "application-1", "lever", "job-1", "Example", "Engineer"
    )
    start = source.text.index("tomorrow")
    valid = {
        "proposals": [{
            "kind": "deadline",
            "application_id": candidate.application_id,
            "confidence": 0.8,
            "evidence_quote": "tomorrow",
            "span_start": start,
            "span_end": start + len("tomorrow"),
            "starts_at": None,
            "ends_at": None,
            "due_at": "2026-09-03T12:00:00Z",
            "time_zone": "America/Chicago",
        }]
    }
    assert validate_temporal_output(
        valid, source=source, candidates=[candidate], producer_version="temporal-v1"
    )
    for mutated in (
        {"proposals": [{**valid["proposals"][0], "tool_call": "calendar"}]},
        {"proposals": [{**valid["proposals"][0], "time_zone": "Central-ish"}]},
        {"proposals": [{**valid["proposals"][0], "span_start": 0}]},
    ):
        try:
            validate_temporal_output(
                mutated, source=source, candidates=[candidate], producer_version="temporal-v1"
            )
        except TemporalExtractionError:
            pass
        else:
            raise AssertionError("unsafe temporal output was accepted")


def test_local_temporal_command_has_no_shell_tools_network_or_inherited_secrets() -> None:
    captured = {}

    def isolate(command, directory, read_paths):
        assert directory.name.startswith("job-mail-temporal-") and not read_paths
        return ("isolated", *command)

    def runner(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0, '{"proposals":[]}', "")

    source = TemporalSource("archive-1", "Interview next week", NOW)
    candidate = CandidateApplication(
        "application-1", "lever", "job-1", "Example", "Engineer"
    )
    result = LocalTemporalExtractor(
        ("local-model", "--json"), isolation_builder=isolate, runner=runner
    ).extract(source, [candidate], "America/Chicago")
    request = json.loads(captured["input"])
    assert result == {"proposals": []}
    assert captured["shell"] is False and captured["command"][:2] == ("isolated", "local-model")
    assert request["constraints"]["no_tools"] is True
    assert request["constraints"]["no_network"] is True
    assert not ({"OPENAI_API_KEY", "DATABASE_URL", "OUTLOOK_CLIENT_ID"} & captured["env"].keys())


def test_accepted_deadline_creates_queryable_local_reminder_without_schedule() -> None:
    with tempfile.TemporaryDirectory() as directory:
        service, archive, _ = make_archive(directory)
        application_id = start_application(service)
        text = (
            "BEGIN UNTRUSTED EMAIL\nSUBJECT\nAssessment\nBODY\nPlease complete the "
            "assessment by Friday.\nEND UNTRUSTED EMAIL"
        )
        archived = archive.archive_message(
            account_id="personal",
            immutable_message_id="deadline-message",
            sanitized_text=text,
            truncated=False,
            context=context("archive-deadline"),
        )
        source = TemporalSource(archived["archive"]["archive_id"], text, NOW)
        candidate = CandidateApplication(
            application_id, "greenhouse", "job-1", "Example Labs", "Engineer"
        )
        proposal = TemporalProposalEngine(
            service, FixedDeadlineExtractor(), "deadline-v1"
        ).propose(source, [candidate])[0]["proposal"]
        accepted = service.decide_temporal_proposal(
            proposal["temporal_proposal_id"],
            "accepted",
            "deadline confirmed",
            context("accept-deadline", "user"),
        )
        assert accepted["schedule"] is None
        assert [item["kind"] for item in accepted["reminders"]] == ["deadline"]
        assert service.list_interview_schedules() == ()
        assert service.list_due_local_reminders("2026-09-04T22:00:00Z")[0]["kind"] == "deadline"


def main() -> None:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"ok ({len(tests)} secure mail tests)")


if __name__ == "__main__":
    main()
