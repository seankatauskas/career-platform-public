"""Trusted human interactions; deliberately absent from the model tool registry."""
__all__ = ['InteractionsService']


def __getattr__(name):
    if name == 'InteractionsService':
        from .service import InteractionsService
        return InteractionsService
    raise AttributeError(name)
