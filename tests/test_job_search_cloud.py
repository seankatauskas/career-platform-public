#!/usr/bin/env python3
"""Offline checks for the cross-platform cloud process loops."""

from __future__ import annotations

import http.client
import io
import json
import os
import re
import runpy
import shutil
import subprocess
import tempfile
import threading
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from pathlib import PurePosixPath
from types import SimpleNamespace

from job_search.cloud import (
    _health_path,
    _worker_health_record,
    _write_health,
    check_worker_health,
    run_worker_loop,
    serve_http,
)
from job_search.runtime import RuntimeConfigV1

NOW = datetime(2026, 9, 3, 18, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


def _docker_context_includes(rules: str, candidate: str) -> bool:
    """Evaluate this repository's simple allowlist patterns with Docker ordering."""

    included = True
    for raw in rules.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        pattern = line[1:] if negated else line
        directory_rule = pattern.endswith("/")
        pattern = pattern.rstrip("/")
        if directory_rule:
            matched = candidate == pattern or candidate.startswith(pattern + "/")
        else:
            matched = PurePosixPath(candidate).match(pattern)
        if matched:
            included = negated
    return included


def test_cost_snapshot_mount_is_dashboard_only_read_only_and_not_a_secret_directory() -> None:
    compose = (ROOT / "compose.cloud.yaml").read_text()
    dashboard = compose.split("\n  dashboard:\n", 1)[1].split("\n  mcp:\n", 1)[0]
    assert compose.count("JOB_SEARCH_COST_DIR") == 2  # variable and required-variable message
    assert "JOB_SEARCH_COST_DIR:?set JOB_SEARCH_COST_DIR" in dashboard
    assert "target: /run/job-search-costs\n        read_only: true" in dashboard
    assert "JOB_SEARCH_COST_SNAPSHOT: /run/job-search-costs/snapshot.json" in dashboard
    assert "cost_collector" not in compose


def test_hermes_acceptance_supplies_all_required_compose_settings() -> None:
    module = runpy.run_path(str(ROOT / "scripts/hermes-healthcheck-acceptance.py"))
    env = module["fixture_environment"]("/fixture")
    required = set()
    for name in ("compose.cloud.yaml", "compose.hermes.yaml"):
        required.update(re.findall(r"\$\{(JOB_SEARCH_[A-Z_]+):?\?", (ROOT / name).read_text()))
    assert required
    assert required <= env.keys(), required - env.keys()
    assert all(env[key] for key in required)
    assert env["JOB_SEARCH_COST_DIR"] == "/fixture"


class FakeRuntime:
    def __init__(self, stop: threading.Event) -> None:
        self.stop = stop
        self.active = 0
        self.maximum_active = 0
        self.ticks = 0

    def tick(self, *, should_stop=lambda: False):
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        self.ticks += 1
        self.active -= 1
        self.stop.set()
        return {"acquired": True, "work": {"succeeded": 1}}


class FakeServer:
    def __init__(self, stop: threading.Event) -> None:
        self.stop = stop
        self.timeout = None
        self.handled = 0
        self.closed = False

    def handle_request(self) -> None:
        self.handled += 1
        self.stop.set()

    def server_close(self) -> None:
        self.closed = True


def test_worker_loop_is_sequential_bounded_and_publishes_private_health() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        stop = threading.Event()
        fake = FakeRuntime(stop)
        factory_calls = []

        def factory(config, **kwargs):
            factory_calls.append((config, kwargs))
            return fake

        emitted = []
        result = run_worker_loop(
            RuntimeConfigV1.defaults(root),
            lane="core",
            interval_seconds=1,
            max_work=3,
            max_outbox=4,
            health_dir=root / "health",
            stop=stop,
            runtime_factory=factory,
            now_provider=lambda: NOW,
            emit=emitted.append,
        )
        assert result == 0 and fake.ticks == 1 and fake.maximum_active == 1
        assert factory_calls[0][1] == {
            "lane": "core",
            "max_work_per_tick": 3,
            "max_outbox_per_tick": 4,
        }
        report = json.loads(emitted[0])
        assert report["lane"] == "core" and report["report"]["acquired"]
        health_path = _health_path(root / "health", "core")
        health = json.loads(health_path.read_text(encoding="utf-8"))
        assert health["state"] == "stopped"
        assert os.stat(health_path).st_mode & 0o777 == 0o600


def test_model_loop_never_drains_core_outbox() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        stop = threading.Event()
        fake = FakeRuntime(stop)
        supplied = {}

        def factory(_config, **kwargs):
            supplied.update(kwargs)
            return fake

        assert (
            run_worker_loop(
                RuntimeConfigV1.defaults(root),
                lane="model",
                interval_seconds=1,
                max_outbox=99,
                health_dir=root / "health",
                stop=stop,
                runtime_factory=factory,
                now_provider=lambda: NOW,
                emit=lambda _value: None,
            )
            == 0
        )
        assert supplied["max_outbox_per_tick"] == 0


def test_worker_health_rejects_stale_failed_and_malformed_records() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "worker.json"
        _write_health(path, _worker_health_record("core", "idle", NOW))
        assert check_worker_health(path, now=NOW + timedelta(seconds=10))
        assert not check_worker_health(
            path, now=NOW + timedelta(seconds=901), max_idle_seconds=900
        )
        _write_health(path, _worker_health_record("core", "failed", NOW))
        assert not check_worker_health(path, now=NOW)
        path.write_text("not json", encoding="utf-8")
        assert not check_worker_health(path, now=NOW)


def test_http_service_closes_server_after_stop_event() -> None:
    stop = threading.Event()
    fake = FakeServer(stop)
    emitted = []
    result = serve_http(
        RuntimeConfigV1.defaults(Path.cwd()),
        "dashboard",
        stop=stop,
        poll_seconds=0.05,
        server_factory=lambda _config: fake,
        emit=emitted.append,
    )
    assert result == 0 and fake.handled == 1 and fake.closed
    assert fake.timeout == 0.05 and "127.0.0.1:8766" in emitted[0]


def test_invalid_cloud_loop_bounds_fail_before_runtime_construction() -> None:
    for values in (
        {"interval_seconds": 0},
        {"max_work": -1},
        {"max_outbox": 101},
    ):
        try:
            run_worker_loop(
                RuntimeConfigV1.defaults(Path.cwd()),
                lane="core",
                runtime_factory=lambda *_args, **_kwargs: None,
                **values,
            )
        except ValueError:
            pass
        else:
            raise AssertionError("invalid worker-loop bounds were accepted")


def test_cloud_packaging_keeps_private_services_loopback_and_single_replica() -> None:
    compose = (ROOT / "compose.cloud.yaml").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    dashboard = compose.split("\n  dashboard:\n", 1)[1].split("\n  mcp:\n", 1)[0]
    mcp = compose.split("\n  mcp:\n", 1)[1].split("\n  core:\n", 1)[0]
    core = compose.split("\n  core:\n", 1)[1].split("\n  model:\n", 1)[0]
    mail = (ROOT / "compose.mail.yaml").read_text(encoding="utf-8")
    assert "  core:" in mail
    assert all("  " + name + ":" not in mail for name in ("dashboard", "mcp", "model", "tools", "hermes"))
    assert mail.count("create_host_path: false") == 2
    assert "target: /run/job-search/openrouter-api-key" in mail
    assert "target: /run/job-search/mail-inference.json" in mail
    assert "network_mode: host" in compose and "ports:" not in compose
    assert 'JOB_SEARCH_PLATFORM:-linux/amd64' in compose
    assert compose.count("replicas: 1") == 5
    assert "network_mode: none" in compose
    assert "JOB_SEARCH_NETWORKLESS_TOOL_CONTAINER" in compose
    assert "no-new-privileges:true" in compose and "cap_drop:" in compose
    assert "JOB_SEARCH_PRIVATE_DIR" in compose and "read_only: true" in compose
    assert "network_mode: host" in dashboard
    assert "network_mode: host" not in mcp
    assert "- agent-boundary" in mcp
    assert "--bind-host" in mcp and "0.0.0.0" in mcp
    assert "--allowed-host" in mcp and "- mcp" in mcp
    assert compose.count("*mcp-token-volume") == 1
    assert compose.count("*notification-socket-volume") == 2
    assert "*notification-socket-volume" in dashboard
    assert "*notification-socket-volume" not in mcp
    assert "*notification-socket-volume" in core
    assert "*runpod-key-volume" not in mcp
    assert "*resume-model-volume" not in mcp
    assert "*resume-model-volume" in core
    assert "*tool-socket-volume" not in mcp
    assert "tools:" not in mcp.split("depends_on:", 1)[-1]
    assert "pids_limit: 64" in mcp
    assert "mem_limit: 512m" in mcp
    assert "cpus: 1.0" in mcp
    assert "target: /run/job-search\n" not in compose
    assert "USER jobsearch" in dockerfile
    assert "python:3.12-slim-bookworm@sha256:" in dockerfile
    assert "--no-deps -r requirements/cloud.txt" in dockerfile
    assert "chmod 0555 /opt/job-search" in dockerfile
    assert "chmod 1775 /opt/job-search" not in dockerfile
    for secret_name in ("RUNPOD_API_KEY", "TELEGRAM_BOT_TOKEN", "OUTLOOK_TOKEN"):
        assert secret_name not in compose and secret_name not in dockerfile
    ignore_lines = set(dockerignore.splitlines())
    from job_search.dashboard import STATIC_ROUTES
    # Cover every served dashboard asset, including newly split page modules.
    for filename, _ in STATIC_ROUTES.values():
        path = "job_search/web/" + filename
        assert _docker_context_includes(dockerignore, path), path
    assert "!job_search/**" not in ignore_lines
    assert "!*.py" not in ignore_lines
    assert "!job_search/**/*.py" not in ignore_lines
    assert "!job_search/*.py" not in ignore_lines
    assert "!requirements-*.txt" not in ignore_lines
    packaged_python = {
        line[1:]
        for line in ignore_lines
        if line.startswith("!job_search/") and line.endswith(".py")
    }
    repository_python = {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "job_search").rglob("*.py")
        if "__pycache__" not in path.parts
    }
    # Host administration ships in the verified release archive, not application
    # images. It invokes privileged Docker/AWS operations and has no app caller.
    host_only_python = {"job_search/aws_ops.py", "job_search/operation_journal.py",
                        "job_search/release_policy.py", "job_search/cost_collector.py",
                        "job_search/review_host.py", "job_search/prepared_release.py",
                        "job_search/release_coordinator.py"}
    assert packaged_python == repository_python - host_only_python
    assert not (packaged_python & host_only_python)
    for excluded in (
        "**/.env.*",
        "**/*.db",
        "**/*.key",
        "**/*.token",
        "**/*-api-key",
        "**/*credential*",
        "**/*secret*",
    ):
        assert excluded in ignore_lines
    for included in (
        "job_search/runtime.py",
        "job_search/inference/providers.py",
        "job_search/mail/runtime.py",
        "job_search/outlook/auth.py",
        "job_search/resume_lab/gateway.py",
        "job_search/resume_lab/career_ops_template.tex",
        "job_search/web/app.js",
        "job_search/web/console.js",
        "deploy/runpod/exact_embedding_service.py",
        "deploy/hermes/cont-init.d/018-job-search-mcp-token",
        "deploy/hermes/s6-rc.d/job-search-notifications/run",
        "job_search/cli.py",
        "job_search/__main__.py",
        "job_search/collection/boards.seed.json",
        "job_search/ranking/web/index.html",
        "job_search/salary/web/labeler/app.js",
        "job_search/salary/web/review/styles.css",
        "requirements/cloud.txt",
    ):
        assert _docker_context_includes(dockerignore, included), included
    for excluded in (
        "job_search/private.json",
        "job_search/private.py",
        "job_search/collection/private.json",
        "job_search/ranking/private.py",
        "job_search/salary/web/labeler/private.json",
        "job_search/mail/private.bin",
        "job_search/mail/private.py",
        "job_search/resume_lab/operator-notes.txt",
        "job_search/web/private.json",
        "deploy/hermes/local-config.txt",
        "deploy/hermes/cont-init.d/private-copy",
        "deploy/runpod/embedding.env.example",
        "unlisted.py",
        "requirements-private.txt",
        ".env",
        "private/state.db",
    ):
        assert not _docker_context_includes(dockerignore, excluded), excluded


