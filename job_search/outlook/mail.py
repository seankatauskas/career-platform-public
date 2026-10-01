"""Privacy-bounded Outlook mail paging primitives."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional, Sequence
from urllib.parse import quote, urlencode

from job_search.contracts import MailChange, MailDeltaPage

from .auth import BASE_SCOPES
from .transport import GraphSession, RetryClass, validate_graph_url


MAIL_SELECT = (
    "id,conversationId,internetMessageId,sender,subject,receivedDateTime,"
    "lastModifiedDateTime,webLink"
)
BODY_SELECT = (
    "id,conversationId,internetMessageId,sender,from,replyTo,subject,"
    "receivedDateTime,lastModifiedDateTime,body,bodyPreview,isDraft,"
    "hasAttachments,webLink,internetMessageHeaders"
)
FOLDER_SELECT = "id,parentFolderId,childFolderCount,isHidden"
ATTACHMENT_SELECT = "id,name,contentType,size,isInline"


@dataclass(frozen=True)
class MailBackfillPage:
    changes: Sequence[MailChange]
    next_link: Optional[str]


@dataclass(frozen=True)
class MailFolder:
    folder_id: str
    parent_folder_id: str
    child_folder_count: int
    is_hidden: bool


@dataclass(frozen=True)
class MailFolderPage:
    folders: Sequence[MailFolder]
    next_link: Optional[str]


def _sender_address(item: Mapping[str, Any]) -> Optional[str]:
    sender = item.get("sender")
    if not isinstance(sender, Mapping):
        return None
    email_address = sender.get("emailAddress")
    if not isinstance(email_address, Mapping):
        return None
    address = email_address.get("address")
    return str(address) if isinstance(address, str) else None


def _mail_change(item: Mapping[str, Any]) -> MailChange:
    immutable_id = item.get("id")
    if not isinstance(immutable_id, str) or not immutable_id:
        raise ValueError("mail delta item is missing an immutable id")
    removed = isinstance(item.get("@removed"), Mapping)
    return MailChange(
        immutable_id=immutable_id,
        removed=removed,
        conversation_id=_optional_string(item.get("conversationId")),
        internet_message_id=_optional_string(item.get("internetMessageId")),
        sender_address=_sender_address(item),
        subject=_optional_string(item.get("subject")),
        received_at=_optional_string(item.get("receivedDateTime")),
        modified_at=_optional_string(item.get("lastModifiedDateTime")),
        body_preview=_optional_string(item.get("bodyPreview")),
        web_link=_optional_string(item.get("webLink")),
    )


def _optional_string(value: Any) -> Optional[str]:
    return value if isinstance(value, str) else None


def _page_values(payload: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
    values = payload.get("value", [])
    if not isinstance(values, list) or any(not isinstance(item, Mapping) for item in values):
        raise ValueError("Microsoft Graph page value must be an array of objects")
    return values


def _page_link(payload: Mapping[str, Any], name: str) -> Optional[str]:
    value = payload.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a URL")
    return validate_graph_url(value)


def _mail_folder(item: Mapping[str, Any]) -> MailFolder:
    folder_id = item.get("id")
    parent = item.get("parentFolderId") or ""
    children = item.get("childFolderCount", 0)
    if not isinstance(folder_id, str) or not folder_id or len(folder_id) > 2048:
        raise ValueError("mail folder is missing an immutable id")
    if not isinstance(parent, str) or len(parent) > 2048:
        raise ValueError("mail folder parent id is invalid")
    if isinstance(children, bool) or not isinstance(children, int) or children < 0:
        raise ValueError("mail folder child count is invalid")
    return MailFolder(folder_id, parent, children, bool(item.get("isHidden", False)))


class GraphMailClient:
    def __init__(self, session: GraphSession) -> None:
        self._session = session

    @staticmethod
    def backfill_url(
        *, folder: str = "inbox", now: Optional[datetime] = None, days: int = 90
    ) -> str:
        if days <= 0 or days > 90:
            raise ValueError("mail backfill must be between 1 and 90 days")
        cutoff = ((now or datetime.now(timezone.utc)).astimezone(timezone.utc) - timedelta(days=days))
        cutoff_text = cutoff.isoformat(timespec="seconds").replace("+00:00", "Z")
        params = urlencode(
            {
                "$select": MAIL_SELECT,
                "$filter": f"receivedDateTime ge {cutoff_text}",
                "$orderby": "receivedDateTime desc",
                "$top": "100",
            }
        )
        return f"/v1.0/me/mailFolders/{quote(folder, safe='')}/messages?{params}"

    @staticmethod
    def initial_delta_url(
        *, folder: str = "inbox", now: Optional[datetime] = None, days: int = 90
    ) -> str:
        if days <= 0 or days > 90:
            raise ValueError("mail delta backfill must be between 1 and 90 days")
        cutoff = ((now or datetime.now(timezone.utc)).astimezone(timezone.utc) - timedelta(days=days))
        cutoff_text = cutoff.isoformat(timespec="seconds").replace("+00:00", "Z")
        params = urlencode(
            {
                "$select": MAIL_SELECT,
                "$filter": f"receivedDateTime ge {cutoff_text}",
                "$orderby": "receivedDateTime desc",
                "$top": "100",
            }
        )
        return f"/v1.0/me/mailFolders/{quote(folder, safe='')}/messages/delta?{params}"

    @staticmethod
    def initial_all_history_delta_url(*, folder: str) -> str:
        """Begin a complete per-folder delta enumeration without a date filter."""

        if not isinstance(folder, str) or not folder:
            raise ValueError("mail folder id is required")
        params = urlencode({"$select": MAIL_SELECT, "$top": "100"})
        return f"/v1.0/me/mailFolders/{quote(folder, safe='')}/messages/delta?{params}"

    def read_mail_folder(self, folder_ref: str) -> MailFolder:
        if not isinstance(folder_ref, str) or not folder_ref:
            raise ValueError("mail folder reference is required")
        payload = self._session.request_json(
            "GET",
            f"/v1.0/me/mailFolders/{quote(folder_ref, safe='')}?$select={FOLDER_SELECT}",
            scopes=BASE_SCOPES,
            retry_class=RetryClass.READ,
        )
        return _mail_folder(payload)

    def read_folder_page(self, url: str) -> MailFolderPage:
        payload = self._session.request_json(
            "GET", url, scopes=BASE_SCOPES, retry_class=RetryClass.READ
        )
        return MailFolderPage(
            tuple(_mail_folder(item) for item in _page_values(payload)),
            _page_link(payload, "@odata.nextLink"),
        )

    def list_folder_tree(
        self, *, max_folders: int = 4096, max_pages: int = 1024
    ) -> Sequence[MailFolder]:
        """Enumerate top-level and nested folders with explicit page/cycle bounds."""

        if (
            isinstance(max_folders, bool)
            or not isinstance(max_folders, int)
            or max_folders < 1
            or max_folders > 10_000
        ):
            raise ValueError("mail folder bound must be between 1 and 10000")
        if (
            isinstance(max_pages, bool)
            or not isinstance(max_pages, int)
            or max_pages < 1
            or max_pages > 4096
        ):
            raise ValueError("mail folder page bound must be between 1 and 4096")
        queue = [
            "/v1.0/me/mailFolders?"
            + urlencode({"includeHiddenFolders": "true", "$select": FOLDER_SELECT, "$top": "100"})
        ]
        seen_urls = set()
        folders: dict[str, MailFolder] = {}
        expanded = set()
        while queue:
            if len(seen_urls) >= max_pages:
                raise ValueError("mail folder paging exceeded its bound")
            url = queue.pop(0)
            if url in seen_urls:
                raise ValueError("mail folder paging cycle detected")
            seen_urls.add(url)
            page = self.read_folder_page(url)
            for folder in page.folders:
                previous = folders.get(folder.folder_id)
                if previous is not None and previous != folder:
                    raise ValueError("mail folder identity changed during discovery")
                folders[folder.folder_id] = folder
                if len(folders) > max_folders:
                    raise ValueError("mail folder enumeration exceeded its bound")
                if folder.child_folder_count and folder.folder_id not in expanded:
                    expanded.add(folder.folder_id)
                    queue.append(
                        f"/v1.0/me/mailFolders/{quote(folder.folder_id, safe='')}/childFolders?"
                        + urlencode(
                            {
                                "includeHiddenFolders": "true",
                                "$select": FOLDER_SELECT,
                                "$top": "100",
                            }
                        )
                    )
            if page.next_link:
                queue.insert(0, page.next_link)
        return tuple(sorted(folders.values(), key=lambda item: item.folder_id))

    def read_backfill_page(self, url: str) -> MailBackfillPage:
        payload = self._session.request_json(
            "GET",
            url,
            scopes=BASE_SCOPES,
            retry_class=RetryClass.READ,
        )
        return MailBackfillPage(
            tuple(_mail_change(item) for item in _page_values(payload)),
            _page_link(payload, "@odata.nextLink"),
        )

    def read_delta_page(self, opaque_url: str) -> MailDeltaPage:
        payload = self._session.request_json(
            "GET",
            opaque_url,
            scopes=BASE_SCOPES,
            retry_class=RetryClass.READ,
        )
        next_link = _page_link(payload, "@odata.nextLink")
        delta_link = _page_link(payload, "@odata.deltaLink")
        if next_link and delta_link:
            raise ValueError("delta page cannot contain both nextLink and deltaLink")
        if not next_link and not delta_link:
            raise ValueError("delta page must contain a continuation or completed cursor")
        return MailDeltaPage(
            tuple(_mail_change(item) for item in _page_values(payload)),
            next_link,
            delta_link,
        )

    def read_message_body(self, immutable_message_id: str) -> Mapping[str, Any]:
        if not immutable_message_id:
            raise ValueError("immutable message id is required")
        url = f"/v1.0/me/messages/{quote(immutable_message_id, safe='')}?$select={BODY_SELECT}"
        return self._session.request_json(
            "GET",
            url,
            scopes=BASE_SCOPES,
            retry_class=RetryClass.READ,
            preferences=('outlook.body-content-type="text"',),
        )

    def list_attachments(
        self, immutable_message_id: str, *, limit: int = 20
    ) -> Sequence[Mapping[str, Any]]:
        if not isinstance(immutable_message_id, str) or not immutable_message_id:
            raise ValueError("immutable message id is required")
        if limit < 1 or limit > 20:
            raise ValueError("attachment limit must be between 1 and 20")
        url = (
            f"/v1.0/me/messages/{quote(immutable_message_id, safe='')}/attachments?"
            + urlencode({"$select": ATTACHMENT_SELECT, "$top": str(limit)})
        )
        payload = self._session.request_json(
            "GET", url, scopes=BASE_SCOPES, retry_class=RetryClass.READ
        )
        return tuple(_page_values(payload)[:limit])

    def read_file_attachment(
        self, immutable_message_id: str, immutable_attachment_id: str
    ) -> Mapping[str, Any]:
        if not immutable_message_id or not immutable_attachment_id:
            raise ValueError("message and attachment ids are required")
        select = ATTACHMENT_SELECT + ",contentBytes"
        payload = self._session.request_json(
            "GET",
            f"/v1.0/me/messages/{quote(immutable_message_id, safe='')}/attachments/"
            f"{quote(immutable_attachment_id, safe='')}?$select={select}",
            scopes=BASE_SCOPES,
            retry_class=RetryClass.READ,
        )
        encoded = payload.get("contentBytes")
        if not isinstance(encoded, str) or len(encoded) > 8 * 1024 * 1024:
            raise ValueError("Graph file attachment content is missing or too large")
        try:
            content = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise ValueError("Graph file attachment content is not valid base64") from exc
        result = dict(payload)
        result["contentBytes"] = content
        return result
