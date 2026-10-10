"""Thin, capability-limited adapters for the replacement application's operations."""
from .commands import CommandContext, DomainError, Principal
from .applications.workflows import INTERNAL_OPERATIONS


class HumanApplicationAdapter:
    """Construct only after the dashboard/human interaction authenticates its session."""
    def __init__(self,runtime,actor_id):
        self.runtime=runtime
        self.principal=Principal(actor_id,"human",frozenset({"*"}))

    def command(self,operation,payload,key):
        return self.runtime.command(CommandContext(self.principal,key),operation,payload)


class AgentApplicationAdapter:
    """A model gets these bounded capabilities, never a human decision endpoint."""
    def __init__(self,runtime,actor_id="application-agent",*,delegation=None):
        self._runtime=runtime
        self._delegation=delegation
        capabilities={"propose_changes","prepare_reply","prepare_calendar_change"}
        if delegation is not None:
            capabilities.add(delegation.operation)
        self._principal=Principal(actor_id,"agent",frozenset(capabilities))

    def call(self,name,arguments=None,*,idempotency_key=None):
        arguments=arguments or {}
        if not isinstance(arguments,dict) or set(arguments)&{"actor_kind","principal","capabilities","origin","delegation"}:
            raise DomainError("not_authorized","Agent input cannot establish authority")
        if name=="list_applications":
            return self._runtime.queries.list_applications(**arguments)
        if name in {"get_application_timeline","get_application_workspace"}:
            return self._redact(self._runtime.queries.workspace(**arguments))
        if name in {"list_attention_items","list_pending_reviews"}:
            return self._redact(self._runtime.queries.review_queue(**arguments))
        if name=="get_briefing":
            return self._redact(self._runtime.queries.briefing(**arguments))
        if name=="get_application_message":
            if set(arguments)-{"application_id","message_id","max_chars"} or not {"application_id","message_id"} <= set(arguments):
                raise DomainError("invalid_input","Application and message identities are required")
            limit=arguments.get("max_chars",16000)
            if type(limit) is not int or not 1 <= limit <= 32000:
                raise DomainError("invalid_input","Invalid message text bound")
            reader=getattr(self._runtime,"mail_reader",None)
            if reader is None:
                raise DomainError("source_unavailable","An authorized application mail reader is required")
            return self._redact(reader.read_message(arguments["application_id"],arguments["message_id"],limit=limit))
        if name in {"propose_application_update","propose_interview_revision","create_reminder","cancel_reminder",
                    "get_application_briefing","list_application_conversation","list_application_tasks",
                    "list_application_details","get_application_record_history","list_lifecycle_reviews",
                    "list_interview_rounds","list_application_reminders","list_reminders","search_mail_history"}:
            from .application_compatibility import translate_tool
            translated=translate_tool(name,arguments)
            if translated.kind=="query":
                return self._redact(self._runtime.queries.query(translated.operation,translated.input))
            arguments={"operation":translated.operation,"input":dict(translated.input),
                "application_id":translated.input.get("application_id")}
            name="propose_changes"
            idempotency_key=idempotency_key or translated.idempotency_key
        if name=="propose_reply" and "envelope" not in arguments:
            if not idempotency_key:
                raise DomainError("invalid_input","Mutation requires a stable idempotency key")
            if set(arguments)-{"message_id","body","kind","task_id","allow_closed"}:
                raise DomainError("invalid_input","Unsupported reply preparation fields")
            prepared=self._runtime.reply_preparation.prepare(**arguments)
            if prepared["status"]!="ready":
                return prepared
            arguments={"envelope":prepared["envelope"]}
        operation={"propose_reply":"prepare_reply","propose_interview_slots":"prepare_calendar_change"}.get(name,name)
        if operation not in self._principal.capabilities:
            raise DomainError("not_authorized","This operation is not exposed to the agent")
        if not idempotency_key:
            raise DomainError("invalid_input","Mutation requires a stable idempotency key")
        context=CommandContext(self._principal,idempotency_key,delegation=self._delegation)
        return self._redact(self._runtime.command(context,operation,arguments))

    @classmethod
    def _redact(cls,value):
        if isinstance(value,dict):
            return {k:cls._redact(v) for k,v in value.items() if k not in {
                "account_id","provider_message_id","archive_ref","remote_id","provider_request_id"}}
        if isinstance(value,list):
            return [cls._redact(v) for v in value]
        return value


class BrowserObservationAdapter:
    """Existing paired-device authentication supplies device_id; payload cannot override it."""
    def __init__(self,runtime):
        self.runtime=runtime

    def observe(self,authenticated_device_id,payload,*,idempotency_key):
        if not authenticated_device_id or "device_id" in payload:
            raise DomainError("not_authorized","Use the authenticated device identity")
        principal=Principal("browser:"+authenticated_device_id,"worker",frozenset({"record_browser_observation"}))
        return self.runtime.command(CommandContext(principal,idempotency_key,"inferred"),
            "record_browser_observation",{**payload,"device_id":authenticated_device_id})

