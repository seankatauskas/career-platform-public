"""Measure Docker startup probe scheduling using a fictional, networkless service.

Uses an already available local image; never pulls or touches the app project.
This isolates detection latency, not application initialization or AWS downtime.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import time
import uuid


def command(*args):
    return subprocess.check_output(args, text=True).strip()


def trial(image, *, start_period, start_interval):
    name = "career-health-probe-" + uuid.uuid4().hex[:12]
    args = ["docker", "create", "--name", name, "--network", "none", "--read-only",
            "--memory", "64m", "--cpus", "0.25", "--pids-limit", "32",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
            "--health-cmd", "python -c 'pass'", "--health-interval", "30s",
            "--health-timeout", "5s", "--health-retries", "3",
            "--health-start-period", start_period, "--health-start-interval", start_interval,
            "--entrypoint", "python", image, "-c", "import time; time.sleep(120)"]
    command(*args)
    try:
        started = time.monotonic()
        command("docker", "start", name)
        while time.monotonic() - started < 90:
            state = json.loads(command("docker", "inspect", "--format", "{{json .State}}", name))
            if state.get("Health", {}).get("Status") == "healthy":
                return {"seconds_to_healthy": round(time.monotonic() - started, 3),
                        "started_at": state["StartedAt"],
                        "first_probe_at": state["Health"]["Log"][0]["Start"],
                        "start_period": start_period, "start_interval": start_interval}
            if state["Status"] != "running":
                raise RuntimeError("fixture process exited")
            time.sleep(0.1)
        raise RuntimeError("fixture did not reach healthy")
    finally:
        command("docker", "rm", "--force", name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="python:3.12-slim-bookworm")
    parser.add_argument("--runs", type=int, default=2)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.runs <= 5:
        parser.error("runs must be between 1 and 5")
    image = command("docker", "image", "inspect", "--format", "{{.Id}}", args.image)
    result = {"fixture": "always-ready networkless process, same successful probe in both modes",
              "image_id": image, "docker_version": command("docker", "version", "--format", "{{.Server.Version}}"),
              "limitations": ["Docker Desktop/Linux VM; not native AWS", "No application startup, initialization, or dependency ordering measured"],
              "baseline": [], "candidate": []}
    for _ in range(args.runs):
        result["baseline"].append(trial(image, start_period="0s", start_interval="5s"))
        result["candidate"].append(trial(image, start_period="20s", start_interval="2s"))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps({key: result[key][-1] for key in ("baseline", "candidate")}), flush=True)


if __name__ == "__main__":
    main()