def test_hermes_overlay_preserves_upstream_supervision_and_packages_s6_service() -> (
    None
):
    compose = (ROOT / "compose.hermes.yaml").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile.hermes").read_text(encoding="utf-8")
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    run_script = (
        ROOT / "deploy/hermes/s6-rc.d/job-search-notifications/run"
    ).read_text(encoding="utf-8")
    init_script = (
        ROOT / "deploy/hermes/cont-init.d/018-job-search-mcp-token"
    ).read_text(encoding="utf-8")
    assert 'command: ["gateway", "run"]' in compose
    assert 'JOB_SEARCH_HERMES_PLATFORM:-linux/amd64' in compose
    assert "network_mode: host" not in compose and "ports:" not in compose
    assert "- agent-boundary" in compose
    assert "JOB_SEARCH_STATE_DIR" not in compose
    assert "JOB_SEARCH_PRIVATE_DIR" not in compose
    assert "user:" not in compose and "init: true" not in compose
    assert "HERMES_UID:" in compose and "HERMES_GID:" in compose
    assert "ENTRYPOINT" not in dockerfile and 'CMD ["gateway", "run"]' in dockerfile
    assert "test -x /init" in dockerfile
    assert "test -x /command/s6-setuidgid" in dockerfile
    assert "test -x /opt/hermes/.venv/bin/python" in dockerfile
    assert "getent passwd hermes" in dockerfile
    assert "JOB_SEARCH_BUILT_HERMES_BASE_IMAGE" in dockerfile
    assert "JOB_SEARCH_EXPECTED_HERMES_BASE_IMAGE" in compose
    assert "built_base" in init_script and "expected_base" in init_script
    assert "deploy/hermes/s6-rc.d/" in dockerfile
    assert "!deploy/hermes/cont-init.d/018-job-search-mcp-token" in dockerignore
    assert "!deploy/hermes/s6-rc.d/job-search-notifications/run" in dockerignore
    assert "s6-setuidgid hermes" in run_script
    assert "- /command/s6-setuidgid\n        - hermes" in compose
    assert "unset JOB_SEARCH_MCP_TOKEN" in run_script


