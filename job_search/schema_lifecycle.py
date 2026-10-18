"""Explicit maintenance migrations and read-only production startup checks.

Local tools retain bootstrap behavior. Production opts into require_current;
only an explicit migration context may initialize or upgrade persisted schemas.
"""
from contextlib import closing, contextmanager
from contextvars import ContextVar
from functools import lru_cache
import os
from pathlib import Path
import re
import sqlite3
import stat


_MIGRATING = ContextVar("explicit_schema_migration", default=False)
_OFFLINE = ContextVar("offline_schema_inspection", default=False)
STARTUP_POLICY = "require-current-v2"


def require_current():
    mode = os.environ.get("JOB_SEARCH_SCHEMA_MODE", "migrate")
    if mode not in {"migrate", "require_current"}:
        raise RuntimeError("Unknown JOB_SEARCH_SCHEMA_MODE")
    return mode == "require_current" and not _MIGRATING.get()


@contextmanager
def explicit_migration():
    token = _MIGRATING.set(True)
    try:
        yield
    finally:
        _MIGRATING.reset(token)


def read_connection(path):
    """Never create a file, change journal mode, chmod, or run migrations."""
    path = Path(path)
    query = "?mode=ro"
    if _OFFLINE.get():
        # The host must stop every writer before using immutable. Without that
        # operational fence, a new WAL could appear between inspection and open.
        if not stat.S_ISREG(path.lstat().st_mode):
            raise RuntimeError("Offline schema check requires a regular database")
        for suffix in ("-wal", "-journal", "-shm"):
            sidecar = Path(str(path) + suffix)
            try:
                metadata = sidecar.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(metadata.st_mode) or (suffix != "-shm" and metadata.st_size):
                raise RuntimeError("Offline schema check requires checkpointed database sidecars")
        query += "&immutable=1"
    con = sqlite3.connect(path.resolve().as_uri() + query, uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    return con


def _objects(con):
    return {(row[0], row[1]): row[2] for row in con.execute(
        "SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
    )}


def _sql(value):
    return re.sub(r"\s+", " ", value.strip()).replace("IF NOT EXISTS ", "").rstrip(";")


def require_objects(con, expected):
    """Check required objects without replaying DDL against the source.

Table columns are compared structurally because SQLite preserves ALTER history
in CREATE text. Existing additive columns/defaults are retained for compatibility.
Migration ledgers/checksums separately fence ordered and owner schema versions.
"""
    actual = _objects(con)
    for (kind, name), sql in _objects(expected).items():
        if (kind, name) not in actual:
            raise RuntimeError(f"Schema migration required: missing {kind} {name}")
        if kind == "table":
            quoted = '"' + name.replace('"', '""') + '"'
            columns = {r[1]: (r[2].upper(), r[5]) for r in con.execute(f"PRAGMA table_info({quoted})")}
            for row in expected.execute(f"PRAGMA table_info({quoted})"):
                if columns.get(row[1]) != (row[2].upper(), row[5]):
                    raise RuntimeError(f"Schema migration required: incompatible {name}.{row[1]}")
        elif _sql(actual[(kind, name)]) != _sql(sql):
            raise RuntimeError(f"Schema migration required: incompatible {kind} {name}")


@lru_cache(maxsize=1)
def _ledger_schema():
    from . import db
    from .recovery import install_owner_resolution_schema
    from .inference.usage import install_allowance_schema
    con = sqlite3.connect(":memory:")
    con.execute("BEGIN")
    for _, _, sql in db.MIGRATIONS:
        for statement in db._migration_statements(sql):
            con.execute(statement)
    install_owner_resolution_schema(con)
    install_allowance_schema(con)
    con.commit()
    result = tuple(con.iterdump())
    con.close()
    return result


def check_ledger(path):
    from . import db
    with closing(read_connection(path)) as con:
        db._reject_application_candidate(con)
        applied = {int(r["version"]): (r["name"], r["checksum"]) for r in db._applied_migrations(con)}
        expected = {version: (name, db._checksum(sql)) for version, name, sql in db.MIGRATIONS}
        if applied != expected or con.execute("PRAGMA user_version").fetchone()[0] != max(expected):
            raise RuntimeError("Schema migration required: ledger version or checksum mismatch")
        # Do not share an SQLite connection across service threads.
        with closing(sqlite3.connect(":memory:")) as schema:
            for statement in _ledger_schema():
                schema.execute(statement)
            require_objects(con, schema)


def check_owner(con, schemas):
    import hashlib
    applied = dict(con.execute("SELECT owner,checksum FROM command_schema_versions"))
    schemas = dict(schemas)
    if "migration" in applied and "migration" not in schemas:
        # Real converted installations retain their immutable provenance owner.
        # Ordinary runtime deliberately does not register it as a command owner.
        from .application_migration import SCHEMA as MIGRATION_SCHEMA
        schemas["migration"] = MIGRATION_SCHEMA
    expected = {owner: hashlib.sha256(sql.encode()).hexdigest() for owner, sql in schemas.items()}
    if applied != expected:
        raise RuntimeError("Schema migration required: owner schema version or checksum mismatch")
    with closing(sqlite3.connect(":memory:")) as schema:
        for sql in schemas.values():
            schema.executescript(sql)
        require_objects(con, schema)


def check_collector(con, *, component="all"):
    from .collection import boards
    with closing(sqlite3.connect(":memory:")) as schema, explicit_migration():
        if component in {"all", "jobs"}:
            boards._prepare(schema)
        if component in {"all", "registry"}:
            boards._prepare_board_registry(schema)
        if component in {"all", "etags"}:
            boards._prepare_etags(schema)
        require_objects(con, schema)


@lru_cache(maxsize=None)
def _optional_schema(component):
    """Build expected metadata in memory, never by migrating the installed store."""
    with closing(sqlite3.connect(":memory:")) as con, explicit_migration():
        con.row_factory = sqlite3.Row
        if component == "preferences":
            from .collection import boards
            from .ranking import labeler
            boards._prepare(con)
            labeler._create_preferences(con)
            labeler._prepare_product_schema(con)
        elif component == "ranking":
            from .ranking.model import STATE_SCHEMA
            con.executescript(STATE_SCHEMA)
        elif component == "proxy":
            from .ranking.proxy import DDL
            con.executescript(DDL)
        elif component == "resume":
            from .resume_lab.store import SCHEMA_SQL
            from .resume_lab.career_runs import CAREER_SCHEMA, SOURCE_TRIGGERS
            con.executescript(CAREER_SCHEMA + SCHEMA_SQL + SOURCE_TRIGGERS)
        elif component == "career":
            from .resume_lab.career_store import SCHEMA_SQL
            con.executescript(SCHEMA_SQL)
        elif component == "enrichment":
            from .salary.enrichment import prepare_enrichment
            prepare_enrichment(con)
        elif component == "salary_llm":
            from .salary.llm import prepare_schema
            prepare_schema(con)
        elif component == "salary_validation":
            from .salary.labeler import SCHEMA
            con.executescript(SCHEMA)
        elif component in {"salary_teacher", "salary_v3"}:
            from importlib import import_module
            module = import_module("job_search.salary." + ("teacher" if component == "salary_teacher" else "v3"))
            con.executescript(module.DDL)
        elif component == "locations":
            from .collection.locations import SCHEMA
            con.executescript(SCHEMA)
        elif component == "families":
            from .collection.dedupe import _SCHEMA
            for statement in _SCHEMA:
                con.execute(statement)
        elif component == "mail_archive":
            from .mail.revision_archive import SCHEMA
            con.executescript(SCHEMA)
        else:
            raise ValueError("Unknown optional schema component")
        # No data copied: even defaults and preparation bookkeeping above stay
        # entirely in the in-memory model used to derive current schema objects.
        return tuple(sql for sql in _objects(con).values())


def check_optional(con, component):
    with closing(sqlite3.connect(":memory:")) as expected:
        for sql in _optional_schema(component):
            expected.execute(sql)
        require_objects(con, expected)
    if component == "resume":
        versions = {row[0] for row in con.execute("SELECT version FROM resume_lab_schema")}
        if versions != {1, 2, 3, 4}:
            raise RuntimeError("Schema migration required: resume schema version mismatch")
    if component == "career" and not con.execute(
        "SELECT 1 FROM career_profile_state WHERE singleton=1"
    ).fetchone():
        raise RuntimeError("Initialization required: career profile state is missing")


def check_optional_path(path, component):
    with closing(read_connection(path)) as con:
        check_optional(con, component)


# Presence is established from every owned object, not only one anchor table:
# partial schemas must not disappear from the read-only check inventory.
_JOB_COMPONENTS = ("preferences", "enrichment", "salary_llm", "salary_validation",
                   "salary_teacher", "salary_v3", "locations", "families")


def _present_components(con, components):
    actual = set(_objects(con))
    for component in components:
        with closing(sqlite3.connect(":memory:")) as expected:
            for sql in _optional_schema(component):
                expected.execute(sql)
            owned = set(_objects(expected))
        if component == "preferences":
            # The reference includes collector dependencies, which do not mean
            # the user has enabled ranking or labeling.
            owned = {obj for obj in owned if obj[1].startswith(("preference_", "recommendation_", "job_preferences"))}
        if component == "salary_llm":
            owned = {obj for obj in owned if obj[1].startswith("salary_llm_")}
        if owned & actual:
            yield component


PREPARABLE_FEATURES = ("ranking", "salary")


def _configured_features(config, requested=()):
    requested = set(requested)
    if requested - set(PREPARABLE_FEATURES):
        raise ValueError("Unknown feature schema preparation request")
    with closing(read_connection(config.application_db)) as con:
        controls = dict(con.execute("SELECT capability,enabled FROM automation_controls"))
    enabled = {name for name, value in controls.items() if value}
    # Missing controls preserve legacy activation semantics. Existing ranking
    # sidecars establish that this feature was configured before explicit switches.
    if "ranking" not in controls and (config.preference_db.exists() or config.proxy_db.exists()):
        enabled.add("ranking")
    return requested | (enabled & set(PREPARABLE_FEATURES))


def _optional_components(config, *, features=()):
    with closing(read_connection(config.jobs_db)) as con:
        present = set(_present_components(con, _JOB_COMPONENTS))
    # Native salary extraction runs on every collector save, and locations are
    # the normal collection pipeline's next stage. Empty schemas activate no models.
    required = {"enrichment", "locations"}
    if "ranking" in features:
        required.update(("preferences", "families"))
    if "salary" in features:
        required.add("salary_llm")
    jobs = tuple(component for component in _JOB_COMPONENTS if component in present | required)
    yield config.jobs_db, jobs
    for path, component in ((config.preference_db, "ranking"), (config.proxy_db, "proxy")):
        if path.exists() or "ranking" in features:
            yield path, (component,)
    if config.resume_lab_db is not None:
        yield config.resume_lab_db, ("resume", "career")
    with closing(read_connection(config.application_db)) as con:
        archive = tuple(_present_components(con, ("mail_archive",)))
    yield config.application_db, (("mail_archive",) if config.application_backend == "owners" else archive)


def _migrate_optional(config, *, features=()):
    from .ranking import labeler, model, proxy, refresh_freshness
    from .salary import enrichment, llm, teacher, v3, labeler as salary_labeler
    from .collection import locations, dedupe, dedupe_incremental
    from .resume_lab.store import ResumeLabStore
    from .resume_lab.career_store import CareerStore
    from .mail.revision_archive import SCHEMA as ARCHIVE_SCHEMA
    for path, components in _optional_components(config, features=features):
        for component in components:
            prepare = {"preferences": labeler.prepare_preferences, "ranking": model.prepare_state,
                       "proxy": proxy.prepare_schema, "salary_teacher": teacher.prepare_schema,
                       "salary_v3": v3.prepare_schema, "salary_validation": salary_labeler.prepare_validation,
                       "resume": ResumeLabStore, "career": CareerStore}.get(component)
            if prepare is not None:
                prepare(path)
            else:
                with closing(sqlite3.connect(path)) as con, con:
                    if component == "enrichment":
                        enrichment.prepare_enrichment(con)
                    elif component == "salary_llm":
                        llm.prepare_schema(con)
                    elif component == "locations":
                        locations.prepare_schema(con)
                    elif component == "families":
                        for sql in dedupe._SCHEMA:
                            con.execute(sql)
                        dedupe_incremental.install(con)
                    elif component == "mail_archive":
                        con.executescript(ARCHIVE_SCHEMA)
        # Existing trackers are schema, not instructions to activate ranking.
        with closing(read_connection(path)) as con:
            names = {row[0] for row in con.execute("SELECT name FROM sqlite_master")}
        for kind in ("source", "scores"):
            required = (kind == "source" and path == config.jobs_db and "ranking" in features
                        or kind == "scores" and path == config.preference_db)
            if required or any(name.startswith("ranking_refresh_" + kind + "_") for name in names):
                refresh_freshness.ensure_tracking(path, kind)


def _check_optional_config(config, *, features=None):
    from .ranking.refresh_freshness import check_tracking
    from .collection import dedupe_incremental
    features = _configured_features(config) if features is None else features
    for path, components in _optional_components(config, features=features):
        with closing(read_connection(path)) as con:
            for component in components:
                check_optional(con, component)
            names = {row[0] for row in con.execute("SELECT name FROM sqlite_master")}
            for kind in ("source", "scores"):
                required = (kind == "source" and path == config.jobs_db and "ranking" in features
                            or kind == "scores" and path == config.preference_db)
                if required or any(name.startswith("ranking_refresh_" + kind + "_") for name in names):
                    check_tracking(con, kind)
            if "families" in components or any(name.startswith(dedupe_incremental.PREFIX) for name in names):
                dedupe_incremental.check_schema(con)



def check_feature_schemas(config, feature):
    """Reject activation before it can enqueue work against missing schema."""
    if feature not in PREPARABLE_FEATURES:
        raise ValueError("Unknown feature schema preparation request")
    _check_optional_config(config, features=_configured_features(config, (feature,)))


def check_config(config, *, offline=False):
    """Bounded metadata checks only; no full-data integrity scan or initialization."""
    token = _OFFLINE.set(offline)
    try:
        return _check_config(config)
    finally:
        _OFFLINE.reset(token)


def _check_config(config):
    check_ledger(config.application_db)
    from .application_installation import require_backend
    require_backend(config)
    if config.application_backend == "owners":
        from .application_runtime import ApplicationRuntime
        # Explicitly request verification even for local invocations of this CLI.
        ApplicationRuntime(config.application_owner_db, require_schema=True)
        from .application_production import check_owner_schedules
        check_owner_schedules(config, config.environment(os.environ))
    with closing(read_connection(config.jobs_db)) as con:
        check_collector(con)
    from .scheduler import DEFAULT_SCHEDULES
    with closing(read_connection(config.application_db)) as con:
        schedules = {r[0] for r in con.execute("SELECT schedule_key FROM schedule_specs")}
        if not {spec.schedule_key for spec in DEFAULT_SCHEDULES} <= schedules:
            raise RuntimeError("Initialization required: default schedules are missing")
    _check_optional_config(config)
    return {"schema": "ready", "startup_schema_policy": STARTUP_POLICY}


def migrate_config(config, *, prepare_features=()):
    """Explicit maintenance only; callers provide worker quiescence/checkpoints."""
    from datetime import datetime, timezone
    from .contracts import utc_now
    from .db import prepare_database
    from .scheduler import seed_default_schedules
    from .application_installation import require_backend, LEGACY_TASKS
    from .collection import boards
    with explicit_migration():
        prepare_database(config.application_db, utc_now())
        config.jobs_db.parent.mkdir(parents=True, exist_ok=True)
        from .db import _ensure_private_file
        _ensure_private_file(config.jobs_db)
        with closing(sqlite3.connect(config.jobs_db, timeout=60)) as con, con:
            boards._prepare(con)
            boards._prepare_board_registry(con)
            boards._prepare_etags(con)
        require_backend(config)
        schedules = seed_default_schedules(config.application_db, datetime.now(timezone.utc),
            config.environment(os.environ), excluded_tasks=LEGACY_TASKS if config.application_backend == "owners" else ())
        if config.application_backend == "owners":
            from .application_runtime import ApplicationRuntime
            ApplicationRuntime(config.application_owner_db)
            from .application_production import _seed
            _seed(config, lambda: datetime.now(timezone.utc), config.environment(os.environ))
        _migrate_optional(config, features=_configured_features(config, prepare_features))
    return {"database": str(config.application_db), "schema": "ready",
            "runtime_config_version": config.version, "schedules": schedules}
