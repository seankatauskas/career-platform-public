"""Pull public job postings from every Ashby, Greenhouse and Lever job board.

No API key. Each ATS publishes an unauthenticated per-company posting API with no
global search, so this runs in two phases: discover board slugs from the Internet
Archive, then fetch and filter every board.

    uv run python -m job_search.collection.boards --all                              # every job, all platforms
    uv run python -m job_search.collection.boards --title "software engineer"
    uv run python -m job_search.collection.boards --title "software engineer" --match exact
    uv run python -m job_search.collection.boards --ats greenhouse --title "swe"     # one platform
    uv run python -m job_search.collection.boards --ats ashby,lever --all            # a subset
    uv run python -m job_search.collection.boards --grep '\\brust\\b|\\bgolang\\b'     # search descriptions
    uv run python -m job_search.collection.boards --refresh-boards
    uv run python -m job_search.collection.boards --discover-only --refresh-boards  # boards, no postings

Results go to CSV and JSON, and accumulate into a SQLite database keyed on
(ats, posting id) so first_seen/last_seen/closed_at survive across scrapes. Discovery-
only runs persist the current board registry and per-run audit history in the same DB.

job_search/collection/boards.seed.json ships with the repo, so --refresh-boards is optional. See the README.
"""

# Keeps `X | None` annotations from being evaluated at import, so the file also
# imports under Python 3.9 — which is what a bare `python3` is on macOS, and what
# tooling gets when uv is not on its PATH. Keep the no-uv collector fallback
# documented in AGENTS.md working even when the application uses newer Python.
from __future__ import annotations

import argparse
import csv
import gzip
import http.client
from html import escape
from io import BytesIO
import json
import os
import re
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from job_search.salary.enrichment import enrich_job, html_to_text, save_enrichments
from job_search.salary.llm import sync_scan_enrichments

HERE = Path(__file__).resolve().parents[2]
# Two files on purpose. The seed is small, curated and committed, so a fresh clone
# works without touching any archive. The cache is whatever the last crawl produced
# — potentially three vendors' entire customer lists — and is gitignored, so a full
# crawl never turns this repo into published competitive intelligence.
BOARDS_SEED = HERE / "job_search/collection/boards.seed.json"
BOARDS_CACHE = Path(os.environ.get("JOB_BOARDS_CACHE") or HERE / "boards.json")
COLLINFO = "https://index.commoncrawl.org/collinfo.json"
WAYBACK_CDX = (
    "http://web.archive.org/cdx/search/cdx?url={domain}"
    "&matchType=domain&fl=original&collapse=urlkey&output=json"
)
URLSCAN_SEARCH = "https://urlscan.io/api/v1/search/?q=page.domain%3A{domain}&size=10000"
_SLUG_SHAPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,60}$")
_SLUG_JUNK = {
    "_next", "api", "static", "assets", "meeting", "b", "favicon.ico",
    "robots.txt", "sitemap.xml", "embed", "css", "js", "images", "img",
}

# Archive operators ask that clients identify themselves. Set JOB_SCRAPER_CONTACT to
# your own email so a server operator can reach *you* about *your* traffic.
#
# Must be ASCII: http.client encodes headers as latin-1, so a stray em-dash or an
# accented character here makes every single request raise before it leaves the
# process. Non-ASCII is stripped rather than allowed to break the run.
_CONTACT = (
    os.environ.get("JOB_SCRAPER_CONTACT")
    or os.environ.get("ASHBY_SCRAPER_CONTACT")  # the name this had before Greenhouse
    or "set JOB_SCRAPER_CONTACT"
)
UA = f"job-boards-scraper/1.0 (public posting APIs; contact: {_CONTACT})".encode(
    "ascii", "ignore"
).decode()

# `ats` and `company` identify the board; the rest is normalised from whichever API
# it came from. Descriptions are normalized to plain text for portable output and
# future preference modeling. Original formatting is retained in description_html.
# `matched` holds --grep context and is empty without it.
FIELDS = [
    "ats", "company", "id", "title", "department", "team", "employmentType",
    "location", "isRemote", "workplaceType", "publishedAt", "jobUrl",
    "description", "matched", "posted_at", "source_updated_at", "description_html",
]

_SPACE = re.compile(r"\s+")


class NotFound(Exception):
    """Board slug returned 404 — not a customer of that ATS (or never was)."""


class NotModified(Exception):
    """Server answered 304: the body is byte-identical to what we last fetched."""


class RateLimited(Exception):
    """Common Crawl returned 503. Per their docs this means the request rate was
    too high; a repeatedly-abusive IP can be blocked for 24 hours."""


# Hosts whose connections are pooled. Only the posting APIs: they take one request
# per board — over 13,000 in a full run — and a fresh TLS handshake for each was
# measured at 102ms against 64ms on a reused connection. Everything else (the
# archives, urlscan) is a handful of requests per run and may redirect, which
# urlopen handles and a raw connection would not, so those stay on urlopen.
_POOLED_HOSTS = {
    "api.ashbyhq.com",
    "boards-api.greenhouse.io",
    "api.lever.co",
}
# One connection per thread per host. Sharing across threads would need a lock and
# serialise the pool; a thread-local dict keeps the 8 workers independent, so a full
# run opens ~8 connections per host rather than one per board.
_CONNECTIONS = threading.local()


def _lower_headers(items) -> dict:
    """Lowercase header names.

    urlopen returns an email.message.Message, which looks keys up case-insensitively.
    A plain dict does not, so the pooled path has to normalise or a vendor changing
    `Content-Encoding` to `content-encoding` would silently skip gunzipping and hand
    back compressed bytes. Not hypothetical: these three APIs already disagree about
    the casing of `ETag`.
    """
    return {k.lower(): v for k, v in (items.items() if hasattr(items, "items") else items)}


def _pooled_request(url: str, method: str, timeout: int, headers: dict) -> tuple[int, dict, bytes]:
    """One request over a reused per-thread connection. Returns (status, headers, body).

    A pooled connection can be closed by the server between requests, which surfaces
    as an exception on the next use rather than at close time, so a dead connection is
    dropped and retried once before giving up.
    """
    parts = urllib.parse.urlsplit(url)
    pool = getattr(_CONNECTIONS, "pool", None)
    if pool is None:
        pool = _CONNECTIONS.pool = {}
    target = parts.path + (f"?{parts.query}" if parts.query else "")

    for attempt in (0, 1):
        conn = pool.get(parts.netloc)
        if conn is None:
            conn = pool[parts.netloc] = http.client.HTTPSConnection(
                parts.netloc, timeout=timeout
            )
        try:
            conn.request(method, target, headers=headers)
            resp = conn.getresponse()
            body = resp.read()  # must drain, or the connection cannot be reused
            return resp.status, _lower_headers(resp.getheaders()), body
        except (http.client.HTTPException, OSError):
            try:
                conn.close()
            except Exception:
                pass
            pool.pop(parts.netloc, None)
            if attempt:
                raise
    raise RuntimeError("unreachable")


