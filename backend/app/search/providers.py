from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from app.core.config import settings
from app.search.security import fetch_with_safe_redirects


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


class SearchProvider(ABC):
    name: str

    @abstractmethod
    async def search(self, query: str, limit: int) -> list[SearchResult]:
        raise NotImplementedError


class _DuckDuckGoParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.results: list[SearchResult] = []
        self._href = ""
        self._text: list[str] = []
        self._inside = False

    def handle_starttag(self, tag: str, attrs):
        attributes = dict(attrs)
        if tag == "a" and "result__a" in attributes.get("class", ""):
            self._inside = True
            self._href = attributes.get("href", "")
            self._text = []

    def handle_data(self, data: str):
        if self._inside:
            self._text.append(data)

    def handle_endtag(self, tag: str):
        if tag != "a" or not self._inside:
            return
        href = self._href
        parsed = urlparse(href)
        if parsed.hostname and parsed.hostname.lower().endswith("duckduckgo.com"):
            redirect = parse_qs(parsed.query).get("uddg")
            href = unquote(redirect[0]) if redirect else ""
        title = " ".join("".join(self._text).split())
        if title and href.startswith(("http://", "https://")) and len(href) <= 2000:
            self.results.append(SearchResult(title=title, url=href))
        self._inside = False


class DuckDuckGoSearchProvider(SearchProvider):
    name = "duckduckgo"
    endpoint = "https://html.duckduckgo.com/html/"

    async def search(self, query: str, limit: int) -> list[SearchResult]:
        headers = {"User-Agent": "Mozilla/5.0 LearningAgent/0.4"}
        async with httpx.AsyncClient(timeout=settings.WEB_SEARCH_TIMEOUT_SECONDS, headers=headers) as client:
            response, _ = await fetch_with_safe_redirects(client, self.endpoint, params={"q": query})
        parser = _DuckDuckGoParser()
        parser.feed(response.text)
        unique: list[SearchResult] = []
        seen: set[str] = set()
        for result in parser.results:
            if result.url in seen:
                continue
            seen.add(result.url)
            unique.append(result)
            if len(unique) >= limit:
                break
        return unique


class _BingParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.results: list[SearchResult] = []
        self._in_result = False
        self._is_ad = False
        self._href = ""
        self._title_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs):
        attributes = dict(attrs)
        classes = attributes.get("class", "").split()
        if tag == "li" and "b_algo" in classes:
            self._in_result = True
            self._is_ad = "b_ad" in classes
            self._href = ""
            self._title_parts = []
        elif self._in_result and tag == "a" and not self._href:
            self._href = attributes.get("href", "")

    def handle_data(self, data: str):
        if self._in_result and self._href:
            self._title_parts.append(data)

    def handle_endtag(self, tag: str):
        if tag != "li" or not self._in_result:
            return
        title = " ".join("".join(self._title_parts).split())
        if (
            not self._is_ad
            and title
            and self._href.startswith(("http://", "https://"))
            and len(self._href) <= 2000
        ):
            self.results.append(SearchResult(title=title, url=self._href))
        self._in_result = False


class BingSearchProvider(SearchProvider):
    name = "bing"
    endpoint = "https://www.bing.com/search"

    async def search(self, query: str, limit: int) -> list[SearchResult]:
        headers = {"User-Agent": "Mozilla/5.0 LearningAgent/0.5"}
        async with httpx.AsyncClient(timeout=settings.WEB_SEARCH_TIMEOUT_SECONDS, headers=headers) as client:
            response, _ = await fetch_with_safe_redirects(client, self.endpoint, params={"q": query})
        parser = _BingParser()
        parser.feed(response.text)
        unique: list[SearchResult] = []
        seen: set[str] = set()
        for result in parser.results:
            if result.url in seen:
                continue
            seen.add(result.url)
            unique.append(result)
            if len(unique) >= limit:
                break
        return unique


def get_search_provider(name: str) -> SearchProvider:
    providers: dict[str, type[SearchProvider]] = {
        "duckduckgo": DuckDuckGoSearchProvider,
        "bing": BingSearchProvider,
    }
    provider = providers.get(name.lower())
    if provider is None:
        raise ValueError(f"Unsupported search provider: {name}")
    return provider()
