"""Offline root-init credential handoff check using a fictional token only."""
import importlib.util
import os
from pathlib import Path
import pwd
import subprocess
import tempfile


def main():
    spec=importlib.util.spec_from_file_location('career_bootstrap','/work/job_search/interactions/bootstrap.py')
    bootstrap=importlib.util.module_from_spec(spec);spec.loader.exec_module(bootstrap)
    owner=pwd.getpwnam('hermes')
    with tempfile.TemporaryDirectory() as directory:
        os.chmod(directory,0o755)
        root=Path(directory);source=root/'mounted-token'
        source.write_text('fictional-test-token-'*3);os.chmod(source,0o600);os.chown(source,10001,10001)
        bootstrap.install_runtime_token(source,str(root/'private-runtime'),str(root/'s6-environment'))
        target=root/'private-runtime'/'token'
        assert target.stat().st_uid==owner.pw_uid and target.stat().st_mode&0o077==0
        assert (root/'s6-environment'/'JOB_SEARCH_INTERACTION_TOKEN_FILE').read_text()==str(target)
        def demote():
            os.setgroups([]);os.setgid(owner.pw_gid);os.setuid(owner.pw_uid)
        script='from pathlib import Path; import sys; assert len(Path(sys.argv[1]).read_text())>=32; print("ok (Hermes user reads private runtime credential copied from app-owned mount)")'
        subprocess.run(['/opt/hermes/.venv/bin/python','-c',script,str(target)],preexec_fn=demote,check=True)


if __name__=='__main__':main()