def _single_request(
    url: str, method: str, timeout: int, etag: str | None = None
) -> tuple[int, dict, bytes]:
    """One request, pooled where that is safe and via urlopen everywhere else."""
    headers = {"User-Agent": UA, "Accept-Encoding": "gzip"}
    if etag:
        headers["If-None-Match"] = etag
    if urllib.parse.urlsplit(url).netloc in _POOLED_HOSTS:
        return _pooled_request(url, method, timeout, headers)
    req = urllib.request.Request(url, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, _lower_headers(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, _lower_headers(e.headers or {}), e.read()


# ponytail: seconds-form Retry-After only. The HTTP-date form is legal but none of
# these APIs send it, and falling back to the exponential delay is already correct.
def _retry_delay(retry_after: str | None, attempt: int, cap: float = 30.0) -> float:
    """How long to wait before retrying a throttled request.

    Capped: a server asking for an hour would stall a 13,000-board run behind one
    slug, and at that point giving up and logging the board is the better trade.
    """
    try:
        return min(float(retry_after), cap)
    except (TypeError, ValueError):
        return float(2**attempt)


def fetch(
    url: str,
    timeout: int = 30,
    retries: int = 4,
    method: str = "GET",
    etag: str | None = None,
    meta: dict | None = None,
) -> bytes:
    """GET a URL, transparently gunzipping. Raises NotFound on 404.

    Common Crawl's CDX index 502/504s under load often enough that a single
    attempt fails maybe half the time, so 5xx gets exponential backoff.

    429 and 403 get the same backoff. They are 4xx, so without this they took the
    raise-immediately path and a throttled board was dropped for the whole run: a
    real Greenhouse scrape lost 8 consecutive slugs that way. `Retry-After` wins
    over the exponential delay when the server sends it, since that is the server
    telling us exactly how long it wants.
    """
    for attempt in range(retries):
        try:
            status, headers, body = _single_request(url, method, timeout, etag)
            if meta is not None:
                meta["etag"] = headers.get("etag")
            if status == 304:
                raise NotModified(url)
            if status == 404:
                raise NotFound(url)
            if status == 503:
                raise RateLimited(url)
            if status >= 500:
                if attempt == retries - 1:
                    raise urllib.error.HTTPError(url, status, "server error", None, None)
                time.sleep(2**attempt)
                continue
            if status in (429, 403):
                if attempt == retries - 1:
                    raise urllib.error.HTTPError(url, status, "throttled", None, None)
                time.sleep(_retry_delay(headers.get("retry-after"), attempt))
                continue
            if status >= 400:
                # Bounded evidence for a caller recognizing a protocol-specific error.
                raise urllib.error.HTTPError(url, status, "client error", headers, BytesIO(body[:4096]))
            if headers.get("content-encoding") == "gzip":
                body = gzip.decompress(body)
            return body
        except urllib.error.HTTPError:
            # HTTP retry/backoff decisions were already made above. In particular,
            # do not immediately repeat non-retryable 400s through URLError's base class.
            raise
        except (urllib.error.URLError, TimeoutError, http.client.HTTPException, OSError):
            if attempt == retries - 1:
                raise
            time.sleep(2**attempt)
    raise RuntimeError("unreachable")


def plain_text(value: str) -> str:
    """Decode entities, strip HTML tags and collapse whitespace."""
    return html_to_text(value)


# --------------------------------------------------------------------------- #
# Per-ATS adapters
#
# Each API returns a different shape, so a normaliser maps it onto FIELDS and
# everything downstream — filters, CSV, SQLite — stays platform-agnostic. A
# normaliser returns None for a posting that should not be listed at all.
#
# Description payloads may contain HTML. scan_board() turns them into plain text
# before filtering or emitting the shared row shape.
# --------------------------------------------------------------------------- #


def normalize_ashby(job: dict) -> dict | None:
    if not job.get("isListed"):
        return None
    return {
        "id": str(job.get("id", "")),
        "title": job.get("title") or "",
        "department": job.get("department") or "",
        "team": job.get("team") or "",
        "employmentType": job.get("employmentType") or "",
        "location": job.get("location") or "",
        "isRemote": bool(job.get("isRemote")),
        "workplaceType": job.get("workplaceType") or "",
        "publishedAt": job.get("publishedAt") or "",
        "posted_at": job.get("publishedAt") or "",
        "source_updated_at": "",
        "jobUrl": job.get("jobUrl") or "",
        "description": job.get("descriptionPlain") or job.get("descriptionHtml") or "",
        "description_html": formatted_description("ashby", job),
    }


def normalize_greenhouse(job: dict) -> dict | None:
    # location is a nested object, not a string. Greenhouse exposes no remote flag
    # and no department on this endpoint, so remoteness is inferred from the label.
    loc = job.get("location") or {}
    # `or ""` rather than a get() default: Greenhouse sends {"name": null}, where
    # the key exists so the default never applies and the value stays None.
    name = (loc.get("name") or "") if isinstance(loc, dict) else str(loc)
    return {
        "id": str(job.get("id", "")),
        "title": job.get("title") or "",
        "department": "",
        "team": "",
        "employmentType": "",
        "location": name,
        "isRemote": "remote" in name.lower(),
        "workplaceType": "",
        "publishedAt": job.get("first_published") or job.get("updated_at") or "",
        "posted_at": job.get("first_published") or "",
        "source_updated_at": job.get("updated_at") or "",
        "jobUrl": job.get("absolute_url") or "",
        # Present on posting scans because scan_board requests ?content=true.
        "description": job.get("content") or "",
        "description_html": formatted_description("greenhouse", job),
    }


def lever_description(job: dict) -> str:
    """Assemble every public description section without duplicating the body.

    Lever's plain-text fields are optional even when their HTML counterparts contain
    content. `descriptionPlain`, when present, is already the combined opening + body;
    `descriptionBodyPlain` must therefore be a fallback, not something appended to it.
    Requirements/benefits live separately in `lists`, and the closing text lives in
    `additionalPlain`/`additional`.
    """
    combined = job.get("descriptionPlain") or job.get("description")
    if not combined:
        opening = job.get("openingPlain") or job.get("opening") or ""
        body = job.get("descriptionBodyPlain") or job.get("descriptionBody") or ""
        combined = " ".join(str(value) for value in (opening, body) if value)

    parts = [str(combined)] if combined else []
    lists = job.get("lists") or []
    if isinstance(lists, list):
        for section in lists:
            if not isinstance(section, dict):
                continue
            heading = section.get("text") or ""
            content = section.get("content") or ""
            if heading:
                parts.append(str(heading))
            if content:
                parts.append(str(content))

    additional = job.get("additionalPlain") or job.get("additional") or ""
    if additional:
        parts.append(str(additional))
    return " ".join(parts)


def formatted_description(ats: str, job: dict) -> str:
    """Keep the source layout separately from the stable model/search text."""
    def section(html_key: str, plain_key: str) -> str:
        if job.get(html_key):
            return str(job[html_key])
        plain = str(job.get(plain_key) or "")
        return "<p>" + escape(plain).replace("\n", "<br>") + "</p>" if plain else ""

    if ats == "ashby":
        return section("descriptionHtml", "descriptionPlain")
    if ats == "greenhouse":
        return str(job.get("content") or "")
    if job.get("description") or job.get("descriptionPlain"):
        body = section("description", "descriptionPlain")
    else:
        body = section("opening", "openingPlain") + section("descriptionBody", "descriptionBodyPlain")
    lists = job.get("lists") or []
    for item in lists if isinstance(lists, list) else []:
        if not isinstance(item, dict):
            continue
        heading = escape(str(item.get("text") or ""))
        body += "<h3>" + heading + "</h3><ul>" + str(item.get("content") or "") + "</ul>"
    return body + section("additional", "additionalPlain")


def normalize_lever(job: dict) -> dict | None:
    # Two traps here. The title field is `text`, not `title` — reading `title` gives
    # a silently empty column. And createdAt is epoch milliseconds, which has to
    # become ISO or it sorts and compares wrongly against the other platforms.
    cat = job.get("categories") or {}
    created = job.get("createdAt")
    published = ""
    if isinstance(created, (int, float)):
        published = datetime.fromtimestamp(
            created / 1000, timezone.utc
        ).isoformat(timespec="seconds")
    workplace = job.get("workplaceType") or ""
    return {
        "id": str(job.get("id", "")),
        "title": job.get("text") or "",
        "department": cat.get("department") or "",
        "team": cat.get("team") or "",
        "employmentType": cat.get("commitment") or "",
        "location": cat.get("location") or "",
        "isRemote": workplace.lower() == "remote",
        "workplaceType": workplace,
        "publishedAt": published,
        "posted_at": published,
        "source_updated_at": "",
        "jobUrl": job.get("hostedUrl") or "",
        "description": lever_description(job),
        "description_html": formatted_description("lever", job),
    }


SOURCES = {
    "ashby": {
        "domains": ["jobs.ashbyhq.com"],
        "api": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
        "jobs": lambda payload: payload.get("jobs"),
        "normalize": normalize_ashby,
        # Ashby returns descriptions whether or not we want them.
        "content_param": None,
        # Adds the compensation object to each posting in the same board response.
        # It is not a per-job request.
        "always_param": "includeCompensation=true",
        "junk_prefixes": ("root.",),
    },
    "greenhouse": {
        "domains": ["boards.greenhouse.io", "job-boards.greenhouse.io"],
        "api": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
        "jobs": lambda payload: payload.get("jobs"),
        "normalize": normalize_greenhouse,
        # Descriptions are opt-in and cost ~26x the bytes (25KB -> 653KB gzipped
        # on one measured board). Posting scans deliberately request them so every
        # emitted and persisted job carries its full normalized description.
        "content_param": "content=true",
        "always_param": None,
        "junk_prefixes": (),
    },
    "lever": {
        "domains": ["jobs.lever.co"],
        "api": "https://api.lever.co/v0/postings/{slug}?mode=json",
        # Lever's payload IS the list; there is no wrapper object.
        "jobs": lambda payload: payload if isinstance(payload, list) else None,
        "normalize": normalize_lever,
        "content_param": None,
        "always_param": None,
        "junk_prefixes": (),
    },
}


def _clean(row: dict) -> dict:
    """Trim stray whitespace. Real payloads carry tabs and newlines inside titles,
    which otherwise corrupt sort order and leak into the CSV."""
    return {
        k: (_SPACE.sub(" ", v).strip() if isinstance(v, str) and k not in {"description", "description_html"} else v)
        for k, v in row.items()
    }


def board_url(ats: str, slug: str, want_content: bool = False) -> str:
    url = SOURCES[ats]["api"].format(slug=urllib.parse.quote(slug))
    params = []
    if SOURCES[ats].get("always_param"):
        params.append(SOURCES[ats]["always_param"])
    if want_content and SOURCES[ats]["content_param"]:
        params.append(SOURCES[ats]["content_param"])
    if params:
        url += ("&" if "?" in url else "?") + "&".join(params)
    return url


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


def slug_from_url(url: str) -> str | None:
    """First path segment of a board URL, percent-decoded.

    Slugs may contain spaces, e.g. .../A1%20Garage%20Door%20Service/... -> that name.
    """
    path = urllib.parse.urlparse(url).path
    first = path.strip("/").split("/")[0]
    return urllib.parse.unquote(first) or None


def _add(seen: dict[str, str], url: str) -> None:
    """Record the first path segment of a board URL, deduping case-insensitively."""
    slug = slug_from_url(url)
    if slug:
        seen.setdefault(slug.lower(), slug)


class DiscoveryFailed(RuntimeError):
    """One or more discovery sources failed; the run is not complete."""


def _archive_json(url: str, *, json_lines: bool = False, **kwargs):
    """Retry malformed/truncated JSON locally, without repeating board probes."""
    for attempt in range(3):
        body = fetch(url, **kwargs)
        try:
            if json_lines:
                return [json.loads(line) for line in body.decode("utf-8").splitlines() if line.strip()]
            return json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)


def _commoncrawl_page_finished(error: urllib.error.HTTPError, page: int) -> bool:
    # Generic 400s are not success. Match pywb's exact pagination response only
    # after a successful page and with the expected final page number.
    if error.code != 400 or page < 1:
        return False
    try:
        if (error.headers or {}).get("content-encoding") == "gzip":
            with gzip.GzipFile(fileobj=error) as stream:
                body = stream.read(4097)
        else:
            body = error.read(4097)
        if len(body) > 4096:
            return False
        value = json.loads(body)
    except (ValueError, UnicodeError, OSError):
        return False
    if not isinstance(value, dict):
        return False
    expected = f"Page {page} invalid: First Page is 0, Last Page is {page - 1}"
    return value.get("message") == expected or value.get("error") == expected


def candidates_from_wayback(domains: list[str], since_days: int | None = None) -> dict[str, str]:
    """The Internet Archive's CDX index. Broader than Common Crawl and far more
    reliable — it is the default for that reason.

    `since_days` adds CDX's `from=` filter, which is what makes a daily refresh
    affordable: the last 30 days of Ashby captures is ~6,200 URLs against 191,117
    for the full crawl.
    """
    window = ""
    if since_days is not None:
        start = datetime.now(timezone.utc) - timedelta(days=since_days)
        window = f"&from={start:%Y%m%d}"
    seen: dict[str, str] = {}
    for domain in domains:
        scope = f"last {since_days}d of " if since_days else ""
        print(f"  querying the Wayback Machine for {scope}{domain}...", file=sys.stderr)
        rows = _archive_json(WAYBACK_CDX.format(domain=domain) + window, timeout=300, retries=3)
        for row in rows[1:]:  # first row is the header
            _add(seen, row[0])
        print(f"    {len(rows) - 1} archived URLs -> {len(seen)} candidates so far",
              file=sys.stderr)
    return seen


def candidates_from_urlscan(domains: list[str]) -> dict[str, str]:
    """urlscan.io's public scan corpus.

    The Internet Archive is thorough but slow to notice a new board — a median of
    48 days between a board's first posting and its first capture. urlscan indexes
    scans people ran today, so it surfaces boards the archive has not reached yet.
    Sampled once, it found 14 live boards a full Wayback crawl had missed.

    Anonymous use is capped at 30 searches/minute per IP; this makes one per domain.
    A failure here is not fatal — Wayback remains the primary source.
    """
    seen: dict[str, str] = {}
    for domain in domains:
        url = URLSCAN_SEARCH.format(domain=urllib.parse.quote(domain))
        try:
            results = json.loads(fetch(url, timeout=60, retries=2)).get("results", [])
        except Exception as e:
            print(f"  urlscan failed for {domain} ({e}); skipping", file=sys.stderr)
            continue
        for row in results:
            _add(seen, row.get("page", {}).get("url", ""))
        print(f"  urlscan {domain}: {len(results)} scans -> {len(seen)} candidates so far",
              file=sys.stderr)
    return seen


def candidates_from_commoncrawl(domains: list[str], max_pages: int = 20) -> dict[str, str]:
    """Common Crawl's CDX index. Kept as a fallback: narrower coverage, and it
    sheds requests under load often enough to fail for hours at a time."""
    collections = _archive_json(COLLINFO)
    cdx = collections[0]["cdx-api"]
    print(f"  querying Common Crawl index {collections[0]['id']}...", file=sys.stderr)
    seen: dict[str, str] = {}
    for domain in domains:
        query = f"{cdx}?url={urllib.parse.quote(domain)}%2F*&output=json&fl=url"
        # Walk pages until empty or a verified out-of-range response rather than asking
        # showNumPages first — that query is the most expensive one CDX offers and
        # times out far more often than the pages themselves.
        for page in range(max_pages):
            if page:
                time.sleep(1)  # Common Crawl asks for max 1 CDX request/second.
            try:
                rows = _archive_json(f"{query}&page={page}", json_lines=True, timeout=120, retries=6)
            except NotFound:
                break
            except urllib.error.HTTPError as exc:
                if _commoncrawl_page_finished(exc, page):
                    break
                raise
            if not rows:
                break
            for row in rows:
                _add(seen, row["url"])
    return seen


def plausible(slug: str, ats: str = "ashby") -> bool:
    """Cheap shape filter, so validation probes thousands of URLs and not millions.

    Archived URLs include tracking blobs, compensation strings and JS fragments as
    "path segments". Every live slug observed is alphanumeric plus space, dot,
    underscore or hyphen; `root.<uuid>` is Ashby's internal embed path, never a board.
    """
    lower = slug.lower()
    return (
        bool(_SLUG_SHAPE.match(slug))
        and lower not in _SLUG_JUNK
        and not lower.startswith(SOURCES.get(ats, {}).get("junk_prefixes", ()))
        and not re.fullmatch(r"[0-9a-f-]{30,}", lower)
    )


def board_exists(ats: str, slug: str) -> bool:
    """HEAD the posting API: 200 for a real board, 404 otherwise.

    HEAD returns the status with a zero-length body, so validating thousands of
    candidates costs nothing. A GET would download hundreds of kilobytes per live
    board — gigabytes just to learn which slugs are real.
    """
    return validate_board(ats, slug)["outcome"] == "active"


def validate_board(ats: str, slug: str) -> dict:
    """HEAD one candidate and preserve enough detail for recurring discovery.

    Discovery used to flatten both a definitive 404 and a transient timeout to False.
    That is acceptable for a disposable list, but not for a durable registry: only a
    404 proves that a previously-known board is inactive. Every other failure is an
    observation to retry while the board's existing status remains unchanged.
    """
    try:
        fetch(board_url(ats, slug), timeout=25, retries=2, method="HEAD")
        return {"outcome": "active", "http_status": 200, "error": ""}
    except NotFound:
        return {"outcome": "inactive", "http_status": 404, "error": ""}
    except urllib.error.HTTPError as e:
        return {"outcome": "error", "http_status": e.code, "error": str(e)}
    except Exception as e:
        return {"outcome": "error", "http_status": None, "error": str(e)}


def discover_boards(
    ats: str,
    concurrency: int = 8,
    recent_days: int | None = None,
    observations: list[dict] | None = None,
) -> list[str]:
    """Find board slugs for one ATS: harvest candidates, then validate each.

    `recent_days` switches to the cheap mode: only archive captures from that window,
    plus urlscan.io, which indexes scans run today rather than waiting on the
    archive's ~48-day median capture lag. Measured at ~4 minutes against ~26 for the
    full crawl, and purely additive: one run added 14 boards and lost none.
    """
    domains = SOURCES[ats]["domains"]
    print(f"{ats}: discovering boards", file=sys.stderr)
    primary_source = "wayback"
    try:
        seen = candidates_from_wayback(domains, since_days=recent_days)
    except Exception as e:
        print(f"  Wayback failed ({e}); falling back to Common Crawl", file=sys.stderr)
        seen = candidates_from_commoncrawl(domains)
        primary_source = "commoncrawl"
    sources = {key: {primary_source} for key in seen}
    if recent_days is not None:
        # Additive: urlscan finds boards the archive has not reached, and a failure
        # there must not lose the Wayback results already gathered.
        for key, value in candidates_from_urlscan(domains).items():
            seen.setdefault(key, value)
            sources.setdefault(key, set()).add("urlscan")

    candidates = sorted((s for s in seen.values() if plausible(s, ats)), key=str.lower)
    candidate_map = {s.lower(): s for s in candidates}
    print(f"  validating {len(candidates)} plausible slugs...", file=sys.stderr)
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        results = list(pool.map(lambda x: validate_board(ats, x), candidates))
    outcomes = {s.lower(): result for s, result in zip(candidates, results)}
    live = [s for s in candidates if outcomes[s.lower()]["outcome"] == "active"]
    dead = sum(r["outcome"] == "inactive" for r in results)
    errors = sum(r["outcome"] == "error" for r in results)
    print(f"  {len(live)} live boards ({dead} dead, {errors} error)", file=sys.stderr)

    # Discovery only sees what the archive captured, so a real board that was never
    # crawled is invisible to it. Union in every slug already known-good rather than
    # letting a refresh lose boards an earlier run had.
    known = {s.lower(): s for s in live}
    for path, source_name in ((BOARDS_SEED, "seed"), (BOARDS_CACHE, "cache")):
        for slug in _read_boards(path).get(ats, []):
            key = slug.lower()
            sources.setdefault(key, set()).add(source_name)
            # A confirmed 404 is the one result allowed to remove an existing board.
            # Transient errors preserve it so a flaky refresh cannot lose companies.
            if outcomes.get(key, {}).get("outcome") != "inactive":
                known.setdefault(key, slug)
    if len(known) > len(live):
        print(f"  +{len(known) - len(live)} from seed/previous runs", file=sys.stderr)

    if observations is not None:
        for key, slug in sorted(
            {**candidate_map, **known}.items(), key=lambda item: item[1].lower()
        ):
            result = outcomes.get(key, {
                "outcome": "unverified", "http_status": None, "error": ""
            })
            observations.append({
                "ats": ats,
                "slug": slug,
                "sources": sorted(sources.get(key, ())),
                **result,
            })
    return sorted(known.values(), key=str.lower)


def _read_boards(path: Path) -> dict[str, list[str]]:
    """Read a board file, accepting the pre-multi-ATS flat list as Ashby."""
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    return {"ashby": data} if isinstance(data, list) else data


RECENT_WINDOW_DAYS = 30


def load_boards(
    refresh: bool,
    ats_list: list[str],
    concurrency: int = 8,
    recent: bool = False,
    observations: list[dict] | None = None,
    discover_if_empty: bool = True,
    write_cache: bool = True,
) -> dict[str, list[str]]:
    if not refresh and not recent:
        # Merge per platform rather than taking the first file that has anything.
        # The cache may cover only the platforms the last refresh ran, and picking
        # it wholesale would silently return zero boards for all the others.
        merged: dict[str, dict[str, str]] = {}
        source_sets: dict[tuple[str, str], set[str]] = {}
        for path, source_name in ((BOARDS_SEED, "seed"), (BOARDS_CACHE, "cache")):
            for ats, slugs in _read_boards(path).items():
                for slug in slugs:
                    key = slug.lower()
                    # Cache casing wins, but a partial cache cannot erase seed entries.
                    merged.setdefault(ats, {})[key] = slug
                    source_sets.setdefault((ats, key), set()).add(source_name)
        got = {
            ats: sorted(merged.get(ats, {}).values(), key=str.lower)
            for ats in ats_list
        }
        if any(got.values()) or not discover_if_empty:
            summary = ", ".join(f"{a} {len(v)}" for a, v in got.items())
            print(f"{sum(len(v) for v in got.values())} boards ({summary})",
                  file=sys.stderr)
            for ats, slugs in got.items():
                if not slugs:
                    print(f"  note: no {ats} boards cached; run --refresh-boards",
                          file=sys.stderr)
                elif observations is not None:
                    observations.extend({
                        "ats": ats,
                        "slug": slug,
                        "sources": sorted(source_sets.get((ats, slug.lower()), ())),
                        "outcome": "unverified",
                        "http_status": None,
                        "error": "",
                    } for slug in slugs)
            return got
    boards = _read_boards(BOARDS_CACHE)
    failures = []
    for ats in ats_list:
        try:
            boards[ats] = discover_boards(
                ats,
                concurrency,
                recent_days=RECENT_WINDOW_DAYS if recent else None,
                observations=observations,
            )
        except RateLimited:
            sys.exit(
                "Common Crawl returned 503: request rate too high. Their docs say to "
                "slow down, and that a repeatedly-abusive IP can be blocked for 24 "
                "hours. Wait before retrying."
            )
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, UnicodeError) as e:
            failure = f"{ats}: {type(e).__name__}: {e}"
            failures.append(failure)
            print(f"  discovery incomplete for {failure}; continuing other platforms", file=sys.stderr)
    if failures:
        raise DiscoveryFailed("board discovery incomplete: " + "; ".join(failures))
    total = sum(len(boards.get(a, [])) for a in ats_list)
    if write_cache:
        BOARDS_CACHE.write_text(json.dumps(boards, indent=2))
        print(f"cached {total} slugs -> {BOARDS_CACHE.name}", file=sys.stderr)
    return {a: boards.get(a, []) for a in ats_list}


