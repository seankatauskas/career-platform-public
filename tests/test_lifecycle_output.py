"""Lifecycle transport regression checks for bounded, resumable agent views."""
from __future__ import annotations

import json

from job_search.lifecycle.output import MAX_BYTES, bounded_lifecycle


def record(index):
    return {
        'observation_id':f'message-{index:04d}', 'direction':'inbound',
        'sender':'recruiter@example.test', 'subject':'Recruiting ' + ('🧪' * 500),
        'received_at':'2026-09-01T12:00:00Z', 'sent_at':None,
        'source_at':'2026-09-01T12:00:00Z', 'modified_at':'2026-09-01T12:01:00Z',
        'evidence_id':f'evidence-{index}', 'archive_id':f'archive-{index}',
        'action_id':None, 'created_at':'2026-09-01T12:00:00Z',
        'updated_at':'2026-09-01T12:00:00Z', 'confidence':1.0,
        'source':'reviewed', 'recipients':['recipient@example.test']*100,
        'account_id':'must-not-expose-account', 'immutable_message_id':'must-not-expose-transport',
    }


def assert_safe(result):
    serialized = json.dumps(result, ensure_ascii=False)
    assert len(serialized.encode('utf-8')) <= MAX_BYTES
    assert 'must-not-expose' not in serialized
    assert '"access_token"' not in serialized
    assert '"immutable_message_id"' not in serialized
    assert '"account_id"' not in serialized


def test_long_conversation_pages_retain_cursor_without_skipping_any_row():
    source = [record(i) for i in range(73)]
    position, found = 0, []
    while position < len(source):
        rows = source[position:position+25]
        raw = {'items':rows,'complete':position+len(rows)==len(source),
               'next_cursor':rows[-1]['observation_id'] if position+len(rows)<len(source) else None,
               'coverage':'Linked observations only.'}
        output = bounded_lifecycle('list_application_conversation',raw,{'limit':25})
        assert_safe(output)
        ids = [item['observation_id'] for item in output['items']]
        assert ids == [item['observation_id'] for item in source[position:position+len(ids)]]
        found.extend(ids)
        if output['complete']:
            assert output['next_cursor'] is None
            position = len(source)
        else:
            assert output['next_cursor'] == ids[-1]
            position = next(i+1 for i,item in enumerate(source) if item['observation_id']==output['next_cursor'])
    assert found == [row['observation_id'] for row in source]


def test_history_and_interview_offsets_resume_at_last_returned_record():
    for name,key in [('get_application_record_history','items'),('list_interview_rounds','rounds')]:
        position, seen = 0, []
        source = [{'revision_no':i+1,'revision_id':f'rev-{i}','round_id':f'round-{i}',
                   'state':{'status':'open','note':'Long content '*1000,'access_token':'must-not-expose-secret'}} for i in range(43)]
        while position < len(source):
            rows = source[position:position+25]
            remaining = position+len(rows)<len(source)
            raw = {key:rows,'complete':not remaining,
                   'next_revision':rows[-1]['revision_no'] if remaining else None,
                   'next_offset':position+len(rows) if remaining else None}
            output = bounded_lifecycle(name,raw,{'offset':position,'limit':25,'after_revision':position})
            assert_safe(output)
            seen.extend(item['revision_no'] for item in output[key])
            if output['complete']:
                assert seen == list(range(1,44))
                break
            position = output['next_revision'] if name=='get_application_record_history' else output['next_offset']
            assert position == len(seen)


def test_archive_cursor_is_preserved_with_every_match_and_utf8_size_bound():
    rows = [{'message_id':f'archive-{i}','subject':'🧪'*512,'excerpt':'界'*2048,'archive_truncated':False} for i in range(25)]
    opaque = 'opaque-ciphertext-scan-continuation'
    output = bounded_lifecycle('search_mail_history',{'items':rows,'next_cursor':opaque,'complete':False,'scanned':200,'scan_limit':200,'coverage':'Archived text only'}, {'limit':25})
    assert_safe(output)
    assert output['next_cursor']==opaque and len(output['items'])==25
    assert [item['message_id'] for item in output['items']] == [row['message_id'] for row in rows]
    empty = bounded_lifecycle('search_mail_history',{'items':[],'next_cursor':opaque,'complete':False,'scanned':200}, {'limit':25})
    assert empty['next_cursor']==opaque and not empty['complete']


def test_twenty_task_briefing_retains_coverage_obligations_and_all_sections():
    tasks = [{'task_id':f'task-{i}','kind':'reply','owner':'applicant','status':'open',
              'note':'Please send availability. '*100,'source_time':'2026-09-01T12:00:00Z',
              'evidence_id':f'evidence-{i}'} for i in range(20)]
    coverage = {'complete':False,'processing_counts':{'failed':1,'pending':8},
                'processing_cutoffs':[{'started_at':'2026-09-01T00:00:00Z'}],
                'note':'Unprocessed messages can still exist.'}
    source = {'application':{'application_id':'app-1','current_phase':'active'},
              'explanation':'You owe an availability reply.', 'coverage':coverage,
              'next_obligations':tasks,'tasks':tasks,'reminders':tasks,'details':tasks,
              'pending_reviews':tasks,'actions':tasks,'evidence':tasks,'attention':tasks,
              'legacy_interviews':tasks,'follow_up':{'after_days':7},
              'conversation':{'items':[record(i) for i in range(20)],'complete':False,'next_cursor':'message-0019'},
              'interviews':{'rounds':[{'round_id':f'round-{i}','status':'confirmed','details':{'join_url':'https://example.test'}} for i in range(20)],'next_offset':20},
              'truncated':False,'as_of':'2026-10-01T00:00:00Z'}
    output = bounded_lifecycle('get_application_briefing',source,{})
    assert_safe(output)
    assert output['coverage']['processing_counts']['failed']==1
    assert output['explanation']==source['explanation']
    assert output['next_obligations'][0]['task_id']=='task-0'
    assert output['truncated']
    assert set(source)-set(output) == set()
    assert output['conversation']['next_cursor']=='message-0002'
    assert output['interviews']['next_offset']==3


def test_task_and_detail_pages_report_actual_next_offset():
    for name,key in [('list_application_tasks','tasks'),('list_application_details','details'),('list_lifecycle_reviews','items'),('list_application_reminders','reminders')]:
        source = [{'task_id':str(i),'detail_id':str(i),'note':'Long '*1000} for i in range(25)]
        output = bounded_lifecycle(name,{key:source},{'offset':50,'limit':25})
        assert_safe(output)
        assert output['next_offset']==50+len(output[key]) and not output['complete']
        assert [row['task_id'] for row in output[key]]==[str(i) for i in range(len(output[key]))]


if __name__=='__main__':
    tests = [value for name,value in sorted(globals().items()) if name.startswith('test_')]
    for test in tests:
        test()
    print(f'ok ({len(tests)} lifecycle output tests)')
