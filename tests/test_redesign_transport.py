"""Real local HTTP trust boundary and shared candidate operation behavior."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from job_search.application_runtime import ApplicationRuntime
from job_search.application_candidate import make_candidate_server


class TransportTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime=ApplicationRuntime(Path(self.tmp.name)/"candidate.db")
        self.server=make_candidate_server(self.runtime)
        thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.origin="http://127.0.0.1:"+str(self.server.server_port)
        con=http.client.HTTPConnection("127.0.0.1",self.server.server_port)
        con.request("GET","/api/v1/session")
        response=con.getresponse()
        self.cookie=response.getheader("Set-Cookie").split(";",1)[0]
        self.csrf=json.loads(response.read())["csrf_token"]
        con.close()

    def post(self,operation,payload,*,authorized=True,key="command-one"):
        con=http.client.HTTPConnection("127.0.0.1",self.server.server_port)
        headers={"Content-Type":"application/json","Idempotency-Key":key}
        if authorized: headers.update({"Origin":self.origin,"Cookie":self.cookie,"X-CSRF-Token":self.csrf})
        con.request("POST","/api/v1/application-commands/"+operation,json.dumps(payload),headers)
        response=con.getresponse()
        result=response.status,json.loads(response.read())
        con.close()
        return result

    def test_csrf_and_actor_fields_cannot_grant_authority(self):
        payload={"job_source":{"source":"test","source_id":"one"}}
        self.assertEqual(self.post("save_job",payload,authorized=False)[0],403)
        self.assertEqual(self.post("save_job",{**payload,"actor_kind":"user"})[0],403)
        self.assertEqual(self.runtime.queries.list_applications()["items"],[])
        self.assertEqual(self.post("save_job",payload)[0],200)
        self.assertEqual(len(self.runtime.queries.list_applications()["items"]),1)
        self.assertEqual(self.post("save_job",payload)[0],200)
        self.assertEqual(len(self.runtime.queries.list_applications()["items"]),1)

    def test_paired_http_queue_and_late_answers_require_separate_review(self):
        from job_search.application_extension import PairedExtensionAdapter
        from job_search.application_transport import HumanApplicationAdapter
        from tests.test_browser_tracking import fixture,observation,URL
        from tests.test_job_search_autofill import EXTENSION_ORIGIN
        from job_search.contracts import utc_now
        import uuid
        authdir=Path(self.tmp.name)/"auth"
        authdir.mkdir()
        _,tracker,_,_,enrollment=fixture(authdir)
        adapter=PairedExtensionAdapter(self.runtime,tracker.authenticate,"local")
        server=make_candidate_server(self.runtime,extension=adapter)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        def send(path,body,method="POST"):
            con=http.client.HTTPConnection("127.0.0.1",server.server_port)
            con.request(method,"/api/v1/extension/"+path,json.dumps(body),
                {"Content-Type":"application/json","Origin":EXTENSION_ORIGIN})
            response=con.getresponse()
            result=response.status,json.loads(response.read())
            self.assertEqual(response.getheader("Access-Control-Allow-Origin"),EXTENSION_ORIGIN)
            con.close()
            return result
        first=observation(resume_sha256="a"*64)
        self.assertEqual(send("observations",first)[0],403)
        self.assertEqual(send("observations",{},"OPTIONS")[0],200)
        authorized={"device_token":enrollment["device_token"]}
        status,receipt=send("observations",{**first,**authorized})
        self.assertEqual(status,200)
        self.assertEqual(send("observations",{**first,**authorized})[1],receipt)
        self.assertEqual(send("observations",{**observation("site_acknowledged",first["attempt_id"]),**authorized})[0],200)
        snapshot={"version":1,"fields":[{"field_key":"why","prompt":"Why?","section":"","control":"textarea","value":"  e\u0301\n東京  "},
            {"field_key":"check","prompt":"Check?","section":"","control":"checkbox","value":False}],"omitted_fields":0,"truncated_values":0}
        capture={"capture_id":uuid.uuid4().hex,"attempt_id":first["attempt_id"],"page_url":URL,"captured_at":utc_now(),"snapshot":snapshot,**authorized}
        self.assertEqual(send("answers",capture)[0],200)
        pending=self.runtime.queries.review_queue()["items"]
        self.assertEqual(len(pending),1)
        self.assertEqual(pending[0]["operation"],"confirm_submission")
        human=HumanApplicationAdapter(self.runtime,"test-human")
        def accept(p,key):
            return human.command("review_changes",{"decisions":[{"proposal_id":p["id"],"expected_version":p["version"],"decision":"accept"}]},key)
        accept(pending[0],"confirm")
        view=self.runtime.queries.workspace(receipt["application_id"])
        submission=view["records"]["submissions"]["items"][0]
        self.assertEqual(submission["documents"][0]["sha256"],"a"*64)
        self.assertEqual(submission.get("answer_snapshots",[]),[])
        attach=view["review"]["items"][0]
        self.assertEqual(attach["operation"],"attach_submission_answers")
        accept(attach,"attach")
        view=self.runtime.queries.workspace(receipt["application_id"])
        self.assertEqual(len(view["records"]["submissions"]["items"]),1)
        self.assertEqual(view["records"]["submissions"]["items"][0]["answer_snapshots"][0]["snapshot"],snapshot)
        self.assertEqual(view["review"]["items"],[])

    def test_invalid_host_and_unknown_legacy_writer_do_not_run(self):
        con=http.client.HTTPConnection("127.0.0.1",self.server.server_port)
        con.request("GET","/api/v1/applications",headers={"Host":"evil.example"})
        response=con.getresponse()
        self.assertEqual(response.status,400)
        response.read();con.close()
        self.assertEqual(self.post("auto_apply_event",{})[0],400)


if __name__=="__main__":
    unittest.main()
