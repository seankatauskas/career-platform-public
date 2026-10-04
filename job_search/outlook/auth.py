"""Optional MSAL authentication backed by encrypted macOS persistence."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, FrozenSet, Iterable, Optional, Protocol


AUTHORITY = "https://login.microsoftonline.com/consumers"
# Personal Outlook live verification returned 403 for Calendars.ReadBasic despite
# a successful grant. Request read-only calendar access; writes remain opt-in.
BASE_SCOPES = frozenset({"User.Read", "Mail.Read", "Calendars.Read"})
DRAFT_SCOPES = frozenset(set(BASE_SCOPES) | {"Mail.ReadWrite"})
SEND_SCOPES = frozenset(set(BASE_SCOPES) | {"Mail.Send"})
HOLD_SCOPES = frozenset(set(BASE_SCOPES) | {"Calendars.ReadWrite"})


class OutlookAuthError(RuntimeError):
    """Microsoft authentication failed without exposing token material."""


class OutlookAuthRequired(OutlookAuthError):
    """A user interaction is required; background workers must surface this."""


class SecureTokenStorageError(OutlookAuthError):
    """Encrypted token persistence is unavailable."""


class TokenProvider(Protocol):
    def get_token(
        self,
        scopes: FrozenSet[str],
        *,
        interactive: bool = False,
        force_refresh: bool = False,
    ) -> str: ...


def _safe_error(result: Any) -> str:
    if not isinstance(result, dict):
        return "authentication failed"
    # error_description can contain account identifiers.  Keep only the stable code.
    return str(result.get("error") or "authentication failed")


def _device_flow_message(flow: Any) -> str:
    """Return MSAL's bounded human instruction without terminal control bytes."""

    if not isinstance(flow, dict) or not isinstance(flow.get("user_code"), str):
        raise OutlookAuthError(_safe_error(flow))
    message = flow.get("message")
    if (
        not isinstance(message, str)
        or not message.strip()
        or len(message) > 4096
        or any(ord(character) < 32 and character not in "\r\n\t" for character in message)
    ):
        raise OutlookAuthError("device_code_instructions_unavailable")
    return message.strip()


class MsalTokenProvider:
    """Public-client MSAL adapter whose cache always uses encrypted persistence.

    ``msal`` and ``msal-extensions`` are intentionally imported lazily so all offline
    code and tests remain dependency-free. macOS uses the platform Keychain by
    default; headless hosts may inject the authenticated file persistence backed by
    a mounted secret. There is no plaintext fallback.
    """

    def __init__(
        self,
        client_id: str,
        cache_path: Path,
        *,
        authority: str = AUTHORITY,
        account_home_id: Optional[str] = None,
        msal_module: Any = None,
        extensions_module: Any = None,
        persistence: Any = None,
    ) -> None:
        if not isinstance(client_id, str) or not client_id.strip():
            raise OutlookAuthError("Microsoft client_id is required")
        if authority != AUTHORITY:
            raise OutlookAuthError("personal Outlook must use the consumers authority")
        if msal_module is None or extensions_module is None:
            try:
                import msal as imported_msal  # type: ignore[import-not-found]
                import msal_extensions as imported_extensions  # type: ignore[import-not-found]
            except ImportError as exc:
                raise OutlookAuthError(
                    "live Outlook authentication requires msal and msal-extensions"
                ) from exc
            msal_module = imported_msal
            extensions_module = imported_extensions

        cache_path = Path(cache_path).expanduser()
        cache_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if persistence is None:
            try:
                persistence = extensions_module.build_encrypted_persistence(str(cache_path))
            except Exception as exc:
                raise SecureTokenStorageError(
                    "encrypted token persistence is unavailable"
                ) from exc
        if not bool(getattr(persistence, "is_encrypted", False)):
            raise SecureTokenStorageError("refusing plaintext token persistence")

        self._cache = extensions_module.PersistedTokenCache(persistence)
        self._app = msal_module.PublicClientApplication(
            client_id,
            authority=authority,
            token_cache=self._cache,
        )
        self._account_home_id = account_home_id

    def _account(self) -> Any:
        accounts = list(self._app.get_accounts())
        if self._account_home_id:
            accounts = [
                account
                for account in accounts
                if account.get("home_account_id") == self._account_home_id
            ]
        if not accounts:
            return None
        if len(accounts) > 1 and not self._account_home_id:
            raise OutlookAuthRequired("multiple cached Microsoft accounts require selection")
        return accounts[0]

    def get_token(
        self,
        scopes: FrozenSet[str],
        *,
        interactive: bool = False,
        force_refresh: bool = False,
        device_code_callback: Optional[Callable[[str], None]] = None,
    ) -> str:
        if device_code_callback is not None and not interactive:
            raise OutlookAuthError("device code authentication must be interactive")
        requested = sorted(_validate_scopes(scopes))
        account = self._account()
        result = None
        if account is not None:
            result = self._app.acquire_token_silent(
                requested,
                account=account,
                force_refresh=force_refresh,
            )
        if not result and interactive:
            if device_code_callback is None:
                result = self._app.acquire_token_interactive(
                    requested,
                    redirect_uri="http://localhost",
                )
            else:
                flow = self._app.initiate_device_flow(scopes=requested)
                device_code_callback(_device_flow_message(flow))
                result = self._app.acquire_token_by_device_flow(flow)
        if not isinstance(result, dict) or "access_token" not in result:
            error = _safe_error(result)
            if error in {
                "interaction_required",
                "consent_required",
                "login_required",
                "no_tokens_found",
            } or not interactive:
                raise OutlookAuthRequired(error)
            raise OutlookAuthError(error)
        return str(result["access_token"])

    def disconnect(self) -> None:
        for account in list(self._app.get_accounts()):
            self._app.remove_account(account)


def _validate_scopes(scopes: Iterable[str]) -> FrozenSet[str]:
    result = frozenset(scopes)
    if not result:
        raise OutlookAuthError("at least one Microsoft Graph scope is required")
    if any(not isinstance(scope, str) or not scope.strip() for scope in result):
        raise OutlookAuthError("Microsoft Graph scopes must be nonempty strings")
    if result not in {BASE_SCOPES, DRAFT_SCOPES, HOLD_SCOPES, SEND_SCOPES}:
        raise OutlookAuthError("Microsoft Graph scopes exceed the Outlook capability boundary")
    return result


def default_cache_path() -> Path:
    base = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    return base / "job-search" / "outlook-token-cache.bin"
