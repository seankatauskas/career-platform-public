"""Personal Outlook connector with a deliberately narrow Microsoft Graph surface.

The package can be imported without the optional MSAL dependencies.  Constructing
``MsalTokenProvider`` is the only operation that imports them.
"""

from .auth import (
    BASE_SCOPES,
    DRAFT_SCOPES,
    HOLD_SCOPES,
    MsalTokenProvider,
    OutlookAuthError,
    OutlookAuthRequired,
    SecureTokenStorageError,
)
from .calendar import GraphCalendarClient
from .client import GraphOutlookClient, UnsafeOutlookAction
from .cursor import (
    CursorConflict,
    CursorStateAdapter,
    InMemoryCursorStateAdapter,
    MailCursorState,
)
from .mail import GraphMailClient, MailBackfillPage, MailFolder, MailFolderPage
from .transport import (
    GraphHttpError,
    GraphLinkError,
    GraphOutcomeUnknown,
    retry_decision,
    validate_graph_url,
)
from .state import SQLiteOutlookState

__all__ = [
    "BASE_SCOPES",
    "DRAFT_SCOPES",
    "HOLD_SCOPES",
    "CursorConflict",
    "CursorStateAdapter",
    "GraphCalendarClient",
    "GraphHttpError",
    "GraphLinkError",
    "GraphMailClient",
    "GraphOutcomeUnknown",
    "GraphOutlookClient",
    "InMemoryCursorStateAdapter",
    "MailBackfillPage",
    "MailFolder",
    "MailFolderPage",
    "MailCursorState",
    "MsalTokenProvider",
    "OutlookAuthError",
    "OutlookAuthRequired",
    "SecureTokenStorageError",
    "UnsafeOutlookAction",
    "retry_decision",
    "validate_graph_url",
    "SQLiteOutlookState",
]
