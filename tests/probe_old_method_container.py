"""Packaged native-shell continuation and workspace probe; synthetic model, no credentials."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

PROBE = r'''
import http.server,json,os,pathlib,socket,subprocess,threading
from job_search.job_reviews.native_client import native_codex_command
from job_search.job_reviews.old_method.workspace import Review
assert os.getuid()==10001
assert not pathlib.Path('/proc/net/route').read_text().splitlines()[1:]
assert 'CapEff:\t0000000000000000' in pathlib.Path('/proc/self/status').read_text()
assert 'NoNewPrivs:\t1' in pathlib.Path('/proc/self/status').read_text()
for name in ('auth_owner.py','model_gateway.py','old_method/store.py','old_method/host.py'):
 assert not pathlib.Path('/opt/reviewer/job_search/job_reviews',name).exists(),name
assert not pathlib.Path('/var/run/docker.sock').exists()
requests=[]
class Model(http.server.BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_POST(self):
  requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
  if len(requests)==1:
   item={'type':'custom_tool_call','id':'ct_probe','call_id':'call_probe','namespace':'functions','name':'exec','status':'completed',
    'input':'text(await tools.exec_command({cmd:"python -c \\\"from pathlib import Path; Path(\'/output/probe.txt\').write_text(\'native shell works\'); print(\'done\')\\\""}));'}
  else:
   item={'type':'message','id':'msg_probe','role':'assistant','status':'completed','content':[{'type':'output_text','text':'done','annotations':[]}]}
  events=[{'type':'response.output_item.done','output_index':0,'item':item},
   {'type':'response.completed','response':{'id':'resp_probe','status':'completed','output':[item],'usage':{'input_tokens':1,'output_tokens':1,'total_tokens':2}}}]
  body=''.join('data: '+json.dumps(event)+'\n\n' for event in events).encode()
  self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
pathlib.Path('/tmp/review-home').mkdir(exist_ok=True)
server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Model)
threading.Thread(target=server.serve_forever,daemon=True).start()
result=subprocess.run(native_codex_command(server.server_port),input='Execute the supplied probe.',text=True,capture_output=True,timeout=60)
server.shutdown()
assert result.returncode==0,(result.returncode,result.stderr[-1500:],result.stdout[-1500:])
assert pathlib.Path('/output/probe.txt').read_text()=='native shell works',result.stdout[-2000:]
assert len(requests)==2,len(requests)
print(json.dumps({'requests':requests,'isolation':'passed','native_shell':'passed','paid_calls':0}))
'''


def host_runtime_probe(image):
    """Exercise the production launcher, socket bridge and entrypoint on Linux."""
    if sys.platform != 'linux' or os.getuid() != 0:
        return 'requires Linux root coordinator; exercised in release CI'
    from job_search.contracts import payload_sha256
    from job_search.job_reviews.contracts import fingerprint
    from job_search.job_reviews.model_gateway import gateway_server
    from job_search.job_reviews.old_method import runtime
    from job_search.job_reviews.old_method.workspace import validate_result
    from job_search.job_reviews.runner_config import RunnerConfig

    job = {'ats': 'ashby', 'id': 'probe', 'title': 'Software Engineer', 'company': 'example',
           'location': 'United States', 'description': 'Build Python APIs.',
           'posted_at': '2026-10-08T12:00:00Z', 'closed_at': None}
    context = {'facts': [{'fact_id': 'f1', 'source': 'resume', 'text': 'Synthetic fixture'}]}
    packet = {'workflow': 'old-method-v1', 'review_id': 'probe', 'screen_version': 'old-method-screen-v1',
              'window_start': '2026-10-08T00:00:00Z', 'window_end': '2026-10-09T00:00:00Z',
              'jobs': [job], 'context': context, 'context_sha256': payload_sha256(context),
              'sources_sha256': payload_sha256([fingerprint(job)])}
    requests = []

    def transport(body, headers):
        requests.append(json.loads(body))
        assert 'Authorization' not in headers and 'authorization' not in headers
        if len(requests) == 1:
            command = "python -c 'from job_search.job_reviews.old_method.workspace import Review; r=Review(); print(r.finish([]))'"
            item = {'type': 'custom_tool_call', 'id': 'ct_host_probe', 'call_id': 'call_host_probe',
                    'namespace': 'functions', 'name': 'exec', 'status': 'completed',
                    'input': 'text(await tools.exec_command(' + json.dumps({'cmd': command}) + '));'}
        else:
            item = {'type': 'message', 'id': 'msg_host_probe', 'role': 'assistant', 'status': 'completed',
                    'content': [{'type': 'output_text', 'text': 'done', 'annotations': []}]}
        events = [{'type': 'response.output_item.done', 'output_index': 0, 'item': item},
                  {'type': 'response.completed', 'response': {'id': 'resp_host_probe', 'status': 'completed',
                   'output': [item], 'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}}]
        return 200, ''.join('data: ' + json.dumps(e) + '\n\n' for e in events).encode()

    def gateway(*args, **kwargs):
        return gateway_server(*args, **kwargs, transport=transport)

    with tempfile.TemporaryDirectory(prefix='om-probe-') as temporary:
        root = Path(temporary)
        config = RunnerConfig(application_config=root/'unused.json', state_dir=root/'state',
                              runtime_dir=root/'sockets', auth_home=root/'unused-auth', model_image=image)
        fake_auth = SimpleNamespace(_request_headers=lambda **kwargs: {})
        # Never touch real credentials or reconcile any other worker during a probe.
        with patch.object(runtime, 'NativeAuthOwner', return_value=fake_auth), \
             patch.object(runtime, 'gateway_server', gateway), \
             patch.object(runtime, 'cleanup_inventory', return_value={'removed_container_ids': []}):
            with runtime.worker(config, packet, root/'attempt') as handle:
                deadline = time.monotonic() + 120
                while handle.poll() is None and time.monotonic() < deadline:
                    time.sleep(.1)
                if handle.poll() is None:
                    handle.terminate()
                    raise AssertionError('host runtime probe timed out')
                assert handle.wait() == 0, ((root/'attempt/stderr.log').read_text()[-3000:] +
                                           (root/'attempt/codex.jsonl').read_text()[-3000:])
                result = runtime.read_output(handle.output_directory/'result.json')
                validate_result(packet, result)
                assert result == runtime.read_output(handle.output_directory/'progress.json')
        assert len(requests) == 2, len(requests)
    return 'passed'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--require-host-runtime', action='store_true')
    args = parser.parse_args()
    if args.require_host_runtime and (sys.platform != 'linux' or os.getuid() != 0):
        parser.error('complete host runtime probe requires the Linux root coordinator')
    command = ['docker', 'run', '--rm', '--platform', 'linux/amd64', '--network', 'none',
               '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
               '--user', '10001:10001', '--memory', '1g', '--pids-limit', '96',
               '--tmpfs', '/tmp:rw,nosuid,nodev,noexec,size=128m,mode=1777',
               '--tmpfs', '/output:rw,nosuid,nodev,size=64m,mode=0700,uid=10001,gid=10001',
               '--entrypoint', 'python', args.image, '-c', PROBE]
    result = subprocess.run(command, capture_output=True, text=True, timeout=90)
    if result.returncode:
        raise RuntimeError(result.stderr[-5000:] + result.stdout[-2000:])
    value = json.loads(result.stdout)
    from job_search.job_reviews.native_protocol import validate_request
    for request in value.pop('requests'):
        validate_request(request)
    value['host_runtime'] = host_runtime_probe(args.image)
    print(json.dumps(value))


if __name__ == '__main__':
    main()
