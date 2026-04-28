"""Unit tests for :mod:`pip_agent.web`.

Strategy: stub :class:`httpx.AsyncClient` inside the ``pip_agent.web``
namespace with one backed by :class:`httpx.MockTransport`, so the real
``fetch_url`` code path runs (URL construction, redirect handling,
charset decoding, status / content-type sniffing, trafilatura
extraction) without any network. Each test installs its own handler
to drive the relevant branch.

Async handlers are exercised via ``asyncio.run`` to match the rest of
the test suite; the project has not opted into pytest-asyncio.
"""

from __future__ import annotations

import asyncio
from typing import Callable

import httpx
import pytest

from pip_agent import web


def _run(coro):
    return asyncio.run(coro)


def _install_mock(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    """Patch ``pip_agent.web.httpx.AsyncClient`` to inject a mock transport.

    The handler runs sync (httpx's MockTransport contract); the rest of
    the async client (timeouts, redirect handling, response decoding)
    is the real httpx code.
    """
    transport = httpx.MockTransport(handler)
    real_client_cls = httpx.AsyncClient

    def factory(**kwargs):
        # Drop any caller-supplied transport (we always inject ours)
        # and forward the rest verbatim — covers timeout, headers,
        # follow_redirects.
        kwargs.pop("transport", None)
        return real_client_cls(transport=transport, **kwargs)

    monkeypatch.setattr(web.httpx, "AsyncClient", factory)


_SAMPLE_ARTICLE_HTML = """\
<!doctype html>
<html><head><title>Sample Article</title></head><body>
<header><nav>Site nav</nav></header>
<article>
<h1>The Title</h1>
<p>The first paragraph has enough words for trafilatura to consider this
a real article body and not boilerplate, which is the threshold above
which extraction kicks in.</p>
<p>A second paragraph with more substantive prose so the recall-favouring
extractor has plenty of signal to lock onto.</p>
</article>
<footer>Site footer junk</footer>
</body></html>
"""


# ---------------------------------------------------------------------------
# HTML extraction path
# ---------------------------------------------------------------------------


class TestHtmlExtraction:
    def test_html_is_reduced_to_article_markdown(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                text=_SAMPLE_ARTICLE_HTML,
                headers={"content-type": "text/html; charset=utf-8"},
            )

        _install_mock(monkeypatch, handler)
        result = _run(web.fetch_url("https://example.com/article"))

        assert result["ok"] is True
        assert result["status"] == 200
        assert result["content_type"] == "text/html"
        # Body keeps the article prose; site nav / footer are stripped.
        assert "first paragraph" in result["content"]
        assert "Site nav" not in result["content"]
        assert "Site footer" not in result["content"]
        assert result["truncated"] is False

    def test_extraction_failure_falls_back_to_raw_text(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Tiny page with no detectable main content — trafilatura
        # returns None. We fall back to ``resp.text`` (the raw HTML)
        # rather than refuse, so the model can still see *something*.
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, text="<html><body><p>x</p></body></html>",
                headers={"content-type": "text/html"},
            )

        _install_mock(monkeypatch, handler)
        monkeypatch.setattr(web, "_extract_html", lambda _html: None)

        result = _run(web.fetch_url("https://example.com/empty"))
        assert result["ok"] is True
        assert "<p>x</p>" in result["content"]


# ---------------------------------------------------------------------------
# Passthrough path (JSON / plain text)
# ---------------------------------------------------------------------------


class TestPassthrough:
    def test_json_body_is_returned_verbatim(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        payload = '{"answer": 42, "items": [1, 2, 3]}'

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, text=payload,
                headers={"content-type": "application/json"},
            )

        _install_mock(monkeypatch, handler)
        result = _run(web.fetch_url("https://api.example.com/data"))

        assert result["ok"] is True
        assert result["content_type"] == "application/json"
        assert result["content"] == payload

    def test_plain_text_is_returned_verbatim(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        body = "line one\nline two\nline three\n"

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, text=body, headers={"content-type": "text/plain"},
            )

        _install_mock(monkeypatch, handler)
        result = _run(web.fetch_url("https://example.com/log.txt"))

        assert result["ok"] is True
        assert result["content"] == body

    def test_unknown_text_subtype_passes_through(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # ``text/yaml`` isn't in our explicit allowlist but starts with
        # ``text/`` — refusing it would be paternalistic, so we pass
        # the body through verbatim.
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, text="key: value\n",
                headers={"content-type": "text/yaml"},
            )

        _install_mock(monkeypatch, handler)
        result = _run(web.fetch_url("https://example.com/conf.yaml"))

        assert result["ok"] is True
        assert "key: value" in result["content"]

    def test_binary_content_type_is_refused(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, content=b"\x89PNG\r\n\x1a\n",
                headers={"content-type": "image/png"},
            )

        _install_mock(monkeypatch, handler)
        result = _run(web.fetch_url("https://example.com/pic.png"))

        assert result["ok"] is False
        assert "unsupported content type" in result["error"]
        assert "image/png" in result["error"]


# ---------------------------------------------------------------------------
# Error / size / timeout branches
# ---------------------------------------------------------------------------


class TestErrors:
    def test_4xx_surfaces_as_error_with_status(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                404, text="not found",
                headers={"content-type": "text/plain"},
            )

        _install_mock(monkeypatch, handler)
        result = _run(web.fetch_url("https://example.com/missing"))

        assert result["ok"] is False
        assert result["status"] == 404
        assert "HTTP 404" in result["error"]

    def test_5xx_surfaces_as_error_with_status(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        _install_mock(monkeypatch, handler)
        result = _run(web.fetch_url("https://example.com/down"))

        assert result["ok"] is False
        assert result["status"] == 503

    def test_timeout_is_caught(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("connect timeout", request=request)

        _install_mock(monkeypatch, handler)
        result = _run(
            web.fetch_url("https://slow.example.com/", timeout=0.5),
        )

        assert result["ok"] is False
        assert result["status"] is None
        assert "timeout" in result["error"].lower()

    def test_transport_failure_is_caught(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("dns failure", request=request)

        _install_mock(monkeypatch, handler)
        result = _run(web.fetch_url("https://nx.example.com/"))

        assert result["ok"] is False
        assert result["status"] is None
        assert "http error" in result["error"].lower()

    def test_oversized_response_is_refused(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Lower the cap to keep the test cheap — semantics are
        # identical to the production 5 MB limit.
        monkeypatch.setattr(web, "_MAX_RESPONSE_BYTES", 1024)

        big_body = "x" * 5000

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, text=big_body,
                headers={"content-type": "text/plain"},
            )

        _install_mock(monkeypatch, handler)
        result = _run(web.fetch_url("https://big.example.com/"))

        assert result["ok"] is False
        assert "too large" in result["error"]

    def test_unhandled_exception_does_not_crash_caller(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Defensive last-resort branch: a non-httpx error inside
        # ``client.get`` (e.g. a misbehaving SSL stack) must surface
        # as ``ok=False`` instead of bubbling up.
        def handler(_request: httpx.Request) -> httpx.Response:
            raise RuntimeError("unexpected boom")

        _install_mock(monkeypatch, handler)
        result = _run(web.fetch_url("https://oops.example.com/"))

        assert result["ok"] is False
        assert "RuntimeError" in result["error"]


# ---------------------------------------------------------------------------
# Truncation
# ---------------------------------------------------------------------------


class TestTruncation:
    def test_max_chars_trims_returned_content(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        body = "a" * 200

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, text=body, headers={"content-type": "text/plain"},
            )

        _install_mock(monkeypatch, handler)
        result = _run(
            web.fetch_url("https://example.com/long", max_chars=50),
        )

        assert result["ok"] is True
        assert result["truncated"] is True
        assert len(result["content"]) == 50

    def test_under_cap_content_is_not_marked_truncated(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        body = "short"

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, text=body, headers={"content-type": "text/plain"},
            )

        _install_mock(monkeypatch, handler)
        result = _run(
            web.fetch_url("https://example.com/short", max_chars=1000),
        )

        assert result["ok"] is True
        assert result["truncated"] is False
        assert result["content"] == body


# ---------------------------------------------------------------------------
# Web search — Tavily primary, DuckDuckGo fallback
# ---------------------------------------------------------------------------


def _tavily_response(results: list[dict]) -> httpx.Response:
    """Shape a Tavily-like JSON response for tests."""
    return httpx.Response(
        200,
        json={"query": "q", "results": results},
        headers={"content-type": "application/json"},
    )


def _patch_ddg(monkeypatch, results=None, raises=None):
    """Replace :func:`pip_agent.web._ddg_search` with a stub.

    ``results`` → success payload; ``raises`` → failure payload.
    Mutually exclusive — pass one. This is simpler than stubbing the
    ``ddgs`` import (which may not be installed in CI) and lets each
    test drive the fallback branch directly.
    """
    async def fake(query: str, *, max_results: int) -> dict:
        if raises is not None:
            return {"ok": False, "error": raises}
        return {
            "ok": True,
            "provider": "duckduckgo",
            "query": query,
            "results": results or [],
        }

    monkeypatch.setattr("pip_agent.web._ddg_search", fake)


class TestSearchEmptyQuery:
    def test_empty_query_is_error_before_network(self, monkeypatch):
        # No mocks installed — empty query must short-circuit without
        # either provider being touched.
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        result = _run(web.search_web(""))
        assert result["ok"] is False
        assert "empty" in result["error"]

    def test_whitespace_only_query_is_error(self, monkeypatch):
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        result = _run(web.search_web("   \n\t"))
        assert result["ok"] is False


class TestSearchTavilyPath:
    def test_tavily_success_wins_when_key_is_set(self, monkeypatch):
        """With a key configured and a healthy response, Tavily answers
        and DDG is never consulted."""
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-test-key")

        def handler(request: httpx.Request) -> httpx.Response:
            assert str(request.url) == "https://api.tavily.com/search"
            return _tavily_response([
                {
                    "title": "Result One",
                    "url": "https://example.com/1",
                    "content": "snippet one",
                },
                {
                    "title": "Result Two",
                    "url": "https://example.com/2",
                    "content": "snippet two",
                },
            ])

        _install_mock(monkeypatch, handler)

        def _ddg_must_not_fire(*_a, **_kw):
            raise AssertionError("DDG fallback must not be invoked on Tavily success")

        monkeypatch.setattr("pip_agent.web._ddg_search", _ddg_must_not_fire)

        result = _run(web.search_web("hello", max_results=2))

        assert result["ok"] is True
        assert result["provider"] == "tavily"
        assert result["query"] == "hello"
        titles = [r["title"] for r in result["results"]]
        assert titles == ["Result One", "Result Two"]
        # Snippet is normalised from Tavily's ``content`` key.
        assert result["results"][0]["snippet"] == "snippet one"

    def test_tavily_http_error_falls_back_to_ddg(self, monkeypatch):
        """401 / 403 / 429 from Tavily should not poison the search —
        the model still gets results via DuckDuckGo."""
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-bad")

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                429,
                json={"detail": "rate limited"},
                headers={"content-type": "application/json"},
            )

        _install_mock(monkeypatch, handler)
        _patch_ddg(monkeypatch, results=[
            {"title": "From DDG", "url": "https://ddg.example/x", "snippet": "ok"},
        ])

        result = _run(web.search_web("hello"))
        assert result["ok"] is True
        assert result["provider"] == "duckduckgo"
        assert result["results"][0]["title"] == "From DDG"

    def test_tavily_transport_error_falls_back(self, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-x")

        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("network down")

        _install_mock(monkeypatch, handler)
        _patch_ddg(monkeypatch, results=[
            {"title": "Backup", "url": "https://b.example/", "snippet": "s"},
        ])

        result = _run(web.search_web("q"))
        assert result["ok"] is True
        assert result["provider"] == "duckduckgo"


class TestSearchNoKey:
    def test_ddg_is_used_when_key_absent(self, monkeypatch):
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)

        # Tavily must not be called — asserting via a handler that would
        # fail the test if it fired.
        def handler(_request: httpx.Request) -> httpx.Response:
            raise AssertionError("Tavily called despite no TAVILY_API_KEY")

        _install_mock(monkeypatch, handler)
        _patch_ddg(monkeypatch, results=[
            {"title": "D1", "url": "https://d1/", "snippet": "x"},
        ])

        result = _run(web.search_web("q"))
        assert result["ok"] is True
        assert result["provider"] == "duckduckgo"

    def test_blank_key_is_treated_as_unset(self, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "   ")
        _patch_ddg(monkeypatch, results=[
            {"title": "D", "url": "https://d/", "snippet": "x"},
        ])
        result = _run(web.search_web("q"))
        assert result["ok"] is True
        assert result["provider"] == "duckduckgo"


class TestSearchBothFail:
    def test_error_surfaces_both_providers(self, monkeypatch):
        """When Tavily errors and DDG also errors, the returned error
        should mention BOTH so a caller can tell which half broke."""
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-x")

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        _install_mock(monkeypatch, handler)
        _patch_ddg(monkeypatch, raises="ddg rate limited")

        result = _run(web.search_web("q"))
        assert result["ok"] is False
        assert "tavily" in result["error"]
        assert "duckduckgo" in result["error"]
