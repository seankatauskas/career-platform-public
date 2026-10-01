"""Narrow production assembly for encrypted Outlook mail ingestion.

This module owns only mail-content capabilities.  It does not construct Graph
authentication, workers, schedules, notifications, or Hermes, which keeps tokens and
raw message bodies outside the generic runtime registry.
"""

from __future__ import annotations

from typing import Any

from .archive import EncryptedMailArchive, KeychainArchiveKeyProvider
from .attachments import SandboxedAttachmentExtractor, SecureAttachmentPipeline
from .secure_ingest import SecureMailIngestor
from .temporal import LocalTemporalExtractor, TemporalProposalEngine


def build_secure_mail_ingestor(
    service: Any,
    mail: Any,
    *,
    classifier_config: Any | None = None,
    archive: Any | None = None,
    attachments: Any | None = None,
    key_provider: Any | None = None,
    attachment_extractor: Any | None = None,
    temporal_extractor: Any | None = None,
    temporal_producer_version: str = "",
) -> SecureMailIngestor:
    """Build the archive/attachment/temporal lane from narrow injected adapters.

    The optional local classifier config is also the local-model command contract for
    temporal extraction: that fixed JSON-in/JSON-out executable must dispatch on the
    request ``task`` field.  With no model config, full sanitized mail and eligible
    attachments are still encrypted, while temporal extraction remains disabled.
    """

    encrypted_archive = archive or EncryptedMailArchive(
        service, key_provider or KeychainArchiveKeyProvider()
    )
    attachment_pipeline = attachments or SecureAttachmentPipeline(
        mail, attachment_extractor or SandboxedAttachmentExtractor()
    )
    temporal = None
    if classifier_config is not None:
        extractor = temporal_extractor or LocalTemporalExtractor(
            classifier_config.command,
            allowed_read_paths=classifier_config.allowed_read_paths,
            timeout_seconds=classifier_config.timeout_seconds,
        )
        temporal = TemporalProposalEngine(
            service, extractor, classifier_config.producer_version
        )
    elif temporal_extractor is not None:
        temporal = TemporalProposalEngine(
            service, temporal_extractor, temporal_producer_version
        )
    return SecureMailIngestor(
        encrypted_archive,
        attachments=attachment_pipeline,
        temporal=temporal,
    )


__all__ = ["build_secure_mail_ingestor"]
