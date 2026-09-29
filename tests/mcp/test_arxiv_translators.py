"""Layer 2 — Translator unit tests for the arXiv-MCP replacement.

Exercises the OpenAlex search/abstract translators and the GCS PDF fetcher
against five hand-picked papers covering: new-format ID, old-format ID, paper
with no abstract, paper with no extractable arxiv ID (skip case), paper with
multiple versions.

Assertions enforce response-shape parity with the upstream arxiv_mcp_server
contract: field names match, field types match, abstract non-empty when arXiv
has one, arxiv IDs always extractable when expected.

Run:
    pytest tests/mcp/test_arxiv_translators.py -q
    GPD_ARXIV_NO_NETWORK=1 pytest tests/mcp/test_arxiv_translators.py -q   # skip live calls

The translators are imported lazily; if they do not yet exist this module
xfails cleanly so the suite stays green during the bring-up window.
"""

from __future__ import annotations

import os

import pytest

pytestmark = [pytest.mark.parity]

NETWORK = os.environ.get("GPD_ARXIV_NO_NETWORK") != "1"

# Hand-picked corpus. Stable IDs only; do not replace casually.
PAPERS = {
    "new_format": "2401.12345",          # standard new-format ID
    "old_format": "hep-th/9901001",      # pre-2007 old-format ID
    "no_abstract": "1701.00001",         # known to lack abstract on OpenAlex
    "no_arxiv_link": "W2741809807",      # OpenAlex work with no arxiv ID (skip)
    "multi_version": "1706.03762v5",     # Attention Is All You Need, v5 exists
}

UPSTREAM_PAPER_KEYS = {
    "id",
    "title",
    "authors",
    "abstract",
    "categories",
    "published",
    "url",
    "resource_uri",
}
UPSTREAM_ABSTRACT_KEYS = {
    "status",
    "paper_id",
    "title",
    "authors",
    "abstract",
    "categories",
    "published",
    "pdf_url",
}


@pytest.fixture(scope="module")
def translators():
    # Only xfail when the module/symbols genuinely do not exist yet — a broad
    # ``except Exception`` would also swallow runtime defects (TypeError,
    # AttributeError, etc.) inside the translator module and silently mark
    # them as "expected to fail", hiding real regressions.
    try:
        from gpd.mcp.servers.arxiv_translators import (  # type: ignore
            gcs_fetch_pdf,
            openalex_abstract,
            openalex_search,
        )
    except (ImportError, ModuleNotFoundError) as exc:
        pytest.xfail(f"arxiv_translators not yet implemented: {exc}")
    return openalex_search, openalex_abstract, gcs_fetch_pdf


@pytest.mark.skipif(not NETWORK, reason="network probes disabled")
def test_search_new_format_id_extractable(translators):
    openalex_search, _, _ = translators
    res = openalex_search({"query": "attention is all you need", "max_results": 5})
    assert isinstance(res, dict)
    assert isinstance(res.get("papers"), list)
    assert res.get("total_results") == len(res["papers"])
    # OpenAlex occasionally returns an empty payload for live queries; that is
    # an upstream API hiccup rather than a translator regression, so skip the
    # id-extraction assertions when no results came back.
    if not res["papers"]:
        pytest.skip("OpenAlex returned no results for the live probe query")
    for p in res["papers"]:
        assert set(p.keys()) >= UPSTREAM_PAPER_KEYS, (
            f"missing keys: {UPSTREAM_PAPER_KEYS - set(p.keys())}"
        )
        assert isinstance(p["id"], str) and p["id"]
        assert isinstance(p["authors"], list)
        assert isinstance(p["categories"], list)
        assert p["abstract"].startswith("[EXTERNAL CONTENT]"), "must preserve upstream prefix"


def _skip_if_budget_exhausted(res: dict) -> None:
    """OpenAlex answers HTTP 429 once the shared anonymous daily budget for the
    caller's IP is spent (CI runners share IPs); that is not a translator bug."""
    if res.get("status") == "error" and "HTTP 429" in str(res.get("message", "")):
        pytest.skip("OpenAlex daily budget exhausted for this IP (HTTP 429)")


