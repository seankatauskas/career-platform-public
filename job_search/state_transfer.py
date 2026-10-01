"""Move a stopped installation without losing application or resume history.

The archive is private data, not a release artifact. OAuth caches, configuration,
provider credentials and encryption keys are deliberately transferred separately.
"""
from __future__ import annotations
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tarfile
import tempfile

from .aws_ops import copy_snapshot, digest, safe_extract, sqlite_check, write_json

DATABASES = {
    'application_db': 'applications.db', 'jobs_db': 'jobs.db',
    'preference_db': 'preferences.db', 'proxy_db': 'policies.db',
    'resume_lab_db': 'career.db',
}


def private_destination(path: Path) -> Path:
    raw = path.expanduser().absolute()
    if raw.is_symlink() or raw.exists():
        raise ValueError('destination already exists; refusing to overwrite state')
    result = raw.resolve()
    repository = Path(__file__).resolve().parents[1]
    if result == repository or repository in result.parents:
        raise ValueError('private transfer destinations must be outside the repository')
    if not result.parent.is_dir():
        raise ValueError('destination parent must exist')
    return result


def key_fingerprint(path: Path | None) -> str:
    if not path or not path.is_file() or path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError('an owner-only portable encryption key is required')
    return digest(path)


def export_state(config, output: Path, *, writers_stopped: bool = False) -> dict:
    if not writers_stopped:
        raise ValueError('stop dashboard, MCP, workers and all other writers before exporting')
    output = private_destination(output)
    fingerprint = key_fingerprint(config.portable_encryption_key_file)
    with closing(sqlite3.connect(config.application_db.resolve().as_uri()+'?mode=ro', uri=True)) as con:
        if (con.execute("SELECT 1 FROM work_items WHERE status='running' LIMIT 1").fetchone()
                or con.execute("SELECT 1 FROM outbox_messages WHERE status='delivering' LIMIT 1").fetchone()):
            raise ValueError('resolve running work before export')
    with tempfile.TemporaryDirectory(prefix='.state-export-', dir=output.parent) as directory:
        stage = Path(directory) / 'payload'; stage.mkdir(mode=0o700)
        for field, name in DATABASES.items():
            source = getattr(config, field)
            if not source or not source.is_file() or source.is_symlink():
                raise ValueError('missing installation database: '+field)
            with closing(sqlite3.connect(source.resolve().as_uri()+'?mode=ro', uri=True)) as src, \
                 closing(sqlite3.connect(stage/name)) as dst:
                src.backup(dst)
            (stage/name).chmod(0o600)
            sqlite_check(stage/name)
        if not config.resume_artifact_root or not config.resume_artifact_root.is_dir():
            raise ValueError('resume artifact storage is missing')
        copy_snapshot(config.resume_artifact_root, stage/'resume-artifacts')
        models = {}
        with closing(sqlite3.connect(stage/'preferences.db')) as con:
            for run, source, raw_manifest in con.execute('SELECT run_id,artifact_path,manifest_json FROM preference_model_runs'):
                if not run.startswith('run_') or not all(c.isalnum() or c=='_' for c in run):
                    raise ValueError('invalid model identifier')
                manifest = json.loads(raw_manifest)
                source = Path(source)
                target = stage/'models'/'runs'/run
                target.mkdir(parents=True, mode=0o700)
                if json.loads((source/'manifest.json').read_text()) != manifest:
                    raise ValueError('model manifest differs from registry')
                for name, expected in manifest['artifacts'].items():
                    if Path(name).name != name or name in ('.','..'):
                        raise ValueError('invalid model artifact path')
                    asset = source/name
                    if asset.is_symlink() or digest(asset) != expected:
                        raise ValueError('model artifact checksum mismatch')
                    shutil.copyfile(asset,target/name); (target/name).chmod(0o600)
                write_json(target/'manifest.json',manifest)
                models[run] = manifest['model_revision']
        files = {str(p.relative_to(stage)):{'sha256':digest(p),'size':p.stat().st_size}
                 for p in stage.rglob('*') if p.is_file()}
        manifest = {'version':1,'files':files,'models':models,'portable_key_sha256':fingerprint,
                    'oauth_included':False,'source_paths':{k:str(getattr(config,k)) for k in DATABASES}}
        write_json(stage/'transfer.json',manifest)
        bundle = Path(directory)/'state.tar.gz'
        with tarfile.open(bundle,'w:gz') as archive:
            for path in sorted(stage.rglob('*')):
                if path.is_file(): archive.add(path,arcname=str(path.relative_to(stage)),recursive=False)
        bundle.chmod(0o600)
        checksum = digest(bundle)
        os.link(bundle,output)
    return {'status':'exported','sha256':checksum,'portable_key_transferred':False,'oauth_included':False}


