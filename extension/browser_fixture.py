#!/usr/bin/env python3
"""Loopback-only ATS/Tailscale fixtures for the real extension browser check.

The TLS proxy supplies synthetic identity; it is not a Tailscale implementation.
No real accounts, inboxes, application forms, or provider endpoints are used.
"""
from __future__ import annotations

import http.client
import hashlib
import json
import ssl
import sys
import tempfile
import threading
from urllib.parse import urlsplit, parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from job_search.autofill import AutofillBroker, HANDOFF_TTL_SECONDS, SUBMISSION_TTL_SECONDS
from job_search.dashboard import DashboardController, make_server
from job_search.contracts import JobSnapshot, MutationContext, RecommendationProvenance, EventInput, ApplicationEventType, utc_now
from tests.test_job_search_autofill import make_ledger, profile
from tests.test_job_search_dashboard import FakePreferences
from job_search.resume_integration import ResumeArtifactContent

class FixtureResumes:
    def get_autofill_resume(self, application_id=None):
        pdf=b'%PDF-1.7\nSynthetic extension upload fixture\n%%EOF'
        return ResumeArtifactContent('fixture','resume.pdf','application/pdf',pdf,hashlib.sha256(pdf).hexdigest())

    def get_selection(self, application_id):
        return {'selection': None}

ORIGIN = "https://career.fixture-tailnet.ts.net"
OWNER = "owner@example.test"
HOSTS = {"greenhouse": "job-boards.greenhouse.io", "ashby": "jobs.ashbyhq.com", "lever": "jobs.lever.co"}
FORM = b"""<!doctype html><html><head><title>Synthetic ATS application</title>
<style>body{font:18px system-ui;max-width:650px;margin:50px auto}label{display:block;margin:20px 0}input,textarea{display:block;padding:12px;width:95%}button{padding:12px}</style>
</head><body><h1>Platform Engineer</h1><p>Local fixture: no application is sent.</p>
<form><label>First name<input id="first" autocomplete="given-name"></label>
<label>Email<input id="email" type="email"></label>
<label>Why are you interested in this role?<textarea id="why"></textarea></label>
<label>Desired salary<input id="salary"></label>
<label>Upload resume<input id="resume" type="file"></label>
<button type="submit" id="submit">Submit application</button></form>
<p id="submitted">Not submitted</p><script>window.submitCount=0;
document.querySelector('form').addEventListener('submit',e=>{e.preventDefault();window.submitCount++;document.querySelector('#submitted').textContent='Submitted by test user';});</script></body></html>"""


