"""Worker-safe native Codex setup and bounded routing metadata.

No authentication owner, model gateway, storage, or host authority is imported.
Execution still requires the existing credentialless network-none Docker worker.
"""
from __future__ import annotations

import json

from .codex_runtime import MODEL, codex_command

REQUEST_HEADERS = frozenset((
    'session-id', 'thread-id', 'x-client-request-id', 'x-codex-window-id',
    'x-codex-turn-metadata', 'x-codex-beta-features', 'x-codex-turn-state',
    'x-openai-internal-codex-responses-lite', 'originator', 'user-agent',
))
RESPONSE_HEADERS = frozenset(('x-codex-turn-state', 'x-request-id'))


def forwarding_headers(headers, *, response=False):
    """Forward bounded protocol metadata, never upstream identity or cookies."""
    permitted = RESPONSE_HEADERS if response else REQUEST_HEADERS
    result = {}
    for key, value in headers.items():
        name = key.lower()
        if not response and name in ('authorization', 'chatgpt-account-id', 'cookie'):
            raise ValueError('worker credentials are prohibited')
        if name not in permitted:
            continue
        if (name in result or not isinstance(value, str) or len(value) > 8192 or
                any(ord(c) < 32 or ord(c) > 126 for c in value)):
            raise ValueError('invalid protocol header')
        result[name] = value
    if sum(len(k) + len(v) for k, v in result.items()) > 32768:
        raise ValueError('protocol headers exceed bound')
    return result


routing_headers = forwarding_headers


def native_codex_command(port, model=MODEL, effort='high', executable='/opt/codex/bin/codex'):
    """Use only inside the existing credentialless, network-none Docker worker."""
    if model != MODEL or effort != 'high':
        raise ValueError('native review requires Astra high')
    original = codex_command(port, model, effort, executable)
    command, settings, index = [], {}, 0
    while index < len(original) - 1:
        if original[index] == '-c':
            key, raw = original[index + 1].split('=', 1)
            if not key.startswith('mcp_servers.'):
                settings[key] = json.loads(raw)
            index += 2
        else:
            command.append(original[index])
            index += 1
    command[command.index('--sandbox') + 1] = 'danger-full-access'
    settings.update({'agents.enabled': False, 'features.shell_tool': True,
                     'agents.max_threads': 1, 'features.unified_exec': True,
                     'allow_login_shell': False})
    for key, value in settings.items():
        command += ['-c', key + '=' + json.dumps(value)]
    return command + ['-']