def test_runpod_packaging_has_two_guarded_scale_to_zero_lifecycles() -> None:
    generation = (ROOT / "scripts/runpod-vllm-endpoint").read_text(encoding="utf-8")
    embeddings = (ROOT / "scripts/runpod-embedding-endpoint").read_text(
        encoding="utf-8"
    )
    guard = (ROOT / "scripts/runpod-endpoint-guard").read_text(encoding="utf-8")
    for script in (generation, embeddings):
        assert "workers-min 0" in script and "workers-max 1" in script
        assert "2.10.0 or newer" in script
        assert "runpod-endpoint-guard" in script
        assert "verify_eventually" in script
        assert "never repeat the billable create operation automatically" in script
        assert "create_status=$?" in script
        assert "create outcome is unresolved" in script
        assert "--template-id" in script and "--model-reference" in script
        assert "--execution-timeout" not in script.split("wake)", 1)[1].split(
            ";;", 1
        )[0]
        delete_branch = script.split("  delete)", 1)[1].split(";;", 1)[0]
        assert "verify_template" in delete_branch
        assert "verify_endpoint --endpoint-id" in delete_branch
    assert "worker-v1-vllm@sha256:" in generation
    assert "stock embedding worker cannot attest" in embeddings
    assert "JOB_SEARCH_EMBEDDING_PROTOCOL=job_search_infinity_exact_v1" in embeddings
    assert 'local revision="${RUNPOD_EMBEDDING_MODEL_REFERENCE##*:}"' in embeddings
    assert '--env "MODEL_REVISION=$revision"' in embeddings
    assert "HF_HUB_CACHE=/runpod-volume/huggingface-cache/hub" in embeddings
    assert "INFINITY_TRUST_REMOTE_CODE=false" in embeddings
    exact_worker = (ROOT / "deploy/runpod/exact_embedding_service.py").read_text(
        encoding="utf-8"
    )
    worker_dockerfile = (ROOT / "Dockerfile.runpod-embedding").read_text(
        encoding="utf-8"
    )
    assert "resolve_exact_snapshot" in exact_worker
    assert "trust_remote_code=False" in exact_worker
    assert 'served_model_name=model_id' in exact_worker
    assert 'CMD ["python", "-u", "/handler.py"]' in worker_dockerfile
    assert "MODEL_REVISION=${RUNPOD_MODEL_REFERENCE##*:}" in generation
    assert "TOKENIZER_REVISION=${RUNPOD_MODEL_REFERENCE##*:}" in generation
    assert "HF_HUB_OFFLINE=1" in generation
    assert "TRANSFORMERS_OFFLINE=1" in generation
    assert "ENABLE_LOG_REQUESTS=false" in generation
    assert "DISABLE_LOG_REQUESTS=true" in generation
    assert '"model": args.model' in guard and '"input": [' in guard
    assert "includeTemplate=true" in guard
    assert 'selected.get("workersMin"), 0' in guard
    assert 'selected.get("executionTimeoutMs")' in guard
    assert 'model_response["data"]["myself"]["endpoint"]' in guard