TRACKING_FORM = b"""<!doctype html><html><head><title>Engineer</title></head><body>
<h1>Engineer</h1><form><label>First name<input autocomplete="given-name" id="first"></label>
<label>Email<input id="email" type="email" required></label>
<label>Why this company?<textarea id="why" name="why"></textarea></label>
<label>Describe your company infrastructure experience<textarea id="experience"></textarea></label>
<span id="rich-label">Tell us about a project</span><div id="rich" role="textbox" contenteditable="true" aria-labelledby="rich-label" style="min-height:2em"></div>
<label>Desired salary<input id="salary" type="number"></label>
<label>Location<select id="location"><option value="">Choose</option><option value="remote">Remote</option></select></label>
<fieldset><legend>Work authorization</legend><label>Yes<input id="authorized" name="authorization" type="radio" value="yes"></label><label>No<input name="authorization" type="radio" value="no"></label></fieldset>
<label>I agree to the terms<input id="terms" type="checkbox"></label>
<label>Password<input id="password" type="password"></label><input type="hidden" id="token" value="SECRET-TOKEN">
<label>Resume<input id="resume" name="resume" type="file"></label>
<button type="submit" id="submit">Submit application</button></form><h2 role="status" id="outcome"></h2>
<script>window.submitCount=0;
document.querySelector('form').addEventListener('submit', async e=>{
 e.preventDefault(); window.submitCount++;
 const mode=new URL(location.href).searchParams.get('outcome');
 const parts=location.pathname.split('/').filter(Boolean);
 let endpoint=location.hostname.includes('lever') ? location.pathname+'/apply' : location.hostname.includes('ashby') ? '/api/non-user-graphql?op=ApiSubmitSingleApplicationFormAction' : '/applications';
 if(mode==='embed_redirect') endpoint='/embed/acme/jobs/'+new URL(location.href).searchParams.get('token');
 const response=await fetch(endpoint+(endpoint.includes('?')?'&':'?')+'fixture_status='+(mode==='failed'?'500':'200'),{method:'POST',body:'{}',headers:{'Content-Type':'application/json'}});
 if(mode==='uncertain') return;
 if(mode==='redirect' && response.ok) {location.assign(location.pathname+'/confirmation'); return;}
 if(mode==='embed_redirect' && response.ok) {
   const params=new URLSearchParams(location.search);
   location.assign('/embed/job_app/confirmation?for='+params.get('for')+'&token='+params.get('token'));
   return;
 }
 if(location.hostname.includes('ashby') && response.ok) {
   document.querySelector('#outcome').outerHTML='<div class="ashby-application-form-success-container"><div role="status" aria-live="polite"><h2>Success</h2><p>Thank you for applying to Acme! We will review your application.</p></div></div>';
   return;
 }
 document.querySelector('#outcome').textContent=response.ok ? 'Your application has been submitted.' : 'Unable to submit application.';
});</script></body></html>"""


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="career-extension-fixture-") as directory:
        ledger = make_ledger(directory)
        now = [1000.0]
        broker = AutofillBroker(ledger, profile(), clock=lambda: now[0])
        controller = DashboardController(ledger, FakePreferences(), autofill=broker)
        controller.browser_tracking.resume_lab = FixtureResumes()
        backend = make_server(controller, 0, https_origin=ORIGIN, allowed_tailscale_login=OWNER)
        state = {"owner": OWNER, "redirect": False, "requests": [], "offline": False}

        class Proxy(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def handle_request(self):
                host = self.headers.get("Host", "").split(":", 1)[0]
                if host == "employer.fixture.test" and self.command == "GET" and self.path.startswith("/embed?"):
                    target = parse_qs(urlsplit(self.path).query)["url"][0]
                    import html
                    output = ('<!doctype html><h1>Company careers</h1><iframe style="width:900px;height:700px" src="'+html.escape(target, quote=True)+'"></iframe>').encode()
                    self.send_response(200); self.send_header("Content-Type", "text/html"); self.send_header("Content-Length", str(len(output))); self.end_headers(); self.wfile.write(output)
                    return
                if host in HOSTS.values() and self.command == "POST":
                    status = int(parse_qs(urlsplit(self.path).query).get("fixture_status", ["200"])[0])
                    self.rfile.read(int(self.headers.get("Content-Length", "0")))
                    self.send_response(status); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"{}")
                    return
                if host in HOSTS.values() and self.command == "GET":
                    form = TRACKING_FORM if "fixture=tracking" in self.path else FORM
                    if urlsplit(self.path).path.endswith('/confirmation'):
                        form = b'<!doctype html><h1>Thank you for applying.</h1><p>Your application has been received.</p>'
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(form)))
                    self.end_headers()
                    self.wfile.write(form)
                    return
                if host != "career.fixture-tailnet.ts.net":
                    self.send_error(404)
                    return
                if state["offline"]:
                    self.send_error(503)
                    return
                if state["redirect"]:
                    self.send_response(302)
                    self.send_header("Location", "https://different.fixture-tailnet.ts.net/")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                size = int(self.headers.get("Content-Length", "0"))
                if size > (2 * 1024 * 1024 + 16384 if self.path == '/api/v1/extension/answers' else 65536):
                    self.send_error(413)
                    return
                body = self.rfile.read(size) if size else None
                headers = {key: value for key, value in self.headers.items()
                           if not key.lower().startswith(("tailscale-", "x-forwarded-"))
                           and key.lower() not in {"forwarded", "connection", "host"}}
                headers.update({"Host": "career.fixture-tailnet.ts.net", "Tailscale-User-Login": state["owner"], "X-Forwarded-Proto": "https"})
                state["requests"].append({"method": self.command, "path": self.path, "origin": headers.get("Origin"), "cookie": bool(headers.get("Cookie"))})
                connection = http.client.HTTPConnection("127.0.0.1", backend.server_port, timeout=5)
                try:
                    connection.request(self.command, self.path, body, headers)
                    response = connection.getresponse()
                    output = response.read()
                    self.send_response(response.status)
                    for key, value in response.getheaders():
                        if key.lower() not in {"server", "date", "connection"}:
                            self.send_header(key, value)
                    self.end_headers()
                    self.wfile.write(output)
                finally:
                    connection.close()

            do_GET = do_POST = do_OPTIONS = handle_request

        proxy = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(sys.argv[1], sys.argv[2])
        proxy.socket = tls.wrap_socket(proxy.socket, server_side=True)
        threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (backend, proxy)]
        for thread in threads:
            thread.start()
        print(json.dumps({"dashboard": f"http://127.0.0.1:{backend.server_port}", "cloud": ORIGIN, "tls_port": proxy.server_port}), flush=True)
        count = 0
        try:
            for line in sys.stdin:
                command = json.loads(line)
                action = command["action"]
                if action == "issue":
                    count += 1
                    ats = command.get("ats", "greenhouse")
                    application_id = ledger.start_application(
                        JobSnapshot(ats, "job-1", "family-" + ats, "Platform Engineer", "Acme", "acme",
                                    f"https://{HOSTS[ats]}/acme/job-1"),
                        RecommendationProvenance(), MutationContext("start-" + ats, "user", "fixture"),
                    )["application"]["application_id"]
                    handoff = broker.issue(application_id, "fixture-session", f"handoff-{count}")
                    result = {**handoff, "ats": ats, "page_url": f"https://{HOSTS[ats]}/acme/job-1"}
                elif action == "autopair":
                    result = controller.browser_tracking.issue_pairing(ORIGIN+"|"+OWNER if command.get("cloud") else "local")
                elif action == "applications":
                    result = {"applications": ledger.list_applications(), "requests": state["requests"]}
                elif action == "offline":
                    state["offline"] = command["value"]
                    result = {"changed": True}
                elif action == "mailconfirm":
                    app = command["application_id"]
                    result = ledger.record_event(EventInput(app, ApplicationEventType.SUBMISSION_CONFIRMED, utc_now(), {}, "email:"+app, MutationContext("email:"+app, "system", "outlook")))
                elif action == "expire":
                    now[0] += SUBMISSION_TTL_SECONDS + HANDOFF_TTL_SECONDS + 1
                    result = {"expired": True}
                elif action == "owner":
                    state["owner"] = command["value"]
                    result = {"changed": True}
                elif action == "redirect":
                    state["redirect"] = command["value"]
                    result = {"changed": True}
                elif action == "state":
                    result = {"requests": state["requests"], "timeline": ledger.get_application_timeline(command["application_id"]),
                              "answers": controller.application_workspace(command["application_id"])["answer_snapshots"]}
                elif action == "stop":
                    break
                else:
                    raise ValueError("unknown fixture command")
                print(json.dumps(result), flush=True)
        finally:
            for server in (proxy, backend):
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(timeout=2)


if __name__ == "__main__":
    main()