# --------------------------------------------------------------------------- #
# Filtering
# --------------------------------------------------------------------------- #


_DURATION = re.compile(r"^(\d+)\s*([dwmy]?)$", re.IGNORECASE)
_DURATION_DAYS = {"d": 1, "w": 7, "m": 30, "y": 365, "": 1}


def parse_duration(text: str) -> int:
    """'7d' / '2w' / '3m' / '1y' / '7' -> days. Raises ValueError on anything else."""
    m = _DURATION.match(text.strip())
    if not m:
        raise ValueError(f"expected something like 7d, 2w, 3m or 90; got {text!r}")
    return int(m.group(1)) * _DURATION_DAYS[m.group(2).lower()]


def published_within(published_at: str, cutoff: datetime) -> bool:
    """Is this posting newer than the cutoff?

    An unparseable or missing date counts as too old. Every one of 308,100 rows
    measured had a usable date, so this only guards against a future API change —
    and excluding is the safe direction, since --since exists to promise freshness.
    """
    if not published_at:
        return False
    try:
        return datetime.fromisoformat(published_at.replace("Z", "+00:00")) >= cutoff
    except ValueError:
        return False


def matches(job_title: str, wanted: str, mode: str = "fuzzy") -> bool:
    """Does a posting's title match what the user asked for? Case-insensitive.

    exact  — the whole title equals the query.
             "software engineer" matches "Software Engineer" only.
    fuzzy  — either string contains the other, so it works in both directions:
             a short query finds longer titles ("software engineer" ->
             "Senior Software Engineer, Backend") and a long query still finds
             the short title it contains ("senior software engineer, backend"
             -> "Software Engineer").

             The reverse direction requires the title to be at least two words.
             Without that, querying "senior software engineer" also matches jobs
             titled just "Engineer", "Software", or "Senior" — every one-word
             title that happens to appear in the query.
    """
    title, want = job_title.lower().strip(), wanted.lower().strip()
    if not title or not want:
        return False  # an empty query would otherwise match every job
    if mode == "exact":
        return title == want
    return want in title or (len(title.split()) >= 2 and title in want)


