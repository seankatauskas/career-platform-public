"""Coordinate manual releases using live host facts and durable S3 receipts.

Preparation runs inside aws-release.yml's single concurrency group. The source
is the dispatch SHA, never a moving branch. Installation remains a separate
explicit workflow with host locks and a final transition gate.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

from .prepared_release import validate_selection, verify
from .release_policy import validate_policy

PREFIX = 'releases/coordination'
SETTINGS = ('APP_REPOSITORY', 'HERMES_REPOSITORY', 'HERMES_BASE_IMAGE', 'TECTONIC_VERSION')


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()


def identity(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {'release_id', 'source_sha'}:
        raise ValueError('invalid installed release identity')
    validate_selection(value['release_id'], '0' * 64)
    if value['source_sha'] != value['release_id'][:40]:
        raise ValueError('installed release source mismatch')
    return value


def require_idle(host):
    if host.get('version') != 1 or host.get('state') != 'idle':
        raise ValueError('host is busy, recovering, or unknown; inspect release status before preparing or deploying')
    return identity(host['installed'])


def check_predecessor(host, policy):
    installed = require_idle(host)
    validate_policy(policy)
    if policy['predecessor'] != installed:
        raise ValueError('committed predecessor differs from production; propose-policy, review and merge that update, then prepare again')
    return installed


class Cloud:
    def __init__(self, bucket, instance, document):
        if not bucket or not re.fullmatch(r'i-[a-f0-9]+', instance or '') or not document:
            raise ValueError('set AWS_RELEASE_BUCKET, AWS_INSTANCE_ID, and AWS_RELEASE_STATUS_DOCUMENT')
        self.bucket, self.instance, self.document = bucket, instance, document

    def aws(self, *args):
        result = subprocess.run(['aws', *args, '--output', 'json'], capture_output=True,
                                text=True, timeout=90, env={**os.environ, 'AWS_PAGER': ''})
        if result.returncode:
            match = re.search(r'\(([^)]+)\) when calling', result.stderr)
            code = match.group(1) if match else 'AWSCommandFailed'
            raise RuntimeError(f'{args[0]} {args[1]} failed: {code}')
        return json.loads(result.stdout or '{}')

    def host(self):
        command = self.aws('ssm', 'send-command', '--instance-ids', self.instance,
                           '--document-name', self.document)['Command']['CommandId']
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                response = self.aws('ssm', 'get-command-invocation', '--command-id', command,
                                    '--instance-id', self.instance)
            except RuntimeError as error:
                if 'InvocationDoesNotExist' not in str(error):
                    raise
                time.sleep(2)
                continue
            if response['Status'] == 'Success':
                value = json.loads(response.get('StandardOutputContent', ''))
                if not isinstance(value, dict) or value.get('version') != 1:
                    raise ValueError('invalid host status response')
                return value
            if response['Status'] not in {'Pending', 'InProgress', 'Delayed'}:
                raise ValueError('read-only host status command failed: ' + command)
            time.sleep(2)
        raise ValueError('host status is unknown; read-only SSM command: ' + command)

    def get(self, key, *, optional=False):
        if optional:
            # Explicit scoped listing distinguishes absence from denied access.
            listing = self.aws('s3api', 'list-objects-v2', '--bucket', self.bucket,
                               '--prefix', key, '--max-keys', '1', '--no-paginate')
            if not any(item['Key'] == key for item in listing.get('Contents', [])):
                return None
        with tempfile.TemporaryDirectory(prefix='career-release-read-') as directory:
            path = Path(directory) / 'object'
            self.aws('s3api', 'get-object', '--bucket', self.bucket, '--key', key, str(path))
            if path.stat().st_size > 65536:
                raise ValueError('release metadata exceeds size limit')
            return path.read_bytes()

    def put(self, key, value, *, immutable=False):
        raw = encoded(value)
        with tempfile.TemporaryDirectory(prefix='career-release-write-') as directory:
            path = Path(directory) / 'object.json'
            path.write_bytes(raw)
            try:
                self.aws('s3api', 'put-object', '--bucket', self.bucket, '--key', key,
                         '--body', str(path), *(['--if-none-match', '*'] if immutable else []))
            except RuntimeError as error:
                if not immutable or 'PreconditionFailed' not in str(error):
                    raise
                if self.get(key) != raw:
                    raise ValueError('another candidate already owns this selection') from None


def settings(environment):
    value = {key: environment.get(key, '') for key in SETTINGS}
    if not all(value.values()):
        raise ValueError('release build settings are incomplete')
    return value


def candidate_key(instance, source, policy, build_settings):
    if not re.fullmatch(r'[a-f0-9]{40}', source):
        raise ValueError('source must be an exact commit')
    fingerprint = hashlib.sha256(encoded({'source_sha': source, 'policy': policy,
                                          'settings': build_settings})).hexdigest()
    return f'{PREFIX}/{instance}/candidates/{fingerprint}.json'


def verified_receipt(cloud, receipt):
    release, checksum = receipt['release_id'], receipt['manifest_sha256']
    validate_selection(release, checksum)
    raw = cloud.get(f'releases/{release}/manifest.json')
    result = verify(raw, release, checksum)
    if result != receipt:
        raise ValueError('candidate receipt differs from its immutable manifest')
    return result, json.loads(raw)


def validate_candidate(manifest, selection):
    config = selection['settings']
    if (manifest['source_sha'] != selection['source_sha']
            or manifest['release_policy'] != selection['policy']
            or manifest['hermes_base_image'] != config['HERMES_BASE_IMAGE']
            or manifest['tectonic_version'] != config['TECTONIC_VERSION']
            or manifest['app_image'].split('@')[0] != config['APP_REPOSITORY']
            or manifest['hermes_image'].split('@')[0] != config['HERMES_REPOSITORY']):
        raise ValueError('candidate does not match selected source, policy, and build settings')


def select(cloud, source, run_number, policy, build_settings):
    installed = require_idle(cloud.host())
    if installed and installed['source_sha'] == source:
        raw = cloud.get(f"releases/{installed['release_id']}/manifest.json")
        verify(raw, installed['release_id'], hashlib.sha256(raw).hexdigest())
        validate_candidate(json.loads(raw), {'source_sha': source, 'policy': policy, 'settings': build_settings})
        return {'action': 'already_installed', 'installed': installed}
    check_predecessor({'version': 1, 'state': 'idle', 'installed': installed}, policy)
    key = candidate_key(cloud.instance, source, policy, build_settings)
    existing = cloud.get(key, optional=True)
    selection = {'source_sha': source, 'policy': policy, 'settings': build_settings, 'key': key}
    if existing is not None:
        receipt, manifest = verified_receipt(cloud, json.loads(existing))
        validate_candidate(manifest, selection)
        check_predecessor(cloud.host(), policy)
        cloud.put(f'{PREFIX}/{cloud.instance}/latest.json', receipt)
        return {**selection, 'action': 'reuse', 'receipt': receipt, 'release_id': receipt['release_id']}
    release = source + '-' + run_number
    validate_selection(release, '0' * 64)
    return {**selection, 'action': 'build', 'release_id': release}


def publish(cloud, selection, receipt):
    verified, manifest = verified_receipt(cloud, receipt)
    validate_candidate(manifest, selection)
    if verified['release_id'] != selection['release_id']:
        raise ValueError('prepared release changed after selection')
    key = candidate_key(cloud.instance, selection['source_sha'], selection['policy'], selection['settings'])
    if key != selection['key']:
        raise ValueError('candidate selection key mismatch')
    check_predecessor(cloud.host(), selection['policy'])
    cloud.put(key, verified, immutable=True)
    cloud.put(f'{PREFIX}/{cloud.instance}/latest.json', verified)
    return verified


def check_deploy(cloud, receipt):
    receipt, manifest = verified_receipt(cloud, receipt)
    installed = require_idle(cloud.host())
    if installed and installed['release_id'] == receipt['release_id']:
        return {'status': 'already_installed', 'installed': installed}
    check_predecessor({'version': 1, 'state': 'idle', 'installed': installed}, manifest['release_policy'])
    return {'status': 'ready_to_install', 'release_id': receipt['release_id']}


def workflow_activity(repository):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('invalid GitHub repository')
    runs = {}
    paths = {f'.github/workflows/{name}.yml' for name in ('aws-release', 'aws-deploy', 'aws-terraform')}
    try:
        # Query active states directly so a long-running deployment cannot fall
        # outside the most recent page of completed runs. Deduplicate runs that
        # change state between these observations.
        for state in ('in_progress', 'queued', 'requested', 'waiting', 'pending'):
            response = subprocess.run(
                ['gh', 'api', '--paginate', '--slurp',
                 f'repos/{repository}/actions/runs?status={state}&per_page=100'],
                text=True, capture_output=True, timeout=30)
            if response.returncode:
                return {'state': 'unknown', 'runs': list(runs.values())}
            for page in json.loads(response.stdout):
                for run in page['workflow_runs']:
                    if run['path'] in paths and run['status'] != 'completed':
                        runs[run['id']] = {key: run[key] for key in ('id', 'name', 'status', 'head_sha', 'html_url')}
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return {'state': 'unknown', 'runs': list(runs.values())}
    return {'state': 'observed', 'runs': list(runs.values())}


def status(cloud, repository):
    host = cloud.host()
    raw = cloud.get(f'{PREFIX}/{cloud.instance}/latest.json', optional=True)
    prepared = None
    if raw is not None:
        receipt, _ = verified_receipt(cloud, json.loads(raw))
        installed = host.get('installed')
        disposition = ('installed' if installed and installed['release_id'] == receipt['release_id'] else
                       'ready' if host['state'] == 'idle' and receipt['expected_predecessor'] == installed else
                       'stale' if host['state'] == 'idle' else 'unknown')
        prepared = {**receipt, 'disposition': disposition}
    return {'observed_at': datetime.now(timezone.utc).isoformat(), 'host': host,
            'latest_prepared': prepared, 'workflows': workflow_activity(repository),
            'note': 'Host identity and maintenance state are observed; this is not an application health check.'}


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded(value))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('status', 'propose-policy', 'select', 'publish', 'check-deploy'))
    parser.add_argument('--bucket', default=os.environ.get('AWS_RELEASE_BUCKET') or os.environ.get('RELEASE_BUCKET'))
    parser.add_argument('--instance', default=os.environ.get('AWS_INSTANCE_ID') or os.environ.get('INSTANCE_ID'))
    parser.add_argument('--status-document', default=os.environ.get('AWS_RELEASE_STATUS_DOCUMENT'))
    parser.add_argument('--repository', default=os.environ.get('GITHUB_REPOSITORY', 'seankatauskas/career-platform'))
    parser.add_argument('--policy', type=Path, default=Path('deploy/release-policy.json'))
    parser.add_argument('--selection', type=Path, default=Path('.cache/release-selection.json'))
    parser.add_argument('--receipt', type=Path, default=Path('.cache/prepared-release.json'))
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    try:
        cloud = Cloud(args.bucket, args.instance, args.status_document)
        if args.command == 'status':
            value = status(cloud, args.repository)
        elif args.command == 'propose-policy':
            policy = validate_policy(json.loads(args.policy.read_text()))
            installed = require_idle(cloud.host())
            value = {**policy, 'predecessor': installed}
            if installed:
                value['test_baseline_sha'] = installed['source_sha']
            validate_policy(value)
        elif args.command == 'select':
            source = os.environ['GITHUB_SHA']
            if (os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('GITHUB_REF') != 'refs/heads/main'
                    or os.environ.get('GITHUB_EVENT_NAME') != 'workflow_dispatch'):
                raise ValueError('select must run through Prepare AWS release on main')
            head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
            dirty = subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=no'], text=True).strip()
            if head != source or dirty:
                raise ValueError('release checkout must be clean and match the dispatch SHA')
            value = select(cloud, source, os.environ['GITHUB_RUN_NUMBER'],
                           validate_policy(json.loads(args.policy.read_text())), settings(os.environ))
            write(args.selection, value)
            if value['action'] == 'reuse':
                write(args.receipt, value['receipt'])
            with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
                stream.write('build=' + str(value['action'] == 'build').lower() + '\n')
            if value.get('release_id'):
                with open(os.environ['GITHUB_ENV'], 'a') as stream:
                    stream.write('RELEASE_ID=' + value['release_id'] + '\n')
        elif args.command == 'publish':
            value = publish(cloud, json.loads(args.selection.read_text()), json.loads(args.receipt.read_text()))
        else:
            value = check_deploy(cloud, json.loads(args.receipt.read_text()))
        if args.output:
            write(args.output, value)
        if os.environ.get('GITHUB_STEP_SUMMARY'):
            with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as stream:
                stream.write('### Release coordination\n\n```json\n' + encoded(value).decode() + '```\n')
        print(encoded(value).decode(), end='')
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as error:
        parser.exit(1, 'Release coordination failed: ' + str(error) + '\n')


if __name__ == '__main__':
    main()
