"""Native Hermes Telegram hook, packaged into the image as a plugin.

Only the existing gateway receives Telegram updates. This module has no polling
or webhook receiver; it uses the already-authenticated PTB Application.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import urllib.error
import urllib.request
from urllib.parse import urlsplit

REVIEWED_HERMES_REVISION = '3c9847f5e86e81c23335a54daa532de28eeffeb3'
CAREER_TOOLS = frozenset('''search_mail_history get_application_briefing list_application_conversation
list_application_tasks list_application_details get_application_record_history list_lifecycle_reviews
propose_application_update propose_interview_revision list_interview_rounds list_application_reminders
get_notification_preferences get_briefing preview_briefing list_briefings list_attention get_career_reply
list_career_replies propose_career_reply request_career_reply review_career_reply search_jobs list_shortlist
list_resume_standards compare_resumes_for_job list_applications list_attention_items get_application_timeline
get_application_resume get_application_resume_content list_interviews explain_status search_mail get_mail_message
get_sanitized_evidence propose_reply propose_interview_slots create_reminder list_reminders cancel_reminder
get_action_status system_health'''.split())
DEFAULT_ALLOWED_TOOLS = frozenset('mcp_job_search_'+name for name in CAREER_TOOLS)
COMMAND_TEXT_PATTERN = r'(yes(?: send it)?|send it|no|cancel|got it|ack(?:nowledge)?|snooze 1 ?h(?:our)?)[.!]*'


def read_token(path):
    fd = os.open(path,os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 4096:
            raise ValueError('interaction token file must be an owner-only regular file')
        value = os.read(fd,4096).decode().strip()
    finally:
        os.close(fd)
    if len(value)<32 or any(c.isspace() for c in value):
        raise ValueError('invalid interaction credential')
    return value


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('interaction redirects are forbidden')


class InteractionClient:
    def __init__(self, url, token_file):
        target = urlsplit(url)
        if target.scheme not in ('http','https') or not target.hostname or target.username or target.password or target.query or target.fragment or target.path not in ('','/'):
            raise ValueError('invalid interaction service URL')
        if target.scheme == 'http' and target.hostname not in ('127.0.0.1','localhost','interactions'):
            raise ValueError('plaintext interaction ingress must be local or the private interactions service')
        self.url = url.rstrip('/')
        self.token_file = token_file
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),_NoRedirect())

    def call(self, path, payload=None):
        if path not in ('/health','/v1/reviews','/v1/reviews/delivered','/v1/reviews/claim','/v1/reviews/unknown','/v1/interactions'):
            raise ValueError('unsupported interaction operation')
        data = json.dumps(payload,separators=(',',':')).encode() if payload is not None else None
        req = urllib.request.Request(self.url+path,data=data,headers={'Authorization':'Bearer '+read_token(self.token_file),'Content-Type':'application/json'})
        with self.opener.open(req,timeout=5) as response:
            raw = response.read(131073)
            if len(raw)>131072:
                raise ValueError('interaction response too large')
            return json.loads(raw)


class Spool:
    def __init__(self,path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        if self.path.is_symlink():
            raise ValueError('spool cannot be a symlink')
        fd = os.open(self.path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError('spool must be a regular file')
            os.fchmod(fd,0o600)
        finally:
            os.close(fd)
        with self.connect() as con:
            con.executescript("""
              CREATE TABLE IF NOT EXISTS commands (key TEXT PRIMARY KEY,envelope TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',result TEXT);
              CREATE TABLE IF NOT EXISTS deliveries (ticket_id TEXT PRIMARY KEY,status TEXT NOT NULL,message_id TEXT);
            """)

    def connect(self):
        con = sqlite3.connect(self.path,timeout=5)
        con.row_factory = sqlite3.Row
        con.execute('PRAGMA synchronous=FULL')
        return con

    def put(self,envelope):
        key = envelope['bot_id']+':'+envelope['update_id']
        encoded = json.dumps(envelope,sort_keys=True,separators=(',',':'))
        with self.connect() as con:
            row = con.execute('SELECT envelope FROM commands WHERE key=?',(key,)).fetchone()
            if row and row[0] != encoded:
                raise ValueError('update content changed')
            if con.execute("SELECT count(*) FROM commands WHERE status='pending'").fetchone()[0] >= 1000:
                raise ValueError('interaction queue is full')
            con.execute('INSERT OR IGNORE INTO commands(key,envelope) VALUES (?,?)',(key,encoded))
        return key

    def pending(self):
        with self.connect() as con:
            return [dict(r) for r in con.execute("SELECT * FROM commands WHERE status='pending' ORDER BY rowid LIMIT 25")]

    def complete(self,key,result):
        with self.connect() as con:
            con.execute("UPDATE commands SET status='completed',result=? WHERE key=?",(json.dumps(result),key))

    def delivery(self,ticket_id):
        with self.connect() as con:
            row = con.execute('SELECT * FROM deliveries WHERE ticket_id=?',(ticket_id,)).fetchone()
            return dict(row) if row else None

    def unrecorded_deliveries(self):
        with self.connect() as con:
            return [dict(r) for r in con.execute("SELECT * FROM deliveries WHERE status='sent' LIMIT 25")]

    def unfinished_deliveries(self):
        with self.connect() as con:
            return [dict(r) for r in con.execute("SELECT * FROM deliveries WHERE status='sending' LIMIT 25")]

    def abandon_delivery(self,ticket_id,state):
        with self.connect() as con:
            if state=='deferred':
                con.execute("DELETE FROM deliveries WHERE ticket_id=? AND status='sending'",(ticket_id,))
            else:
                con.execute('UPDATE deliveries SET status=? WHERE ticket_id=?',(state,ticket_id))

    def delivered(self,ticket_id):
        with self.connect() as con:
            con.execute("UPDATE deliveries SET status='recorded' WHERE ticket_id=?",(ticket_id,))

    def claim_delivery(self,ticket_id):
        with self.connect() as con:
            return con.execute("INSERT OR IGNORE INTO deliveries(ticket_id,status) VALUES (?,'sending')",(ticket_id,)).rowcount == 1

    def sent(self,ticket_id,message_id):
        with self.connect() as con:
            con.execute("UPDATE deliveries SET status='sent',message_id=? WHERE ticket_id=?",(str(message_id),ticket_id))


def envelope_from_update(update,bot_id,identity):
    query = getattr(update,'callback_query',None)
    message = getattr(query,'message',None) if query else getattr(update,'message',None)
    user = getattr(query,'from_user',None) if query else getattr(message,'from_user',None)
    chat = getattr(message,'chat',None)
    if not message or not user or not chat:
        return None
    if str(bot_id) != identity['bot_id'] or str(user.id) != identity['user_id'] or str(chat.id) != identity['chat_id'] or chat.type != 'private':
        return None
    if user.is_bot or getattr(message,'forward_origin',None) or getattr(message,'forward_date',None) or getattr(update,'edited_message',None):
        return None
    value = dict(identity,chat_type='private',update_id=str(update.update_id),message_id=str(message.message_id),is_bot=False,edited=False,forwarded=False)
    if query:
        data = str(getattr(query,'data','') or '')
        match = re.fullmatch(r'career:([0-9a-f]{32}):(approve|reject|ack|snooze)',data)
        if not match:
            return None
        value.update(ticket_id=match[1],command=match[2],callback_id=str(query.id))
    else:
        text = str(getattr(message,'text','') or '').strip()
        if len(text)>1000:
            return None
        # Non-command editing requests continue to Hermes, which can only propose.
        if not re.fullmatch(COMMAND_TEXT_PATTERN,text,re.I):
            return None
        value['text'] = text
        reply = getattr(message,'reply_to_message',None)
        if reply and str(getattr(getattr(reply,'from_user',None),'id','')) == str(bot_id):
            value['reply_to_message_id'] = str(reply.message_id)
    return value


def allowed_tools_from_env():
    value = json.loads(os.environ.get('JOB_SEARCH_CAREER_ALLOWED_TOOLS_JSON','[]'))
    # Exact installed MCP names only. No wildcard or general Hermes execution tools.
    if not isinstance(value,list) or len(value)>150 or any(not isinstance(v,str) or v not in DEFAULT_ALLOWED_TOOLS for v in value):
        raise ValueError('career profile requires an explicit MCP tool allowlist')
    return frozenset(value) if value else DEFAULT_ALLOWED_TOOLS


def tool_policy(allowed,tool_name=None,**kwargs):
    if tool_name not in allowed:
        return {'action':'block','message':'This career profile permits only its reviewed Career Platform tools.'}
    return None


def review_text(ticket):
    r = ticket['review']
    if ticket['kind'] == 'send':
        recipients = ', '.join(str(x) for x in (r.get('recipients') or []))
        return '\n'.join(['Send this email?', 'Account: '+str(r.get('account_id') or ''),'To: '+recipients,'Subject: '+str(r.get('subject') or ''),'',str(r.get('body') or ''),'','Approval expires: '+ticket['expires_at']])
    return '\n'.join([str(r.get('title') or 'Career reminder'),str(r.get('summary') or ''),'Deadline: '+str(r.get('due_at') or 'Not recorded'),'Acknowledgment leaves the obligation open.'])


class TelegramInteractions:
    def __init__(self,app,identity,client,spool):
        self.app,self.identity,self.client,self.spool = app,identity,client,spool

    async def handle(self,update,context):
        from telegram.ext import ApplicationHandlerStop
        query = getattr(update,'callback_query',None)
        candidate_callback = bool(query and str(getattr(query,'data','')).startswith('career:'))
        message = getattr(update,'effective_message',None)
        sender = getattr(query,'from_user',None) if query else getattr(message,'from_user',None)
        chat = getattr(message,'chat',None)
        # The career profile itself is private, including read-only conversation.
        # Upstream allow-all/pairing settings cannot broaden this identity boundary.
        if message or query:
            authorized = bool(sender and chat and not getattr(sender,'is_bot',True) and str(sender.id)==self.identity['user_id'] and str(chat.id)==self.identity['chat_id'] and chat.type=='private' and str(context.bot.id)==self.identity['bot_id'])
            if not authorized or (query and not candidate_callback):
                if query:
                    await query.answer('This career profile accepts only its private career review controls.')
                raise ApplicationHandlerStop
            if message and re.fullmatch(COMMAND_TEXT_PATTERN,str(getattr(message,'text','')).strip(),re.I) and (getattr(update,'edited_message',None) or getattr(message,'forward_origin',None) or getattr(message,'forward_date',None)):
                await context.bot.send_message(chat_id=self.identity['chat_id'],text='Edited or forwarded text cannot authorize a decision. Reply directly to the current review.')
                raise ApplicationHandlerStop
        # Administrative slash commands could disable the policy/plugin. Career
        # profile administration remains outside this user/model conversation.
        if message and str(getattr(message,'text','')).startswith('/'):
            if getattr(message,'chat',None) and str(message.chat.id) == self.identity['chat_id']:
                await context.bot.send_message(chat_id=self.identity['chat_id'],text='Use the career dashboard for settings. Reply to a review to send, acknowledge, or snooze.')
            raise ApplicationHandlerStop
        envelope = envelope_from_update(update,context.bot.id,self.identity)
        if envelope is None:
            if candidate_callback:
                await query.answer('This review is not available for this user or chat.')
                raise ApplicationHandlerStop
            return
        try:
            self.spool.put(envelope)
        except Exception:
            if query:
                await query.answer('Could not save your decision. Please try again.')
            else:
                await context.bot.send_message(chat_id=self.identity['chat_id'],text='Could not save your decision. Please try again.')
            raise ApplicationHandlerStop
        if query:
            await query.answer('Decision received.')
        raise ApplicationHandlerStop

    async def tick(self):
        for receipt in self.spool.unfinished_deliveries():
            await asyncio.to_thread(self.client.call,'/v1/reviews/unknown',{'ticket_id':receipt['ticket_id']})
            self.spool.abandon_delivery(receipt['ticket_id'],'unknown')
        # A button can arrive before its delivery receipt reaches the app. Repair
        # that binding first; never consume the command against an unbound card.
        for receipt in self.spool.unrecorded_deliveries():
            await asyncio.to_thread(self.client.call,'/v1/reviews/delivered',{'ticket_id':receipt['ticket_id'],'message_id':receipt['message_id']})
            self.spool.delivered(receipt['ticket_id'])
        for row in self.spool.pending():
            try:
                result = await asyncio.to_thread(self.client.call,'/v1/interactions',json.loads(row['envelope']))
            except urllib.error.HTTPError as exc:
                if exc.code in (400,403,409):
                    result = {'status':'rejected','message':'This decision is no longer valid. Open a fresh review.'}
                else:
                    continue
            except Exception:
                continue
            self.spool.complete(row['key'],result)
            await self.app.bot.send_message(chat_id=self.identity['chat_id'],text=str(result.get('message','Decision recorded.'))[:1000])
        response = await asyncio.to_thread(self.client.call,'/v1/reviews')
        from telegram import InlineKeyboardButton,InlineKeyboardMarkup
        for ticket in response.get('items',[])[:25]:
            if any(str(ticket.get(k)) != v for k,v in self.identity.items()):
                continue
            saved = self.spool.delivery(ticket['ticket_id'])
            if saved and saved['status'] in ('sent','recorded'):
                await asyncio.to_thread(self.client.call,'/v1/reviews/delivered',{'ticket_id':ticket['ticket_id'],'message_id':saved['message_id']})
                continue
            if not self.spool.claim_delivery(ticket['ticket_id']):
                # Crash/timeout during Telegram send is ambiguous; never repeat it.
                continue
            claim = await asyncio.to_thread(self.client.call,'/v1/reviews/claim',{'ticket_id':ticket['ticket_id']})
            if not claim.get('send_allowed'):
                self.spool.abandon_delivery(ticket['ticket_id'],claim.get('state','unknown'))
                continue
            text = review_text(ticket)
            options = [('Send email','approve'),('Cancel','reject')] if ticket['kind']=='send' else [('Got it','ack'),('Snooze 1 hour','snooze')]
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(label,callback_data='career:'+ticket['ticket_id']+':'+command) for label,command in options]])
            chunks=[text[start:start+3300] for start in range(0,len(text),3300)] or ['Career review']
            for chunk in chunks[:-1]:
                await self.app.bot.send_message(chat_id=self.identity['chat_id'],text=chunk)
            anchor = await self.app.bot.send_message(chat_id=self.identity['chat_id'],text=chunks[-1]+'\n\nUse a button or reply to this message. Changes require another review.',reply_markup=keyboard)
            self.spool.sent(ticket['ticket_id'],anchor.message_id)
            await asyncio.to_thread(self.client.call,'/v1/reviews/delivered',{'ticket_id':ticket['ticket_id'],'message_id':str(anchor.message_id)})
            self.spool.delivered(ticket['ticket_id'])

    async def run(self):
        while True:
            try:
                if self.app.running:
                    await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # Durable rows remain pending; never log private payloads.
            await asyncio.sleep(5)


def register(ctx):
    if not os.environ.get('JOB_SEARCH_INTERACTION_URL'):
        return
    allowed = allowed_tools_from_env()
    identity = {k:os.environ.get('JOB_SEARCH_TELEGRAM_'+k.upper(),'') for k in ('bot_id','user_id','chat_id')}
    if any(not re.fullmatch(r'[0-9]{1,20}',v) for v in identity.values()):
        raise ValueError('career interactions require exact private Telegram identity')
    ctx.register_hook('pre_tool_call',lambda **kw:tool_policy(allowed,**kw))

    def factory(app,adapter):
        from telegram import Update
        from telegram.ext import TypeHandler
        from hermes_cli.plugins import get_pre_tool_call_directive
        # Verify the effective dispatch policy, not merely a configuration label.
        directive = get_pre_tool_call_directive('terminal',{})
        if not directive or directive[0] != 'block':
            raise RuntimeError('career tool restriction is not active')
        bridge = TelegramInteractions(app,identity,InteractionClient(os.environ['JOB_SEARCH_INTERACTION_URL'],os.environ['JOB_SEARCH_INTERACTION_TOKEN_FILE']),Spool(os.environ.get('JOB_SEARCH_INTERACTION_SPOOL','/opt/data/career-interactions.sqlite')))
        app.add_handler(TypeHandler(Update,bridge.handle,block=True),group=-100)
        app.bot_data['career_interactions'] = bridge
        app.bot_data['career_interactions_task'] = asyncio.create_task(bridge.run())
    ctx.register_platform_handler('telegram',factory)
