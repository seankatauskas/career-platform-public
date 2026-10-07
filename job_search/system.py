"""Production hosts that compose the dashboard and Hermes MCP boundary.

The workers, dashboard, and MCP server are separate processes, but every process is
assembled from the same owner-only runtime configuration.  This module keeps Graph
credentials, archive keys, databases, and execution objects behind narrow adapters;
Hermes receives only the frozen capabilities in :mod:`job_search.hermes`.
"""

from __future__ import annotations

import argparse
import os
import secrets
import stat
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from .autofill import AutofillBroker, EncryptedAutofillVault, load_profile
from .availability import AvailabilityPlanner, AvailabilityPolicy
from .dashboard import (
    DashboardController,
    DashboardSettings,
    make_server as make_dashboard_server,
    resume_submission_snapshot,
)
from .hermes_mcp import make_mcp_server_from_sources
from .integration import (
    ConfiguredShortlistSource,
    LedgerProposalSource,
    LocalJobCatalog,
    make_hermes_sources,
)
from .outlook.auth import MsalTokenProvider, default_cache_path
from .outlook.client import GraphOutlookClient
from .outlook.transport import GraphSession, UrllibHttpAdapter
from .preference import PreferenceGateway, PreferencePaths
from .curated import CuratedShortlists
from .runtime import DEFAULT_CONFIG_PATH, RuntimeConfigV1, load_runtime_config
from .resume_integration import ResumeLabGateway
from .scheduler import has_outlook_config, has_real_scraper_contact
from .runtime_readiness import runtime_readiness
from .service import JobSearchLedger


def _configured_resume_lab(
    config: RuntimeConfigV1,
    supplied: Optional[ResumeLabGateway],
    *,
    read_only: bool = False,
) -> Optional[ResumeLabGateway]:
    if supplied is not None:
        return supplied
    from .resume_lab.gateway import (
        build_resume_lab_gateway,
        build_resume_lab_read_gateway,
    )

    builder = build_resume_lab_read_gateway if read_only else build_resume_lab_gateway
    return builder(config)