def import_state(archive: Path, expected_sha256: str, destination: Path, *, portable_key: Path,
                 runtime_root: Path | None = None, embedding_identity: str = '') -> dict:
    destination = private_destination(destination)
    if archive.is_symlink() or not archive.is_file() or archive.stat().st_mode & 0o077:
        raise ValueError('transfer archive must be an owner-only regular file')
    if digest(archive) != expected_sha256:
        raise ValueError('archive checksum mismatch')
    runtime = runtime_root or destination
    if not runtime.is_absolute() or runtime == Path('/') or '..' in runtime.parts:
        raise ValueError('runtime root must be an absolute state directory')
    with tempfile.TemporaryDirectory(prefix='.state-import-', dir=destination.parent) as directory:
        stage = Path(directory)/'payload';stage.mkdir(mode=0o700)
        safe_extract(archive,stage)
        manifest = json.loads((stage/'transfer.json').read_text())
        if manifest.get('version') != 1 or manifest.get('portable_key_sha256') != key_fingerprint(portable_key):
            raise ValueError('transfer version or separately supplied encryption key does not match')
        files = {str(p.relative_to(stage)) for p in stage.rglob('*') if p.is_file() and p.name!='transfer.json'}
        if files != set(manifest['files']) or not set(DATABASES.values()) <= files:
            raise ValueError('transfer file inventory mismatch')
        for name, expected in manifest['files'].items():
            p = stage/name
            if p.stat().st_size != expected['size'] or digest(p) != expected['sha256']:
                raise ValueError('transfer member checksum mismatch')
        for name in DATABASES.values(): sqlite_check(stage/name)
        with closing(sqlite3.connect(stage/'preferences.db')) as con:
            with con:
                for run in manifest['models']:
                    con.execute('UPDATE preference_model_runs SET artifact_path=? WHERE run_id=?', (str(runtime/'models'/'runs'/run),run))
        with closing(sqlite3.connect(stage/'policies.db')) as con:
            with con:
                con.execute('UPDATE proxy_students SET state_db=?,artifact_dir=?', (str(runtime/'preferences.db'),str(runtime/'models')))
        from .activation import GROUPS
        from .contracts import utc_now
        with closing(sqlite3.connect(stage/'applications.db')) as con:
            with con:
                for group in GROUPS:
                    con.execute('INSERT INTO automation_controls VALUES (?,0,0,?) ON CONFLICT(capability) DO UPDATE SET enabled=0,revision=revision+1,updated_at=excluded.updated_at',(group,utc_now()))
                con.execute('UPDATE schedule_specs SET enabled=0,enabled_since=NULL')
        for name in DATABASES.values():
            with closing(sqlite3.connect(stage/name)) as con: con.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        receipt = {'status':'imported_paused','archive_sha256':expected_sha256,
                   'oauth_reauthentication_required':True,'configuration_required':True,
                   'models':{run:('identity_matches' if embedding_identity and identity==embedding_identity else 'verify_embedding_identity')
                             for run,identity in manifest['models'].items()}}
        write_json(stage/'transfer-receipt.json',receipt)
        # Reserve the destination before publishing; an existing installation is never replaced.
        destination.mkdir(mode=0o700)
        try: os.replace(stage,destination)
        except BaseException:
            destination.rmdir(); raise
    return receipt


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    export=sub.add_parser('export');export.add_argument('--config',type=Path,required=True)
    export.add_argument('--output',type=Path,required=True);export.add_argument('--writers-stopped',action='store_true')
    restore=sub.add_parser('import');restore.add_argument('--archive',type=Path,required=True)
    restore.add_argument('--sha256',required=True);restore.add_argument('--destination',type=Path,required=True)
    restore.add_argument('--portable-key',type=Path,required=True);restore.add_argument('--runtime-root',type=Path)
    restore.add_argument('--embedding-identity',default='')
    args=parser.parse_args(argv)
    if args.command=='export':
        from .runtime import load_runtime_config
        result=export_state(load_runtime_config(args.config,required=True),args.output,writers_stopped=args.writers_stopped)
    else:
        result=import_state(args.archive,args.sha256,args.destination,portable_key=args.portable_key,
                            runtime_root=args.runtime_root,embedding_identity=args.embedding_identity)
    print(json.dumps(result,indent=2))

if __name__=='__main__': main()
