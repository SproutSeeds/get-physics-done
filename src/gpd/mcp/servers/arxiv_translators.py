"""OpenAlex-backed translators that mimic the upstream arxiv_mcp_server shape.

The bridge calls these in front of the upstream MCP so that ``search_papers``
and ``get_abstract`` traffic is served from `api.openalex.org` whenever
possible, leaving ``export.arxiv.org`` only for the long-tail fallback. The
downstream model receives the exact same record shape it would from the
upstream MCP, with one prefix-tag swap (`[EXTERNAL CONTENT]`) so the
prompt-injection guard remains visible.

Public surface:

* :func:`openalex_search` — search by free-text query, returns papers
  list in upstream's Atom-derived shape.
* :func:`openalex_abstract` — fetch a single arxiv-id abstract record.
* :func:`openalex_results_to_papers` — pure-function helper used by both
  the live translator and shape-parity unit tests.
* :func:`gcs_fetch_pdf` — re-export of the GCS PDF fetcher so the
  translator boundary is the only import callers need.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from functools import lru_cache
from urllib.parse import quote

import httpx

from gpd.mcp.servers import _arxiv_gcs
from gpd.version import __version__ as GPD_VERSION

logger = logging.getLogger("gpd.arxiv_bridge.translators")

_OPENALEX_BASE = "https://api.openalex.org"

# arXiv's canonical OpenAlex *source* id (i.e. the venue, not an institution).
# Verified live against api.openalex.org/sources?search=arxiv on 2026-05-20:
# S4306400194 = "arXiv (Cornell University)", count = 28,975 results when used
# as `primary_location.source.id:S4306400194` on a sample query. The earlier
# `I4210109252` / `I4210168979` institution-style IDs returned 0 results for
# every query, which silently turned `openalex_search` into a no-op. Always
# filter via this source id, not an institution lineage.
OPENALEX_ARXIV_SOURCE_ID = "S4306400194"

# Prefix tag the downstream model sees on any abstract / search-result body.
# Kept short and stable so a fine-tuned prompt-injection guard can pattern-match.
EXTERNAL_CONTENT_PREFIX = "[EXTERNAL CONTENT] "

_USER_AGENT = f"gpd-arxiv-bridge/{GPD_VERSION} (+https://github.com/SproutSeeds/get-physics-done)"
_HEADERS = {"User-Agent": _USER_AGENT, "Accept": "application/json"}

# OpenAlex meters requests. Without a key, every request counts against a
# small free daily budget shared by all clients on the caller's IP address
# (observed 2026-09-29: $0.10 per day, $0.001 per search, $0.0001 per filter
# lookup), and exhausted budgets return HTTP 429 until midnight UTC. A free
# personal key has its own budget: https://help.openalex.org/api/authentication/
OPENALEX_API_KEY_ENV = "OPENALEX_API_KEY"

# macOS Keychain items read when OPENALEX_API_KEY is unset, as (service,
# account): GPD's own item, then the item `orp secrets keychain-add --alias
# openalex-api-key --provider openalex` creates. Some runtimes pass only an
# allowlist of environment variables to MCP servers, and a key must not be
# written into their config files, so the server looks it up itself.
KEYCHAIN_ITEMS = (
    ("get-physics-done", "OPENALEX_API_KEY"),
    ("orp.secret.openalex", "openalex-api-key"),
)


@lru_cache(maxsize=1)
def _keychain_api_key() -> str:
    if sys.platform != "darwin" or shutil.which("security") is None:
        return ""
    for service, account in KEYCHAIN_ITEMS:
        try:
            proc = subprocess.run(
                ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        key = proc.stdout.strip()
        if proc.returncode == 0 and key:
            return key
    return ""


def openalex_api_key() -> str:
    """The OpenAlex key from ``OPENALEX_API_KEY``, else the macOS Keychain, else ``""``."""
    return os.environ.get(OPENALEX_API_KEY_ENV, "").strip() or _keychain_api_key()


def _request_headers() -> dict[str, str]:
    headers = dict(_HEADERS)
    key = openalex_api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers
_TIMEOUT = httpx.Timeout(20.0, connect=10.0)

# Recognise arxiv IDs anywhere inside an OpenAlex Work record (pdf_url,
# landing page, doi). New-format e.g. "2401.12345v3"; old-format e.g.
# "hep-th/9901001".
_ARXIV_ID_RE = re.compile(
    r"arxiv\.org/(?:abs|pdf|html)/(?P<id>(?:[a-z\-]+(?:\.[a-z][a-z0-9\-]*)?/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?)",
    re.IGNORECASE,
)
_DOI_ARXIV_RE = re.compile(
    r"10\.48550/arxiv\.(?P<id>(?:[a-z\-]+(?:\.[a-z][a-z0-9\-]*)?/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?)",
    re.IGNORECASE,
)


def gcs_fetch_pdf(paper_id: str) -> bytes | None:
    """Fetch a paper PDF from the public Cornell-Google GCS mirror."""
    return _arxiv_gcs.fetch_pdf_from_gcs(paper_id)


def _strip_version(paper_id: str) -> str:
    """Drop a trailing ``vN`` for ID comparison; keep the canonical stem."""
    return re.sub(r"v\d+$", "", paper_id)


def _extract_arxiv_id(work: dict[str, object]) -> str | None:
    """Find the arxiv identifier inside an OpenAlex Work record.

    Checked in order: ``primary_location.pdf_url``, ``primary_location.landing_page_url``,
    ``ids.doi``, and any URL inside ``locations[*]``. Returns the canonical
    stem (no ``vN``) so the bridge can hand it back to the downstream model
    in the format the upstream MCP would have used.
    """

    def _scan(url: str | None) -> str | None:
        if not isinstance(url, str):
            return None
        m = _ARXIV_ID_RE.search(url) or _DOI_ARXIV_RE.search(url)
        if m:
            return _strip_version(m.group("id"))
        return None

    primary = work.get("primary_location") or {}
    for key in ("pdf_url", "landing_page_url", "source_url"):
        found = _scan(primary.get(key))
        if found:
            return found

    ids = work.get("ids") or {}
    for key in ("doi", "openalex"):
        found = _scan(ids.get(key))
        if found:
            return found

    for loc in work.get("locations") or []:
        if not isinstance(loc, dict):
            continue
        for key in ("pdf_url", "landing_page_url"):
            found = _scan(loc.get(key))
            if found:
                return found

    return None


def _reassemble_abstract(inverted: dict[str, list[int]] | None) -> str:
    """Reconstruct plain abstract text from OpenAlex's inverted index.

    OpenAlex returns ``abstract_inverted_index`` as ``{word: [positions...]}``
    instead of plain text. Reassemble by sorting (position, word) pairs and
    joining. Returns an empty string when the index is missing/empty —
    callers decide whether that constitutes an error.
    """
    if not isinstance(inverted, dict) or not inverted:
        return ""
    positioned: list[tuple[int, str]] = []
    for word, positions in inverted.items():
        if not isinstance(word, str) or not isinstance(positions, list):
            continue
        for pos in positions:
            if isinstance(pos, int):
                positioned.append((pos, word))
    positioned.sort(key=lambda pair: pair[0])
    return " ".join(word for _, word in positioned)


def _authors(work: dict[str, object]) -> list[str]:
    out: list[str] = []
    for entry in work.get("authorships") or []:
        if not isinstance(entry, dict):
            continue
        author = entry.get("author") or {}
        name = author.get("display_name")
        if isinstance(name, str) and name:
            out.append(name)
    return out


def _categories(work: dict[str, object]) -> list[str]:
    """Best-effort category list — OpenAlex 'concepts' or arxiv subfields.

    Upstream MCP returns arxiv categories like ``hep-th``. OpenAlex doesn't
    carry that taxonomy directly, so we fall back to concept display names.
    The downstream contract only requires a ``list[str]``, not a specific
    taxonomy, so consumers must not rely on arxiv-cat semantics here.
    """
    out: list[str] = []
    for concept in work.get("concepts") or []:
        if isinstance(concept, dict):
            name = concept.get("display_name")
            if isinstance(name, str) and name:
                out.append(name)
    return out


def _landing_url(work: dict[str, object], arxiv_id: str) -> str:
    # Always return the canonical arXiv URL. OpenAlex ``primary_location`` can
    # point at a publisher or OpenAlex page even when the work has an arXiv id,
    # and the upstream arxiv_mcp_server contract requires the arxiv.org URL so
    # downstream consumers route through the bridge's normal download path.
    del work  # signature parity with prior call sites; OpenAlex fields ignored.
    return f"https://arxiv.org/abs/{arxiv_id}"


def _pdf_url(work: dict[str, object], arxiv_id: str) -> str:
    del work  # same rationale as ``_landing_url``: canonical arxiv.org only.
    return f"https://arxiv.org/pdf/{arxiv_id}"


def _to_paper_record(work: dict[str, object]) -> dict[str, object] | None:
    """Translate one OpenAlex Work into upstream's search-result shape.

    Returns ``None`` when the work has no recoverable arxiv ID — the bridge
    drops these because the downstream contract requires arxiv IDs (the
    model uses them to call ``download_paper``).
    """
    arxiv_id = _extract_arxiv_id(work)
    if not arxiv_id:
        return None
    abstract = _reassemble_abstract(work.get("abstract_inverted_index"))
    return {
        "id": arxiv_id,
        "title": work.get("title") or "",
        "authors": _authors(work),
        "abstract": EXTERNAL_CONTENT_PREFIX + abstract,
        "categories": _categories(work),
        "published": work.get("publication_date") or "",
        "url": _landing_url(work, arxiv_id),
        "resource_uri": f"arxiv://{arxiv_id}",
    }


def openalex_results_to_papers(response: dict[str, object]) -> list[dict[str, object]]:
    """Convert an OpenAlex ``/works`` response into a list of upstream-shaped
    paper records, silently dropping works without an extractable arxiv ID."""
    out: list[dict[str, object]] = []
    for work in response.get("results") or []:
        if not isinstance(work, dict):
            continue
        record = _to_paper_record(work)
        if record is not None:
            out.append(record)
    return out


def _http_get(
    path: str, params: dict[str, object] | None = None
) -> tuple[int, dict[str, object] | None, str]:
    """Single OpenAlex GET. Returns ``(status_code, parsed_json, raw_text)``.

    Never raises on HTTP/parse errors — the translator caller chooses the
    error envelope. Returns ``(0, None, str(exc))`` on transport failure so
    callers can distinguish HTTP failures (status >= 400) from network
    failures (status == 0).
    """
    url = f"{_OPENALEX_BASE}{path}"
    try:
        resp = httpx.get(url, params=params, headers=_request_headers(), timeout=_TIMEOUT)
    except httpx.RequestError as exc:
        logger.info("OpenAlex request error on %s: %s", path, exc)
        return 0, None, str(exc)
    try:
        body = resp.json()
        if not isinstance(body, dict):
            body = None
    except ValueError:
        body = None
    return resp.status_code, body, resp.text


_BOOLEAN_OR_GROUPED = re.compile(r'["()]|\b(?:AND|OR|NOT)\b')


def phrase_form(query: str) -> str | None:
    """Quoted form of a plain multi-word query, or ``None``.

    Searched loosely, a specialised phrase such as ``neural network field
    theory`` returns generic machine learning papers. Queries that already
    carry quotes, parentheses or boolean operators are left as written.
    """
    if len(query.split()) < 2 or _BOOLEAN_OR_GROUPED.search(query):
        return None
    return f'"{query}"'


def openalex_search(args: dict[str, object]) -> dict[str, object]:
    """Search OpenAlex and return upstream-shaped ``{papers, total_results}``.

    Recognised ``args``: ``query`` (str, required) and ``max_results`` (int,
    1-200, default 10). Other keys are ignored — callers must translate
    arxiv-style filters before passing them through.

    A plain multi-word query is searched as a quoted phrase first; works whose
    title or abstract contains all of its words then fill any remaining slots,
    without duplicates. OpenAlex's free-text ``search`` also matches full text,
    so a loose multi-concept query (``reflection positivity neural networks``)
    otherwise returns papers that merely mention each word somewhere.
    """
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        return {"papers": [], "total_results": 0}
    query = query.strip()

    raw_max = args.get("max_results", 10)
    try:
        per_page = max(1, min(200, int(raw_max)))
    except (TypeError, ValueError):
        per_page = 10

    # Any location on arXiv, not only the primary one: OpenAlex merges arXiv
    # preprints into canonical works whose primary location is often the
    # journal version, so a primary-location filter drops most published
    # papers. The source id is verified live, see `OPENALEX_ARXIV_SOURCE_ID`.
    on_arxiv = f"locations.source.id:{OPENALEX_ARXIV_SOURCE_ID}"
    phrase = phrase_form(query)
    if phrase:
        # Commas separate OpenAlex filters, so they cannot appear in a value.
        words = " ".join(query.replace(",", " ").split())
        steps: list[tuple[dict[str, object], dict[str, object]]] = [
            ({"search": phrase, "filter": on_arxiv}, {"search": phrase}),
            (
                {"filter": f"title_and_abstract.search:{words},{on_arxiv}", "sort": "relevance_score:desc"},
                {"filter": f"title_and_abstract.search:{words}", "sort": "relevance_score:desc"},
            ),
        ]
    else:
        steps = [({"search": query, "filter": on_arxiv}, {"search": query})]

    papers: list[dict[str, object]] = []
    works: list[dict[str, object]] = []
    seen: set[str] = set()
    for params, without_arxiv_filter in steps:
        status, body, _ = _http_get("/works", {**params, "per-page": per_page})
        # Only retry without the arXiv filter when OpenAlex rejects the filter
        # itself (400 Bad Request / 422 Unprocessable). For 429, 5xx, timeouts,
        # or parse failures, the filter is not the problem — retrying doubles
        # upstream load without improving the outcome, which violates the
        # rate-limit-resilient contract this translator is built around.
        if status in {400, 422}:
            status, body, _ = _http_get("/works", {**without_arxiv_filter, "per-page": per_page})
        if status == 429:
            break  # budget exhausted; the bridge falls back to arXiv
        if status != 200 or body is None:
            continue
        for work in body.get("results") or []:
            if not isinstance(work, dict):
                continue
            paper = _to_paper_record(work)
            paper_id = paper.get("id") if paper else None
            if isinstance(paper_id, str) and paper_id not in seen:
                seen.add(paper_id)
                papers.append(paper)
                works.append(work)
        if len(papers) >= per_page:
            break

    papers = papers[:per_page]
    result: dict[str, object] = {"papers": papers, "total_results": len(papers), "coverage_note": COVERAGE_NOTE}
    cited = frequently_cited(works[:per_page])
    if cited:
        result["frequently_cited"] = cited
        result["frequently_cited_note"] = FREQUENTLY_CITED_NOTE
    return result


# Measured 2026-09-29 on hep-th: 0 of 50 papers submitted 2 to 3 days earlier were
# in OpenAlex, 69 of 70 submitted 5 to 45 days earlier were.
COVERAGE_NOTE = (
    "OpenAlex adds new arXiv papers about five days after submission; "
    "use recent_papers for the newest ones."
)

FREQUENTLY_CITED_NOTE = (
    "Works cited by several of these results; for a topic they are often the foundational "
    "papers, even when their wording differs from the query."
)


def frequently_cited(
    works: list[dict[str, object]], *, limit: int = 8, min_shared: int = 2
) -> list[dict[str, object]]:
    """The works most often referenced by ``works``, as a short list.

    Keyword search misses canonical papers worded differently from the query
    (for "quantum error correction holography", the AdS/CFT error-correction
    papers never say "holography"), yet the results cite them: both were
    referenced by 9 of the top 10 on 2026-09-29. Counts come from each result's
    OpenAlex ``referenced_works``; one filter lookup resolves titles and arXiv
    ids. Works already among the results are skipped. Returns ``[]`` when
    fewer than three results carry references or the lookup fails.
    """
    own = {work.get("id") for work in works}
    with_refs = [work for work in works if work.get("referenced_works")]
    if len(with_refs) < 3:
        return []
    # dict.fromkeys dedupes each list in order, so ties keep first-seen order.
    counts = Counter(
        ref
        for work in with_refs
        for ref in dict.fromkeys(work.get("referenced_works") or [])
        if isinstance(ref, str) and ref not in own
    )
    shared = [(ref, n) for ref, n in counts.most_common() if n >= min_shared][:limit]
    if not shared:
        return []
    ids = "|".join(ref.rsplit("/", 1)[-1] for ref, _ in shared)
    status, body, _ = _http_get(
        "/works",
        {
            "filter": f"openalex:{ids}",
            "per-page": len(shared),
            "select": "id,title,publication_year,doi,ids,primary_location,locations",
        },
    )
    if status != 200 or body is None:
        return []
    by_id = {work.get("id"): work for work in body.get("results") or [] if isinstance(work, dict)}
    out: list[dict[str, object]] = []
    for ref, n in shared:
        work = by_id.get(ref)
        if not work:
            continue
        arxiv_id = _extract_arxiv_id(work) or ""
        doi = work.get("doi") if isinstance(work.get("doi"), str) else ""
        out.append(
            {
                "id": arxiv_id,
                "title": work.get("title") or "",
                "year": work.get("publication_year") or 0,
                "cited_by_results": n,
                "url": f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else (doi or ref),
            }
        )
    return out


def _find_arxiv_work(paper_id: str) -> tuple[int, dict[str, object] | None]:
    """Find the OpenAlex work that carries an arXiv preprint.

    OpenAlex merges arXiv preprints into canonical works whose primary DOI can
    belong to another version, so ``/works/doi:10.48550/arxiv.<id>`` returns
    404 for most arXiv IDs (observed 2026-09-29). The arXiv DOI and abstract
    page remain as location landing pages, which the list filter matches
    exactly. Returns ``(status, work)``; ``work`` is ``None`` when nothing
    matches or the request fails.
    """
    landing_pages = (
        f"https://doi.org/10.48550/arxiv.{paper_id}",
        f"http://arxiv.org/abs/{paper_id}",
    )
    status, body, _ = _http_get(
        "/works",
        {"filter": "locations.landing_page_url:" + "|".join(landing_pages), "per_page": 1},
    )
    if status != 200 or body is None:
        return status, None
    results = body.get("results")
    if not isinstance(results, list) or not results or not isinstance(results[0], dict):
        return status, None
    return status, results[0]


def openalex_abstract(args: dict[str, object]) -> dict[str, object]:
    """Fetch a single paper's metadata + abstract by arxiv ID."""
    paper_id_raw = args.get("paper_id")
    if not isinstance(paper_id_raw, str) or not paper_id_raw.strip():
        return _abstract_error("", "paper_id must be a non-empty string")
    paper_id = _strip_version(paper_id_raw.strip())

    status, body = _find_arxiv_work(paper_id)
    if body is None:
        # Fall back to the DOI singleton, which still resolves works whose
        # primary DOI is the arXiv DOI. URL-encode the DOI segment so old-style
        # arXiv IDs containing slashes (e.g. "hep-th/9901001") don't break the
        # request path.
        doi = f"10.48550/arxiv.{paper_id}"
        status, body, _ = _http_get(f"/works/doi:{quote(doi, safe='')}")
    if status != 200 or body is None:
        return _abstract_error(paper_id, f"OpenAlex lookup failed (HTTP {status}) for {paper_id}")

    abstract = _reassemble_abstract(body.get("abstract_inverted_index"))
    if not abstract:
        return _abstract_error(paper_id, f"No abstract available on OpenAlex for {paper_id}")

    return {
        "status": "success",
        "paper_id": paper_id,
        "title": body.get("title") or "",
        "authors": _authors(body),
        "abstract": EXTERNAL_CONTENT_PREFIX + abstract,
        "categories": _categories(body),
        "published": body.get("publication_date") or "",
        "pdf_url": _pdf_url(body, paper_id),
    }