@pytest.mark.skipif(not NETWORK, reason="network probes disabled")
def test_abstract_new_format(translators):
    _, openalex_abstract, _ = translators
    res = openalex_abstract({"paper_id": PAPERS["new_format"]})
    assert isinstance(res, dict)
    assert set(res.keys()) >= UPSTREAM_ABSTRACT_KEYS
    _skip_if_budget_exhausted(res)
    assert res["status"] == "success"
    assert res["paper_id"] == PAPERS["new_format"]
    assert res["abstract"].startswith("[EXTERNAL CONTENT]")
    assert len(res["abstract"]) > len("[EXTERNAL CONTENT] ") + 50


@pytest.mark.skipif(not NETWORK, reason="network probes disabled")
def test_abstract_old_format(translators):
    _, openalex_abstract, _ = translators
    res = openalex_abstract({"paper_id": PAPERS["old_format"]})
    _skip_if_budget_exhausted(res)
    assert res["status"] == "success"
    assert isinstance(res["categories"], list)


@pytest.mark.skipif(not NETWORK, reason="network probes disabled")
def test_abstract_missing_falls_back_gracefully(translators):
    """When OpenAlex has no abstract, the translator must surface a clear error
    (status='error', message non-empty) rather than crash or return a partial
    success masquerading as success."""
    _, openalex_abstract, _ = translators
    res = openalex_abstract({"paper_id": PAPERS["no_abstract"]})
    assert res["status"] in {"success", "error"}
    if res["status"] == "success":
        assert res["abstract"].startswith("[EXTERNAL CONTENT]")
    else:
        assert isinstance(res.get("message"), str) and res["message"]


def test_search_skips_works_without_arxiv_id(translators):
    """If OpenAlex returns a work that has no extractable arxiv ID, the
    translator must drop it from the result list rather than emit a paper
    object with id=None / id=''."""
    openalex_search, _, _ = translators
    fake_openalex_response = {
        "results": [
            {  # has arxiv id via pdf_url
                "id": "https://openalex.org/W1",
                "title": "good paper",
                "authorships": [{"author": {"display_name": "A"}}],
                "abstract_inverted_index": {"hello": [0], "world": [1]},
                "primary_location": {"pdf_url": "https://arxiv.org/pdf/2401.12345.pdf"},
                "publication_date": "2024-01-22",
                "concepts": [{"display_name": "physics"}],
            },
            {  # no arxiv id anywhere => skip
                "id": "https://openalex.org/W2741809807",
                "title": "non-arxiv preprint",
                "authorships": [{"author": {"display_name": "B"}}],
                "abstract_inverted_index": {"x": [0]},
                "primary_location": {"pdf_url": "https://example.org/foo.pdf"},
                "publication_date": "2017-09-01",
                "concepts": [],
            },
        ]
    }
    from gpd.mcp.servers import arxiv_translators  # type: ignore
    if not hasattr(arxiv_translators, "openalex_results_to_papers"):
        pytest.xfail("openalex_results_to_papers helper not implemented yet")
    papers = arxiv_translators.openalex_results_to_papers(fake_openalex_response)
    ids = [p["id"] for p in papers]
    assert "2401.12345" in ids
    assert all(i for i in ids), "no empty/None ids allowed"
    assert len(papers) == 1, "non-arxiv result must be dropped"


@pytest.mark.skipif(not NETWORK, reason="network probes disabled")
def test_pdf_fetch_handles_multiple_versions(translators):
    _, _, gcs_fetch_pdf = translators
    raw = PAPERS["multi_version"]  # contains explicit v5
    res = gcs_fetch_pdf(raw)
    # ``gcs_fetch_pdf`` is a re-export of ``_arxiv_gcs.fetch_pdf_from_gcs``,
    # which contractually returns raw PDF bytes (or ``None`` on miss). If the
    # GCS probe missed entirely, skip the body assertions rather than fail —
    # the multi-version probe is a live network test.
    if res is None:
        pytest.skip("GCS PDF not available for multi-version probe")
    if hasattr(res, "read"):
        res = res.read()
    assert isinstance(res, (bytes, bytearray))
    assert len(res) > 10_000, "PDF should be > 10KB"
    assert res[:4] == b"%PDF"