def fragments(text: str, pattern: re.Pattern[str], limit: int = 2) -> list[str]:
    """Windows of surrounding text for each match, so a hit can be judged in context."""
    found: list[str] = []
    for match in pattern.finditer(text):
        window = text[max(0, match.start() - 90) : match.end() + 150].strip()
        if window not in found:
            found.append(window)
        if len(found) == limit:
            break
    return found


def scan_board(
    ats: str,
    slug: str,
    wanted: str | None,
    remote_only: bool,
    mode: str,
    pattern: re.Pattern[str] | None = None,
    cutoff: datetime | None = None,
    etag: str | None = None,
    meta: dict | None = None,
    enrichment_sink: list[dict] | None = None,
) -> list[dict]:
    """Fetch one board, return flat rows for matching listed jobs.

    Every posting scan requests descriptions, including Greenhouse's larger
    content=true representation. Descriptions are normalized to plain text and
    retained in the output row. --grep matches against the title and description;
    see the loop below for why they are searched apart.
    """
    source = SOURCES[ats]
    payload = json.loads(
        fetch(board_url(ats, slug, want_content=True), etag=etag, meta=meta)
    )
    jobs = source["jobs"](payload)
    if not isinstance(jobs, list):
        # Fail loudly on a shape change rather than silently reporting no results.
        raise ValueError(f"{ats}/{slug}: response has no jobs array")

    rows = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        norm = source["normalize"](job)
        if norm is None:
            continue
        norm = _clean(norm)
        raw_description = norm["description"]
        norm["description"] = plain_text(norm["description"])
        if cutoff is not None and not published_within(norm["publishedAt"], cutoff):
            continue
        if wanted and not matches(norm["title"], wanted, mode):
            continue
        if remote_only and not norm["isRemote"]:
            continue

        hits: list[str] = []
        if pattern is not None:
            # The title is searched too. Searching only the description dropped
            # postings whose subject is *in the title* — "SSO Integrations Lead",
            # "Identity Platform Engineer" — whenever the body happened to phrase
            # it differently. Searched separately rather than concatenated, or a
            # regex could match across the seam and report a hit in neither field.
            hits = fragments(plain_text(norm["title"]), pattern, limit=1)
            for window in fragments(norm["description"], pattern):
                if window not in hits:
                    hits.append(window)
            if not hits:
                continue

        if enrichment_sink is not None and norm.get("id"):
            # Parse while the un-normalized ATS payload is still available. Ashby
            # compensation and Lever salaryRange would otherwise be discarded by
            # the shared CSV/job model, and Greenhouse pay-block structure would be
            # flattened out of its HTML description.
            enrichment_sink.append(enrich_job(
                ats, norm["id"], raw_description, job
            ))
        rows.append({"ats": ats, "company": slug, **norm, "matched": " … ".join(hits)})
    return rows


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #

