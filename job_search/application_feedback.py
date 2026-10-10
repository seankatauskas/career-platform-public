"""Durable bridge from accepted submissions to the existing preference owner."""
from .worker import FollowUpTask,TaskResult,PermanentTaskError


def build_feedback_handlers(runtime,preferences):
    def dispatch(payload,context):
        with runtime.executor.read() as con:
            page=runtime.executor.work_page(con,owner='applications',kind='submission_feedback',after=payload.get('after',''))
        followups=[FollowUpTask('application.submission_feedback',{'work_id':item['id'],'key':item['dedupe_key']},
            dedupe_key=item['id']) for item in page['items']]
        if page['next_cursor']:
            followups.append(FollowUpTask('application.feedback.dispatch',{'after':page['next_cursor']}))
        return TaskResult({'scheduled':len(followups)},tuple(followups))
    def apply(payload,context):
        import json
        with runtime.executor.read() as con:
            work=runtime.executor.find_work(con,'applications','submission_feedback',payload['key'])
        if work is None or work['id']!=payload['work_id']:
            raise PermanentTaskError('Submission feedback identity changed')
        if work['status']=='done':return {'status':'already_applied'}
        value=json.loads(work['payload'])
        feedback=value.get('feedback')
        if not feedback or not feedback.get('ats') or not feedback.get('job_id'):
            raise PermanentTaskError('Submission feedback requires its captured posting context')
        if feedback['ats'] not in {'greenhouse','ashby','lever'}:
            result={'created':False,'reason':'no_supported_catalog_posting'}
        else:
            result=preferences.deliver_applied_feedback(feedback,source_event_id='owner-submission:'+value['submission_id'])
        # The destination receipt key makes a crash between databases replay-safe.
        runtime.executor.complete_work(work['id'],owner='applications',kind='submission_feedback')
        return {'status':'applied','result':result}
    return {'application.feedback.dispatch':dispatch,'application.submission_feedback':apply}