def test_shape_parity_search(translators):
    """Pure shape test: translator output must match upstream paper-record
    shape exactly (keys + types). No network: feeds a synthetic response."""
    from gpd.mcp.servers import arxiv_translators  # type: ignore
    if not hasattr(arxiv_translators, "openalex_results_to_papers"):
        pytest.xfail("openalex_results_to_papers helper not implemented yet")
    fake = {
        "results": [
            {
                "id": "https://openalex.org/W1",
                "title": "t",
                "authorships": [{"author": {"display_name": "A1"}}, {"author": {"display_name": "A2"}}],
                "abstract_inverted_index": {"hello": [0]},
                "primary_location": {"pdf_url": "https://arxiv.org/pdf/2401.12345.pdf"},
                "publication_date": "2024-01-22",
                "concepts": [{"display_name": "physics.gen-ph"}],
            }
        ]
    }
    papers = arxiv_translators.openalex_results_to_papers(fake)
    assert len(papers) == 1
    p = papers[0]
    assert set(p.keys()) == UPSTREAM_PAPER_KEYS
    assert isinstance(p["id"], str)
    assert isinstance(p["title"], str)
    assert isinstance(p["authors"], list) and all(isinstance(a, str) for a in p["authors"])
    assert isinstance(p["abstract"], str) and p["abstract"].startswith("[EXTERNAL CONTENT]")
    assert isinstance(p["categories"], list)
    assert isinstance(p["published"], str)
    assert isinstance(p["url"], str)
    assert p["resource_uri"] == f"arxiv://{p['id']}"


def test_abstract_lookup_matches_arxiv_landing_pages(monkeypatch):
    """OpenAlex merges arXiv preprints into works whose primary DOI can belong
    to another version, so the lookup filters on location landing pages rather
    than the ``/works/doi:`` singleton. No network: fakes the HTTP layer."""
    from gpd.mcp.servers import arxiv_translators  # type: ignore

    work = {
        "id": "https://openalex.org/W2041262588",
        "title": "String Junctions and Their Duals in Heterotic String Theory",
        "authorships": [{"author": {"display_name": "A"}}],
        "abstract_inverted_index": {"hello": [0], "world": [1]},
        "publication_date": "1999-01-04",
        "concepts": [{"display_name": "physics"}],
    }
    calls = []

    def fake_get(path, params=None):
        calls.append((path, params))
        return 200, {"meta": {"count": 1}, "results": [work]}, ""

    monkeypatch.setattr(arxiv_translators, "_http_get", fake_get)
    res = arxiv_translators.openalex_abstract({"paper_id": "hep-th/9901001v2"})

    assert res["status"] == "success"
    assert res["paper_id"] == "hep-th/9901001"
    assert res["abstract"] == arxiv_translators.EXTERNAL_CONTENT_PREFIX + "hello world"
    assert res["pdf_url"] == "https://arxiv.org/pdf/hep-th/9901001"
    assert calls == [
        (
            "/works",
            {
                "filter": (
                    "locations.landing_page_url:https://doi.org/10.48550/arxiv.hep-th/9901001"
                    "|http://arxiv.org/abs/hep-th/9901001"
                ),
                "per_page": 1,
            },
        )
    ]


def test_abstract_lookup_falls_back_to_doi_singleton(monkeypatch):
    """With no landing-page match the lookup tries the DOI singleton and
    reports its HTTP status. No network: fakes the HTTP layer."""
    from gpd.mcp.servers import arxiv_translators  # type: ignore

    calls = []

    def fake_get(path, params=None):
        calls.append(path)
        if path == "/works":
            return 200, {"meta": {"count": 0}, "results": []}, ""
        return 404, None, "not found"

    monkeypatch.setattr(arxiv_translators, "_http_get", fake_get)
    res = arxiv_translators.openalex_abstract({"paper_id": "2401.12345"})

    assert res["status"] == "error"
    assert "HTTP 404" in res["message"]
    assert calls == ["/works", "/works/doi:10.48550%2Farxiv.2401.12345"]


