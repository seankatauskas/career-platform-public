"""Versioned runtime configuration and the deterministic composition root."""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from .db import prepare_database
from .dashboard_access import DashboardAccess
from .pipeline import NOTIFICATION_TASK, build_opportunity_handlers
from .preference import PreferenceGateway, PreferencePaths
from .scheduler import (
    SCHEDULE_TIMEZONE,
    has_outlook_config,
    seed_default_schedules,
    utc_stamp,
)
from .worker import (
    ATSCommandHandler,
    ApprovedActionTaskHandler,
    DueLocalReminderTaskHandler,
    OutlookMailTaskHandler,
    OutboxContext,
    TaskContext,
    Worker,
    _safe_error,
    _unavailable_outlook_handler,
    preference_outbox_handler,
)


CONFIG_VERSION = 1
DEFAULT_CONFIG_PATH = Path.home() / ".config" / "job-search" / "config.json"
DEFAULT_DASHBOARD_PORT = 8766
DEFAULT_MCP_PORT = 8767


def _path(
    value: Any, name: str, root: Path, *, optional: bool = False
) -> Optional[Path]:
    if (value is None or value == "") and optional:
        return None
    if not isinstance(value, (str, os.PathLike)):
        raise ValueError(f"{name} must be a path")
    candidate = Path(value).expanduser()
    return (candidate if candidate.is_absolute() else root / candidate).resolve()


def _text(value: Any, name: str, maximum: int = 1000) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text")
    result = value.strip()
    if len(result) > maximum or any(ord(character) < 32 for character in result):
        raise ValueError(f"{name} is invalid")
    return result


