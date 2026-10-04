#!/usr/bin/env python3
"""Run the production healthcheck in the Hermes image without init or credentials.

An unavailable bridge must reach the Python healthcheck and fail closed. This
catches executable/PATH and packaging errors before publishing a release; live
gateway and Telegram acceptance remain separate.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile


def fixture_environment(directory):
    """Supply isolated values for every required Compose setting."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("JOB_SEARCH_")}
    env.update({k: directory for k in (
        "JOB_SEARCH_MAINTENANCE_DIR", "JOB_SEARCH_STATE_DIR", "JOB_SEARCH_PRIVATE_DIR",
        "JOB_SEARCH_COST_DIR", "JOB_SEARCH_TOOLCHAIN_DIR", "JOB_SEARCH_TOOL_RUNTIME_DIR",
        "JOB_SEARCH_NOTIFICATION_RUNTIME_DIR", "JOB_SEARCH_HERMES_DATA_DIR",
        "JOB_SEARCH_MCP_TOKEN_FILE")})
    env.update(JOB_SEARCH_HERMES_BASE_IMAGE="fixture/base@sha256:" + "a" * 64,
               JOB_SEARCH_NOTIFICATION_TARGET="fixture", JOB_SEARCH_TECTONIC_VERSION="fixture")
    return env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, default=Path(".cache/hermes-healthcheck.json"))
    args = parser.parse_args()
    report = {"passed": False, "scope": "healthcheck, private handoff, and native chief plugin contracts; fictional credentials only, no gateway or network"}
    try:
        image = json.loads(subprocess.check_output(["docker", "image", "inspect", args.image], text=True, timeout=20))[0]
        report["image_id"] = image["Id"]
        with tempfile.TemporaryDirectory(prefix="hermes-healthcheck-") as temp:
            env = fixture_environment(temp)
            spec = json.loads(subprocess.check_output([
                "docker", "compose", "-f", str(args.repo / "compose.cloud.yaml"),
                "-f", str(args.repo / "compose.hermes.yaml"), "config", "--format", "json"
            ], env=env, text=True, timeout=20))
            command = spec["services"]["hermes"]["healthcheck"]["test"]
            if command[0] != "CMD":
                raise RuntimeError("expected an exec-form production healthcheck")
            result = subprocess.run([
                "docker", "run", "--rm", "--platform", "linux/amd64", "--network", "none", "--read-only",
                "--cap-drop", "ALL", "--cap-add", "SETUID", "--cap-add", "SETGID",
                "--security-opt", "no-new-privileges:true", "--entrypoint", command[1], image["Id"], *command[2:]
            ], text=True, capture_output=True, timeout=30)
            report["exit_code"] = result.returncode
            if result.returncode != 2 or "job-search Hermes delivery: HermesDeliveryBridgeError" not in result.stderr:
                raise RuntimeError("production healthcheck did not reach the expected unavailable-bridge rejection: " + result.stderr[-2000:])
            # Exercise the supervisor's cross-UID shutdown with the actual
            # production capabilities, inside an isolated container PID namespace.
            probe = ["docker", "run", "--rm", "--platform", "linux/amd64", "--network", "none",
                     "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true"]
            for capability in spec["services"]["hermes"]["cap_add"]:
                probe.extend(["--cap-add", capability])
            probe.extend(["--entrypoint", "/opt/hermes/.venv/bin/python", image["Id"], "-c", '''
import os,pwd,signal,time
user=pwd.getpwnam('hermes'); read,write=os.pipe(); pid=os.fork()
if pid==0:
    os.close(read); os.setgid(user.pw_gid); os.setuid(user.pw_uid)
    os.write(write,b'1'); time.sleep(2); os._exit(0)
os.close(write); os.read(read,1)
try:
    os.kill(pid,signal.SIGTERM)
finally:
    _,status=os.waitpid(pid,0)
assert os.WIFSIGNALED(status) and os.WTERMSIG(status)==signal.SIGTERM
'''])
            stopped = subprocess.run(probe, text=True, capture_output=True, timeout=15)
            if stopped.returncode:
                raise RuntimeError("supervisor cannot stop its non-root child: " + stopped.stderr[-2000:])
            report["non_root_child_termination"] = True
            # Exercise the post-upstream permission hook and the upstream atomic
            # config writer using synthetic files only. No host state is mounted.
            private_probe = probe[:probe.index("--entrypoint")]
            private_probe.extend(["--tmpfs", "/tmp:rw,noexec,nosuid,nodev,mode=1777",
                                  "--env", "HERMES_HOME=/tmp/hermes-private",
                                  "--entrypoint", "/opt/hermes/.venv/bin/python", image["Id"], "-c", '''
import os,pwd,stat,subprocess
from pathlib import Path
home=Path(os.environ['HERMES_HOME']); home.mkdir()
user=pwd.getpwnam('hermes'); os.chown(home,user.pw_uid,user.pw_gid)
for name in ('config.yaml','.env'):
    path=home/name; path.write_text('fixture: true\\n' if name=='config.yaml' else '')
    os.chown(path,user.pw_uid,user.pw_gid); path.chmod(0o640)
hook=['/bin/sh','/etc/cont-init.d/017-job-search-private-config']
subprocess.run(hook,check=True)
for name in ('config.yaml','.env'):
    assert stat.S_IMODE((home/name).stat().st_mode)==0o600
subprocess.run(['/command/s6-setuidgid','hermes','/opt/hermes/.venv/bin/python','-c',
    'import os; from pathlib import Path; from hermes_cli.config import atomic_config_write; atomic_config_write(Path(os.environ["HERMES_HOME"])/"config.yaml", {"fixture":False})'],check=True)
assert stat.S_IMODE((home/'config.yaml').stat().st_mode)==0o600
(home/'config.yaml').unlink(); (home/'config.yaml').symlink_to('/etc/passwd')
assert subprocess.run(hook,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode!=0
'''])
            private = subprocess.run(private_probe, text=True, capture_output=True, timeout=30)
            if private.returncode:
                raise RuntimeError("Hermes private config permission check failed: " + private.stderr[-2000:])
            report["private_config_permissions"] = True
            for script, user, key in (
                ('native_hermes_contract.py', 'hermes', 'native_chief_plugin'),
                ('native_hermes_init_contract.py', '0:0', 'native_chief_private_handoff'),
            ):
                command = probe[:probe.index('--entrypoint')]
                command.extend(['--user', user, '--tmpfs', '/tmp:rw,noexec,nosuid,nodev,mode=1777',
                                '--env', 'PYTHONDONTWRITEBYTECODE=1',
                                '--volume', str(args.repo.resolve()) + ':/work:ro',
                                '--entrypoint', '/opt/hermes/.venv/bin/python', image['Id'],
                                '/work/tests/' + script])
                native = subprocess.run(command, text=True, capture_output=True, timeout=60)
                if native.returncode:
                    raise RuntimeError(script + ' failed: ' + native.stderr[-2000:])
                report[key] = True
            report["passed"] = True
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        report["error"] = str(error)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
