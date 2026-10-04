"""User-approved recruiter replies and private calendar commitments."""
def __getattr__(name):
    if name == "CareerActionService":
        from .service import CareerActionService
        return CareerActionService
    raise AttributeError(name)