def _port(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not 1 <= result <= 65535:
        raise ValueError(f"{name} must be between 1 and 65535")
    return result


@dataclass(frozen=True)
class RuntimeConfigV1:
    version: int
    project_root: Path
    application_db: Path
    jobs_db: Path
    preference_db: Path
    proxy_db: Path
    resume_lab_db: Optional[Path] = None
    resume_artifact_root: Optional[Path] = None
    resume_mode: str = "tailored"
    resume_model_config: Optional[Path] = None
    resume_tectonic_executable: Optional[Path] = None
    resume_tectonic_bundle: Optional[Path] = None
    resume_tectonic_version: str = ""
    tool_service_socket: Optional[Path] = None
    timezone: str = SCHEDULE_TIMEZONE
    dashboard_port: int = DEFAULT_DASHBOARD_PORT
    dashboard_https_origin: str = ""
    dashboard_allowed_tailscale_login: str = ""
    mcp_port: int = DEFAULT_MCP_PORT
    mcp_token_file: Path = field(
        default_factory=lambda: Path.home() / ".config/job-search/mcp-token"
    )
    log_dir: Path = field(
        default_factory=lambda: Path.home() / ".local/state/job-search/logs"
    )
    autofill_profile: Optional[Path] = None
    autofill_vault: Optional[Path] = None
    outlook_new_messages_only: bool = False
    mail_recruiting_only: bool = False
    outlook_client_id: str = ""
    outlook_account_id: str = "outlook-personal"
    outlook_mail_folders: tuple[str, ...] = ("inbox",)
    outlook_poll_interval_minutes: int = 5
    scraper_contact: str = ""
    board_registry_path: Optional[Path] = None
    mail_classifier_config: Optional[Path] = None
    mail_inference_config: Optional[Path] = None
    # Optional AWS secret projection source; never exposed in public diagnostics.
    mail_inference_profile: Optional[Mapping[str, Any]] = field(default=None, repr=False)
    inference_config: Optional[Path] = None
    inference_usage_limits: Mapping[str, Any] = field(default_factory=dict)
    remote_mail_inference_enabled: bool = False
    remote_mail_temporal_enabled: bool = True
    portable_encryption_key_file: Optional[Path] = field(
        default=None, repr=False, compare=False
    )
    shortlist_limit: int = 20
    shortlist_policy: str = "champion"
    shortlist_days: int = 30
    shortlist_salary_floor: Optional[float] = None
    shortlist_remote_only: bool = False
    shortlist_max_per_company: int = 2
    shortlist_max_per_title: int = 2
    shortlist_notifications_enabled: bool = False
    shortlist_notification_min_new_jobs: int = 1
    shortlist_notification_cooldown_minutes: int = 240
    shortlist_notification_start_at: Optional[str] = None
    hermes_container: str = "job-search-chief-of-staff"
    hermes_image: str = ""
    hermes_data_dir: Optional[Path] = None
    hermes_workspace_dir: Optional[Path] = None
    hermes_executable: Optional[Path] = None
    hermes_notification_socket: Optional[Path] = None
    hermes_telegram_target: str = ""
    source_path: Optional[Path] = field(default=None, repr=False, compare=False)

    @classmethod
    def defaults(cls, project_root: Optional[Path] = None) -> "RuntimeConfigV1":
        root = (project_root or Path.cwd()).expanduser().resolve()
        return cls(
            version=CONFIG_VERSION,
            project_root=root,
            application_db=root / "job-search.db",
            jobs_db=root / "job-boards.db",
            preference_db=root / "job-boards-preference.db",
            proxy_db=root / "job-boards-proxy.db",
            log_dir=(Path.home() / ".local/state/job-search/logs").resolve(),
            mcp_token_file=(Path.home() / ".config/job-search/mcp-token").resolve(),
            hermes_data_dir=(Path.home() / ".local/share/job-search/hermes").resolve(),
            hermes_workspace_dir=(root / "hermes-workspace").resolve(),
        )

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        source_path: Optional[Path] = None,
        default_root: Optional[Path] = None,
    ) -> "RuntimeConfigV1":
        allowed = {item.name for item in cls.__dataclass_fields__.values()} - {
            "source_path"
        }
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(
                f"unknown runtime config fields: {', '.join(sorted(unknown))}"
            )
        if value.get("version", CONFIG_VERSION) != CONFIG_VERSION:
            raise ValueError(f"runtime config version must be {CONFIG_VERSION}")
        default = cls.defaults(default_root)
        for flag in ("outlook_new_messages_only", "mail_recruiting_only"):
            if flag in value and not isinstance(value[flag], bool):
                raise ValueError(flag + " must be a boolean")
        if value.get("resume_mode", "tailored") not in {"standard", "tailored"}:
            raise ValueError("resume_mode must be standard or tailored")
        root_value = value.get("project_root", default.project_root)
        root = Path(root_value).expanduser().resolve()
        folders = value.get("outlook_mail_folders", default.outlook_mail_folders)
        if not isinstance(folders, (list, tuple)) or not folders:
            raise ValueError("outlook_mail_folders must be a nonempty array")
        normalized_folders = tuple(
            dict.fromkeys(_text(item, "outlook_mail_folders", 128) for item in folders)
        )
        policy = _text(
            value.get("shortlist_policy", default.shortlist_policy), "shortlist_policy"
        )
        if policy not in {"champion", "selective", "broad", "compare"}:
            raise ValueError("shortlist_policy is invalid")
        salary = value.get("shortlist_salary_floor", default.shortlist_salary_floor)
        if salary is not None:
            if isinstance(salary, bool):
                raise ValueError("shortlist_salary_floor must be a number")
            salary = float(salary)
            if not 0 <= salary <= 10_000_000:
                raise ValueError("shortlist_salary_floor is out of range")
        remote = value.get("shortlist_remote_only", default.shortlist_remote_only)
        if not isinstance(remote, bool):
            raise ValueError("shortlist_remote_only must be a boolean")
        notify = value.get(
            "shortlist_notifications_enabled",
            default.shortlist_notifications_enabled,
        )
        if not isinstance(notify, bool):
            raise ValueError("shortlist_notifications_enabled must be a boolean")
        remote_mail_inference = value.get(
            "remote_mail_inference_enabled",
            default.remote_mail_inference_enabled,
        )
        if not isinstance(remote_mail_inference, bool):
            raise ValueError("remote_mail_inference_enabled must be a boolean")

        def bounded_int(
            name: str, default_value: int, minimum: int, maximum: int
        ) -> int:
            raw = value.get(name, default_value)
            if isinstance(raw, bool):
                raise ValueError(f"{name} must be an integer")
            result = int(raw)
            if not minimum <= result <= maximum:
                raise ValueError(f"{name} is out of range")
            return result

        limit = bounded_int("shortlist_limit", default.shortlist_limit, 1, 100)
        result = cls(
            version=CONFIG_VERSION,
            project_root=root,
            application_db=_path(
                value.get("application_db", "job-search.db"), "application_db", root
            ),
            jobs_db=_path(value.get("jobs_db", "job-boards.db"), "jobs_db", root),
            preference_db=_path(
                value.get("preference_db", "job-boards-preference.db"),
                "preference_db",
                root,
            ),
            proxy_db=_path(
                value.get("proxy_db", "job-boards-proxy.db"), "proxy_db", root
            ),
            resume_lab_db=_path(
                value.get("resume_lab_db"), "resume_lab_db", root, optional=True
            ),
            resume_artifact_root=_path(
                value.get("resume_artifact_root"),
                "resume_artifact_root",
                root,
                optional=True,
            ),
            resume_mode=value.get("resume_mode", "tailored"),
            resume_model_config=_path(
                value.get("resume_model_config"),
                "resume_model_config",
                root,
                optional=True,
            ),
            resume_tectonic_executable=_path(
                value.get("resume_tectonic_executable"),
                "resume_tectonic_executable",
                root,
                optional=True,
            ),
            resume_tectonic_bundle=_path(
                value.get("resume_tectonic_bundle"),
                "resume_tectonic_bundle",
                root,
                optional=True,
            ),
            resume_tectonic_version=_text(
                value.get("resume_tectonic_version", default.resume_tectonic_version),
                "resume_tectonic_version",
                200,
            ),
            tool_service_socket=_path(
                value.get("tool_service_socket"),
                "tool_service_socket",
                root,
                optional=True,
            ),
            timezone=_text(value.get("timezone", default.timezone), "timezone", 100),
            dashboard_port=_port(
                value.get("dashboard_port", default.dashboard_port), "dashboard_port"
            ),
            dashboard_https_origin=_text(
                value.get("dashboard_https_origin", ""), "dashboard_https_origin", 300
            ),
            dashboard_allowed_tailscale_login=_text(
                value.get("dashboard_allowed_tailscale_login", ""),
                "dashboard_allowed_tailscale_login", 320,
            ),
            mcp_port=_port(value.get("mcp_port", default.mcp_port), "mcp_port"),
            mcp_token_file=_path(
                value.get("mcp_token_file", default.mcp_token_file),
                "mcp_token_file",
                root,
            ),
            log_dir=_path(value.get("log_dir", default.log_dir), "log_dir", root),
            autofill_profile=_path(
                value.get("autofill_profile"), "autofill_profile", root, optional=True
            ),
            autofill_vault=_path(
                value.get("autofill_vault"), "autofill_vault", root, optional=True
            ),
            outlook_client_id=_text(
                value.get("outlook_client_id", default.outlook_client_id),
                "outlook_client_id",
                128,
            ),
            outlook_account_id=_text(
                value.get("outlook_account_id", default.outlook_account_id),
                "outlook_account_id",
                128,
            ),
            outlook_mail_folders=normalized_folders,
            outlook_poll_interval_minutes=value.get("outlook_poll_interval_minutes", 5),
            scraper_contact=_text(
                value.get("scraper_contact", default.scraper_contact),
                "scraper_contact",
                320,
            ),
            board_registry_path=_path(value.get("board_registry_path"), "board_registry_path", root, optional=True),
            mail_classifier_config=_path(
                value.get("mail_classifier_config"),
                "mail_classifier_config",
                root,
                optional=True,
            ),
            inference_config=_path(
                value.get("inference_config"),
                "inference_config",
                root,
                optional=True,
            ),
            mail_inference_config=_path(
                value.get("mail_inference_config"), "mail_inference_config", root,
                optional=True,
            ),
            mail_inference_profile=value.get("mail_inference_profile"),
            remote_mail_inference_enabled=remote_mail_inference,
            remote_mail_temporal_enabled=value.get("remote_mail_temporal_enabled", True),
            outlook_new_messages_only=value.get("outlook_new_messages_only", False),
            mail_recruiting_only=value.get("mail_recruiting_only", False),
            inference_usage_limits=value.get("inference_usage_limits", {}),
            portable_encryption_key_file=_path(
                value.get("portable_encryption_key_file"),
                "portable_encryption_key_file",
                root,
                optional=True,
            ),
            shortlist_limit=limit,
            shortlist_policy=policy,
            shortlist_days=bounded_int(
                "shortlist_days", default.shortlist_days, 1, 3650
            ),
            shortlist_salary_floor=salary,
            shortlist_remote_only=remote,
            shortlist_max_per_company=bounded_int(
                "shortlist_max_per_company",
                min(default.shortlist_max_per_company, limit),
                1,
                limit,
            ),
            shortlist_max_per_title=bounded_int(
                "shortlist_max_per_title",
                min(default.shortlist_max_per_title, limit),
                1,
                limit,
            ),
            shortlist_notifications_enabled=notify,
            shortlist_notification_start_at=value.get("shortlist_notification_start_at"),
            shortlist_notification_min_new_jobs=bounded_int(
                "shortlist_notification_min_new_jobs",
                default.shortlist_notification_min_new_jobs,
                1,
                limit,
            ),
            shortlist_notification_cooldown_minutes=bounded_int(
                "shortlist_notification_cooldown_minutes",
                default.shortlist_notification_cooldown_minutes,
                0,
                7 * 24 * 60,
            ),
            hermes_container=_text(
                value.get("hermes_container", default.hermes_container),
                "hermes_container",
                256,
            ),
            hermes_image=_text(
                value.get("hermes_image", default.hermes_image), "hermes_image", 500
            ),
            hermes_data_dir=_path(
                value.get("hermes_data_dir", default.hermes_data_dir),
                "hermes_data_dir",
                root,
                optional=True,
            ),
            hermes_workspace_dir=_path(
                value.get("hermes_workspace_dir", default.hermes_workspace_dir),
                "hermes_workspace_dir",
                root,
                optional=True,
            ),
            hermes_executable=_path(
                value.get("hermes_executable", default.hermes_executable),
                "hermes_executable",
                root,
                optional=True,
            ),
            hermes_notification_socket=_path(
                value.get(
                    "hermes_notification_socket",
                    default.hermes_notification_socket,
                ),
                "hermes_notification_socket",
                root,
                optional=True,
            ),
            hermes_telegram_target=_text(
                value.get("hermes_telegram_target", default.hermes_telegram_target),
                "hermes_telegram_target",
                256,
            ),
            source_path=source_path.resolve() if source_path else None,
        )
        result.validate()
        return result

    def validate(self) -> None:
        if not isinstance(self.remote_mail_temporal_enabled, bool):
            raise ValueError("remote_mail_temporal_enabled must be a boolean")
        if type(self.outlook_poll_interval_minutes) is not int or not 1 <= self.outlook_poll_interval_minutes <= 1440:
            raise ValueError("outlook_poll_interval_minutes must be an integer from 1 to 1440")
        if self.mail_inference_profile is not None:
            if self.mail_inference_config is None:
                raise ValueError("mail_inference_profile requires mail_inference_config")
            profile = self.mail_inference_profile
            expected = {"version", "profile_id", "structured_generation", "embeddings"}
            generation_fields = {"kind", "model", "credential_file", "timeout_seconds",
                                 "max_response_bytes", "max_input_tokens", "default_max_output_tokens"}
            if (not isinstance(profile, Mapping) or set(profile) != expected
                    or profile["version"] != 1 or profile["embeddings"] is not None
                    or not isinstance(profile["structured_generation"], Mapping)
                    or set(profile["structured_generation"]) != generation_fields
                    or profile["structured_generation"]["kind"] != "openrouter"):
                raise ValueError("mail_inference_profile must be a generation-only OpenRouter profile")
            # This is host projection metadata. Do not load its credential here:
            # dashboard, model, and MCP deliberately never receive the mail key.
            # Core validates the complete materialized profile before inference.
        if self.shortlist_notification_start_at is not None:
            from .contracts import parse_utc
            parse_utc(self.shortlist_notification_start_at)
        from .inference.usage import UsagePolicy
        UsagePolicy.from_mapping(self.inference_usage_limits)
        DashboardAccess(self.dashboard_https_origin, self.dashboard_allowed_tailscale_login)
        if self.version != CONFIG_VERSION:
            raise ValueError(f"runtime config version must be {CONFIG_VERSION}")
        if self.timezone != SCHEDULE_TIMEZONE:
            raise ValueError(f"runtime timezone must be {SCHEDULE_TIMEZONE}")
        if self.dashboard_port == self.mcp_port:
            raise ValueError("dashboard_port and mcp_port must differ")
        databases = {
            self.application_db,
            self.jobs_db,
            self.preference_db,
            self.proxy_db,
        }
        if self.resume_lab_db is not None:
            databases.add(self.resume_lab_db)
        expected_databases = 5 if self.resume_lab_db is not None else 4
        if len(databases) != expected_databases:
            raise ValueError("all runtime database paths must be distinct")
        if (self.resume_lab_db is None) != (self.resume_artifact_root is None):
            raise ValueError(
                "resume_lab_db and resume_artifact_root must be configured together"
            )
        tectonic_paths = (
            self.resume_tectonic_executable,
            self.resume_tectonic_bundle,
        )
        if any(value is not None for value in tectonic_paths) and not all(
            value is not None for value in tectonic_paths
        ):
            raise ValueError(
                "resume_tectonic_executable and resume_tectonic_bundle must be configured together"
            )
        local_toolchain = all(value is not None for value in tectonic_paths)
        remote_toolchain = self.tool_service_socket is not None
        if local_toolchain and remote_toolchain:
            raise ValueError(
                "local resume toolchain paths and tool_service_socket are mutually exclusive"
            )
        if (local_toolchain or remote_toolchain) != bool(self.resume_tectonic_version):
            raise ValueError(
                "resume_tectonic_version is required exactly when a resume toolchain is configured"
            )
        if self.outlook_client_id and not re.fullmatch(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
            self.outlook_client_id,
        ):
            raise ValueError("outlook_client_id must be a GUID")
        if self.hermes_container and not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]*", self.hermes_container
        ):
            raise ValueError("hermes_container is invalid")
        if (
            self.hermes_executable is not None
            and not self.hermes_executable.is_absolute()
        ):
            raise ValueError("hermes_executable must be an absolute path")
        if self.hermes_executable and self.hermes_notification_socket:
            raise ValueError(
                "hermes_executable and hermes_notification_socket are mutually exclusive"
            )
        if self.hermes_telegram_target and not (
            self.hermes_executable or self.hermes_notification_socket
        ):
            raise ValueError(
                "hermes_executable is required unless hermes_notification_socket is configured when delivery is enabled"
            )

    def environment(
        self, base: Optional[Mapping[str, str]] = None
    ) -> Mapping[str, str]:
        environment = dict(base if base is not None else os.environ)
        configured = {
            "JOB_BOARDS_CACHE": str(self.board_registry_path) if self.board_registry_path else "",
            "JOB_SCRAPER_CONTACT": self.scraper_contact,
            "OUTLOOK_CLIENT_ID": self.outlook_client_id,
            "OUTLOOK_ACCOUNT_ID": self.outlook_account_id,
            "JOB_SEARCH_OUTLOOK_POLL_INTERVAL_MINUTES": str(self.outlook_poll_interval_minutes),
            "JOB_SEARCH_MAIL_CLASSIFIER_CONFIG": (
                str(self.mail_classifier_config) if self.mail_classifier_config else ""
            ),
            "JOB_SEARCH_INFERENCE_CONFIG": (
                str(self.inference_config) if self.inference_config else ""
            ),
            "JOB_SEARCH_NOTIFICATION_TARGET": self.hermes_telegram_target,
        }
        for name, value in configured.items():
            if value:
                if name in {
                    "JOB_BOARDS_CACHE",
                    "JOB_SEARCH_MAIL_CLASSIFIER_CONFIG",
                    "JOB_SEARCH_INFERENCE_CONFIG",
                    "JOB_SEARCH_OUTLOOK_POLL_INTERVAL_MINUTES",
                }:
                    # An owner-only runtime file or explicit CLI override is more
                    # specific than an inherited shell profile.
                    environment[name] = value
                else:
                    environment.setdefault(name, value)
        return environment

    def shortlist_defaults(self) -> Mapping[str, Any]:
        return {
            "limit": self.shortlist_limit,
            "days": self.shortlist_days,
            "salary_floor": self.shortlist_salary_floor,
            "remote_only": self.shortlist_remote_only,
            "max_per_company": self.shortlist_max_per_company,
            "max_per_title": self.shortlist_max_per_title,
            "policy": self.shortlist_policy,
        }

    def notification_defaults(self) -> Mapping[str, Any]:
        return {
            "enabled": self.shortlist_notifications_enabled,
            "min_new_jobs": self.shortlist_notification_min_new_jobs,
            "cooldown_minutes": self.shortlist_notification_cooldown_minutes,
        }

    def public_mapping(self) -> Mapping[str, Any]:
        result = asdict(self)
        portable_encryption_configured = (
            self.portable_encryption_key_file is not None
        )
        result.pop("portable_encryption_key_file", None)
        result.pop("mail_inference_profile", None)
        for name, value in tuple(result.items()):
            if isinstance(value, Path):
                result[name] = str(value)
            elif isinstance(value, tuple):
                result[name] = list(value)
        result.pop("source_path", None)
        result["portable_encryption_configured"] = portable_encryption_configured
        return result