_CITATION_SELECT = "id,title,publication_year,doi,ids,primary_location,locations,cited_by_count"


def unique_titles(works: list[dict[str, object]]) -> list[dict[str, object]]:
    """Drop repeated titles (OpenAlex keeps some works, such as book editions, twice)."""
    seen: set[str] = set()
    out = []
    for work in works:
        key = re.sub(r"[^a-z0-9]+", " ", str(work.get("title") or "").lower()).strip()
        if key and key in seen:
            continue
        seen.add(key)
        out.append(work)
    return out


def _citation_entry(work: dict[str, object]) -> dict[str, object]:
    arxiv_id = _extract_arxiv_id(work) or ""
    doi = work.get("doi") if isinstance(work.get("doi"), str) else ""
    return {
        "id": arxiv_id,
        "title": work.get("title") or "",
        "year": work.get("publication_year") or 0,
        "cited_by_count": work.get("cited_by_count") or 0,
        "url": f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else (doi or str(work.get("id") or "")),
    }


_CITATION_ID_RE = re.compile(r"(?:\d{4}\.\d{4,5}|[a-z][a-z\-]*(?:\.[a-z]{2})?/\d{7})(?:v\d+)?", re.IGNORECASE)


def parse_citation_args(args: dict[str, object]) -> tuple[str, str, str, int]:
    """``(paper_id, direction, order, max_results)`` for a citations request.

    Raises ``ValueError`` with a caller-facing message. The id must look like
    an arXiv id (an ``arXiv:`` prefix and a version suffix are accepted), so
    it can go into source query syntax as written.
    """
    raw = args.get("paper_id")
    paper_id = re.sub(r"^arxiv:", "", raw.strip(), flags=re.IGNORECASE) if isinstance(raw, str) else ""
    if not _CITATION_ID_RE.fullmatch(paper_id):
        raise ValueError("paper_id must be an arXiv id such as 1411.7041 or hep-th/9711200")
    direction = args.get("direction") or "both"
    order = args.get("order") or "influential"
    if direction not in ("both", "references", "cited_by"):
        raise ValueError("direction must be both, references or cited_by")
    if order not in ("influential", "recent"):
        raise ValueError("order must be influential or recent")
    try:
        limit = max(1, min(50, int(args.get("max_results", 20))))
    except (TypeError, ValueError):
        limit = 20
    return _strip_version(paper_id), str(direction), str(order), limit


