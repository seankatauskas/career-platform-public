"""Atomic primary-calendar coverage; incomplete refreshes preserve the last snapshot."""
import json
from datetime import timedelta
from ..contracts import ContractError, canonical_json, parse_utc, utc_now
from ..db import connect


class AgendaMixin:
    def refresh_agenda(self, starts_at, ends_at, context):
        context.validate()
        if context.actor_kind!='system':raise ContractError('agenda refresh requires trusted runtime')
        if not self.outlook or not self.account_id:raise ContractError('Outlook agenda unavailable')
        if parse_utc(ends_at)<=parse_utc(starts_at) or parse_utc(ends_at)-parse_utc(starts_at)>timedelta(days=14,hours=1):
            raise ContractError('agenda window must be at most 14 local days')
        stamp=self._now()
        try:
            items=list(self.outlook.read_agenda(starts_at,ends_at))
            if len(items)>10000 or len({v['id'] for v in items})!=len(items):raise ContractError('invalid complete agenda')
            # Defense in depth: private text never enters storage even with another adapter.
            safe=[]
            fields={'id','source_ref','starts_at','ends_at','title','status','show_as','is_all_day','private','type','series_master_id','original_start_time_zone','original_end_time_zone'}
            for item in items:
                parse_utc(item['starts_at']);parse_utc(item['ends_at'])
                entry={k:v for k,v in item.items() if k in fields}
                if entry.get('private'):entry['title']='Private commitment'
                safe.append(entry)
            with connect(self.store.db_path) as con:
                con.execute('INSERT INTO career_agenda_snapshots VALUES (?,?,?,?,?,?,\'\') ON CONFLICT(account_id) DO UPDATE SET window_start=excluded.window_start,window_end=excluded.window_end,items_json=excluded.items_json,checked_at=excluded.checked_at,last_attempt_at=excluded.last_attempt_at,error_code=\'\'',(self.account_id,starts_at,ends_at,canonical_json(safe),stamp,stamp))
        except Exception as exc:
            with connect(self.store.db_path) as con:
                con.execute('UPDATE career_agenda_snapshots SET last_attempt_at=?,error_code=? WHERE account_id=?',(stamp,type(exc).__name__,self.account_id))
            raise
        return self.agenda(starts_at,ends_at)

    def agenda(self, starts_at, ends_at):
        parse_utc(starts_at);parse_utc(ends_at)
        with connect(self.store.db_path) as con:
            saved=con.execute('SELECT * FROM career_agenda_snapshots WHERE account_id=?',(self.account_id,)).fetchone()
            linked={r[0]:r[1] for r in con.execute('SELECT calendar_event_id,round_id FROM interview_rounds WHERE calendar_account_id=? AND calendar_event_id<>\'\'',(self.account_id,))}
            owned={r[0]:r[1] for r in con.execute("SELECT remote_id,commitment_id FROM career_commitments WHERE remote_id<>''")}
        if not saved:return {'items':[],'coverage':{'complete':False,'reason':'not_checked','checked_at':None,'calendar':'primary'}}
        fresh=parse_utc(self._now())-parse_utc(saved['checked_at'])<=timedelta(minutes=10)
        contained=starts_at>=saved['window_start'] and ends_at<=saved['window_end']
        items=[]
        for item in json.loads(saved['items_json']):
            if item['ends_at']<=starts_at or item['starts_at']>=ends_at or item.get('status') in {'cancelled','declined','draft'}:continue
            item['round_id']=linked.get(item['id'])
            item['commitment_id']=owned.get(item['id'])
            items.append(item)
        items.sort(key=lambda x:(x['starts_at'],x['id']))
        reason='refresh_failed' if saved['error_code'] else 'stale' if not fresh else 'outside_snapshot' if not contained else ''
        return {'items':items,'coverage':{'complete':not reason,'reason':reason,'checked_at':saved['checked_at'],'last_attempt_at':saved['last_attempt_at'],'window_start':saved['window_start'],'window_end':saved['window_end'],'calendar':'primary'}}