def load_runtime_config(
    path: Optional[Path] = None,
    *,
    required: bool = False,
    default_root: Optional[Path] = None,
) -> RuntimeConfigV1:
    target = (path or DEFAULT_CONFIG_PATH).expanduser()
    if not target.exists():
        if required:
            raise ValueError(f"runtime config does not exist: {target}")
        return RuntimeConfigV1.defaults(default_root)
    if os.stat(target).st_mode & 0o077:
        raise ValueError("runtime config must be owner-only (mode 0600)")
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("runtime config must be valid UTF-8 JSON") from exc
    if not isinstance(value, Mapping):
        raise ValueError("runtime config must be a JSON object")
    return RuntimeConfigV1.from_mapping(
        value, source_path=target, default_root=default_root
    )


def override_runtime_config(config: RuntimeConfigV1, **values: Any) -> RuntimeConfigV1:
    changes = {name: value for name, value in values.items() if value is not None}
    path_names = {
        "board_registry_path",
        "project_root",
        "application_db",
        "jobs_db",
        "preference_db",
        "proxy_db",
        "log_dir",
        "mcp_token_file",
        "mail_classifier_config",
        "mail_inference_config",
        "inference_config",
        "portable_encryption_key_file",
        "autofill_profile",
        "autofill_vault",
        "hermes_executable",
        "hermes_notification_socket",
        "resume_lab_db",
        "resume_artifact_root",
        "resume_model_config",
        "resume_tectonic_executable",
        "resume_tectonic_bundle",
        "tool_service_socket",
    }
    root = Path(changes.get("project_root", config.project_root)).expanduser().resolve()
    changes["project_root"] = root
    for name in path_names - {"project_root"}:
        if name in changes:
            changes[name] = _path(
                changes[name],
                name,
                root,
                optional=name
                in {
                    "board_registry_path",
                    "mail_classifier_config",
                    "mail_inference_config",
                    "inference_config",
                    "portable_encryption_key_file",
                    "autofill_profile",
                    "autofill_vault",
                    "hermes_executable",
                    "hermes_notification_socket",
                    "resume_lab_db",
                    "resume_artifact_root",
                    "resume_model_config",
                    "resume_tectonic_executable",
                    "resume_tectonic_bundle",
                    "tool_service_socket",
                },
            )
    updated = replace(config, **changes)
    updated.validate()
    return updated


