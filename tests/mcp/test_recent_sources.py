"""Zenodo and OpenAlex sources for recent_papers."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import httpx
import pytest

from gpd.mcp.servers import _recent_sources, arxiv_translators

START = date(2026, 9, 1)


def test_zenodo_searches_first_versions_in_the_window_and_maps_records(monkeypatch):
    seen = []

    def fake_get(params):
        seen.append(params)
        return 200, {"hits": {"total": 1, "hits": [{
            "id": 1, "conceptdoi": "10.5281/zenodo.100", "doi": "10.5281/zenodo.101", "created": "2026-09-20T10:00:00+00:00",
            "metadata": {"title": "Reflection positivity", "creators": [{"name": "Doe, Jane"}, {"affiliation": "x"}],
                         "description": "<p>We prove <b>reflection</b> positivity &amp; more.</p>",
                         "resource_type": {"type": "publication", "subtype": "preprint"}},
        }]}}

    monkeypatch.setattr(_recent_sources, "_zenodo_get", fake_get)
    papers, error = _recent_sources.zenodo_recent('"reflection positivity"', START)

    assert error is None
    assert seen == [{"q": '("reflection positivity") AND created:[2026-09-01 TO *] AND versions.index:1',
                     "sort": "mostrecent", "size": 25, "type": "publication", "allversions": "true"}]
    assert papers == [{
        "id": "10.5281/zenodo.100", "title": "Reflection positivity", "authors": ["Doe, Jane"],
        "abstract": "[EXTERNAL CONTENT] We prove reflection positivity & more.", "categories": [],
        "published": "2026-09-20T10:00:00+00:00", "url": "https://doi.org/10.5281/zenodo.100",
        "source": "Zenodo", "type": "preprint",
    }]


def test_zenodo_reports_failures(monkeypatch):
    monkeypatch.setattr(_recent_sources, "_zenodo_get", lambda params: (429, None))
    papers, error = _recent_sources.zenodo_recent("x", START)
    assert papers == [] and "rate limit" in error


def test_zenodo_requests_are_paced_and_survive_network_errors(monkeypatch):
    clock, sleeps = [100.0], []
    monkeypatch.setattr(_recent_sources, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=lambda s: sleeps.append(s)))
    monkeypatch.setattr(_recent_sources, "_last_request", 0.0)
    answers = [SimpleNamespace(status_code=200, json=lambda: {"hits": {"hits": []}})]

    def fake_get(*_args, **_kwargs):
        if not answers:
            raise httpx.ConnectError("offline")
        return answers.pop()

    monkeypatch.setattr(_recent_sources.httpx, "get", fake_get)
    assert _recent_sources._zenodo_get({"q": "x"})[0] == 200
    assert _recent_sources._zenodo_get({"q": "x"}) == (0, None)
    assert sleeps == [pytest.approx(2.1)]


def _work(wid, title, *, doi=None, source="Physical Review D", arxiv=False, date_="2026-09-10"):
    locations = [{"landing_page_url": "http://arxiv.org/abs/2609.00001"}] if arxiv else []
    return {"id": f"https://openalex.org/{wid}", "doi": doi, "title": title, "publication_date": date_, "type": "article",
            "authorships": [{"author": {"display_name": "A. Author"}}],
            "abstract_inverted_index": {"We": [0], "prove": [1], "it.": [2]},
            "primary_location": {"source": {"display_name": source}, "landing_page_url": "https://example.org/w"},
            "locations": locations}


def test_openalex_leaves_out_arxiv_and_optionally_zenodo_works(monkeypatch):
    seen = []

    def fake_get(path, params=None):
        seen.append(params)
        return 200, {"results": [
            _work("W1", "Journal paper", doi="https://doi.org/10.1103/abc"),
            _work("W2", "Journal version of an arXiv paper", arxiv=True),
            _work("W3", "Zenodo upload", doi="https://doi.org/10.5281/zenodo.5", source="Zenodo (CERN European Organization for Nuclear Research)"),
        ]}, ""

    monkeypatch.setattr(arxiv_translators, "_http_get", fake_get)
    papers, error = _recent_sources.openalex_recent('"reflection positivity", x', START, skip_zenodo=True)

    assert error is None
    assert seen[0]["filter"] == ('title_and_abstract.search:"reflection positivity"  x,from_publication_date:2026-09-01,'
                                 "type:article|preprint|review|letter|book|book-chapter|report|dissertation")
    assert seen[0]["sort"] == "publication_date:desc"
    assert [p["id"] for p in papers] == ["10.1103/abc"]
    assert papers[0] == {"id": "10.1103/abc", "title": "Journal paper", "authors": ["A. Author"],
                         "abstract": "[EXTERNAL CONTENT] We prove it.", "categories": [], "published": "2026-09-10",
                         "url": "https://doi.org/10.1103/abc", "source": "Physical Review D", "type": "article"}

    papers, _ = _recent_sources.openalex_recent("x", START, skip_zenodo=False)
    assert [p["id"] for p in papers] == ["10.1103/abc", "10.5281/zenodo.5"]


def test_openalex_reports_failures(monkeypatch):
    monkeypatch.setattr(arxiv_translators, "_http_get", lambda path, params=None: (0, None, "offline"))
    papers, error = _recent_sources.openalex_recent("x", START, skip_zenodo=True)
    assert papers == [] and "network error" in error
