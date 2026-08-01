from html.parser import HTMLParser

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.config import settings
from app.models import LearningResource, Plan
from app.search import fetch_with_safe_redirects, get_search_provider
from app.tools.base import ToolContext, ToolDefinition


class WebSearchArgs(BaseModel):
    query: str = Field(min_length=2, max_length=300)
    limit: int = Field(default=5, ge=1, le=10)
    plan_id: int | None = None
    save_results: bool = False


class WebOpenArgs(BaseModel):
    url: str = Field(min_length=8, max_length=2000)
    max_chars: int = Field(default=12000, ge=500, le=30000)


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


async def web_search(ctx: ToolContext, args: WebSearchArgs) -> dict:
    if ctx.plan_id is not None and args.plan_id not in {None, ctx.plan_id}:
        return {"error": "Plan-focused runs cannot save resources to another plan"}
    target_plan = args.plan_id if args.plan_id is not None else ctx.plan_id
    if args.save_results and target_plan is not None:
        plan = await ctx.db.get(Plan, target_plan)
        if not plan or plan.owner_id != ctx.owner_id:
            return {"error": "Plan not found"}

    provider = get_search_provider(settings.WEB_SEARCH_PROVIDER)
    results = await provider.search(args.query, args.limit)
    result_rows = [result.as_dict() for result in results]
    saved_ids: list[int] = []
    if args.save_results:
        existing_urls = {
            item.url for item in (await ctx.db.execute(
                select(LearningResource).where(
                    LearningResource.owner_id == ctx.owner_id,
                    LearningResource.plan_id == target_plan,
                )
            )).scalars()
        }
        for result in result_rows:
            if result["url"] in existing_urls:
                continue
            resource = LearningResource(
                owner_id=ctx.owner_id,
                plan_id=target_plan,
                title=result["title"],
                url=result["url"],
                resource_type="web",
                summary=f"Search result for: {args.query}",
                source=f"web_search:{provider.name}",
            )
            ctx.db.add(resource)
            await ctx.db.flush()
            saved_ids.append(resource.id)
        await ctx.db.commit()
    return {
        "provider": provider.name,
        "query": args.query,
        "results": result_rows,
        "saved_resource_ids": saved_ids,
    }


async def web_open(_: ToolContext, args: WebOpenArgs) -> dict:
    headers = {"User-Agent": "Mozilla/5.0 LearningAgent/0.4"}
    async with httpx.AsyncClient(timeout=settings.WEB_SEARCH_TIMEOUT_SECONDS, headers=headers) as client:
        response, redirect_count = await fetch_with_safe_redirects(client, args.url)
    content_type = response.headers.get("content-type", "")
    if not any(kind in content_type for kind in ("text/", "json", "xml")):
        return {"error": f"Unsupported content type: {content_type}"}
    parser = _TextParser()
    parser.feed(response.text)
    text = " ".join("".join(parser.parts).split())
    return {
        "url": str(response.url),
        "title": _page_title(response.text),
        "content": text[: args.max_chars],
        "truncated": len(text) > args.max_chars,
        "redirect_count": redirect_count,
    }


def _page_title(html: str) -> str:
    start = html.lower().find("<title")
    if start < 0:
        return ""
    start = html.find(">", start) + 1
    end = html.lower().find("</title>", start)
    return " ".join(html[start:end].split()) if end >= start else ""


WEB_TOOLS = [
    ToolDefinition("web_search", "Search the public web through the configured provider and optionally save unique results to the focused plan.", WebSearchArgs, web_search),
    ToolDefinition("web_open", "Open a public HTTP(S) page, validate every redirect hop, and extract readable text for source verification.", WebOpenArgs, web_open),
]