@dataclass(frozen=True)
class Runtime:
    config: RuntimeConfigV1
    lane: str
    worker: Worker
    schedules: Mapping[str, Any]

    def tick(self, now: Optional[datetime] = None, *, should_stop: Callable[[], bool] = lambda: False) -> Mapping[str, Any]:
        if should_stop():
            return {"acquired": False, "reason": "worker draining"}
        if self.lane == "core":
            from .dependency_health import dependency_health
            from .dependency_snapshot import publish_snapshot
            publish_snapshot(self.config.application_db, dependency_health(self.config))
        return self.worker.tick(now=now, should_stop=should_stop)


def _configured_mail_models(
    config: RuntimeConfigV1,
    environment: Mapping[str, str],
) -> tuple[Any | None, Any | None, Any | None, str]:
    """Select local mail inference before considering explicitly allowed egress."""

    configured_classifier = str(
        config.mail_classifier_config
        or environment.get("JOB_SEARCH_MAIL_CLASSIFIER_CONFIG")
        or ""
    ).strip()
    if configured_classifier:
        from .mail.model import load_classifier_config

        classifier_config = load_classifier_config(Path(configured_classifier))
        return (
            classifier_config.build(),
            classifier_config,
            None,
            classifier_config.producer_version,
        )
    if not config.remote_mail_inference_enabled:
        return None, None, None, "local-mail-model-v1"
    from .inference import build_structured_provider
    from .mail.remote import RemoteMailClassifier, RemoteTemporalExtractor

    inference = _configured_remote_mail_profile(config, environment)
    provider = build_structured_provider(inference)
    classifier = RemoteMailClassifier(provider)
    temporal = RemoteTemporalExtractor(provider) if config.remote_mail_temporal_enabled else None
    return classifier, None, temporal, classifier.producer_version


