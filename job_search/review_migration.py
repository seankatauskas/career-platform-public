"""Preview and resume owner review reconciliation without deleting evidence.

python -m job_search.review_migration --help
The manifest includes private application evidence identifiers; store it privately.
"""
import argparse
import json
import os
from pathlib import Path

from .application_runtime import ApplicationRuntime
from .application_transport import HumanApplicationAdapter
from .applications.review import ReviewService
from .commands import DomainError, digest


def write_private(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(value, stream, indent=2)
        stream.write("\n")


def apply(runtime, manifest, *, actor):
    if manifest.get("version") != 1 or manifest.get("installation") != runtime.executor.installation_identity():
        raise DomainError("invalid_input", "Manifest belongs to another installation or migration version")
    human = HumanApplicationAdapter(runtime, actor)
    if manifest.get("operation") == "refresh_review_group":
        results = []
        for group in manifest["groups"]:
            value = {k: group[k] for k in ("group_id", "expected_digest")}
            try:
                result = human.command("refresh_review_group", value, "review-refresh:" + digest(value))
                results.append({"group_id": group["group_id"], "result": result})
            except DomainError as exc:
                results.append({"group_id": group["group_id"], "error": exc.code})
        return {"installation": manifest["installation"], "results": results,
                "completed": sum("result" in r for r in results), "needs_refresh": sum("error" in r for r in results)}
    policy = manifest["policy"]
    if manifest.get("enable_browser") and not policy["enabled"]:
        policy = human.command("configure_browser_recording", {"enabled": True, "expected_version": policy["version"]},
                               "review-migration-policy:" + digest(manifest))
    results = []
    for group in manifest["groups"]:
        value = {k: group[k] for k in ("group_id", "expected_digest", "expected_policy_version")}
        value["expected_policy_version"] = policy["version"]
        try:
            result = human.command("migrate_review_group", value, "review-migration:" + digest(value))
            results.append({"group_id": group["group_id"], "result": result})
        except DomainError as exc:
            # Group transactions are independent. A stale group cannot cancel or
            # silently overwrite another group's completed, replayable migration.
            results.append({"group_id": group["group_id"], "error": exc.code})
    return {"installation": manifest["installation"], "results": results,
            "completed": sum("result" in r for r in results), "needs_refresh": sum("error" in r for r in results)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    preview = sub.add_parser("preview", help="Write a private manifest; no business decisions change")
    preview.add_argument("--output", type=Path, required=True)
    preview.add_argument("--enable-browser-recording", action="store_true", help="Preview the explicit browser opt-in to apply with this migration")
    refresh = sub.add_parser("refresh-preview", help="Preview reanalysis of open emails with the current prompt and context")
    refresh.add_argument("--output", type=Path, required=True)
    run = sub.add_parser("apply", help="Apply a reviewed manifest, one atomic evidence group at a time")
    run.add_argument("--manifest", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--actor", required=True, help="Attributable owner/operator identity")
    args = parser.parse_args(argv)
    if not args.database.is_file():
        parser.error("The existing owners database is required")
    runtime = ApplicationRuntime(args.database)
    if args.command == "preview":
        value = ReviewService(runtime).migration_preview(enable_browser=args.enable_browser_recording)
    elif args.command == "refresh-preview":
        value = ReviewService(runtime).refresh_preview()
    else:
        value = apply(runtime, json.loads(args.manifest.read_text()), actor=args.actor)
    write_private(args.output, value)
    print("Private review migration " + args.command + " written to " + str(args.output))
    return 1 if value.get("needs_refresh") else 0


if __name__ == "__main__":
    raise SystemExit(main())
