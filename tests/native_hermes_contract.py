"""Run inside the pinned Hermes image with --network none and repository /work:ro.

This is an image conformance check, not part of the dependency-free Python suite.
"""
import asyncio
import importlib.util
import os
from pathlib import Path
import tempfile
from unittest.mock import patch


async def main():
    with tempfile.TemporaryDirectory() as directory:
        os.environ.update(HERMES_HOME=directory,JOB_SEARCH_INTERACTION_URL='http://127.0.0.1:8768',JOB_SEARCH_INTERACTION_TOKEN_FILE=directory+'/token',JOB_SEARCH_INTERACTION_SPOOL=directory+'/spool.sqlite',JOB_SEARCH_TELEGRAM_BOT_ID='123',JOB_SEARCH_TELEGRAM_USER_ID='456',JOB_SEARCH_TELEGRAM_CHAT_ID='456')
        Path(directory+'/config.yaml').write_text('plugins:\n  enabled: []\n')
        Path(directory+'/token').write_text('test-only-token-'*4)
        os.chmod(directory+'/token',0o600)
        spec=importlib.util.spec_from_file_location('career_native','/work/job_search/interactions/telegram.py')
        runtime=importlib.util.module_from_spec(spec);spec.loader.exec_module(runtime)
        from hermes_cli.plugins import PluginContext,PluginManifest,get_plugin_manager,get_pre_tool_call_directive
        manager=get_plugin_manager();manager.discover_and_load()
        ctx=PluginContext(PluginManifest(name='career-interactions',version='1.0.0'),manager)
        runtime.register(ctx)
        assert get_pre_tool_call_directive('terminal',{})[0]=='block'
        assert get_pre_tool_call_directive('mcp_job_search_get_briefing',{})[0] is None
        assert get_pre_tool_call_directive('mcp_other_execute',{})[0]=='block'
        from telegram import Update,User,CallbackQuery
        from telegram.ext import Application,TypeHandler
        from plugins.platforms.telegram.adapter import TelegramAdapter
        app=Application.builder().token('123:offline-test-token').build()
        app._initialized=True
        app.bot._bot_user=User(id=123,is_bot=True,first_name='Career')
        adapter=object.__new__(TelegramAdapter)
        # Actual plugin factory + actual PTB dispatch; no connect/getUpdates call.
        factories=manager.get_platform_handler_factories('telegram')
        factory=next(f for f,name in factories if name=='career-interactions')
        factory(app,adapter)
        seen=[]
        async def model_handler(update,context):seen.append(update.update_id)
        app.add_handler(TypeHandler(Update,model_handler),group=0)
        message={'message_id':101,'date':1791028800,'chat':{'id':456,'type':'private'},'from':{'id':456,'is_bot':False,'first_name':'Owner'},'text':'yes send it','reply_to_message':{'message_id':100,'date':1791028790,'chat':{'id':456,'type':'private'},'from':{'id':123,'is_bot':True,'first_name':'Career'},'text':'Review'}}
        await app.process_update(Update.de_json({'update_id':1,'message':message},app.bot))
        assert not seen,'approval reached model handler'
        bridge=app.bot_data['career_interactions'];assert len(bridge.spool.pending())==1
        message['text']='Please edit the reply to say Friday instead.'
        await app.process_update(Update.de_json({'update_id':2,'message':message},app.bot))
        assert seen==[2],'editing request did not reach proposal-only conversation'
        message['from']['id']=999
        await app.process_update(Update.de_json({'update_id':3,'message':message},app.bot))
        assert seen==[2],'unconfigured user reached career tools'
        answers=[]
        async def answer(query,*args,**kwargs):answers.append(True)
        callback={'id':'callback1','from':{'id':456,'is_bot':False,'first_name':'Owner'},'chat_instance':'test','data':'career:'+'a'*32+':approve','message':message['reply_to_message']}
        with patch.object(CallbackQuery,'answer',answer):
            await app.process_update(Update.de_json({'update_id':4,'callback_query':callback},app.bot))
        assert len(bridge.spool.pending())==2 and answers and seen==[2],'button escaped pre-model control'
        task=app.bot_data['career_interactions_task'];task.cancel()
        try:await task
        except asyncio.CancelledError:pass
        print('ok (native Hermes plugin registration, effective policy, PTB pre-model consumption, edit pass-through; network disabled)')


if __name__=='__main__':asyncio.run(main())
