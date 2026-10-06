"""Untrusted-email normalization, classification, evaluation, and policy."""

from .context import CandidateApplication, bounded_candidates
from .evaluation import (
    BenchmarkCase,
    EvaluationObservation,
    EvaluationReport,
    EventClassMetrics,
    evaluate_observations,
    run_benchmark,
)
from .model import (
    LocalClassifierConfig,
    LocalCommandClassifier,
    ModelExecutionError,
    load_classifier_config,
)
from .pipeline import analyze_mail
from .policy import PolicyDecision, ProposalDisposition, decide_proposal
from .proposals import ProposalValidationError, build_proposal, validate_model_output
from .rules import RuleMatch, match_known_template
from .sanitizer import SanitizedMail, sanitize_mail
from .archive import AESGCMCipher, EncryptedMailArchive, KeychainArchiveKeyProvider
from .archive_source import EncryptedArchiveMailSource, build_archive_mail_source
from .attachments import (
    AttachmentRejected,
    SandboxedAttachmentExtractor,
    SecureAttachmentPipeline,
)
from .secure_ingest import SecureMailIngestor
from .runtime import build_secure_mail_ingestor
from .temporal import (
    LocalTemporalExtractor,
    TemporalExtractionError,
    TemporalProposalEngine,
    TemporalSource,
    validate_temporal_output,
)

__all__ = [
    "BenchmarkCase",
    "AESGCMCipher",
    "AttachmentRejected",
    "CandidateApplication",
    "EvaluationObservation",
    "EvaluationReport",
    "EventClassMetrics",
    "EncryptedMailArchive",
    "EncryptedArchiveMailSource",
    "KeychainArchiveKeyProvider",
    "LocalCommandClassifier",
    "LocalClassifierConfig",
    "ModelExecutionError",
    "PolicyDecision",
    "ProposalDisposition",
    "ProposalValidationError",
    "RemoteMailClassifier",
    "RemoteTemporalExtractor",
    "RuleMatch",
    "SandboxedAttachmentExtractor",
    "SanitizedMail",
    "SecureAttachmentPipeline",
    "SecureMailIngestor",
    "build_secure_mail_ingestor",
    "build_archive_mail_source",
    "LocalTemporalExtractor",
    "TemporalExtractionError",
    "TemporalProposalEngine",
    "TemporalSource",
    "analyze_mail",
    "bounded_candidates",
    "build_proposal",
    "decide_proposal",
    "evaluate_observations",
    "match_known_template",
    "load_classifier_config",
    "run_benchmark",
    "sanitize_mail",
    "validate_model_output",
    "validate_temporal_output",
]


def __getattr__(name):
    # Schema imports must not initialize provider usage (which depends on db).
    if name in {"RemoteMailClassifier", "RemoteTemporalExtractor"}:
        from . import remote
        return getattr(remote, name)
    raise AttributeError(name)
