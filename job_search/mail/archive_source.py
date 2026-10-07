"""Bounded read-only Hermes source over the encrypted all-mail archive."""

from __future__ import annotations

from itertools import islice
from typing import Any, Mapping, Sequence

from job_search.contracts import ContractError, validate_identifier

from .archive import EncryptedMailArchive, KeychainArchiveKeyProvider


MAX_ARCHIVES_SCANNED = 200
MAX_SEARCH_RESULTS = 25
MAX_RESULT_EXCERPT = 600
MAX_MESSAGE_EXCERPT = 2048

_PREFIX = "BEGIN UNTRUSTED EMAIL\nSUBJECT\n"
_MIDDLE = "\nBODY\n"
_SUFFIX = "\nEND UNTRUSTED EMAIL"


def _parts(text: str) -> tuple[str, str]:
    """Split the authenticated sanitizer envelope without trusting its contents."""

    if text.startswith(_PREFIX) and text.endswith(_SUFFIX):
        payload = text[len(_PREFIX) : -len(_SUFFIX)]
        subject, marker, body = payload.partition(_MIDDLE)
        if marker:
            return subject[:512], body
    return "", text


def _excerpt(text: str, start: int, query_length: int, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = max(0, (limit - query_length) // 2)
    left = max(0, start - half)
    right = min(len(text), left + limit)
    left = max(0, right - limit)
    value = text[left:right]
    if left:
        value = "…" + value[1:]
    if right < len(text):
        value = value[:-1] + "…"
    return value


class EncryptedArchiveMailSource:
    """Decrypt only a bounded recent window and return sanitized text views.

    The source accepts no Graph client or token provider.  Plaintext lives only for
    the duration of each method call and is never written back to the ledger.
    """

    def __init__(
        self,
        ledger: Any,
        archive: EncryptedMailArchive,
        *,
        scan_limit: int = MAX_ARCHIVES_SCANNED,
    ) -> None:
        if (
            isinstance(scan_limit, bool)
            or not isinstance(scan_limit, int)
            or not 1 <= scan_limit <= MAX_ARCHIVES_SCANNED
        ):
            raise ValueError("mail archive scan limit must be between 1 and 200")
        self._ledger = ledger
        self._archive = archive
        self.scan_limit = scan_limit

    @staticmethod
    def _query(value: str) -> str:
        if not isinstance(value, str):
            raise ContractError("mail search query must be text")
        query = " ".join(value.split())
        if not 1 <= len(query) <= 200:
            raise ContractError("mail search query must be between 1 and 200 characters")
        return query

    def search_mail(
        self, query: str, limit: int
    ) -> Sequence[Mapping[str, Any]]:
        normalized = self._query(query)
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_SEARCH_RESULTS
        ):
            raise ContractError("mail search limit must be between 1 and 25")
        needle = normalized.casefold()
        results = []
        records = self._ledger.list_mail_archive_index(limit=self.scan_limit)
        for record in islice(records, self.scan_limit):
            archive_id = str(record["archive_id"])
            text = self._archive.read_message(archive_id)
            subject, body = _parts(text)
            searchable = subject + "\n" + body
            location = searchable.casefold().find(needle)
            if location < 0:
                continue
            results.append(
                {
                    "message_id": archive_id,
                    "subject": subject,
                    "excerpt": _excerpt(
                        searchable, location, len(normalized), MAX_RESULT_EXCERPT
                    ),
                }
            )
            if len(results) >= limit:
                break
        return tuple(results)

    def search_mail_page(self, query: str, limit: int = 25, *, cursor=None) -> Mapping[str, Any]:
        """Scan a bounded archive page; a negative result is scoped to this page."""
        import base64
        import json
        from job_search.contracts import payload_sha256
        from job_search.db import connect
        normalized = self._query(query)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_SEARCH_RESULTS:
            raise ContractError('mail search limit must be between 1 and 25')
        query_hash = payload_sha256({'query': normalized.casefold()})
        after = ''
        with connect(self._ledger.store.db_path) as con:
            high = con.execute('SELECT COALESCE(MAX(rowid),0) FROM mail_archive').fetchone()[0]
            if cursor is not None:
                try:
                    if not isinstance(cursor, str) or len(cursor) > 2048:
                        raise ValueError('invalid cursor')
                    decoded = json.loads(base64.urlsafe_b64decode(cursor.encode()))
                    if decoded['query'] != query_hash or not isinstance(decoded['high'], int) or isinstance(decoded['high'], bool) or decoded['high'] < 0:
                        raise ValueError('cursor query mismatch')
                    high, after = decoded['high'], decoded['after']
                    validate_identifier(after, 'cursor archive id')
                except (ValueError, TypeError, KeyError) as exc:
                    raise ContractError('invalid mail search cursor') from exc
            records = con.execute('SELECT archive_id,truncated FROM mail_archive WHERE rowid<=? AND archive_id>? ORDER BY archive_id LIMIT ?', (high, after, self.scan_limit+1)).fetchall()
        results = []
        scanned = 0
        last = after
        for record in records[:self.scan_limit]:
            scanned += 1
            last = str(record['archive_id'])
            subject, body = _parts(self._archive.read_message(last))
            text = subject + '\n' + body
            location = text.casefold().find(normalized.casefold())
            if location >= 0:
                results.append({'message_id': last, 'subject': subject,
                                'excerpt': _excerpt(text, location, len(normalized), MAX_RESULT_EXCERPT),
                                'archive_truncated': bool(record['truncated'])})
            if len(results) >= limit:
                break
        more = scanned < len(records)
        continuation = base64.urlsafe_b64encode(json.dumps({'query': query_hash, 'high': high, 'after': last}, separators=(',', ':')).encode()).decode() if more else None
        return {'items': results, 'next_cursor': continuation, 'complete': not more,
                'scanned': scanned, 'scan_limit': self.scan_limit,
                'coverage': 'encrypted archived text only; unarchived messages and truncated content are not searchable'}

    def get_review_message(self, message_id: str) -> Mapping[str, Any]:
        """Read the full archived text for an explicit dashboard disclosure."""
        validate_identifier(message_id, "message_id")
        record = self._ledger.get_encrypted_mail_archive(message_id)
        subject, body = _parts(self._archive.read_message(message_id))
        return {"subject": subject, "body": body, "truncated": bool(record['truncated'])}

    def get_mail_message(self, message_id: str) -> Mapping[str, Any]:
        validate_identifier(message_id, "message_id")
        text = self._archive.read_message(message_id)
        subject, body = _parts(text)
        return {
            "message_id": message_id,
            "subject": subject,
            "excerpt": _excerpt(body, 0, 0, MAX_MESSAGE_EXCERPT),
        }

    def get_review_message(self, message_id: str) -> Mapping[str, Any]:
        """Dashboard disclosure of the archived text without the agent excerpt cap."""
        validate_identifier(message_id, "message_id")
        record = self._ledger.get_encrypted_mail_archive(message_id)
        subject, body = _parts(self._archive.read_message(message_id))
        return {"subject": subject, "body": body, "truncated": bool(record['truncated'])}


def build_archive_mail_source(
    ledger: Any,
    *,
    archive: EncryptedMailArchive | None = None,
    key_provider: Any | None = None,
    scan_limit: int = MAX_ARCHIVES_SCANNED,
) -> EncryptedArchiveMailSource:
    """Construct the source with the same Keychain-backed archive boundary."""

    encrypted = archive or EncryptedMailArchive(
        ledger, key_provider or KeychainArchiveKeyProvider()
    )
    return EncryptedArchiveMailSource(ledger, encrypted, scan_limit=scan_limit)


__all__ = [
    "EncryptedArchiveMailSource",
    "MAX_ARCHIVES_SCANNED",
    "MAX_MESSAGE_EXCERPT",
    "MAX_RESULT_EXCERPT",
    "MAX_SEARCH_RESULTS",
    "build_archive_mail_source",
]