_BOARD_REGISTRY_SCHEMA = """
CREATE TABLE IF NOT EXISTS discovery_runs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    mode             TEXT NOT NULL,
    ats              TEXT NOT NULL,
    started_at       TEXT NOT NULL,
    finished_at      TEXT,
    status           TEXT NOT NULL,
    candidate_count  INTEGER NOT NULL DEFAULT 0,
    live_count       INTEGER NOT NULL DEFAULT 0,
    not_found_count  INTEGER NOT NULL DEFAULT 0,
    error_count      INTEGER NOT NULL DEFAULT 0,
    error            TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS ats_boards (
    ats                  TEXT NOT NULL,
    slug                 TEXT NOT NULL COLLATE NOCASE,
    api_url              TEXT NOT NULL,
    status               TEXT NOT NULL,
    first_discovered_at  TEXT NOT NULL,
    last_discovered_at   TEXT NOT NULL,
    last_validation_at   TEXT,
    last_active_at       TEXT,
    inactivated_at       TEXT,
    last_http_status     INTEGER,
    last_error           TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (ats, slug)
);

CREATE INDEX IF NOT EXISTS ats_boards_status ON ats_boards(status);

CREATE TABLE IF NOT EXISTS discovery_observations (
    run_id       INTEGER NOT NULL,
    ats          TEXT NOT NULL,
    slug         TEXT NOT NULL,
    sources      TEXT NOT NULL,
    outcome      TEXT NOT NULL,
    http_status  INTEGER,
    error        TEXT NOT NULL DEFAULT '',
    observed_at  TEXT NOT NULL,
    PRIMARY KEY (run_id, ats, slug),
    FOREIGN KEY (run_id) REFERENCES discovery_runs(id)
);
"""


def _prepare_board_registry(con: sqlite3.Connection) -> None:
    """Create the additive board registry schema without disturbing job tables."""
    con.executescript(_BOARD_REGISTRY_SCHEMA)


def start_discovery_run(
    db_path: Path, mode: str, ats_list: list[str], started_at: str
) -> int:
    """Open an auditable discovery run before any network work begins."""
    with sqlite3.connect(db_path) as con:
        _prepare_board_registry(con)
        cur = con.execute(
            "INSERT INTO discovery_runs (mode, ats, started_at, status) VALUES (?,?,?,?)",
            (mode, json.dumps(ats_list), started_at, "running"),
        )
        return int(cur.lastrowid)


def finish_discovery_run(
    db_path: Path,
    run_id: int,
    observations: list[dict],
    finished_at: str,
    status: str = "completed",
    error: str = "",
) -> dict[str, int]:
    """Persist observations and update current state without unsafe downgrades."""
    counts = {
        "candidate": len(observations),
        "active": sum(o["outcome"] == "active" for o in observations),
        "inactive": sum(o["outcome"] == "inactive" for o in observations),
        "error": sum(o["outcome"] == "error" for o in observations),
    }
    with sqlite3.connect(db_path) as con:
        _prepare_board_registry(con)
        con.executemany(
            "INSERT OR REPLACE INTO discovery_observations "
            "(run_id, ats, slug, sources, outcome, http_status, error, observed_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [
                (
                    run_id,
                    o["ats"],
                    o["slug"],
                    json.dumps(o.get("sources", [])),
                    o["outcome"],
                    o.get("http_status"),
                    o.get("error", ""),
                    finished_at,
                )
                for o in observations
            ],
        )

        for o in observations:
            ats, slug, outcome = o["ats"], o["slug"], o["outcome"]
            api_url = board_url(ats, slug)
            http_status = o.get("http_status")
            last_error = o.get("error", "")
            known_input = bool({"seed", "cache"} & set(o.get("sources", [])))

            if outcome == "active":
                con.execute(
                    "INSERT INTO ats_boards "
                    "(ats, slug, api_url, status, first_discovered_at, "
                    "last_discovered_at, last_validation_at, last_active_at, "
                    "inactivated_at, last_http_status, last_error) "
                    "VALUES (?,?,?,'active',?,?,?,?,NULL,?,'') "
                    "ON CONFLICT(ats, slug) DO UPDATE SET "
                    "api_url=excluded.api_url, status='active', "
                    "last_discovered_at=excluded.last_discovered_at, "
                    "last_validation_at=excluded.last_validation_at, "
                    "last_active_at=excluded.last_active_at, inactivated_at=NULL, "
                    "last_http_status=excluded.last_http_status, last_error=''",
                    (ats, slug, api_url, finished_at, finished_at, finished_at,
                     finished_at, http_status),
                )
            elif outcome == "inactive":
                # Never-valid archive noise belongs in observations, not the board
                # registry. Only an existing board can transition inactive.
                con.execute(
                    "UPDATE ats_boards SET status='inactive', api_url=?, "
                    "last_discovered_at=?, last_validation_at=?, "
                    "inactivated_at=COALESCE(inactivated_at, ?), "
                    "last_http_status=?, last_error='' WHERE ats=? AND slug=?",
                    (api_url, finished_at, finished_at, finished_at, http_status, ats, slug),
                )
            elif outcome == "unverified" or known_input:
                # On conflict, retain active/inactive: importing a cache is not new
                # evidence about whether a board currently exists.
                con.execute(
                    "INSERT INTO ats_boards "
                    "(ats, slug, api_url, status, first_discovered_at, "
                    "last_discovered_at, last_validation_at, last_http_status, last_error) "
                    "VALUES (?,?,?,'unverified',?,?,?,?,?) "
                    "ON CONFLICT(ats, slug) DO UPDATE SET "
                    "api_url=excluded.api_url, "
                    "last_discovered_at=excluded.last_discovered_at, "
                    "last_validation_at=CASE WHEN excluded.last_validation_at IS NOT NULL "
                    "THEN excluded.last_validation_at ELSE ats_boards.last_validation_at END, "
                    "last_http_status=CASE WHEN excluded.last_validation_at IS NOT NULL "
                    "THEN excluded.last_http_status ELSE ats_boards.last_http_status END, "
                    "last_error=CASE WHEN excluded.last_validation_at IS NOT NULL "
                    "THEN excluded.last_error ELSE ats_boards.last_error END",
                    (
                        ats,
                        slug,
                        api_url,
                        finished_at,
                        finished_at,
                        finished_at if outcome == "error" else None,
                        http_status,
                        last_error,
                    ),
                )
            elif outcome == "error":
                # Unknown transient candidates stay in run history only. Existing
                # boards retain status but record the failed validation attempt.
                con.execute(
                    "UPDATE ats_boards SET api_url=?, last_discovered_at=?, "
                    "last_validation_at=?, last_http_status=?, last_error=? "
                    "WHERE ats=? AND slug=?",
                    (api_url, finished_at, finished_at, http_status, last_error, ats, slug),
                )

        con.execute(
            "UPDATE discovery_runs SET finished_at=?, status=?, candidate_count=?, "
            "live_count=?, not_found_count=?, error_count=?, error=? WHERE id=?",
            (
                finished_at,
                status,
                counts["candidate"],
                counts["active"],
                counts["inactive"],
                counts["error"],
                error,
                run_id,
            ),
        )
    return counts


def boards_from_registry(db_path: Path, ats_list: list[str]) -> dict[str, list[str]]:
    """Active and not-yet-verified boards, in boards.json's existing shape."""
    if not db_path.exists():
        return {ats: [] for ats in ats_list}
    with sqlite3.connect(db_path) as con:
        if not con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='ats_boards'"
        ).fetchone():
            return {ats: [] for ats in ats_list}
        return {
            ats: [r[0] for r in con.execute(
                "SELECT slug FROM ats_boards WHERE ats=? "
                "AND status IN ('active','unverified') ORDER BY slug COLLATE NOCASE",
                (ats,),
            )]
            for ats in ats_list
        }


def export_boards_from_registry(db_path: Path, ats_list: list[str]) -> dict[str, list[str]]:
    """Replace selected platforms in the cache without erasing unselected ones."""
    boards = _read_boards(BOARDS_CACHE)
    boards.update(boards_from_registry(db_path, ats_list))
    BOARDS_CACHE.write_text(json.dumps(boards, indent=2))
    return boards


def mark_registered_boards_inactive(
    db_path: Path, boards: set[tuple[str, str]], seen_at: str
) -> None:
    """Carry definitive posting-API 404s back into the durable registry."""
    if not boards or not db_path.exists():
        return
    with sqlite3.connect(db_path) as con:
        _prepare_board_registry(con)
        con.executemany(
            "UPDATE ats_boards SET status='inactive', last_validation_at=?, "
            "inactivated_at=COALESCE(inactivated_at, ?), last_http_status=404, "
            "last_error='' WHERE ats=? AND slug=?",
            [(seen_at, seen_at, ats, slug) for ats, slug in boards],
        )


