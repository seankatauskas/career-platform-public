"""Image startup check and explicit activation for the restricted career profile."""
import importlib.util
import os
from pathlib import Path
import sys
import stat
import tempfile


def install_runtime_token(source,root='/run/job-search-interactions',environment='/run/s6/container_environment',owner_name='hermes'):
    """Root init copies the mounted credential into Hermes-owned private tmpfs."""
    import pwd
    owner=pwd.getpwnam(owner_name)
    fd=os.open(source,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode&0o077 or info.st_size>4096:
            raise RuntimeError('interaction credential mount must be an owner-only regular file')
        value=os.read(fd,4096).strip()
        if len(value)<32 or any(chr(c).isspace() for c in value):
            raise RuntimeError('invalid interaction credential mount')
    finally:os.close(fd)
    directory=Path(root)
    if directory.is_symlink():raise RuntimeError('unsafe interaction runtime directory')
    directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    os.chmod(directory,0o700);os.chown(directory,owner.pw_uid,owner.pw_gid)
    descriptor,temporary=tempfile.mkstemp(prefix='.token-',dir=directory)
    try:
        with os.fdopen(descriptor,'wb') as target:
            target.write(value);target.flush();os.fsync(target.fileno());os.fchmod(target.fileno(),0o600);os.fchown(target.fileno(),owner.pw_uid,owner.pw_gid)
        os.replace(temporary,directory/'token')
    finally:
        if os.path.exists(temporary):os.unlink(temporary)
    env=Path(environment)
    if env.is_symlink():raise RuntimeError('unsafe s6 environment directory')
    env.mkdir(parents=True,exist_ok=True)
    path=env/'JOB_SEARCH_INTERACTION_TOKEN_FILE'
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o644)
    with os.fdopen(fd,'w') as output:output.write(str(directory/'token'))


def main():
    sys.path.insert(0,'/opt/hermes')
    from hermes_cli.plugins import PluginContext,get_pre_tool_call_directive
    from plugins.platforms.telegram.adapter import TelegramAdapter
    if not callable(getattr(PluginContext,'register_platform_handler',None)) or not callable(getattr(TelegramAdapter,'_wire_plugin_handlers',None)):
        raise RuntimeError('Hermes image lacks the reviewed native handler contract')
    spec = importlib.util.spec_from_file_location('career_boot_runtime','/opt/hermes/plugins/career-interactions/runtime.py')
    runtime = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runtime)
    allowed = runtime.allowed_tools_from_env()
    runtime.InteractionClient(os.environ['JOB_SEARCH_INTERACTION_URL'],os.environ['JOB_SEARCH_INTERACTION_TOKEN_FILE'])
    runtime.read_token(os.environ['JOB_SEARCH_INTERACTION_TOKEN_FILE'])
    if runtime.tool_policy(allowed,tool_name='terminal').get('action') != 'block':
        raise RuntimeError('career policy failed closed check')
    import yaml
    home = Path(os.environ.get('HERMES_HOME','/opt/data'))
    path = home / 'config.yaml'
    if path.is_symlink():
        raise RuntimeError('Hermes configuration cannot be a symlink')
    config = yaml.safe_load(path.read_text()) or {}
    plugins = config.setdefault('plugins',{})
    enabled = plugins.setdefault('enabled',[])
    if not isinstance(enabled,list):
        raise RuntimeError('plugins.enabled must be a list')
    if 'career-interactions' not in enabled:
        enabled.append('career-interactions')
    if 'career-interactions' in plugins.get('disabled',[]):
        raise RuntimeError('career interactions cannot run with the mandatory policy disabled')
    # Disable project-sourced plugins; image plugin supplies all trusted mutations.
    if (home/'plugins'/'career-interactions').exists():
        raise RuntimeError('a user plugin cannot override the image career policy')
    temp = home / '.career-config.tmp'
    fd = os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as output:
        yaml.safe_dump(config,output,sort_keys=False)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temp,path)
    from hermes_cli.plugins import discover_plugins
    discover_plugins(force=True)
    for denied in ('terminal','execute_code','python','read_file','write_file','web_fetch','spawn_agent'):
        if get_pre_tool_call_directive(denied,{})[0] != 'block':
            raise RuntimeError('career tool policy was not installed')


if __name__ == '__main__':
    if sys.argv[1:] == ['--install-runtime-token']:
        install_runtime_token(os.environ['JOB_SEARCH_INTERACTION_TOKEN_FILE'])
    else:
        main()
