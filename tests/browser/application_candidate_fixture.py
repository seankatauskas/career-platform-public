"""Fictional replacement workspace for browser acceptance; no provider clients."""
import argparse
import json
from pathlib import Path

from job_search.application_runtime import ApplicationRuntime
from job_search.application_transport import HumanApplicationAdapter,AgentApplicationAdapter
from job_search.application_candidate import make_candidate_server


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--database",type=Path,required=True)
    args=parser.parse_args()
    runtime=ApplicationRuntime(args.database)
    human=HumanApplicationAdapter(runtime,"fixture-owner")
    app=human.command("save_job",{"job_source":{"source":"fixture","source_id":"example-role","employer":"Example Labs","title":"Platform Engineer"}},"seed")
    AgentApplicationAdapter(runtime).call("propose_changes",{"operation":"create_task","application_id":app["id"],
        "input":{"application_id":app["id"],"kind":"complete_assessment","description":"Complete the design exercise"}},idempotency_key="proposal")
    server=make_candidate_server(runtime)
    print(json.dumps({"url":"http://127.0.0.1:"+str(server.server_port),"application_id":app["id"]}),flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__=="__main__":
    main()
