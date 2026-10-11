"""Scheduled owner-backed briefs using the existing operational outbox/history.

Understanding owns fact selection. This adapter owns only slots, durable handoff,
expiry and delivery revalidation; it cannot write application or inference state.
"""
import json
from datetime import timedelta
from zoneinfo import ZoneInfo

from ..commands import digest
from ..contracts import MutationContext, canonical_json
from ..db import connect
from ..applications.understanding.briefing import portfolio
from .service import AttentionService, _active, _briefing, _now, _preferences
from .policy import slot_time, text_time
from .portfolio import render


class OwnerBriefingService(AttentionService):
    def __init__(self, ledger, queries, **kwargs):
        super().__init__(ledger, **kwargs)
        self.queries = queries

    def preview(self, slot='morning', local_date=None):
        now = _now(self.clock())
        prefs = self.preferences()
        local_date = local_date or now.astimezone(ZoneInfo(prefs['timezone'])).date().isoformat()
        with connect(self.store.db_path) as con:
            prior = con.execute("SELECT b.snapshot_json FROM attention_briefings b JOIN notification_outbox n USING(notification_id) WHERE n.status='delivered' ORDER BY b.scheduled_for DESC LIMIT 1").fetchone()
        since = json.loads(prior[0])['as_of'] if prior else text_time(now - timedelta(days=1))
        day = _now(slot_time(slot, local_date, prefs))
        if slot == 'evening' and day.astimezone(ZoneInfo(prefs['timezone'])).weekday() == 4:
            local = day.astimezone(ZoneInfo(prefs['timezone']))
            since = text_time((local - timedelta(days=4)).replace(hour=0, minute=0, second=0))
        since = max(since, prefs.get('activated_at') or since)
        snapshot = portfolio(self.queries.briefing_records(), now=now, slot=slot, local_date=local_date,
                             timezone_name=prefs['timezone'], since=since)
        title, body, refs = render(snapshot, dashboard_url=self.dashboard_url + '/#applications' if self.dashboard_url else None)
        return dict(slot=slot, local_date=local_date, scheduled_for=text_time(slot_time(slot, local_date, prefs)),
                    snapshot=snapshot, title=title, body=body, selected_refs=refs)

    @staticmethod
    def _revision(snapshot):
        return digest({key: snapshot[key] for key in ('owner_identity', 'owner_activation', 'facts', 'counts', 'coverage', 'application_activity')}
                      | {'application_activity': {key: value for key, value in snapshot['application_activity'].items() if key != 'through'}})

    def finalize_briefing(self, briefing_id, context=None):
        briefing = self.get_briefing(briefing_id)
        now = _now(self.clock()); stamp = text_time(now)
        if briefing['status'] != 'prepared' or now < _now(briefing['scheduled_for']):
            return {'created': False, 'briefing': briefing}
        preview = self.preview(briefing['slot'], briefing['local_date'])
        context = context or MutationContext('owner-brief-finalize:' + briefing_id + ':' + stamp, 'system', 'owner_briefing')
        def operation(con, _stamp):
            saved = _briefing(con.execute('SELECT * FROM attention_briefings WHERE briefing_id=?', (briefing_id,)).fetchone())
            if saved['status'] != 'prepared': return {'created': False, 'briefing': saved}
            if now >= _now(saved['expires_at']):
                con.execute("UPDATE attention_briefings SET status='expired',finalized_at=? WHERE briefing_id=?", (stamp, briefing_id))
                return {'created': False, 'briefing': self._saved(con, briefing_id)}
            prefs = _preferences(con); snapshot = preview['snapshot']
            unchanged = saved['snapshot'].get('backend') == 'owners' and self._revision(saved['snapshot']) == self._revision(snapshot)
            generation = saved['generation'] if unchanged else None
            title, body, refs = render(snapshot, generation.get('ordered_refs') if generation else None, generation,
                                      self.dashboard_url + '/#applications' if self.dashboard_url else None)
            nid = None
            if (prefs['enabled'] and not prefs['shadow'] and _active(con)
                    and prefs['revision'] == saved['preference_revision'] and not snapshot['owner_activation']['paused'] and not snapshot['owner_activation'].get('restore_required')):
                nid = self._enqueue(con, None, briefing_id, None, title, body, saved['expires_at'], prefs['revision'], stamp,
                                    {'briefing_id': briefing_id, 'slot': saved['slot'], 'local_date': saved['local_date'], 'backend': 'owners'})
            con.execute("UPDATE attention_briefings SET status='finalized',title=?,body=?,snapshot_json=?,selected_refs_json=?,notification_id=?,finalized_at=?,generation_json=?,renderer=? WHERE briefing_id=?",
                        (title, body, canonical_json(snapshot), canonical_json(refs), nid, stamp,
                         canonical_json(generation) if generation else None, 'grounded_model_selection' if generation else 'deterministic', briefing_id))
            return {'created': bool(nid), 'briefing': self._saved(con, briefing_id)}
        return self._command('owner_finalize', {'briefing_id': briefing_id, 'now': stamp}, context, operation)

    @staticmethod
    def _saved(con, briefing_id):
        return _briefing(con.execute('SELECT * FROM attention_briefings WHERE briefing_id=?', (briefing_id,)).fetchone())

    def process_tick(self, context=None):
        now = _now(self.clock())
        for slot in self.due_briefings(now):
            with connect(self.store.db_path) as con:
                exists = con.execute('SELECT 1 FROM attention_briefings WHERE slot=? AND local_date=? AND timezone=?',
                    (slot['slot'], slot['local_date'], self.preferences()['timezone'])).fetchone()
            if exists is None:
                self.prepare_briefing(**slot)
        with connect(self.store.db_path) as con:
            pending = [r[0] for r in con.execute("SELECT briefing_id FROM attention_briefings WHERE status='prepared' AND json_extract(snapshot_json,'$.backend')='owners'")]
        finished, generation = [], []
        for bid in pending:
            briefing = self.get_briefing(bid)
            if self.preferences()['ai_enabled'] and not briefing['generation'] and now < _now(briefing['generation_deadline']):
                generation.append(bid)
            finished.append(self.finalize_briefing(bid)['briefing'])
        return {'briefings': finished, 'generation_pending': generation}

    def validate_delivery(self, con, notification, now):
        """Refresh changed facts only before dispatch; never replay an uncertain send."""
        row = dict(notification)
        if not row.get('briefing_id'):
            # Retired urgent alerts cannot send facts from the frozen predecessor.
            if row.get('attention_decision_id'):
                con.execute("UPDATE notification_outbox SET status='cancelled',last_error='retired application attention' WHERE notification_id=?", (row['notification_id'],))
                return False
            return AttentionService.validate_delivery(con, row, now)
        briefing = self._saved(con, row['briefing_id'])
        if briefing['snapshot'].get('backend') != 'owners':
            con.execute("UPDATE notification_outbox SET status='cancelled',last_error='retired application briefing' WHERE notification_id=?", (row['notification_id'],))
            return False
        if not AttentionService.validate_delivery(con, row, now, owner_validated=True): return False
        fresh = self.preview(briefing['slot'], briefing['local_date'])
        snapshot = fresh['snapshot']
        same_installation = snapshot['owner_identity'] == briefing['snapshot']['owner_identity']
        if same_installation and not snapshot['owner_activation']['paused'] and not snapshot['owner_activation'].get('restore_required') and self._revision(snapshot) == self._revision(briefing['snapshot']):
            return True
        # Creating a replacement ID is safe only before external I/O. On a
        # recovered delivery, retain uncertainty under its original ID.
        safe = row['status'] == 'pending' and row['attempts'] == 0 or row['status'] == 'delivering' and row['attempts'] == 1
        if same_installation and not snapshot['owner_activation']['paused'] and not snapshot['owner_activation'].get('restore_required') and safe:
            prefs = _preferences(con); stamp = text_time(_now(now)); revision = self._revision(snapshot)
            nid = self._enqueue(con, None, briefing['briefing_id'], None, fresh['title'], fresh['body'], briefing['expires_at'], prefs['revision'], stamp,
                                {'briefing_id': briefing['briefing_id'], 'backend': 'owners', 'replaces_notification_id': row['notification_id']}, ':owner-refresh:' + revision)
            con.execute("UPDATE attention_briefings SET notification_id=?,snapshot_json=?,selected_refs_json=?,title=?,body=?,renderer='deterministic_refresh',generation_json=NULL WHERE briefing_id=?",
                        (nid, canonical_json(snapshot), canonical_json(fresh['selected_refs']), fresh['title'], fresh['body'], briefing['briefing_id']))
        con.execute("UPDATE notification_outbox SET status=?,last_error=?,lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL WHERE notification_id=?",
                    ('cancelled' if safe else 'dead', 'owner briefing changed before delivery' if safe else 'delivery_reconciliation_required', row['notification_id']))
        return False
