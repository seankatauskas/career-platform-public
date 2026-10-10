"""Submission feedback crosses databases with an exact replay identity."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
from job_search.application_runtime import ApplicationRuntime
from job_search.application_feedback import build_feedback_handlers
from job_search.commands import CommandContext,Principal

class FeedbackTest(unittest.TestCase):
    def test_crash_after_preference_commit_replays_same_receipt_then_acknowledges(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime=ApplicationRuntime(Path(directory)/'owners.db')
            def human(op,data,key):
                return runtime.command(CommandContext(Principal('human','human',{'*'}),key),op,data)
            app=human('save_job',{'job_source':{'source':'greenhouse','source_id':'posting'}},'save')
            submission=human('record_submission',{'application_id':app['id'],'occurred_at':'2026-10-09T12:00:00Z'},'submit')
            class Destination:
                def __init__(self):self.receipts=set();self.calls=[]
                def deliver_applied_feedback(self,payload,*,source_event_id):
                    self.calls.append((payload,source_event_id));self.receipts.add(source_event_id)
                    return {'created':len(self.calls)==1}
            destination=Destination();handlers=build_feedback_handlers(runtime,destination)
            work=handlers['application.feedback.dispatch']({},SimpleNamespace()).follow_ups[0]
            with patch.object(runtime.executor,'complete_work',side_effect=RuntimeError('crash after destination commit')):
                with self.assertRaises(RuntimeError):handlers[work.task_kind](work.payload,SimpleNamespace())
            handlers[work.task_kind](work.payload,SimpleNamespace())
            self.assertEqual(len(destination.calls),2)
            self.assertEqual(destination.receipts,{'owner-submission:'+submission['id']})
            self.assertEqual(destination.calls[0][0]['job_id'],'posting')
            self.assertFalse(handlers['application.feedback.dispatch']({},SimpleNamespace()).follow_ups)

if __name__=='__main__':unittest.main()