_INDEXES = """
CREATE INDEX IF NOT EXISTS jobs_company ON jobs(ats, company);
CREATE INDEX IF NOT EXISTS jobs_last_seen ON jobs(last_seen);
CREATE INDEX IF NOT EXISTS jobs_closed_at ON jobs(closed_at);
"""


def _create_table(con: sqlite3.Connection, name: str = "jobs") -> None:
    # (ats, id) rather than id alone: Greenhouse ids are integers while Ashby and
    # Lever use UUIDs, so a bare id risks a collision that would silently overwrite
    # one platform's posting with another's.
    body = "".join(
        "description TEXT NOT NULL DEFAULT '',"
        if f == "description" else f"{f} TEXT,"
        for f in FIELDS if f not in ("ats", "id")
    )
    con.executescript(f"""
        CREATE TABLE IF NOT EXISTS {name} (
            ats         TEXT NOT NULL,
            id          TEXT NOT NULL,
            {body}
            first_seen  TEXT NOT NULL,
            last_seen   TEXT NOT NULL,
            closed_at   TEXT,
            PRIMARY KEY (ats, id)
        );
    """)


def _prepare(con: sqlite3.Connection) -> None:
    """Create the table, migrate an older one, then index — in that order.

    Indexes come last because an index on a column the migration has not added yet
    cannot be created; putting them in the same script as CREATE TABLE is what broke
    the previous migration.
    """
    cols = {c[1] for c in con.execute("PRAGMA table_info(jobs)")}
    if cols and "ats" not in cols:
        # Pre-multi-ATS database. The primary key is changing, which ALTER TABLE
        # cannot do, so rebuild and label every existing row as Ashby.
        if "closed_at" not in cols:
            con.execute("ALTER TABLE jobs ADD COLUMN closed_at TEXT")
            cols.add("closed_at")
        carried = [
            c for c in (*FIELDS, "first_seen", "last_seen", "closed_at")
            if c in cols and c != "ats"
        ]
        selected = [
            "COALESCE(description, '')" if c == "description" else c
            for c in carried
        ]
        _create_table(con, "jobs_new")
        con.execute(
            f"INSERT INTO jobs_new (ats, {','.join(carried)}) "
            f"SELECT 'ashby', {','.join(selected)} FROM jobs"
        )
        con.execute("DROP TABLE jobs")
        con.execute("ALTER TABLE jobs_new RENAME TO jobs")
    else:
        _create_table(con)
        if cols:
            # Additive field migrations preserve accumulated history. Description
            # arrived after the initial multi-ATS schema, so old rows receive an
            # empty value and are filled the next time their board is fetched.
            for field in FIELDS:
                if field not in cols and field not in ("ats", "id"):
                    if field == "description":
                        con.execute(
                            "ALTER TABLE jobs ADD COLUMN description "
                            "TEXT NOT NULL DEFAULT ''"
                        )
                    else:
                        con.execute(f"ALTER TABLE jobs ADD COLUMN {field} TEXT")
                    cols.add(field)
            if "closed_at" not in cols:
                con.execute("ALTER TABLE jobs ADD COLUMN closed_at TEXT")
    con.executescript(_INDEXES)
    from .history import prepare_history
    prepare_history(con)


def sort_rows(rows: list[dict], mode: str) -> None:
    """Order rows in place. `recent` puts the newest posting first.

    Comparing the ISO strings is correct without parsing, because every adapter
    normalises to ISO — including Lever's epoch milliseconds. Two passes rather than
    one compound key: Python's sort is stable, so sorting by board first and then by
    date gives newest-first with a deterministic order inside each timestamp. An
    empty date is the smallest string, so reversing puts undated rows last.
    """
    rows.sort(key=lambda r: (r["ats"], str(r["company"]).lower(), str(r["title"]).lower()))
    if mode == "recent":
        rows.sort(key=lambda r: str(r["publishedAt"]), reverse=True)


def may_close_postings(
    title: str | None,
    pattern: re.Pattern[str] | None,
    cutoff: datetime | None,
    new_only: bool,
) -> bool:
    """Did this run see every posting on the boards it scanned?

    Only such a run may stamp closed_at. Every filter has to be listed here: a run
    that skipped old postings, or ones it had seen before, did not observe them and
    cannot conclude they are gone. Miss one and, for example, `--all --since 7d`
    would mark every posting older than a week as closed.
    """
    return not (title or pattern or cutoff or new_only)


_ETAG_SCHEMA = """
CREATE TABLE IF NOT EXISTS board_etag (
    ats                   TEXT NOT NULL,
    company               TEXT NOT NULL,
    etag                  TEXT NOT NULL,
    seen_at               TEXT NOT NULL,
    includes_descriptions INTEGER NOT NULL DEFAULT 1,
    representation_version INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (ats, company)
);
"""


def _prepare_etags(con: sqlite3.Connection) -> None:
    """Create the ETag cache and distrust pre-description representations."""
    con.executescript(_ETAG_SCHEMA)
    cols = {c[1] for c in con.execute("PRAGMA table_info(board_etag)")}
    if "includes_descriptions" not in cols:
        # Old ETags came from Greenhouse's small, descriptionless URL. Keeping them
        # eligible could 304 a fresh content=true request before descriptions have
        # ever been persisted.
        con.execute(
            "ALTER TABLE board_etag ADD COLUMN includes_descriptions "
            "INTEGER NOT NULL DEFAULT 0"
        )
    if "representation_version" not in cols:
        # Version 1 is the old response shape. Ashby's compensation-bearing URL
        # uses version 2, so an old ETag cannot 304 the first enriched request.
        con.execute(
            "ALTER TABLE board_etag ADD COLUMN representation_version "
            "INTEGER NOT NULL DEFAULT 1"
        )


# Require a fresh representation once to populate the original description layout.
_ETAG_REPRESENTATION = {"ashby": 3, "greenhouse": 2, "lever": 2}


def may_use_etags(
    title: str | None,
    pattern: re.Pattern[str] | None,
    cutoff: datetime | None,
    remote_only: bool,
    new_only: bool,
) -> bool:
    """Is a 304 safe to treat as "nothing new on this board"?

    Only for a run that is unfiltered apart from --new-only. A 304 says the body is
    unchanged since the stored etag; concluding "no new postings" from that also
    requires that the fetch which stored the etag actually persisted every posting.
    A --title run stores rows for matching postings only, so trusting its etag later
    would skip a board whose non-matching postings were never recorded.

    Storing and using etags are gated on the same predicate, so an etag in the
    database always came from a full, persisted fetch.
    """
    return new_only and not (title or pattern or cutoff or remote_only)


def load_etags(db_path: Path) -> dict[tuple[str, str], str]:
    """Stored etags, keyed by board. Empty if the table does not exist yet."""
    if not db_path.exists():
        return {}
    with sqlite3.connect(db_path) as con:
        _prepare_etags(con)
        return {(a, c): e for a, c, e, version in con.execute(
            "SELECT ats, company, etag, representation_version FROM board_etag "
            "WHERE includes_descriptions=1")
            if version == _ETAG_REPRESENTATION.get(a, 1)}


def save_etags(db_path: Path, etags: dict[tuple[str, str], str], seen_at: str) -> None:
    if not etags:
        return
    with sqlite3.connect(db_path) as con:
        _prepare_etags(con)
        con.executemany(
            "INSERT INTO board_etag "
            "(ats, company, etag, seen_at, includes_descriptions, representation_version) "
            "VALUES (?,?,?,?,1,?) "
            "ON CONFLICT(ats, company) DO UPDATE SET etag=excluded.etag, "
            "seen_at=excluded.seen_at, includes_descriptions=1, "
            "representation_version=excluded.representation_version",
            [
                (a, c, e, seen_at, _ETAG_REPRESENTATION.get(a, 1))
                for (a, c), e in etags.items()
            ],
        )


def known_keys(db_path: Path) -> set[tuple[str, str]]:
    """The (ats, id) pairs already recorded. Empty set if the database is new."""
    if not db_path.exists():
        return set()
    with sqlite3.connect(db_path) as con:
        if not con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='jobs'"
        ).fetchone():
            return set()
        cols = {c[1] for c in con.execute("PRAGMA table_info(jobs)")}
        if "ats" not in cols:  # pre-multi-ATS database; everything in it is Ashby
            return {("ashby", r[0]) for r in con.execute("SELECT id FROM jobs")}
        return {tuple(r) for r in con.execute("SELECT ats, id FROM jobs")}