def _guard_module():
    return runpy.run_path(
        str(ROOT / "scripts/runpod-endpoint-guard"),
        run_name="runpod_endpoint_guard_test",
    )


def test_runpod_guard_normalizes_a_truncated_http_response() -> None:
    module = _guard_module()
    request_json = module["request_json"]

    class PartialResponse:
        headers = SimpleNamespace(get_content_type=lambda: "application/json")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return "https://rest.runpod.io/v1/endpoints"

        def read(self, _limit):
            raise http.client.IncompleteRead(b'{"partial":')

    class PartialOpener:
        def open(self, _request, timeout):
            assert timeout == 30
            return PartialResponse()

    original_build_opener = module["urllib"].request.build_opener
    original_api_key = request_json.__globals__["api_key"]
    module["urllib"].request.build_opener = lambda *_handlers: PartialOpener()
    request_json.__globals__["api_key"] = lambda: "test-api-key"
    try:
        try:
            request_json("GET", "https://rest.runpod.io/v1/endpoints")
        except module["GuardError"] as exc:
            assert str(exc) == "Runpod guard request failed"
        else:
            raise AssertionError("guard accepted a truncated HTTP response")
    finally:
        module["urllib"].request.build_opener = original_build_opener
        request_json.__globals__["api_key"] = original_api_key