def _work(arxiv_id: str) -> dict:
    return {
        "id": f"https://openalex.org/W{abs(hash(arxiv_id)) % 10**9}",
        "title": f"T {arxiv_id}",
        "authorships": [{"author": {"display_name": "A"}}],
        "abstract_inverted_index": {"x": [0]},
        "locations": [{"landing_page_url": f"http://arxiv.org/abs/{arxiv_id}"}],
        "publication_date": "2024-01-01",
        "concepts": [],
    }


def test_search_uses_any_arxiv_location_and_phrase_first(monkeypatch):
    """A plain multi-word query is searched as a quoted phrase over works with
    any arXiv location; works whose title or abstract holds all its words then
    fill the remaining slots without duplicates. No network: fakes the HTTP
    layer."""
    from gpd.mcp.servers import arxiv_translators  # type: ignore

    calls = []

    def fake_get(path, params=None):
        calls.append(dict(params))
        if "search" in params:
            return 200, {"results": [_work("2008.08601")]}, ""
        return 200, {"results": [_work("2008.08601"), _work("2112.04527")]}, ""

    monkeypatch.setattr(arxiv_translators, "_http_get", fake_get)
    res = arxiv_translators.openalex_search({"query": "neural network field theory", "max_results": 5})

    on_arxiv = f"locations.source.id:{arxiv_translators.OPENALEX_ARXIV_SOURCE_ID}"
    assert [p["id"] for p in res["papers"]] == ["2008.08601", "2112.04527"]
    assert res["total_results"] == 2
    assert calls == [
        {"search": '"neural network field theory"', "filter": on_arxiv, "per-page": 5},
        {
            "filter": f"title_and_abstract.search:neural network field theory,{on_arxiv}",
            "sort": "relevance_score:desc",
            "per-page": 5,
        },
    ]


def test_search_fill_keeps_commas_out_of_the_filter_and_retries_without_arxiv_filter(monkeypatch):
    """Commas separate OpenAlex filters; a rejected filter (400) is retried
    without the arXiv clause, keeping the title-and-abstract search."""
    from gpd.mcp.servers import arxiv_translators  # type: ignore

    calls = []

    def fake_get(path, params=None):
        calls.append(dict(params))
        if "search" in params:
            return 200, {"results": []}, ""
        if "locations.source.id" in params["filter"]:
            return 400, None, "bad filter"
        return 200, {"results": [_work("2401.12345")]}, ""

    monkeypatch.setattr(arxiv_translators, "_http_get", fake_get)
    res = arxiv_translators.openalex_search({"query": "anharmonic oscillator, large N", "max_results": 5})

    assert [p["id"] for p in res["papers"]] == ["2401.12345"]
    assert calls[1]["filter"].startswith("title_and_abstract.search:anharmonic oscillator large N,")
    assert calls[2] == {
        "filter": "title_and_abstract.search:anharmonic oscillator large N",
        "sort": "relevance_score:desc",
        "per-page": 5,
    }


@pytest.mark.parametrize("query", ['"reflection positivity"', "sphaleron", "lattice AND QCD", "(a b) c"])
def test_search_sends_quoted_boolean_and_single_word_queries_once(monkeypatch, query):
    from gpd.mcp.servers import arxiv_translators  # type: ignore

    calls = []

    def fake_get(path, params=None):
        calls.append(params["search"])
        return 200, {"results": [_work("2401.12345")]}, ""

    monkeypatch.setattr(arxiv_translators, "_http_get", fake_get)
    arxiv_translators.openalex_search({"query": query, "max_results": 5})
    assert calls == [query]


def test_search_stops_when_openalex_budget_is_exhausted(monkeypatch):
    """HTTP 429 means the daily budget is spent; the second query would only
    spend more, so the translator returns what it has and the bridge falls
    back to arXiv."""
    from gpd.mcp.servers import arxiv_translators  # type: ignore

    calls = []

    def fake_get(path, params=None):
        calls.append(params["search"])
        return 429, {"error": "Rate limit exceeded"}, ""

    monkeypatch.setattr(arxiv_translators, "_http_get", fake_get)
    res = arxiv_translators.openalex_search({"query": "neural network field theory"})
    assert res["papers"] == [] and res["total_results"] == 0
    assert "frequently_cited" not in res
    assert calls == ['"neural network field theory"']


