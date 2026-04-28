"""Local ``web_fetch`` / ``web_search`` backing the ``mcp__pip__web_*`` tools.

Both Claude Code's ``WebFetch`` and ``WebSearch`` are shadowed via
``disallowed_tools`` (see :mod:`pip_agent.agent_runner`) — the
upstream gateway Pip-Boy is pointed at rejects the experimental-betas
header those tools require, so we ship our own.

Implementations:

* :func:`fetch_url` — HTTP GET with redirects, 30 s timeout, 5 MB cap.
  HTML is reduced to article-body markdown via trafilatura; JSON /
  plain-text / XML pass through verbatim; binaries are refused.
* :func:`search_web` — Tavily first (when ``TAVILY_API_KEY`` is set),
  falling back to DuckDuckGo (``ddgs`` library, no key required) when
  Tavily is unconfigured, rate-limited, or erroring. Returns a uniform
  result shape regardless of which provider answered.

Both return ``{"ok": False, "error": ...}`` on failure rather than
raising — callers surface the string to the model instead of crashing
the turn. Both are wrapped in :func:`pip_agent._profile.span` so each
call shows up as a ``web.fetch`` / ``web.search`` row in profile
traces.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

log = logging.getLogger(__name__)


_DEFAULT_TIMEOUT_S: float = 30.0
# Response body cap (bytes). 5 MB covers article-shaped pages with
# generous headroom while still rejecting accidental hits on large
# binaries (PDFs, images, archives) that would balloon the model's
# context if extraction silently passed them through.
_MAX_RESPONSE_BYTES: int = 5 * 1024 * 1024

# Default character cap for the *returned* extracted content. Callers
# can override per-call via ``max_chars``. Picked to fit comfortably
# inside a single tool result without dominating the model's context
# — long pages still come back with the head intact and a
# ``truncated`` flag so the model knows to ask for less or follow up.
_DEFAULT_MAX_CHARS: int = 50_000

# A real-browser-ish UA. A handful of CDNs (Cloudflare's challenge
# pages, some e-commerce front-ends) hard-block the bare
# ``python-httpx`` UA; we are not trying to evade bot detection, just
# to look like a normal HTTP client so plain article pages work.
_DEFAULT_USER_AGENT: str = (
    "Mozilla/5.0 (compatible; Pip-Boy/1.0; "
    "+https://github.com/ByeDream/Pip-Boy)"
)

# Content types that round-trip as-is (the model reads them directly,
# no extraction needed). Anything else falls through to either the
# HTML-extract branch or the binary-refuse branch.
_PASSTHROUGH_PREFIXES: tuple[str, ...] = (
    "application/json",
    "application/ld+json",
    "application/xml",
    "text/plain",
    "text/markdown",
    "text/csv",
    "text/xml",
    "text/x-",
)
_HTML_TYPES: tuple[str, ...] = (
    "text/html",
    "application/xhtml+xml",
)


async def fetch_url(
    url: str,
    *,
    max_chars: int = _DEFAULT_MAX_CHARS,
    timeout: float = _DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    """Fetch ``url`` and return either extracted text or an error dict.

    Parameters
    ----------
    url:
        Absolute http(s) URL to GET.
    max_chars:
        Maximum characters of the *returned* content string. The full
        body is always downloaded and counted against
        :data:`_MAX_RESPONSE_BYTES`; ``max_chars`` only trims the
        post-extraction text the model sees.
    timeout:
        Total request timeout in seconds. Includes connect, read, and
        write phases — matches httpx's default timeout semantics.

    Returns
    -------
    dict
        Success::

            {
                "ok": True,
                "url": <final-url-after-redirects>,
                "status": <int>,
                "content_type": <str>,
                "content": <str>,
                "truncated": <bool>,
            }

        Failure::

            {
                "ok": False,
                "error": <str>,
                "url": <url>,
                "status": <int | None>,
            }
    """
    from pip_agent import _profile  # PROFILE

    async with _profile.span("web.fetch", url=url):
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=timeout,
                headers={"User-Agent": _DEFAULT_USER_AGENT},
            ) as client:
                resp = await client.get(url)
        except httpx.TimeoutException as exc:
            return {
                "ok": False,
                "error": f"timeout after {timeout}s: {exc}",
                "url": url,
                "status": None,
            }
        except httpx.HTTPError as exc:
            return {
                "ok": False,
                "error": f"http error: {exc}",
                "url": url,
                "status": None,
            }
        except Exception as exc:  # noqa: BLE001
            # Defensive: httpx wraps most failures in HTTPError, but
            # we don't want a stray DNS/SSL/proxy edge case to escape
            # as an unhandled exception that crashes the agent turn.
            return {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "url": url,
                "status": None,
            }

        final_url = str(resp.url)
        status = resp.status_code
        content_type = (
            resp.headers.get("content-type", "")
            .split(";", 1)[0]
            .strip()
            .lower()
        )

        if status >= 400:
            return {
                "ok": False,
                "error": f"HTTP {status}",
                "url": final_url,
                "status": status,
            }

        body_bytes = resp.content
        if len(body_bytes) > _MAX_RESPONSE_BYTES:
            return {
                "ok": False,
                "error": (
                    f"response too large ({len(body_bytes)} bytes > "
                    f"{_MAX_RESPONSE_BYTES} cap)"
                ),
                "url": final_url,
                "status": status,
            }

        content = _select_content(content_type, resp.text)
        if content is None:
            return {
                "ok": False,
                "error": (
                    f"unsupported content type {content_type!r} "
                    "(non-text payload)"
                ),
                "url": final_url,
                "status": status,
            }

        truncated = False
        if len(content) > max_chars:
            content = content[:max_chars]
            truncated = True

        return {
            "ok": True,
            "url": final_url,
            "status": status,
            "content_type": content_type,
            "content": content,
            "truncated": truncated,
        }


def _select_content(content_type: str, text: str) -> str | None:
    """Pick the right rendering for a given Content-Type.

    Returns ``None`` for content types we won't pass to the model
    (binaries, images, archives) — the caller turns this into an
    error response. HTML is sent through trafilatura; passthrough
    types come back verbatim. Anything else that *looks* textual
    (empty content type, ``text/*`` not in our allowlist) is also
    passed through verbatim — refusing it would be paternalistic.
    """
    if content_type in _HTML_TYPES:
        extracted = _extract_html(text)
        # Extraction can return None on pages with no detectable main
        # content (login walls, JS-only SPAs). Falling back to the raw
        # response is noisy but better than a hard refusal — the model
        # can decide what to do with it.
        return extracted if extracted else text

    if any(content_type.startswith(p) for p in _PASSTHROUGH_PREFIXES):
        return text

    # Empty / unknown content type but the body is already a string
    # (httpx decoded it as text per its own heuristic) → trust the
    # body and pass it through. This covers servers that omit the
    # header or send something idiosyncratic.
    if not content_type or content_type.startswith("text/"):
        return text

    return None


def _extract_html(html: str) -> str | None:
    """Reduce ``html`` to article-body markdown via trafilatura.

    Returns ``None`` when extraction yields nothing usable — caller
    decides whether to fall back to the raw HTML string or surface
    an error.
    """
    try:
        import trafilatura
    except ImportError:
        log.warning("trafilatura not installed; HTML extraction disabled")
        return None

    try:
        return trafilatura.extract(
            html,
            output_format="markdown",
            include_links=True,
            include_images=False,
            include_tables=True,
            favor_recall=True,
        )
    except Exception as exc:  # noqa: BLE001
        # trafilatura is generally robust but lxml occasionally chokes
        # on adversarial HTML; never let an extraction crash kill the
        # turn — fall back to the raw response.
        log.warning("trafilatura.extract failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Web search — Tavily first, DuckDuckGo fallback
# ---------------------------------------------------------------------------

_TAVILY_URL: str = "https://api.tavily.com/search"
_SEARCH_TIMEOUT_S: float = 15.0
_DEFAULT_MAX_RESULTS: int = 5


async def search_web(
    query: str,
    *,
    max_results: int = _DEFAULT_MAX_RESULTS,
    timeout: float = _SEARCH_TIMEOUT_S,
) -> dict[str, Any]:
    """Search the web via Tavily (preferred) or DuckDuckGo (fallback).

    Parameters
    ----------
    query:
        Free-text search query.
    max_results:
        Maximum number of result items to return. Providers are asked
        for exactly this many; fewer may come back if the upstream
        returns a short list.
    timeout:
        Per-provider request timeout in seconds.

    Returns
    -------
    dict
        Success::

            {
                "ok": True,
                "provider": "tavily" | "duckduckgo",
                "query": <str>,
                "results": [
                    {"title": <str>, "url": <str>, "snippet": <str>},
                    ...
                ],
            }

        Failure::

            {
                "ok": False,
                "error": <str>,
                "provider": <last-attempted or None>,
            }
    """
    from pip_agent import _profile  # PROFILE

    q = (query or "").strip()
    if not q:
        return {"ok": False, "error": "empty query", "provider": None}

    async with _profile.span("web.search", query=q):
        errors: list[str] = []
        api_key = os.getenv("TAVILY_API_KEY", "").strip()

        if api_key:
            tav = await _tavily_search(
                q, api_key=api_key, max_results=max_results, timeout=timeout,
            )
            if tav.get("ok"):
                return tav
            errors.append(f"tavily: {tav.get('error', 'unknown')}")

        ddg = await _ddg_search(q, max_results=max_results)
        if ddg.get("ok"):
            return ddg
        errors.append(f"duckduckgo: {ddg.get('error', 'unknown')}")

        return {
            "ok": False,
            "error": "; ".join(errors) if errors else "no provider available",
            "provider": "duckduckgo",
        }


async def _tavily_search(
    query: str, *, api_key: str, max_results: int, timeout: float,
) -> dict[str, Any]:
    """One Tavily call. Any failure returns ``{"ok": False, "error": ...}``
    so the outer ``search_web`` can decide whether to fall back."""
    body = {
        "api_key": api_key,
        "query": query,
        "max_results": max(1, min(max_results, 20)),
        "search_depth": "basic",
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(_TAVILY_URL, json=body)
    except httpx.TimeoutException as exc:
        return {"ok": False, "error": f"timeout after {timeout}s: {exc}"}
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"http error: {exc}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    if resp.status_code >= 400:
        # Surface a compact error message. Tavily puts the reason under
        # ``detail`` for auth / quota / validation errors; fall back to
        # the bare status line when the body isn't JSON.
        detail: str = f"HTTP {resp.status_code}"
        try:
            data = resp.json()
            if isinstance(data, dict):
                detail_msg = data.get("detail") or data.get("error") or ""
                if detail_msg:
                    detail = f"HTTP {resp.status_code}: {detail_msg}"
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "error": detail}

    try:
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"bad json: {exc}"}

    raw_results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(raw_results, list):
        return {"ok": False, "error": "unexpected response shape (no results)"}

    results: list[dict[str, str]] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        results.append({
            "title": str(item.get("title") or ""),
            "url": str(item.get("url") or ""),
            "snippet": str(item.get("content") or ""),
        })
    return {
        "ok": True,
        "provider": "tavily",
        "query": query,
        "results": results,
    }


async def _ddg_search(query: str, *, max_results: int) -> dict[str, Any]:
    """DuckDuckGo fallback via the :mod:`ddgs` library.

    The library is sync-only; we dispatch to a thread so the host event
    loop keeps serving other turns. Any import / runtime failure is
    captured and returned as an ``error`` string rather than raised.
    """
    import asyncio

    try:
        from ddgs import DDGS  # type: ignore[import-untyped]
    except ImportError:
        return {
            "ok": False,
            "error": (
                "'ddgs' not installed (pip install ddgs); "
                "DuckDuckGo fallback unavailable"
            ),
        }

    def _run_sync() -> list[dict[str, Any]]:
        # ``DDGS`` is a context manager in recent versions; ``text``
        # returns an iterable of dicts with ``title`` / ``href`` /
        # ``body`` keys. We materialise it into a list inside the
        # thread so the iterator's lifetime is bounded.
        with DDGS() as ddgs:
            return list(ddgs.text(query, max_results=max(1, min(max_results, 20))))

    try:
        raw_results = await asyncio.to_thread(_run_sync)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    results: list[dict[str, str]] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        results.append({
            "title": str(item.get("title") or ""),
            "url": str(item.get("href") or item.get("url") or ""),
            "snippet": str(item.get("body") or ""),
        })
    return {
        "ok": True,
        "provider": "duckduckgo",
        "query": query,
        "results": results,
    }