def _template_record() -> dict[str, object]:
    return {
        "id": "template-123",
        "name": "template-name",
        "imageName": "runpod/worker@sha256:" + "a" * 64,
        "isServerless": True,
        "isPublic": False,
        "containerDiskInGb": 20,
        "env": {"MODEL_NAMES": "org/model", "BATCH_SIZES": "32"},
    }


def test_runpod_guard_fetches_template_by_exact_id_and_rejects_extra_env() -> None:
    module = _guard_module()
    template = module["template"]
    calls = []
    record = _template_record()

    def request_json(method, url, body=None):
        calls.append((method, url, body))
        return record

    template.__globals__["request_json"] = request_json
    args = SimpleNamespace(
        template_id="template-123",
        name="template-name",
        image="runpod/worker@sha256:" + "a" * 64,
        container_disk_gb=20,
        env=["MODEL_NAMES=org/model", "BATCH_SIZES=32"],
    )
    with redirect_stdout(io.StringIO()):
        template(args)
    assert calls == [
        (
            "GET",
            "https://rest.runpod.io/v1/templates/template-123"
            "?includeEndpointBoundTemplates=true",
            None,
        )
    ]
    record["env"] = {
        "MODEL_NAMES": "org/model",
        "BATCH_SIZES": "32",
        "UNEXPECTED": "value",
    }
    try:
        template(args)
    except module["GuardError"]:
        pass
    else:
        raise AssertionError("template guard accepted an additional environment key")


