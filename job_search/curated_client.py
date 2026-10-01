"""Small local publishing client; no catalog selection or ranking behavior."""
import http.client
import json

from .contracts import ContractError
from .curated import validate_publication
from .hermes_mcp import MCP_PROTOCOL_VERSION, MAX_MCP_REQUEST_BYTES
from .system import read_mcp_token


def publish(config, request):
    request = validate_publication(request)
    token = read_mcp_token(config.mcp_token_file)
    connection = http.client.HTTPConnection('127.0.0.1', config.mcp_port, timeout=30)

    def call(method, params, sequence):
        payload = json.dumps({'jsonrpc': '2.0', 'id': sequence, 'method': method, 'params': params}).encode()
        if len(payload) > MAX_MCP_REQUEST_BYTES:
            raise ContractError('publication exceeds the agent API request limit; shorten explanations')
        connection.request('POST', '/mcp', payload, {'Authorization': 'Bearer ' + token,
            'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': MCP_PROTOCOL_VERSION})
        response = connection.getresponse()
        body = response.read()
        if response.status != 200:
            raise ContractError(f'local agent API returned HTTP {response.status}; check that its updated service is running')
        value = json.loads(body)
        if value.get('error'):
            raise ContractError(value['error'].get('message', 'agent API request failed'))
        return value['result']

    try:
        initialized = call('initialize', {'protocolVersion': MCP_PROTOCOL_VERSION, 'capabilities': {},
                          'clientInfo': {'name': 'career-shortlist-publisher', 'version': '1'}}, 1)
        if initialized.get('protocolVersion') != MCP_PROTOCOL_VERSION:
            raise ContractError('local agent API protocol is incompatible')
        result = call('tools/call', {'name': 'publish_curated_shortlist', 'arguments': request}, 2)
        if result.get('isError'):
            raise ContractError(result['content'][0]['text'])
        receipt = result['structuredContent']
        origin = config.dashboard_https_origin or f'http://127.0.0.1:{config.dashboard_port}'
        return {**receipt, 'dashboard_url': origin.rstrip('/') + '/' + receipt['dashboard_path']}
    finally:
        connection.close()
