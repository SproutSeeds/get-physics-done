"""Citation lists combined from OpenAlex and INSPIRE-HEP."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from gpd.mcp.servers import _arxiv_citations, arxiv_translators


def _record(recid, title, *, arxiv=None, doi=None, cites=0, date="2020-01-01"):
    metadata = {"control_number": recid, "titles": [{"title": title}], "citation_count": cites, "earliest_date": date}
    if arxiv:
        metadata["arxiv_eprints"] = [{"value": arxiv}]
    if doi:
        metadata["dois"] = [{"value": doi}]
    return {"metadata": metadata}


def _found(*records, total=None):
    return 200, {"hits": {"total": len(records) if total is None else total, "hits": list(records)}}


def _fake_inspire(monkeypatch, lookup, lists=None, fail=None):
    """INSPIRE stand-in: ``arxiv:`` lookups answer ``lookup``; ``citedby:`` and
    ``refersto:`` queries answer from ``lists`` unless ``fail`` names them."""
    calls = []

    def fake_get(params):
        calls.append(dict(params))
        kind = params["q"].split(":", 1)[0]
        if kind == "arxiv":
            return lookup
        if fail and kind in fail:
            return fail[kind], None
        return lists[kind]

    monkeypatch.setattr(_arxiv_citations, "_inspire_get", fake_get)
    return calls


NNFT = _record(2675173, "Neural network field theories", arxiv="2307.03223", cites=42, date="2023-07-06")


def test_inspire_lists_references_and_citing_papers(monkeypatch):
    references = [
        _record(1, "Deep learning", doi="10.1038/nature14539", cites=1312, date="2015-05-27"),
        _record(2, "Machine learning and the physical sciences", arxiv="1903.10563", cites=1132, date="2019-03-25"),
        _record(3, "Axioms for Euclidean Green's functions", cites=983, date="1973"),
    ]
    citing = [_record(4, "Neural network field theory at finite width", arxiv="2608.21588", cites=1, date="2026-08-21")]
    calls = _fake_inspire(
        monkeypatch, _found(NNFT), {"citedby": _found(*references, total=30), "refersto": _found(*citing, total=42)}
    )

    res = _arxiv_citations.inspire_citations("2307.03223", direction="both", order="recent", limit=3)

    assert (res["status"], res["source"], res["title"], res["year"]) == (
        "success",
        "INSPIRE-HEP",
        "Neural network field theories",
        2023,
    )
    assert res["references_total"] == 30 and res["cited_by_total"] == 42
    assert [r["url"] for r in res["references"]] == [
        "https://doi.org/10.1038/nature14539",
        "https://arxiv.org/abs/1903.10563",
        "https://inspirehep.net/literature/3",
    ]
    assert res["references"][2]["year"] == 1973
    assert res["cited_by"] == [
        {
            "id": "2608.21588",
            "title": "Neural network field theory at finite width",
            "year": 2026,
            "cited_by_count": 1,
            "url": "https://arxiv.org/abs/2608.21588",
        }
    ]
    assert [(c["q"], c.get("sort"), c.get("size")) for c in calls] == [
        ("arxiv:2307.03223", None, 1),
        ("citedby:recid:2675173", "mostcited", 13),
        ("refersto:recid:2675173", "mostrecent", 13),
    ]


def test_inspire_reports_missing_papers_and_failures(monkeypatch):
    _fake_inspire(monkeypatch, _found())
    assert _arxiv_citations.inspire_citations("2609.99999", direction="both", order="influential", limit=5) == {
        "status": "not_indexed",
        "source": "INSPIRE-HEP",
    }

    _fake_inspire(monkeypatch, (429, None))
    res = _arxiv_citations.inspire_citations("1411.7041", direction="both", order="influential", limit=5)
    assert res["status"] == "error" and "rate limit" in res["message"]

    _fake_inspire(monkeypatch, _found(NNFT), {"citedby": _found()}, fail={"refersto": 500})
    res = _arxiv_citations.inspire_citations("2307.03223", direction="both", order="influential", limit=5)
    assert res["references"] == [] and res["references_total"] == 0
    assert "cited_by" not in res and "HTTP 500" in res["cited_by_error"]


def test_inspire_requests_are_paced_and_survive_network_errors(monkeypatch):
    clock, sleeps = [100.0], []
    monkeypatch.setattr(
        _arxiv_citations, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=lambda s: sleeps.append(s))
    )
    monkeypatch.setattr(_arxiv_citations, "_last_request", 0.0)
    answers = [SimpleNamespace(status_code=200, json=lambda: {"hits": {"total": 0, "hits": []}})]

    def fake_get(*_args, **_kwargs):
        if not answers:
            raise httpx.ConnectError("offline")
        return answers.pop()

    monkeypatch.setattr(_arxiv_citations.httpx, "get", fake_get)
    assert _arxiv_citations._inspire_get({"q": "arxiv:1411.7041"})[0] == 200
    assert _arxiv_citations._inspire_get({"q": "arxiv:1411.7041"}) == (0, None)
    assert sleeps == [pytest.approx(0.35)]


def _openalex(**fields):
    return {"status": "success", "source": "OpenAlex", "title": "Bulk locality", "year": 2014, **fields}


def _inspire(**fields):
    return {"status": "success", "source": "INSPIRE-HEP", "title": "Bulk Locality", "year": 2014, **fields}


def test_each_list_comes_from_the_source_with_more_works():
    res = _arxiv_citations.combine_citations(
        "1411.7041",
        "both",
        "influential",
        [
            _openalex(references=[{"title": "A"}], references_total=64, cited_by=[{"title": "B"}], cited_by_total=801),
            _inspire(references=[{"title": "C"}], references_total=48, cited_by=[{"title": "D"}], cited_by_total=885),
        ],
    )
    assert (res["status"], res["title"]) == ("success", "Bulk locality")
    assert (res["references"], res["references_total"], res["references_source"]) == ([{"title": "A"}], 64, "OpenAlex")
    assert (res["cited_by"], res["cited_by_total"], res["cited_by_source"]) == ([{"title": "D"}], 885, "INSPIRE-HEP")
    assert res["cited_by_order"] == "influential"
    assert res["sources"] == {
        "OpenAlex": {"status": "success", "references_total": 64, "cited_by_total": 801},
        "INSPIRE-HEP": {"status": "success", "references_total": 48, "cited_by_total": 885},
    }
    assert "note" in res and "references_note" not in res


def test_a_missing_openalex_reference_list_is_filled_from_inspire():
    res = _arxiv_citations.combine_citations(
        "2307.03223",
        "references",
        "influential",
        [
            _openalex(references=[], references_total=0),
            _inspire(references=[{"title": "Deep learning"}], references_total=30),
        ],
    )
    assert res["references_source"] == "INSPIRE-HEP" and res["references_total"] == 30
    assert "cited_by" not in res and "cited_by_order" not in res


def test_partial_and_failed_answers():
    both_missing = _arxiv_citations.combine_citations(
        "2609.99999",
        "both",
        "recent",
        [{"status": "not_indexed", "source": "OpenAlex"}, {"status": "not_indexed", "source": "INSPIRE-HEP"}],
    )
    assert both_missing["status"] == "not_indexed" and "Neither OpenAlex nor INSPIRE-HEP" in both_missing["message"]

    failed = _arxiv_citations.combine_citations(
        "1411.7041",
        "both",
        "recent",
        [
            {"status": "error", "source": "OpenAlex", "message": "OpenAlex lookup failed (HTTP 429: rate limit)"},
            {"status": "not_indexed", "source": "INSPIRE-HEP"},
        ],
    )
    assert failed["status"] == "error"
    assert "OpenAlex: OpenAlex lookup failed" in failed["message"] and "INSPIRE-HEP: not_indexed" in failed["message"]

    partial = _arxiv_citations.combine_citations(
        "1411.7041",
        "both",
        "recent",
        [
            _openalex(references=[], references_total=0, cited_by_error="OpenAlex cited_by lookup failed (HTTP 500)"),
            {"status": "not_indexed", "source": "INSPIRE-HEP"},
        ],
    )
    assert partial["cited_by"] == [] and "HTTP 500" in partial["cited_by_note"]
    assert "Neither" in partial["references_note"]


def test_paper_citations_asks_both_sources(monkeypatch):
    requests = []
    monkeypatch.setattr(
        arxiv_translators,
        "openalex_citations",
        lambda args: requests.append(args) or {"status": "not_indexed", "source": "OpenAlex", "paper_id": "2307.03223"},
    )
    _fake_inspire(monkeypatch, _found(NNFT), {"citedby": _found(total=0), "refersto": _found(total=0)})

    res = _arxiv_citations.paper_citations({"paper_id": "arXiv:2307.03223v2", "direction": "cited_by"})

    assert requests == [{"paper_id": "2307.03223", "direction": "cited_by", "order": "influential", "max_results": 20}]
    assert res["status"] == "success" and res["cited_by_source"] == "INSPIRE-HEP"
    assert res["sources"]["OpenAlex"] == {"status": "not_indexed"}


def test_paper_citations_survives_a_failing_source_and_rejects_bad_ids(monkeypatch):
    def broken(_args):
        raise RuntimeError("boom")

    monkeypatch.setattr(arxiv_translators, "openalex_citations", broken)
    calls = _fake_inspire(monkeypatch, _found(NNFT), {"citedby": _found(total=0), "refersto": _found(total=0)})
    res = _arxiv_citations.paper_citations({"paper_id": "2307.03223"})
    assert res["status"] == "success" and res["sources"]["OpenAlex"] == {"status": "error", "message": "boom"}

    calls.clear()
    bad = _arxiv_citations.paper_citations({"paper_id": "2307.03223 OR title:x"})
    assert bad["status"] == "error" and "arXiv id" in bad["message"] and calls == []
