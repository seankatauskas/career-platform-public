#!/usr/bin/env python3
"""Publish allowlisted deployment facts, including an explicitly unknown SSM outcome."""
import argparse
import json
import os
from pathlib import Path
import subprocess


def summary(environment, invocation=None):
    value = {'release_id': environment.get('RELEASE_ID'), 'ssm_command_id': environment.get('COMMAND_ID'),
             'workflow_status': environment.get('JOB_STATUS'), 'deployment_status': 'not_started'}
    if value['ssm_command_id']:
        value['deployment_status'] = 'unknown'
        value['next_action'] = 'Inspect the existing SSM command and host operations status before retrying.'
    if isinstance(invocation, dict) and invocation:
        value['ssm_status'] = invocation.get('Status')
        try:
            result = json.loads(invocation.get('StandardOutputContent', ''))
        except (ValueError, TypeError):
            result = {}
        if not isinstance(result, dict):
            result = {}
        if invocation.get('Status') == 'Success' and result.get('status') in {'deployed','deployed_paused'}:
            value.update({key: result.get(key) for key in ('operation_id','previous_release','phase_times','backup')})
            value['deployment_status'] = result['status']
            value['next_action'] = 'Verify host status; a paused installation still requires explicit activation.'
        elif invocation.get('Status') in {'Failed','Cancelled','TimedOut'}:
            value['deployment_status'] = 'attention'
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    invocation = None
    if os.environ.get('COMMAND_ID') and os.environ.get('INSTANCE_ID'):
        try:
            result = subprocess.run(['aws','ssm','get-command-invocation','--command-id',os.environ['COMMAND_ID'],
                '--instance-id',os.environ['INSTANCE_ID'],'--output','json'], capture_output=True, text=True, timeout=30, check=True)
            invocation = json.loads(result.stdout)
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    value = summary(os.environ, invocation)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(value, indent=2) + '\n')
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as stream:
            stream.write('### Career Platform deployment\n\n```json\n' + json.dumps(value, indent=2) + '\n```\n')
    print(json.dumps(value))


if __name__ == '__main__':
    main()