def test_runpod_guard_requires_matching_v2_privacy_when_v1_omits_it() -> None:
    module = _guard_module()
    validate = module["validate_template_record"]
    record = _template_record()
    del record["isPublic"]
    current = {
        "id": record["id"], "name": record["name"], "image": record["imageName"],
        "serverless": True, "disk": 20, "env": record["env"], "public": False,
    }

    def request_json(method, url, body=None):
        assert (method, url, body) == (
            "GET", "https://api.runpod.io/v2/templates/template-123", None,
        )
        return current

    validate.__globals__["request_json"] = request_json
    kwargs = dict(template_id="template-123", name="template-name",
                  image=record["imageName"], container_disk_gb=20, environment=record["env"])
    assert validate(record, **kwargs)["isPublic"] is False
    incomplete = {key: value for key, value in record.items() if key != "imageName"}
    assert validate(incomplete, **kwargs)["imageName"] == record["imageName"]
    for field, value in (("public", True), ("public", None), ("image", "different"),
                         ("env", {"UNEXPECTED": "value"}), ("id", "another-template")):
        previous = current[field]
        current[field] = value
        try:
            validate(record, **kwargs)
        except module["GuardError"]:
            pass
        else:
            raise AssertionError(f"guard accepted an unverified v2 template {field}")
        current[field] = previous


def test_runpod_current_endpoint_rejects_effective_overrides_and_unbounded_workers() -> None:
    module = _guard_module()
    verify = module["verify_current_gpu_endpoint"]
    args = SimpleNamespace(expected_name="endpoint-name", image="pinned-image",
                           container_disk_gb=20, idle_timeout=60, execution_timeout=900)
    current = {
        "id": "endpoint-123", "name": "endpoint-name", "type": "QUEUE",
        "image": "pinned-image", "env": {"MODEL_NAME": "expected"}, "disk": 20,
        "gpu": {"count": 1, "pools": ["ADA_24"]},
        "workers": {"min": 0, "max": 1, "idleTimeout": 60}, "timeout": 900000,
    }
    def request_json(method, url, body=None):
        assert (method, url, body) == (
            "GET", "https://api.runpod.io/v2/serverless/endpoint-123", None,
        )
        return current
    verify.__globals__["request_json"] = request_json
    selected = {"id": "endpoint-123"}
    assert verify(selected, args, {"MODEL_NAME": "expected"})["computeType"] == "GPU"
    for field, value in (("env", {"MODEL_NAME": "different"}), ("gpu", None),
                         ("workers", {"min": 1, "max": 1, "idleTimeout": 60}),
                         ("timeout", 900001), ("image", "different")):
        previous = current[field]
        current[field] = value
        try:
            verify(selected, args, {"MODEL_NAME": "expected"})
        except module["GuardError"]:
            pass
        else:
            raise AssertionError(f"guard accepted an invalid current endpoint {field}")
        current[field] = previous


def test_runpod_guard_attests_endpoint_template_policy_and_model_reference() -> None:
    module = _guard_module()
    endpoint = module["endpoint"]
    calls = []
    model_reference = "https://huggingface.co/org/model:" + "b" * 40
    returned_references = [model_reference]
    record = {
        "id": "endpoint-123",
        "name": "endpoint-name",
        "templateId": "template-123",
        "template": _template_record(),
        "computeType": "GPU",
        "gpuCount": 1,
        "workersMin": 0,
        "workersMax": 1,
        "idleTimeout": 300,
        "executionTimeoutMs": 900_000,
    }

    def request_json(method, url, body=None):
        calls.append((method, url, body))
        if url == "https://api.runpod.io/graphql":
            return {
                "data": {
                    "myself": {
                        "endpoint": {"modelReferences": [model_reference]}
                    }
                }
            }
        return [record]

    endpoint.__globals__["request_json"] = request_json
    args = SimpleNamespace(
        endpoint_id="endpoint-123",
        name=None,
        expected_name="endpoint-name",
        template_id="template-123",
        template_name="template-name",
        image="runpod/worker@sha256:" + "a" * 64,
        container_disk_gb=20,
        idle_timeout=300,
        execution_timeout=900,
        model_reference=model_reference,
        env=["MODEL_NAMES=org/model", "BATCH_SIZES=32"],
    )
    with redirect_stdout(io.StringIO()):
        endpoint(args)
    assert calls[0][1].endswith("/endpoints?includeTemplate=true")
    assert calls[1][1] == "https://api.runpod.io/graphql"
    assert calls[1][2]["variables"] == {"id": "endpoint-123"}

    record["executionTimeoutMs"] = 901_000
    try:
        endpoint(args)
    except module["GuardError"]:
        pass
    else:
        raise AssertionError("endpoint guard accepted a different execution timeout")
    record["executionTimeoutMs"] = 900_000
    returned_references[0] = "https://huggingface.co/org/model:" + "c" * 40

    def request_with_changed_reference(method, url, body=None):
        if url == "https://api.runpod.io/graphql":
            return {
                "data": {
                    "myself": {
                        "endpoint": {"modelReferences": returned_references}
                    }
                }
            }
        return [record]

    endpoint.__globals__["request_json"] = request_with_changed_reference
    try:
        endpoint(args)
    except module["GuardError"]:
        pass
    else:
        raise AssertionError("endpoint guard accepted a different model reference")


