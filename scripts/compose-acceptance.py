#!/usr/bin/env python3
"""Run isolated production-Compose acceptance with synthetic state and no providers.

Requires an already-built image and a Linux Tectonic executable/offline bundle.
Never uses the owner's config, state, Docker Compose project, or cloud accounts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import secrets
import shutil
import subprocess
import tempfile
import time

SERVICES = ("tools", "dashboard", "mcp", "core", "model")


def run(argv, *, env=None, input=None, timeout=180):
    result = subprocess.run(argv, env=env, input=input, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{argv[0]} command failed ({result.returncode}): {result.stderr[-4000:]} {result.stdout[-2000:]}")
    return result.stdout.strip()


def private_json(path, value):
    path.write_text(json.dumps(value) + "\n")
    path.chmod(0o600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--image", default="career-platform:completion")
    parser.add_argument("--toolchain-dir", type=Path, required=True)
    parser.add_argument("--tectonic-version", default="0.15.0")
    parser.add_argument("--output", type=Path, default=Path(".cache/compose-acceptance"))
    parser.add_argument("--require-linux-host", action="store_true")
    args = parser.parse_args()
    repo, toolchain, output = args.repo.resolve(), args.toolchain_dir.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    for name in ("tectonic", "tectonic.bundle"):
        if not (toolchain / name).is_file() or (toolchain / name).is_symlink():
            raise SystemExit(f"Toolchain must contain a regular {name} file")
    daemon = json.loads(run(["docker", "info", "--format", "{{json .}}"], timeout=20))
    linux_host = platform.system() == "Linux" and "desktop" not in daemon["OperatingSystem"].lower()
    if args.require_linux_host and not linux_host:
        raise SystemExit("Linux host acceptance requires native Linux Docker, not Docker Desktop")
    image_info = json.loads(run(["docker", "image", "inspect", args.image]))[0]
    image = image_info["Id"]  # Freeze this run even if another task rebuilds the tag.
    machine = image_info["Architecture"]
    if image_info.get("Os") != "linux" or machine != "amd64":
        raise SystemExit("The pinned acceptance toolchain requires an image built with --platform linux/amd64")
    source = run(["git", "-C", str(repo), "rev-parse", "HEAD"])
    image_source = (image_info.get("Config", {}).get("Labels") or {}).get("org.opencontainers.image.revision")
    image_environment = dict(item.split("=", 1) for item in image_info.get("Config", {}).get("Env", []) if "=" in item)
    if image_source != source or image_environment.get("JOB_SEARCH_SOURCE_REVISION") != source:
        raise SystemExit("Acceptance image must be built from this checkout with SOURCE_REVISION set to its full commit SHA")
    if run(["git", "-C", str(repo), "status", "--porcelain"]):
        raise SystemExit("Commit source changes before acceptance so the image and host helpers identify the same reviewed revision")
    project = "career-acceptance-" + secrets.token_hex(5)
    report = {"passed": False, "source_sha": source, "image_id": image, "image_architecture": machine, "image_source_sha": image_source, "working_tree_dirty": False,
              "host": platform.system(), "docker_os": daemon["OperatingSystem"],
              "linux_host_verified": linux_host, "checks": [],
              "limitations": ["No live ATS, Outlook, Telegram, inference, Tailscale, AWS IAM, alarms, or cloud disk recovery is exercised.",
                              "Restore uses generated fixture secrets; live secret-manager recovery remains external acceptance."]}
    if not linux_host:
        report["limitations"].append("Docker Desktop verifies container behavior but does not establish native Linux host networking or AWS behavior.")
        report["limitations"].append("Docker Desktop uses VM-native named volumes for the tool socket and SQLite state, substitutes fixture ownership during restore, and copies quiescent restored state into a separate empty VM volume; native Linux acceptance uses the production binds and ownership implementation unchanged.")
    temporary = Path(tempfile.mkdtemp(prefix="career-compose-acceptance-"))
    env = dict(os.environ)
    # Only this generated configuration enters containers. Clear accidental Compose
    # overrides; inherited provider credentials are not passed through the file.
    for key in list(env):
        if key.startswith("JOB_SEARCH_"):
            env.pop(key)
    env.update({"JOB_SEARCH_IMAGE": image, "JOB_SEARCH_PLATFORM": "linux/" + machine,
                "JOB_SEARCH_UID": str(os.getuid()), "JOB_SEARCH_GID": str(os.getgid()),
                "JOB_SEARCH_TECTONIC_VERSION": args.tectonic_version})
    compose_file = temporary / "compose.cloud.yaml"
    shutil.copyfile(repo / "compose.cloud.yaml", compose_file)
    compose_prefix = ["docker", "compose", "--project-name", project, "--file", str(compose_file)]
    mail_overlay = repo / "compose.mail.yaml"
    if mail_overlay.is_file():
        compose_prefix.extend(["--file", str(mail_overlay)])
    socket_volume = None
    state_volumes = {}
    created_volumes = []
    if not linux_host:
        # macOS file sharing cannot chmod a Unix socket. Preserve the service's
        # socket mode/owner checks inside a VM-native volume for this local run.
        # SQLite WAL files also require shared native locking across containers.
        socket_volume = project + "-tools"
        state_volumes = {name: project + "-state-" + name for name in ("original", "restored")}
        env["JOB_SEARCH_ACCEPTANCE_STATE_VOLUME"] = state_volumes["original"]
        override = {"services": {}, "volumes": {"acceptance-tools": {"external": True, "name": socket_volume}}}
        override["volumes"]["acceptance-state"] = {"external": True, "name": "${JOB_SEARCH_ACCEPTANCE_STATE_VOLUME:?set fixture state volume}"}
        for service in ("tools", "dashboard", "core", "model"):
            override["services"][service] = {"volumes": [{"type": "volume", "source": "acceptance-tools", "target": "/run/job-search-tools", "read_only": service != "tools"}]}
        # Only services that already have the production state mount receive it.
        # Document tools retain their existing isolated filesystem boundary.
        for service in ("initialize", "dashboard", "mcp", "core", "model"):
            override["services"].setdefault(service, {}).setdefault("volumes", []).append(
                {"type": "volume", "source": "acceptance-state", "target": "/var/lib/job-search", "volume": {"nocopy": True}})
        override_file = temporary / "desktop.override.json"
        private_json(override_file, override)
        compose_prefix.extend(["--file", str(override_file)])

    def compose(*argv, timeout=180, input=None):
        return run([*compose_prefix, *argv], env=env, timeout=timeout, input=input)

    def execute(service, code):
        return json.loads(compose("exec", "-T", service, "python", "-c", code))

    def setup_data(data, *, copy_tools=True):
        for name in ("state", "private", "hermes", "toolchain", "runtime/tools", "runtime/notifications", "maintenance"):
            (data / name).mkdir(parents=True, exist_ok=True, mode=0o700)
        private_json(data / "maintenance/gate.json", {"version": 1, "allowed_services": list(SERVICES), "draining": False, "initialize_operation": "acceptance"})
        (data / "maintenance").chmod(0o755)
        (data / "maintenance/gate.json").chmod(0o644)
        if copy_tools:
            for name in ("tectonic", "tectonic.bundle"):
                shutil.copyfile(toolchain / name, data / "toolchain" / name)
            (data / "toolchain" / "tectonic").chmod(0o555)
            (data / "toolchain" / "tectonic.bundle").chmod(0o444)
        state = "/var/lib/job-search"
        config = {"version": 1, "project_root": "/opt/job-search", "application_db": state + "/applications.db",
                  "jobs_db": state + "/jobs.db", "preference_db": state + "/preferences.db", "proxy_db": state + "/proxy.db",
                  "resume_lab_db": state + "/resumes.db", "resume_artifact_root": state + "/artifacts",
                  "tool_service_socket": "/run/job-search-tools/tools.sock", "resume_tectonic_version": args.tectonic_version,
                  "mcp_token_file": "/run/job-search/mcp-token", "portable_encryption_key_file": "/run/job-search/portable-master-key",
                  "dashboard_port": 28766, "mcp_port": 28767, "log_dir": state + "/logs", "timezone": "America/Chicago",
                  "outlook_client_id": "", "scraper_contact": "", "hermes_telegram_target": "",
                  "shortlist_notifications_enabled": False, "remote_mail_inference_enabled": False}
        private_json(data / "private/config.json", config)
        for name in ("inference.json", "resume-model.json", "mail-inference.json"):
            private_json(data / "private" / name, {})  # Mounted, but intentionally unconfigured.
        for name, value in (("mcp-token", secrets.token_urlsafe(48).encode()), ("portable-master-key", secrets.token_bytes(32)), ("runpod-api-key", b"unused-fixture-key")):
            (data / "private" / name).write_bytes(value)
            (data / "private" / name).chmod(0o600)
        private_json(data / "materialized-secrets.json", {})
        (data / "private/openrouter-api-key").write_text("fictional-mail-key\n")
        (data / "private/openrouter-api-key").chmod(0o600)
        return config

    def use_data(data):
        env.update({"JOB_SEARCH_MAINTENANCE_DIR": str(data / "maintenance"), "JOB_SEARCH_INITIALIZE_OPERATION": "acceptance", "JOB_SEARCH_STATE_DIR": str(data / "state"), "JOB_SEARCH_PRIVATE_DIR": str(data / "private"),
                    "JOB_SEARCH_TOOLCHAIN_DIR": str(data / "toolchain"), "JOB_SEARCH_TOOL_RUNTIME_DIR": str(data / "runtime/tools"),
                    "JOB_SEARCH_NOTIFICATION_RUNTIME_DIR": str(data / "runtime/notifications")})
        if state_volumes:
            env["JOB_SEARCH_ACCEPTANCE_STATE_VOLUME"] = state_volumes[data.name]

    def healthy(services=SERVICES):
        raw = compose("ps", "--format", "json")
        rows = json.loads(raw) if raw.startswith("[") else [json.loads(line) for line in raw.splitlines() if line.strip()]
        if isinstance(rows, dict):
            rows = [rows]
        values = {row["Service"]: row for row in rows}
        return all(values.get(name, {}).get("Health") == "healthy" for name in services)

    def await_healthy(services=SERVICES):
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            if healthy(services):
                return
            time.sleep(2)
        raise RuntimeError("Production services did not become healthy within 150 seconds")

    def ops_helper(action, data, **values):
        # Host operations need ownership privileges. Only this isolated fixture
        # directory is writable in the helper; network access is disabled.
        payload = {"action": action, "data": str(data.relative_to(temporary)), "uid": os.getuid(), "gid": os.getgid(), "native_linux": linux_host, "tectonic_version": args.tectonic_version, **values}
        helper = r'''
import json,os,shutil,sys
from pathlib import Path
sys.path.insert(0,"/source")
from job_search import aws_ops as ops
p=json.loads(sys.stdin.read()); root=Path("/acceptance"); data=root/p["data"]
release_root=root/(p["data"]+"-release-root"); release=release_root/"releases"/"acceptance"
release.mkdir(parents=True,exist_ok=True)
ops.write_json(release/"release.json",{"version":1,"release_id":"acceptance","app_image":"fixture/app@sha256:"+"a"*64,"hermes_image":"fixture/hermes@sha256:"+"b"*64,"hermes_base_image":"fixture/base@sha256:"+"c"*64,"tectonic_version":p["tectonic_version"]})
c={"data_root":str(data),"release_root":str(release_root),"secret_arns":{},"app_uid":p["uid"],"app_gid":p["gid"]}
if not p["native_linux"]:
 # macOS shared mounts cannot establish Linux root ownership. Only this test
 # helper substitutes fixture permissions; production restore is unchanged.
 def fixture_ownership(config):
  for name in ("state","private","hermes","runtime"):
   folder=data/name; folder.mkdir(mode=0o700,exist_ok=True)
  for name in ("tectonic","tectonic.bundle"):
   (data/"toolchain"/name).chmod(0o555 if name=="tectonic" else 0o444)
 ops.chown_runtime=fixture_ownership
if p["action"]=="backup":
 (release_root/"current").symlink_to(release)
 result=ops.backup_unlocked(c,paused=True,upload=False)
 result["bundle"]=str((data/"backups"/(result["backup_id"]+".tar.gz")).relative_to(root))
elif p["action"]=="restore":
 result=ops.restore_unlocked(c,root/p["bundle"],p["sha256"])
 assert json.loads((data/"activation.json").read_text())["enabled"] is False
 if not p["native_linux"]:
  # A mounted state directory cannot participate in restore's atomic rename.
  # Keep that transaction intact, then transport its quiescent result into a
  # fresh VM-native volume before any application container is started.
  destination=Path("/restored-state")
  if any(destination.iterdir()): raise RuntimeError("restored fixture volume is not empty")
  os.chown(destination,0,0)
  shutil.copytree(data/"state",destination,dirs_exist_ok=True,copy_function=shutil.copy2)
  for item in [destination,*destination.rglob("*")]:
   if item.is_symlink(): raise RuntimeError("restored fixture state contains a symlink")
   os.chown(item,p["uid"],p["gid"])
elif p["action"]=="bad-checksum":
 try: ops.restore_unlocked(c,root/p["bundle"],"0"*64)
 except ops.OpsError: result={"rejected":True}
 else: raise AssertionError("bad checksum accepted")
elif p["action"]=="review-gate":
 assert json.loads((data/"maintenance/gate.json").read_text())["allowed_services"]==[]
 ops.set_gate(c,["tools","dashboard","mcp"],initialize="acceptance")
 result={"status":"fixture_review_allowed"}
else: raise ValueError("unknown action")
print(json.dumps(result))
'''
        state_mount = []
        if state_volumes and action == "backup":
            state_mount = ["--volume", f"{state_volumes[data.name]}:/acceptance/{data.name}/state"]
        elif state_volumes and action == "restore":
            state_mount = ["--volume", f"{state_volumes[data.name]}:/restored-state"]
        # Host operations chmod fixture directories created by the runner UID.
        # FOWNER is confined to this networkless helper's temporary data mount.
        return json.loads(run(["docker", "run", "--rm", "--interactive", "--platform", "linux/" + machine, "--network", "none", "--user", "0:0", "--cap-drop", "ALL", "--cap-add", "CHOWN", "--cap-add", "DAC_OVERRIDE", "--cap-add", "FOWNER",
                               "--volume", f"{temporary}:/acceptance", "--volume", f"{repo}:/source:ro", *state_mount, "--entrypoint", "python", image, "-c", helper], input=json.dumps(payload)))

    try:
        for volume in ([socket_volume, *state_volumes.values()] if socket_volume else []):
            run(["docker", "volume", "create", volume])
            created_volumes.append(volume)
            run(["docker", "run", "--rm", "--network", "none", "--user", "0:0", "--volume", volume + ":/runtime", "--entrypoint", "python", image, "-c",
                 "import os; os.chown('/runtime'," + str(os.getuid()) + "," + str(os.getgid()) + "); os.chmod('/runtime',0o700)"])
        data = temporary / "original"
        setup_data(data)
        use_data(data)
        # Exercise Docker's actual tmpfs defaults using the merged Hermes service
        # settings. No Hermes credentials, state, gateway or network are needed.
        hermes_env = dict(env, JOB_SEARCH_HERMES_BRIDGE_IMAGE=image,
                          JOB_SEARCH_HERMES_BASE_IMAGE="fixture/base@sha256:" + "a" * 64,
                          JOB_SEARCH_HERMES_DATA_DIR=str(data / "hermes"),
                          JOB_SEARCH_MCP_TOKEN_FILE=str(data / "private/mcp-token"),
                          JOB_SEARCH_NOTIFICATION_TARGET="fixture")
        merged = json.loads(run(["docker", "compose", "-f", str(repo / "compose.cloud.yaml"),
                                 "-f", str(repo / "compose.hermes.yaml"), "config", "--format", "json"], env=hermes_env))
        probe = ["docker", "run", "--rm", "--network", "none", "--user", "0:0",
                 "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true"]
        for mount in merged["services"]["hermes"]["tmpfs"]:
            probe.extend(["--tmpfs", mount])
        probe.extend(["--entrypoint", "python", image, "-c", '''
import json,shutil,subprocess
from pathlib import Path
mounts={row.split()[1]:row.split() for row in Path('/proc/mounts').read_text().splitlines()}
for directory in ('/run','/tmp'):
    row=mounts[directory]
    assert row[2]=='tmpfs' and {'nosuid','nodev'} <= set(row[3].split(','))
    shutil.copyfile('/bin/true',directory+'/acceptance-executable')
    Path(directory+'/acceptance-executable').chmod(0o700)
subprocess.run(['/run/acceptance-executable'],check=True)
try:
    subprocess.run(['/tmp/acceptance-executable'],check=True)
except PermissionError:
    pass
else:
    raise AssertionError('/tmp unexpectedly permits executable files')
print(json.dumps({'supervisor_exec':True,'temporary_exec_blocked':True}))
'''])
        assert json.loads(run(probe)) == {"supervisor_exec": True, "temporary_exec_blocked": True}
        report["checks"].append("Hermes production tmpfs permits supervisor executables under /run while /tmp remains non-executable; neither mount permits devices or setuid.")
        version = run(["docker", "run", "--rm", "--network", "none", "--volume", f"{data / 'toolchain'}:/toolchain:ro",
                       "--entrypoint", "/toolchain/tectonic", image, "--version"])
        assert args.tectonic_version in version
        report["toolchain"] = {"version": version, **{name + "_sha256": hashlib.sha256((data / "toolchain" / name).read_bytes()).hexdigest() for name in ("tectonic", "tectonic.bundle")}}
        compose("up", "--detach", "--no-build", *SERVICES, timeout=200)
        await_healthy()
        report["checks"].append("All five production Compose services are healthy with generated fixture configuration.")
        if mail_overlay.is_file():
            for service in SERVICES:
                mounts = execute(service, 'import json; from pathlib import Path; print(json.dumps({n:Path("/run/job-search",n).is_file() for n in ("mail-inference.json","openrouter-api-key")}))')
                assert all(value == (service == "core") for value in mounts.values()), service
            report["checks"].append("Only core receives the dedicated mail profile and OpenRouter key; all other service boundaries remain unchanged.")

        dashboard = execute("dashboard", 'import json,urllib.request; print(json.dumps(json.load(urllib.request.urlopen("http://127.0.0.1:28766/api/v1/ops"))))')
        assert dashboard["health"]
        assets = execute("dashboard", '''
import json,urllib.request
from job_search.dashboard import STATIC_ROUTES,WEB_ROOT
checked=[]
for route,(filename,content_type) in STATIC_ROUTES.items():
    with urllib.request.urlopen("http://127.0.0.1:28766"+route,timeout=10) as response:
        assert response.status==200
        assert response.headers["Content-Type"]==content_type
        assert response.read()==(WEB_ROOT/filename).read_bytes()
    checked.append(route)
print(json.dumps(checked))
''')
        report["dashboard_assets"] = assets
        report["checks"].append("Every dashboard static route serves the packaged asset bytes from the running image.")
        if linux_host:
            report["dashboard_browser"] = json.loads(run([
                "node", str(repo / "tests/browser/test_deployed_dashboard.mjs"), "http://127.0.0.1:28766"
            ], timeout=90))
            report["checks"].append("A real browser renders every main page and the career profile from the production image without script or asset errors.")
        mcp = execute("mcp", 'import json; from pathlib import Path; from job_search.cloud import check_http_health; from job_search.runtime import load_runtime_config; print(json.dumps({"healthy":check_http_health(load_runtime_config(Path("/run/job-search/config.json")),"mcp")}))')
        assert mcp["healthy"]
        report["checks"].append("Real dashboard HTTP and authenticated MCP health probes succeed inside their containers.")
        pdf = execute("dashboard", r'''
import json
from pathlib import Path
from job_search.tool_service import RemoteTectonicCompiler,RemotePypdfExtractor
from job_search.runtime import load_runtime_config
socket=Path("/run/job-search-tools/tools.sock")
config=load_runtime_config(Path("/run/job-search/config.json"))
result=RemoteTectonicCompiler(socket,config.resume_tectonic_version).compile("\\documentclass{article}\n\\begin{document}\nSynthetic Career Platform acceptance resume.\\end{document}\n")
extracted=RemotePypdfExtractor(socket).extract(result.pdf_bytes)
assert "Synthetic Career Platform" in extracted.logical_text
Path("/var/lib/job-search/acceptance.pdf").write_bytes(result.pdf_bytes)
print(json.dumps({"pdf_sha256":result.pdf_sha256,"bytes":len(result.pdf_bytes),"pages":extracted.pages}))
''')
        report["pdf"] = pdf
        compose("cp", "dashboard:/var/lib/job-search/acceptance.pdf", str(output / "acceptance.pdf"))
        report["checks"].append("A real PDF compiles and extracts through the networkless production Unix-socket tool service.")

        compose("kill", "--signal", "SIGKILL", "core", "model")
        # Simulate a worker interrupted after a local-only claim. Its lease is in
        # the past; provider writes are never injected or retried by this check.
        seed = execute("dashboard", r'''
import json,sqlite3
from datetime import datetime,timedelta,timezone
from job_search.contracts import JobSnapshot,MutationContext,RecommendationProvenance
from job_search.service import JobSearchLedger
db="/var/lib/job-search/applications.db"
app=JobSearchLedger(db).start_application(JobSnapshot("greenhouse","fixture-job","fixture-family","Platform Engineer","Synthetic Employer","synthetic","https://example.test/job"),RecommendationProvenance(),MutationContext("acceptance-app","user","fixture"))["application"]["application_id"]
old=(datetime.now(timezone.utc)-timedelta(minutes=5)).isoformat().replace("+00:00","Z")
with sqlite3.connect(db) as con:
 con.execute("UPDATE worker_leases SET expires_at=?",(old,))
 con.execute("INSERT INTO work_items(work_id,task_kind,dedupe_key,payload_json,status,priority,due_at,attempts,max_attempts,lease_owner,lease_token,lease_expires_at,created_at,lane) VALUES('acceptance-recovery','system.worker_tick','acceptance-recovery','{}','running',100,?,1,3,'terminated-fixture-worker','expired-fixture-token',?,?,'core')",(old,old,old))
print(json.dumps({"application_id":app}))
''')
        compose("up", "--detach", "--no-build", "core", "model")
        await_healthy()
        # Liveness accepts a worker that is still starting or running its first
        # tick. Wait for durable recovery evidence independently of that probe.
        deadline = time.monotonic() + 150
        while True:
            recovered = execute("dashboard", 'import json,sqlite3; c=sqlite3.connect("/var/lib/job-search/applications.db"); c.row_factory=sqlite3.Row; print(json.dumps(dict(c.execute("SELECT status,attempts FROM work_items WHERE work_id=\'acceptance-recovery\'").fetchone())))')
            if recovered["status"] in {"succeeded", "dead", "cancelled"} or recovered["attempts"] > 2:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Worker did not recover expired work within 150 seconds: {recovered}")
            time.sleep(2)
        assert recovered == {"status": "succeeded", "attempts": 2}, recovered
        report["checks"].append("After SIGKILL and restart, the core worker recovers an expired local-work lease exactly once; both worker lanes recover health.")
        report["recovered_work"] = recovered

        compose("stop", "--timeout", "15", *SERVICES)
        backup = ops_helper("backup", data)
        restored = temporary / "restored"
        setup_data(restored, copy_tools=False)
        assert ops_helper("bad-checksum", restored, bundle=backup["bundle"])["rejected"]
        restore = ops_helper("restore", restored, bundle=backup["bundle"], sha256=backup["sha256"])
        assert restore["status"] == "restored_paused"
        report["backup"] = {"sha256": backup["sha256"], "status": restore["status"]}
        report["checks"].append("Production backup/restore functions reject a bad checksum and restore a quiesced snapshot into a fresh paused fixture directory.")
        # A successful restore intentionally leaves the startup gate closed.
        # Explicitly allow only fixture review services; never recurring workers.
        # The root-owned gate must be changed by the host-operations helper,
        # including on native Linux where the runner cannot overwrite it.
        assert ops_helper("review-gate", restored)["status"] == "fixture_review_allowed"
        use_data(restored)
        compose("up", "--detach", "--no-build", "tools", "dashboard", "mcp", timeout=200)
        await_healthy(("tools", "dashboard", "mcp"))
        restored_state = execute("dashboard", 'import json; from job_search.service import JobSearchLedger; print(json.dumps(JobSearchLedger("/var/lib/job-search/applications.db").get_application_timeline(' + repr(seed["application_id"]) + ')))')
        assert restored_state["application"]["title_snapshot"] == "Platform Engineer"
        assert len(restored_state["events"]) == 1
        result = execute("dashboard", 'import hashlib,json; from pathlib import Path; print(json.dumps({"sha256":hashlib.sha256(Path("/var/lib/job-search/acceptance.pdf").read_bytes()).hexdigest()}))')
        assert result["sha256"] == pdf["pdf_sha256"]
        running = compose("ps", "--services", "--status", "running").splitlines()
        assert "core" not in running and "model" not in running
        report["checks"].append("Restored dashboard reads the same application event and PDF bytes; core/model remain stopped pending activation.")
        report["passed"] = True
    except Exception as error:
        report["error"] = str(error)
        try:
            (output / "compose.log").write_text(compose("logs", "--no-color", "--tail", "80", timeout=20))
        except Exception:
            pass
    finally:
        try:
            compose("down", "--timeout", "15", "--remove-orphans", timeout=90)
        except Exception as error:
            report["cleanup_error"] = str(error)
            report["passed"] = False
        for volume in created_volumes:
            try:
                run(["docker", "volume", "rm", volume])
            except Exception as error:
                report["cleanup_error"] = str(error)
                report["passed"] = False
        # Restore made selected fixture directories root-owned, as production
        # requires. Relinquish only this temporary mount for host cleanup.
        try:
            if linux_host:
                run(["docker", "run", "--rm", "--network", "none", "--user", "0:0", "--volume", f"{temporary}:/acceptance", "--entrypoint", "python", image, "-c",
                     "import os; from pathlib import Path; root=Path('/acceptance'); paths=[root,*root.rglob('*')]; [(os.chown(p," + str(os.getuid()) + "," + str(os.getgid()) + "),os.chmod(p,0o700 if p.is_dir() else 0o600)) for p in paths if not p.is_symlink()]"])
            shutil.rmtree(temporary)
        except Exception as error:
            report["cleanup_error"] = str(error)
            report["passed"] = False
        if report["passed"]:
            (output / "compose.log").unlink(missing_ok=True)
        private_json(output / "results.json", report)
    print(json.dumps({"passed": report["passed"], "checks": len(report["checks"]), "report": str(output / "results.json")}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