def _configured_remote_mail_profile(config: RuntimeConfigV1, environment: Mapping[str, str]):
    """Resolve mail independently; an invalid explicit profile never falls back."""
    configured_inference = str(
        config.mail_inference_config
        or config.inference_config
        or environment.get("JOB_SEARCH_INFERENCE_CONFIG")
        or ""
    ).strip()
    if not configured_inference:
        raise ValueError(
            "remote_mail_inference_enabled requires an inference configuration"
        )
    from .inference import load_inference_config
    from .mail.remote import CLASSIFIER_MAX_OUTPUT_TOKENS, TEMPORAL_MAX_OUTPUT_TOKENS

    inference = load_inference_config(Path(configured_inference))
    if inference.structured_generation is None:
        raise ValueError(
            "remote mail inference requires structured_generation configuration"
        )
    required_output = TEMPORAL_MAX_OUTPUT_TOKENS if config.remote_mail_temporal_enabled else CLASSIFIER_MAX_OUTPUT_TOKENS
    if inference.structured_generation.default_max_output_tokens < required_output:
        raise ValueError(f"remote mail requires at least {required_output} configured output tokens")
    if inference.structured_generation.model == "numind/NuExtract3":
        raise ValueError("NuExtract3 is incompatible with the mail JSON adapter; configure a separate mail inference profile")
    return inference


