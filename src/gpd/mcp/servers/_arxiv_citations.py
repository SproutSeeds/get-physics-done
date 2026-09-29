"""Citation lists for arXiv papers from OpenAlex and INSPIRE-HEP.

OpenAlex covers every field but lags new arXiv papers by about five days and
lacks reference lists for some papers (none for 2307.03223 on 2026-09-29).
INSPIRE-HEP, the high energy physics literature database, links references
and citing papers for its fields within days. Both are queried; each list
comes from the source that holds more works for it.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from gpd.mcp.servers import arxiv_translators
from gpd.version import __version__ as GPD_VERSION

logger = logging.getLogger("gpd.arxiv_bridge.citations")

INSPIRE = "INSPIRE-HEP"
OPENALEX = "OpenAlex"

_INSPIRE_API = "https://inspirehep.net/api/literature"
_USER_AGENT = f"gpd-arxiv-bridge/{GPD_VERSION} (+https://github.com/SproutSeeds/get-physics-done)"
_HEADERS = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
_FIELDS = "control_number,titles,arxiv_eprints,dois,citation_count,earliest_date"
# INSPIRE allows 15 requests per 5 seconds from one address.
_MIN_INTERVAL = 0.35
_pace_lock = threading.Lock()
_last_request = 0.0

CITATIONS_NOTE = (
    "Each list comes from the source that holds more works for it, OpenAlex (all fields) or "
    "INSPIRE-HEP (high energy physics and neighboring fields); cited_by_count is that source's count."
)
NOT_FOUND_NOTE = (
    "OpenAlex adds new arXiv papers about five days after submission, and INSPIRE-HEP "
    "covers high energy physics and neighboring fields."
)


def _inspire_get(params: dict[str, object]) -> tuple[int, dict[str, object] | None]:
    """One paced INSPIRE request. Returns ``(status, body)``; status 0 on a network error."""
    global _last_request
    with _pace_lock:
        wait = _last_request + _MIN_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()
    try:
        resp = httpx.get(_INSPIRE_API, params=params, headers=_HEADERS, timeout=_TIMEOUT)
    except httpx.RequestError as exc:
        logger.info("INSPIRE request error: %s", exc)
        return 0, None
    try:
        body = resp.json()
    except ValueError:
        body = None
    return resp.status_code, body if isinstance(body, dict) else None


def _first_value(items: object, key: str) -> str:
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and isinstance(item.get(key), str) and item[key].strip():
            return item[key].strip()
    return ""


def _inspire_entry(metadata: dict[str, object]) -> dict[str, object]:
    arxiv_id = _first_value(metadata.get("arxiv_eprints"), "value")
    doi = _first_value(metadata.get("dois"), "value")
    earliest = str(metadata.get("earliest_date") or "")
    if arxiv_id:
        url = f"https://arxiv.org/abs/{arxiv_id}"
    elif doi:
        url = f"https://doi.org/{doi}"
    else:
        url = f"https://inspirehep.net/literature/{metadata.get('control_number')}"
    return {
        "id": arxiv_id,
        "title": _first_value(metadata.get("titles"), "title"),
        "year": int(earliest[:4]) if earliest[:4].isdigit() else 0,
        "cited_by_count": metadata.get("citation_count") or 0,
        "url": url,
    }


def _hits(body: dict[str, object] | None) -> tuple[list[dict[str, object]], int]:
    hits = body.get("hits") if isinstance(body, dict) else None
    if not isinstance(hits, dict):
        return [], 0
    rows = [
        h["metadata"] for h in hits.get("hits") or [] if isinstance(h, dict) and isinstance(h.get("metadata"), dict)
    ]
    total = hits.get("total")
    return rows, total if isinstance(total, int) else len(rows)


def inspire_citations(paper_id: str, *, direction: str, order: str, limit: int) -> dict[str, object]:
    """References and citing works of an arXiv paper from INSPIRE-HEP, in the
    shape :func:`arxiv_translators.openalex_citations` returns."""
    status, body = _inspire_get({"q": f"arxiv:{paper_id}", "size": 1, "fields": _FIELDS})
    if status != 200 or body is None:
        return {
            "status": "error",
            "source": INSPIRE,
            "message": f"INSPIRE-HEP lookup failed ({arxiv_translators.http_problem(status)})",
        }
    records, _ = _hits(body)
    if not records:
        return {"status": "not_indexed", "source": INSPIRE}
    record = _inspire_entry(records[0])
    recid = records[0].get("control_number")
    result: dict[str, object] = {
        "status": "success",
        "source": INSPIRE,
        "title": record["title"],
        "year": record["year"],
    }
    lists = []
    if direction in ("both", "references"):
        lists.append(("references", f"citedby:recid:{recid}", "mostcited"))
    if direction in ("both", "cited_by"):
        lists.append(("cited_by", f"refersto:recid:{recid}", "mostcited" if order == "influential" else "mostrecent"))
    for key, query, sort in lists:
        status, body = _inspire_get({"q": query, "sort": sort, "size": min(50, limit + 10), "fields": _FIELDS})
        if status != 200 or body is None:
            result[f"{key}_error"] = f"INSPIRE-HEP {key} lookup failed ({arxiv_translators.http_problem(status)})"
            continue
        rows, total = _hits(body)
        result[f"{key}_total"] = total
        result[key] = arxiv_translators.unique_titles([_inspire_entry(row) for row in rows])[:limit]
    return result


def combine_citations(paper_id: str, direction: str, order: str, results: list[dict[str, object]]) -> dict[str, object]:
    """One answer from per-source results: each list from the source with the larger total."""
    sources = {
        str(r.get("source")): {k: r[k] for k in ("status", "references_total", "cited_by_total", "message") if k in r}
        for r in results
    }
    found = [r for r in results if r.get("status") == "success"]
    if not found:
        if all(r.get("status") == "not_indexed" for r in results):
            return {
                "status": "not_indexed",
                "paper_id": paper_id,
                "message": f"Neither OpenAlex nor INSPIRE-HEP has {paper_id}. {NOT_FOUND_NOTE}",
                "sources": sources,
            }
        problems = "; ".join(f"{r.get('source')}: {r.get('message') or r.get('status')}" for r in results)
        return {"status": "error", "paper_id": paper_id, "message": f"No citation source answered ({problems})"}

    combined: dict[str, object] = {
        "status": "success",
        "paper_id": paper_id,
        "title": found[0].get("title") or "",
        "year": found[0].get("year") or 0,
    }
    keys = [k for k in ("references", "cited_by") if direction in ("both", k)]
    for key in keys:
        candidates = [r for r in found if isinstance(r.get(key), list)]
        if not candidates:
            errors = "; ".join(str(r[f"{key}_error"]) for r in found if f"{key}_error" in r)
            combined[key] = []
            combined[f"{key}_note"] = errors or f"No source returned {key}."
            continue
        best = max(candidates, key=lambda r: (r.get(f"{key}_total") or 0, len(r[key])))
        combined[key] = best[key]
        combined[f"{key}_total"] = best.get(f"{key}_total") or 0
        combined[f"{key}_source"] = best["source"]
        if key == "references" and not best[key]:
            combined["references_note"] = "Neither OpenAlex nor INSPIRE-HEP has a reference list for this paper."
    if "cited_by" in keys:
        combined["cited_by_order"] = order
    combined["sources"] = sources
    combined["note"] = CITATIONS_NOTE
    return combined


def paper_citations(args: dict[str, object]) -> dict[str, object]:
    """References and citing works of an arXiv paper from OpenAlex and INSPIRE-HEP."""
    try:
        paper_id, direction, order, limit = arxiv_translators.parse_citation_args(args)
    except ValueError as exc:
        return {"status": "error", "message": str(exc)}
    request = {"paper_id": paper_id, "direction": direction, "order": order, "max_results": limit}
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {
            OPENALEX: pool.submit(arxiv_translators.openalex_citations, request),
            INSPIRE: pool.submit(inspire_citations, paper_id, direction=direction, order=order, limit=limit),
        }
        results = []
        for source, future in futures.items():
            try:
                results.append(future.result())
            except Exception as exc:  # a source bug must not hide the other source
                logger.exception("%s citations failed", source)
                results.append({"status": "error", "source": source, "message": str(exc)})
    return combine_citations(paper_id, direction, order, results)