@pytest.mark.parametrize("key", ["", "test-key"])
def test_openalex_api_key_is_sent_as_bearer_token(monkeypatch, key):
    from gpd.mcp.servers import arxiv_translators  # type: ignore

    seen = {}

    class FakeResponse:
        status_code = 200
        text = "{}"

        def json(self):
            return {"results": []}

    def fake_httpx_get(url, params=None, headers=None, timeout=None):
        seen.update(headers or {})
        return FakeResponse()

    if key:
        monkeypatch.setenv(arxiv_translators.OPENALEX_API_KEY_ENV, key)
    else:
        monkeypatch.delenv(arxiv_translators.OPENALEX_API_KEY_ENV, raising=False)
    monkeypatch.setattr(arxiv_translators, "_keychain_api_key", lambda: "")
    monkeypatch.setattr(arxiv_translators.httpx, "get", fake_httpx_get)
    arxiv_translators._http_get("/works", {"search": "x"})

    if key:
        assert seen["Authorization"] == f"Bearer {key}"
    else:
        assert "Authorization" not in seen
    assert "psi.inc" not in seen["User-Agent"]


def test_environment_key_wins_without_reading_the_keychain(monkeypatch):
    from gpd.mcp.servers import arxiv_translators  # type: ignore

    def no_subprocess(*args, **kwargs):
        raise AssertionError("the Keychain must not be read when the variable is set")

    arxiv_translators._keychain_api_key.cache_clear()
    monkeypatch.setenv(arxiv_translators.OPENALEX_API_KEY_ENV, "env-key")
    monkeypatch.setattr(arxiv_translators.subprocess, "run", no_subprocess)
    assert arxiv_translators.openalex_api_key() == "env-key"


def test_keychain_supplies_the_key_on_macos(monkeypatch):
    """GPD's own Keychain item is tried first, then ORP's; the first hit wins."""
    import subprocess as _subprocess

    from gpd.mcp.servers import arxiv_translators  # type: ignore

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd[cmd.index("-s") + 1], cmd[cmd.index("-a") + 1]))
        if cmd[cmd.index("-s") + 1] == "orp.secret.openalex":
            return _subprocess.CompletedProcess(cmd, 0, stdout="keychain-key\n", stderr="")
        return _subprocess.CompletedProcess(cmd, 44, stdout="", stderr="not found")

    arxiv_translators._keychain_api_key.cache_clear()
    monkeypatch.delenv(arxiv_translators.OPENALEX_API_KEY_ENV, raising=False)
    monkeypatch.setattr(arxiv_translators.sys, "platform", "darwin")
    monkeypatch.setattr(arxiv_translators.shutil, "which", lambda name: "/usr/bin/security")
    monkeypatch.setattr(arxiv_translators.subprocess, "run", fake_run)
    try:
        assert arxiv_translators.openalex_api_key() == "keychain-key"
        assert calls == list(arxiv_translators.KEYCHAIN_ITEMS)
    finally:
        arxiv_translators._keychain_api_key.cache_clear()


def test_keychain_is_not_consulted_off_macos(monkeypatch):
    from gpd.mcp.servers import arxiv_translators  # type: ignore

    def no_subprocess(*args, **kwargs):
        raise AssertionError("no Keychain lookup outside macOS")

    arxiv_translators._keychain_api_key.cache_clear()
    monkeypatch.delenv(arxiv_translators.OPENALEX_API_KEY_ENV, raising=False)
    monkeypatch.setattr(arxiv_translators.sys, "platform", "linux")
    monkeypatch.setattr(arxiv_translators.subprocess, "run", no_subprocess)
    try:
        assert arxiv_translators.openalex_api_key() == ""
    finally:
        arxiv_translators._keychain_api_key.cache_clear()


def _cited_work(openalex_id: str, arxiv_id: str, refs: list[str]) -> dict:
    return {
        "id": f"https://openalex.org/{openalex_id}",
        "title": f"T {arxiv_id}",
        "authorships": [],
        "abstract_inverted_index": {"x": [0]},
        "locations": [{"landing_page_url": f"http://arxiv.org/abs/{arxiv_id}"}],
        "publication_date": "2020-01-01",
        "concepts": [],
        "referenced_works": [f"https://openalex.org/{r}" for r in refs],
    }


