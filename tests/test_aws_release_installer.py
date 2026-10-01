#!/usr/bin/env python3
"""Offline release integrity, fresh-host setup, and deployment timeout checks."""
import importlib.machinery
import importlib.util
import io
import json
from pathlib import Path
import shutil
import tarfile
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

loader = importlib.machinery.SourceFileLoader('release_installer', str(Path(__file__).parents[1] / 'deploy/aws/install-release'))
spec = importlib.util.spec_from_loader(loader.name, loader)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)

SOURCE = 'a' * 40
RID = SOURCE + '-1'
IMAGE = '123456789012.dkr.ecr.us-east-2.amazonaws.com/career/app@sha256:' + 'b' * 64


def fixture(root):
    source = root / 'source'
    source.mkdir()
    internal = dict(version=1, release_id=RID, source_sha=SOURCE, app_image=IMAGE,
                    hermes_image=IMAGE, hermes_base_image=IMAGE,
                    schema_compatibility='reviewed-v1', tectonic_version='tectonic 0.15.0', operations_protocol=1,
                    release_policy={'version':1, 'schema_compatibility':'reviewed-v1', 'test_baseline_sha':'0'*40, 'predecessor':None},
                    transition_validation={'schema_version':1, 'passed':True, 'runtime':'docker', 'source_sha':SOURCE, 'baseline_sha':'0'*40, 'rollback_passed':False})
    with tarfile.open(source / 'release.tar.gz', 'w:gz') as archive:
        for name, data in [('release.json', json.dumps(internal).encode()),
                           ('scripts/job-search-ops', b'fixture entrypoint')]:
            info = tarfile.TarInfo(name)
            info.mode = 0o755
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    external = {**internal, 'bundle_sha256': installer.sha(source / 'release.tar.gz')}
    (source / 'manifest.json').write_text(json.dumps(external))
    c = dict(release_root=str(root / 'installed'), aws_region='us-east-2', release_bucket='fixture-bucket')

    def download(*args, **kwargs):
        if 'get-login-password' in args:
            return 'REGISTRY_PASSWORD_MUST_NOT_BE_LOGGED'
        output = Path(args[-2])
        shutil.copyfile(source / output.name, output)
        return ''
    return c, installer.sha(source / 'manifest.json'), download


def test_blocked_setup_stages_tools_without_switching_or_deploying():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        c, expected, download = fixture(root)
        with patch.object(installer, 'call', side_effect=download), patch.object(installer, 'operations', return_value=(2, {'status': 'error'})) as ops:
            result = installer.install(root / 'config', c, RID, expected)
        assert result['status'] == 'blocked_setup'
        assert result['issues'] == ['secret_setup_failed']
        assert Path(result['staged_path']).joinpath('scripts/job-search-ops').is_file()
        assert not (Path(c['release_root']) / 'current').exists()
        assert ops.call_count == 1 and ops.call_args.args[2] == 'secrets'


def test_preflight_reports_missing_compiler_without_registry_login():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        c, expected, download = fixture(root)
        results = [(0, {'status': 'secrets_materialized'}),
                   (2, {'status': 'blocked_setup', 'issues': ['missing_tectonic', 'missing_tectonic.bundle']})]
        with patch.object(installer, 'call', side_effect=download), patch.object(installer, 'operations', side_effect=results), patch.object(installer.subprocess, 'run') as process:
            result = installer.install(root / 'config', c, RID, expected)
        assert result['issues'] == ['missing_tectonic', 'missing_tectonic.bundle']
        assert process.call_count == 0


def test_verified_install_allows_full_deployment_timeout_and_idempotent_restage():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        c, expected, download = fixture(root)
        for attempt in range(2):
            results = [(0, {'status': 'secrets_materialized'}),
                       (0, {'status': 'configuration_ready', 'issues': []}),
                       (0, {'status': 'deployed_paused', 'release_id': RID})]
            with patch.object(installer, 'call', side_effect=download), patch.object(installer, 'operations', side_effect=results) as ops, patch.object(installer.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as process:
                result = installer.install(root / 'config', c, RID, expected)
            assert result['status'] == 'deployed_paused'
            assert ops.call_args.kwargs['timeout'] > 10800
            assert ops.call_args.args[2:] == ('deploy', '--release', RID)
            assert process.call_args.kwargs['input'] == 'REGISTRY_PASSWORD_MUST_NOT_BE_LOGGED'


def test_bad_manifest_never_executes_downloaded_code():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        c, _, download = fixture(root)
        with patch.object(installer, 'call', side_effect=download), patch.object(installer, 'operations') as ops:
            try:
                installer.install(root / 'config', c, RID, '0' * 64)
            except RuntimeError as error:
                assert str(error) == 'manifest checksum mismatch'
            else:
                raise AssertionError('bad checksum accepted')
        assert ops.call_count == 0


def test_existing_deployment_does_not_refresh_secrets_before_transaction():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        c, expected, download = fixture(root)
        release_root = Path(c['release_root'])
        old_release = release_root / 'releases' / ('c' * 40 + '-1')
        old_release.mkdir(parents=True)
        (release_root / 'current').symlink_to(old_release, target_is_directory=True)
        with patch.object(installer, 'call', side_effect=download), patch.object(installer, 'operations', return_value=(0, {'status': 'deployed', 'release_id': RID})) as ops, patch.object(installer.subprocess, 'run', return_value=SimpleNamespace(returncode=0)):
            result = installer.install(root / 'config', c, RID, expected)
        assert result['status'] == 'deployed'
        assert ops.call_count == 1 and ops.call_args.args[2] == 'deploy'


def test_installation_lock_rejects_concurrent_release_changes():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        c, expected, _ = fixture(root)
        release_root = Path(c['release_root'])
        release_root.mkdir()
        with installer.installation_lock(release_root):
            try:
                installer.install(root / 'config', c, RID, expected)
            except RuntimeError as error:
                assert str(error) == 'another release installation is active'
            else:
                raise AssertionError('concurrent installation accepted')


def test_extractor_rejects_escape_duplicate_and_link_members():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        for index, names in enumerate((['../escape'], ['same', './same'], ['link'])):
            bundle = root / f'{index}.tar.gz'
            with tarfile.open(bundle, 'w:gz') as archive:
                for name in names:
                    info = tarfile.TarInfo(name)
                    if name == 'link':
                        info.type = tarfile.SYMTYPE
                        info.linkname = '/etc/passwd'
                    archive.addfile(info)
            try:
                installer.extract_release(bundle, root / f'out-{index}')
            except RuntimeError:
                pass
            else:
                raise AssertionError('unsafe member accepted')


if __name__ == '__main__':
    tests = [value for name, value in sorted(globals().items()) if name.startswith('test_')]
    for test in tests:
        test()
    print(f'ok ({len(tests)} release installer tests)')