def _dry_run(script: str, action: str, values: dict[str, str]):
    return subprocess.run(
        [str(ROOT / "scripts" / script), action],
        cwd=ROOT,
        env={"PATH": os.environ.get("PATH", ""), **values},
        text=True,
        capture_output=True,
        check=False,
    )


def _vllm_environment() -> dict[str, str]:
    return {
        "RUNPOD_ENDPOINT_NAME": "endpoint-name",
        "RUNPOD_ENDPOINT_ID": "endpoint-123",
        "RUNPOD_TEMPLATE_NAME": "template-name",
        "RUNPOD_TEMPLATE_ID": "template-123",
        "RUNPOD_WORKER_IMAGE": "runpod/worker-v1-vllm@sha256:" + "a" * 64,
        "RUNPOD_MODEL_NAME": "org/model",
        "RUNPOD_MODEL_REFERENCE": "https://huggingface.co/org/model:" + "b" * 40,
        "RUNPOD_GPU_ID": "gpu-pool",
    }


def _embedding_environment() -> dict[str, str]:
    return {
        "RUNPOD_EMBEDDING_ENDPOINT_NAME": "endpoint-name",
        "RUNPOD_EMBEDDING_ENDPOINT_ID": "endpoint-123",
        "RUNPOD_EMBEDDING_TEMPLATE_NAME": "template-name",
        "RUNPOD_EMBEDDING_TEMPLATE_ID": "template-123",
        "RUNPOD_EMBEDDING_WORKER_IMAGE": (
            "registry.example.test/job-search-embedding@sha256:" + "a" * 64
        ),
        "RUNPOD_EMBEDDING_MODEL": "org/model",
        "RUNPOD_EMBEDDING_MODEL_REFERENCE": (
            "https://huggingface.co/org/model:" + "b" * 40
        ),
        "RUNPOD_EMBEDDING_GPU_ID": "gpu-pool",
    }


def test_runpod_wrappers_validate_names_and_wake_timeouts_before_apply() -> None:
    values = _vllm_environment()
    values["RUNPOD_ENDPOINT_NAME"] = "ab"
    result = _dry_run("runpod-vllm-endpoint", "deploy", values)
    assert result.returncode == 2 and "invalid shape" in result.stderr

    values = _vllm_environment()
    values["RUNPOD_EXECUTION_TIMEOUT"] = "29"
    result = _dry_run("runpod-vllm-endpoint", "wake", values)
    assert result.returncode == 2 and "between 30 and 86400" in result.stderr

    values = _embedding_environment()
    values["RUNPOD_EMBEDDING_ENDPOINT_NAME"] = "ab"
    result = _dry_run("runpod-embedding-endpoint", "deploy", values)
    assert result.returncode == 2 and "invalid shape" in result.stderr

    values = _embedding_environment()
    values["RUNPOD_EMBEDDING_IDLE_TIMEOUT"] = "4"
    result = _dry_run("runpod-embedding-endpoint", "wake", values)
    assert result.returncode == 2 and "between 5 and 3600" in result.stderr

    assert _dry_run(
        "runpod-vllm-endpoint", "wake", _vllm_environment()
    ).returncode == 0
    assert _dry_run(
        "runpod-embedding-endpoint", "wake", _embedding_environment()
    ).returncode == 0

    stock = _embedding_environment()
    stock["RUNPOD_EMBEDDING_WORKER_IMAGE"] = (
        "runpod/worker-infinity-embedding@sha256:" + "a" * 64
    )
    rejected = _dry_run("runpod-embedding-endpoint", "deploy", stock)
    assert rejected.returncode == 2 and "stock embedding worker" in rejected.stderr


