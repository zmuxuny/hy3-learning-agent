from __future__ import annotations

from html.parser import HTMLParser
import asyncio
import ipaddress
import socket
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from pydantic import BaseModel, Field

from app.core.config import settings
from app.models import LearningResource
from app.tools.base import ToolContext, ToolDefinition


class WebSearchArgs(BaseModel):
    query: str = Field(min_length=2, max_length=300)
    limit: int = Field(default=5, ge=1, le=10)
    plan_id: int | None = None
    save_results: bool = False


class WebOpenArgs(BaseModel):
    url: str = Field(min_length=8, max_length=2000)
    max_chars: int = Field(default=12000, ge=500, le=30000)


class _SearchParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.results: list[dict[str, str]] = []
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
        if tag == "a" and self._inside:
            href = self._href
            parsed = urlparse(href)
            if parsed.netloc.endswith("duckduckgo.com"):
                redirect = parse_qs(parsed.query).get("uddg")
                href = unquote(redirect[0]) if redirect else ""
            title = " ".join("".join(self._text).split())
            if title and href.startswith(("http://", "https://")):
                self.results.append({"title": title, "url": href})
            self._inside = False


class _TextParser(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.depth = 0

    def handle_starttag(self, tag: str, attrs):
        if tag in self.SKIP:
            self.depth += 1
        elif tag in {"p", "h1", "h2", "h3", "li", "br"} and not self.depth:
            self.parts.append("\n")

    def handle_endtag(self, tag: str):
        if tag in self.SKIP and self.depth:
            self.depth -= 1

    def handle_data(self, data: str):
        if not self.depth:
            value = " ".join(data.split())
            if value:
                self.parts.append(value)


async def _validate_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Only public HTTP(S) URLs are supported")
    if parsed.hostname in {"localhost", "127.0.0.1", "::1"} or parsed.hostname.endswith(".local"):
        raise ValueError("Local network URLs are not allowed")
    addresses = await asyncio.to_thread(socket.getaddrinfo, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ValueError("Private or reserved network targets are not allowed")


async def web_search(ctx: ToolContext, args: WebSearchArgs) -> dict:
    if ctx.plan_id is not None and args.plan_id not in {None, ctx.plan_id}:
        return {"error": "Plan-focused runs cannot save resources to another plan"}
    headers = {"User-Agent": "Mozilla/5.0 LearningAgent/0.3"}
    async with httpx.AsyncClient(timeout=settings.WEB_SEARCH_TIMEOUT_SECONDS, follow_redirects=True, headers=headers) as client:
        response = await client.get("https://html.duckduckgo.com/html/", params={"q": args.query})
        response.raise_for_status()
    parser = _SearchParser()
    parser.feed(response.text)
    results = parser.results[: args.limit]
    saved_ids: list[int] = []
    if args.save_results:
        target_plan = args.plan_id if args.plan_id is not None else ctx.plan_id
        for result in results:
            resource = LearningResource(
                owner_id=ctx.owner_id,
                plan_id=target_plan,
                title=result["title"],
                url=result["url"],
                resource_type="web",
                summary=f"Search result for: {args.query}",
                source="web_search",
            )
            ctx.db.add(resource)
            await ctx.db.flush()
            saved_ids.append(resource.id)
        await ctx.db.commit()
    return {"query": args.query, "results": results, "saved_resource_ids": saved_ids}


async def web_open(_: ToolContext, args: WebOpenArgs) -> dict:
    await _validate_url(args.url)
    headers = {"User-Agent": "Mozilla/5.0 LearningAgent/0.3"}
    async with httpx.AsyncClient(timeout=settings.WEB_SEARCH_TIMEOUT_SECONDS, follow_redirects=True, headers=headers) as client:
        response = await client.get(args.url)
        response.raise_for_status()
    content_type = response.headers.get("content-type", "")
    if not any(kind in content_type for kind in ("text/", "json", "xml")):
        return {"error": f"Unsupported content type: {content_type}"}
    parser = _TextParser()
    parser.feed(response.text)
    text = " ".join("".join(parser.parts).split())
    return {"url": str(response.url), "title": _page_title(response.text), "content": text[: args.max_chars], "truncated": len(text) > args.max_chars}


def _page_title(html: str) -> str:
    start = html.lower().find("<title")
    if start < 0:
        return ""
    start = html.find(">", start) + 1
    end = html.lower().find("</title>", start)
    return " ".join(html[start:end].split()) if end >= start else ""


WEB_TOOLS = [
    ToolDefinition("web_search", "Search the public web for current learning resources and optionally save the results to the focused plan.", WebSearchArgs, web_search),
    ToolDefinition("web_open", "Open a public HTTP(S) page and extract readable text for source verification.", WebOpenArgs, web_open),
]
