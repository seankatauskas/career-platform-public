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
import errno,hashlib,http.server,json,os,pathlib,socket,socketserver,subprocess,threading
from job_search.job_reviews.codex_runtime import codex_command,preload_evidence,review_prompt
from job_search.job_reviews.contracts import fingerprint
from job_search.job_reviews.reviewer_api import ReviewerClient,validate_arguments
from job_search.job_reviews.reviewer_mcp import tools_for
validate_arguments('order',{'ordinals':list(range(1,6001)),
 'groups':[{'id':'related','label':'Related roles','ordinals':[1,2]}]})
if ADJUDICATOR:
 assert {t['name'] for t in tools_for('adjudicator','job-review-v2')}=={'review_assignment','review_context','review_job','review_disagreement','review_resolutions'}
 validate_arguments('disagreement',{'ordinal':1,'offset':0,'limit':4000})
 validate_arguments('resolutions',{'resolutions':RESOLUTIONS})
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
description='Full original source evidence. '*(2400 if SCREENING else 3000)+'FINAL_SOURCE_SENTINEL'
jobs=[{'ats':'ashby','id':str(n),'title':'Synthetic Engineer','description':description} for n in (1,2)]
assignment={'grant_id':'synthetic','kind':'primary' if SCREENING else 'adjudicator' if ADJUDICATOR else 'check','purpose':'screening' if SCREENING else 'detailed','rubric_version':'job-review-v2','context_fingerprint':'fixed',
 'jobs':[{'ordinal':n,'expected_revision':0,'snapshot_sha256':fingerprint(j),'submitted':False,
          'job':{k:v for k,v in j.items() if k!='description'}} for n,j in enumerate(jobs,1)]}
saved=[]
class Review(http.server.BaseHTTPRequestHandler):
 def respond(self,value):
  data=json.dumps(value).encode()
  self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
 def do_GET(self):
  review_requests.append(self.path)
  assert self.path=='/v1/assignment'
  self.respond(assignment)
 def do_POST(self):
  review_requests.append(self.path)
  args=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
  if self.path=='/v1/context':
   section=args['section'];offset=args['offset']
   values=[{'fact_id':'fact'+str(i),'source':'projects','text':'Built software'} for i in range(25)] if section=='facts' else []
   end=offset+args['limit']
   self.respond({'section':section,section:values[offset:end],'total':len(values),'next_offset':end if end<len(values) else None,
                 'fingerprint':'fixed','rubric_version':'job-review-v2'})
  elif self.path=='/v1/job':
   n=args['ordinal'];offset=args['offset'];end=offset+args['limit']
   self.respond({'ordinal':n,'revision':0,'snapshot_sha256':fingerprint(jobs[n-1]),'job':assignment['jobs'][n-1]['job'],
                 'description':description[offset:end],'offset':offset,'description_chars':len(description),
                 'next_offset':end if end<len(description) else None})
  elif self.path=='/v1/disagreement':
   assert ADJUDICATOR
   pair={'ordinal':args['ordinal'],'basis_sha256':'b'*64,'primary':ASSESSMENT,
         'check':dict(ASSESSMENT,alignment='adjacent'),'differing_dimensions':['alignment']}
   payload=json.dumps(pair,ensure_ascii=False,sort_keys=True,separators=(',',':'))
   offset=args.get('offset',0);end=min(offset+args.get('limit',4000),len(payload))
   self.respond({'ordinal':args['ordinal'],'basis_sha256':'b'*64,'encoding':'canonical_json',
     'payload_sha256':hashlib.sha256(payload.encode()).hexdigest(),'content':payload[offset:end],
     'offset':offset,'total_chars':len(payload),'next_offset':end if end<len(payload) else None})
  elif self.path=='/v1/resolutions':
   assert ADJUDICATOR
   assert args['resolutions']==(RESOLUTIONS if not saved else RESOLUTIONS[1:])
   ordinal=1 if not saved else 2
   saved.append(ordinal)
   results=[{'ordinal':ordinal,'status':'saved','receipt':{'review_id':'synthetic','ordinal':ordinal,'basis_sha256':'b'*64,'choice':'unresolved'}}]
   if ordinal==1:results.append({'ordinal':2,'status':'error','error':'validation_failed'})
   self.respond({'results':results})
  elif self.path=='/v1/routes':
   assert SCREENING
   ordinals=[x['ordinal'] for x in args['routes']]
   assert all(x['route']=='detailed' for x in args['routes'])
   assert ordinals==([1,2] if not saved else [2])
   ordinal=1 if not saved else 2
   saved.append(ordinal)
   results=[{'ordinal':ordinal,'status':'saved','receipt':{'review_id':'synthetic','ordinal':ordinal,'route':'detailed','revision':0}}]
   if ordinal==1:results.append({'ordinal':2,'status':'error','error':'validation_failed'})
   self.respond({'results':results})
  elif self.path=='/v1/assessments':
   assert [x['ordinal'] for x in args['assessments']]==[1,2]
   saved.append(1)
   self.respond({'results':[{'ordinal':1,'status':'saved','receipt':{'review_id':'synthetic','ordinal':1,'revision':1}},
                            {'ordinal':2,'status':'error','error':'validation_failed'}]})
  elif self.path=='/v1/assessment':
   assert args['ordinal']==2
   saved.append(2)
   self.respond({'review_id':'synthetic','ordinal':2,'revision':1})
  else: raise AssertionError('unapproved review operation')
 def log_message(self,*args):pass