def _build_outlook_handlers(
    config: RuntimeConfigV1, environment: Mapping[str, str]
) -> Mapping[str, Callable[[Mapping[str, Any], TaskContext], Mapping[str, Any]]]:
    if not has_outlook_config(environment):
        return {}
    client_id = str(environment["OUTLOOK_CLIENT_ID"])
    account_id = str(environment.get("OUTLOOK_ACCOUNT_ID") or config.outlook_account_id)
    folder_override = str(environment.get("OUTLOOK_MAIL_FOLDER") or "").strip()
    folder_refs = (folder_override,) if folder_override else config.outlook_mail_folders
    try:
        from .actions import ActionExecutor
        from .availability import AvailabilityPlanner
        from .mail.runtime import build_secure_mail_ingestor
        from .outlook.auth import MsalTokenProvider, default_cache_path
        from .outlook.client import GraphOutlookClient
        from .outlook.mail import GraphMailClient
        from .outlook.state import SQLiteOutlookState
        from .outlook.transport import GraphSession, UrllibHttpAdapter
        from .service import JobSearchLedger
        from .sync import OutlookMailCoordinator

        cache_value = str(environment.get("OUTLOOK_TOKEN_CACHE") or "").strip()
        cache_path = (
            Path(cache_value).expanduser() if cache_value else default_cache_path()
        )
        persistence = None
        archive_key_provider = None
        if config.portable_encryption_key_file is not None:
            from .secure_persistence import (
                EncryptedFilePersistence,
                PortableArchiveKeyProvider,
            )

            persistence = EncryptedFilePersistence(
                cache_path.resolve(),
                config.portable_encryption_key_file,
                "outlook-token-cache",
            )
            archive_key_provider = PortableArchiveKeyProvider(
                config.portable_encryption_key_file
            )
        provider = MsalTokenProvider(
            client_id,
            cache_path,
            account_home_id=(
                str(environment.get("OUTLOOK_HOME_ACCOUNT_ID") or "").strip() or None
            ),
            persistence=persistence,
        )
        session = GraphSession(provider, UrllibHttpAdapter())
        service = JobSearchLedger(config.application_db)
        state = SQLiteOutlookState(config.application_db)
        outlook = GraphOutlookClient(session)
        try:
            (
                classifier,
                classifier_config,
                temporal_extractor,
                model_version,
            ) = _configured_mail_models(config, environment)
            mail = GraphMailClient(session)
            attachment_extractor = None
            if config.tool_service_socket is not None:
                from .tool_service import RemoteAttachmentExtractor

                attachment_extractor = RemoteAttachmentExtractor(
                    config.tool_service_socket
                )
            secure_ingestor = build_secure_mail_ingestor(
                service,
                mail,
                classifier_config=classifier_config,
                key_provider=archive_key_provider,
                attachment_extractor=attachment_extractor,
                temporal_extractor=temporal_extractor,
                temporal_producer_version=model_version,
            )
            from .activation import mail_start
            start = mail_start(config.application_db, account_id) if config.outlook_new_messages_only else None
            coordinator = OutlookMailCoordinator(
                mail,
                state,
                service,
                classifier=classifier,
                model_version=model_version,
                secure_ingestor=secure_ingestor,
                received_since=start,
                recruiting_only=config.mail_recruiting_only,
            )
            mail_handler: Callable[
                [Mapping[str, Any], TaskContext], Mapping[str, Any]
            ] = OutlookMailTaskHandler(
                coordinator,
                account_id=account_id,
                folder_refs=folder_refs,
                all_history=True,
                max_pages_per_folder=5 if config.outlook_new_messages_only else 100,
                activation_start=(lambda: mail_start(config.application_db, account_id)) if config.outlook_new_messages_only else None,
            )
        except Exception as exc:
            safe_classifier_error = _safe_error(exc)
            for folder_ref in folder_refs:
                state.set_health(
                    f"outlook:{account_id}:{folder_ref}",
                    "failed",
                    safe_classifier_error,
                )
            mail_handler = _unavailable_outlook_handler(safe_classifier_error)
        return {
            "outlook.mail.sync": mail_handler,
            "outlook.actions.execute": ApprovedActionTaskHandler(
                service,
                ActionExecutor(
                    service,
                    outlook,
                    AvailabilityPlanner(outlook),
                    account_id=account_id,
                ),
            ),
        }
    except Exception as exc:
        safe = _safe_error(exc)
        from .outlook.state import SQLiteOutlookState

        state = SQLiteOutlookState(config.application_db)
        for folder_ref in folder_refs:
            state.set_health(f"outlook:{account_id}:{folder_ref}", "failed", safe)
        unavailable = _unavailable_outlook_handler(safe)
        return {
            "outlook.mail.sync": unavailable,
            "outlook.actions.execute": unavailable,
        }


