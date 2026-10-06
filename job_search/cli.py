#!/usr/bin/env python3
"""Operator CLI for the deterministic local job-search core."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import stat
import sys
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from job_search.contracts import ContractError, MutationContext, utc_now
from job_search.db import prepare_database
from job_search.runtime import (
    DEFAULT_CONFIG_PATH,
    RuntimeConfigV1,
    load_runtime_config,
    override_runtime_config,
)
from job_search.scheduler import automation_health, seed_default_schedules
from job_search.service import JobSearchLedger


def _json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Operate the deterministic local job-search core"
    )
    parser.add_argument("--config", type=Path)
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--jobs-db", type=Path)
    parser.add_argument("--preference-db", type=Path)
    parser.add_argument("--proxy-db", type=Path)
    parser.add_argument("--resume-db", type=Path)
    parser.add_argument("--resume-artifact-root", type=Path)
    parser.add_argument("--resume-model-config", type=Path)
    parser.add_argument("--inference-config", type=Path)
    parser.add_argument("--portable-encryption-key-file", type=Path)
    parser.add_argument("--resume-tectonic-executable", type=Path)
    parser.add_argument("--resume-tectonic-bundle", type=Path)
    parser.add_argument("--resume-tectonic-version")
    parser.add_argument("--tool-service-socket", type=Path)
    parser.add_argument("--hermes-notification-socket", type=Path)
    parser.add_argument("--log-dir", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    mail_review = commands.add_parser('mail-review', help='inspect, preview, and resolve user-authorized email reviews')
    mail_review.add_argument('action', choices=('list', 'applications', 'message', 'preview', 'apply'))
    mail_review.add_argument('--input', default='-', help='decision JSON or saved preview JSON; - reads stdin')
    mail_review.add_argument('--proposal-id')
    mail_review.add_argument('--limit', type=int, default=50)
    mail_review.add_argument('--after', default='')
    mail_review.add_argument('--search', default='')
    mail_review.add_argument('--idempotency-key')
    understanding = commands.add_parser("mail-understanding", help="operate shared mail evaluation and archive-only history reanalysis")
    understanding_commands = understanding.add_subparsers(dest="understanding_command", required=True)
    history = understanding_commands.add_parser("history", help="reanalyse all linked inbound history without notifications")
    history.add_argument("action", choices=("preview", "start", "resume", "inspect", "cancel"))
    history.add_argument("--account-id")
    history.add_argument("--replay-id")
    history.add_argument("--limit", type=int, default=50)
    history.add_argument("--retry-failed", action="store_true")
    history.add_argument("--idempotency-key")
    evaluation = understanding_commands.add_parser("evaluate", help="score reviewed expected findings against recorded predictions")
    evaluation.add_argument("--input", type=Path, required=True)
    evaluation.add_argument("--output", type=Path, required=True)
    evaluation.add_argument("--producer-version", required=True)
    evaluation.add_argument("--dataset-kind", choices=("synthetic", "reviewed_private_holdout"), default="synthetic")
    from .job_reviews.runner import add_arguments as review_runner_arguments
    review_runner_arguments(commands.add_parser('review-runner', help='operate isolated Codex review sessions'))
    from .job_reviews.service import FIELDS
    review = commands.add_parser("review", help="operate an agent review through the private dashboard")
    review.add_argument("action", choices=tuple(FIELDS))
    review.add_argument("--dashboard-url", default=os.environ.get("CAREER_DASHBOARD_URL", ""))
    review.add_argument("--input", default="-", help="JSON argument file or stdin; credentials are never included")
    shortlist = commands.add_parser("shortlist", help="publish caller-selected jobs through the local agent API")
    shortlist.add_argument("action", choices=("publish",))
    shortlist.add_argument("--input", default="-", help="JSON file or - for stdin")
    setup = commands.add_parser("setup", help="inspect or initialize a private installation")
    setup.add_argument("action", choices=("inspect", "initialize"))
    setup.add_argument("--state-root", type=Path)
    automation = commands.add_parser("automation", help="inspect or explicitly enable background capabilities")
    automation.add_argument("action", choices=("list", "enable", "disable"))
    automation.add_argument("capability", nargs="?")
    automation.add_argument("--expected-revision", type=int, default=0)
    automation.add_argument("--idempotency-key")
    commands.add_parser(
        "init", help="create/migrate the private ledger and seed schedules"
    )
    commands.add_parser("readiness", help="inspect configuration and workflow progress without starting work")
    commands.add_parser("work-list", help="show bounded failed-work recovery options")
    retry = commands.add_parser("work-retry", help="retry a classified safe failure")
    retry.add_argument("work_id")
    retry.add_argument("--expected-revision", type=int, required=True)
    retry.add_argument("--idempotency-key", required=True)
    commands.add_parser("inference-usage", help="inspect platform inference reservations and limits")
    commands.add_parser("inference-recovery", help="inspect unresolved provider invocations")
    inference = commands.add_parser("inference-reconcile", help="record a checked remote invocation outcome")
    inference.add_argument("invocation_id")
    inference.add_argument("--expected-updated-at", required=True)
    inference.add_argument("--resolution", choices=("absent", "failed", "completed"), required=True)
    inference.add_argument("--provider-job-id", default="")
    inference.add_argument("--idempotency-key", required=True)
    commands.add_parser("notification-recovery", help="inspect uncertain notification delivery")
    delivery = commands.add_parser("notification-reconcile", help="record a checked notification delivery outcome")
    delivery.add_argument("notification_id")
    delivery.add_argument("--expected-attempts", type=int, required=True)
    delivery.add_argument("--expected-payload-sha256", required=True)
    delivery.add_argument("--outcome", choices=("delivered", "not_delivered", "abandoned"), required=True)
    delivery.add_argument("--idempotency-key", required=True)
    commands.add_parser(
        "status", help="show ledger, connector, queue, and schedule health"
    )
    commands.add_parser(
        "verify", help="verify every rebuildable application projection"
    )
    rebuild = commands.add_parser("rebuild", help="preview or apply projection repairs")
    rebuild.add_argument(
        "--apply",
        action="store_true",
        help="apply repairs; without this flag the command is read-only",
    )
    auth = commands.add_parser(
        "outlook-auth", help="interactively seed the encrypted Outlook token cache"
    )
    auth.add_argument("--client-id", default="")
    auth.add_argument("--cache", type=Path)
    auth.add_argument(
        "--enable-drafts",
        action="store_true",
        help="also request Mail.ReadWrite for unsent reply drafts",
    )
    auth.add_argument(
        "--enable-holds",
        action="store_true",
        help="also request Calendars.ReadWrite for personal calendar appointments and holds",
    )
    auth.add_argument("--enable-send", "--send", action="store_true", help="request Mail.Send for explicitly approved recruiter replies")
    auth.add_argument(
        "--device-code",
        action="store_true",
        help="authenticate from a headless host using a code entered on another device",
    )
    disconnect = commands.add_parser(
        "outlook-disconnect",
        help="remove cached Outlook accounts from the encrypted cache",
    )
    disconnect.add_argument("--client-id", default="")
    disconnect.add_argument("--cache", type=Path)
    service = commands.add_parser(
        "service", help="plan, install, inspect, or uninstall launchd runtime services"
    )
    service.add_argument("action", choices=("install", "status", "uninstall"))
    service.add_argument(
        "--apply",
        action="store_true",
        help="perform install/uninstall; without it the command is a dry-run plan",
    )
    service.add_argument("--output-dir", type=Path)
    commands.add_parser(
        "mcp-token-init",
        help="create the owner-only bearer token used by the loopback Hermes MCP server",
    )
    commands.add_parser("interaction-token-init", help="create the separate owner-only Telegram interaction bearer")
    commands.add_parser(
        "encryption-key-init",
        help="create the owner-only master key used by portable encrypted state",
    )
    portable_export = commands.add_parser(
        "portable-state-export",
        help="create no-clobber portable copies of Keychain-backed private state",
    )
    portable_export.add_argument(
        "--destination-db",
        type=Path,
        required=True,
        help="new SQLite copy whose encrypted mail archive uses the portable key",
    )
    portable_export.add_argument(
        "--source-archive-key-file",
        type=Path,
        help="non-default macOS Keychain persistence location for the archive key",
    )
    portable_export.add_argument(
        "--source-autofill-vault",
        type=Path,
        help="override the source Keychain-backed autofill path from runtime config",
    )
    portable_export.add_argument(
        "--destination-autofill-vault",
        type=Path,
        help="new portable autofill ciphertext; required when a source vault is set",
    )
    resume = commands.add_parser(
        "resume", help="inspect setup and manage hand-written TeX resume standards"
    )
    resume_commands = resume.add_subparsers(dest="resume_command", required=True)
    resume_commands.add_parser(
        "doctor", help="show redacted local resume-lab readiness"
    )
    resume_commands.add_parser("list", help="list active hand-written resume standards")
    import_resume = resume_commands.add_parser(
        "import", help="compile, parse, and import a hand-written TeX resume"
    )
    import_resume.add_argument("--name", required=True)
    import_resume.add_argument("--rank", type=int, required=True)
    import_resume.add_argument("--tex", type=Path, required=True)
    existing = resume_commands.add_parser("import-existing", help="register an existing PDF and matching source without generation")
    existing.add_argument("--name", required=True)
    existing.add_argument("--rank", type=int, default=1)
    for field in ("pdf", "tex", "text", "content", "provenance"):
        existing.add_argument("--"+field, type=Path, required=field in {"pdf","tex","text"})
    update_resume = resume_commands.add_parser(
        "update", help="compile and activate a new version of an existing standard"
    )
    update_resume.add_argument("--standard-id", required=True)
    update_resume.add_argument("--tex", type=Path, required=True)
    for legacy_import in (import_resume, update_resume):
        legacy_import.add_argument("--allow-unmanaged-remote-inference", action="store_true",
            help="explicitly permit standalone remote normalization outside worker usage limits")
    rank_resume = resume_commands.add_parser(
        "rank", help="change the manual tie-break rank for a standard"
    )
    rank_resume.add_argument("--standard-id", required=True)
    rank_resume.add_argument("--rank", type=int, required=True)
    for action in ("archive", "activate"):
        standard = resume_commands.add_parser(
            action, help=f"{action} a hand-written resume standard"
        )
        standard.add_argument("--standard-id", required=True)
    career = resume_commands.add_parser("career", help="maintain your career facts and create tailored resumes")
    career_commands = career.add_subparsers(dest="career_command", required=True)
    career_commands.add_parser("show", help="show the draft and approved career profile")
    career_commands.add_parser("export", help="write structured career JSON to stdout")
    save = career_commands.add_parser("save", help="save a structured career JSON draft")
    save.add_argument("--file", type=Path, required=True)
    save.add_argument("--expected-revision-id")
    approve = career_commands.add_parser("approve", help="confirm a reviewed career revision")
    approve.add_argument("--revision-id", required=True)
    imported = career_commands.add_parser("import", help="queue PDF, DOCX, text, or career JSON extraction")
    imported.add_argument("--file", type=Path, required=True)
    seed = career_commands.add_parser("seed-standard", help="copy an imported resume into a career draft")
    seed.add_argument("--standard-version-id", required=True)
    status = career_commands.add_parser("import-status", help="inspect an import result")
    status.add_argument("--import-id", required=True)
    generate = career_commands.add_parser("generate", help="queue a job-specific one-page resume")
    generate.add_argument("--ats", required=True)
    generate.add_argument("--job-id", required=True)
    generate.add_argument("--application-id", required=True)
    generate.add_argument("--pin", action="append", default=[])
    generate.add_argument("--exclude", action="append", default=[])
    research = career_commands.add_parser("research", help="queue optional synthetic comparisons")
    research.add_argument("--run-id", required=True)
    for command in (save, approve, imported, seed, generate, research):
        command.add_argument("--idempotency-key")
    return parser


def _career_command(args: argparse.Namespace, gateway: Any, config: RuntimeConfigV1) -> Any:
    import uuid
    action = args.career_command
    key = getattr(args, "idempotency_key", None) or "career_cli_" + uuid.uuid4().hex
    if action == "show":
        return gateway.get_career_profile()
    if action == "export":
        return gateway.export_career_profile()
    if action in {"save", "import"}:
        with args.file.open("rb") as handle:
            data = handle.read(5 * 1024 * 1024 + 1)
        if len(data) > 5 * 1024 * 1024:
            raise ValueError("career document exceeds 5 MiB")
        if action == "save":
            content = json.loads(data)
            if isinstance(content, dict) and "content" in content:
                content = content["content"]
            return gateway.save_career_profile(content, expected_revision_id=args.expected_revision_id,
                idempotency_key=key)
        mime = {".pdf":"application/pdf", ".docx":"application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                ".txt":"text/plain", ".md":"text/plain", ".tex":"text/plain", ".json":"application/json"}.get(args.file.suffix.lower())
        if not mime:
            raise ValueError("import a PDF, DOCX, text, Markdown, or career JSON file")
        return gateway.import_career_document(data, filename=args.file.name, content_type=mime, idempotency_key=key)
    if action == "approve":
        return gateway.approve_career_profile(args.revision_id, idempotency_key=key)
    if action == "import-status":
        return gateway.get_career_import(args.import_id)
    if action == "seed-standard":
        return gateway.import_career_standard(args.standard_version_id, idempotency_key=key)
    if action == "research":
        return gateway.start_research_comparisons(args.run_id, idempotency_key=key)
    if action == "generate":
        from job_search.integration import LocalJobCatalog
        job = LocalJobCatalog(config.jobs_db).get_job(args.ats, args.job_id)
        return gateway.prepare_from_career_profile(job, application_id=args.application_id,
            pinned_fact_ids=args.pin, excluded_fact_ids=args.exclude, idempotency_key=key)
    raise ValueError("unknown career command")


def _read_resume_tex(path: Path) -> str:
    """Read one bounded regular UTF-8 source without following a symlink."""

    target = Path(path).expanduser()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags)
    except OSError as exc:
        raise ValueError("resume TeX source is unavailable") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 500_000:
            raise ValueError(
                "resume TeX source must be a regular file no larger than 500 KB"
            )
        chunks = []
        remaining = 500_001
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError as exc:
        raise ValueError("resume TeX source must be readable UTF-8") from exc
    finally:
        os.close(descriptor)
    encoded = b"".join(chunks)
    if len(encoded) > 500_000:
        raise ValueError("resume TeX source must be no larger than 500 KB")
    try:
        value = encoded.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("resume TeX source must be readable UTF-8") from exc
    if not value.strip() or "\x00" in value:
        raise ValueError("resume TeX source is empty or invalid")
    return value


def _outlook_provider(args: argparse.Namespace, config: RuntimeConfigV1) -> Any:
    from job_search.outlook.auth import MsalTokenProvider, default_cache_path

    client_id = str(
        args.client_id
        or config.outlook_client_id
        or os.environ.get("OUTLOOK_CLIENT_ID")
        or ""
    ).strip()
    if not client_id:
        raise SystemExit("set OUTLOOK_CLIENT_ID or pass --client-id")
    cache = args.cache.expanduser() if args.cache else default_cache_path()
    persistence = None
    if config.portable_encryption_key_file is not None:
        from job_search.secure_persistence import EncryptedFilePersistence

        persistence = EncryptedFilePersistence(
            cache.expanduser().resolve(),
            config.portable_encryption_key_file,
            "outlook-token-cache",
        )
    return MsalTokenProvider(
        client_id,
        cache,
        account_home_id=(
            str(os.environ.get("OUTLOOK_HOME_ACCOUNT_ID") or "").strip() or None
        ),
        persistence=persistence,
    )


from job_search.dependency_health import dependency_health as _dependency_health


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == 'review-runner':
        from .job_reviews.runner import command as review_runner_command
        try:
            result = review_runner_command(args)
            _json(result)
            return 2 if result.get('status') in ('failed', 'incomplete', 'needs_review', 'interrupted') else 0
        except (OSError, ContractError) as exc:
            raise SystemExit(str(exc)) from None
    if args.command == "review":
        from .job_reviews.client import ReviewClient
        try:
            if args.input == '-':
                raw = sys.stdin.buffer.read(65537)
            else:
                with Path(args.input).open('rb') as stream:
                    raw = stream.read(65537)
            if len(raw) > 65536:
                raise ContractError('review request exceeds 64 KiB')
            origin = args.dashboard_url
            if not origin and args.config:
                origin = load_runtime_config(args.config, required=True).dashboard_https_origin
            if not origin:
                raise ContractError('provide --dashboard-url or CAREER_DASHBOARD_URL')
            _json(ReviewClient(origin).call(args.action, json.loads(raw or b'{}')))
        except (ValueError, OSError, ContractError) as exc:
            raise SystemExit(str(exc)) from None
        return 0
    if args.command == "setup" and args.action == "initialize":
        from .setup import initialize, DEFAULT_STATE
        state = args.state_root or DEFAULT_STATE
        _json(initialize(args.config or state / "config.json", state, args.project_root or Path(__file__).resolve().parents[1]))
        return 0
    config_path = args.config.expanduser() if args.config else DEFAULT_CONFIG_PATH
    default_root = args.project_root.expanduser() if args.project_root else Path.cwd()
    try:
        config = load_runtime_config(
            config_path,
            required=args.config is not None,
            default_root=default_root,
        )
        config = override_runtime_config(
            config,
            project_root=args.project_root,
            application_db=args.db,
            jobs_db=args.jobs_db,
            preference_db=args.preference_db,
            proxy_db=args.proxy_db,
            resume_lab_db=args.resume_db,
            resume_artifact_root=args.resume_artifact_root,
            resume_model_config=args.resume_model_config,
            inference_config=args.inference_config,
            portable_encryption_key_file=args.portable_encryption_key_file,
            resume_tectonic_executable=args.resume_tectonic_executable,
            resume_tectonic_bundle=args.resume_tectonic_bundle,
            resume_tectonic_version=args.resume_tectonic_version,
            tool_service_socket=args.tool_service_socket,
            hermes_notification_socket=args.hermes_notification_socket,
            log_dir=args.log_dir,
        )
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    if args.command == "shortlist":
        from .curated_client import publish
        try:
            if args.input == "-":
                raw = sys.stdin.buffer.read(65537)
            else:
                with Path(args.input).open("rb") as stream:
                    raw = stream.read(65537)
            if len(raw) > 65536:
                raise ValueError("publication exceeds 64 KiB")
            _json(publish(config, json.loads(raw)))
        except (OSError, ValueError, ContractError) as exc:
            raise SystemExit(str(exc)) from None
        return 0
    db_path = config.application_db
    if args.command == 'mail-review':
        from .mail.review import MailReviewService
        try:
            ledger = JobSearchLedger(db_path)
            review = MailReviewService(ledger)
            if args.action == 'list':
                result = review.list_pending(args.limit, args.after)
            elif args.action == 'applications':
                result = review.applications(args.search, args.limit, args.after)
            elif args.action == 'message':
                from .review_messages import review_message
                from .system import _DashboardMailSource
                if not args.proposal_id:
                    raise ContractError('message requires --proposal-id')
                result = review_message(ledger, _DashboardMailSource(config, ledger),
                    {'kind':'event_proposal', 'id':args.proposal_id})
            else:
                # A saved preview includes both decisions and explanatory changes.
                maximum = 2 * 1024 * 1024
                if args.input == '-':
                    raw = sys.stdin.buffer.read(maximum + 1)
                else:
                    with Path(args.input).open('rb') as stream:
                        raw = stream.read(maximum + 1)
                if len(raw) > maximum:
                    raise ContractError('mail review input exceeds 2 MiB')
                payload = json.loads(raw)
                if not isinstance(payload, dict) or 'decisions' not in payload:
                    raise ContractError('mail review input requires decisions')
                if args.action == 'preview':
                    result = review.preview(payload['decisions'])
                else:
                    if not args.idempotency_key:
                        raise ContractError('apply requires --idempotency-key and a saved preview')
                    result = review.apply(payload['decisions'], payload.get('preview_hash'),
                        MutationContext(args.idempotency_key, 'user', 'operator_mail_review'))
            _json(result)
        except (OSError, ValueError, ContractError) as exc:
            raise SystemExit(str(exc)) from None
        return 0
    if args.command == "mail-understanding":
        from .mail.understanding_replay import UnderstandingReplay
        if args.understanding_command == "evaluate":
            from .mail.understanding_evaluation import evaluate_cases
            try:
                if args.input.stat().st_size > 16 * 1024 * 1024:
                    raise SystemExit("evaluation input exceeds 16 MiB")
                cases = json.loads(args.input.read_text())
                if not isinstance(cases, list) or len(cases) > 10000:
                    raise SystemExit("evaluation requires an array of at most 10,000 cases")
                result = evaluate_cases(cases, producer_version=args.producer_version, dataset_kind=args.dataset_kind)
                descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, 'w') as stream:
                    json.dump(result['report'], stream, sort_keys=True)
            except (OSError, ValueError, ContractError) as exc:
                raise SystemExit(str(exc)) from None
            _json({'report_file':str(args.output),'metrics':result['metrics'],'all_classes':result['all_classes'],
                   'automation_eligible_dataset':args.dataset_kind == 'reviewed_private_holdout'})
            return 0
        ledger = JobSearchLedger(db_path)
        runtime = None
        if args.action == 'resume':
            from .mail.archive import EncryptedMailArchive, KeychainArchiveKeyProvider
            from .mail.understanding_runtime import build_understanding_runtime
            if config.portable_encryption_key_file:
                from .secure_persistence import PortableArchiveKeyProvider
                key_provider = PortableArchiveKeyProvider(config.portable_encryption_key_file)
            else:
                key_provider = KeychainArchiveKeyProvider(read_only=True)
            runtime = build_understanding_runtime(config,ledger,EncryptedMailArchive(ledger,key_provider),config.environment(os.environ))
        replay = UnderstandingReplay(ledger,runtime)
        if args.action == 'preview':
            result = replay.preview(args.account_id or config.outlook_account_id)
        elif args.action == 'start':
            if not args.idempotency_key:
                raise SystemExit('history start requires --idempotency-key')
            result = replay.start(args.account_id or config.outlook_account_id,MutationContext(args.idempotency_key,'user','cli'))
        else:
            if not args.replay_id:
                raise SystemExit('history operation requires --replay-id')
            if args.action == 'inspect':
                result = replay.inspect(args.replay_id)
            elif args.action == 'cancel':
                if not args.idempotency_key:
                    raise SystemExit('history cancel requires --idempotency-key')
                result = replay.cancel(args.replay_id,MutationContext(args.idempotency_key,'user','cli'))
            else:
                result = replay.run_batch(args.replay_id,limit=args.limit,retry_failed=args.retry_failed)
        _json(result)
        return 0
    if args.command == "setup":
        from .setup import inspect
        _json(inspect(config)); return 0
    if args.command == "automation":
        from .activation import controls, set_control
        if args.action == "list":
            _json({"controls": controls(db_path)}); return 0
        if not args.idempotency_key:
            raise SystemExit("automation decisions require --idempotency-key")
        _json(set_control(config,args.capability,args.action == "enable",expected_revision=args.expected_revision,command_id=args.idempotency_key)); return 0
    if args.command == "init":
        stamp = utc_now()
        prepare_database(db_path, stamp)
        schedules = seed_default_schedules(
            db_path, datetime.now(timezone.utc), config.environment(os.environ)
        )
        _json(
            {
                "database": str(db_path),
                "schema": "ready",
                "runtime_config_version": config.version,
                "schedules": schedules,
            }
        )
        return 0
    if args.command == "outlook-auth":
        from job_search.outlook.auth import BASE_SCOPES, DRAFT_SCOPES, HOLD_SCOPES, SEND_SCOPES

        provider = _outlook_provider(args, config)
        grants = [("read", BASE_SCOPES)]
        if args.enable_drafts:
            grants.append(("drafts", DRAFT_SCOPES))
        if args.enable_holds:
            grants.append(("holds", HOLD_SCOPES))
        if args.enable_send:
            grants.append(("send", SEND_SCOPES))
        for _name, scopes in grants:
            if args.device_code:
                provider.get_token(
                    scopes,
                    interactive=True,
                    device_code_callback=lambda message: print(
                        message, file=sys.stderr, flush=True
                    ),
                )
            else:
                provider.get_token(scopes, interactive=True)
        _json({"authenticated": True, "capabilities": [name for name, _ in grants]})
        return 0
    if args.command == "outlook-disconnect":
        provider = _outlook_provider(args, config)
        provider.disconnect()
        _json({"disconnected": True})
        return 0
    if args.command == "service":
        from job_search.launchd import manage_launch_agents, service_status

        if args.action == "status":
            if args.apply:
                raise SystemExit(
                    "service status is read-only and does not accept --apply"
                )
            result = service_status(
                config,
                output_dir=args.output_dir,
                config_path=config_path,
            )
        else:
            result = manage_launch_agents(
                config,
                action=args.action,
                apply=args.apply,
                output_dir=args.output_dir,
                config_path=config_path,
            )
        _json(result)
        return 0 if not result.get("applied") or result.get("ok", True) else 2
    if args.command in {"mcp-token-init", "interaction-token-init"}:
        from job_search.system import initialize_mcp_token
        target = config.mcp_token_file if args.command == "mcp-token-init" else config.interaction_token_file
        if target is None:
            raise SystemExit("configure interaction_token_file and Telegram owner identity first")
        try:
            path = initialize_mcp_token(target)
        except FileExistsError as exc:
            raise SystemExit(
                f"Bearer token file already exists; refusing to overwrite: {target}"
            ) from exc
        field = "mcp_token_file" if args.command == "mcp-token-init" else "interaction_token_file"
        _json({"created": True, field: str(path)})
        return 0
    if args.command == "encryption-key-init":
        if config.portable_encryption_key_file is None:
            raise SystemExit(
                "configure portable_encryption_key_file before initializing it"
            )
        from job_search.secure_persistence import initialize_portable_master_key

        try:
            initialize_portable_master_key(config.portable_encryption_key_file)
        except FileExistsError as exc:
            raise SystemExit(
                "portable encryption key already exists; refusing to overwrite"
            ) from exc
        _json({"created": True, "portable_encryption_configured": True})
        return 0
    if args.command == "portable-state-export":
        if config.portable_encryption_key_file is None:
            raise SystemExit(
                "pass --portable-encryption-key-file or configure it before export"
            )
        from job_search.mail.archive import KeychainArchiveKeyProvider
        from job_search.portable_export import export_portable_state

        try:
            source_provider = None
            if args.source_archive_key_file is not None:
                source_provider = KeychainArchiveKeyProvider(
                    args.source_archive_key_file.expanduser().resolve(),
                    read_only=True,
                )
            source_vault = args.source_autofill_vault or config.autofill_vault
            result = export_portable_state(
                source_database=db_path,
                destination_database=args.destination_db.expanduser().resolve(),
                portable_key_file=config.portable_encryption_key_file,
                source_autofill_vault=(
                    source_vault.expanduser().resolve()
                    if source_vault is not None
                    else None
                ),
                destination_autofill_vault=(
                    args.destination_autofill_vault.expanduser().resolve()
                    if args.destination_autofill_vault is not None
                    else None
                ),
                source_archive_key_provider=source_provider,
            )
        except (ContractError, FileExistsError, OSError, sqlite3.Error) as exc:
            raise SystemExit(str(exc)) from exc
        _json(result)
        return 0
    if args.command == "resume":
        from job_search.resume_lab.gateway import (
            build_resume_lab_gateway,
            resume_lab_status,
        )

        if args.resume_command == "doctor":
            result = resume_lab_status(config)
            _json(result)
            return (
                0
                if result["status"] in {"ready", "configuration_ready"}
                else 2
            )
        gateway = build_resume_lab_gateway(config, allow_unmanaged_remote_inference=bool(getattr(args, "allow_unmanaged_remote_inference", False)))
        if gateway is None:
            raise SystemExit(
                "configure both resume_lab_db and resume_artifact_root first"
            )
        try:
            if args.resume_command == "career":
                result = _career_command(args, gateway, config)
            elif args.resume_command == "list":
                result = gateway.list_standards()
            elif args.resume_command == "import-existing":
                result = gateway.import_existing_standard(args.name,args.rank,pdf=args.pdf.read_bytes(),
                    tex_source=_read_resume_tex(args.tex),intended_text=args.text.read_text(),
                    content=json.loads(args.content.read_text()) if args.content else None,
                    provenance=json.loads(args.provenance.read_text()) if args.provenance else None)
            elif args.resume_command == "import":
                result = gateway.import_standard(
                    args.name, args.rank, _read_resume_tex(args.tex)
                )
            elif args.resume_command == "update":
                result = gateway.update_standard(
                    args.standard_id, _read_resume_tex(args.tex)
                )
            elif args.resume_command == "rank":
                result = gateway.service.set_standard_rank(
                    args.standard_id, args.rank, actor_kind="user"
                )
            elif args.resume_command in {"archive", "activate"}:
                result = gateway.service.set_standard_active(
                    args.standard_id,
                    args.resume_command == "activate",
                    actor_kind="user",
                )
            else:  # pragma: no cover - argparse owns the command set
                raise ValueError("unknown resume command")
        except (OSError, RuntimeError, ValueError) as exc:
            raise SystemExit(str(exc)) from exc
        _json(result)
        return 0

    if args.command == "readiness":
        from job_search.runtime_readiness import runtime_readiness
        _json(runtime_readiness(config))
        return 0
    if args.command in {"work-list", "work-retry"}:
        from job_search.recovery import RecoveryService
        recovery = RecoveryService(db_path)
        try:
            result = {"items": recovery.list_work()} if args.command == "work-list" else recovery.retry(
                args.work_id, expected_revision=args.expected_revision,
                command_id=args.idempotency_key, actor_kind="user"
            )
        except (OSError, RuntimeError, ValueError) as exc:
            _json({"status": "rejected", "reason": str(exc)})
            return 2
        _json(result)
        return 0

    if args.command in {"inference-usage", "inference-recovery", "inference-reconcile"}:
        from job_search.inference.usage import InvocationRecoveryService
        from job_search.runtime_readiness import runtime_usage
        try:
            if args.command == "inference-usage":
                result = runtime_usage(config)
            else:
                recovery = InvocationRecoveryService(db_path)
                result = {"items": recovery.list_invocations()} if args.command == "inference-recovery" else recovery.reconcile(
                    args.invocation_id, expected_updated_at=args.expected_updated_at,
                    command_id=args.idempotency_key, resolution=args.resolution,
                    provider_job_id=args.provider_job_id, actor_kind="user",
                )
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            _json({"status": "rejected", "reason": str(exc)})
            return 2
        _json(result)
        return 0

    ledger = JobSearchLedger(db_path)
    if args.command in {"notification-recovery", "notification-reconcile"}:
        from job_search.system import build_notification_recovery
        recovery = build_notification_recovery(config, ledger)
        if recovery is None:
            _json({"status": "disabled", "reason_code": "notification_bridge_not_configured"})
            return 2
        try:
            result = recovery.list_pending() if args.command == "notification-recovery" else recovery.reconcile(
                args.notification_id, expected_attempts=args.expected_attempts,
                expected_payload_sha256=args.expected_payload_sha256, outcome=args.outcome,
                context=MutationContext(args.idempotency_key, "user", "operator_cli"),
            )
        except (OSError, RuntimeError, ValueError) as exc:
            _json({"status": "rejected", "reason": str(exc)})
            return 2
        _json(result)
        return 0
    if args.command == "status":
        result = dict(ledger.system_health())
        result["automation"] = automation_health(db_path, datetime.now(timezone.utc))
        result["dependencies"] = _dependency_health(config)
        from job_search.runtime_readiness import runtime_readiness
        result["readiness"] = runtime_readiness(config, dependencies=result["dependencies"])
        if result["dependencies"]["status"] == "attention":
            result["status"] = "attention"
        elif result["dependencies"]["status"] == "configuration_ready":
            result["status"] = "healthy_unprobed"
        _json(result)
        return 0 if result["status"] in {"healthy", "healthy_unprobed"} else 2
    if args.command == "verify":
        failures = list(ledger.verify_projections())
        _json({"ok": not failures, "projection_failures": failures})
        return 0 if not failures else 2
    if args.command == "rebuild":
        if not args.apply:
            failures = list(ledger.rebuild_projections(dry_run=True))
            _json({"applied": False, "projection_failures": failures})
            return 0 if not failures else 2
        stamp = utc_now()
        rebuilt = ledger.rebuild_projections(
            dry_run=False,
            context=MutationContext(
                "operator-rebuild:" + stamp,
                "user",
                "operator_cli",
            ),
        )
        _json({"applied": True, "rebuilt_application_ids": list(rebuilt)})
        return 0
    raise SystemExit("unknown command")


if __name__ == "__main__":
    raise SystemExit(main())