review=socketserver.UnixStreamServer('/review/review.sock',Review)
os.chmod('/review/review.sock',0o600)
threading.Thread(target=review.serve_forever,daemon=True).start()
class Model(http.server.BaseHTTPRequestHandler):
 def do_POST(self):
  value=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
  assert value['model']==CODEX_MODEL and value['reasoning']['effort']==CODEX_EFFORT
  assert not self.headers.get('Authorization')
  requests.append(value)
  assert len(requests)<=3
  data=[FIRST_RESPONSE,SECOND_RESPONSE,THIRD_RESPONSE][len(requests)-1]
  self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
 def log_message(self,*args):pass
server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Model)
threading.Thread(target=server.serve_forever,daemon=True).start()
command=codex_command(server.server_port,CODEX_MODEL,CODEX_EFFORT)
packet=preload_evidence(ReviewerClient('/review/review.sock'),expected_purpose='screening' if SCREENING else 'detailed')
if ADJUDICATOR:
 assert all(item['disagreement']['primary']==ASSESSMENT for item in packet['jobs'])
 assert all(item['disagreement']['check']==dict(ASSESSMENT,alignment='adjacent') for item in packet['jobs'])
prompt=review_prompt(packet)
assert len(prompt.encode())>128*1024
assert command[-1]=='-' and len(' '.join(command))<10000
run=subprocess.run(command,input=prompt,capture_output=True,text=True,timeout=40)
server.shutdown()
review.shutdown();review.server_close()
assert run.returncode==0,(run.returncode,run.stdout[-2000:],run.stderr[-2000:])
assert len(requests)==3, (len(requests),run.stdout[-1600:])
assert saved==[1,2],(saved,run.stdout[-2000:])
assert review_requests.count('/v1/context')==4
assert review_requests.count('/v1/job')>20
if ADJUDICATOR:
 assert review_requests.count('/v1/disagreement')>20
 assert review_requests.count('/v1/resolutions')==2
 assert '/v1/assessments' not in review_requests and '/v1/assessment' not in review_requests
elif SCREENING:
 assert review_requests.count('/v1/routes')==2
 assert '/v1/assessments' not in review_requests and '/v1/assessment' not in review_requests
else:
 assert review_requests.count('/v1/assessments')==1 and review_requests.count('/v1/assessment')==1
assert any(prompt==part.get('text') for item in requests[0]['input']
 for part in (item.get('content') or []) if isinstance(part,dict)), 'complete stdin prompt was not delivered'
assert 'Synthetic completion.' in run.stdout
assert not pathlib.Path('/tmp/review-home/auth.json').exists()
print(json.dumps({'requests': requests, 'isolation_checks':'passed','native_codex_mock_completion':'passed',
 'screening':SCREENING,'adjudicator':ADJUDICATOR,'stdin_preload_over_128k':'passed','bulk_partial_failure_continuation':'passed','model':CODEX_MODEL,'reasoning_effort':CODEX_EFFORT,'paid_calls':0}))
