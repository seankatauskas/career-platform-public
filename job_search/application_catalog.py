"""Catalog identity at the browser ingress, after command replay has been checked."""
from dataclasses import asdict

from .browser_tracking import identify_job
from .commands import DomainError, digest
from .contracts import ContractError, JobSnapshot


def catalog_snapshot(catalog, source, page_url):
    if catalog is None or not page_url:
        return None
    observed = identify_job(page_url)
    if (source.get("source"), source.get("source_id")) != (observed["ats"], observed["job_id"]):
        raise DomainError("invalid_input", "Browser page does not match the posting identity")
    try:
        job = catalog.get_job(observed["ats"], observed["job_id"])
    except ContractError as exc:
        if str(exc) == "job was not found":
            return None
        raise
    if (job.get("ats"), str(job.get("id"))) != (observed["ats"], observed["job_id"]):
        raise DomainError("invalid_input", "Catalog returned a different posting identity")
    reference = job.get("jobUrl")
    if reference:
        try:
            known = identify_job(reference)
        except ContractError:
            known = None  # Catalogs may contain custom employer landing pages.
        if known and any(known[k].casefold() != observed[k].casefold() for k in ("ats", "job_id", "board")):
            raise DomainError("invalid_input", "Posting board does not match the catalog job")
    snapshot = JobSnapshot(ats=observed["ats"], job_id=observed["job_id"],
        title=job["title"], employer=job["company"], company_slug=job["company"],
        job_url=reference or observed["canonical_url"], family_id="")
    snapshot.validate()
    return asdict(snapshot)


def browser_snapshot(runtime, tx, payload):
    source = payload.get("source") or {}
    if source.get("adapter") != "paired_extension_v1":
        return None
    posting = payload.get("job_source") or {}
    observed = identify_job(source.get("page_url"))
    app = runtime.applications.find_application_by_job(tx.connection, observed["ats"], observed["job_id"])
    if app:
        target = runtime.applications.catalog_capture_target(tx.connection, app["id"])
        references = [target["page_url"]] + [r["snapshot"].get("job_url") for r in target["job"]["recorded_snapshots"]]
        for reference in references:
            if not reference:
                continue
            try:
                known = identify_job(reference)
            except ContractError:
                continue
            # Combined jobs may retain other posting identities. Only the exact
            # observed posting can establish its board when the catalog is gone.
            if (known["ats"], known["job_id"]) == (observed["ats"], observed["job_id"]) and known["board"].casefold() != observed["board"].casefold():
                raise DomainError("invalid_input", "Posting board does not match the saved application")
    return catalog_snapshot(runtime.job_catalog, posting, source.get("page_url"))


def repair_preview(operations, connection, catalog, application_id):
    target = operations.catalog_capture_target(connection, application_id)
    job = target["job"]
    if job["recorded_snapshots"] or len(job["sources"]) != 1 or not target["page_url"]:
        return {"application_id": application_id, "status": "ineligible"}
    snapshot = catalog_snapshot(catalog, job["sources"][0], target["page_url"])
    if snapshot is None:
        return {"application_id": application_id, "status": "catalog_missing"}
    return {"application_id": target["application"]["id"], "status": "ready",
        "expected_version": target["application"]["version"], "snapshot_sha256": digest(snapshot),
        "before": {k: job[k] for k in ("title", "employer")},
        "after": {k: snapshot[k] for k in ("title", "employer")}}


def repair(runtime, tx, application_id, expected_version, snapshot_sha256):
    if type(expected_version) is not int or expected_version < 1:
        raise DomainError("invalid_input", "An exact application version is required")
    preview = repair_preview(runtime.applications, tx.connection, runtime.job_catalog, application_id)
    if preview["status"] != "ready" or preview["expected_version"] != expected_version or preview["snapshot_sha256"] != snapshot_sha256:
        raise DomainError("version_conflict", "Application or catalog changed; preview the repair again")
    target = runtime.applications.catalog_capture_target(tx.connection, application_id)
    snapshot = catalog_snapshot(runtime.job_catalog, target["job"]["sources"][0], target["page_url"])
    if digest(snapshot) != snapshot_sha256:
        raise DomainError("version_conflict", "Catalog changed during repair")
    return runtime.applications.capture_catalog_snapshot(tx, application_id, snapshot)


def main(argv=None):
    """Preview read-only; apply only the exact reviewed, version-bound plan."""
    import argparse
    import json
    import sqlite3
    from contextlib import closing
    from pathlib import Path
    from .runtime import load_runtime_config
    from .integration import LocalJobCatalog
    from .applications.api import ApplicationOperations
    from .application_runtime import ApplicationRuntime
    from .commands import CommandContext, Principal
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--application-id", action="append", default=[])
    parser.add_argument("--apply-plan", type=Path)
    args = parser.parse_args(argv)
    config = load_runtime_config(args.config, required=True)
    if config.application_backend != "owners" or not config.application_owner_db:
        parser.error("The application owners backend is required")
    catalog = LocalJobCatalog(config.jobs_db)
    if args.apply_plan:
        if args.application_id:
            parser.error("Use application IDs for preview or an exact plan for apply")
        plan = json.loads(args.apply_plan.read_text())
        if not isinstance(plan, list) or not 1 <= len(plan) <= 100:
            parser.error("A bounded preview plan is required")
        runtime = ApplicationRuntime(config.application_owner_db, job_catalog=catalog)
        results = []
        for item in plan:
            if item.get("status") != "ready":
                parser.error("Only ready preview items may be applied")
            value = {k: item[k] for k in ("application_id", "expected_version", "snapshot_sha256")}
            context = CommandContext(Principal("catalog-repair-operator", "human", frozenset({"repair_catalog_job"})),
                "catalog-repair:" + digest(value))
            results.append(runtime.command(context, "repair_catalog_job", value))
        print(json.dumps(results, indent=2))
    else:
        if not 1 <= len(args.application_id) <= 100:
            parser.error("Provide between 1 and 100 explicit application IDs")
        with closing(sqlite3.connect(config.application_owner_db.as_uri() + "?mode=ro", uri=True)) as con:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA query_only=ON")
            print(json.dumps([repair_preview(ApplicationOperations(), con, catalog, aid) for aid in args.application_id], indent=2))


if __name__ == "__main__":
    main()
