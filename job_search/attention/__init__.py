"""Deterministic chief-of-staff attention and briefing authority."""

def __getattr__(name):
    if name == 'AttentionService':
        from .service import AttentionService
        return AttentionService
    raise AttributeError(name)