def build_local_reminder_handler(
    config: RuntimeConfigV1,
    *,
    notification_target: Optional[str] = None,
    now_provider: Optional[Callable[[], datetime]] = None,
) -> DueLocalReminderTaskHandler:
    """Assemble due-reminder publishing without Graph or Hermes credentials."""

    from .notifications import DurableNotificationPublisher, NotificationPolicy
    from .service import JobSearchLedger

    service = JobSearchLedger(config.application_db)
    default_policy = NotificationPolicy()
    target = (
        config.hermes_telegram_target
        if notification_target is None
        else notification_target
    )
    policy = NotificationPolicy(
        policy_id=default_policy.policy_id,
        enabled_topics=(default_policy.enabled_topics if target else frozenset()),
        max_attempts=default_policy.max_attempts,
    )
    return DueLocalReminderTaskHandler(
        service,
        DurableNotificationPublisher(service, policy, now=now_provider),
        now_provider=now_provider,
    )


def build_runtime(
    config: RuntimeConfigV1,
    *,
    lane: str = "core",
    now_provider: Optional[Callable[[], datetime]] = None,
    task_overrides: Optional[
        Mapping[str, Callable[[Mapping[str, Any], TaskContext], Mapping[str, Any]]]
    ] = None,
    outbox_overrides: Optional[
        Mapping[str, Callable[[Mapping[str, Any], OutboxContext], Mapping[str, Any]]]
    ] = None,
    command_runner: Optional[Callable[..., subprocess.CompletedProcess[str]]] = None,
    salary_status_provider: Optional[Callable[[Path], Mapping[str, Any]]] = None,
    base_environment: Optional[Mapping[str, str]] = None,
    max_work_per_tick: int = 10,
    max_outbox_per_tick: int = 20,
) -> Runtime:
    config.validate()
    if lane not in {"core", "model"}:
        raise ValueError("lane must be core or model")
    clock = now_provider or (lambda: datetime.now(timezone.utc))
    now = clock()
    prepare_database(config.application_db, utc_stamp(now))
    environment = config.environment(base_environment)
    target = str(environment.get("JOB_SEARCH_NOTIFICATION_TARGET") or "").strip()
    if target and not (
        config.hermes_executable or config.hermes_notification_socket
    ):
        raise ValueError(
            "hermes_executable is required unless hermes_notification_socket is configured when delivery is enabled"
        )
    schedules = seed_default_schedules(config.application_db, now, environment)

    def environment_provider() -> Mapping[str, str]:
        return environment

    dag, opportunity_handlers = build_opportunity_handlers(
        project_root=config.project_root,
        jobs_db=config.jobs_db,
        preference_db=config.preference_db,
        proxy_db=config.proxy_db,
        policy_refresh=config.shortlist_policy in {"selective", "broad", "compare"},
        environment_provider=environment_provider,
        runner=command_runner,
        salary_status_provider=salary_status_provider,
    )
    handlers: dict[str, Any] = {
        "ats.authoritative": ATSCommandHandler(
            "authoritative",
            project_root=config.project_root,
            jobs_db=config.jobs_db,
            board_registry_path=config.board_registry_path,
            environment_provider=environment_provider,
            runner=command_runner,
            follow_up_factory=dag.after_ats,
        ),
        "ats.new_only": ATSCommandHandler(
            "new_only",
            project_root=config.project_root,
            jobs_db=config.jobs_db,
            board_registry_path=config.board_registry_path,
            environment_provider=environment_provider,
            runner=command_runner,
            follow_up_factory=dag.after_ats,
        ),
        "ats.refresh_recent": ATSCommandHandler(
            "refresh_recent",
            project_root=config.project_root,
            jobs_db=config.jobs_db,
            board_registry_path=config.board_registry_path,
            environment_provider=environment_provider,
            runner=command_runner,
        ),
        **opportunity_handlers,
    }
    if lane == "core":
        reminder_handler = build_local_reminder_handler(
            config, notification_target=target, now_provider=clock
        )
        handlers.update(
            {
                "system.worker_tick": reminder_handler,
                "outlook.reminders.publish": reminder_handler,
            }
        )
        handlers.update(_build_outlook_handlers(config, environment))
    gateway = PreferenceGateway(
        PreferencePaths(config.jobs_db, config.preference_db, config.proxy_db)
    )
    from .integration import (
        ApplicationEventNotificationHandler,
        ReminderNotificationHandler,
        ShortlistNotificationEvaluator,
    )
    from .notifications import (
        DurableNotificationPublisher,
        HermesSendClient,
        NotificationOutboxHandler,
        NotificationPolicy,
        RemoteHermesSendClient,
    )
    from .service import JobSearchLedger

    ledger = JobSearchLedger(config.application_db)
    default_policy = NotificationPolicy()
    policy = NotificationPolicy(
        policy_id=default_policy.policy_id,
        enabled_topics=default_policy.enabled_topics if target else frozenset(),
        max_attempts=default_policy.max_attempts,
    )
    publisher = DurableNotificationPublisher(ledger, policy, now=clock)
    if lane == "core":
        handlers.update(
            {
                NOTIFICATION_TASK: ShortlistNotificationEvaluator(
                    gateway,
                    ledger,
                    publisher,
                    options=config.shortlist_defaults(),
                    enabled=config.shortlist_notifications_enabled and bool(target),
                    minimum_jobs=config.shortlist_notification_min_new_jobs,
                    cooldown_minutes=config.shortlist_notification_cooldown_minutes,
                    first_seen_since=config.shortlist_notification_start_at,
                    now=clock,
                ),
                "notification.deliver": (
                    NotificationOutboxHandler(
                        ledger,
                        (
                            RemoteHermesSendClient(
                                config.hermes_notification_socket,
                                target=target,
                            )
                            if config.hermes_notification_socket is not None
                            else HermesSendClient(
                                executable=config.hermes_executable,
                                target=target,
                            )
                        ),
                        now=clock,
                    ).handle_task
                    if target
                    else lambda _payload, _context: {
                        "disabled": True,
                        "reason": "notification target is not configured",
                    }
                ),
                "notification.reminders_due": ReminderNotificationHandler(
                    ledger, publisher, now=clock
                ),
            }
        )
    if lane == "model" and config.resume_lab_db is not None:
        from .resume_lab.gateway import (
            RESUME_OPTIMIZE_TASK,
            build_resume_lab_gateway,
        )

        resume_lab = build_resume_lab_gateway(config)
        # Career imports and approved-wording composition do not require a local
        # language model. Each task checks its own document/model dependencies.
        if resume_lab is not None:
            handlers[RESUME_OPTIMIZE_TASK] = resume_lab.handle_work
    handlers.update(task_overrides or {})
    outbox_handlers = {
        "recommendation.applied": preference_outbox_handler(gateway),
        "notification.application_event": ApplicationEventNotificationHandler(
            ledger, publisher
        ),
        **dict(outbox_overrides or {}),
    }
    deferred = () if NOTIFICATION_TASK in handlers else (NOTIFICATION_TASK,)
    worker = Worker(
        config.application_db,
        task_handlers=handlers,
        outbox_handlers=outbox_handlers,
        now_provider=clock,
        lane=lane,
        deferred_task_kinds=deferred,
        max_work_per_tick=max_work_per_tick,
        max_outbox_per_tick=max_outbox_per_tick,
        inference_usage_limits=config.inference_usage_limits,
    )
    return Runtime(config, lane, worker, schedules)
