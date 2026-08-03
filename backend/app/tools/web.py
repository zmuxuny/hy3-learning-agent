from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Literal
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.config import settings
from app.models import LearningEvent, LearningResource, Operation, Plan
from app.search import fetch_with_safe_redirects, get_search_provider
from app.search.security import validate_public_url
from app.tools.base import ToolContext, ToolDefinition, json_safe


class WebSearchArgs(BaseModel):
    query: str = Field(min_length=2, max_length=300)
    limit: int = Field(default=5, ge=1, le=10)
    plan_id: int | None = None
    save_results: bool = False


class WebOpenArgs(BaseModel):
    url: str = Field(min_length=8, max_length=2000)
    max_chars: int = Field(default=12000, ge=500, le=30000)


class ResourceSaveArgs(BaseModel):
    plan_id: int
    title: str = Field(min_length=2, max_length=300)
    url: str = Field(min_length=8, max_length=2000)
    resource_type: Literal["course", "tutorial", "lab", "documentation", "video", "book", "repository", "curriculum"]
    provider: str = Field(default="", max_length=120)
    language: str = Field(default="", max_length=32)
    difficulty: Literal["beginner", "intermediate", "advanced", "mixed"] = "mixed"
    summary: str = Field(min_length=8, max_length=1200)
    why_recommended: str = Field(min_length=8, max_length=1200)


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

    primary_name = settings.WEB_SEARCH_PROVIDER
    fallback_name = settings.WEB_SEARCH_FALLBACK_PROVIDER
    provider = get_search_provider(primary_name)
    fallback_used = False
    try:
        results = await provider.search(args.query, args.limit)
    except Exception:
        if not fallback_name or fallback_name.lower() == "none" or fallback_name.lower() == primary_name.lower():
            raise
        provider = get_search_provider(fallback_name)
        results = await provider.search(args.query, args.limit)
        fallback_used = True
    if not results and fallback_name and fallback_name.lower() not in {"none", primary_name.lower()}:
        provider = get_search_provider(fallback_name)
        results = await provider.search(args.query, args.limit)
        fallback_used = True
    result_rows = []
    for result in results:
        row = result.as_dict()
        row.update(_catalog_metadata(result.url, result.title))
        result_rows.append(row)
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
                resource_type=result["resource_type"],
                provider=result["provider"],
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
        "fallback_used": fallback_used,
    }


async def resource_save(ctx: ToolContext, args: ResourceSaveArgs) -> dict:
    if ctx.plan_id is not None and args.plan_id != ctx.plan_id:
        return {"error": "Plan-focused runs cannot save resources to another plan"}
    plan = await ctx.db.get(Plan, args.plan_id)
    if not plan or plan.owner_id != ctx.owner_id:
        return {"error": "Plan not found"}
    if plan.status == "archived":
        return {"error": "Restore the plan before saving resources"}
    await validate_public_url(args.url)

    resource = (await ctx.db.execute(
        select(LearningResource).where(
            LearningResource.owner_id == ctx.owner_id,
            LearningResource.plan_id == args.plan_id,
            LearningResource.url == args.url,
        )
    )).scalars().one_or_none()
    created = resource is None
    if created:
        resource = LearningResource(owner_id=ctx.owner_id, plan_id=args.plan_id, title=args.title, url=args.url)
        ctx.db.add(resource)
        await ctx.db.flush()
        inverse = {"delete": resource.id}
    else:
        inverse = {"changes": {
            "title": resource.title,
            "resource_type": resource.resource_type,
            "provider": resource.provider,
            "language": resource.language,
            "difficulty": resource.difficulty,
            "summary": resource.summary,
            "why_recommended": resource.why_recommended,
            "source": resource.source,
            "verified_at": json_safe(resource.verified_at),
        }}

    provider = args.provider.strip() or _catalog_metadata(args.url, args.title)["provider"]
    changes = {
        "title": args.title,
        "resource_type": args.resource_type,
        "provider": provider,
        "language": args.language,
        "difficulty": args.difficulty,
        "summary": args.summary,
        "why_recommended": args.why_recommended,
        "source": "agent_curated",
        "verified_at": datetime.now(timezone.utc),
    }
    for field, value in changes.items():
        setattr(resource, field, value)
    operation = Operation(
        owner_id=ctx.owner_id,
        run_id=ctx.run_id,
        tool_name="resource.save",
        entity_type="learning_resource",
        entity_id=str(resource.id),
        forward_patch={"changes": json_safe(changes)},
        inverse_patch=inverse,
    )
    ctx.db.add_all([
        operation,
        LearningEvent(
            owner_id=ctx.owner_id,
            plan_id=args.plan_id,
            run_id=ctx.run_id,
            event_type="resource.curated",
            summary=f"Curated {args.resource_type}: {args.title}",
            payload={"resource_id": resource.id, "url": args.url, "provider": provider},
        ),
    ])
    await ctx.db.commit()
    return {
        "resource_id": resource.id,
        "plan_id": args.plan_id,
        "created": created,
        "operation_id": operation.id,
        "undo_available": True,
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


def _catalog_metadata(url: str, title: str = "") -> dict[str, str]:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    provider = host or "Web"
    providers = {
        "runoob.com": "菜鸟教程",
        "coursera.org": "Coursera",
        "huggingface.co": "Hugging Face",
        "kaggle.com": "Kaggle",
        "edx.org": "edX",
        "freecodecamp.org": "freeCodeCamp",
        "csdiy.wiki": "CS 自学指南",
        "youtube.com": "YouTube",
        "youtu.be": "YouTube",
        "bilibili.com": "哔哩哔哩",
    }
    for domain, name in providers.items():
        if host == domain or host.endswith(f".{domain}"):
            provider = name
            break
    if "stanford" in host or "cs336" in host:
        provider = "Stanford"

    value = f"{host} {urlparse(url).path} {title}".lower()
    if "kaggle.com/learn" in value or "lab" in value:
        resource_type = "lab"
    elif "stanford" in host or "cs336" in value:
        resource_type = "course"
    elif any(domain in host for domain in ("coursera.org", "edx.org")) or "/course" in value or "/learn" in value:
        resource_type = "course"
    elif "runoob.com" in host or "tutorial" in value:
        resource_type = "tutorial"
    elif any(domain in host for domain in ("youtube.com", "youtu.be", "bilibili.com")):
        resource_type = "video"
    elif "github.com" in host:
        resource_type = "repository"
    elif "csdiy.wiki" in host or "curriculum" in value:
        resource_type = "curriculum"
    elif "docs" in value or "documentation" in value:
        resource_type = "documentation"
    else:
        resource_type = "tutorial"
    return {"provider": provider, "resource_type": resource_type}


WEB_TOOLS = [
    ToolDefinition("web_search", "Search the public web. Results include inferred provider and resource type; raw auto-save is for capture only, not curriculum curation.", WebSearchArgs, web_search, idempotent=True),
    ToolDefinition("web_open", "Open a public HTTP(S) page, validate every redirect hop, and extract readable text for source verification.", WebOpenArgs, web_open),
    ToolDefinition("resource_save", "Save or update one deliberately selected learning resource after opening it. Record its course/tutorial type, level, language, summary, and why it fits this plan.", ResourceSaveArgs, resource_save, idempotent=True),
]
