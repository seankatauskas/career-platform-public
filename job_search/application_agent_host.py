"""One MCP registry: application owner tools plus unrelated existing capabilities."""
from .application_agent_tools import ApplicationAgentTools
from .hermes import HermesAdapter, HermesValidationError, build_hermes_capabilities
from .job_reviews.tools import TOOL_NAMES as REVIEW_TOOLS, READ_TOOLS as REVIEW_READS

# Explicit capability retention prevents an old lifecycle tool from reappearing
# merely because it was added to the legacy registry.
RETAINED = frozenset(REVIEW_TOOLS) | {
    'publish_curated_shortlist','search_jobs','list_shortlist','list_resume_standards',
    'compare_resumes_for_job','get_application_resume','get_application_resume_content','system_health',
}

class ProductionAgentTools:
    serialize_output=staticmethod(ApplicationAgentTools.serialize_output)
    destructive_tool_names=frozenset()
    def __init__(self,runtime,sources):
        self.runtime=runtime
        self.sources=sources
        self.applications=ApplicationAgentTools(runtime)
        self.existing=HermesAdapter(build_hermes_capabilities(sources))
        self.tool_names=tuple(self.applications.tool_names)+tuple(sorted(RETAINED-set(self.applications.tool_names)))
        self.read_only_tool_names=self.applications.read_only_tool_names|REVIEW_READS|{
            'search_jobs','list_shortlist','list_resume_standards','compare_resumes_for_job',
            'get_application_resume','get_application_resume_content','system_health'}
        self.idempotent_tool_names=frozenset(self.tool_names)
    def tool_definitions(self):
        return self.applications.tool_definitions()+tuple(x for x in self.existing.tool_definitions()
            if x['name'] in RETAINED and x['name'] not in self.applications.tool_names)
    def invoke(self,name,arguments=None):
        if name == 'system_health':
            if arguments is not None and arguments != {}:
                raise HermesValidationError('system_health does not accept arguments')
            from .application_gateway import ApplicationGateway
            return ApplicationGateway(self.runtime,self.sources.ledger).system_health()
        if name in self.applications.tool_names:return self.applications.invoke(name,arguments)
        if name in RETAINED:return self.existing.invoke(name,arguments)
        raise HermesValidationError('Unknown or retired application tool')


class RetiredInteractionReviews:
    """Old Telegram tickets cannot approve a different owner's operations.

    New reviews use the authenticated Applications workspace. The ingress remains
    healthy so a stale button receives an explicit conflict instead of a retry.
    """
    def __init__(self,identity):self.identity=identity
    def pending_reviews(self):return {'items':[],'review_location':'applications_workspace'}
    def _retired(self,*args,**kwargs):
        from .contracts import ConflictError
        raise ConflictError('This review has been retired; open the Applications workspace for the current proposal')
    ingest=mark_delivered=claim_delivery=delivery_unknown=_retired

class RetiredMailTools:
    def _retired(self,*args,**kwargs):
        raise HermesValidationError('Use the Correspondence owner tools')
    search_mail=get_mail_message=_retired