def save(
    rows: list[dict],
    db_path: Path,
    seen_at: str,
    covered: list[tuple[str, str]] | None = None,
    enrichments: list[dict] | None = None,
) -> tuple[int, int, int]:
    """Upsert rows keyed on (ats, posting id). Returns (new, updated, closed).

    first_seen is preserved across runs and last_seen is refreshed, which is the
    whole reason to keep a database rather than just the CSV: it answers "when did
    this posting appear" and "is it still up" across scrapes.

    `covered` is the list of (ats, board) pairs this run scanned exhaustively, and is
    only passed for an unfiltered run. On a filtered run a missing job is ambiguous —
    it may be gone, or it may simply not have matched --title — so only an unfiltered
    run has the standing to close a posting. Anything on a covered board that this run
    did not see is stamped closed_at; anything that reappears has it cleared.
    """
    keyed = {(r["ats"], r["id"]): r for r in rows if r.get("id")}
    cols = ["ats", "id", *[f for f in FIELDS if f not in ("ats", "id")]]
    with sqlite3.connect(db_path) as con:
        _prepare(con)
        known = {tuple(r) for r in con.execute("SELECT ats, id FROM jobs")}
        con.executemany(
            f"INSERT INTO jobs ({','.join(cols)}, first_seen, last_seen) "
            f"VALUES ({','.join('?' * len(cols))}, ?, ?) "
            "ON CONFLICT(ats, id) DO UPDATE SET "
            # Everything except first_seen is refreshed; titles and locations do
            # get edited in place on live postings. `matched` is the exception: it
            # belongs to whichever --grep produced it, so a later title-only run
            # must not blank out context an earlier search found.
            + ",".join(
                (f"{c}=COALESCE(NULLIF(excluded.{c},''),jobs.{c})"
                 if c in ("posted_at", "source_updated_at") else f"{c}=excluded.{c}")
                for c in cols if c not in ("ats", "id", "matched")
            )
            + ", matched=CASE WHEN excluded.matched != '' "
            "THEN excluded.matched ELSE jobs.matched END"
            ", last_seen=excluded.last_seen",
            [
                [str(r.get(c, "")) for c in cols] + [seen_at, seen_at]
                for r in keyed.values()
            ],
        )
        if enrichments is not None:
            # The workers only construct dictionaries. Jobs and their enrichment
            # are written together here on the main thread/transaction, avoiding
            # SQLite write contention and orphaned enrichment rows.
            sync_scan_enrichments(con, enrichments, keyed.keys() - known, seen_at)
            save_enrichments(con, enrichments, seen_at)
        closed = 0
        if covered is not None:
            con.execute(
                "CREATE TEMP TABLE scanned (ats TEXT, company TEXT, "
                "PRIMARY KEY (ats, company))"
            )
            con.executemany("INSERT OR IGNORE INTO scanned VALUES (?, ?)", covered)
            cur = con.execute(
                "UPDATE jobs SET closed_at = ? "
                "WHERE closed_at IS NULL AND last_seen < ? "
                "AND (ats, company) IN (SELECT ats, company FROM scanned)",
                (seen_at, seen_at),
            )
            closed = cur.rowcount
            # A posting that came back is open again.
            con.execute(
                "UPDATE jobs SET closed_at = NULL "
                "WHERE closed_at IS NOT NULL AND last_seen = ?",
                (seen_at,),
            )

    new = len(keyed.keys() - known)
    return new, len(keyed) - new, closed


def run_recorded_discovery(
    db_path: Path,
    mode: str,
    ats_list: list[str],
    refresh: bool,
    recent: bool,
    concurrency: int,
) -> tuple[dict[str, list[str]], int, dict[str, int]]:
    """Run discovery/import with durable run state, then export the selected boards."""
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    run_id = start_discovery_run(db_path, mode, ats_list, started_at)
    observations: list[dict] = []
    try:
        load_boards(
            refresh,
            ats_list,
            concurrency,
            recent=recent,
            observations=observations,
            discover_if_empty=False,
            write_cache=False,
        )
    except BaseException as e:
        finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        finish_discovery_run(
            db_path, run_id, observations, finished_at, status="failed", error=str(e)
        )
        if isinstance(e, DiscoveryFailed):
            # Preserve verified progress and known boards without claiming success.
            export_boards_from_registry(db_path, ats_list)
        raise

    finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    counts = finish_discovery_run(db_path, run_id, observations, finished_at)
    exported = export_boards_from_registry(db_path, ats_list)
    return {ats: exported.get(ats, []) for ats in ats_list}, run_id, counts


def print_discovery_summary(
    db_path: Path,
    boards: dict[str, list[str]],
    run_id: int,
    counts: dict[str, int],
) -> None:
    exported_count = sum(len(slugs) for slugs in boards.values())
    print(
        f"discovery run {run_id} complete: {counts['candidate']} candidates | "
        f"{counts['active']} active | {counts['inactive']} 404 | "
        f"{counts['error']} error | {exported_count} boards -> "
        f"{db_path.name}, {BOARDS_CACHE.name}",
        file=sys.stderr,
    )


