#!/usr/bin/env python3
"""Test the reviewed predecessor and candidate against identical fictional state."""
from __future__ import annotations
import argparse
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from job_search.release_policy import validate_policy


def run(argv, **kwargs):
    result = subprocess.run(argv, text=True, capture_output=True, timeout=1800, **kwargs)
    if result.returncode:
        raise RuntimeError('command failed (exit ' + str(result.returncode) + '): ' + (result.stdout + result.stderr)[-6000:])
    return result.stdout.strip()


def predecessor_build_args(dockerfile, revision):
    # Keep the archived sources and base digest intact. Older Dockerfiles still
    # name Docker Hub; use the same official-image mirror as current builds.
    values = ['SOURCE_REVISION=' + revision]
    base = re.search(r'^ARG PYTHON_BASE_IMAGE=(python:[A-Za-z0-9_.-]+@sha256:[a-f0-9]{64})$',
                     dockerfile, re.MULTILINE)
    if base:
        values.append('PYTHON_BASE_IMAGE=public.ecr.aws/docker/library/' + base[1])
    return values


def predecessor_dockerfile(source):
    # Supplying a recipe on stdin also works on older BuildKit versions that
    # cannot select the bundled frontend via BUILDKIT_SYNTAX=dockerfile.v0.
    return source.removeprefix('# syntax=docker/dockerfile:1\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate-image')
    parser.add_argument('--local', action='store_true', help='Python-only development check; not valid release evidence')
    parser.add_argument('--output', type=Path, default=ROOT / '.cache/release-transition.json')
    args = parser.parse_args()
    if not args.local and not args.candidate_image:
        parser.error('--candidate-image is required unless --local')
    policy = validate_policy(json.loads((ROOT / 'deploy/release-policy.json').read_text()))
    source = run(['git', 'rev-parse', 'HEAD'], cwd=ROOT)
    dirty = bool(run(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=ROOT))
    report = {'schema_version': 1, 'source_sha': source, 'baseline_sha': policy['test_baseline_sha'],
              'runtime': 'python' if args.local else 'docker', 'passed': False, 'rollback_passed': False,
              'working_tree_dirty': dirty, 'scope': 'fictional data; no live providers', 'checks': []}
    volume = 'career-transition-' + os.urandom(6).hex()
    previous_image = volume + ':previous'
    created_volume = False
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix='career-transition-') as directory:
            temp = Path(directory); previous = temp / 'previous'; previous.mkdir()
            raw = subprocess.check_output(['git', 'archive', policy['test_baseline_sha']], cwd=ROOT)
            with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
                for member in archive:
                    path = Path(member.name)
                    if path.is_absolute() or '..' in path.parts or not (member.isfile() or member.isdir()):
                        raise RuntimeError('unsafe predecessor source member')
                    archive.extract(member, previous)
            old_policy_path = previous / 'deploy/release-policy.json'
            old_policy = validate_policy(json.loads(old_policy_path.read_text())) if old_policy_path.exists() else None
            rollback_expected = bool(old_policy and old_policy['schema_compatibility'] == policy['schema_compatibility'])
            if not args.local:
                revision = run(['docker', 'image', 'inspect', '--format', '{{index .Config.Labels "org.opencontainers.image.revision"}}', args.candidate_image])
                if revision != source:
                    raise RuntimeError('candidate image does not identify the current commit')
                machine = run(['docker', 'image', 'inspect', '--format', '{{.Architecture}}', args.candidate_image])
                report['candidate_image_id'] = run(['docker', 'image', 'inspect', '--format', '{{.Id}}', args.candidate_image])
                report['phase'] = 'build_predecessor'
                old_dockerfile = (previous / 'Dockerfile').read_text()
                build_args = predecessor_build_args(old_dockerfile, policy['test_baseline_sha'])
                report['predecessor_build_args'] = build_args
                report['predecessor_bundled_frontend'] = old_dockerfile != predecessor_dockerfile(old_dockerfile)
                run(['docker', 'build', '--platform', 'linux/' + machine,
                     *[part for value in build_args for part in ('--build-arg', value)],
                     '--file', '-', '--tag', previous_image, str(previous)],
                    input=predecessor_dockerfile(old_dockerfile))
                run(['docker', 'volume', 'create', volume]); created_volume = True
            def run_probe(action, old=False):
                if args.local:
                    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(previous if old else ROOT), str(ROOT)]))
                    argv = [sys.executable, str(ROOT / 'tests/fixtures/release_transition.py'), action, str(temp / 'state')]
                    return json.loads(run(argv, cwd=previous if old else ROOT, env=env))
                return json.loads(run(['docker', 'run', '--rm', '--platform', 'linux/' + machine, '--network', 'none', '--user', '0:0',
                    '--volume', volume + ':/state', '--volume', str(ROOT) + ':/fixtures:ro',
                    '--env', 'PYTHONPATH=/opt/job-search:/fixtures', '--entrypoint', 'python',
                    previous_image if old else args.candidate_image,
                    '/fixtures/tests/fixtures/release_transition.py', action, '/state']))
            def probe(action, old=False):
                start = time.monotonic()
                result = run_probe(action, old=old)
                result['elapsed_seconds'] = round(time.monotonic() - start, 3)
                return result
            report['phase'] = 'seed_predecessor'
            report['checks'].append(probe('seed', old=True))
            report['phase'] = 'upgrade_candidate'
            report['checks'].append(probe('upgrade'))
            if rollback_expected:
                report['phase'] = 'rollback_predecessor'
                report['checks'].append(probe('rollback', old=True))
                report['rollback_passed'] = True
            else:
                report['checks'].append({'action': 'rollback', 'status': 'unsupported', 'reason': 'predecessor lacks the same hardened compatibility policy'})
            report['phase'] = 'application_owners'
            report['checks'].append(probe('owners'))
            report['phase'] = 'complete'
            report['passed'] = True
    except Exception as error:
        report['error'] = type(error).__name__ + ': ' + str(error)
    finally:
        if created_volume:
            try:
                run(['docker', 'volume', 'rm', volume])
            except Exception as error:
                report.update(passed=False, cleanup_error=str(error))
        if not args.local:
            subprocess.run(['docker', 'image', 'rm', previous_image], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        report['total_seconds'] = round(time.monotonic() - started, 3)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