def test_search_adds_frequently_cited_references(monkeypatch):
    """Works cited by at least two results are listed with how many results
    cite them; the results themselves are skipped; a work without an arXiv
    location keeps its DOI. No network: fakes the HTTP layer."""
    from gpd.mcp.servers import arxiv_translators  # type: ignore

    results = [
        _cited_work("W1", "2001.00001", ["R1", "R2"]),
        _cited_work("W2", "2001.00002", ["R1", "R3"]),
        _cited_work("W3", "2001.00003", ["R1", "R2", "W1"]),
        _cited_work("W4", "2001.00004", ["R2", "W1"]),
    ]
    resolved = [
        {"id": "https://openalex.org/R1", "title": "Bulk locality", "publication_year": 2014,
         "locations": [{"landing_page_url": "http://arxiv.org/abs/1411.7041"}]},
        {"id": "https://openalex.org/R2", "title": "Journal only", "publication_year": 1999,
         "doi": "https://doi.org/10.1000/x", "locations": []},
    ]
    calls = []

    def fake_get(path, params=None):
        calls.append(dict(params))
        if "search" in params:
            return 200, {"results": results}, ""
        return 200, {"results": resolved}, ""

    monkeypatch.setattr(arxiv_translators, "_http_get", fake_get)
    res = arxiv_translators.openalex_search({"query": "sphaleron", "max_results": 10})

    assert [p["id"] for p in res["papers"]] == ["2001.00001", "2001.00002", "2001.00003", "2001.00004"]
    assert all(set(p) == UPSTREAM_PAPER_KEYS for p in res["papers"])
    assert res["frequently_cited"] == [
        {"id": "1411.7041", "title": "Bulk locality", "year": 2014, "cited_by_results": 3,
         "url": "https://arxiv.org/abs/1411.7041"},
        {"id": "", "title": "Journal only", "year": 1999, "cited_by_results": 3, "url": "https://doi.org/10.1000/x"},
    ]
    assert res["frequently_cited_note"] == arxiv_translators.FREQUENTLY_CITED_NOTE
    assert calls[1]["filter"] == "openalex:R1|R2"


def test_frequently_cited_is_omitted_when_the_lookup_fails(monkeypatch):
    from gpd.mcp.servers import arxiv_translators  # type: ignore

    results = [_cited_work(f"W{n}", f"2001.0000{n}", ["R1"]) for n in range(1, 4)]

    def fake_get(path, params=None):
        if "search" in params:
            return 200, {"results": results}, ""
        return 429, {"error": "Rate limit exceeded"}, ""

    monkeypatch.setattr(arxiv_translators, "_http_get", fake_get)
    res = arxiv_translators.openalex_search({"query": "sphaleron"})
    assert len(res["papers"]) == 3
    assert "frequently_cited" not in res


def _fake_citation_http(work, lists, fail=None):
    """OpenAlex stand-in: the landing-page lookup returns ``work``; list
    filters (``cited_by:``/``cites:``) return ``lists[prefix]`` as
    ``(count, results)``; a prefix in ``fail`` answers with that status."""
    calls = []

    def fake_get(path, params=None):
        calls.append(dict(params))
        flt = params.get("filter", "")
        if flt.startswith("locations.landing_page_url:"):
            return 200, {"results": [work] if work else []}, ""
        prefix = flt.split(":", 1)[0]
        if fail and prefix in fail:
            return fail[prefix], None, ""
        count, results = lists[prefix]
        return 200, {"meta": {"count": count}, "results": results}, ""

    return fake_get, calls


