"""Private, repeatable installation enrollment and read-only asset diagnostics."""
from __future__ import annotations
from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
from datetime import datetime, timezone

DEFAULT_STATE = Path.home()/'.local/share/career-platform'

def inspect(config) -> dict:
    from .activation import controls
    from .ranking.refresh import inspect_policies
    from .runtime_readiness import runtime_readiness
    from .resume_lab.gateway import resume_lab_status
    assets={}
    for field in ('application_db','jobs_db','preference_db','proxy_db','resume_lab_db','resume_artifact_root','mcp_token_file'):
        p=getattr(config,field)
        assets[field]={'configured':bool(p),'exists':bool(p and p.exists())}
    career={'standard_count':0,'approved_profile':False,'status':'unavailable'}
    if config.resume_lab_db and config.resume_lab_db.is_file():
        try:
            with sqlite3.connect(config.resume_lab_db.resolve().as_uri()+'?mode=ro',uri=True) as con:
                career['standard_count']=con.execute('SELECT COUNT(*) FROM resume_standards WHERE active=1').fetchone()[0]
                row=con.execute('SELECT approved_revision_id FROM career_profile_state').fetchone()
                career['approved_profile']=bool(row and row[0]);career['status']='readable'
        except sqlite3.Error: career['status']='unreadable'
    code_root=Path(__file__).resolve().parents[1]
    return {'schema_version':1,'project_root_matches_running_code':config.project_root==code_root,
            'assets':assets,'ranking':inspect_policies(config.preference_db,config.proxy_db,config.jobs_db),
            'career':career,'resume':resume_lab_status(config),
            'automation':controls(config.application_db),'readiness':runtime_readiness(config)}

def initialize(config_path: Path, state_root: Path, project_root: Path) -> dict:
    from .runtime import load_runtime_config, RuntimeConfigV1
    from .db import prepare_database
    from .activation import initialize_paused
    from .scheduler import seed_default_schedules
    from .contracts import utc_now
    from .system import initialize_mcp_token
    from .secure_persistence import initialize_portable_master_key
    root=state_root.expanduser().absolute(); source=project_root.resolve()
    target=config_path.expanduser().absolute()
    if root.is_symlink() or target.is_symlink():
        raise ValueError('setup destinations must not be symlinks')
    root, target = root.resolve(), target.resolve()
    if root==source or source in root.parents or target==source or source in target.parents:
        raise ValueError('private state and config must be outside the repository')
    if target.exists():
        existing=load_runtime_config(target,required=True)
        if existing.application_db.parent != root.resolve():
            raise ValueError('existing configuration has a different state root; nothing changed')
        return {'status':'already_initialized','configuration_written':False,'inspection':inspect(existing)}
    root.mkdir(parents=True,exist_ok=True,mode=0o700);os.chmod(root,0o700)
    if any(root.glob('*.db')):
        raise ValueError('state directory already contains databases; use a separate enrollment directory')
    config=replace(RuntimeConfigV1.defaults(source),application_db=root/'applications.db',jobs_db=root/'jobs.db',
        preference_db=root/'preferences.db',proxy_db=root/'policies.db',resume_lab_db=root/'career.db',
        resume_artifact_root=root/'resume-artifacts',mcp_token_file=root/'private/mcp-token',
        portable_encryption_key_file=root/'private/portable-master-key',log_dir=root/'logs',
        shortlist_policy='selective',resume_mode='standard',outlook_new_messages_only=True,mail_recruiting_only=True,
        inference_usage_limits={'daily_requests':1000,'daily_tokens':2000000,'max_inflight':1})
    config.resume_artifact_root.mkdir(mode=0o700)
    config.log_dir.mkdir(mode=0o700)
    prepare_database(config.application_db,utc_now());initialize_paused(config.application_db)
    seed_default_schedules(config.application_db,datetime.now(timezone.utc),config.environment({}))
    from .resume_lab.career_store import CareerStore
    CareerStore(config.resume_lab_db)
    initialize_mcp_token(config.mcp_token_file)
    initialize_portable_master_key(config.portable_encryption_key_file)
    from dataclasses import asdict
    value=asdict(config);value.pop('source_path',None)
    target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as stream:
        json.dump(value,stream,default=str,indent=2);stream.write('\n')
    return {'status':'initialized','recurring_work_enabled':False,'configuration_written':True}
