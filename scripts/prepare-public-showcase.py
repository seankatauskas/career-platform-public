#!/usr/bin/env python3
"""Export reviewed source files without Git history or untracked runtime data."""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('private_guard', ROOT / 'scripts/check-private-files.py')
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)
ROOT_NAMES = {'LICENSE', 'Dockerfile', 'Dockerfile.hermes', 'Dockerfile.runpod-embedding', '.dockerignore', '.gitignore', 'job_search/collection/boards.seed.json', 'tests/browser/test_console_browser.mjs', 'tests/browser/test_ops_browser.mjs', 'tests/fixtures/resume_lab_v3.sql', 'launchd/com.local.job-search-worker.plist.template', 'scripts/acceptance-tex-files.tsv', 'tests/fixtures/mail/malicious_messages.fixture'}
# Reviewed source additions; new skills and container files still require review.
ROOT_NAMES.update({
    'Dockerfile.codex-review',
    'Dockerfile.codex-review.dockerignore',
    'skills/career-job-review/SKILL.md',
    'skills/career-job-review/references/interface.md',
})
TREES = {'tests', 'examples', 'requirements', 'job_search', 'extension', 'scripts', 'deploy', 'infra', 'docs', 'openwiki', '.github'}
SOURCE_SUFFIXES = {'.py', '.js', '.mjs', '.json', '.md', '.txt', '.css', '.html', '.tex', '.tf', '.tftpl', '.hcl', '.sh', '.yaml', '.yml', '.service', '.timer', '.example', '.png', '.mp4'}
BANNED_SUFFIXES = {'.db', '.sqlite', '.sqlite3', '.pem', '.key', '.token', '.p12', '.pt', '.safetensors', '.csv'}
SECRETS = [re.compile(rb'AKIA[0-9A-Z]{16}'), re.compile(rb'gh[pousr]_[A-Za-z0-9]{30,}'), re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'), re.compile(rb'sk-proj-[A-Za-z0-9_-]{40,}')]


def export_tree(root: Path, revision: str, target: Path) -> dict:
    revision = subprocess.check_output(['git', 'rev-parse', '--verify', revision + '^{commit}'], cwd=root, text=True).strip()
    failures = guard.inspect_index(root, revision)
    if failures:
        raise ValueError('Public export refused: ' + '; '.join(failures))
    target=target.resolve()
    if target.exists():
        raise ValueError('Use a new output directory; existing files are never replaced.')
    tracked=subprocess.check_output(['git','ls-tree','-rz',revision],cwd=root).decode().split('\0')
    files={}
    # Inert examples may already exist; the current active workflow takes precedence.
    entries=[]
    for record in filter(None, tracked):
        metadata, name = record.split('\t', 1)
        mode, kind, oid = metadata.split()
        if kind != 'blob' or mode not in {'100644', '100755'}:
            raise ValueError(f'Non-regular source requires review: {name}')
        entries.append((name, mode, oid))
    for name, mode, oid in sorted(entries, key=lambda item: item[0].startswith('.github/workflows/')):
        path=Path(name)
        if path.suffix in BANNED_SUFFIXES or any(part in {'.git','.cache','node_modules','__pycache__'} for part in path.parts):
            raise SystemExit(f'Runtime data is tracked: {name}')
        allowed=name == '.githooks/pre-commit' or name in ROOT_NAMES or (len(path.parts)==1 and (path.suffix in {'.py','.md','.txt','.yaml'} or path.name.endswith('.example.json'))) or (path.parts[0] in TREES and (path.suffix in SOURCE_SUFFIXES or not path.suffix))
        if not allowed:
            raise SystemExit(f'Unreviewed source path: {name}')
        content=subprocess.check_output(['git','cat-file','blob',oid],cwd=root)
        if path.suffix not in {'.png','.mp4'} and any(pattern.search(content) for pattern in SECRETS):
            raise SystemExit(f'Possible credential requires review: {name}')
        # Keep credential-free public checks active; deployment/publishing are examples.
        if name.startswith('.github/workflows/') and name != '.github/workflows/public-checks.yml':
            name = name.replace('.github/workflows/', '.github/workflow-examples/', 1)
        if name == 'README.md':
            content += b"\n## Public source snapshots\n\nThis repository receives a daily source snapshot when there are changes, scheduled for 10 PM America/Chicago. Its history starts with one initial release. Offline checks run here; deployment and publishing workflows are included only as inactive setup examples.\n"
        files[name]=(content, int(mode, 8) & 0o777)
    target.mkdir(parents=True)
    manifest=[]
    for name,(content,mode) in sorted(files.items()):
        dest=target/name;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(content);dest.chmod(mode)
        manifest.append({'path':name,'sha256':hashlib.sha256(content).hexdigest()})
    return {'output':str(target),'files':manifest,'source_sha':revision,'history_included':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--revision',default='HEAD')
    args=parser.parse_args()
    report=export_tree(ROOT,args.revision,args.output)
    receipt=ROOT/'.cache/public-export.json';receipt.parent.mkdir(exist_ok=True)
    receipt.write_text(json.dumps(report,indent=2)+'\n')
    files=report['files'];target=args.output
    print(f'Prepared {len(files)} reviewed files at {target}; no Git history or runtime state.')


if __name__=='__main__':
    main()
