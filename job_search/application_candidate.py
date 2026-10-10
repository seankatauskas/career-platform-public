"""Local-only candidate host. Uses the same owners and never starts live workers."""
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
from pathlib import Path
import secrets
from urllib.parse import urlsplit,parse_qs

from .commands import DomainError
from .dashboard import SessionManager,SESSION_COOKIE
from .application_transport import HumanApplicationAdapter,AgentApplicationAdapter
from .application_compatibility import translate_dashboard


WEB=Path(__file__).parent/"web"


def make_candidate_mcp_server(runtime,bearer_token,*,port=0):
    """Reuse the authenticated MCP host with the candidate's bounded tool registry."""
    from .application_agent_tools import ApplicationAgentTools
    from .hermes_mcp import make_mcp_server
    return make_mcp_server(ApplicationAgentTools(runtime),bearer_token,port=port,
        bind_host="127.0.0.1",allowed_hosts=("127.0.0.1","localhost"))


def make_candidate_server(runtime,*,host="127.0.0.1",port=0,extension=None):
    if host not in {"127.0.0.1","localhost"}:
        raise ValueError("The isolated candidate binds only to loopback")
    sessions=SessionManager()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass

        def valid_host(self):
            return self.headers.get("Host") in {"127.0.0.1:"+str(self.server.server_port),"localhost:"+str(self.server.server_port)}

        def valid_origin(self):
            return self.headers.get("Origin")=="http://"+self.headers.get("Host","")

        def send_content(self,body,content_type="application/json",status=200):
            if not isinstance(body,bytes):
                body=json.dumps(body,ensure_ascii=False,allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type",content_type+"; charset=utf-8")
            self.send_header("Content-Length",str(len(body)))
            self.send_header("Cache-Control","no-store")
            self.send_header("X-Content-Type-Options","nosniff")
            self.send_header("Content-Security-Policy","default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'")
            if getattr(self,"extension_origin",None):
                self.send_header("Access-Control-Allow-Origin",self.extension_origin)
                self.send_header("Vary","Origin")
                self.send_header("Access-Control-Allow-Methods","POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers","Content-Type")
            if getattr(self,"new_session",False):
                self.send_header("Set-Cookie",f"{SESSION_COOKIE}={self.session.session_id}; Path=/; HttpOnly; SameSite=Strict")
            self.end_headers()
            self.wfile.write(body)

        def prepare(self):
            if not self.valid_host():
                self.send_content({"error":"invalid_host"},status=400)
                return False
            self.session,self.new_session=sessions.resolve(self.headers.get("Cookie",""),"candidate")
            return True

        def fail(self,exc):
            if isinstance(exc,DomainError):
                status=403 if exc.code=="not_authorized" else 409 if exc.code in {"version_conflict","idempotency_conflict","dependency_unresolved","needs_reconciliation"} else 400
                self.send_content({"error":exc.code,"message":str(exc)},status=status)
            elif isinstance(exc,(ValueError,TypeError,KeyError)):
                self.send_content({"error":"invalid_input","message":"Check the requested operation and required fields."},status=400)
            else:
                self.send_content({"error":"internal_error","message":"The operation could not be completed."},status=500)

        def do_GET(self):
            if not self.prepare(): return
            if "Origin" in self.headers and not self.valid_origin():
                self.send_content({"error":"invalid_origin"},status=403);return
            url=urlsplit(self.path)
            query={k:v[-1] for k,v in parse_qs(url.query).items()}
            try:
                static={"/":("application-candidate.html","text/html"),
                    "/candidate.js":("application-candidate.js","text/javascript"),
                    "/candidate.css":("application-candidate.css","text/css")}
                if url.path in static:
                    filename,content_type=static[url.path]
                    self.send_content((WEB/filename).read_bytes(),content_type);return
                if url.path=="/api/v1/session":
                    self.send_content({"csrf_token":self.session.csrf_token,"paused":True});return
                if url.path=="/api/v1/applications":
                    result=runtime.queries.list_applications(limit=int(query.get("limit",50)),after=query.get("after"))
                elif url.path=="/api/v1/workspace":
                    result=runtime.queries.workspace(query["application_id"])
                elif url.path=="/api/v1/workspace-page":
                    result=runtime.queries.workspace_page(query["application_id"],query["group"],limit=int(query.get("limit",25)),cursor=query.get("cursor"))
                elif url.path=="/api/v1/review":
                    result=runtime.queries.review_queue(application_id=query.get("application_id"))
                elif url.path=="/api/v1/briefing":
                    result=runtime.queries.briefing()
                elif url.path=="/api/v1/correction-preview":
                    with runtime.executor.read() as con:
                        result=runtime.workflows.preview_association_correction(con,query["association_id"])
                elif url.path=="/api/v1/closure-preview":
                    with runtime.executor.read() as con:
                        result=runtime.applications.preview_closure(con,query["application_id"])
                else:
                    self.send_content({"error":"not_found"},status=404);return
                self.send_content(result)
            except Exception as exc:
                self.fail(exc)

        def extension_request(self,preflight=False):
            if not self.valid_host():
                self.send_content({"error":"invalid_host"},status=400);return
            try:
                from .autofill import validate_extension_origin
                from .application_answers import MAX_SNAPSHOT_BYTES
                self.extension_origin=validate_extension_origin(self.headers.get("Origin",""))
                if preflight:
                    self.send_content({"ready":True});return
                length=int(self.headers.get("Content-Length","0"))
                if not 0<length<=MAX_SNAPSHOT_BYTES+16384 or self.headers.get("Content-Type","").split(";")[0]!="application/json":
                    raise DomainError("invalid_input","Invalid extension request size or content type")
                def reject_constant(_): raise ValueError("Non-finite number")
                body=json.loads(self.rfile.read(length),parse_constant=reject_constant)
                result=extension.handle(urlsplit(self.path).path,self.headers,body)
                self.send_content(result)
            except Exception as exc:
                self.fail(exc)

        def do_OPTIONS(self):
            if extension is not None and urlsplit(self.path).path in extension.paths:
                self.extension_request(preflight=True)
            else:
                self.send_content({"error":"not_found"},status=404)

        def do_POST(self):
            if extension is not None and urlsplit(self.path).path in extension.paths:
                self.extension_request();return
            if not self.prepare(): return
            if not self.valid_origin() or not secrets.compare_digest(self.headers.get("X-CSRF-Token",""),self.session.csrf_token):
                self.send_content({"error":"not_authorized"},status=403);return
            try:
                length=int(self.headers.get("Content-Length","0"))
                if not 0<length<=65536 or self.headers.get("Content-Type","").split(";")[0]!="application/json":
                    raise DomainError("invalid_input","Send a JSON object of at most 64 KiB")
                def reject_constant(_):
                    raise ValueError("Non-finite number")
                body=json.loads(self.rfile.read(length),parse_constant=reject_constant)
                if not isinstance(body,dict): raise ValueError("Expected an object")
                key=self.headers.get("Idempotency-Key") or body.pop("idempotency_key",None)
                path=urlsplit(self.path).path
                if path.startswith("/api/v1/lifecycle/"):
                    translated=translate_dashboard(path,body)
                    operation,payload=translated.operation,dict(translated.input)
                    key=key or translated.idempotency_key
                    if translated.kind=="proposal":
                        payload={"operation":operation,"input":payload,"application_id":payload.get("application_id")}
                        operation="propose_changes"
                elif path.startswith("/api/v1/application-commands/"):
                    operation=path.rsplit("/",1)[-1]
                    payload=body
                else:
                    self.send_content({"error":"not_found"},status=404);return
                result=HumanApplicationAdapter(runtime,"local-owner").command(operation,payload,key)
                self.send_content(result)
            except Exception as exc:
                self.fail(exc)
    return ThreadingHTTPServer((host,port),Handler)


def add_arguments(parser):
    parser.add_argument("action",choices=("serve","list","workspace","propose","convert"))
    parser.add_argument("--database",type=Path)
    parser.add_argument("--application-id")
    parser.add_argument("--port",type=int,default=8766)
    parser.add_argument("--source",type=Path)
    parser.add_argument("--destination",type=Path)
    parser.add_argument("--idempotency-key")


def command(args):
    from .application_runtime import ApplicationRuntime
    if args.action=="convert":
        from .application_migration import convert_snapshot
        if args.source is None or args.destination is None:
            raise ValueError("Conversion requires --source and --destination")
        return convert_snapshot(args.source,args.destination)
    if args.database is None:
        raise ValueError("An explicit isolated --database is required")
    runtime=ApplicationRuntime(args.database)
    if args.action=="serve":
        server=make_candidate_server(runtime,port=args.port)
        print("Paused application candidate: http://127.0.0.1:"+str(server.server_port),flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return {"status":"stopped","paused":True}
    adapter=AgentApplicationAdapter(runtime,"candidate-cli")
    if args.action=="list":
        return adapter.call("list_applications")
    if args.action=="workspace":
        return adapter.call("get_application_workspace",{"application_id":args.application_id})
    import sys
    raw=sys.stdin.buffer.read(65537)
    if len(raw)>65536:
        raise ValueError("Proposal input exceeds 64 KiB")
    return adapter.call("propose_changes",json.loads(raw),idempotency_key=args.idempotency_key)