def test_citations_list_references_and_citing_works_by_influence(monkeypatch):
    from gpd.mcp.servers import arxiv_translators  # type: ignore

    work = {"id": "https://openalex.org/W9", "title": "Bulk locality", "publication_year": 2014,
            "referenced_works": ["https://openalex.org/R1", "https://openalex.org/R2"]}
    references = [
        {"id": "https://openalex.org/R2", "title": "Classic book", "publication_year": 2000, "cited_by_count": 900,
         "doi": "https://doi.org/10.1/book", "locations": []},
        {"id": "https://openalex.org/R3", "title": "Classic Book", "publication_year": 2010, "cited_by_count": 800,
         "locations": []},
        {"id": "https://openalex.org/R1", "title": "Minor", "publication_year": 2001, "cited_by_count": 5, "locations": []},
    ]
    citing = [{"id": "https://openalex.org/C1", "title": "Replica wormholes", "publication_year": 2019,
               "cited_by_count": 933, "locations": [{"landing_page_url": "http://arxiv.org/abs/1911.12333"}]}]
    fake_get, calls = _fake_citation_http(work, {"cited_by": (64, references), "cites": (801, citing)})
    monkeypatch.setattr(arxiv_translators, "_http_get", fake_get)

    res = arxiv_translators.openalex_citations({"paper_id": "arXiv:1411.7041v2", "max_results": 5})

    assert res["status"] == "success" and res["source"] == "OpenAlex" and res["paper_id"] == "1411.7041"
    assert res["references_total"] == 64
    assert [r["title"] for r in res["references"]] == ["Classic book", "Minor"]  # repeated title dropped
    assert res["references"][0]["url"] == "https://doi.org/10.1/book"
    assert res["cited_by_total"] == 801
    assert res["cited_by"] == [{"id": "1911.12333", "title": "Replica wormholes", "year": 2019, "cited_by_count": 933,
                                "url": "https://arxiv.org/abs/1911.12333"}]
    assert [(c["filter"], c["sort"], c["per-page"]) for c in calls[1:]] == [
        ("cited_by:W9", "cited_by_count:desc", 15),
        ("cites:W9", "cited_by_count:desc", 15),
    ]


def test_citations_recent_order_single_direction_and_missing_reference_list(monkeypatch):
    from gpd.mcp.servers import arxiv_translators  # type: ignore

    work = {"id": "https://openalex.org/W9", "title": "T", "referenced_works": []}
    fake_get, calls = _fake_citation_http(work, {"cites": (0, [])})
    monkeypatch.setattr(arxiv_translators, "_http_get", fake_get)
    res = arxiv_translators.openalex_citations({"paper_id": "1411.7041", "direction": "cited_by", "order": "recent"})
    assert "references" not in res
    assert calls[-1]["sort"] == "publication_date:desc"

    calls.clear()
    res = arxiv_translators.openalex_citations({"paper_id": "1411.7041", "direction": "references"})
    assert res["references"] == [] and res["references_total"] == 0
    assert len(calls) == 1  # no list request without a reference list


def test_citations_report_a_failed_list_and_unindexed_papers(monkeypatch):
    from gpd.mcp.servers import arxiv_translators  # type: ignore

    work = {"id": "https://openalex.org/W9", "title": "T", "referenced_works": ["https://openalex.org/R1"]}
    fake_get, _ = _fake_citation_http(work, {"cited_by": (1, [])}, fail={"cites": 429})
    monkeypatch.setattr(arxiv_translators, "_http_get", fake_get)
    res = arxiv_translators.openalex_citations({"paper_id": "1411.7041"})
    assert res["status"] == "success" and "cited_by" not in res
    assert "rate limit" in res["cited_by_error"]

    fake_get, _ = _fake_citation_http(None, {})
    monkeypatch.setattr(arxiv_translators, "_http_get", fake_get)
    assert arxiv_translators.openalex_citations({"paper_id": "2609.20001"})["status"] == "not_indexed"


@pytest.mark.parametrize(
    "args, message",
    [({}, "paper_id"), ({"paper_id": "1411.7041 OR title:x"}, "paper_id"),
     ({"paper_id": "1411.7041", "direction": "up"}, "direction"),
     ({"paper_id": "1411.7041", "order": "random"}, "order")],
)
def test_citations_reject_bad_arguments(args, message):
    from gpd.mcp.servers import arxiv_translators  # type: ignore

    res = arxiv_translators.openalex_citations(args)
    assert res["status"] == "error" and message in res["message"]
