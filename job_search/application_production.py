"""Production composition for Applications, Correspondence and External Actions.

All provider dependencies are lazy: opening the dashboard or starting a worker
never sends mail, calls a model, or selects an implicit Microsoft account.
"""
from datetime import timedelta

from .commands import DomainError
from .worker import RetryableTaskError, TaskResult, FollowUpTask


def owner_runtime(config, *, clock=None):
    from .application_runtime import ApplicationRuntime
    from .application_preparation import ReplyProviderContext
    resolver = configured_provider_resolver(config, config.environment())
    runtime=ApplicationRuntime(config.application_owner_db, clock=clock,
        reply_provider_resolver=lambda account: ReplyProviderContext(account,resolver(account)))
    runtime.mail_reader=LazyMailReader(config,runtime)
    runtime.queries.mail_source=runtime.mail_reader
    return runtime


def configured_provider_resolver(config, environment):
    providers = {}
    def resolve(account):
        if account != config.outlook_account_id:
            raise DomainError('not_authorized','Provider account does not match installation')
        if account not in providers:
            from .application_execution import ConfiguredOutlookProvider
            from .outlook.mail import GraphMailClient
            from .outlook.client import GraphOutlookClient
            tokens,session = _graph(config,environment)
            mail=GraphMailClient(session)
            providers[account]=ConfiguredOutlookProvider(account_id=account,
                expected_home_account_id=environment.get('OUTLOOK_HOME_ACCOUNT_ID',''),
                selected_home_account_id=tokens.selected_home_account_id,
                client=GraphOutlookClient(session),
                sent_folder_id=lambda:mail.read_mail_folder('sentitems').folder_id)
        return providers[account]
    return resolve


def _graph(config, environment):
    from pathlib import Path
    from .outlook.auth import MsalTokenProvider, default_cache_path
    from .outlook.transport import GraphSession, UrllibHttpAdapter
    selected=str(environment.get('OUTLOOK_HOME_ACCOUNT_ID') or '').strip()
    client_id=str(environment.get('OUTLOOK_CLIENT_ID') or '').strip()
    if not selected or not client_id:
        raise DomainError('dependency_unresolved','Configure the exact Microsoft home account before provider access')
    path=Path(environment.get('OUTLOOK_TOKEN_CACHE') or default_cache_path())
    persistence=None
    if config.portable_encryption_key_file is not None:
        from .secure_persistence import EncryptedFilePersistence
        persistence=EncryptedFilePersistence(path.resolve(),config.portable_encryption_key_file,'outlook-token-cache')
    tokens=MsalTokenProvider(client_id,path,account_home_id=selected,persistence=persistence)
    if tokens.selected_home_account_id()!=selected:
        raise DomainError('not_authorized','Selected Microsoft identity differs from installation')
    return tokens,GraphSession(tokens,UrllibHttpAdapter())


def _archive(config):
    from .mail.revision_archive import ImmutableMailArchive
    from .mail.archive import KeychainArchiveKeyProvider
    if config.portable_encryption_key_file is not None:
        from .secure_persistence import PortableArchiveKeyProvider
        key=PortableArchiveKeyProvider(config.portable_encryption_key_file)
    else:
        key=KeychainArchiveKeyProvider()
    predecessor=config.application_owner_db.parent/"predecessor.sqlite"
    return ImmutableMailArchive(config.application_db,key,predecessor_path=predecessor if predecessor.is_file() else None)


def configured_understanding_profile(config, environment):
    """Select the owner's structured provider without legacy classifier policy."""
    from pathlib import Path
    from .inference import load_inference_config
    if not config.remote_mail_inference_enabled:
        raise ValueError("Understanding requires explicitly enabled remote mail inference")
    path = config.mail_inference_config or config.inference_config or environment.get("JOB_SEARCH_INFERENCE_CONFIG")
    if not path:
        raise ValueError("Understanding requires an inference configuration")
    profile = load_inference_config(Path(path))
    generation = profile.structured_generation
    if generation is None:
        raise ValueError("Understanding requires structured_generation configuration")
    if generation.default_max_output_tokens < 8192:
        raise ValueError("Understanding requires at least 8192 configured output tokens")
    return profile


def _mail_handlers(config,runtime,environment,clock,*,model=False):
    from .application_mail import build_application_mail_handlers
    from .applications.understanding.analyzer import SharedAnalyzer
    from .inference import build_structured_provider
    if not model:
        return build_application_mail_handlers(runtime,_archive(config),config.outlook_account_id,None,"unconfigured",
            operational_db=config.application_db)
    inference=configured_understanding_profile(config,environment)
    return build_application_mail_handlers(runtime,_archive(config),config.outlook_account_id,
        SharedAnalyzer(build_structured_provider(inference)),inference.structured_generation.generation_identity,
        operational_db=config.application_db)


def _sync_handler(config,runtime,environment):
    from .application_mail import ApplicationMailIngestor,OwnerMailCoordinator,OwnerMailTaskHandler
    from .outlook.mail import GraphMailClient
    from .outlook.state import SQLiteOutlookState
    _,session=_graph(config,environment)
    ingestor=ApplicationMailIngestor(runtime,_archive(config),config.outlook_account_id)
    from .activation import mail_start
    start=(lambda:mail_start(config.application_db,config.outlook_account_id)) if config.outlook_new_messages_only else None
    coordinator=OwnerMailCoordinator(GraphMailClient(session),SQLiteOutlookState(config.application_db),ingestor,received_since=start() if start else None)
    return OwnerMailTaskHandler(coordinator,account_id=config.outlook_account_id,
        folder_refs=config.outlook_mail_folders,max_messages=100,activation_start=start)


