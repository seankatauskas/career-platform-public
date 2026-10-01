"""Composable secure ingestion for archived mail, attachments, and temporal review."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from job_search.contracts import MutationContext, payload_sha256

from .archive import MAX_ARCHIVE_CHARS, EncryptedMailArchive
from .context import CandidateApplication
from .sanitizer import sanitize_mail
from .temporal import TemporalProposalEngine, TemporalSource


@dataclass(frozen=True)
class SecureIngestResult:
    archive_id: str
    attachments_archived: int
    temporal_proposals: int


class SecureMailIngestor:
    """Archive every supplied message; analyze only explicitly eligible recruiting mail."""

    def __init__(
        self,
        archive: EncryptedMailArchive,
        *,
        attachments: Any | None = None,
        temporal: TemporalProposalEngine | None = None,
    ) -> None:
        self._archive = archive
        self._attachments = attachments
        self._temporal = temporal

    def ingest(
        self,
        *,
        account_id: str,
        immutable_message_id: str,
        subject: str,
        body: str,
        body_kind: str,
        received_at: str,
        candidates: Sequence[CandidateApplication],
        has_attachments: bool,
        analyze_temporal: bool,
        default_time_zone: str = "America/Chicago",
    ) -> SecureIngestResult:
        sanitized = sanitize_mail(
            subject, body, body_kind=body_kind, max_chars=MAX_ARCHIVE_CHARS
        )
        archive_result = self._archive.archive_message(
            account_id=account_id,
            immutable_message_id=immutable_message_id,
            sanitized_text=sanitized.text,
            truncated=sanitized.truncated,
            context=MutationContext(
                "archive:" + payload_sha256(
                    {
                        "account_id": account_id,
                        "immutable_message_id": immutable_message_id,
                        "sanitized_sha256": sanitized.content_sha256,
                    }
                ),
                "system",
                "outlook_secure_archive",
                immutable_message_id,
            ),
        )
        archive_id = str(archive_result["archive"]["archive_id"])
        proposals = 0
        if analyze_temporal and self._temporal is not None:
            proposals += len(self._temporal.propose(
                TemporalSource(archive_id, sanitized.text, received_at),
                candidates,
                default_time_zone=default_time_zone,
            ))
        archived_attachments = 0
        if has_attachments and self._attachments is not None:
            for extracted in self._attachments.acquire(immutable_message_id):
                attachment_result = self._archive.archive_attachment_text(
                    archive_id=archive_id,
                    immutable_attachment_id=extracted.attachment_id,
                    mime_type=extracted.mime_type,
                    source_size=extracted.source_size,
                    source_sha256=extracted.source_sha256,
                    extracted_text=extracted.sanitized_text,
                    context=MutationContext(
                        "archive-attachment:" + payload_sha256(
                            {
                                "archive_id": archive_id,
                                "attachment_id": extracted.attachment_id,
                                "source_sha256": extracted.source_sha256,
                            }
                        ),
                        "system",
                        "outlook_secure_archive",
                        immutable_message_id,
                    ),
                )
                archived_attachments += int(bool(attachment_result["created"]))
                if analyze_temporal and self._temporal is not None:
                    attachment = attachment_result["attachment"]
                    proposals += len(self._temporal.propose(
                        TemporalSource(
                            archive_id,
                            extracted.sanitized_text,
                            received_at,
                            str(attachment["attachment_record_id"]),
                        ),
                        candidates,
                        default_time_zone=default_time_zone,
                    ))
        return SecureIngestResult(archive_id, archived_attachments, proposals)