def http_problem(status: int) -> str:
    """Short description of a failed HTTP status for error messages."""
    if status == 0:
        return "network error"
    if status == 429:
        return "HTTP 429: rate limit or daily budget reached"
    return f"HTTP {status}"


def openalex_citations(args: dict[str, object]) -> dict[str, object]:
    """References and citing works of an arXiv paper, from OpenAlex.

    ``args``: ``paper_id`` (required), ``direction`` (``both``, ``references``
    or ``cited_by``; default ``both``), ``order`` for citing works
    (``influential``: most cited first, default; ``recent``: newest first) and
    ``max_results`` (1-50, default 20) per list. References come most cited
    first from the ``cited_by`` filter, one request per list. Brand-new arXiv
    papers are not in OpenAlex yet; they come back with ``status:
    not_indexed``. A list whose request fails carries ``<list>_error``.
    """
    try:
        paper_id, direction, order, limit = parse_citation_args(args)
    except ValueError as exc:
        return {"status": "error", "source": "OpenAlex", "message": str(exc)}

    status, work = _find_arxiv_work(paper_id)
    if work is None and status == 200:
        return {"status": "not_indexed", "source": "OpenAlex", "paper_id": paper_id}
    if work is None:
        return {
            "status": "error",
            "source": "OpenAlex",
            "paper_id": paper_id,
            "message": f"OpenAlex lookup failed ({http_problem(status)})",
        }

    work_id = str(work.get("id") or "").rsplit("/", 1)[-1]
    result: dict[str, object] = {
        "status": "success",
        "source": "OpenAlex",
        "paper_id": paper_id,
        "title": work.get("title") or "",
        "year": work.get("publication_year") or 0,
    }
    lists = []
    if direction in ("both", "references"):
        if work.get("referenced_works"):
            lists.append(("references", f"cited_by:{work_id}", "cited_by_count:desc"))
        else:
            result["references_total"] = 0
            result["references"] = []
    if direction in ("both", "cited_by"):
        sort = "cited_by_count:desc" if order == "influential" else "publication_date:desc"
        lists.append(("cited_by", f"cites:{work_id}", sort))
    for key, flt, sort in lists:
        # A few extra rows leave room for the repeated titles unique_titles drops.
        status, body, _ = _http_get(
            "/works", {"filter": flt, "sort": sort, "per-page": min(50, limit + 10), "select": _CITATION_SELECT}
        )
        if status != 200 or body is None:
            result[f"{key}_error"] = f"OpenAlex {key} lookup failed ({http_problem(status)})"
            continue
        meta = body.get("meta") if isinstance(body.get("meta"), dict) else {}
        works = [w for w in body.get("results") or [] if isinstance(w, dict)]
        result[f"{key}_total"] = meta.get("count", len(works))
        result[key] = [_citation_entry(w) for w in unique_titles(works)[:limit]]
    return result


def _abstract_error(paper_id: str, message: str) -> dict[str, object]:
    return {
        "status": "error",
        "paper_id": paper_id,
        "title": "",
        "authors": [],
        "abstract": "",
        "categories": [],
        "published": "",
        "pdf_url": "",
        "message": message,
    }


__all__ = [
    "EXTERNAL_CONTENT_PREFIX",
    "phrase_form",
    "gcs_fetch_pdf",
    "openalex_abstract",
    "http_problem",
    "openalex_citations",
    "parse_citation_args",
    "unique_titles",
    "openalex_results_to_papers",
    "openalex_search",
]