'''


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--adjudicator', action='store_true', help='Exercise paged paired judgments and bulk resolution continuation')
    modes.add_argument('--screening', action='store_true', help='Exercise purpose-bound bulk routing continuation')
    args = parser.parse_args(argv)
    from job_search.job_reviews.codex_runtime import RuntimeConfig, readiness
    config = RuntimeConfig(args.image, model='gpt-6-luna' if args.screening else 'gpt-6-astra',
                           reasoning_effort='low' if args.screening else 'high', screening_enabled=args.screening,
                           adjudication_enabled=args.adjudicator,
                           purpose='screening' if args.screening else 'detailed')
    scope = {'kind': 'primary' if args.screening else 'adjudicator' if args.adjudicator else 'check', 'rubric_version': 'job-review-v2',
             'purpose': config.purpose}
    if not readiness(config)['ready']:
        parser.error('pinned Linux amd64 reviewer image is unavailable')
    with tempfile.TemporaryDirectory(prefix='review-host-sentinel-') as temp:
        sentinel = Path(temp) / 'private-ranking.db'
        sentinel.write_text('HOST_RANKING_SENTINEL')
        from job_search.job_reviews.model_gateway import native_response
        def output_stream(items):
            events = [{'type': 'response.output_item.done', 'output_index': i, 'item': item}
                      for i, item in enumerate(items)]
            events.append({'type': 'response.completed', 'response': {
                'id': 'resp_test', 'status': 'completed', 'output': []}})
            return native_response(''.join('data: ' + json.dumps(event) + '\n\n' for event in events).encode(),
                                   **scope)
        message = {'type': 'message', 'id': 'msg_comment', 'status': 'completed', 'role': 'assistant',
                   'phase': 'commentary', 'content': [{'type': 'output_text',
                   'text': 'Assessing the complete preloaded synthetic evidence.', 'annotations': [], 'logprobs': []}]}
        assessment = {'stage': 'detailed', 'decision': 'needs_info', 'family': 'backend', 'alignment': 'core',
            'reason_code': 'uncertain', 'explanation': 'Synthetic fixture requires clarification.',
            'evidence': [{'field': 'description', 'quote': 'Full original source evidence.'}],
            'strengths': [], 'gaps': [], 'unknowns': ['Synthetic gap'], 'borderline': True,
            'eligibility': 'unresolved', 'eligibility_condition': 'Synthetic condition', 'next_step': 'clarify', 'category': 'core'}
        resolutions = [{'ordinal': n, 'basis_sha256': 'b' * 64, 'choice': 'unresolved',
            'checked_dimensions': ['alignment'], 'explanation': 'Synthetic evidence does not settle the complete judgment.',
            'evidence': [{'field': 'description', 'quote': 'Full original source evidence.'}]} for n in (1, 2)]
        if args.adjudicator:
            assessment.update(explanation='Synthetic unresolved scope. ' * 60,
                evidence=[{'field': 'description', 'quote': ('Full original source evidence. ' * 40)[:1000]} for _ in range(12)],
                **{key: ['Synthetic uncertainty. ' * 20 for _ in range(10)] for key in ('strengths', 'gaps', 'unknowns')})
            first_response = output_stream([message, {'type': 'function_call', 'id': 'fc1', 'call_id': 'call1',
                'name': 'mcp__review__review_resolutions', 'arguments': json.dumps({'resolutions': resolutions})}])
            second_response = output_stream([{'type': 'function_call', 'id': 'fc2', 'call_id': 'call2',
                'name': 'mcp__review__review_resolutions', 'arguments': json.dumps({'resolutions': resolutions[1:]})}])
        elif not args.screening:
            first_response = output_stream([message, {'type': 'function_call', 'id': 'fc1', 'call_id': 'call1',
                             'name': 'mcp__review__review_assessments', 'arguments': json.dumps({'assessments': [
                                 {'ordinal': n, 'assessment': assessment} for n in (1, 2)]})}])
            second_response = output_stream([{'type': 'function_call', 'id': 'fc2', 'call_id': 'call2',
                'name': 'mcp__review__review_assessment', 'arguments': json.dumps({'ordinal': 2, 'assessment': assessment})}])
        if args.screening:
            first_response = output_stream([message, {'type': 'function_call', 'id': 'fc1', 'call_id': 'call1',
                'name': 'mcp__review__review_routes', 'arguments': json.dumps({'routes': [
                    {'ordinal': n, 'route': 'detailed'} for n in (1, 2)]})}])
            second_response = output_stream([{'type': 'function_call', 'id': 'fc2', 'call_id': 'call2',
                'name': 'mcp__review__review_routes', 'arguments': json.dumps({'routes': [{'ordinal': 2, 'route': 'detailed'}]})}])
        third_response = output_stream([dict(message, id='msg_final', phase='final_answer',
            content=[{'type': 'output_text', 'text': 'Synthetic completion.', 'annotations': [], 'logprobs': []}])])
        code = ('ADJUDICATOR=' + repr(args.adjudicator) + '\nASSESSMENT=' + repr(assessment) + '\nRESOLUTIONS=' + repr(resolutions) + '\nCODEX_MODEL=' + repr(config.model) + '\nCODEX_EFFORT=' + repr(config.reasoning_effort) + '\nSCREENING=' + repr(args.screening) + '\nHOST_SENTINEL=' + repr(str(sentinel)) + '\nFIRST_RESPONSE=' + repr(first_response) +
                '\nSECOND_RESPONSE=' + repr(second_response) + '\nTHIRD_RESPONSE=' + repr(third_response) + '\n' + PROBE)
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
        requests = result.pop('requests')
        projected = [validate_request(value, config.model, config.reasoning_effort, **scope) for value in requests]
        native_outputs = [item for item in requests[1]['input']
                          if item.get('type') == 'custom_tool_call_output']
        upstream_outputs = [item for item in projected[1]['input']
                            if item.get('type') == 'function_call_output']
        assert native_outputs and all(item.get('id', '').startswith('ctco_') for item in native_outputs), 'native output IDs were not exercised'
        assert [item['call_id'] for item in native_outputs] == [item['call_id'] for item in upstream_outputs], 'tool result linkage changed'
        assert all('id' not in item for item in upstream_outputs), 'native output ID leaked upstream'
        assert any(item.get('phase') == 'commentary' for item in requests[1]['input']), 'assistant phase was lost'
        result['native_output_identity'] = 'passed'
        result['native_gateway_protocol'] = 'passed'
        print(json.dumps(result))


if __name__ == '__main__':
    main()