def _seed(config,clock,environment):
    from .db import connect
    from .scheduler import utc_stamp
    from .contracts import canonical_json
    now=clock();stamp=utc_stamp(now)
    kinds={'applications.tick':1,'application.dispatch':1,'application.feedback.dispatch':1}
    if config.remote_mail_inference_enabled or environment.get("OUTLOOK_CLIENT_ID"):
        kinds['applications.mail.dispatch']=1
    if environment.get("OUTLOOK_CLIENT_ID"):
        kinds['applications.mail.sync']=config.outlook_poll_interval_minutes
    with connect(config.application_db) as con:
        from .activation import disabled_tasks
        disabled=set(disabled_tasks(con))
        for kind,minutes in kinds.items():
            con.execute("INSERT INTO schedule_specs(schedule_key,task_kind,schedule_json,enabled,coalesce,next_due_at,updated_at,enabled_since) VALUES(?,?,?,?,1,?,?,?) ON CONFLICT(schedule_key) DO UPDATE SET enabled=excluded.enabled,updated_at=excluded.updated_at",
                (kind,kind,canonical_json({'kind':'interval','minutes':minutes}),int(kind not in disabled),utc_stamp(now+timedelta(minutes=minutes)),stamp,stamp))


def build_owner_handlers(config,environment,*,lane,clock):
    from .application_runtime import ApplicationRuntime
    runtime=ApplicationRuntime(config.application_owner_db,clock=lambda:clock().isoformat().replace('+00:00','Z'))
    handlers={}
    mail_cache={}
    def mail(kind):
        def handle(payload,context):
            if not context.heartbeat():raise RetryableTaskError('Worker lease lost')
            if not mail_cache:
                mail_cache.update(_mail_handlers(config,runtime,environment,clock,model=lane=='model'))
            return mail_cache[kind](payload,context)
        return handle
    if lane=='model':
        handlers['applications.mail.understand']=mail('applications.mail.understand')
        return handlers
    _seed(config,clock,environment)
    for kind in ('applications.mail.dispatch','applications.mail.project','applications.mail.outgoing'):
        handlers[kind]=mail(kind)
    sync_cache={}
    def sync(payload,context):
        if not sync_cache:sync_cache['handler']=_sync_handler(config,runtime,environment)
        return sync_cache['handler'](payload,context)
    handlers['applications.mail.sync']=sync
    def tick(payload,context):
        if not context.heartbeat():raise RetryableTaskError('Worker lease lost')
        # Internal consequences are allowed while external dispatch is paused.
        return {'scheduled':runtime.process_scheduled(),'results':runtime.process_results()}
    handlers['applications.tick']=tick
    from .application_feedback import build_feedback_handlers
    from .preference import PreferenceGateway,PreferencePaths
    handlers.update(build_feedback_handlers(runtime,PreferenceGateway(PreferencePaths(config.jobs_db,config.preference_db,config.proxy_db))))
    from .application_execution import ProductionExecution
    client=None
    if config.hermes_notification_socket and config.hermes_telegram_target:
        from .hermes_delivery import HermesDeliveryClient
        client=HermesDeliveryClient(config.hermes_notification_socket,expected_target=config.hermes_telegram_target)
    execution=ProductionExecution(runtime,configured_provider_resolver(config,environment),
        activation_status=runtime.executor.activation_status,activation_guard=runtime.executor.activation_guard,
        notification_client=client,notification_target=config.hermes_telegram_target)
    handlers['application.dispatch']=execution.dispatch
    for kind,method,owner,work_kind in (
        ('application.execute_action',execution.execute_action,'external_actions','execute_action'),
        ('application.reconcile_action',execution.reconcile_action,None,None),
        ('application.deliver_owner_notification',execution.deliver_owner_notification,'applications','deliver_owner_notification')):
        def handle(payload,context,method=method,owner=owner,work_kind=work_kind):
            work_id=payload.get('owner_work_id')
            result=method({k:v for k,v in payload.items() if k!='owner_work_id'},context)
            if not result.get('work_complete'):
                raise RetryableTaskError('Owner work awaits recovery',retry_after_seconds=60)
            if work_id and owner:
                runtime.executor.complete_work(work_id,owner=owner,kind=work_kind)
            return result
        handlers[kind]=handle
    return handlers


class LazyMailReader:
    def __init__(self,config,runtime):self.config,self.runtime=config,runtime
    def read_message(self,application_id,message_id,*,limit=250000):
        from .application_mail import ApplicationMailReader
        return ApplicationMailReader(self.runtime,_archive(self.config),(self.config.outlook_account_id,)).read_message(application_id,message_id,limit=limit)
    def can_review_source(self,source_id,revision,sha256):
        from .application_mail import ApplicationMailReader
        return ApplicationMailReader(self.runtime,None,(self.config.outlook_account_id,)).can_review_source(source_id,revision,sha256)
    def read_review_source(self,source_id,revision,sha256,*,limit=250000):
        from .application_mail import ApplicationMailReader
        return ApplicationMailReader(self.runtime,_archive(self.config),(self.config.outlook_account_id,)).read_review_source(source_id,revision,sha256,limit=limit)
    def search_mail_page(self,query,limit=25,*,cursor=None):
        from .application_mail import ApplicationMailReader
        return ApplicationMailReader(self.runtime,_archive(self.config),(self.config.outlook_account_id,)).search_mail_page(query,limit,cursor=cursor)