# --------------------------------------------------------------------------- #


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--ats",
        default="all",
        help="comma-separated platforms: " + ", ".join(SOURCES) + " (default: all)",
    )
    p.add_argument(
        "--title",
        help="title to match (default: 'software engineer', unless --grep is given)",
    )
    p.add_argument(
        "--match",
        choices=("fuzzy", "exact"),
        default="fuzzy",
        help="fuzzy: either string contains the other (default). exact: titles must be equal",
    )
    p.add_argument(
        "--grep",
        metavar="REGEX",
        help="case-insensitive regex searched against the job title and description; "
        "matching context lands in the 'matched' column",
    )
    p.add_argument(
        "--all",
        action="store_true",
        help="every listed job on every board, no title or description filter",
    )
    p.add_argument(
        "--since",
        metavar="AGE",
        help="only postings published within this window: 7d, 2w, 3m, 1y, or a bare "
        "number of days. Across all platforms the median posting is 62 days old; "
        "--since 7d returns ~11%% of them at a median age of 4 days",
    )
    p.add_argument(
        "--new-only",
        action="store_true",
        help="only postings the database has never seen. Catches an old requisition "
        "that appeared today, which --since cannot. Requires the database",
    )
    p.add_argument(
        "--sort",
        choices=("board", "recent"),
        default="board",
        help="board: grouped by platform and company (default). recent: newest "
        "posting first, which is what you want with --since",
    )
    p.add_argument("--limit", type=int, help="max boards per platform (default: all)")
    p.add_argument("--remote", action="store_true", help="only remote postings")
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--refresh-boards", action="store_true", help="re-crawl slug lists")
    p.add_argument(
        "--refresh-recent",
        action="store_true",
        help="cheap daily discovery: urlscan.io plus the last 30 days of archive "
        "captures. ~4 minutes against the full crawl's ~26",
    )
    p.add_argument(
        "--discover-only",
        action="store_true",
        help="persist ATS boards and discovery history, export boards.json, and exit "
        "without fetching job postings",
    )
    p.add_argument("--out", default="job-boards", help="output filename prefix")
    p.add_argument(
        "--boards-from",
        metavar="FILE",
        help="scan only the boards in this file instead of the discovered list. "
        "Takes the same shape as boards.json, which is what <out>.failed.json is "
        "written in, so retrying a run's failures is --boards-from <out>.failed.json",
    )
    p.add_argument(
        "--db",
        default="job-boards.db",
        help="SQLite file accumulating jobs and board discovery (default: job-boards.db)",
    )
    p.add_argument("--no-db", action="store_true", help="skip the database write")
    p.add_argument("--no-export", action="store_true",
                   help="save jobs to SQLite without CSV/JSON row exports; keep run receipts")
    args = p.parse_args()

    ats_list = list(SOURCES) if args.ats == "all" else [
        a.strip() for a in args.ats.split(",") if a.strip()
    ]
    unknown = [a for a in ats_list if a not in SOURCES]
    if unknown:
        sys.exit(f"unknown --ats {', '.join(unknown)}; choose from {', '.join(SOURCES)}")

    if args.no_export and args.no_db:
        p.error("--no-export requires database storage; remove --no-db")

    if args.refresh_boards and args.refresh_recent:
        sys.exit("choose either --refresh-boards or --refresh-recent, not both")

    db_path = HERE / args.db if not Path(args.db).is_absolute() else Path(args.db)
    if args.discover_only:
        incompatible = []
        for used, flag in (
            (args.all, "--all"),
            (args.title is not None, "--title"),
            (args.grep is not None, "--grep"),
            (args.remote, "--remote"),
            (args.since is not None, "--since"),
            (args.new_only, "--new-only"),
            (args.boards_from is not None, "--boards-from"),
            (args.limit is not None, "--limit"),
            (args.match != "fuzzy", "--match"),
            (args.sort != "board", "--sort"),
            (args.no_db, "--no-db"),
            (args.no_export, "--no-export"),
        ):
            if used:
                incompatible.append(flag)
        if incompatible:
            sys.exit("--discover-only does not accept posting options: "
                     + ", ".join(incompatible))

        mode = "full" if args.refresh_boards else (
            "recent" if args.refresh_recent else "import"
        )
        boards, run_id, counts = run_recorded_discovery(
            db_path,
            mode,
            ats_list,
            args.refresh_boards,
            args.refresh_recent,
            args.concurrency,
        )
        print_discovery_summary(db_path, boards, run_id, counts)
        return

    if args.all and (args.title or args.grep):
        sys.exit("--all takes no filters; drop --title/--grep or drop --all")
    if args.new_only and args.no_db:
        sys.exit("--new-only compares against the database; it cannot be used with --no-db")
    cutoff = None
    if args.since:
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(days=parse_duration(args.since))
        except ValueError as e:
            sys.exit(f"--since: {e}")
    # The title default only applies when nothing else narrows the search. Applying
    # it to a --grep run would silently AND an unrequested title filter onto it.
    title = None if args.all else (
        args.title or (None if args.grep else "software engineer")
    )
    try:
        pattern = re.compile(args.grep, re.IGNORECASE) if args.grep else None
    except re.error as e:
        sys.exit(f"--grep is not a valid regex: {e}")
    if args.grep and r"\b" not in args.grep:
        # Silent and severe: `rust` matches "trust", which appears in almost every
        # description's boilerplate. Measured 1350 hits vs 72 word-bounded.
        print(
            rf"note: --grep {args.grep!r} has no \b word boundary, so it matches "
            rf"inside longer words. Consider '\b{args.grep}\b'.",
            file=sys.stderr,
        )
    if "greenhouse" in ats_list:
        print(
            "note: Greenhouse job scans include full descriptions, roughly 26x "
            "the bytes of its descriptionless response.",
            file=sys.stderr,
        )

    if args.boards_from:
        source = Path(args.boards_from)
        if not source.is_absolute():
            source = HERE / source
        boards = _read_boards(source)
        if not boards:
            sys.exit(f"--boards-from {args.boards_from}: no boards in that file")
    elif (args.refresh_boards or args.refresh_recent) and not args.no_db:
        mode = "full" if args.refresh_boards else "recent"
        boards, run_id, counts = run_recorded_discovery(
            db_path,
            mode,
            ats_list,
            args.refresh_boards,
            args.refresh_recent,
            args.concurrency,
        )
        print_discovery_summary(db_path, boards, run_id, counts)
    else:
        boards = load_boards(
            args.refresh_boards, ats_list, args.concurrency, recent=args.refresh_recent
        )
    scanned = [
        (ats, slug)
        for ats in ats_list
        for slug in (boards.get(ats, [])[: args.limit] if args.limit else boards.get(ats, []))
    ]
    criteria = [f"title {title!r} ({args.match})" if title else "",
                f"title or description /{args.grep}/" if args.grep else "",
                f"published within {args.since}" if args.since else "",
                "unseen postings only" if args.new_only else ""]
    what = " + ".join(c for c in criteria if c) or "every listed job"
    print(f"scanning {len(scanned)} boards across {len(ats_list)} platforms "
          f"for {what}...", file=sys.stderr)

    rows: list[dict] = []
    enrichments: list[dict] = []
    dead: set[tuple[str, str]] = set()
    # Which boards failed, not just how many. A count on stderr left no way to
    # re-scan the survivors of a throttle without repeating all 13,000 boards.
    # list.append is atomic under the GIL, so this needs no lock — same as `dead`.
    failed: list[tuple[str, str]] = []

    # Conditional requests, but only when a 304 genuinely means "nothing new here".
    conditional = not args.no_db and may_use_etags(
        title, pattern, cutoff, args.remote, args.new_only
    )
    etags = load_etags(db_path) if conditional else {}
    fresh_etags: dict[tuple[str, str], str] = {}
    unchanged = 0
    unchanged_boards: set[tuple[str, str]] = set()
    etag_lock = threading.Lock()
    if conditional and etags:
        print(f"  {len(etags)} boards have a stored etag; unchanged ones will be skipped",
              file=sys.stderr)

    def work(item: tuple[str, str]) -> tuple[list[dict], list[dict]]:
        nonlocal unchanged
        ats, slug = item
        meta: dict = {}
        for attempt in range(2):
            try:
                board_enrichments: list[dict] = []
                found = scan_board(
                    ats, slug, title, args.remote, args.match, pattern, cutoff,
                    etag=etags.get(item) if conditional else None,
                    meta=meta if conditional else None,
                    enrichment_sink=board_enrichments if not args.no_db else None,
                )
                if conditional and meta.get("etag"):
                    with etag_lock:
                        fresh_etags[item] = meta["etag"]
                return found, board_enrichments
            except NotModified:
                with etag_lock:
                    unchanged += 1
                    unchanged_boards.add(item)
                return [], []
            except NotFound:
                dead.add(item)
                return [], []
            except Exception as e:
                if attempt:
                    failed.append(item)
                    print(f"  ! {ats}/{slug}: {e}", file=sys.stderr)
        return [], []

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        for i, (found, board_enrichments) in enumerate(pool.map(work, scanned), 1):
            rows.extend(found)
            enrichments.extend(board_enrichments)
            if i % 500 == 0 or i == len(scanned):
                print(
                    f"  {i}/{len(scanned)} boards | {len(dead)} 404 | "
                    f"{len(failed)} err | {unchanged} unchanged | {len(rows)} matches",
                    file=sys.stderr,
                )

    if args.new_only:
        # Drop anything the database has already recorded. Done here rather than in
        # scan_board so the board fetch stays independent of storage.
        before = len(rows)
        seen_before = known_keys(db_path)
        rows = [r for r in rows if (r["ats"], r["id"]) not in seen_before]
        print(f"  --new-only: {before - len(rows)} already known, {len(rows)} new",
              file=sys.stderr)

    sort_rows(rows, args.sort)

    if not args.no_export:
        # BOM so Excel renders the en-dashes and bullets in location strings.
        csv_path = HERE / f"{args.out}.csv"
        with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            w.writerows(rows)
        json_path = HERE / f"{args.out}.json"
        # Full collections contain hundreds of thousands of descriptions. Stream
        # encoding so the exporter does not retain a second corpus-sized string.
        with json_path.open("w", encoding="utf-8") as stream:
            json.dump(rows, stream, indent=2)

    # Boards that errored, in the same shape as boards.json, so the retry is just
    # `--boards-from <out>.failed.json`. Written only when something failed, and
    # removed otherwise so a stale file from an earlier run cannot be re-read as
    # if it described this one. 404s are excluded: a dead slug is an expected
    # answer, not a failure, and it is already self-pruned below.
    failed_path = HERE / f"{args.out}.failed.json"
    if failed:
        by_platform: dict[str, list[str]] = {}
        for ats, slug in failed:
            by_platform.setdefault(ats, []).append(slug)
        failed_path.write_text(json.dumps(by_platform, indent=2))
        print(f"  {len(failed)} board{'s' if len(failed) != 1 else ''} failed -> "
              f"{failed_path.name} (retry with --boards-from {failed_path.name})",
              file=sys.stderr)
    elif failed_path.exists():
        failed_path.unlink()

    if dead and not args.no_db:
        mark_registered_boards_inactive(
            db_path, dead, datetime.now(timezone.utc).isoformat(timespec="seconds")
        )

    # Self-prune: drop slugs that 404'd so later runs skip them. Skipped for
    # --boards-from as well as --limit: `boards` is then a caller-supplied subset,
    # and with no cache on disk to fall back to it would be written out as though
    # it were the whole discovered board list.
    if dead and not args.limit and not args.boards_from:
        cached = _read_boards(BOARDS_CACHE) or boards
        for ats in ats_list:
            cached[ats] = [s for s in cached.get(ats, []) if (ats, s) not in dead]
        BOARDS_CACHE.write_text(json.dumps(cached, indent=2))

    written = "" if args.no_export else f"{csv_path.name}, {json_path.name}"
    if not args.no_db:
        seen_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if conditional:
            save_etags(db_path, fresh_etags, seen_at)
        # Only an unfiltered run saw everything, so only it may close postings.
        # An errored or conditional-304 response cannot supply a new absence
        # claim. Only boards whose complete response was observed may close jobs.
        excluded = set(failed) | unchanged_boards
        covered = [item for item in scanned if item not in excluded] if may_close_postings(title, pattern, cutoff, args.new_only) else None
        new, updated, closed = save(
            rows, db_path, seen_at, covered, enrichments=enrichments
        )
        written += (", " if written else "") + f"{db_path.name} ({new} new, {updated} already seen"
        written += f", {closed} closed)" if covered is not None else ")"

    receipt = {
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "boards_scanned": len(scanned), "boards_failed": len(failed),
        "boards_not_found": len(dead), "boards_unchanged": unchanged,
        "postings_returned": len(rows), "ats": ats_list,
        "limited": bool(args.limit),
        "authoritative": may_close_postings(title, pattern, cutoff, args.new_only),
        "status": "partial" if failed else "complete",
    }
    (HERE / f"{args.out}.collection.json").write_text(json.dumps(receipt, indent=2) + "\n")

    by_ats = {a: sum(1 for r in rows if r["ats"] == a) for a in ats_list}
    print(f"\n{len(rows)} jobs ({', '.join(f'{a} {n}' for a, n in by_ats.items())}) "
          f"-> {written}", file=sys.stderr)


if __name__ == "__main__":
    main()
