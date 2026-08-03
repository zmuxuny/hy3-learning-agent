import json

import httpx
import pytest

from app.core.config import settings
from app.db.database import AsyncSessionLocal
from app.models import AgentRun
from app.search.providers import BingSearchProvider, SearchResult
from app.tools import ToolContext, execute_tool


class FailingProvider:
    name = "failing"

    async def search(self, query, limit):
        raise RuntimeError("primary provider is down")


class EmptyProvider:
    name = "duckduckgo"

    async def search(self, query, limit):
        return []


class OkProvider:
    name = "ok"

    async def search(self, query, limit):
        return [SearchResult(title="官方 asyncio 教程", url="https://docs.python.org/3/library/asyncio.html")]


@pytest.mark.asyncio
async def test_web_search_uses_fallback_when_primary_fails(monkeypatch):
    import app.tools.web as web_tools

    monkeypatch.setattr(settings, "WEB_SEARCH_PROVIDER", "primary")
    monkeypatch.setattr(settings, "WEB_SEARCH_FALLBACK_PROVIDER", "bing")
    requested = []

    def fake_get_provider(name):
        requested.append(name)
        return FailingProvider() if name == "primary" else OkProvider()

    monkeypatch.setattr(web_tools, "get_search_provider", fake_get_provider)
    async with AsyncSessionLocal() as db:
        run = AgentRun(owner_id="local", trigger="user_message", objective="搜索")
        db.add(run)
        await db.commit()
        result = await execute_tool(
            "web_search",
            json.dumps({"query": "python asyncio 教程", "limit": 3}),
            ToolContext(db=db, owner_id="local", run_id=run.id, trigger="user_message"),
        )
    assert result["ok"] is True
    assert requested == ["primary", "bing"]
    assert result["data"]["fallback_used"] is True
    assert result["data"]["provider"] == "ok"
    assert result["data"]["results"][0]["url"] == "https://docs.python.org/3/library/asyncio.html"


@pytest.mark.asyncio
async def test_web_search_uses_fallback_when_primary_returns_nothing(monkeypatch):
    import app.tools.web as web_tools

    monkeypatch.setattr(settings, "WEB_SEARCH_PROVIDER", "duckduckgo")
    monkeypatch.setattr(settings, "WEB_SEARCH_FALLBACK_PROVIDER", "bing")

    def fake_get_provider(name):
        return EmptyProvider() if name == "duckduckgo" else OkProvider()

    monkeypatch.setattr(web_tools, "get_search_provider", fake_get_provider)
    async with AsyncSessionLocal() as db:
        run = AgentRun(owner_id="local", trigger="user_message", objective="搜索")
        db.add(run)
        await db.commit()
        result = await execute_tool(
            "web_search",
            json.dumps({"query": "python asyncio"}),
            ToolContext(db=db, owner_id="local", run_id=run.id, trigger="user_message"),
        )
    assert result["ok"] is True
    assert result["data"]["fallback_used"] is True


@pytest.mark.asyncio
async def test_web_search_fails_safely_without_fallback(monkeypatch):
    import app.tools.web as web_tools

    monkeypatch.setattr(settings, "WEB_SEARCH_PROVIDER", "primary")
    monkeypatch.setattr(settings, "WEB_SEARCH_FALLBACK_PROVIDER", "none")
    monkeypatch.setattr(web_tools, "get_search_provider", lambda name: FailingProvider())
    async with AsyncSessionLocal() as db:
        run = AgentRun(owner_id="local", trigger="user_message", objective="搜索")
        db.add(run)
        await db.commit()
        result = await execute_tool(
            "web_search",
            json.dumps({"query": "python asyncio"}),
            ToolContext(db=db, owner_id="local", run_id=run.id, trigger="user_message"),
        )
    assert result["ok"] is False
    assert "primary provider is down" in result["error"]


@pytest.mark.asyncio
async def test_bing_provider_parses_results_and_skips_ads(monkeypatch):
    import app.search.providers as providers

    html = (
        '<li class="b_algo"><h2><a href="https://docs.python.org/3/library/asyncio.html">'
        "Python asyncio documentation</a></h2></li>"
        '<li class="b_algo b_ad"><h2><a href="https://ads.example">Sponsored</a></h2></li>'
        '<li class="b_algo"><h2><a href="https://realpython.com/async-io-python/">Real Python async walkthrough</a></h2></li>'
    )

    async def fake_fetch(client, url, *, params=None):
        request = httpx.Request("GET", url, params=params)
        return httpx.Response(200, text=html, request=request), 0

    monkeypatch.setattr(providers, "fetch_with_safe_redirects", fake_fetch)
    results = await BingSearchProvider().search("asyncio", 5)
    assert [item.url for item in results] == [
        "https://docs.python.org/3/library/asyncio.html",
        "https://realpython.com/async-io-python/",
    ]
