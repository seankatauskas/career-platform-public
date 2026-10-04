"""Bounded Linux memory measurements without process names, arguments, or payloads.

RSS belongs to the selected process only. Cgroup counters include its entire
cgroup, including other processes and charged file cache. Neither a process
high-water mark nor ``memory.peak`` can be attributed to this sampling interval.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
from typing import Callable


_HOST = {"MemTotal": "total", "MemAvailable": "available", "AnonPages": "anonymous",
         "Cached": "file_cache", "Buffers": "buffers", "Slab": "slab",
         "SReclaimable": "reclaimable_slab", "SwapTotal": "swap_total", "SwapFree": "swap_free"}
_PROCESS = {"VmRSS": "rss", "RssAnon": "anonymous", "RssFile": "file",
            "RssShmem": "shared", "VmHWM": "lifetime_peak_rss", "VmSwap": "swap"}
_CGROUP = ("anon", "file", "kernel", "slab", "shmem", "inactive_file")
_EVENTS = ("low", "high", "max", "oom", "oom_kill", "oom_group_kill")
_MAX_READ = 65536


def _read(path: Path) -> str:
    try:
        with path.open(encoding="ascii", errors="replace") as stream:
            return stream.read(_MAX_READ)
    except OSError:
        return ""


def _number(value: str) -> int | None:
    try:
        result = int(value)
        return result if result >= 0 else None
    except ValueError:
        return None


def _kb_fields(text: str, fields: dict[str, str], prefix: str) -> dict[str, int | None]:
    result = {f"{prefix}_{name}_bytes": None for name in fields.values()}
    for line in text.splitlines():
        parts = line.split()
        key = parts[0].rstrip(":") if parts else ""
        if key in fields and len(parts) == 3 and parts[2] == "kB":
            value = _number(parts[1])
            result[f"{prefix}_{fields[key]}_bytes"] = value * 1024 if value is not None else None
    return result


def _pairs(text: str) -> dict[str, int | None]:
    return {parts[0]: _number(parts[1]) for line in text.splitlines()
            if len(parts := line.split()) == 2}


def _cgroup_path(proc: Path, pid: int, root: Path) -> Path | None:
    membership = _read(proc / str(pid) / "cgroup")
    for line in membership.splitlines():
        if not line.startswith("0::/"):
            continue
        relative = Path(line[3:].lstrip("/"))
        if ".." in relative.parts:
            return None
        candidate = root / relative
        if (candidate / "memory.current").is_file():
            return candidate
        # In a cgroup namespace, /sys/fs/cgroup can already be mounted at the
        # service root while proc reports a path from an outer namespace.
        if membership == _read(proc / "self" / "cgroup") and (root / "memory.current").is_file():
            return root
    return None


def memory_snapshot(*, pid: int | None = None, proc_root: Path = Path("/proc"),
                    cgroup_root: Path = Path("/sys/fs/cgroup")) -> dict[str, int | None]:
    """Read a fixed allowlist of counters; unsupported/missing counters are null."""
    pid = os.getpid() if pid is None else pid
    if isinstance(pid, bool) or not isinstance(pid, int) or pid < 1:
        raise ValueError("pid must be a positive integer")
    result = _kb_fields(_read(proc_root / "meminfo"), _HOST, "host")
    result.update(_kb_fields(_read(proc_root / str(pid) / "status"), _PROCESS, "process"))
    for name in ("current", "lifetime_peak", "limit") + _CGROUP:
        result[f"cgroup_{name}_bytes"] = None
    for name in _EVENTS:
        result[f"cgroup_event_{name}"] = None
    group = _cgroup_path(proc_root, pid, cgroup_root)
    if group is not None:
        for name, filename in (("current", "memory.current"), ("lifetime_peak", "memory.peak"),
                               ("limit", "memory.max")):
            result[f"cgroup_{name}_bytes"] = _number(_read(group / filename).strip())
        stat = _pairs(_read(group / "memory.stat"))
        events = _pairs(_read(group / "memory.events"))
        for name in _CGROUP:
            result[f"cgroup_{name}_bytes"] = stat.get(name)
        for name in _EVENTS:
            result[f"cgroup_event_{name}"] = events.get(name)
    return result


class MemorySampler:
    """Keep min/max/first/last counters in constant space, at most 3600 samples.

    Call start()/finish() around work, or use as a context manager. Sampling
    failures never fail the work. This does not publish metrics or write files.
    """
    def __init__(self, *, pid: int | None = None, interval_seconds: float = 1,
                 max_samples: int = 3600, snapshot: Callable[[], dict] | None = None):
        if (isinstance(interval_seconds, bool) or not 1 <= interval_seconds <= 60
                or isinstance(max_samples, bool) or not isinstance(max_samples, int)
                or not 1 <= max_samples <= 3600):
            raise ValueError("interval must be 1..60 seconds and samples 1..3600")
        self.interval = interval_seconds
        self.max_samples = max_samples
        self.snapshot = snapshot or (lambda: memory_snapshot(pid=pid))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._counters: dict[str, dict] = {}
        self.samples = 0
        self.failures = 0
        self.started_at: str | None = None
        self.finished_at: str | None = None
        self.last_sample_at: str | None = None

    def _sample(self) -> None:
        if self.samples >= self.max_samples:
            return
        self.samples += 1
        self.last_sample_at = datetime.now(timezone.utc).isoformat()
        try:
            values = self.snapshot()
        except Exception:
            self.failures += 1
            return
        # Ignore injected non-counter data as well as unsupported counters.
        allowed = {f"host_{v}_bytes" for v in _HOST.values()} | {f"process_{v}_bytes" for v in _PROCESS.values()}
        allowed |= {f"cgroup_{v}_bytes" for v in ("current", "lifetime_peak", "limit") + _CGROUP}
        allowed |= {f"cgroup_event_{v}" for v in _EVENTS}
        for key, value in values.items():
            if key not in allowed or isinstance(value, bool) or not isinstance(value, int) or value < 0:
                continue
            counter = self._counters.setdefault(key, {"samples": 0, "first": value, "last": value,
                                                      "minimum": value, "maximum": value})
            counter.update(samples=counter["samples"] + 1, last=value,
                           minimum=min(counter["minimum"], value), maximum=max(counter["maximum"], value))

    def _run(self) -> None:
        while self.samples < self.max_samples and not self._stop.wait(self.interval):
            self._sample()

    def start(self) -> "MemorySampler":
        if self.started_at is not None:
            raise RuntimeError("sampler already started")
        self.started_at = datetime.now(timezone.utc).isoformat()
        self._sample()
        self._thread = threading.Thread(target=self._run, name="memory-sampler", daemon=True)
        self._thread.start()
        return self

    def finish(self) -> dict:
        if self.started_at is None:
            raise RuntimeError("sampler not started")
        if self.finished_at is None:
            self._stop.set()
            self._thread.join()
            self._sample()
            self.finished_at = datetime.now(timezone.utc).isoformat()
        return {"version": 1, "started_at": self.started_at, "finished_at": self.finished_at,
                "last_sample_at": self.last_sample_at,
                "interval_seconds": self.interval, "samples": self.samples, "failed_samples": self.failures,
                "sample_limit_reached": self.samples >= self.max_samples, "counters": self._counters,
                "semantics": {"maximum": "largest observed sample, not a guaranteed interval peak",
                              "lifetime_peak": "kernel high-water mark; may predate this interval",
                              "process": "selected process only; excludes child processes",
                              "cgroup": "whole cgroup; includes other processes and charged file cache",
                              "host": "whole host; includes workloads outside this cgroup",
                              "missing": "unsupported or unreadable counters are omitted"}}

    def __enter__(self) -> "MemorySampler":
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.finish()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, default=None)
    args = parser.parse_args(argv)
    try:
        counters = memory_snapshot(pid=args.pid)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps({"version": 1, "measured_at": datetime.now(timezone.utc).isoformat(),
                      "scope": "one snapshot; lifetime peaks may predate this observation",
                      "counters": counters}, sort_keys=True))


if __name__ == "__main__":
    main()
