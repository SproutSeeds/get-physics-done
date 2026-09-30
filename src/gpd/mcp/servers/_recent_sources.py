"""New papers outside arXiv for ``recent_papers``: Zenodo, and journals or repositories indexed by OpenAlex.

Much new physics never reaches arXiv: many researchers post preprints on
Zenodo (arXiv requires an endorsement to submit), and journal articles and
other repositories (OSF, SSRN, HAL and others) reach OpenAlex. Each function
returns candidate papers in the bridge's paper shape with a ``source`` label,
plus an error message when the source could not be searched. The bridge
applies the same window and topic checks to every source.
"""

from __future__ import annotations

import html
import logging
import re
import threading
import time
from datetime import date

import httpx

from gpd.mcp.servers import arxiv_translators
from gpd.version import __version__ as GPD_VERSION

logger = logging.getLogger("gpd.arxiv_bridge.recent_sources")

ZENODO = "Zenodo"
OPENALEX = "OpenAlex"

_ZENODO_API = "https://zenodo.org/api/records"
_USER_AGENT = f"gpd-arxiv-bridge/{GPD_VERSION} (+https://github.com/SproutSeeds/get-physics-done)"
_HEADERS = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
# Guests may request at most 25 records per page and 30 pages a minute.
_ZENODO_PAGE = 25
_MIN_INTERVAL = 2.1
_pace_lock = threading.Lock()
_last_request = 0.0
# Scholarly types only; datasets, software and paratext are left out.
_OPENALEX_TYPES = "article|preprint|review|letter|book|book-chapter|report|dissertation"


def _zenodo_get(params: dict[str, object]) -> tuple[int, dict[str, object] | None]:
    """One paced Zenodo request. Returns ``(status, body)``; status 0 on a network error."""
    global _last_request
    with _pace_lock:
        wait = _last_request + _MIN_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()
    try:
        resp = httpx.get(_ZENODO_API, params=params, headers=_HEADERS, timeout=_TIMEOUT)
    except httpx.RequestError as exc:
        logger.info("Zenodo request error: %s", exc)
        return 0, None
    try:
        body = resp.json()
    except ValueError:
        body = None
    return resp.status_code, body if isinstance(body, dict) else None


def _plain_text(markup: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", markup)).split())


def zenodo_recent(search: str, start: date) -> tuple[list[dict[str, object]], str | None]:
    """Zenodo publications whose first version was created on or after ``start``.

    ``search`` uses Zenodo's query syntax (quoted phrases, ``AND``). Zenodo
    lists only the latest version of each record by default, so every version
    is searched and only first versions (``versions.index:1``) are kept: a new
    version of an older upload is not new work. Newest first, one page.
    """
    query = f"({search}) AND created:[{start.isoformat()} TO *] AND versions.index:1"
    status, body = _zenodo_get(
        {"q": query, "sort": "mostrecent", "size": _ZENODO_PAGE, "type": "publication", "allversions": "true"}
    )
    if status != 200 or body is None:
        return [], f"Zenodo search failed ({arxiv_translators.http_problem(status)})"
    hits = body.get("hits") if isinstance(body.get("hits"), dict) else {}
    papers: list[dict[str, object]] = []
    for hit in hits.get("hits") or []:
        if not isinstance(hit, dict):
            continue
        meta = hit.get("metadata") if isinstance(hit.get("metadata"), dict) else {}
        doi = str(hit.get("conceptdoi") or hit.get("doi") or "")
        kind = meta.get("resource_type") if isinstance(meta.get("resource_type"), dict) else {}
        papers.append({
            "id": doi or str(hit.get("id") or ""),
            "title": str(meta.get("title") or ""),
            "authors": [c["name"] for c in meta.get("creators") or [] if isinstance(c, dict) and c.get("name")],
            "abstract": arxiv_translators.EXTERNAL_CONTENT_PREFIX + _plain_text(str(meta.get("description") or "")),
            "categories": [],
            "published": str(hit.get("created") or ""),
            "url": f"https://doi.org/{doi}" if doi else str((hit.get("links") or {}).get("self_html") or ""),
            "source": ZENODO,
            "type": str(kind.get("subtype") or kind.get("type") or ""),
        })
    return papers, None


def openalex_recent(search: str, start: date, *, skip_zenodo: bool) -> tuple[list[dict[str, object]], str | None]:
    """Works in OpenAlex published on or after ``start`` that are not on arXiv.

    ``search`` uses OpenAlex's search syntax (quoted phrases, ``AND``). arXiv
    papers, including journal versions of arXiv preprints, are left out
    because arXiv is searched directly; Zenodo records are left out when
    Zenodo is searched directly. OpenAlex adds new works a few days late.
    """
    status, body, _ = arxiv_translators._http_get(
        "/works",
        {
            "filter": f"title_and_abstract.search:{search.replace(',', ' ')},"
            f"from_publication_date:{start.isoformat()},type:{_OPENALEX_TYPES}",
            "sort": "publication_date:desc",
            "per-page": 50,
            "select": "id,doi,title,publication_date,type,authorships,abstract_inverted_index,primary_location,locations",
        },
    )
    if status != 200 or body is None:
        return [], f"OpenAlex search failed ({arxiv_translators.http_problem(status)})"
    papers: list[dict[str, object]] = []
    for work in body.get("results") or []:
        if not isinstance(work, dict) or arxiv_translators._extract_arxiv_id(work):
            continue
        location = work.get("primary_location") if isinstance(work.get("primary_location"), dict) else {}
        source = str(((location.get("source") or {}) if isinstance(location.get("source"), dict) else {}).get("display_name") or OPENALEX)
        doi = str(work.get("doi") or "").removeprefix("https://doi.org/")
        if skip_zenodo and (source.startswith(ZENODO) or doi.lower().startswith("10.5281/zenodo")):
            continue
        papers.append({
            "id": doi or str(work.get("id") or "").rsplit("/", 1)[-1],
            "title": str(work.get("title") or ""),
            "authors": arxiv_translators._authors(work),
            "abstract": arxiv_translators.EXTERNAL_CONTENT_PREFIX
            + arxiv_translators._reassemble_abstract(work.get("abstract_inverted_index")),
            "categories": [],
            "published": str(work.get("publication_date") or ""),
            "url": f"https://doi.org/{doi}" if doi else str(location.get("landing_page_url") or work.get("id") or ""),
            "source": source,
            "type": str(work.get("type") or ""),
        })
    return papers, None