def test_vllm_smoke_rejects_completed_worker_error_from_fake_runpodctl() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        script = root / "runpod-vllm-endpoint"
        guard = root / "runpod-endpoint-guard"
        fake_runpodctl = root / "runpodctl"
        shutil.copy2(ROOT / "scripts/runpod-vllm-endpoint", script)
        real_guard = str(ROOT / "scripts/runpod-endpoint-guard")
        guard.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "if len(sys.argv) > 1 and sys.argv[1] == 'template':\n"
            "    print('template-123')\n"
            "elif len(sys.argv) > 1 and sys.argv[1] == 'endpoint':\n"
            "    print('endpoint-123')\n"
            "else:\n"
            f"    os.execv({real_guard!r}, [{real_guard!r}, *sys.argv[1:]])\n",
            encoding="utf-8",
        )
        fake_runpodctl.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "args = sys.argv[1:]\n"
            "if args == ['version']:\n"
            "    print('runpodctl version 2.10.0')\n"
            "elif args == ['user']:\n"
            "    print('{}')\n"
            "elif args == ['serverless', 'create', '--help']:\n"
            "    print('--model-reference --workers-min')\n"
            "elif args == ['template', 'create', '--help']:\n"
            "    print('--serverless')\n"
            "elif args[:2] == ['serverless', 'run']:\n"
            "    print(os.environ['FAKE_RUNPOD_JOB'])\n"
            "else:\n"
            "    raise SystemExit(3)\n",
            encoding="utf-8",
        )
        guard.chmod(0o755)
        fake_runpodctl.chmod(0o755)
        environment = _vllm_environment()
        environment["PATH"] = f"{root}{os.pathsep}{os.environ.get('PATH', '')}"
        environment["FAKE_RUNPOD_JOB"] = json.dumps(
            {
                "id": "job-1",
                "status": "COMPLETED",
                "output": [{"error": {"message": "vLLM returned HTTP 400"}}],
            }
        )
        result = subprocess.run(
            [str(script), "smoke", "--apply"],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 2
        assert "validation failed" in result.stderr

        environment["FAKE_RUNPOD_JOB"] = json.dumps(
            {
                "id": "job-2",
                "status": "COMPLETED",
                "output": [
                    {
                        "model": "org/model",
                        "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {
                                    "role": "assistant",
                                    "content": '{"ok":true}',
                                },
                            }
                        ],
                    }
                ],
            }
        )
        accepted = subprocess.run(
            [str(script), "smoke", "--apply"],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        assert accepted.returncode == 0
        assert "validated_vllm_model=org/model" in accepted.stdout


def test_exact_embedding_worker_resolves_only_one_pinned_snapshot() -> None:
    module = runpy.run_path(
        str(ROOT / "deploy/runpod/exact_embedding_service.py"),
        run_name="exact_embedding_service_test",
    )
    resolve = module["resolve_exact_snapshot"]
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        revision = "b" * 40
        snapshot = root / "models--baai--bge-base-en-v1.5" / "snapshots" / revision
        snapshot.mkdir(parents=True)
        assert os.path.samefile(
            resolve(root, "BAAI/bge-base-en-v1.5", revision), snapshot
        )
        try:
            resolve(root, "BAAI/bge-base-en-v1.5", "main")
        except ValueError as exc:
            assert "immutable" in str(exc)
        else:
            raise AssertionError("embedding worker accepted a mutable revision")

        canonical = root / "models--BAAI--bge-base-en-v1.5" / "snapshots" / revision
        canonical.mkdir(parents=True, exist_ok=True)
        if os.path.samefile(canonical, snapshot):
            # The development Mac may use a case-insensitive filesystem; these are
            # one directory there and must not be treated as two cached snapshots.
            assert resolve(root, "BAAI/bge-base-en-v1.5", revision).is_dir()
        else:
            try:
                resolve(root, "BAAI/bge-base-en-v1.5", revision)
            except ValueError as exc:
                assert "ambiguous" in str(exc)
            else:
                raise AssertionError("embedding worker accepted two cache snapshots")


def main() -> None:
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in tests:
        test()
    print(f"ok ({len(tests)} job-search cloud tests)")


if __name__ == "__main__":
    main()
