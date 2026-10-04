"""Explicit no-inference Docker acceptance probe; requires a built reviewer image.

Run: python -m tests.probe_codex_review_container --image <immutable-image-id>
The only model server is a synthetic loopback HTTP server inside network=none.
"""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile


PROBE = r'''
import errno,http.server,json,os,pathlib,socket,socketserver,subprocess,threading
from job_search.job_reviews.codex_runtime import codex_command
assert os.getuid()==10001
assert not pathlib.Path('/proc/net/route').read_text().splitlines()[1:]
status=pathlib.Path('/proc/self/status').read_text()
assert 'CapEff:\t0000000000000000' in status
assert 'NoNewPrivs:\t1' in status
assert not pathlib.Path('/var/run/docker.sock').exists()
try:
 assert not pathlib.Path('/root/.aws').exists()
except PermissionError: pass
assert not pathlib.Path('/opt/reviewer/job_search/ranking').exists()
assert not pathlib.Path('/opt/reviewer/job_search/job_reviews/auth_owner.py').exists()
assert not pathlib.Path(HOST_SENTINEL).exists()
assert not any(k.startswith(('AWS_', 'OPENAI_API_', 'SSH_', 'CAREER_DASHBOARD')) for k in os.environ)
try:
 pathlib.Path('/opt/reviewer/forbidden').write_text('bad')
except OSError: pass
else: raise AssertionError('image root writable')
for address in ('169.254.169.254','1.1.1.1','172.17.0.1'):
 try:
  connection=socket.create_connection((address,80),timeout=.3)
 except OSError: pass
 else: connection.close();raise AssertionError('network reachable')
pathlib.Path('/tmp/review-home').mkdir(mode=0o700,exist_ok=True)
requests=[]
review_requests=[]
class Review(http.server.BaseHTTPRequestHandler):
 def do_GET(self):
  review_requests.append(self.path)
  assert self.path=='/v1/assignment'
  data=b'{"grant_id":"synthetic","kind":"primary","jobs":[]}'
  self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
 def log_message(self,*args):pass
review=socketserver.UnixStreamServer('/review/review.sock',Review)
os.chmod('/review/review.sock',0o600)
threading.Thread(target=review.serve_forever,daemon=True).start()
class Model(http.server.BaseHTTPRequestHandler):
 def do_POST(self):
  value=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
  assert value['model']=='gpt-6-astra' and value['reasoning']['effort']=='high'
  assert not self.headers.get('Authorization')
  requests.append(value)
  item={'type':'message','id':'msg_test','status':'completed','role':'assistant','content':[{'type':'output_text','text':'Synthetic completion.','annotations':[]}]}
  events=[{'type':'response.created','response':{'id':'resp_test','object':'response','status':'in_progress','output':[]}},
          {'type':'response.output_item.done','output_index':0,'item':item},
          {'type':'response.completed','response':{'id':'resp_test','object':'response','status':'completed','output':[item],'usage':{'input_tokens':1,'output_tokens':1,'total_tokens':2}}}]
  data=''.join('data: '+json.dumps(e)+'\n\n' for e in events).encode()
  if len(requests)==1: data=FIRST_RESPONSE
  self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
 def log_message(self,*args):pass
server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Model)
threading.Thread(target=server.serve_forever,daemon=True).start()
command=codex_command(server.server_port)
run=subprocess.run(command,capture_output=True,text=True,timeout=30)
server.shutdown()
review.shutdown();review.server_close()
assert run.returncode==0,(run.returncode,run.stdout[-2000:],run.stderr[-2000:])
assert len(requests)==2, (len(requests),run.stdout[-1600:])
assert review_requests==['/v1/assignment'], (review_requests,run.stdout[-2000:])
assert 'Synthetic completion.' in run.stdout
assert not pathlib.Path('/tmp/review-home/auth.json').exists()
print(json.dumps({'requests': requests, 'isolation_checks':'passed','native_codex_mock_completion':'passed','model':'gpt-6-astra','reasoning_effort':'high','paid_calls':0}))
'''


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    args = parser.parse_args(argv)
    from job_search.job_reviews.codex_runtime import RuntimeConfig, readiness
    config = RuntimeConfig(args.image)
    if not readiness(config)['ready']:
        parser.error('pinned Linux amd64 reviewer image is unavailable')
    with tempfile.TemporaryDirectory(prefix='review-host-sentinel-') as temp:
        sentinel = Path(temp) / 'private-ranking.db'
        sentinel.write_text('HOST_RANKING_SENTINEL')
        from job_search.job_reviews.model_gateway import native_response
        final = {'type': 'response.completed', 'response': {'id': 'resp_test', 'status': 'completed',
            'output': [{'type': 'function_call', 'id': 'fc1', 'call_id': 'call1',
                        'name': 'mcp__review__review_assignment', 'arguments': '{}'}]}}
        first_response = native_response(('data: ' + json.dumps(final) + '\n\n').encode())
        code = 'HOST_SENTINEL=' + repr(str(sentinel)) + '\nFIRST_RESPONSE=' + repr(first_response) + '\n' + PROBE
        command = ['docker', 'run', '--rm', '--platform', 'linux/amd64', '--pull', 'never', '--network', 'none', '--read-only',
                   '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true', '--pids-limit', '96',
                   '--memory', '1g', '--memory-swap', '1g', '--cpus', '1', '--user', '10001:10001',
                   '--ipc', 'none', '--log-driver', 'none', '--tmpfs', '/tmp:rw,nosuid,nodev,noexec,size=128m,mode=1777',
                   '--tmpfs', '/review:rw,nosuid,nodev,noexec,size=1m,mode=0700,uid=10001,gid=10001',
                   '--env', 'HOME=/tmp/review-home', '--env', 'CODEX_HOME=/tmp/review-home',
                   '--entrypoint', 'python', config.image, '-c', code]
        run = subprocess.run(command, capture_output=True, text=True, timeout=60)
        if run.returncode:
            raise RuntimeError('Native isolation probe failed: ' + run.stderr[-3000:])
        result = json.loads(run.stdout)
        from job_search.job_reviews.model_gateway import validate_request
        for value in result.pop('requests'):
            validate_request(value)
        result['native_gateway_protocol'] = 'passed'
        print(json.dumps(result))


if __name__ == '__main__':
    main()