def initialize_mcp_token(path: Path) -> Path:
    """Create a new owner-only bearer token without ever printing its value."""

    target = Path(path).expanduser().resolve()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(target.parent, 0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(target, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        value = (secrets.token_urlsafe(48) + "\n").encode("ascii")
        os.write(descriptor, value)
        os.fsync(descriptor)
    except Exception:
        os.close(descriptor)
        target.unlink(missing_ok=True)
        raise
    else:
        os.close(descriptor)
    return target


def read_mcp_token(path: Path) -> str:
    """Read a regular, owner-only MCP token and validate the server contract."""

    target = Path(path).expanduser().resolve()
    metadata = os.stat(target, follow_symlinks=False)
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("MCP token file must be a regular file")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise ValueError("MCP token file must be owned by the current user")
    if metadata.st_mode & 0o077:
        raise ValueError("MCP token file must be owner-only (mode 0600)")
    try:
        value = target.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError("MCP token file is unreadable") from exc
    if len(value) < 32 or any(character.isspace() for character in value):
        raise ValueError("MCP bearer token must be at least 32 non-space characters")
    return value


def build_notification_recovery(config: RuntimeConfigV1, ledger: JobSearchLedger):
    if config.hermes_notification_socket is None or not config.hermes_telegram_target:
        return None
    from .delivery_recovery import NotificationRecoveryService
    from .hermes_delivery import HermesDeliveryClient
    return NotificationRecoveryService(ledger, HermesDeliveryClient(
        config.hermes_notification_socket, expected_target=config.hermes_telegram_target,
        timeout_seconds=3,
    ))


def build_dashboard_controller(
    config: RuntimeConfigV1, *, resume_lab: Optional[ResumeLabGateway] = None, mail_source: Any = None
) -> DashboardController:
    ledger = JobSearchLedger(config.application_db)
    from .chief_runtime import configure_services
    configure_services(ledger, config)
    preferences = PreferenceGateway(
        PreferencePaths(config.jobs_db, config.preference_db, config.proxy_db)
    )
    resume = _configured_resume_lab(config, resume_lab)
    vault = None
    if config.autofill_vault is not None:
        persistence = None
        if config.portable_encryption_key_file is not None:
            from .secure_persistence import EncryptedFilePersistence

            persistence = EncryptedFilePersistence(
                config.autofill_vault,
                config.portable_encryption_key_file,
                "autofill-vault",
            )
        vault = EncryptedAutofillVault(
            config.autofill_vault, persistence=persistence
        )
    autofill = AutofillBroker(
        ledger,
        load_profile(config.autofill_profile),
        vault,
        submission_context=lambda application_id, decision: resume_submission_snapshot(
            resume, application_id, decision
        ),
    )
    environment = config.environment(os.environ)
    jobs = LocalJobCatalog(config.jobs_db)
    if has_outlook_config(environment):
        ledger.lifecycle.calendar = _DashboardCalendarSource(config)
    def review_classifier():
        from .runtime import _configured_mail_models
        classifier, _, _, version = _configured_mail_models(config, environment)
        return classifier, version
    return DashboardController(
        ledger,
        preferences,
        DashboardSettings(
            resume_mode=config.resume_mode,
            timezone=config.timezone,
            outlook_configured=has_outlook_config(environment),
            job_scraper_contact_configured=has_real_scraper_contact(environment),
            mail_folder=(config.outlook_mail_folders or ("inbox",))[0],
        ),
        autofill,
        jobs,
        resume,
        readiness=lambda: runtime_readiness(config, use_snapshot=True),
        notification_recovery=build_notification_recovery(config, ledger),
        mail_source=mail_source or _DashboardMailSource(config, ledger),
        review_classifier_factory=(review_classifier if config.mail_understanding_mode not in {'shared', 'paused'}
            and (config.mail_classifier_config or environment.get('JOB_SEARCH_MAIL_CLASSIFIER_CONFIG')
                 or config.remote_mail_inference_enabled) else None),
        automation_config=config,
        cost_snapshot_path=(Path(environment["JOB_SEARCH_COST_SNAPSHOT"])
                            if environment.get("JOB_SEARCH_COST_SNAPSHOT") else None),
    )


class _DashboardMailSource:
    """Archive dependencies are needed only when opening a linked message."""
    def __init__(self, config: RuntimeConfigV1, ledger: JobSearchLedger):
        self.config, self.ledger = config, ledger

    def get_mail_message(self, message_id: str):
        return _archive_source(self.config, self.ledger).get_mail_message(message_id)

    def get_review_message(self, message_id: str):
        return _archive_source(self.config, self.ledger).get_review_message(message_id)


def _archive_source(config: RuntimeConfigV1, ledger: JobSearchLedger):
    from .mail import build_archive_mail_source
    key_provider = None
    if config.portable_encryption_key_file is not None:
        from .secure_persistence import PortableArchiveKeyProvider
        key_provider = PortableArchiveKeyProvider(config.portable_encryption_key_file)
    return build_archive_mail_source(ledger, key_provider=key_provider)


def _outlook_from_config(
    config: RuntimeConfigV1, environment: Mapping[str, str]
):
    if not has_outlook_config(environment):
        return None
    cache_value = str(environment.get("OUTLOOK_TOKEN_CACHE") or "").strip()
    cache_path = Path(cache_value).expanduser() if cache_value else default_cache_path()
    persistence = None
    if config.portable_encryption_key_file is not None:
        from .secure_persistence import EncryptedFilePersistence

        persistence = EncryptedFilePersistence(
            cache_path.resolve(),
            config.portable_encryption_key_file,
            "outlook-token-cache",
        )
    provider = MsalTokenProvider(
        str(environment["OUTLOOK_CLIENT_ID"]),
        cache_path,
        account_home_id=(
            str(environment.get("OUTLOOK_HOME_ACCOUNT_ID") or "").strip() or None
        ),
        persistence=persistence,
    )
    outlook = GraphOutlookClient(GraphSession(provider, UrllibHttpAdapter()))
    return outlook


class _DashboardCalendarSource:
    """Construct the read adapter only when a user reviews an interview time."""
    def __init__(self, config):
        self.config = config

    def read_calendar_view(self, starts_at, ends_at):
        outlook = _outlook_from_config(self.config, self.config.environment(os.environ))
        if outlook is None:
            raise ValueError("Outlook availability is not configured")
        return outlook.read_calendar_view(starts_at, ends_at)


def _availability_from_config(config, environment):
    outlook = _outlook_from_config(config, environment)
    return AvailabilityPlanner(outlook, AvailabilityPolicy(timezone_name=config.timezone)) if outlook is not None else None


def build_hermes_sources_from_config(
    config: RuntimeConfigV1,
    *,
    mail_source: Any = None,
    availability: Optional[AvailabilityPlanner] = None,
    resume_lab: Optional[ResumeLabGateway] = None,
) -> Any:
    """Compose Hermes from bounded adapters, never from raw database/Graph clients."""

    ledger = JobSearchLedger(config.application_db)
    from .chief_runtime import configure_services
    configure_services(ledger, config)
    gateway = PreferenceGateway(
        PreferencePaths(config.jobs_db, config.preference_db, config.proxy_db)
    )
    if mail_source is None:
        from .mail import build_archive_mail_source

        key_provider = None
        if config.portable_encryption_key_file is not None:
            from .secure_persistence import PortableArchiveKeyProvider

            key_provider = PortableArchiveKeyProvider(
                config.portable_encryption_key_file
            )
        mail_source = build_archive_mail_source(
            ledger, key_provider=key_provider
        )
    environment = config.environment(os.environ)
    planner = availability
    if planner is None:
        planner = _availability_from_config(config, environment)
    resume = _configured_resume_lab(config, resume_lab, read_only=True)
    from .job_reviews.service import JobReviews
    from .job_reviews.context import profile_context
    from .scanning import scan_status
    return make_hermes_sources(
        reviews=JobReviews(config.application_db, LocalJobCatalog(config.jobs_db),
                           lambda: profile_context(resume), collection_provider=lambda: scan_status(config)),
        curated=CuratedShortlists(config.application_db, LocalJobCatalog(config.jobs_db)),
        jobs=LocalJobCatalog(config.jobs_db),
        shortlist=ConfiguredShortlistSource(
            gateway, ledger, config.shortlist_defaults()
        ),
        ledger=ledger,
        mail=mail_source,
        proposals=LedgerProposalSource(
            ledger,
            account_id=config.outlook_account_id,
            availability=planner,
        ),
        resume=resume,
        readiness=lambda: runtime_readiness(config, use_snapshot=True),
    )


def make_dashboard_host(
    config: RuntimeConfigV1, *, resume_lab: Optional[ResumeLabGateway] = None
):
    return make_dashboard_server(
        build_dashboard_controller(config, resume_lab=resume_lab), config.dashboard_port,
        https_origin=config.dashboard_https_origin,
        allowed_tailscale_login=config.dashboard_allowed_tailscale_login,
    )


def make_mcp_host(
    config: RuntimeConfigV1,
    *,
    mail_source: Any = None,
    resume_lab: Optional[ResumeLabGateway] = None,
    bind_host: str = "127.0.0.1",
    allowed_hosts: Sequence[str] = ("127.0.0.1", "localhost"),
):
    sources = build_hermes_sources_from_config(
        config, mail_source=mail_source, resume_lab=resume_lab
    )
    return make_mcp_server_from_sources(
        sources,
        read_mcp_token(config.mcp_token_file),
        config.mcp_port,
        bind_host=bind_host,
        allowed_hosts=allowed_hosts,
    )


def make_interaction_host(config: RuntimeConfigV1, *, bind_host="127.0.0.1", allowed_hosts=()):
    """Dedicated human-interaction ingress, separate from the model's MCP bearer."""
    if config.interaction_token_file is None:
        raise ValueError("configure Telegram identity and interaction_token_file first")
    from .chief_runtime import configure_services
    from .interactions.server import make_interaction_server
    ledger = configure_services(JobSearchLedger(config.application_db), config)
    return make_interaction_server(
        ledger.interactions, read_mcp_token(config.interaction_token_file),
        host=bind_host, port=config.interaction_port, allowed_hosts=allowed_hosts,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a job-search local service")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("service", choices=("dashboard", "mcp"))
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_runtime_config(args.config, required=True)
        server = (
            make_dashboard_host(config)
            if args.service == "dashboard"
            else make_mcp_host(config)
        )
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    url = (
        f"http://127.0.0.1:{config.dashboard_port}"
        if args.service == "dashboard"
        else f"http://127.0.0.1:{config.mcp_port}/mcp"
    )
    print(f"Job-search {args.service} listening on {url}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "build_dashboard_controller",
    "build_hermes_sources_from_config",
    "initialize_mcp_token",
    "make_dashboard_host",
    "make_mcp_host",
    "read_mcp_token",
]
